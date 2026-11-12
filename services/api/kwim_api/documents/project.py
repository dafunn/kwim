"""Projection of a document's current revision into a team's docs graph:
chunk, embed and write. Used by ingest, promotion and the docs rebuild.
"""
import logging
from typing import Any

from .chunk import Chunk, chunk_extraction

log = logging.getLogger(__name__)


async def embed_chunks(embedder: Any,
                       chunks: list[Chunk]) -> tuple[list[list[float] | None], str | None]:
    """One vector per chunk, or all None (with a warning) when there is no
    embedder or it fails. Chunks without vectors are still found by full-text
    search."""
    if embedder is None:
        return [None] * len(chunks), "no document embedder configured; chunks have no vectors"
    try:
        return list(await embedder.embed([c.embed_text for c in chunks])), None
    except Exception as exc:
        log.warning("document embedding failed: %s", exc)
        return [None] * len(chunks), f"embedding failed: {exc}"


def projection(document: dict[str, Any], revision: dict[str, Any],
               extracted: dict[str, Any], page_count: int | None, chunks: list[Chunk],
               vectors: list[list[float] | None], *, scope: str = "team") -> dict[str, Any]:
    """Arguments for FalkorStore.project_document, with ids scoped to the document."""
    doc_id = str(document["id"])

    def gid(local: str) -> str:
        return f"{doc_id}:{local}"

    sections: list[dict[str, Any]] = []

    def walk(items: list[dict[str, Any]], parent: str | None, path: list[str]) -> None:
        for s in items:
            here = path + ([s["heading"]] if s["heading"] else [])
            sections.append({"id": gid(s["id"]), "parent": gid(parent) if parent else None,
                             "heading": s["heading"], "path": " > ".join(here),
                             "level": s["level"], "page_start": s["page_start"],
                             "page_end": s["page_end"], "seq": len(sections)})
            walk(s["children"], s["id"], here)

    walk(extracted["sections"], None, [])
    promoted_to = document.get("promoted_to")
    return {
        "document": {"id": doc_id, "title": document["title"],
                     "tags": document.get("tags") or [],
                     "revision_id": str(revision["id"]),
                     "revision_number": revision["revision_number"],
                     "mime_type": revision["mime_type"], "page_count": page_count,
                     "scope": scope, "promoted_to": str(promoted_to) if promoted_to else None},
        "sections": sections,
        "chunks": [{"id": gid(c.id), "section_id": gid(c.section_id), "seq": c.seq,
                    "text": c.text, "heading_path": " > ".join(c.heading_path),
                    "page_start": c.page_start, "page_end": c.page_end,
                    "block_types": c.block_types,
                    "figure_ids": [gid(f) for f in c.figure_ids], "embedding": v}
                   for c, v in zip(chunks, vectors, strict=True)],
        "figures": [{"id": gid(f["id"]), "figure_id": f["id"], "page": f.get("page"),
                     "caption": f.get("caption"), "object_key": f.get("object_key"),
                     "sections": [gid(s) for s in f.get("referenced_by", [])]}
                    for f in extracted.get("figures", [])],
    }


async def project_document(falkor: Any, embedder: Any, team: str, *,
                           document: dict[str, Any], revision: dict[str, Any],
                           extracted: dict[str, Any], page_count: int | None,
                           scope: str = "team", graph_name: str | None = None,
                           ) -> dict[str, Any]:
    """Chunk, embed and project one revision. Returns counts and any warning.
    `graph_name` projects into that graph instead of the team's live docs graph."""
    chunks = chunk_extraction(extracted, title=document["title"])
    vectors, warning = await embed_chunks(embedder, chunks)
    if falkor is not None:
        await falkor.project_document(team, **projection(
            document, revision, extracted, page_count, chunks, vectors, scope=scope),
            graph_name=graph_name)
    return {"chunks": len(chunks), "embedded": sum(v is not None for v in vectors),
            "warning": warning}


async def project_current(pg: Any, falkor: Any, embedder: Any, team: str,
                          document_id: str, *, scope: str = "team",
                          graph_name: str | None = None) -> dict[str, Any]:
    """Project a document's current revision from its stored extraction."""
    document = await pg.get_document(team, document_id)
    current = await pg.get_current_extracted(team, document_id)
    if document is None or current is None or current["extracted"] is None:
        raise LookupError(f"document {document_id} has no extracted current revision")
    extracted = current["extracted"]
    figure_keys = {f["figure_id"]: f["object_key"]
                   for f in await pg.list_figures(team, str(current["id"]))}
    for fig in extracted.get("figures", []):
        fig["object_key"] = figure_keys.get(fig["id"])
        fig.pop("file", None)
    return await project_document(
        falkor, embedder, team, document=document, revision=current, extracted=extracted,
        page_count=current["page_count"], scope=scope, graph_name=graph_name)
