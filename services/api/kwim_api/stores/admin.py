"""Postgres store for `kwim_admin` - cluster-wide operator identity, the API key
store, and the audit log. One schema for the whole cluster; `team` is column data,
never part of a SQL identifier.
"""
import json
import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .pg_pool import cursor, open_pool

log = logging.getLogger(__name__)


class AdminStore:
    def __init__(self) -> None:
        self._pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        self._pool = await open_pool("kwim-admin")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def get_api_key(self, key_prefix: str) -> dict[str, Any] | None:
        """The live row for `key_prefix` - not revoked, not expired - or None."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, team, label, key_prefix, key_hash, capabilities, "
                "created_at, expires_at, revoked_at, last_used_at "
                "FROM kwim_admin.api_keys "
                "WHERE key_prefix = %s AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())",
                (key_prefix,),
            )
            return await cur.fetchone()

    async def touch_api_key_last_used(self, key_prefix: str) -> None:
        async with cursor(self._pool) as cur:
            await cur.execute(
                "UPDATE kwim_admin.api_keys SET last_used_at = now() WHERE key_prefix = %s",
                (key_prefix,),
            )

    async def create_api_key(self, *, team: str, label: str, key_prefix: str, key_hash: str,
                             capabilities: list[str], created_by: str | None = None,
                             expires_at: Any = None) -> dict[str, Any]:
        """Insert a new key row. Returns the row - never the secret, which isn't stored."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO kwim_admin.api_keys "
                "(team, label, key_prefix, key_hash, capabilities, created_by, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, team, label, key_prefix, capabilities, created_at, "
                "created_by, expires_at, revoked_at, last_used_at",
                (team, label, key_prefix, key_hash, capabilities, created_by, expires_at),
            )
            return await cur.fetchone()

    async def list_api_keys(self, *, team: str | None = None,
                            include_revoked: bool = False) -> list[dict[str, Any]]:
        """Key rows for the operator CLI and the console. Never returns key_hash -
        nothing outside resolution has a reason to see it."""
        clauses, params = [], []
        if team is not None:
            clauses.append("team = %s")
            params.append(team)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, team, label, key_prefix, capabilities, created_at, "
                "created_by, expires_at, revoked_at, last_used_at "
                f"FROM kwim_admin.api_keys {where}ORDER BY team, created_at",
                params,
            )
            return list(await cur.fetchall())

    async def revoke_api_key(self, key_prefix: str) -> dict[str, Any] | None:
        """Revoke a live key. Returns its (id, team, label, revoked_at) for the
        audit trail and API response, or None if no live key had that prefix.
        In-process callers also call `auth.invalidate_key_cache(key_prefix)`.
        """
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE kwim_admin.api_keys SET revoked_at = now() "
                "WHERE key_prefix = %s AND revoked_at IS NULL "
                "RETURNING id, team, label, revoked_at",
                (key_prefix,),
            )
            return await cur.fetchone()

    async def revoke_all_team_keys(self, team: str) -> list[dict[str, Any]]:
        """Revoke every live key for a team in one statement (team.decommission).
        Returns the revoked rows so the caller can invalidate each prefix's
        resolver cache entry."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE kwim_admin.api_keys SET revoked_at = now() "
                "WHERE team = %s AND revoked_at IS NULL "
                "RETURNING id, key_prefix, label, revoked_at",
                (team,),
            )
            return list(await cur.fetchall())

    async def api_key_labels(self, *, team: str | None = None) -> set[str]:
        """Labels already present, live or revoked. The legacy import uses this to
        skip entries it has already imported."""
        clause, params = ("WHERE team = %s ", [team]) if team is not None else ("", [])
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"SELECT label FROM kwim_admin.api_keys {clause}", params)
            return {r[0] for r in await cur.fetchall()}

    # --- Operators ---------------------------------------------------------

    async def count_operators(self) -> int:
        async with cursor(self._pool) as cur:
            await cur.execute("SELECT count(*) FROM kwim_admin.operators")
            return (await cur.fetchone())[0]

    async def get_operator_by_username(self, username: str) -> dict[str, Any] | None:
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, username, password_hash, is_active "
                "FROM kwim_admin.operators WHERE username = %s",
                (username,),
            )
            return await cur.fetchone()

    async def create_operator(self, *, username: str, password_hash: str,
                              display_name: str | None = None) -> dict[str, Any]:
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO kwim_admin.operators (username, password_hash, display_name) "
                "VALUES (%s, %s, %s) RETURNING id, username, display_name, created_at",
                (username, password_hash, display_name),
            )
            return await cur.fetchone()

    async def list_operators(self) -> list[dict[str, Any]]:
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, username, display_name, is_active, created_at, last_login_at "
                "FROM kwim_admin.operators ORDER BY username")
            return list(await cur.fetchall())

    async def touch_operator_login(self, operator_id: str) -> None:
        async with cursor(self._pool) as cur:
            await cur.execute(
                "UPDATE kwim_admin.operators SET last_login_at = now() WHERE id = %s",
                (operator_id,),
            )

    # --- Sessions ------------------------------------------------------------

    async def create_session(self, *, operator_id: str, token_hash: str, expires_at: Any,
                             user_agent: str | None = None) -> dict[str, Any]:
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO kwim_admin.sessions (operator_id, token_hash, expires_at, user_agent) "
                "VALUES (%s, %s, %s, %s) RETURNING id, operator_id, created_at, expires_at",
                (operator_id, token_hash, expires_at, user_agent),
            )
            return await cur.fetchone()

    async def get_session(self, token_hash: str) -> dict[str, Any] | None:
        """The live session for `token_hash`, joined to its operator - not revoked,
        not expired, operator still active - or None."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT s.id, s.operator_id, o.username, s.created_at, s.expires_at "
                "FROM kwim_admin.sessions s JOIN kwim_admin.operators o ON o.id = s.operator_id "
                "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now() "
                "AND o.is_active",
                (token_hash,),
            )
            return await cur.fetchone()

    async def refresh_session(self, token_hash: str, expires_at: Any) -> None:
        async with cursor(self._pool) as cur:
            await cur.execute(
                "UPDATE kwim_admin.sessions SET expires_at = %s WHERE token_hash = %s",
                (expires_at, token_hash),
            )

    async def revoke_session(self, token_hash: str) -> dict[str, Any] | None:
        """Revoke the live session for `token_hash`. Returns its (id, operator_id)
        for the audit trail, or None if nothing matched - logout is idempotent."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE kwim_admin.sessions SET revoked_at = now() "
                "WHERE token_hash = %s AND revoked_at IS NULL "
                "RETURNING id, operator_id",
                (token_hash,),
            )
            return await cur.fetchone()

    async def list_audit(
        self, *, team: str | None = None, operator_id: str | None = None,
        action: str | None = None, since: Any = None, cursor: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Cluster-wide audit browse, newest first (`seq DESC`). `cursor` is the
        prior page's last `seq` - an exclusive upper bound, matching the descending
        order. Every mutation writes here (`record`); this is the read side."""
        clauses, params = [], []
        if team is not None:
            clauses.append("team = %s")
            params.append(team)
        if operator_id is not None:
            clauses.append("operator_id = %s")
            params.append(operator_id)
        if action is not None:
            clauses.append("action = %s")
            params.append(action)
        if since is not None:
            clauses.append("at >= %s")
            params.append(since)
        if cursor is not None:
            clauses.append("seq < %s")
            params.append(cursor)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT seq, at, operator_id, action, team, object_type, object_id, "
                "detail, result FROM kwim_admin.audit_log "
                f"{where}ORDER BY seq DESC LIMIT %s",
                params,
            )
            return list(await cur.fetchall())

    # --- Team registry (kwim_admin.teams) ---------------------------------

    async def list_teams(self) -> list[dict[str, Any]]:
        """Every console-registered team - one half of the GET /v1/admin/teams
        reconciliation; `list_team_schemas()` on PostgresStore is the other."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT team, display_name, status, created_at, created_by, "
                "decommissioned_at FROM kwim_admin.teams ORDER BY team")
            return list(await cur.fetchall())

    async def get_team(self, team: str) -> dict[str, Any] | None:
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT team, display_name, status, created_at, created_by, "
                "decommissioned_at FROM kwim_admin.teams WHERE team = %s", (team,))
            return await cur.fetchone()

    async def create_team_record(self, team: str, *, display_name: str | None = None,
                                 created_by: str | None = None) -> dict[str, Any]:
        """Insert the console record for a team, idempotently. Used by team
        creation and by /adopt. Never changes `status`."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO kwim_admin.teams (team, display_name, created_by) "
                "VALUES (%s, %s, %s) ON CONFLICT (team) DO NOTHING "
                "RETURNING team, display_name, status, created_at, created_by, "
                "decommissioned_at",
                (team, display_name, created_by),
            )
            row = await cur.fetchone()
            return row if row is not None else await self.get_team(team)

    async def set_team_status(self, team: str, status: str, *,
                              decommissioned_at: Any = None) -> dict[str, Any]:
        """Upsert the console record's lifecycle status (team.decommission,
        team.restore), creating the record for a team that was never adopted."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO kwim_admin.teams (team, status, decommissioned_at) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (team) DO UPDATE SET "
                "  status = EXCLUDED.status, "
                "  decommissioned_at = EXCLUDED.decommissioned_at "
                "RETURNING team, display_name, status, created_at, created_by, "
                "decommissioned_at",
                (team, status, decommissioned_at),
            )
            return await cur.fetchone()

    async def delete_team_record(self, team: str) -> None:
        """Destructive (team destroy only): delete the team's console record."""
        async with cursor(self._pool) as cur:
            await cur.execute("DELETE FROM kwim_admin.teams WHERE team = %s", (team,))

    # --- Audit log -------------------------------------------------------------

    async def record(self, *, operator_id: str | None, action: str, result: str,
                     team: str | None = None, object_type: str | None = None,
                     object_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        """Append one audit_log row. Never raises: a failed write must not fail the
        request; a failure is logged as a warning."""
        try:
            async with cursor(self._pool) as cur:
                await cur.execute(
                    "INSERT INTO kwim_admin.audit_log "
                    "(operator_id, action, team, object_type, object_id, detail, result) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (operator_id, action, team, object_type, object_id,
                     json.dumps(detail or {}), result),
                )
        except Exception:
            log.warning("audit log write failed: action=%s result=%s", action, result, exc_info=True)

    # --- Background jobs (kwim_admin.jobs) -------------------------------------

    async def create_job(self, *, kind: str, team: str, operator_id: str | None = None,
                         detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Insert a running job row. The unique partial index on (team, kind) WHERE
        status='running' raises IntegrityError if one is already running."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO kwim_admin.jobs (kind, team, status, operator_id, detail) "
                "VALUES (%s, %s, 'running', %s, %s) "
                "RETURNING id, kind, team, status, started_at, operator_id, detail",
                (kind, team, operator_id, json.dumps(detail or {})),
            )
            return await cur.fetchone()

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Fetch one job row by id."""
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, kind, team, status, started_at, finished_at, operator_id, detail "
                "FROM kwim_admin.jobs WHERE id = %s",
                (job_id,),
            )
            return await cur.fetchone()

    async def list_jobs(self, *, team: str | None = None, status: str | None = None,
                        limit: int = 50) -> list[dict[str, Any]]:
        """List jobs, newest first, optionally filtered."""
        clauses, params = [], []
        if team is not None:
            clauses.append("team = %s")
            params.append(team)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, kind, team, status, started_at, finished_at, operator_id, detail "
                f"FROM kwim_admin.jobs {where}ORDER BY started_at DESC LIMIT %s",
                params,
            )
            return list(await cur.fetchall())

    async def finish_job(self, job_id: str, status: str,
                         detail: dict[str, Any] | None = None) -> None:
        """Mark a job succeeded or failed."""
        async with cursor(self._pool) as cur:
            await cur.execute(
                "UPDATE kwim_admin.jobs SET status = %s, finished_at = now(), detail = %s "
                "WHERE id = %s",
                (status, json.dumps(detail or {}), job_id),
            )

    async def fail_orphaned_jobs(self) -> int:
        """Mark every running job as failed (startup cleanup). Returns count.

        Returns 0 when the schema is absent.
        """
        try:
            async with cursor(self._pool) as cur:
                await cur.execute(
                    "UPDATE kwim_admin.jobs SET status = 'failed', finished_at = now(), "
                    "detail = jsonb_build_object('error', 'service restarted') "
                    "WHERE status = 'running'",
                )
                return cur.rowcount
        except psycopg.errors.UndefinedTable:
            log.warning("kwim_admin.jobs not found - skipping the orphaned-job sweep; "
                        "apply db/admin-schema.sql (see deployment.md)")
            return 0
