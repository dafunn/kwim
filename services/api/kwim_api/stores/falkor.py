"""FalkorDB store - the queryable projection: K + W graph, semantic vector index,
and working-memory TTL keys.

One graph per team (`kwim_<team>`), whose indexes this store creates on first
use; `kwim_universe` is reached as the pseudo-team "universe". Working memory is
Redis TTL keys on the same instance.
"""
import json as _json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

from falkordb.asyncio import FalkorDB

from ..config import settings
from ..freshness import resolve_decay_class
from .falkor_code import CodeGraphStore, _code_graph_name

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

# "universe" is the reserved pseudo-team name for the shared graph kwim_universe
# (messaging.universe_graph in kwim.defaults.yaml, env-overridable).
_UNIVERSE = settings.universe_graph


def _graph_name(team: str) -> str:
    # "universe" is the reserved pseudo-team for the shared kwim_universe graph.
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")
    return f"kwim_{team}"


# :Fact read projection shared by query_facts and search_facts, whose rows
# memory/context combines. `commit_seq` is the admin listing's cursor key.
_FACT_FIELDS = ("id", "statement", "fact_type", "status", "created_at", "about",
                "decay_class", "source_kind", "last_verified_at", "commit_seq")


def _fact_projection(alias: str) -> str:
    return ", ".join(f"{alias}.{f}" for f in _FACT_FIELDS)


def _fact_row(r: Any) -> dict:
    """Map a `_fact_projection` result row to the standard fact dict."""
    return {
        "id": r[0], "statement": r[1], "fact_type": r[2], "status": r[3],
        "created_at": str(r[4]), "about": list(r[5]) if r[5] else [],
        "decay_class": r[6] or "slow", "source_kind": r[7] or None,
        "last_verified_at": str(r[8]) if r[8] is not None else None,
        "commit_seq": r[9],
    }


# Graph schema (FalkorDB DDL)
#   - range indexes; re-creating one raises "already indexed", which is ignored.
#   - writers MERGE on id; there are no uniqueness constraints.
#   - vector indexes for semantic items and facts.
_INIT_CYPHER = [
    "CREATE INDEX FOR (f:Fact) ON (f.id)",
    "CREATE INDEX FOR (f:Fact) ON (f.status)",
    "CREATE INDEX FOR (r:Rule) ON (r.id)",
    "CREATE INDEX FOR (r:Rule) ON (r.status)",
    "CREATE INDEX FOR (r:Rule) ON (r.rule_type)",
    "CREATE INDEX FOR (r:Rule) ON (r.scope)",              # universe split + promotion dedup
    "CREATE INDEX FOR (a:Agent) ON (a.id)",
    "CREATE INDEX FOR (e:Evidence) ON (e.id)",
    # semantic Memory vector index
    "CREATE VECTOR INDEX FOR (s:SemanticItem) ON (s.embedding) "
    f"OPTIONS {{dimension:{settings.embed_dim}, similarityFunction:'cosine'}}",
    # Fact embedding index
    "CREATE VECTOR INDEX FOR (f:Fact) ON (f.embedding) "
    f"OPTIONS {{dimension:{settings.embed_dim}, similarityFunction:'cosine'}}",
]


class FalkorStore(CodeGraphStore):
    def __init__(self) -> None:
        self._db: FalkorDB | None = None
        self._inited: set[str] = set()

    async def connect(self) -> None:
        # Discrete arguments, not a URL; see docs/DESIGN.md, "Configuration".
        self._db = FalkorDB(
            host=settings.falkor_host, port=settings.falkor_port,
            password=settings.falkor_password or None,
        )

    async def close(self) -> None:
        # The async FalkorDB object exposes no close method itself; teardown goes
        # through its underlying redis.asyncio connection (`aclose()`, a coroutine).
        if self._db is None:
            return
        conn = getattr(self._db, "connection", None)
        aclose = getattr(conn, "aclose", None)
        if aclose is not None:
            await aclose()

    async def _ensure_schema(self, g, name: str, init_cypher: list[str]) -> None:
        """Apply DDL once per process per graph (idempotent across restarts)."""
        if name in self._inited:
            return
        for stmt in init_cypher:
            try:
                await g.query(stmt)
            except Exception as exc:  # idempotent: indexes persist across restarts
                if "already indexed" in str(exc).lower() or "already exists" in str(exc).lower():
                    continue
                raise
        self._inited.add(name)

    async def _graph(self, team: str, graph_name: str | None = None):
        """Return the team's K/W graph, ensuring its schema exists (once per process).

        `graph_name` overrides the derived name (rebuild's temp graph).
        """
        name = graph_name or _graph_name(team)
        g = self._db.select_graph(name)
        await self._ensure_schema(g, name, _INIT_CYPHER)
        return g

    # --- Team provisioning -------------------

    async def init_team_graph(self, team: str) -> None:
        """Touch kwim_<team> so _ensure_schema runs _INIT_CYPHER immediately,
        rather than on the first write. The code graph is created by the extractor."""
        await self._graph(team)

    async def drop_team_graphs(self, team: str) -> dict[str, bool]:
        """Destructive (team destroy only): delete kwim_<team> and
        kwim_<team>_code; either may already be absent. Clears both from the _inited
        cache so a re-provisioned team gets its indexes again.
        """
        results: dict[str, bool] = {}
        for name in (_graph_name(team), _code_graph_name(team)):
            try:
                await self._db.select_graph(name).delete()
                results[name] = True
            except Exception as exc:
                if "not exist" in str(exc).lower() or "unknown graph" in str(exc).lower():
                    results[name] = False
                else:
                    raise
            finally:
                self._inited.discard(name)
        return results

    async def materialize_fact(
        self, team: str, fact: dict[str, Any], provenance: dict[str, Any],
        graph_name: str | None = None, embedding: list[float] | None = None,
        created_at: int | None = None,
    ) -> None:
        """Create/upsert a :Fact node + its provenance edges (gate commit path).

        `embedding` is optional. `created_at` (epoch milliseconds) is set only when
        the node is created; replay passes the row's committed_at.
        """
        g = await self._graph(team, graph_name)
        await g.query(
            "MERGE (f:Fact {id:$id}) "
            "ON CREATE SET f.created_at = coalesce($created_at, timestamp()) "
            "SET f.statement=$statement, f.fact_type=$fact_type, f.status='current', "
            "    f.source_kind=$source_kind, f.commit_seq=$seq, "
            "    f.about=$about, f.decay_class=$decay_class",
            {"id": fact["id"], "statement": fact["statement"], "fact_type": fact["fact_type"],
             "source_kind": fact.get("source_kind", "agent_proposal"), "seq": fact["commit_seq"],
             "about": fact.get("about", []), "created_at": created_at,
             "decay_class": resolve_decay_class(fact["fact_type"], fact.get("decay_class"))},
        )
        if embedding is not None:
            await g.query(
                "MATCH (f:Fact {id:$id}) SET f.embedding=vecf32($embedding)",
                {"id": fact["id"], "embedding": embedding},
            )
        if provenance.get("proposed_by"):
            await g.query(
                "MATCH (f:Fact {id:$fid}) MERGE (a:Agent {id:$aid}) MERGE (f)-[:PROPOSED_BY]->(a)",
                {"fid": fact["id"], "aid": provenance["proposed_by"]},
            )
        for ev_id in provenance.get("supported_by", []):
            await g.query(
                "MATCH (f:Fact {id:$fid}) MERGE (e:Evidence {id:$eid}) "
                "SET e.episodic_event_id=$eid MERGE (f)-[:SUPPORTED_BY]->(e)",
                {"fid": fact["id"], "eid": ev_id},
            )
        if provenance.get("supersedes"):
            await g.query(
                "MATCH (new:Fact {id:$nid}) MATCH (old:Fact {id:$oid}) "
                "SET old.status='superseded' MERGE (new)-[:SUPERSEDES]->(old)",
                {"nid": fact["id"], "oid": provenance["supersedes"]},
            )

    # Content properties an amend may replace, per object type.
    AMENDABLE = {
        "fact": ("statement", "fact_type", "about", "decay_class"),
        "rule": ("situation", "approach", "action_pattern", "verdict", "authority",
                 "severity", "check_tier"),
        "semantic": ("content", "metadata"),
    }

    async def get_fact_content(self, team: str, fact_id: str) -> dict[str, Any] | None:
        """Content properties of one :Fact, plus status. None if absent.

        Feeds an amend's `previous_payload` and the current-status guard.
        """
        g = await self._graph(team)
        res = await g.query(
            "MATCH (f:Fact {id:$id}) "
            "RETURN f.statement, f.fact_type, f.about, f.decay_class, f.status",
            {"id": fact_id},
        )
        if not res.result_set:
            return None
        r = res.result_set[0]
        return {"statement": r[0], "fact_type": r[1], "about": list(r[2]) if r[2] else [],
                "decay_class": r[3] or "slow", "status": r[4]}

    async def get_rule_content(self, team: str, rule_id: str) -> dict[str, Any] | None:
        """Content properties of one :Rule, plus status. None if absent."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (r:Rule {id:$id}) "
            "RETURN r.situation_json, r.approach, r.action_pattern, r.verdict, "
            "       r.authority, r.severity, r.check_tier, r.status, r.rule_type",
            {"id": rule_id},
        )
        if not res.result_set:
            return None
        r = res.result_set[0]
        return {"situation": _json.loads(r[0]) if r[0] else None,
                "approach": r[1] or None, "action_pattern": r[2] or None,
                "verdict": r[3] or None, "authority": r[4] or None,
                "severity": r[5] or None, "check_tier": r[6] or None,
                "status": r[7], "rule_type": r[8]}

    async def get_semantic_content(self, team: str, item_id: str) -> dict[str, Any] | None:
        """Content + metadata of one :SemanticItem. None if absent."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (s:SemanticItem {id:$id}) RETURN s.content, s.metadata",
            {"id": item_id},
        )
        if not res.result_set:
            return None
        r = res.result_set[0]
        return {"content": r[0] or "", "metadata": _json.loads(r[1]) if r[1] else {}}

    async def amend_fact(
        self, team: str, fact_id: str, payload: dict[str, Any], seq: int,
        graph_name: str | None = None, embedding: list[float] | None = None,
    ) -> bool:
        """Replace a current :Fact's content in place. Returns False if no current
        fact with that id exists. Sets `commit_seq` to the amending row.
        """
        g = await self._graph(team, graph_name)
        sets = ["f.commit_seq=$seq"]
        params: dict[str, Any] = {"id": fact_id, "seq": seq}
        for key in self.AMENDABLE["fact"]:
            if key in payload:
                sets.append(f"f.{key}=${key}")
                params[key] = (resolve_decay_class(payload.get("fact_type", ""),
                                                   payload[key])
                               if key == "decay_class" else payload[key])
        res = await g.query(
            "MATCH (f:Fact {id:$id}) WHERE f.status='current' "
            f"SET {', '.join(sets)} RETURN f.id",
            params,
        )
        if not (res.result_set and res.result_set[0][0] is not None):
            return False
        if embedding is not None:
            await g.query("MATCH (f:Fact {id:$id}) SET f.embedding=vecf32($embedding)",
                          {"id": fact_id, "embedding": embedding})
        return True

    async def amend_rule(
        self, team: str, rule_id: str, payload: dict[str, Any], seq: int,
        previous_situation: dict[str, Any] | None = None,
        graph_name: str | None = None,
    ) -> bool:
        """Replace an approved :Rule's content in place. Returns False if no
        approved rule with that id exists. A changed `situation` replaces the
        promoted situation properties.
        """
        g = await self._graph(team, graph_name)
        sets = ["r.commit_seq=$seq"]
        params: dict[str, Any] = {"id": rule_id, "seq": seq}
        removes: list[str] = []

        if "situation" in payload:
            sit = payload["situation"] or {}
            params["situation_json"] = _json.dumps(sit) if sit else ""
            sets.append("r.situation_json=$situation_json")
            new_keys = set()
            for k, v in sit.items():
                safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
                if safe_key in self._RULE_RESERVED:
                    log.warning("amend_rule: situation key %r collides with a reserved "
                                "node property; kept in situation_json only", k)
                    continue
                new_keys.add(safe_key)
                params[f"sit_{safe_key}"] = v
                sets.append(f"r.{safe_key}=$sit_{safe_key}")
            for k in (previous_situation or {}):
                safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
                if safe_key not in self._RULE_RESERVED and safe_key not in new_keys:
                    removes.append(f"r.{safe_key}")

        for key in self.AMENDABLE["rule"]:
            if key == "situation" or key not in payload:
                continue
            sets.append(f"r.{key}=${key}")
            params[key] = payload[key] or ""

        clause = f"REMOVE {', '.join(removes)} " if removes else ""
        res = await g.query(
            "MATCH (r:Rule {id:$id}) WHERE r.status='approved' "
            f"{clause}SET {', '.join(sets)} RETURN r.id",
            params,
        )
        return bool(res.result_set and res.result_set[0][0] is not None)

    async def amend_semantic(
        self, team: str, item_id: str, payload: dict[str, Any],
        previous_metadata: dict[str, Any] | None = None,
        graph_name: str | None = None, embedding: list[float] | None = None,
    ) -> bool:
        """Replace a :SemanticItem's content in place. Returns False if absent.

        Changed metadata replaces the promoted metadata properties.
        """
        g = await self._graph(team, graph_name)
        sets: list[str] = []
        params: dict[str, Any] = {"id": item_id}
        removes: list[str] = []

        if "content" in payload:
            sets.append("s.content=$content")
            params["content"] = payload["content"]
        if "metadata" in payload:
            meta = payload["metadata"] or {}
            params["metadata_json"] = _json.dumps(meta)
            sets.append("s.metadata=$metadata_json")
            new_keys = set()
            for k, v in meta.items():
                if k in self._SEMANTIC_RESERVED:
                    continue
                safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
                new_keys.add(safe_key)
                params[f"meta_{safe_key}"] = v
                sets.append(f"s.{safe_key}=$meta_{safe_key}")
            for k in (previous_metadata or {}):
                if k in self._SEMANTIC_RESERVED:
                    continue
                safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
                if safe_key not in new_keys:
                    removes.append(f"s.{safe_key}")
        if not sets:
            return False

        clause = f"REMOVE {', '.join(removes)} " if removes else ""
        res = await g.query(
            "MATCH (s:SemanticItem {id:$id}) "
            f"{clause}SET {', '.join(sets)} RETURN s.id",
            params,
        )
        if not (res.result_set and res.result_set[0][0] is not None):
            return False
        if embedding is not None:
            await g.query("MATCH (s:SemanticItem {id:$id}) SET s.embedding=vecf32($embedding)",
                          {"id": item_id, "embedding": embedding})
        return True

    async def query_facts(
        self, team: str, fact_type: str | None, status: str, limit: int,
        about: list[str] | None = None, source_kind: str | None = None,
    ) -> list[dict]:
        g = await self._graph(team)
        cypher = "MATCH (f:Fact) WHERE f.status=$status "
        params: dict = {"status": status, "limit": limit}
        if fact_type:
            cypher += "AND f.fact_type=$fact_type "
            params["fact_type"] = fact_type
        if source_kind:
            cypher += "AND f.source_kind=$source_kind "
            params["source_kind"] = source_kind
        if about:
            # case-insensitive membership: any query token is a member of f.about
            cypher += (
                "AND ANY(a IN f.about WHERE "
                "ANY(qa IN $about WHERE toLower(a) = toLower(qa))) "
            )
            params["about"] = about
        cypher += f"RETURN {_fact_projection('f')} LIMIT $limit"
        res = await g.query(cypher, params)
        return [_fact_row(r) for r in res.result_set]

    def _facts_admin_where(
        self, *, status: str | None, fact_type: str | None, source_kind: str | None,
        about: list[str] | None, q: str | None, cursor: dict[str, Any] | None,
    ) -> tuple[list[str], dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if status is not None:
            clauses.append("f.status=$status")
            params["status"] = status
        if fact_type is not None:
            clauses.append("f.fact_type=$fact_type")
            params["fact_type"] = fact_type
        if source_kind is not None:
            clauses.append("f.source_kind=$source_kind")
            params["source_kind"] = source_kind
        if about:
            clauses.append(
                "ANY(a IN f.about WHERE ANY(qa IN $about WHERE toLower(a) = toLower(qa)))")
            params["about"] = about
        if q is not None:
            clauses.append("toLower(f.statement) CONTAINS toLower($q)")
            params["q"] = q
        if cursor is not None:
            clauses.append(
                "(coalesce(f.commit_seq, 0) > $cseq OR "
                " (coalesce(f.commit_seq, 0) = $cseq AND f.id > $cid))")
            params["cseq"] = cursor["seq"]
            params["cid"] = cursor["id"]
        return clauses, params

    async def query_facts_admin(
        self, team: str, *, status: str | None = None, fact_type: str | None = None,
        source_kind: str | None = None, about: list[str] | None = None,
        q: str | None = None, cursor: dict[str, Any] | None = None, limit: int = 50,
    ) -> list[dict]:
        """Cross-status, cursor-paginated fact browse for the admin console.

        Ordered `coalesce(f.commit_seq, 0) ASC, f.id ASC`.
        """
        g = await self._graph(team)
        clauses, params = self._facts_admin_where(
            status=status, fact_type=fact_type, source_kind=source_kind,
            about=about, q=q, cursor=cursor)
        params["limit"] = limit
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        cypher = (
            f"MATCH (f:Fact) {where}"
            f"RETURN {_fact_projection('f')} "
            "ORDER BY coalesce(f.commit_seq, 0) ASC, f.id ASC LIMIT $limit"
        )
        res = await g.query(cypher, params)
        return [_fact_row(r) for r in res.result_set]

    async def count_facts_admin(
        self, team: str, *, status: str | None = None, fact_type: str | None = None,
        source_kind: str | None = None, about: list[str] | None = None,
        q: str | None = None,
    ) -> int:
        g = await self._graph(team)
        clauses, params = self._facts_admin_where(
            status=status, fact_type=fact_type, source_kind=source_kind,
            about=about, q=q, cursor=None)
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        res = await g.query(f"MATCH (f:Fact) {where}RETURN count(f)", params)
        return int(res.result_set[0][0]) if res.result_set else 0

    async def reaffirm_fact(self, team: str, fact_id: str, verified_at: int | None = None,
                            graph_name: str | None = None) -> bool:
        """Stamp last_verified_at on a current :Fact. Non-destructive; does not write
        commit_log. Returns True if the fact existed and was current.
        `verified_at` (epoch milliseconds) defaults to now.
        """
        g = await self._graph(team, graph_name)
        res = await g.query(
            "MATCH (f:Fact {id:$id}) WHERE f.status='current' "
            "SET f.last_verified_at = coalesce($verified_at, timestamp()) RETURN f.id",
            {"id": fact_id, "verified_at": verified_at},
        )
        return bool(res.result_set and res.result_set[0][0] is not None)

    async def query_similar_facts(
        self, team: str, vector: list[float], k: int = 5,
        about: list[str] | None = None, fact_type: str | None = None,
    ) -> list[dict]:
        """KNN over :Fact embeddings - powered by the :Fact vector index.

        Current facts only, nearest first. With `about` and `fact_type`, scores only
        facts of that type whose `about` contains every given ref. The gate's
        duplicate screen; see docs/DESIGN.md, "The governance gate".
        """
        g = await self._graph(team)
        if about and fact_type:
            # Filter, then score the matching facts.
            res = await g.query(
                "MATCH (f:Fact) "
                "WHERE f.status = 'current' AND f.fact_type = $fact_type "
                "  AND f.embedding IS NOT NULL "
                "  AND all(t IN $about WHERE t IN f.about) "
                "RETURN f.id, f.statement, f.status, "
                "       vec.cosineDistance(f.embedding, vecf32($qvec)) AS score "
                "ORDER BY score ASC LIMIT $k",
                {"k": k, "qvec": vector, "fact_type": fact_type, "about": about},
            )
        else:
            try:
                res = await g.query(
                    "CALL db.idx.vector.queryNodes('Fact', 'embedding', $k, vecf32($qvec)) "
                    "YIELD node, score "
                    "WHERE node.status='current' "
                    "RETURN node.id, node.statement, node.status, score "
                    "ORDER BY score ASC LIMIT $k",
                    {"k": k, "qvec": vector},
                )
            except Exception as exc:
                # An empty vector index raises; treat it as no matches.
                log.warning("falkor: query_similar_facts failed (likely empty index): %s", exc)
                return []
        return [
            {"id": r[0], "statement": r[1], "status": r[2], "score": float(r[3])}
            for r in res.result_set
        ]

    async def search_facts(
        self, team: str, qvec: list[float], limit: int = 10,
        about: list[str] | None = None, fact_type: str | None = None,
    ) -> list[dict]:
        """Semantic KNN over :Fact embeddings for the read path - Tier 1 retrieval
        for Knowledge. `about` matches as query_facts does (any ref, case-insensitive).
        Current facts only; `score` is a cosine distance. See docs/DESIGN.md,
        "Retrieval".
        """
        g = await self._graph(team)
        params: dict[str, Any] = {"k": limit, "qvec": qvec}
        filters: list[str] = []
        if fact_type:
            filters.append("f.fact_type=$fact_type")
            params["fact_type"] = fact_type
        if about:
            filters.append(
                "ANY(a IN f.about WHERE ANY(qa IN $about WHERE toLower(a) = toLower(qa)))")
            params["about"] = about

        if filters:
            # Filter, then score the matching facts.
            res = await g.query(
                "MATCH (f:Fact) WHERE f.status='current' AND f.embedding IS NOT NULL "
                "AND " + " AND ".join(filters) + " "
                f"RETURN {_fact_projection('f')}, "
                "vec.cosineDistance(f.embedding, vecf32($qvec)) AS score "
                "ORDER BY score ASC LIMIT $k",
                params,
            )
        else:
            try:
                res = await g.query(
                    "CALL db.idx.vector.queryNodes('Fact', 'embedding', $k, vecf32($qvec)) "
                    "YIELD node, score WHERE node.status='current' "
                    f"RETURN {_fact_projection('node')}, score "
                    "ORDER BY score ASC LIMIT $k",
                    params,
                )
            except Exception as exc:
                # No vectors in the index yet (new team, or nothing embedded).
                log.warning("falkor: search_facts failed (likely empty index): %s", exc)
                return []
        return [{**_fact_row(r), "score": float(r[len(_FACT_FIELDS)])} for r in res.result_set]

    async def facts_missing_embedding(self, team: str, limit: int = 1000) -> list[dict]:
        """Current facts with no `embedding` property - invisible to `search_facts`
        until backfilled. Ordered by commit_seq."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (f:Fact) WHERE f.status='current' AND f.embedding IS NULL "
            "RETURN f.id, f.statement ORDER BY f.commit_seq LIMIT $limit",
            {"limit": limit},
        )
        return [{"id": r[0], "statement": r[1] or ""} for r in res.result_set]

    async def set_fact_embedding(
        self, team: str, fact_id: str, embedding: list[float],
    ) -> bool:
        """Attach an embedding to an existing :Fact in place (backfill path).

        Touches only the vector, and reads it back to confirm the write.
        """
        g = await self._graph(team)
        await g.query(
            "MATCH (f:Fact {id:$id}) SET f.embedding=vecf32($embedding)",
            {"id": fact_id, "embedding": embedding},
        )
        check = await g.query(
            "MATCH (f:Fact {id:$id}) WHERE f.embedding IS NOT NULL RETURN f.id",
            {"id": fact_id},
        )
        return bool(check.result_set)

    async def get_fact_provenance(self, team: str, fact_id: str) -> dict | None:
        """One fact + its immediate provenance edges (knowledge.facts/{id}).

        None if absent. Evidence is returned as episodic_event_id references.
        """
        g = await self._graph(team)
        res = await g.query(
            "MATCH (f:Fact {id:$id}) "
            "OPTIONAL MATCH (f)-[:PROPOSED_BY]->(a:Agent) "
            "OPTIONAL MATCH (f)-[:SUPERSEDES]->(old:Fact) "
            "OPTIONAL MATCH (f)-[:SUPPORTED_BY]->(e:Evidence) "
            "RETURN f.id, f.statement, f.fact_type, f.status, f.created_at, "
            "       f.source_kind, f.last_verified_at, "
            "       a.id, old.id, collect(DISTINCT e.episodic_event_id)",
            {"id": fact_id},
        )
        if not res.result_set:
            return None
        r = res.result_set[0]
        return {
            "id": r[0], "statement": r[1], "fact_type": r[2], "status": r[3],
            "created_at": str(r[4]),
            "source_kind": r[5] or None,
            "last_verified_at": str(r[6]) if r[6] is not None else None,
            "proposed_by": r[7], "supersedes": r[8],
            "supported_by": [x for x in (r[9] or []) if x is not None],
        }

    async def audit_fact(self, team: str, fact_id: str) -> list[dict]:
        """Provenance walk for knowledge.audit/{id}: the fact + its full version
        chain (SUPERSEDES* lineage), newest first, each with its evidence and
        proposing agent. [] if the fact is absent.
        """
        g = await self._graph(team)
        res = await g.query(
            "MATCH (f:Fact {id:$id}) "
            "OPTIONAL MATCH (f)-[:SUPERSEDES*1..]->(o:Fact) "
            "WITH collect(DISTINCT f) + collect(DISTINCT o) AS vs "
            "UNWIND vs AS v "
            "WITH DISTINCT v WHERE v IS NOT NULL "
            "OPTIONAL MATCH (v)-[:PROPOSED_BY]->(a:Agent) "
            "OPTIONAL MATCH (v)-[:SUPPORTED_BY]->(e:Evidence) "
            "RETURN v.id, v.statement, v.status, v.created_at, v.commit_seq, "
            "       a.id, collect(DISTINCT e.episodic_event_id) "
            "ORDER BY v.commit_seq DESC",
            {"id": fact_id},
        )
        return [
            {
                "id": r[0], "statement": r[1], "status": r[2],
                "created_at": str(r[3]) if r[3] is not None else None,
                "commit_seq": r[4], "proposed_by": r[5],
                "supported_by": [x for x in (r[6] or []) if x is not None],
            }
            for r in res.result_set
        ]

    # --- Wisdom materialization + read paths ---

    # :Rule properties a situation key may not overwrite (kept in situation_json only).
    _RULE_RESERVED = {
        "id", "rule_type", "status", "scope", "evidence_count", "commit_seq",
        "created_at", "situation_json", "approach", "action_pattern", "verdict",
        "authority", "severity", "check_tier", "promoted_from_id",
        "promoted_from_team",
    }

    async def materialize_rule(self, team: str, rule: dict[str, Any], provenance: dict[str, Any], graph_name: str | None = None, created_at: int | None = None) -> None:
        """Create/upsert a :Rule node + its provenance edges (gate commit path).

        `rule` needs id, rule_type, status, scope, evidence_count and commit_seq;
        missing optional fields default to empty. Situation keys are also copied to
        node properties for filtering. `created_at` is set only on creation.
        """
        g = await self._graph(team, graph_name)
        sit = rule.get("situation") or {}
        params: dict[str, Any] = {
            "id": rule["id"],
            "rule_type": rule["rule_type"],
            "status": rule["status"],
            "scope": rule.get("scope", "team"),
            "evidence_count": rule.get("evidence_count", 0),
            "seq": rule["commit_seq"],
            "situation_json": _json.dumps(sit) if sit else "",
            "approach": rule.get("approach") or "",
            "action_pattern": rule.get("action_pattern") or "",
            "verdict": rule.get("verdict") or "",
            "authority": rule.get("authority") or "",
            "severity": rule.get("severity") or "",
            "check_tier": rule.get("check_tier") or "",
            "promoted_from_id": rule.get("promoted_from_id") or "",
            "promoted_from_team": rule.get("promoted_from_team") or "",
            "created_at": created_at,
        }
        sit_sets: list[str] = []
        for k, v in sit.items():
            safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
            if safe_key in self._RULE_RESERVED:
                log.warning(
                    "materialize_rule: situation key %r collides with a reserved "
                    "node property; kept in situation_json only", k)
                continue
            params[f"sit_{safe_key}"] = v
            sit_sets.append(f"r.{safe_key}=$sit_{safe_key}")
        set_clause = (
            "ON CREATE SET r.created_at = coalesce($created_at, timestamp()) "
            "SET r.rule_type=$rule_type, r.status=$status, r.scope=$scope, "
            "    r.evidence_count=$evidence_count, r.commit_seq=$seq, "
            "    r.situation_json=$situation_json, "
            "    r.approach=$approach, "
            "    r.action_pattern=$action_pattern, r.verdict=$verdict, "
            "    r.authority=$authority, r.severity=$severity, r.check_tier=$check_tier, "
            "    r.promoted_from_id=$promoted_from_id, "
            "    r.promoted_from_team=$promoted_from_team"
        )
        if sit_sets:
            set_clause += ", " + ", ".join(sit_sets)
        await g.query("MERGE (r:Rule {id:$id}) " + set_clause, params)
        if provenance.get("proposed_by"):
            await g.query(
                "MATCH (r:Rule {id:$rid}) MERGE (a:Agent {id:$aid}) MERGE (r)-[:PROPOSED_BY]->(a)",
                {"rid": rule["id"], "aid": provenance["proposed_by"]},
            )
        for ev_id in provenance.get("learned_from", []):
            await g.query(
                "MATCH (r:Rule {id:$rid}) MERGE (e:Evidence {id:$eid}) "
                "SET e.episodic_event_id=$eid MERGE (r)-[:LEARNED_FROM]->(e)",
                {"rid": rule["id"], "eid": ev_id},
            )

    async def _query_rules_from_graph(
        self, team: str, situation: dict[str, Any] | None,
        limit: int, source_tag: str,
    ) -> list[dict]:
        """Query approved :Rule nodes from one graph, tagged with source_tag.

        `situation` keys must all match; reserved keys are ignored. [] if the graph
        does not exist.
        """
        try:
            g = await self._graph(team)
        except Exception:
            return []
        cypher = "MATCH (r:Rule) WHERE r.status='approved' "
        params: dict[str, Any] = {"limit": limit}
        for k, v in (situation or {}).items():
            safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
            if safe_key in self._RULE_RESERVED:
                log.warning(
                    "query_rules: situation key %r collides with a reserved "
                    "node property; ignored", k)
                continue
            cypher += f"AND r.{safe_key}=$sit_{safe_key} "
            params[f"sit_{safe_key}"] = v
        cypher += (
            "RETURN r.id, r.rule_type, r.situation_json, r.approach, "
            "       r.evidence_count, r.status, r.scope, "
            "       r.action_pattern, r.verdict, r.authority, r.severity, r.check_tier, "
            "       r.promoted_from_id "
            "ORDER BY r.evidence_count DESC LIMIT $limit"
        )
        try:
            res = await g.query(cypher, params)
        except Exception:
            # graph exists but is empty / no :Rule nodes yet - tolerated
            return []
        rows = []
        for r in res.result_set:
            sit_raw = r[2]
            sit = _json.loads(sit_raw) if sit_raw else None
            rows.append({
                "id": r[0],
                "rule_type": r[1],
                "situation": sit,
                "approach": r[3] or None,
                "evidence_count": r[4] or 0,
                "status": r[5],
                "scope": r[6] or "team",
                "action_pattern": r[7] or None,
                "verdict": r[8] or None,
                "authority": r[9] or None,
                "severity": r[10] or None,
                "check_tier": r[11] or None,
                "promoted_from_id": r[12] or None,
                "_source": source_tag,
            })
        return rows

    async def query_rules(
        self, team: str,
        situation: dict[str, Any] | None = None, limit: int = 20,
    ) -> list[dict]:
        """Return approved rules from the team graph + the universe graph, merged.

        Each is tagged with its source. A team rule with a promoted universe copy is
        dropped in favour of the copy. Sorted by evidence_count, highest first.
        """
        team_rows = await self._query_rules_from_graph(
            team, situation, limit, source_tag="team")
        universe_rows = await self._query_rules_from_graph(
            _UNIVERSE, situation, limit, source_tag="universe")

        # Collect ids that have been promoted (universe copies record the original id).
        promoted_ids: set[str] = {
            r["promoted_from_id"] for r in universe_rows if r.get("promoted_from_id")
        }
        # Suppress team originals that have a universe copy.
        merged = [r for r in team_rows if r["id"] not in promoted_ids] + universe_rows
        merged.sort(key=lambda r: r.get("evidence_count", 0), reverse=True)
        return merged[:limit]

    def _rules_admin_where(
        self, *, status: str | None, rule_type: str | None, scope: str | None,
        cursor: dict[str, Any] | None,
    ) -> tuple[list[str], dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if status is not None:
            clauses.append("r.status=$status")
            params["status"] = status
        if rule_type is not None:
            clauses.append("r.rule_type=$rule_type")
            params["rule_type"] = rule_type
        if scope is not None:
            clauses.append("r.scope=$scope")
            params["scope"] = scope
        if cursor is not None:
            clauses.append(
                "(coalesce(r.commit_seq, 0) > $cseq OR "
                " (coalesce(r.commit_seq, 0) = $cseq AND r.id > $cid))")
            params["cseq"] = cursor["seq"]
            params["cid"] = cursor["id"]
        return clauses, params

    async def query_rules_admin(
        self, team: str, *, status: str | None = None, rule_type: str | None = None,
        scope: str | None = None, cursor: dict[str, Any] | None = None, limit: int = 50,
    ) -> list[dict]:
        """Cross-status rule browse for the admin console - pending, deprecated, and
        retracted rules included. One graph only. Ordered
        `coalesce(r.commit_seq, 0) ASC, r.id ASC`.
        """
        g = await self._graph(team)
        clauses, params = self._rules_admin_where(
            status=status, rule_type=rule_type, scope=scope, cursor=cursor)
        params["limit"] = limit
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        res = await g.query(
            f"MATCH (r:Rule) {where}"
            "RETURN r.id, r.rule_type, r.situation_json, r.approach, r.evidence_count, "
            "       r.status, r.scope, r.action_pattern, r.verdict, r.authority, "
            "       r.severity, r.check_tier, r.promoted_from_id, r.commit_seq "
            "ORDER BY coalesce(r.commit_seq, 0) ASC, r.id ASC LIMIT $limit",
            params,
        )
        rows = []
        for r in res.result_set:
            sit_raw = r[2]
            rows.append({
                "id": r[0], "rule_type": r[1],
                "situation": _json.loads(sit_raw) if sit_raw else None,
                "approach": r[3] or None, "evidence_count": r[4] or 0, "status": r[5],
                "scope": r[6] or "team", "action_pattern": r[7] or None, "verdict": r[8] or None,
                "authority": r[9] or None, "severity": r[10] or None, "check_tier": r[11] or None,
                "promoted_from_id": r[12] or None, "commit_seq": r[13],
            })
        return rows

    async def count_rules_admin(
        self, team: str, *, status: str | None = None, rule_type: str | None = None,
        scope: str | None = None,
    ) -> int:
        g = await self._graph(team)
        clauses, params = self._rules_admin_where(
            status=status, rule_type=rule_type, scope=scope, cursor=None)
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        res = await g.query(f"MATCH (r:Rule) {where}RETURN count(r)", params)
        return int(res.result_set[0][0]) if res.result_set else 0

    async def get_rule_provenance(self, team: str, rule_id: str) -> dict[str, Any] | None:
        """One rule (any status) + its provenance edges - the rule-detail counterpart
        of `get_fact_provenance`. None if the rule is not in the team graph."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (r:Rule {id:$id}) "
            "OPTIONAL MATCH (r)-[:PROPOSED_BY]->(a:Agent) "
            "OPTIONAL MATCH (r)-[:LEARNED_FROM]->(e:Evidence) "
            "RETURN r.id, r.rule_type, r.situation_json, r.approach, r.evidence_count, "
            "       r.status, r.scope, r.action_pattern, r.verdict, r.authority, "
            "       r.severity, r.check_tier, r.promoted_from_id, r.promoted_from_team, "
            "       a.id, collect(DISTINCT e.episodic_event_id)",
            {"id": rule_id},
        )
        if not res.result_set:
            return None
        r = res.result_set[0]
        sit_raw = r[2]
        return {
            "id": r[0], "rule_type": r[1], "situation": _json.loads(sit_raw) if sit_raw else None,
            "approach": r[3] or None, "evidence_count": r[4] or 0, "status": r[5],
            "scope": r[6] or "team", "action_pattern": r[7] or None, "verdict": r[8] or None,
            "authority": r[9] or None, "severity": r[10] or None, "check_tier": r[11] or None,
            "promoted_from_id": r[12] or None, "promoted_from_team": r[13] or None,
            "proposed_by": r[14], "learned_from": [x for x in (r[15] or []) if x is not None],
        }

    async def get_rule(self, team: str, rule_id: str) -> dict[str, Any] | None:
        """Fetch a single :Rule node by id. Returns None if absent or not approved."""
        try:
            g = await self._graph(team)
        except Exception:
            return None
        res = await g.query(
            "MATCH (r:Rule {id:$id}) RETURN r.id, r.status, r.evidence_count",
            {"id": rule_id},
        )
        if not res.result_set:
            return None
        r = res.result_set[0]
        return {"id": r[0], "status": r[1], "evidence_count": r[2] or 0}

    async def reinforce_rule(self, team: str, rule_id: str, new_evidence: list[str], seq: int, graph_name: str | None = None) -> bool:
        """Increment a :Rule's evidence_count and attach new LEARNED_FROM edges.

        False if the rule is absent or not approved.
        """
        try:
            g = await self._graph(team, graph_name)
        except Exception:
            return False
        # Verify approved before mutating.
        check = await g.query(
            "MATCH (r:Rule {id:$id, status:'approved'}) RETURN r.id",
            {"id": rule_id},
        )
        if not check.result_set:
            return False
        n = len(new_evidence)
        await g.query(
            "MATCH (r:Rule {id:$id}) SET r.evidence_count = r.evidence_count + $n, r.commit_seq=$seq",
            {"id": rule_id, "n": n, "seq": seq},
        )
        for ev_id in new_evidence:
            await g.query(
                "MATCH (r:Rule {id:$rid}) MERGE (e:Evidence {id:$eid}) "
                "SET e.episodic_event_id=$eid MERGE (r)-[:LEARNED_FROM]->(e)",
                {"rid": rule_id, "eid": ev_id},
            )
        return True

    async def deprecate_rule(self, team: str, rule_id: str, graph_name: str | None = None) -> None:
        """Mark a :Rule as deprecated (rebuild forward-compat)."""
        g = await self._graph(team, graph_name)
        await g.query(
            "MATCH (r:Rule {id:$id}) SET r.status='deprecated'",
            {"id": rule_id},
        )

    async def tag_rule_promoted(self, team: str, rule_id: str) -> None:
        """Set promoted_to_universe=true on the team-side original after promotion."""
        g = await self._graph(team)
        await g.query(
            "MATCH (r:Rule {id:$id}) SET r.promoted_to_universe=true",
            {"id": rule_id},
        )

    # --- post-hoc retract/confirm (optimistic governance) ---

    async def find_object(
        self, team: str, object_id: str, object_type: str | None = None,
    ) -> tuple[str, str] | None:
        """Locate a committed :Fact or :Rule by id. Returns (object_type, status) or None.

        Looks up only `object_type`'s label when given, otherwise both.
        """
        g = await self._graph(team)
        if object_type:
            label = "Fact" if object_type == "fact" else "Rule"
            res = await g.query(f"MATCH (n:{label} {{id:$id}}) RETURN n.status", {"id": object_id})
            if not res.result_set:
                return None
            return object_type, res.result_set[0][0]

        res = await g.query(
            "OPTIONAL MATCH (f:Fact {id:$id}) OPTIONAL MATCH (r:Rule {id:$id}) "
            "RETURN f.id, f.status, r.id, r.status",
            {"id": object_id},
        )
        if not res.result_set:
            return None
        fid, fstatus, rid, rstatus = res.result_set[0]
        if fid is not None:
            return "fact", fstatus
        if rid is not None:
            return "rule", rstatus
        return None

    async def retract_object(
        self, team: str, object_type: str, object_id: str, graph_name: str | None = None,
    ) -> None:
        """Set a committed :Fact/:Rule's status to 'retracted'."""
        label = "Fact" if object_type == "fact" else "Rule"
        g = await self._graph(team, graph_name)
        await g.query(f"MATCH (n:{label} {{id:$id}}) SET n.status='retracted'", {"id": object_id})

    async def confirm_object(
        self, team: str, object_type: str, object_id: str, by: str, at: str,
        graph_name: str | None = None,
    ) -> None:
        """Stamp confirmed_by/confirmed_at on a committed :Fact/:Rule - no status change."""
        label = "Fact" if object_type == "fact" else "Rule"
        g = await self._graph(team, graph_name)
        await g.query(
            f"MATCH (n:{label} {{id:$id}}) SET n.confirmed_by=$by, n.confirmed_at=$at",
            {"id": object_id, "by": by, "at": at},
        )

    # --- working memory (Redis TTL keys on the same instance) ---
    async def working_set(self, team: str, session: str, key: str, value: str, ttl: int | None) -> None:
        conn = self._db.connection
        k = f"kwim:{_graph_name(team)}:{session}:{key}"
        await (conn.set(k, value, ex=ttl) if ttl else conn.set(k, value))

    async def working_get(self, team: str, session: str, key: str) -> str | None:
        conn = self._db.connection
        v = await conn.get(f"kwim:{_graph_name(team)}:{session}:{key}")
        return v.decode() if isinstance(v, bytes) else v

    async def working_list(self, team: str, session: str) -> list[dict]:
        """Key + TTL for every working-memory key under one session (diagnostic;
        values are not returned). Uses SCAN, which does not block the shared instance."""
        conn = self._db.connection
        prefix = f"kwim:{_graph_name(team)}:{session}:"
        rows: list[dict] = []
        cursor = 0
        while True:
            cursor, keys = await conn.scan(cursor=cursor, match=f"{prefix}*", count=100)
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                ttl = await conn.ttl(key)
                rows.append({
                    "session": session, "key": key[len(prefix):],
                    "ttl_seconds": ttl if ttl is not None and ttl >= 0 else None,
                })
            if cursor == 0:
                break
        return rows

    # --- proposal status (Redis, with a TTL), set on propose and on resolve ---
    async def proposal_set(self, proposal_id: str, doc: dict[str, Any], ttl: int = 7 * 24 * 3600) -> None:
        import json as _json
        await self._db.connection.set(f"kwim:proposal:{proposal_id}", _json.dumps(doc), ex=ttl)

    # --- Preview tokens: TTL'd, consumed with GETDEL ---

    async def forget_preview_set(self, token: str, doc: dict[str, Any], ttl: int) -> None:
        import json as _json
        await self._db.connection.set(
            f"kwim:forget-preview:{token}", _json.dumps(doc), ex=ttl)

    async def destroy_preview_set(self, token: str, doc: dict[str, Any], ttl: int) -> None:
        import json as _json
        await self._db.connection.set(
            f"kwim:destroy-preview:{token}", _json.dumps(doc), ex=ttl)

    async def destroy_preview_getdel(self, token: str) -> dict[str, Any] | None:
        import json as _json
        raw = await self._db.connection.execute_command(
            "GETDEL", f"kwim:destroy-preview:{token}")
        if raw is None:
            return None
        return _json.loads(raw if isinstance(raw, str) else raw.decode())

    async def forget_preview_getdel(self, token: str) -> dict[str, Any] | None:
        import json as _json
        raw = await self._db.connection.execute_command(
            "GETDEL", f"kwim:forget-preview:{token}")
        if raw is None:
            return None
        return _json.loads(raw if isinstance(raw, str) else raw.decode())

    # --- Semantic memory (vector index) ---

    # SemanticItem properties a metadata key may not overwrite.
    _SEMANTIC_RESERVED = {"id", "content", "embedding", "metadata", "created_at"}

    async def materialize_semantic(self, team: str, item: dict[str, Any], graph_name: str | None = None, created_at: int | None = None) -> None:
        """Create/upsert a :SemanticItem node with its vector.

        Upserts on id. Metadata keys are also copied to node properties for
        filtering. `created_at` is set only on creation.
        """
        g = await self._graph(team, graph_name)
        metadata = item.get("metadata", {})
        params: dict[str, Any] = {
            "id": item["id"],
            "content": item["content"],
            "embedding": item["embedding"],
            "metadata_json": _json.dumps(metadata),
            "created_at": created_at,
        }
        # Promote each metadata key to a direct node property for Cypher filtering.
        meta_sets: list[str] = []
        for k, v in metadata.items():
            if k in self._SEMANTIC_RESERVED:
                continue
            safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
            params[f"meta_{safe_key}"] = v
            meta_sets.append(f"s.{safe_key}=$meta_{safe_key}")

        set_clause = (
            "ON CREATE SET s.created_at = coalesce($created_at, timestamp()) "
            "SET s.content=$content, s.embedding=vecf32($embedding), "
            "    s.metadata=$metadata_json"
        )
        if meta_sets:
            set_clause += ", " + ", ".join(meta_sets)

        await g.query(
            f"MERGE (s:SemanticItem {{id:$id}}) {set_clause}",
            params,
        )

    async def query_semantic(
        self,
        team: str,
        qvec: list[float] | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict]:
        """KNN vector query over the team's SemanticItem index, optionally filtered
        by metadata properties; metadata only when `qvec` is None. `score` is a
        cosine distance, lower is closer.
        """
        g = await self._graph(team)
        filter_clauses: list[str] = []
        params: dict[str, Any] = {"k": limit}
        if filters:
            for k, v in filters.items():
                safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
                params[f"filter_{safe_key}"] = v
                filter_clauses.append(f"node.{safe_key}=$filter_{safe_key}")

        if qvec is not None:
            params["qvec"] = qvec
            cypher = (
                "CALL db.idx.vector.queryNodes('SemanticItem', 'embedding', $k, vecf32($qvec)) "
                "YIELD node, score "
            )
            if filter_clauses:
                cypher += "WHERE " + " AND ".join(filter_clauses) + " "
            cypher += (
                "RETURN node.id, node.content, node.metadata, score "
                "ORDER BY score ASC LIMIT $k"
            )
        else:
            # Metadata-only match (no vector search)
            cypher = "MATCH (s:SemanticItem) "
            if filter_clauses:
                # Rewrite filter_clauses from 'node.' to 's.' for MATCH context
                rewritten = [fc.replace("node.", "s.") for fc in filter_clauses]
                cypher += "WHERE " + " AND ".join(rewritten) + " "
            cypher += "RETURN s.id, s.content, s.metadata, 0.0 AS score"

        res = await g.query(cypher, params)
        rows: list[dict] = []
        for r in res.result_set:
            meta_raw = r[2]
            rows.append({
                "id": r[0],
                "content": r[1],
                "metadata": _json.loads(meta_raw) if meta_raw else {},
                "score": float(r[3]),
            })
        return rows

    async def get_by_metadata(self, team: str, filters: dict[str, Any]) -> list[dict]:
        """Metadata-only lookup (no vector). Returns items matching all filters."""
        if not filters:
            return []
        g = await self._graph(team)
        where_clauses: list[str] = []
        params: dict[str, Any] = {}
        for k, v in filters.items():
            safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
            params[f"filter_{safe_key}"] = v
            where_clauses.append(f"s.{safe_key}=$filter_{safe_key}")

        cypher = (
            "MATCH (s:SemanticItem) WHERE " + " AND ".join(where_clauses) + " "
            "RETURN s.id, s.content, s.metadata"
        )
        res = await g.query(cypher, params)
        rows: list[dict] = []
        for r in res.result_set:
            meta_raw = r[2]
            rows.append({
                "id": r[0],
                "content": r[1],
                "metadata": _json.loads(meta_raw) if meta_raw else {},
            })
        return rows

    async def list_semantic(
        self, team: str, *, filters: dict[str, Any] | None = None,
        cursor: dict[str, Any] | None = None, limit: int = 50,
    ) -> list[dict]:
        """Full semantic-item browse, cursor-paginated on `(created_at, id)`.

        Unlike get_by_metadata, lists every item when no filter is given.
        """
        g = await self._graph(team)
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        for k, v in (filters or {}).items():
            safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
            params[f"filter_{safe_key}"] = v
            clauses.append(f"s.{safe_key}=$filter_{safe_key}")
        if cursor is not None:
            clauses.append(
                "(s.created_at > $ccreated OR (s.created_at = $ccreated AND s.id > $cid))")
            params["ccreated"] = cursor["created_at"]
            params["cid"] = cursor["id"]
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        res = await g.query(
            f"MATCH (s:SemanticItem) {where}"
            "RETURN s.id, s.content, s.metadata, s.created_at "
            "ORDER BY s.created_at ASC, s.id ASC LIMIT $limit",
            params,
        )
        rows: list[dict] = []
        for r in res.result_set:
            meta_raw = r[2]
            rows.append({
                "id": r[0], "content": r[1],
                "metadata": _json.loads(meta_raw) if meta_raw else {},
                "created_at": r[3],
            })
        return rows

    async def count_semantic(self, team: str, *, filters: dict[str, Any] | None = None) -> int:
        g = await self._graph(team)
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for k, v in (filters or {}).items():
            safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", k)
            params[f"filter_{safe_key}"] = v
            clauses.append(f"s.{safe_key}=$filter_{safe_key}")
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        res = await g.query(f"MATCH (s:SemanticItem) {where}RETURN count(s)", params)
        return int(res.result_set[0][0]) if res.result_set else 0

    async def proposal_get(self, proposal_id: str) -> dict[str, Any] | None:
        import json as _json
        v = await self._db.connection.get(f"kwim:proposal:{proposal_id}")
        if v is None:
            return None
        return _json.loads(v.decode() if isinstance(v, bytes) else v)

    # --- Forget (hard delete), used by kwim_api.forget -----------------

    async def get_object_for_forget(
        self, team: str, object_id: str, object_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve an object for the forget path: its type, status, a short label, and
        the episodic_event_ids it is SUPPORTED_BY. None if not found."""
        found = await self.find_object(team, object_id, object_type)
        if found is None:
            return None
        otype, status = found
        label = "Fact" if otype == "fact" else "Rule"
        text_field = "statement" if otype == "fact" else "approach"
        g = await self._graph(team)
        res = await g.query(
            f"MATCH (n:{label} {{id:$id}}) "
            "OPTIONAL MATCH (n)-[:SUPPORTED_BY]->(e:Evidence) "
            f"RETURN n.{text_field}, collect(DISTINCT e.episodic_event_id)",
            {"id": object_id},
        )
        r = res.result_set[0] if res.result_set else [None, []]
        return {
            "id": object_id, "type": otype, "status": status,
            "label": r[0], "evidence": [x for x in (r[1] or []) if x is not None],
        }

    async def objects_supported_by(self, team: str, episodic_id: str) -> list[str]:
        """All :Fact/:Rule ids SUPPORTED_BY an Evidence carrying this episodic_event_id
        - the shared-evidence guard's refcount source."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (o)-[:SUPPORTED_BY]->(:Evidence {episodic_event_id:$eid}) "
            "WHERE o:Fact OR o:Rule RETURN collect(DISTINCT o.id)",
            {"eid": episodic_id},
        )
        return list(res.result_set[0][0]) if res.result_set and res.result_set[0][0] else []

    async def select_forget_ids(
        self, team: str, *, object_type: str, fact_type: str | None = None,
        source_kind: str | None = None, status: str | None = None,
        statement_contains: str | None = None,
    ) -> list[str]:
        """Batch selector: object ids matching the given filters. Metadata alone
        (fact_type/source_kind/status) or statement text (`statement_contains`)."""
        label = "Fact" if object_type == "fact" else "Rule"
        text_field = "statement" if object_type == "fact" else "approach"
        g = await self._graph(team)
        where, params = [], {}
        if fact_type:
            where.append("n.fact_type=$ft"); params["ft"] = fact_type
        if source_kind:
            where.append("n.source_kind=$sk"); params["sk"] = source_kind
        if status:
            where.append("n.status=$st"); params["st"] = status
        if statement_contains:
            where.append(f"n.{text_field} CONTAINS $sc"); params["sc"] = statement_contains
        clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
        res = await g.query(f"MATCH (n:{label}) {clause}RETURN n.id", params)
        return [r[0] for r in res.result_set]

    async def forget_node(self, team: str, object_type: str, object_id: str) -> None:
        """DETACH DELETE the :Fact/:Rule node (removes node, edges, embedding), then
        delete any :Evidence node it leaves orphaned (no remaining SUPPORTED_BY)."""
        label = "Fact" if object_type == "fact" else "Rule"
        g = await self._graph(team)
        await g.query(f"MATCH (n:{label} {{id:$id}}) DETACH DELETE n", {"id": object_id})
        await g.query(
            "MATCH (e:Evidence) WHERE NOT ()-[:SUPPORTED_BY]->(e) DETACH DELETE e", {})

    async def get_semantic_for_forget(
        self, team: str, item_id: str,
    ) -> dict[str, Any] | None:
        """Resolve a :SemanticItem for the forget path: its id and content. None if
        not found."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (n:SemanticItem {id:$id}) RETURN n.id, n.content", {"id": item_id})
        if not res.result_set:
            return None
        row = res.result_set[0]
        return {"id": row[0], "content": row[1] or ""}

    async def forget_semantic_node(self, team: str, item_id: str) -> bool:
        """DETACH DELETE the :SemanticItem node (removes node and embedding).

        Returns True if the node is gone afterwards."""
        g = await self._graph(team)
        await g.query("MATCH (n:SemanticItem {id:$id}) DETACH DELETE n", {"id": item_id})
        check = await g.query(
            "MATCH (n:SemanticItem {id:$id}) RETURN n.id", {"id": item_id})
        return not check.result_set
