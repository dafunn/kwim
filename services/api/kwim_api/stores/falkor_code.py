"""Code-graph half of the FalkorDB store - reads and writes over kwim_<team>_code.

A separate graph from the K/W store, with its own DDL: `rebuild` replays commit_log
into kwim_<team> and swaps it live, wiping whatever is not in the log. The code
graph's source of truth is the repo, so rebuild never touches it.

`CodeGraphStore` is mixed into `FalkorStore` and uses the connection and the
schema-ensure helper the base class owns.
"""
import logging
import re
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def _code_graph_name(team: str) -> str:
    """The team's code graph - a sibling of kwim_<team>, deliberately separate.

    rebuild.py replays commit_log into kwim_<team> and swaps it live; anything not
    in commit_log is wiped. The code graph's source of truth is the repo, not
    commit_log, so it lives in its own graph that rebuild never touches.
    """
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")
    return f"kwim_{team}_code"


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


class CodeGraphStore:
    """Code-graph methods. Requires `_db` and `_ensure_schema` from FalkorStore."""

    async def _code_graph(self, team: str):
        """Return the team's code graph (kwim_<team>_code), ensuring its schema.

        Separate from _graph: distinct graph name + distinct DDL, so rebuild's
        commit_log replay can never touch it.
        """
        name = _code_graph_name(team)
        g = self._db.select_graph(name)
        await self._ensure_schema(g, name, _CODE_INIT_CYPHER)
        return g

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
        arrow = (f"-[:CALLS*1..{depth}]->" if direction == "outbound"
                 else f"<-[:CALLS*1..{depth}]-")
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
