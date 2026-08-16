"""FalkorDB store - the queryable projection: K + W graph, semantic vector index,
and working-memory TTL keys.

Tenancy: graph-per-tenant (`kwim_<team>`). The KWIM service owns the graph schema -
it ensures constraints + the vector index exist on first touch of a team's graph
(graph-init is the service's job, not the provisioner's). Working memory uses plain
Redis TTL keys on the same instance, not graph nodes.

Universe graph: `kwim_universe` is a peer tenant graph holding scope=universe rules
promoted from team graphs. It is accessed via the same `_graph` path with the
literal pseudo-team "universe". `query_rules` merges team + universe results.
"""
import json as _json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

from falkordb.asyncio import FalkorDB

from ..config import settings
from ..freshness import resolve_decay_class

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

# "universe" is the reserved pseudo-team name for the shared graph kwim_universe
# (messaging.universe_graph in kwim.defaults.yaml, env-overridable).
_UNIVERSE = settings.universe_graph


def _graph_name(team: str) -> str:
    # "universe" is the reserved pseudo-team for the shared kwim_universe graph.
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")
    return f"kwim_{team}"


# Shared :Fact read projection. `query_facts` (tag/structured) and `search_facts`
# (semantic KNN) must return the identical row shape - both feed `_enrich_facts`
# and the `Fact` model, and memory/context unions their results into one list.
_FACT_FIELDS = ("id", "statement", "fact_type", "status", "created_at", "about",
                "decay_class", "source_kind", "last_verified_at")


def _fact_projection(alias: str) -> str:
    return ", ".join(f"{alias}.{f}" for f in _FACT_FIELDS)


def _fact_row(r: Any) -> dict:
    """Map a `_fact_projection` result row to the standard fact dict."""
    return {
        "id": r[0], "statement": r[1], "fact_type": r[2], "status": r[3],
        "created_at": str(r[4]), "about": list(r[5]) if r[5] else [],
        "decay_class": r[6] or "slow", "source_kind": r[7] or None,
        "last_verified_at": str(r[8]) if r[8] is not None else None,
    }


def _code_graph_name(team: str) -> str:
    """The team's code graph - a sibling of kwim_<team>, deliberately separate.

    rebuild.py replays commit_log into kwim_<team> and swaps it live; anything not
    in commit_log is wiped. The code graph's source of truth is the repo, not
    commit_log, so it lives in its own graph that rebuild never touches.
    """
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")
    return f"kwim_{team}_code"


# Graph schema (FalkorDB DDL)
#   - range indexes via `CREATE INDEX FOR (n:L) ON (n.p)` - no `IF NOT EXISTS`;
#     re-running throws "already indexed" (caught below for restart-idempotency).
#   - uniqueness is provided by MERGE-on-id in the writers, so hard
#     GRAPH.CONSTRAINTs are deferred (they need a supporting index + the redis-level
#     GRAPH.CONSTRAINT command; not needed for v1 correctness).
#   - the vector index powers semantic Memory recall.
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

# Code-graph schema - applied to kwim_<team>_code only, never kwim_<team>.
# Node ids are content-stable qualified names (e.g. "repo:path::qualified.name") so
# MERGE-on-id upserts are idempotent across incremental re-extraction.
_CODE_INIT_CYPHER = [
    "CREATE INDEX FOR (f:File) ON (f.id)",
    "CREATE INDEX FOR (f:File) ON (f.repo)",
    "CREATE INDEX FOR (f:File) ON (f.path)",
    "CREATE INDEX FOR (fn:Function) ON (fn.id)",
    "CREATE INDEX FOR (fn:Function) ON (fn.repo)",
    "CREATE INDEX FOR (fn:Function) ON (fn.name)",
    "CREATE INDEX FOR (c:Class) ON (c.id)",
    "CREATE INDEX FOR (c:Class) ON (c.repo)",
    "CREATE INDEX FOR (c:Class) ON (c.name)",
    # semantic code recall
    "CREATE VECTOR INDEX FOR (fn:Function) ON (fn.embedding) "
    f"OPTIONS {{dimension:{settings.embed_dim}, similarityFunction:'cosine'}}",
]


class FalkorStore:
    def __init__(self) -> None:
        self._db: FalkorDB | None = None
        self._inited: set[str] = set()

    async def connect(self) -> None:
        # Discrete kwargs (no redis:// URL) so if the FalkorDB password is base64 and
        # contains +/= it can't corrupt a URL.
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

        `graph_name` overrides the derived name (used by rebuild to target a temp
        graph while keeping the team's identifier for validation).
        """
        name = graph_name or _graph_name(team)
        g = self._db.select_graph(name)
        await self._ensure_schema(g, name, _INIT_CYPHER)
        return g

    async def _code_graph(self, team: str):
        """Return the team's code graph (kwim_<team>_code), ensuring its schema.

        Separate from _graph: distinct graph name + distinct DDL, so rebuild's
        commit_log replay can never touch it.
        """
        name = _code_graph_name(team)
        g = self._db.select_graph(name)
        await self._ensure_schema(g, name, _CODE_INIT_CYPHER)
        return g

    async def materialize_fact(
        self, team: str, fact: dict[str, Any], provenance: dict[str, Any],
        graph_name: str | None = None, embedding: list[float] | None = None,
    ) -> None:
        """Create/upsert a :Fact node + its provenance edges (gate commit path).

        `embedding` is optional (the screen sets it; screen-skipped facts and pre-index
        facts leave it absent - the node simply lacks the property and won't
        appear in query_similar_facts KNN results until a rebuild re-embeds it).
        """
        g = await self._graph(team, graph_name)
        await g.query(
            "MERGE (f:Fact {id:$id}) "
            "SET f.statement=$statement, f.fact_type=$fact_type, f.status='current', "
            "    f.source_kind=$source_kind, f.commit_seq=$seq, f.created_at=timestamp(), "
            "    f.about=$about, f.decay_class=$decay_class",
            {"id": fact["id"], "statement": fact["statement"], "fact_type": fact["fact_type"],
             "source_kind": fact.get("source_kind", "agent_proposal"), "seq": fact["commit_seq"],
             "about": fact.get("about", []),
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

    async def reaffirm_fact(self, team: str, fact_id: str) -> bool:
        """Stamp last_verified_at = now on a current :Fact. Non-destructive;
        does not write commit_log. Returns True if the fact existed."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (f:Fact {id:$id}) WHERE f.status='current' "
            "SET f.last_verified_at = timestamp() RETURN f.id",
            {"id": fact_id},
        )
        return bool(res.result_set and res.result_set[0][0] is not None)

    async def query_similar_facts(
        self, team: str, vector: list[float], k: int = 5,
        about: list[str] | None = None, fact_type: str | None = None,
    ) -> list[dict]:
        """KNN over :Fact embeddings - powered by the :Fact vector index.

        Returns only status='current' facts, ascending distance (lower = closer),
        mirroring query_semantic's KNN shape. Facts without an embedding (screen-skipped
        or pre-index) are absent from the index and won't appear in results.

        Scoped mode (about + fact_type): restricts candidates to current facts of the
        same fact_type whose about set contains every proposal about ref, computing
        cosine distance server-side over the exact candidate set.
        """
        g = await self._graph(team)
        if about and fact_type:
            # Exact, server-side scoped screen. Filters first, then computes
            # vec.cosineDistance over the small matching set - no KNN top-k cliff.
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
                # Vector index may not have any vectors yet (empty team, or no facts
                # have been embedded). Return [] rather than crashing the gate.
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
        for Knowledge, and the counterpart to `query_facts`' structured filter.
        `query_facts` answers "give me the facts tagged X"; this answers "what do we
        know that relates to this?" when the caller cannot know the tag.

        Deliberately separate from `query_similar_facts`, which serves the gate's
        write-side dedup screen. That one scopes with AND-all, case-sensitive `about`
        matching because it is deciding whether two proposals are the same fact;
        this one mirrors `query_facts`' case-insensitive ANY-membership so `about`
        means the same thing on both read paths. Keeping them apart means tuning
        retrieval can never silently change what the gate rejects as a duplicate.

        `score` is a cosine distance - lower = closer (identical -> 0.0), matching
        `query_semantic`, so callers rank ascending.

        Returns only status='current' facts. Facts with no embedding (committed
        while the embedder was down - the gate fails open - or predating the index)
        cannot match; `app.backfill_embeddings` is the repair path.
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
            # Filter first, then score what survives. Going through the vector index
            # here would apply the filter after the top-k cut, so a tag whose facts
            # sit outside the global top-k would return nothing - the same top-k
            # cliff `query_similar_facts`' scoped mode avoids.
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
        until backfilled. Ordered by commit_seq so repeated runs are deterministic.
        See `app.backfill_embeddings`."""
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

        Non-destructive - touches only the vector property, leaving the statement,
        status and every provenance edge alone. Reads back so a silent no-op (id
        gone, write rejected) surfaces to the caller rather than counting as done.
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

        Returns None if the fact is not in the team graph. Evidence is returned as
        episodic_event_id references (not hydrated from Postgres).
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
        chain (SUPERSEDES* lineage), newest-first, each version carrying its own
        evidence (episodic_event_id refs) + proposing agent.

        v1 is not point-in-time - `?at=` is deferred (the graph has no
        valid_from/superseded_at; the commit_log is the authoritative time source).
        Returns [] if the fact is not in the team graph.
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

    # :Rule node properties the situation must not overwrite. A situation key
    # colliding with one of these is skipped (kept in situation_json only).
    _RULE_RESERVED = {
        "id", "rule_type", "status", "scope", "evidence_count", "commit_seq",
        "created_at", "situation_json", "approach", "action_pattern", "verdict",
        "authority", "severity", "check_tier", "promoted_from_id",
        "promoted_from_team",
    }

    async def materialize_rule(self, team: str, rule: dict[str, Any], provenance: dict[str, Any], graph_name: str | None = None) -> None:
        """Create/upsert a :Rule node + its provenance edges (gate commit path).

        Mirrors materialize_fact. `rule` must include at minimum: id, rule_type,
        status, scope, evidence_count, commit_seq. Advisory rules carry a situation
        dict + approach; constraint rules carry action_pattern, verdict, authority,
        severity, check_tier. Missing optional fields default to empty string/None
        so the node is always well-formed for replay.

        Every situation key is promoted to a direct node property (the
        materialize_semantic pattern) so query_rules can WHERE-filter on any
        team-defined key. situation_json remains the full-fidelity truth.
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
            "SET r.rule_type=$rule_type, r.status=$status, r.scope=$scope, "
            "    r.evidence_count=$evidence_count, r.commit_seq=$seq, "
            "    r.created_at=timestamp(), "
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

        `situation` is an open dict of team-defined key/values, AND-matched
        against the promoted situation properties (see materialize_rule).
        Keys colliding with reserved node properties are ignored.

        Returns empty list if the graph doesn't exist yet (tolerates missing
        universe graph on first deploy before any promotion has occurred).
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

        - `situation` is an open dict of team-defined key/values, AND-matched
          against promoted situation properties. None/empty = no situation filter.
        - Team rules and universe rules are both returned, tagged with source.
        - Dedup: when a team rule has been promoted, the universe copy carries
          `promoted_from_id`; if a team rule's id appears as a universe copy's
          promoted_from_id, the team original is suppressed (the universe copy wins).
        - Sorted evidence_count DESC overall after merge.
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

        Returns True on success, False if the rule is absent or not approved
        (caller should reject the proposal in that case).
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

        When `object_type` is given, looks up that label only (the MM-button path,
        where the type was recorded at notify time). Otherwise checks both labels
        (the REST path, where the caller only has the object_id).
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
        """Flip a committed :Fact/:Rule to status='retracted' (governed forgetting).

        Mirrors the existing supersede path's `status='superseded'` flip.
        `query_facts`/`query_rules` filter on `status='current'`/`'approved'`, so a
        retracted object stops being served immediately."""
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

    # --- proposal status tracking (Redis; committed objects live in commit_log) ---
    # Shared between the API (on propose) and the gate consumer (on resolve). TTL'd:
    # the durable record of what committed is the Postgres commit_log, not this.
    async def proposal_set(self, proposal_id: str, doc: dict[str, Any], ttl: int = 7 * 24 * 3600) -> None:
        import json as _json
        await self._db.connection.set(f"kwim:proposal:{proposal_id}", _json.dumps(doc), ex=ttl)

    # --- Semantic memory (vector index) ---

    # Node properties reserved by the SemanticItem schema; metadata keys with these
    # names are not promoted to direct properties (they stay inside metadata JSON).
    _SEMANTIC_RESERVED = {"id", "content", "embedding", "metadata", "created_at"}

    async def materialize_semantic(self, team: str, item: dict[str, Any], graph_name: str | None = None) -> None:
        """Create/upsert a :SemanticItem node with its vector.

        Idempotent on id (the episodic event_id is reused as the SemanticItem id
        so redelivery cannot duplicate).

        Promotes metadata keys to node properties so Cypher WHERE clauses can
        filter on them efficiently (e.g. s.locale='en').
        """
        g = await self._graph(team, graph_name)
        metadata = item.get("metadata", {})
        params: dict[str, Any] = {
            "id": item["id"],
            "content": item["content"],
            "embedding": item["embedding"],
            "metadata_json": _json.dumps(metadata),
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
            "SET s.content=$content, s.embedding=vecf32($embedding), "
            "    s.metadata=$metadata_json, s.created_at=timestamp()"
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
        by metadata properties.

        If `qvec` is None, performs a metadata-only match (no embedding search).

        `score` is cosine **DISTANCE - lower = closer** (identical vector -> 0.0,
        orthogonal -> 1.0), so the ranking is `ORDER BY score ASC`.
        The returned `score` is therefore a distance (0 = best match), not a
        similarity; callers should treat smaller as more relevant.
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

    async def proposal_get(self, proposal_id: str) -> dict[str, Any] | None:
        import json as _json
        v = await self._db.connection.get(f"kwim:proposal:{proposal_id}")
        if v is None:
            return None
        return _json.loads(v.decode() if isinstance(v, bytes) else v)

    # --- Forget (hard-removal) ------------------
    # DESTRUCTIVE. Unlike retract_object (soft: status flip), these remove data.
    # Used by the forget path: the API Forget button (via gate.forget_object) and
    # the standalone `python -m app.forget` operator CLI. Never a raw endpoint.

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
        (fact_type/source_kind/status) often can't separate garbage from legit -
        e.g. code_hub facts share every field and differ only in statement text - so
        `statement_contains` targets by content (the only safe way to forget the
        mcp-snapshot hubs without taking the real ones)."""
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
        not found. The semantic counterpart of `get_object_for_forget` - with no
        evidence to collect, because semantic items are written directly by
        `materialize_semantic` and are never SUPPORTED_BY anything."""
        g = await self._graph(team)
        res = await g.query(
            "MATCH (n:SemanticItem {id:$id}) RETURN n.id, n.content", {"id": item_id})
        if not res.result_set:
            return None
        row = res.result_set[0]
        return {"id": row[0], "content": row[1] or ""}

    async def forget_semantic_node(self, team: str, item_id: str) -> bool:
        """DETACH DELETE the :SemanticItem node (removes node and embedding).

        Returns True if the node is gone afterwards. Unlike `forget_node` there is
        no orphaned-:Evidence sweep: a semantic item carries no SUPPORTED_BY edges,
        and unlike facts/rules it has no commit_log row either - the node is the
        whole object, so this alone is a complete removal."""
        g = await self._graph(team)
        await g.query("MATCH (n:SemanticItem {id:$id}) DETACH DELETE n", {"id": item_id})
        check = await g.query(
            "MATCH (n:SemanticItem {id:$id}) RETURN n.id", {"id": item_id})
        return not check.result_set

    # --- Code graph -----------------------------------------------------------
    # All writes/reads target kwim_<team>_code via _code_graph(). The graph holds
    # structure/signatures/summaries/embeddings,never file bodies.
    # Node ids are content-stable qualified names so MERGE upserts are idempotent
    # across incremental re-extraction.

    async def materialize_code_file(
        self, team: str, *, file_id: str, repo: str, path: str, lang: str,
        content_hash: str, commit: str = "",
    ) -> None:
        g = await self._code_graph(team)
        await g.query(
            "MERGE (f:File {id:$id}) "
            "SET f.repo=$repo, f.path=$path, f.lang=$lang, "
            "    f.content_hash=$content_hash, f.commit=$commit, f.indexed_at=timestamp()",
            {"id": file_id, "repo": repo, "path": path, "lang": lang,
             "content_hash": content_hash, "commit": commit},
        )

    async def materialize_code_function(
        self, team: str, *, fn_id: str, repo: str, path: str, name: str,
        signature: str = "", summary: str = "", start_line: int = 0, end_line: int = 0,
        file_id: str | None = None, embedding: list[float] | None = None,
    ) -> None:
        g = await self._code_graph(team)
        await g.query(
            "MERGE (fn:Function {id:$id}) "
            "SET fn.repo=$repo, fn.path=$path, fn.name=$name, fn.signature=$signature, "
            "    fn.summary=$summary, fn.start_line=$start_line, fn.end_line=$end_line",
            {"id": fn_id, "repo": repo, "path": path, "name": name, "signature": signature,
             "summary": summary, "start_line": start_line, "end_line": end_line},
        )
        if embedding is not None:
            await g.query(
                "MATCH (fn:Function {id:$id}) SET fn.embedding=vecf32($embedding)",
                {"id": fn_id, "embedding": embedding},
            )
        if file_id:
            await g.query(
                "MATCH (f:File {id:$fid}) MATCH (fn:Function {id:$nid}) "
                "MERGE (f)-[:CONTAINS]->(fn)",
                {"fid": file_id, "nid": fn_id},
            )

    async def materialize_code_class(
        self, team: str, *, cls_id: str, repo: str, path: str, name: str,
        methods: list[str] | None = None, file_id: str | None = None,
    ) -> None:
        g = await self._code_graph(team)
        await g.query(
            "MERGE (c:Class {id:$id}) "
            "SET c.repo=$repo, c.path=$path, c.name=$name, c.methods=$methods",
            {"id": cls_id, "repo": repo, "path": path, "name": name,
             "methods": methods or []},
        )
        if file_id:
            await g.query(
                "MATCH (f:File {id:$fid}) MATCH (c:Class {id:$cid}) "
                "MERGE (f)-[:CONTAINS]->(c)",
                {"fid": file_id, "cid": cls_id},
            )

    async def materialize_call_edge(
        self, team: str, *, caller_id: str, callee_id: str,
        confidence: float, strategy: str, candidates: int = 1,
    ) -> None:
        """Resolved CALLS edge carrying confidence/strategy/candidates (from the
        confidence cascade in the resolver)."""
        g = await self._code_graph(team)
        await g.query(
            "MATCH (a:Function {id:$caller}) MATCH (b:Function {id:$callee}) "
            "MERGE (a)-[r:CALLS]->(b) "
            "SET r.confidence=$confidence, r.strategy=$strategy, r.candidates=$candidates",
            {"caller": caller_id, "callee": callee_id, "confidence": confidence,
             "strategy": strategy, "candidates": candidates},
        )

    async def materialize_import_edge(
        self, team: str, *, file_id: str, target: str, kind: str = "internal",
    ) -> None:
        """IMPORTS edge from a File to another File (internal) or an API symbol
        (external). The target node is MERGEd by id (File) or symbol (API)."""
        g = await self._code_graph(team)
        if kind == "external":
            await g.query(
                "MATCH (f:File {id:$fid}) MERGE (api:API {symbol:$target}) "
                "MERGE (f)-[:IMPORTS]->(api)",
                {"fid": file_id, "target": target},
            )
        else:
            await g.query(
                "MATCH (f:File {id:$fid}) MATCH (t:File {id:$target}) "
                "MERGE (f)-[:IMPORTS]->(t)",
                {"fid": file_id, "target": target},
            )

    async def set_member_of(self, team: str, *, fn_id: str, community: int) -> None:
        """Louvain community membership: (:Function)-[:MEMBER_OF]->(:Community)."""
        g = await self._code_graph(team)
        await g.query(
            "MATCH (fn:Function {id:$id}) MERGE (cm:Community {id:$cid}) "
            "MERGE (fn)-[:MEMBER_OF]->(cm)",
            {"id": fn_id, "cid": community},
        )

    async def prune_repo_files(self, team: str, *, repo: str, keep_paths: list[str]) -> int:
        """Delete File nodes (and their contained Function/Class nodes) for `repo`
        whose path is not in keep_paths. MERGE-based extraction only adds/updates;
        this is what removes files that were deleted from the repo or newly excluded
        (e.g. via .cgignore) so they stop polluting queries + the distiller. Returns
        the number of files pruned."""
        g = await self._code_graph(team)
        res = await g.query(
            "MATCH (f:File {repo:$repo}) WHERE NOT f.path IN $keep "
            "WITH f, f.path AS p "
            "OPTIONAL MATCH (f)-[:CONTAINS]->(d) "
            "DETACH DELETE f, d "
            "RETURN count(DISTINCT p)",
            {"repo": repo, "keep": keep_paths},
        )
        return int(res.result_set[0][0]) if res.result_set else 0

    async def code_indexed_repos(self, team: str) -> set[str]:
        """Distinct repos present in the team's code graph - backs the
        repo_not_indexed coverage signal. Empty set if the graph is empty."""
        g = await self._code_graph(team)
        try:
            res = await g.query("MATCH (f:File) RETURN DISTINCT f.repo")
        except Exception as exc:
            log.warning("falkor: code_indexed_repos failed: %s", exc)
            return set()
        return {r[0] for r in res.result_set if r[0]}

    async def code_files_by_hash(self, team: str, repo: str) -> dict[str, str]:
        """{path: content_hash} for a repo - drives incremental re-extraction
        (skip files whose hash is unchanged). Empty if the repo isn't indexed."""
        g = await self._code_graph(team)
        res = await g.query(
            "MATCH (f:File {repo:$repo}) RETURN f.path, f.content_hash",
            {"repo": repo},
        )
        return {r[0]: r[1] for r in res.result_set}

    async def code_search(
        self, team: str, *, qvec: list[float] | None = None, name: str | None = None,
        repos: list[str] | None = None, limit: int = 10,
    ) -> list[dict]:
        """Find functions by semantic vector (qvec) or exact name, optionally scoped
        to repos. Returns signatures + summaries + (for vector) distance score."""
        g = await self._code_graph(team)
        params: dict[str, Any] = {"k": limit}
        repo_clause = ""
        if repos:
            params["repos"] = repos
            repo_clause = "WHERE node.repo IN $repos "
        if qvec is not None:
            params["qvec"] = qvec
            cypher = (
                "CALL db.idx.vector.queryNodes('Function', 'embedding', $k, vecf32($qvec)) "
                "YIELD node, score " + repo_clause +
                "RETURN node.id, node.name, node.signature, node.summary, node.repo, "
                "node.path, score ORDER BY score ASC LIMIT $k"
            )
        else:
            where = ["node.name=$name"] if name else []
            if repos:
                where.append("node.repo IN $repos")
            if name:
                params["name"] = name
            cypher = (
                "MATCH (node:Function) "
                + ("WHERE " + " AND ".join(where) + " " if where else "")
                + "RETURN node.id, node.name, node.signature, node.summary, node.repo, "
                "node.path, 0.0 AS score LIMIT $k"
            )
        try:
            res = await g.query(cypher, params)
        except Exception as exc:
            log.warning("falkor: code_search failed (likely empty index): %s", exc)
            return []
        return [
            {"id": r[0], "name": r[1], "signature": r[2], "summary": r[3],
             "repo": r[4], "path": r[5], "score": float(r[6])}
            for r in res.result_set
        ]

    async def code_trace_calls(
        self, team: str, *, fn_id: str, direction: str = "outbound", depth: int = 2,
        min_confidence: float = 0.0,
    ) -> list[dict]:
        """Call-chain traversal. direction='outbound' = callees (deps),
        'inbound' = callers (impact). Bounded by depth (1..N). Edges below
        min_confidence are excluded so low-trust resolutions don't mislead."""
        depth = max(1, min(int(depth), 5))
        arrow = "-[:CALLS*1..%d]->" % depth if direction == "outbound" else "<-[:CALLS*1..%d]-" % depth
        g = await self._code_graph(team)
        res = await g.query(
            "MATCH p = (seed:Function {id:$id})" + arrow + "(other:Function) "
            "WITH other, [e IN relationships(p) | e.confidence] AS confs "
            "WHERE ALL(c IN confs WHERE c >= $minc) "
            "RETURN DISTINCT other.id, other.name, other.signature, other.repo, "
            "other.path, reduce(m=1.0, c IN confs | CASE WHEN c < m THEN c ELSE m END) AS path_conf "
            "ORDER BY path_conf DESC",
            {"id": fn_id, "minc": min_confidence},
        )
        return [
            {"id": r[0], "name": r[1], "signature": r[2], "repo": r[3],
             "path": r[4], "path_confidence": float(r[5])}
            for r in res.result_set
        ]

    async def code_architecture(self, team: str, *, repos: list[str] | None = None) -> dict:
        """High-level structure: per-community size + representative hub functions
        (highest inbound CALLS). Backs get_architecture."""
        g = await self._code_graph(team)
        params: dict[str, Any] = {}
        scope = ""
        if repos:
            params["repos"] = repos
            scope = "WHERE fn.repo IN $repos "
        res = await g.query(
            "MATCH (fn:Function)-[:MEMBER_OF]->(cm:Community) " + scope +
            "OPTIONAL MATCH (fn)<-[:CALLS]-(caller:Function) "
            "WITH cm, fn, count(caller) AS fan_in "
            "WITH cm, count(fn) AS size, collect({name:fn.name, repo:fn.repo, "
            "  path:fn.path, fan_in:fan_in}) AS members "
            "RETURN cm.id, size, members ORDER BY size DESC",
            params,
        )
        communities = []
        for r in res.result_set:
            members = sorted(r[2], key=lambda m: m.get("fan_in", 0), reverse=True)
            communities.append({
                "community": r[0], "size": int(r[1]),
                "hubs": members[:5],
            })
        return {"communities": communities}

    async def code_hubs(
        self, team: str, *, repo: str | None = None,
        min_fan_in: int = settings.cg_min_fan_in,
        min_confidence: float = settings.cg_min_confidence,
    ) -> list[dict]:
        """Load-bearing functions for a repo using PageRank + cross-community
        bridging, with a confidence-filtered fan-in floor. Returns rows of
        {name, repo, pagerank, fan_in, bridged}. Backs the distiller's
        architecture summary facts."""
        g = await self._code_graph(team)
        params: dict[str, Any] = {"minc": min_confidence, "minf": min_fan_in}
        repo_filter = ""
        if repo:
            params["repo"] = repo
            repo_filter = "AND fn.repo = $repo "
        res = await g.query(
            "CALL algo.pageRank('Function','CALLS') YIELD node, score "
            "WITH node AS fn, score AS pr "
            "OPTIONAL MATCH (caller:Function)-[r:CALLS]->(fn) WHERE r.confidence >= $minc "
            "OPTIONAL MATCH (caller)-[:MEMBER_OF]->(cc:Community) "
            "WITH fn, pr, count(DISTINCT caller) AS fan_in, count(DISTINCT cc) AS bridged "
            f"WHERE fan_in >= $minf {repo_filter}"
            "RETURN fn.name, fn.repo, pr, fan_in, bridged "
            "ORDER BY pr DESC",
            params,
        )
        return [
            {"name": r[0], "repo": r[1], "pagerank": float(r[2]),
             "fan_in": int(r[3]), "bridged": int(r[4])}
            for r in res.result_set
        ]

    async def code_cross_repo_interfaces(
        self, team: str, *, min_confidence: float = settings.cg_min_confidence,
        limit: int = settings.cg_interface_query_limit,
    ) -> list[dict]:
        """CALLS edges that cross a repo boundary - a callee consumed by callers in
        a different repo. Backs 'X is consumed across repos' interface facts."""
        g = await self._code_graph(team)
        res = await g.query(
            "MATCH (caller:Function)-[r:CALLS]->(callee:Function) "
            "WHERE caller.repo <> callee.repo AND r.confidence >= $minc "
            "WITH callee, collect(DISTINCT caller.repo) AS consumer_repos, count(caller) AS n "
            "RETURN callee.id, callee.name, callee.repo, callee.path, consumer_repos, n "
            "ORDER BY n DESC LIMIT $lim",
            {"minc": min_confidence, "lim": limit},
        )
        return [
            {"id": r[0], "name": r[1], "repo": r[2], "path": r[3],
             "consumer_repos": list(r[4]) if r[4] else [], "consumers": int(r[5])}
            for r in res.result_set
        ]

    async def code_changed_since(self, team: str, *, commit: str, repo: str) -> list[dict]:
        """Files whose recorded commit differs from `commit` - the impact surface
        for detect_changes. (Coarse: commit-level, not per-hunk.)"""
        g = await self._code_graph(team)
        res = await g.query(
            "MATCH (f:File {repo:$repo}) WHERE f.commit <> $commit "
            "RETURN f.repo, f.path, f.commit",
            {"repo": repo, "commit": commit},
        )
        return [{"repo": r[0], "path": r[1], "commit": r[2]} for r in res.result_set]
