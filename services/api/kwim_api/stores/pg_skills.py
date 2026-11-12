"""Skill tables in a team's schema: skills and skill_versions. `PgSkillStore` is
mixed into `PostgresStore` and uses its pool.
"""
import json
import re
from typing import Any

from psycopg.rows import dict_row

from .pg_pool import cursor

_SKILL_COLUMNS = ("s.id, s.name, s.status, s.current_version, s.owner, s.promoted_to, "
                  "s.promoted_from_team, s.promoted_from_id, s.created_at, s.created_by, "
                  "s.updated_at")
_VERSION_COLUMNS = ("v.version, v.text, v.description, v.body, v.agents, v.tags, v.refs, "
                    "v.source, v.author, v.approver, v.proposal_id, "
                    "v.created_at AS version_created_at")
_CURRENT = (f"SELECT {_SKILL_COLUMNS}, {_VERSION_COLUMNS} FROM {{s}}.skills s "
            "JOIN {s}.skill_versions v ON v.skill_id = s.id AND v.version = s.current_version")
_WORD = re.compile(r"[a-z0-9]+")


def _tsquery(text: str) -> str:
    """The words of `text` OR-ed together, for to_tsquery. Empty when it has none."""
    return " | ".join(dict.fromkeys(_WORD.findall(text.lower())))


class PgSkillStore:
    _pool: Any

    @staticmethod
    def _skill_schema(team: str) -> str:
        from .postgres import _schema
        return _schema(team)

    async def get_skill(self, team: str, name: str) -> dict[str, Any] | None:
        """A skill with its current version, retired or not."""
        s = self._skill_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(_CURRENT.format(s=s) + " WHERE s.name = %s", (name,))
            return await cur.fetchone()

    async def list_skills(self, team: str, *, include_retired: bool = False) -> list[dict[str, Any]]:
        s = self._skill_schema(team)
        where = "" if include_retired else " WHERE s.status = 'current'"
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(_CURRENT.format(s=s) + where + " ORDER BY s.name")
            return await cur.fetchall()

    async def skill_versions(self, team: str, skill_id: str) -> list[dict[str, Any]]:
        """Every version of a skill, newest first, without embeddings."""
        s = self._skill_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_VERSION_COLUMNS} FROM {s}.skill_versions v "
                "WHERE v.skill_id = %s ORDER BY v.version DESC", (skill_id,))
            return await cur.fetchall()

    async def get_skill_version(self, team: str, skill_id: str,
                                version: int) -> dict[str, Any] | None:
        s = self._skill_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_VERSION_COLUMNS} FROM {s}.skill_versions v "
                "WHERE v.skill_id = %s AND v.version = %s", (skill_id, version))
            return await cur.fetchone()

    async def create_skill(self, team: str, *, name: str, owner: str | None,
                           created_by: str | None, version: dict[str, Any],
                           promoted_from_team: str | None = None,
                           promoted_from_id: str | None = None) -> dict[str, Any]:
        """Insert a skill and its version 1 in one transaction."""
        s = self._skill_schema(team)
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"INSERT INTO {s}.skills (name, current_version, owner, created_by, "
                        " promoted_from_team, promoted_from_id) "
                        "VALUES (%s, 1, %s, %s, %s, %s) RETURNING id",
                        (name, owner, created_by, promoted_from_team, promoted_from_id))
                    skill_id = (await cur.fetchone())["id"]
                    await self._insert_version(cur, s, skill_id, 1, version)
        return await self.get_skill(team, name)

    async def add_skill_version(self, team: str, skill_id: str,
                                version: dict[str, Any]) -> dict[str, Any]:
        """Insert the next version and make it current. The skill row is locked
        so two revisions cannot take the same number."""
        s = self._skill_schema(team)
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"SELECT name, current_version FROM {s}.skills WHERE id = %s FOR UPDATE",
                        (skill_id,))
                    row = await cur.fetchone()
                    number = (await self._max_version(cur, s, skill_id)) + 1
                    await self._insert_version(cur, s, skill_id, number, version)
                    await cur.execute(
                        f"UPDATE {s}.skills SET current_version = %s, updated_at = now() "
                        "WHERE id = %s", (number, skill_id))
        return await self.get_skill(team, row["name"])

    @staticmethod
    async def _max_version(cur: Any, s: str, skill_id: str) -> int:
        await cur.execute(
            f"SELECT COALESCE(MAX(version), 0) AS v FROM {s}.skill_versions WHERE skill_id = %s",
            (skill_id,))
        return int((await cur.fetchone())["v"])

    @staticmethod
    async def _insert_version(cur: Any, s: str, skill_id: str, number: int,
                              v: dict[str, Any]) -> None:
        await cur.execute(
            f"INSERT INTO {s}.skill_versions (skill_id, version, text, description, body, "
            " agents, tags, refs, embedding, source, author, approver, proposal_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (skill_id, number, v["text"], v["description"], v["body"],
             json.dumps(v.get("agents", [])), json.dumps(v.get("tags", [])),
             json.dumps(v.get("refs", [])), v.get("embedding"), v["source"],
             v.get("author"), v.get("approver"), v.get("proposal_id")))

    async def set_skill_status(self, team: str, skill_id: str, status: str) -> None:
        s = self._skill_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"UPDATE {s}.skills SET status = %s, updated_at = now() WHERE id = %s",
                (status, skill_id))

    async def set_skill_owner(self, team: str, skill_id: str, owner: str | None) -> None:
        s = self._skill_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"UPDATE {s}.skills SET owner = %s, updated_at = now() WHERE id = %s",
                (owner, skill_id))

    async def set_skill_promoted(self, team: str, skill_id: str, universe_id: str) -> None:
        s = self._skill_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"UPDATE {s}.skills SET promoted_to = %s WHERE id = %s", (universe_id, skill_id))

    async def skill_search_rows(self, team: str, q: str) -> list[dict[str, Any]]:
        """Every current skill with its embedding and its full-text rank for `q`
        (`fts_hit` true when any word of `q` matches)."""
        s = self._skill_schema(team)
        query = _tsquery(q)
        doc = "to_tsvector('english', s.name || ' ' || v.description || ' ' || v.body)"
        if query:
            rank = (f"ts_rank({doc}, to_tsquery('english', %(q)s)) AS fts_rank, "
                    f"({doc} @@ to_tsquery('english', %(q)s)) AS fts_hit")
        else:
            rank = "0.0::real AS fts_rank, false AS fts_hit"
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_SKILL_COLUMNS}, {_VERSION_COLUMNS}, v.embedding, {rank} "
                f"FROM {s}.skills s JOIN {s}.skill_versions v "
                "ON v.skill_id = s.id AND v.version = s.current_version "
                "WHERE s.status = 'current'", {"q": query})
            return await cur.fetchall()

    async def set_skill_embedding(self, team: str, skill_id: str, version: int,
                                  embedding: list[float]) -> None:
        s = self._skill_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"UPDATE {s}.skill_versions SET embedding = %s "
                "WHERE skill_id = %s AND version = %s", (embedding, skill_id, version))

    async def pending_skill_proposals(self, team: str) -> dict[str, dict[str, Any]]:
        """Open skill proposals by skill name, the newest for each name."""
        s = self._skill_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT DISTINCT ON (body->>'name') proposal_id, proposed_by, created_at, "
                "body->>'name' AS name, body->>'description' AS description "
                f"FROM {s}.pending_proposals "
                "WHERE object_type = 'skill' AND resolved_at IS NULL "
                "ORDER BY body->>'name', created_at DESC")
            return {r["name"]: r for r in await cur.fetchall()}
