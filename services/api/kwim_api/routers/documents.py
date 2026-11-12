"""Document surface for agents - list, search, sections and figures across the
team's documents and the universe's. Read-only; uploads go through /v1/admin.
"""
import os

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from ..auth import CurrentTeam, TeamContext, require_capability
from ..documents import ingest, lifecycle
from ..documents.search import UNIVERSE, search
from ..runtime import State

router = APIRouter(prefix="/v1/documents", tags=["documents"])


async def _owner(team: str, document_id: str) -> str:
    """The graph that holds a document: the team's, else the universe's."""
    for t in (team, UNIVERSE):
        if await State.falkor.docs_has_document(t, document_id):
            return t
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"document {document_id} not found")


@router.get("")
async def documents_list(tag: str | None = None, team: TeamContext = CurrentTeam):
    require_capability(team, "read")
    items = []
    for t in dict.fromkeys((team.team, UNIVERSE)):
        for d in await State.falkor.docs_documents(t, tag=tag):
            items.append({**d, "scope": "universe" if t == UNIVERSE else "team"})
    return {"items": items}


@router.get("/search")
async def documents_search(
    q: str = Query(..., min_length=1, max_length=2000),
    document: list[str] | None = Query(None),
    tag: str | None = None,
    limit: int = Query(5, ge=1, le=20),
    team: TeamContext = CurrentTeam,
):
    """Sections that answer `q`, best first, each with its citation (document,
    revision, section path, pages) and figures."""
    require_capability(team, "read")
    return await search(State.falkor, State.docs_embedder, team.team, q, limit=limit,
                        document_ids=document or None, tag=tag,
                        reranker=State.docs_reranker)


@router.get("/{document_id}/sections/{section_id}")
async def documents_section(document_id: str, section_id: str,
                            team: TeamContext = CurrentTeam):
    require_capability(team, "read")
    owner = await _owner(team.team, document_id)
    section = await State.falkor.docs_section(owner, f"{document_id}:{section_id}")
    if section is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"section {section_id} not found")
    return {
        "document_id": document_id, "document_title": section["document_title"],
        "revision_number": section["revision_number"], "section_id": section_id,
        "heading": section["heading"], "path": section["path"], "level": section["level"],
        "page_start": section["page_start"], "page_end": section["page_end"],
        "text": "\n\n".join(c["text"] for c in section["chunks"]),
        "figures": [{"figure_id": f["figure_id"], "caption": f["caption"], "page": f["page"],
                     "url": (f"/v1/documents/{document_id}/figures/{f['figure_id']}"
                             if f["object_key"] else None)}
                    for f in section["figures"]],
    }


@router.get("/{document_id}/figures/{figure_id}")
async def documents_figure(document_id: str, figure_id: str,
                           team: TeamContext = CurrentTeam):
    require_capability(team, "read")
    owner = await _owner(team.team, document_id)
    figure = await State.falkor.docs_figure(owner, document_id, figure_id)
    if figure is None or not figure["object_key"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"figure {figure_id} not found")
    try:
        path = ingest.storage().path_for_key(owner, figure["object_key"])
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"figure {figure_id} not found")
    if not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"figure {figure_id} is missing")
    return FileResponse(path, media_type="image/png")


@router.post("/{document_id}/promote")
async def documents_promote(document_id: str, team: TeamContext = CurrentTeam):
    """Copy one of the team's ready documents into the universe. Requires `promote`."""
    require_capability(team, "promote")
    return await lifecycle.promote(State.pg, State.falkor, State.docs_embedder,
                                   ingest.storage(), team.team, document_id,
                                   by=team.key_id, via="api")
