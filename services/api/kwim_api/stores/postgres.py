"""PostgreSQL store - the durable system-of-record: episodic events + commit log.

Every statement runs in the caller's `team` schema; the team name is validated as
an identifier, since schema names cannot be bound as parameters.
"""
import json
import logging
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .pg_pool import cursor, open_pool

log = logging.getLogger(__name__)

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def _schema(team: str) -> str:
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")
    return team


class PostgresStore:
    def __init__(self) -> None:
        self._pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        self._pool = await open_pool("kwim-pg")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def append_episodic(self, team: str, ev: dict[str, Any]) -> str:
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO {s}.episodic_events (agent_id, session_id, event_type, event_data) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (ev["agent_id"], ev["session_id"], ev["event_type"],
                 json.dumps(ev.get("event_data", {}))),
            )
            return str((await cur.fetchone())["id"])

    async def pending_stats(self, team: str, *, source_kind: str | None = None) -> dict[str, Any]:
        """Count + sample of unresolved pending review proposals, optionally filtered
        to one proposer source_kind. Read-only - backs the queue-cleanup dry-run."""
        s = _schema(team)
        clause, params = "resolved_at IS NULL", []
        if source_kind:
            clause += " AND body->>'source_kind' = %s"
            params.append(source_kind)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT count(*) AS n, count(DISTINCT body->>'statement') AS distinct_n "
                f"FROM {s}.pending_proposals WHERE {clause}", params)
            row = await cur.fetchone()
            await cur.execute(
                f"SELECT left(body->>'statement', 80) AS stmt FROM {s}.pending_proposals "
                f"WHERE {clause} ORDER BY created_at DESC LIMIT 10", params)
            sample = [r["stmt"] for r in await cur.fetchall()]
        return {"count": row["n"], "distinct": row["distinct_n"], "sample": sample}

    async def reject_pending(self, team: str, *, source_kind: str | None = None,
                             reason: str = "bulk cleanup",
                             resolved_by: str = "cleanup", resolved_via: str = "api",
                             older_than: Any = None) -> int:
        """Resolve unresolved pending proposals as 'rejected' (the governed 'no',
        as a human Reject does). Optional source_kind and older_than filters. Returns
        the number rejected."""
        s = _schema(team)
        clause, params = "resolved_at IS NULL", [resolved_by, resolved_via, reason]
        if source_kind:
            clause += " AND body->>'source_kind' = %s"
            params.append(source_kind)
        if older_than is not None:
            clause += " AND created_at < %s"
            params.append(older_than)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE {s}.pending_proposals SET resolved_at=now(), resolution='rejected', "
                f"resolved_by=%s, resolved_via=%s, reject_reason=%s WHERE {clause}",
                params)
            return cur.rowcount

    async def delete_preflight(self, team: str) -> dict[str, Any]:
        """Read-only: does the connected role have DELETE on the team's commit_log,
        episodic_events, and fact_verifications? `fact_verifications` reports True
        when the table does not exist.
        """
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT current_user AS role, "
                "has_table_privilege(%s, 'DELETE') AS commit_log, "
                "has_table_privilege(%s, 'DELETE') AS episodic, "
                "CASE WHEN to_regclass(%s) IS NULL THEN true "
                "     ELSE has_table_privilege(%s, 'DELETE') END AS verifications",
                (f"{s}.commit_log", f"{s}.episodic_events",
                 f"{s}.fact_verifications", f"{s}.fact_verifications"))
            return dict(await cur.fetchone())

    async def delete_commit_log(self, team: str, object_id: str) -> int:
        """DESTRUCTIVE (forget path only): remove all commit_log rows for an object_id
        - no surviving tombstone. Returns rows deleted."""
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"DELETE FROM {s}.commit_log WHERE object_id = %s", (object_id,))
            return cur.rowcount

    async def delete_episodic(self, team: str, episodic_ids: list[str]) -> int:
        """DESTRUCTIVE (forget path only): remove episodic_event rows by id. Returns
        rows deleted. Callers must apply the shared-evidence guard first."""
        if not episodic_ids:
            return 0
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"DELETE FROM {s}.episodic_events WHERE id = ANY(%s::uuid[])", (episodic_ids,))
            return cur.rowcount

    async def append_commit(self, team: str, row: dict[str, Any]) -> int:
        """Append one governed change to the commit log. Returns the seq."""
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO {s}.commit_log "
                "(object_type, object_id, operation, payload, provenance, proposed_by, "
                " source_kind, gate_decision) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING seq",
                (row["object_type"], row["object_id"], row["operation"],
                 json.dumps(row.get("payload", {})), json.dumps(row.get("provenance", {})),
                 row.get("proposed_by"), row.get("source_kind"), row["gate_decision"]),
            )
            return int((await cur.fetchone())["seq"])

    async def list_team_schemas(self) -> list[str]:
        """Return every schema in the kwim DB that has a commit_log table."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name = 'commit_log' AND table_schema NOT IN "
                "('public', 'information_schema', 'pg_catalog', 'pg_toast')"
            )
            return [r["table_schema"] for r in await cur.fetchall()]

    # --- Team provisioning -------------------

    async def create_team_preflight(self) -> dict[str, Any]:
        """Read-only: can the connected role create a schema? Lets team creation
        fail fast (503) before rendering or applying any DDL."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT current_user AS role, "
                "has_database_privilege(current_user, current_database(), 'CREATE') "
                "AS can_create")
            return await cur.fetchone()

    async def destroy_team_preflight(self, team: str) -> dict[str, Any]:
        """Read-only: can the connected role drop the team's schema? Schema
        Dropping needs ownership or superuser, so this checks pg_namespace and
        pg_has_role. A missing schema reports can_drop from superuser alone."""
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT current_user AS role, "
                "COALESCE((SELECT usesuper FROM pg_user WHERE usename = current_user), "
                "         false) AS is_superuser, "
                "EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = %s "
                "        AND pg_has_role(current_user, n.nspowner, 'MEMBER')) AS owns_schema",
                (s,))
            row = await cur.fetchone()
            return {"role": row["role"],
                    "can_drop": bool(row["is_superuser"] or row["owns_schema"])}

    async def apply_team_schema(self, rendered_sql: str, *, team: str | None = None) -> None:
        """Apply an already-rendered, fully-substituted team schema script as one
        multi-statement execution inside an explicit transaction. No bind
        parameters, so psycopg uses the simple query protocol, which accepts
        several statements. See docs/DESIGN.md, "Provisioning teams".
        """
        log.info("applying team schema DDL: team=%s", team)
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(rendered_sql)

    async def drop_team_schema(self, team: str) -> None:
        """Destructive (team destroy only): drop the team's schema and everything
        in it. Callers run destroy_team_preflight and drop the graphs first."""
        s = _schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")

    async def replay_commit_log(self, team: str) -> list[dict]:
        """Read <team>.commit_log in seq order for replay.

        Replay restores each node's created_at from `committed_at`.
        """
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT seq, committed_at, object_type, object_id, operation, payload, "
                f"       provenance, proposed_by, source_kind, gate_decision "
                f"FROM {s}.commit_log ORDER BY seq ASC"
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    _COMMIT_LOG_FILTERS = ("object_id", "object_type", "operation", "source_kind", "gate_decision")

    def _commit_log_where(self, filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
        clauses, params = [], []
        for key in self._COMMIT_LOG_FILTERS:
            val = filters.get(key)
            if val is not None:
                clauses.append(f"{key} = %s")
                params.append(val)
        return clauses, params

    async def read_commit_log(
        self, team: str, *, filters: dict[str, Any] | None = None,
        since_seq: int | None = None, order: str = "asc", limit: int = 100,
    ) -> list[dict]:
        """Filtered, paginated read over <team>.commit_log - the admin browse primitive.

        `since_seq` is exclusive: a lower bound for order="asc" (default), an upper
        bound for order="desc".
        """
        s = _schema(team)
        clauses, params = self._commit_log_where(filters or {})
        if since_seq is not None:
            op = "<" if order == "desc" else ">"
            clauses.append(f"seq {op} %s")
            params.append(since_seq)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        direction = "DESC" if order == "desc" else "ASC"
        params.append(limit)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT seq, committed_at, object_type, object_id, operation, payload, "
                f"       provenance, proposed_by, source_kind, gate_decision "
                f"FROM {s}.commit_log {where}"
                f"ORDER BY seq {direction} LIMIT %s",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def count_commit_log(self, team: str, *, filters: dict[str, Any] | None = None) -> int:
        s = _schema(team)
        clauses, params = self._commit_log_where(filters or {})
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with cursor(self._pool) as cur:
            await cur.execute(f"SELECT count(*) FROM {s}.commit_log {where}", params)
            return (await cur.fetchone())[0]

    async def list_proposals(
        self, team: str, *, resolved: str = "false", resolution: str | None = None,
        object_type: str | None = None, source_kind: str | None = None,
        cursor: dict[str, Any] | None = None, limit: int = 50,
    ) -> list[dict]:
        """Proposal browse, resolved and unresolved (the rejection audit trail).

        Newest first (`created_at DESC, proposal_id`); `cursor` is `{created_at, id}`,
        an exclusive bound in that order.
        """
        s = _schema(team)
        clauses, params = [], []
        if resolved == "false":
            clauses.append("resolved_at IS NULL")
        elif resolved == "true":
            clauses.append("resolved_at IS NOT NULL")
        if resolution is not None:
            clauses.append("resolution = %s")
            params.append(resolution)
        if object_type is not None:
            clauses.append("object_type = %s")
            params.append(object_type)
        if source_kind is not None:
            # The same expression pending_stats and reject_pending filter on.
            clauses.append("body->>'source_kind' = %s")
            params.append(source_kind)
        if cursor is not None:
            clauses.append("(created_at, proposal_id) < (%s, %s)")
            params.extend([cursor["created_at"], cursor["id"]])
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT proposal_id, object_type, proposed_by, body, bus_message, "
                f"       created_at, resolved_at, resolution, resolved_by, "
                f"       resolved_via, reject_reason "
                f"FROM {s}.pending_proposals {where}"
                f"ORDER BY created_at DESC, proposal_id DESC LIMIT %s",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def count_proposals(
        self, team: str, *, resolved: str = "false", resolution: str | None = None,
        object_type: str | None = None, source_kind: str | None = None,
    ) -> int:
        s = _schema(team)
        clauses, params = [], []
        if resolved == "false":
            clauses.append("resolved_at IS NULL")
        elif resolved == "true":
            clauses.append("resolved_at IS NOT NULL")
        if resolution is not None:
            clauses.append("resolution = %s")
            params.append(resolution)
        if object_type is not None:
            clauses.append("object_type = %s")
            params.append(object_type)
        if source_kind is not None:
            clauses.append("body->>'source_kind' = %s")
            params.append(source_kind)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with cursor(self._pool) as cur:
            await cur.execute(f"SELECT count(*) FROM {s}.pending_proposals {where}", params)
            return (await cur.fetchone())[0]

    async def record_verification(self, team: str, fact_id: str, verified_at: Any,
                                  verified_by: str | None = None) -> bool:
        """Upsert a fact's last_verified_at - the durable half of reaffirm.

        One row per fact. Returns False when the team schema has no such table.
        """
        s = _schema(team)
        try:
            async with cursor(self._pool, row_factory=dict_row) as cur:
                await cur.execute(
                    f"INSERT INTO {s}.fact_verifications (fact_id, last_verified_at, verified_by) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (fact_id) DO UPDATE SET "
                    "  last_verified_at = EXCLUDED.last_verified_at, "
                    "  verified_by = EXCLUDED.verified_by",
                    (fact_id, verified_at, verified_by),
                )
            return True
        except psycopg.errors.UndefinedTable:
            log.warning("record_verification: %s.fact_verifications missing - "
                        "run the team provisioner; verification not durable", s)
            return False

    async def read_verifications(self, team: str) -> list[dict]:
        """Every fact's last_verified_at, for reapplication after a replay.

        Returns [] when the table is absent.
        """
        s = _schema(team)
        try:
            async with cursor(self._pool, row_factory=dict_row) as cur:
                await cur.execute(
                    f"SELECT fact_id, last_verified_at, verified_by "
                    f"FROM {s}.fact_verifications"
                )
                return [dict(r) for r in await cur.fetchall()]
        except psycopg.errors.UndefinedTable:
            log.warning("read_verifications: %s.fact_verifications missing - "
                        "run the team provisioner; last_verified_at not restored", s)
            return []

    async def delete_verifications(self, team: str, fact_ids: list[str]) -> int:
        """Destructive (forget path only): drop verification rows for forgotten facts
        so no state outlives the object. Returns rows deleted."""
        if not fact_ids:
            return 0
        s = _schema(team)
        try:
            async with cursor(self._pool, row_factory=dict_row) as cur:
                await cur.execute(
                    f"DELETE FROM {s}.fact_verifications WHERE fact_id = ANY(%s)",
                    (fact_ids,))
                return cur.rowcount
        except psycopg.errors.UndefinedTable:
            return 0

    async def episodic_with_text(self, team: str) -> list[dict]:
        """Return episodic events carrying non-empty text (for re-embed).

        Skips archived rows (text may have been compacted into compressed_summary).
        """
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id, agent_id, session_id, event_type, event_data, occurred_at "
                f"FROM {s}.episodic_events "
                f"WHERE event_data->>'text' IS NOT NULL "
                f"  AND trim(event_data->>'text') != ''"
                f"  AND (archived IS NOT TRUE) "
                f"ORDER BY occurred_at ASC"
            )
            return [dict(r) for r in await cur.fetchall()]

    async def read_episodic(
        self, team: str, since_ts: Any = None, since_id: str | None = None,
        limit: int = 500, event_type: str | None = None, agent_id: str | None = None,
        order: str = "asc", archived: str = "false",
    ) -> list[dict]:
        """Windowed, team-scoped read over episodic_events on the (occurred_at, id) cursor.

        The cursor is exclusive: a lower bound for `order="asc"` (default), an upper
        bound for `order="desc"`; with no since_ts, reads from the start or end.
        `archived`: "false" (default) excludes archived rows, "true" returns only
        them, "any" both.
        """
        s = _schema(team)
        clauses: list[str] = []
        params: list[Any] = []
        if archived == "false":
            clauses.append("NOT archived")
        elif archived == "true":
            clauses.append("archived")
        if since_ts is not None:
            op = "<" if order == "desc" else ">"
            clauses.append(f"(occurred_at, id) {op} (%s, %s)")
            params.extend([since_ts, since_id])
        if event_type is not None:
            clauses.append("event_type = %s")
            params.append(event_type)
        if agent_id is not None:
            clauses.append("agent_id = %s")
            params.append(agent_id)
        params.append(limit)

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        direction = "DESC" if order == "desc" else "ASC"
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id, agent_id, session_id, event_type, event_data, occurred_at, archived "
                f"FROM {s}.episodic_events {where}"
                f"ORDER BY occurred_at {direction}, id {direction} LIMIT %s",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def count_episodic(self, team: str) -> int:
        """Live (non-archived) episodic row count - the team-summary counts block."""
        s = _schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(f"SELECT count(*) FROM {s}.episodic_events WHERE NOT archived")
            return (await cur.fetchone())[0]

    async def evidence_meta(self, team: str, ids: list[str]) -> list[dict]:
        """Return [{id, session_id, agent_id}] for the given episodic event ids.

        Includes archived rows; ids not in the table are absent from the result.
        """
        if not ids:
            return []
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id::text, session_id, agent_id "
                f"FROM {s}.episodic_events WHERE id = ANY(%s::uuid[])",
                (ids,),
            )
            return [{"id": r["id"], "session_id": r["session_id"],
                     "agent_id": r["agent_id"]} for r in await cur.fetchall()]

    async def insert_pending(self, team: str, row: dict[str, Any]) -> None:
        """Persist a proposal routed to human review."""
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO {s}.pending_proposals "
                "(proposal_id, object_type, proposed_by, body, bus_message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (row["proposal_id"], row["object_type"], row.get("proposed_by"),
                 json.dumps(row["body"]), json.dumps(row["bus_message"])),
            )

    async def list_pending(self, team: str, limit: int = 50) -> list[dict]:
        """Open (unresolved) review queue, oldest first."""
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT proposal_id, object_type, proposed_by, body, bus_message, "
                f"       created_at, resolved_at, resolution, resolved_by, "
                f"       resolved_via, reject_reason "
                f"FROM {s}.pending_proposals WHERE resolved_at IS NULL "
                f"ORDER BY created_at ASC LIMIT %s",
                (limit,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_pending(self, team: str, proposal_id: str) -> dict | None:
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT proposal_id, object_type, proposed_by, body, bus_message, "
                f"       created_at, resolved_at, resolution, resolved_by, "
                f"       resolved_via, reject_reason "
                f"FROM {s}.pending_proposals WHERE proposal_id = %s",
                (proposal_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def claim_pending(
        self, team: str, proposal_id: str, resolution: str, resolved_by: str,
        resolved_via: str, reject_reason: str | None = None,
    ) -> dict | None:
        """Atomically resolve a pending proposal. Returns the claimed row, or None
        if none matched (already resolved or unknown)."""
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE {s}.pending_proposals "
                f"SET resolved_at = now(), resolution = %s, resolved_by = %s, "
                f"    resolved_via = %s, reject_reason = %s "
                f"WHERE proposal_id = %s AND resolved_at IS NULL "
                f"RETURNING proposal_id, object_type, proposed_by, body, bus_message, "
                f"          created_at, resolved_at, resolution, resolved_by, "
                f"          resolved_via, reject_reason",
                (resolution, resolved_by, resolved_via, reject_reason, proposal_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def recent_episodic(self, team: str, session_id: str, limit: int = 20) -> list[dict]:
        s = _schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id, agent_id, event_type, event_data, occurred_at "
                f"FROM {s}.episodic_events WHERE session_id = %s "
                "ORDER BY occurred_at DESC LIMIT %s",
                (session_id, limit),
            )
            return [dict(r) for r in await cur.fetchall()]
