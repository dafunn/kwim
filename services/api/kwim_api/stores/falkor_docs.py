"""Docs-graph half of the FalkorDB store - the projection of each document's
current revision into kwim_<team>_docs. `DocsGraphStore` is mixed into
`FalkorStore` and uses its connection.

    (:Document)<-[:PART_OF]-(:Section)<-[:PART_OF]-(:Section) ...
    (:Chunk)-[:IN]->(:Section)        (:Chunk)-[:NEXT]->(:Chunk)
    (:Figure)-[:IN]->(:Section)       (:Chunk)-[:REFERS_TO]->(:Figure)

Section, chunk and figure ids are "<document id>:<local id>". Every node carries
document_id, so a document is replaced or removed as a unit.
"""
import asyncio
import re
import weakref
from typing import Any

from ..config import settings

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

_BATCH = 200
_EF_RUNTIME = 200


def _docs_graph_name(team: str) -> str:
    if not _IDENT.match(team):
        raise ValueError(f"unsafe team identifier: {team!r}")
    return f"kwim_{team}_docs"


_DOCS_INIT_CYPHER = [
    "CREATE INDEX FOR (d:Document) ON (d.id)",
    "CREATE INDEX FOR (s:Section) ON (s.id)",
    "CREATE INDEX FOR (s:Section) ON (s.document_id)",
    "CREATE INDEX FOR (c:Chunk) ON (c.id)",
    "CREATE INDEX FOR (c:Chunk) ON (c.document_id)",
    "CREATE INDEX FOR (f:Figure) ON (f.id)",
    "CREATE INDEX FOR (f:Figure) ON (f.document_id)",
    # efRuntime is the HNSW search width. FalkorDB's default of 10 misses the
    # nearest chunks among many similar ones.
    "CREATE VECTOR INDEX FOR (c:Chunk) ON (c.embedding) "
    f"OPTIONS {{dimension:{settings.documents_embed_dim}, similarityFunction:'cosine', "
    f"efRuntime:{_EF_RUNTIME}}}",
    "CALL db.idx.fulltext.createNodeIndex({label:'Chunk', language:'English'}, "
    "'text', 'heading_path')",
]


# One lock per team's live docs graph, shared by every store in this process.
# Every query on the live docs graph holds it, reads included: in FalkorDB a
# query on a missing key creates the graph, so a query between a docs rebuild's
# delete of the live graph and its copy would recreate it and fail the copy.
# Queries on a rebuild's temp graph do not take it. Keyed by event loop as well:
# an asyncio.Lock belongs to the loop it first ran on.
_DOCS_GRAPH_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = \
    weakref.WeakKeyDictionary()


def docs_graph_lock(team: str) -> asyncio.Lock:
    locks = _DOCS_GRAPH_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(team, asyncio.Lock())


class _LockedDocsGraph:
    """A team's live docs graph whose queries run under its lock."""

    def __init__(self, graph, lock: asyncio.Lock):
        self._graph, self._lock = graph, lock

    async def query(self, *args, **kwargs):
        async with self._lock:
            return await self._graph.query(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._graph, name)


class DocsGraphStore:
    """Docs-graph methods. Requires `_db` and `_ensure_schema` from FalkorStore."""

    async def _docs_graph(self, team: str, graph_name: str | None = None):
        """The team's live docs graph, or `graph_name` (a rebuild's temp graph)."""
        name = graph_name or _docs_graph_name(team)
        g = self._db.select_graph(name)
        if name == _docs_graph_name(team):
            lock = docs_graph_lock(team)
            async with lock:
                await self._ensure_schema(g, name, _DOCS_INIT_CYPHER)
            return _LockedDocsGraph(g, lock)
        await self._ensure_schema(g, name, _DOCS_INIT_CYPHER)
        return g

    async def remove_document_projection(self, team: str, document_id: str,
                                         graph_name: str | None = None) -> None:
        """Delete every node of one document from the team's docs graph."""
        g = await self._docs_graph(team, graph_name)
        for label in ("Chunk", "Figure", "Section"):
            await g.query(f"MATCH (n:{label} {{document_id:$id}}) DETACH DELETE n",
                          {"id": document_id})
        await g.query("MATCH (d:Document {id:$id}) DETACH DELETE d", {"id": document_id})

    async def project_document(
        self, team: str, *, document: dict[str, Any], sections: list[dict[str, Any]],
        chunks: list[dict[str, Any]], figures: list[dict[str, Any]],
        graph_name: str | None = None,
    ) -> None:
        """Replace a document's projection. `sections` are flattened with a
        `parent` (a section id, or None for the document); `chunks` may carry an
        `embedding`; `figures` carry the section ids that reference them.
        `graph_name` writes to that graph instead of the live one."""
        doc_id = document["id"]
        await self.remove_document_projection(team, doc_id, graph_name)
        g = await self._docs_graph(team, graph_name)
        await g.query(
            "CREATE (:Document {id:$id, title:$title, tags:$tags, revision_id:$revision_id, "
            "revision_number:$revision_number, mime_type:$mime_type, page_count:$page_count, "
            "scope:$scope, promoted_to:$promoted_to})",
            {"id": doc_id, "title": document["title"], "tags": list(document.get("tags") or []),
             "revision_id": document["revision_id"],
             "revision_number": document["revision_number"],
             "mime_type": document["mime_type"], "page_count": document.get("page_count"),
             "scope": document.get("scope", "team"),
             "promoted_to": document.get("promoted_to")},
        )
        for batch in _batches(sections):
            await g.query(
                "UNWIND $rows AS r "
                "CREATE (s:Section {id:r.id, document_id:r.document_id, heading:r.heading, "
                "path:r.path, level:r.level, page_start:r.page_start, page_end:r.page_end, "
                "seq:r.seq})",
                {"rows": [{**s, "document_id": doc_id} for s in batch]},
            )
        for batch in _batches(sections):
            await g.query(
                "UNWIND $rows AS r MATCH (s:Section {id:r.id}) "
                "OPTIONAL MATCH (p:Section {id:r.parent}) "
                "OPTIONAL MATCH (d:Document {id:$doc}) "
                "WITH s, coalesce(p, d) AS parent CREATE (s)-[:PART_OF]->(parent)",
                {"rows": [{"id": s["id"], "parent": s["parent"]} for s in batch],
                 "doc": doc_id},
            )
        for batch in _batches(chunks):
            with_vec = [c for c in batch if c.get("embedding") is not None]
            without = [c for c in batch if c.get("embedding") is None]
            for rows, set_vec in ((with_vec, True), (without, False)):
                if not rows:
                    continue
                await g.query(
                    "UNWIND $rows AS r MATCH (s:Section {id:r.section_id}) "
                    "CREATE (c:Chunk {id:r.id, document_id:$doc, section_id:r.section_id, "
                    "seq:r.seq, text:r.text, heading_path:r.heading_path, "
                    "page_start:r.page_start, page_end:r.page_end, block_types:r.block_types"
                    + (", embedding:vecf32(r.embedding)" if set_vec else "") + "}) "
                    "CREATE (c)-[:IN]->(s)",
                    {"rows": rows, "doc": doc_id},
                )
        pairs = [{"a": a["id"], "b": b["id"]} for a, b in zip(chunks, chunks[1:], strict=False)]
        for batch in _batches(pairs):
            await g.query(
                "UNWIND $rows AS r MATCH (a:Chunk {id:r.a}), (b:Chunk {id:r.b}) "
                "CREATE (a)-[:NEXT]->(b)", {"rows": batch})
        for batch in _batches(figures):
            await g.query(
                "UNWIND $rows AS r "
                "CREATE (f:Figure {id:r.id, document_id:$doc, figure_id:r.figure_id, "
                "page:r.page, caption:r.caption, object_key:r.object_key}) "
                "WITH f, r UNWIND r.sections AS sid MATCH (s:Section {id:sid}) "
                "CREATE (f)-[:IN]->(s)",
                {"rows": batch, "doc": doc_id},
            )
        refs = [{"c": c["id"], "f": f} for c in chunks for f in c.get("figure_ids", [])]
        for batch in _batches(refs):
            await g.query(
                "UNWIND $rows AS r MATCH (c:Chunk {id:r.c}), (f:Figure {id:r.f}) "
                "CREATE (c)-[:REFERS_TO]->(f)", {"rows": batch})

    async def set_document_promoted(self, team: str, document_id: str,
                                    universe_id: str) -> None:
        g = await self._docs_graph(team)
        await g.query("MATCH (d:Document {id:$id}) SET d.promoted_to = $to",
                      {"id": document_id, "to": universe_id})

    async def docs_counts(self, team: str, document_id: str) -> dict[str, int]:
        g = await self._docs_graph(team)
        out = {}
        for label in ("Section", "Chunk", "Figure"):
            res = await g.query(f"MATCH (n:{label} {{document_id:$id}}) RETURN count(n)",
                                {"id": document_id})
            out[label.lower()] = res.result_set[0][0]
        res = await g.query("MATCH (c:Chunk {document_id:$id}) WHERE c.embedding IS NOT NULL "
                            "RETURN count(c)", {"id": document_id})
        out["embedded"] = res.result_set[0][0]
        return out


def _batches(rows: list[Any]):
    for i in range(0, len(rows), _BATCH):
        yield rows[i:i + _BATCH]


# --- Reads ---------------------------------------------------------------------

_CHUNK_RETURN = ("node.id, node.document_id, node.section_id, node.seq, node.text, "
                 "node.heading_path, node.page_start, node.page_end")


def _chunk_row(row: list[Any], score: float) -> dict[str, Any]:
    keys = ("id", "document_id", "section_id", "seq", "text", "heading_path",
            "page_start", "page_end")
    return {**dict(zip(keys, row[:8], strict=True)), "score": score}


def _filter_clause(document_ids: list[str] | None) -> str:
    return "WHERE node.document_id IN $docs " if document_ids else ""


class DocsGraphReads:
    """Read queries over kwim_<team>_docs. Mixed into FalkorStore with DocsGraphStore."""

    async def docs_vector_search(self, team: str, qvec: list[float], k: int,
                                 document_ids: list[str] | None = None) -> list[dict]:
        """Nearest chunks by cosine distance, smallest first."""
        g = await self._docs_graph(team)
        res = await g.query(
            "CALL db.idx.vector.queryNodes('Chunk', 'embedding', $k, vecf32($v)) "
            "YIELD node, score " + _filter_clause(document_ids) +
            f"RETURN {_CHUNK_RETURN}, score ORDER BY score ASC LIMIT $k",
            {"k": k, "v": qvec, "docs": document_ids or []},
        )
        return [_chunk_row(r, float(r[8])) for r in res.result_set]

    async def docs_fulltext_search(self, team: str, query: str, k: int,
                                   document_ids: list[str] | None = None) -> list[dict]:
        """Chunks matching a full-text query (RediSearch syntax), best first."""
        g = await self._docs_graph(team)
        res = await g.query(
            "CALL db.idx.fulltext.queryNodes('Chunk', $q) YIELD node, score "
            + _filter_clause(document_ids) +
            f"RETURN {_CHUNK_RETURN}, score ORDER BY score DESC LIMIT $k",
            {"q": query, "k": k, "docs": document_ids or []},
        )
        return [_chunk_row(r, float(r[8])) for r in res.result_set]

    async def docs_documents(self, team: str, tag: str | None = None) -> list[dict]:
        """Projected documents (ready and searchable), optionally with a tag."""
        g = await self._docs_graph(team)
        where = "WHERE $tag IN d.tags " if tag else ""
        res = await g.query(
            f"MATCH (d:Document) {where}RETURN d.id, d.title, d.tags, d.revision_id, "
            "d.revision_number, d.mime_type, d.page_count, d.scope, d.promoted_to "
            "ORDER BY d.title",
            {"tag": tag},
        )
        keys = ("id", "title", "tags", "revision_id", "revision_number", "mime_type",
                "page_count", "scope", "promoted_to")
        return [dict(zip(keys, r, strict=True)) for r in res.result_set]

    async def docs_section(self, team: str, section_id: str) -> dict | None:
        """A section with its chunks in order and its figures."""
        g = await self._docs_graph(team)
        res = await g.query(
            "MATCH (s:Section {id:$id}) MATCH (d:Document {id:s.document_id}) "
            "RETURN s.id, s.document_id, s.heading, s.path, s.level, s.page_start, "
            "s.page_end, d.title, d.revision_number", {"id": section_id})
        if not res.result_set:
            return None
        keys = ("id", "document_id", "heading", "path", "level", "page_start", "page_end",
                "document_title", "revision_number")
        section = dict(zip(keys, res.result_set[0], strict=True))
        res = await g.query(
            "MATCH (c:Chunk)-[:IN]->(:Section {id:$id}) "
            "RETURN c.id, c.seq, c.text, c.page_start, c.page_end ORDER BY c.seq",
            {"id": section_id})
        section["chunks"] = [dict(zip(("id", "seq", "text", "page_start", "page_end"), r,
                                      strict=True)) for r in res.result_set]
        res = await g.query(
            "MATCH (f:Figure)-[:IN]->(:Section {id:$id}) "
            "RETURN f.figure_id, f.caption, f.page, f.object_key ORDER BY f.figure_id",
            {"id": section_id})
        section["figures"] = [dict(zip(("figure_id", "caption", "page", "object_key"), r,
                                       strict=True)) for r in res.result_set]
        return section

    async def docs_figure(self, team: str, document_id: str,
                          figure_id: str) -> dict | None:
        g = await self._docs_graph(team)
        res = await g.query(
            "MATCH (f:Figure {id:$id}) RETURN f.caption, f.page, f.object_key",
            {"id": f"{document_id}:{figure_id}"})
        if not res.result_set:
            return None
        caption, page, key = res.result_set[0]
        return {"caption": caption, "page": page, "object_key": key}

    async def docs_has_document(self, team: str, document_id: str) -> bool:
        g = await self._docs_graph(team)
        res = await g.query("MATCH (d:Document {id:$id}) RETURN count(d)", {"id": document_id})
        return bool(res.result_set and res.result_set[0][0])
