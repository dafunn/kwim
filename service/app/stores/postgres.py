"""PostgreSQL store - the durable system-of-record: episodic events + commit log.

Tenancy: schema-per-team. Every statement is scoped to the caller's `team` schema,
and the team name is validated as an identifier (it comes from the auth layer, not
user input, but defense-in-depth - schema names can't be parameterized in SQL).
"""
import json
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..config import settings

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def _schema(team: str) -> str:
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")
    return team


class PostgresStore:
    def __init__(self) -> None:
        self._pool: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        # Discrete kwargs (no URL DSN) so special chars in the password can't
        # corrupt parsing. One long-lived async connection is enough for the
        # service's volume; swap for psycopg_pool.AsyncConnectionPool if needed.
        self._pool = await psycopg.AsyncConnection.connect(
            host=settings.pg_host, port=settings.pg_port, dbname=settings.pg_db,
            user=settings.pg_user, password=settings.pg_password, autocommit=True,
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def append_episodic(self, team: str, ev: dict[str, Any]) -> str:
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
                             reason: str = "bulk cleanup") -> int:
        """Resolve unresolved pending proposals as 'rejected' (the governed 'no',
        same as a human clicking Reject) - clears stale/duplicate queue items without
        a hard delete, keeping the audit trail. Optional source_kind filter. Returns
        the number rejected."""
        s = _schema(team)
        clause, params = "resolved_at IS NULL", [reason]
        if source_kind:
            clause += " AND body->>'source_kind' = %s"
            params.append(source_kind)
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE {s}.pending_proposals SET resolved_at=now(), resolution='rejected', "
                f"resolved_by='cleanup', resolved_via='api', reject_reason=%s WHERE {clause}",
                params)
            return cur.rowcount

    async def delete_preflight(self, team: str) -> dict[str, Any]:
        """Read-only: does the connected role have DELETE on the team's commit_log +
        episodic_events? Lets the forget path validate its DB permissions without
        mutating anything (has_table_privilege is a pure read) - both the CLI dry-run
        and the inline Forget button preflight through here before any delete."""
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT current_user AS role, "
                "has_table_privilege(%s, 'DELETE') AS commit_log, "
                "has_table_privilege(%s, 'DELETE') AS episodic",
                (f"{s}.commit_log", f"{s}.episodic_events"))
            return dict(await cur.fetchone())

    async def delete_commit_log(self, team: str, object_id: str) -> int:
        """DESTRUCTIVE (forget path only): remove all commit_log rows for an object_id
        - no surviving tombstone. Returns rows deleted."""
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"DELETE FROM {s}.commit_log WHERE object_id = %s", (object_id,))
            return cur.rowcount

    async def delete_episodic(self, team: str, episodic_ids: list[str]) -> int:
        """DESTRUCTIVE (forget path only): remove episodic_event rows by id. Returns
        rows deleted. Callers must apply the shared-evidence guard first."""
        if not episodic_ids:
            return 0
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"DELETE FROM {s}.episodic_events WHERE id = ANY(%s::uuid[])", (episodic_ids,))
            return cur.rowcount

    async def append_commit(self, team: str, row: dict[str, Any]) -> int:
        """Append one governed change to the commit log. Returns the seq."""
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name = 'commit_log' AND table_schema NOT IN "
                "('public', 'information_schema', 'pg_catalog', 'pg_toast')"
            )
            return [r["table_schema"] for r in await cur.fetchall()]

    async def replay_commit_log(self, team: str) -> list[dict]:
        """Read <team>.commit_log in seq order for replay."""
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT seq, object_type, object_id, operation, payload, provenance, "
                f"       proposed_by, source_kind, gate_decision "
                f"FROM {s}.commit_log ORDER BY seq ASC"
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def episodic_with_text(self, team: str) -> list[dict]:
        """Return episodic events carrying non-empty text (for re-embed).

        Skips archived rows (text may have been compacted into compressed_summary).
        """
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        order: str = "asc",
    ) -> list[dict]:
        """Windowed, team-scoped read over episodic_events on the (occurred_at, id) cursor.

        `order="asc"` (default): `(occurred_at, id) > (since_ts, since_id)` is an exclusive
        lower bound - a stable total order over rows sharing one occurred_at timestamp.
        `order="desc"`: the cursor is an exclusive *upper* bound (`<`), and rows come back
        newest-first - e.g. `limit=1` with no cursor fetches the single latest event in O(1),
        regardless of how many rows exist (the watermark lookup's use case). Cursor is
        omitted entirely when since_ts is None (read from start/end). Excludes archived rows
        (N6 sweeper).
        """
        s = _schema(team)
        clauses = ["NOT archived"]
        params: list[Any] = []
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

        direction = "DESC" if order == "desc" else "ASC"
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id, agent_id, session_id, event_type, event_data, occurred_at "
                f"FROM {s}.episodic_events "
                f"WHERE {' AND '.join(clauses)} "
                f"ORDER BY occurred_at {direction}, id {direction} LIMIT %s",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def evidence_meta(self, team: str, ids: list[str]) -> list[dict]:
        """Return [{id, session_id, agent_id}] for the given episodic event ids.

        Archived rows are included - the event existed even if compacted. Only ids
        that exist in the table are returned; missing ids are simply absent from the
        result (gate uses the gap to detect fabricated or in-flight evidence).
        """
        if not ids:
            return []
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        if no row matched (already resolved or unknown id - caller distinguishes
        via get_pending)."""
        s = _schema(team)
        async with self._pool.cursor(row_factory=dict_row) as cur:
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
        async with self._pool.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id, agent_id, event_type, event_data, occurred_at "
                f"FROM {s}.episodic_events WHERE session_id = %s "
                "ORDER BY occurred_at DESC LIMIT %s",
                (session_id, limit),
            )
            return [dict(r) for r in await cur.fetchall()]
