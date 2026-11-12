"""Document lifecycle: retract, forget and promote. Used by the admin routes and
by the agent promote route.
"""
import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status

from ..forget import preflight
from .project import project_current
from .search import UNIVERSE
from .storage import DocumentStorage

log = logging.getLogger(__name__)


async def _get(pg: Any, team: str, document_id: str) -> dict[str, Any]:
    document = await pg.get_document(team, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"document {document_id} not found")
    return document


async def retract(pg: Any, falkor: Any, team: str, document_id: str, *, by: str,
                  via: str, reason: str | None = None) -> dict[str, Any]:
    """Remove a document from search. Its rows and files are kept."""
    document = await _get(pg, team, document_id)
    if document["status"] == "retracted":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"document {document_id} is already retracted")
    seq = await pg.append_commit(team, {
        "object_type": "document", "object_id": document_id, "operation": "retract",
        "payload": {}, "provenance": {"retracted_by": by, "retracted_via": via,
                                      "retracted_at": datetime.now(UTC).isoformat(),
                                      **({"reason": reason} if reason else {})},
        "proposed_by": None, "source_kind": None, "gate_decision": "human_retracted",
    })
    await pg.set_document_status(team, document_id, "retracted")
    await falkor.remove_document_projection(team, document_id)
    return {"status": "retracted", "document_id": document_id, "seq": seq}


async def forget_plan(pg: Any, team: str, document_id: str) -> dict[str, Any]:
    """What forgetting a document removes."""
    document = await _get(pg, team, document_id)
    files = await pg.document_files(team, document_id)
    return {"document_id": document_id, "title": document["title"],
            "revisions": files["revisions"], "figures": files["figures"],
            "bytes": files["bytes"], "promoted_to": (str(document["promoted_to"])
                                                     if document.get("promoted_to") else None)}


async def forget(pg: Any, falkor: Any, store: DocumentStorage, team: str,
                 document_id: str) -> dict[str, Any]:
    """Delete a document everywhere: graph nodes, rows (its commit_log rows
    included) and the files no other revision in the team refers to. A universe
    copy made by promotion is a separate document and is not touched."""
    await _get(pg, team, document_id)
    pre = await preflight(pg, team)
    if not pre.get("ok"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"the database role cannot delete in {team!r}: {pre}")
    files = await pg.document_files(team, document_id)
    await falkor.remove_document_projection(team, document_id)
    commit_rows = await pg.delete_commit_log(team, document_id)
    await pg.delete_document(team, document_id)
    removed = 0
    for sha in files["shas"]:
        if not await pg.sha256_in_use(team, sha):
            removed += int(await store.delete_original(team, sha))
            await asyncio.to_thread(store.delete_figures, team, sha)
    return {"status": "forgotten", "document_id": document_id,
            "revisions": files["revisions"], "commit_log_rows": commit_rows,
            "files_removed": removed}


async def promote(pg: Any, falkor: Any, embedder: Any, store: DocumentStorage, team: str,
                  document_id: str, *, by: str, via: str) -> dict[str, Any]:
    """Copy a ready team document into the universe and start projecting the
    copy. Returns once the rows and files are in place; the copy is searchable
    when its projection finishes."""
    if team == UNIVERSE:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="universe documents cannot be promoted")
    document = await _get(pg, team, document_id)
    if document["status"] != "ready" or document["current_revision"] is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"document {document_id} is {document['status']}, not ready")
    if document.get("promoted_to"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"document {document_id} is already promoted "
                                   f"(universe document {document['promoted_to']})")

    copy = await pg.copy_document(team, document_id, UNIVERSE, created_by=by)
    await asyncio.to_thread(store.copy_to_team, team, UNIVERSE, copy["sha256"])
    seq = await pg.append_commit(UNIVERSE, {
        "object_type": "document", "object_id": copy["document_id"], "operation": "commit",
        "payload": {"title": document["title"], "tags": document["tags"],
                    "revision_id": copy["revision_id"], "sha256": copy["sha256"]},
        "provenance": {"promoted_from_team": team, "promoted_from_id": document_id,
                       "promoted_by": by, "promoted_via": via,
                       "promoted_at": datetime.now(UTC).isoformat()},
        "proposed_by": by, "source_kind": "promotion", "gate_decision": "human_approved",
    })
    await falkor.set_document_promoted(team, document_id, copy["document_id"])
    asyncio.get_running_loop().create_task(
        _project_copy(pg, falkor, embedder, copy["document_id"], copy["revision_id"]))
    return {"status": "promoted", "document_id": document_id,
            "universe_document_id": copy["document_id"], "seq": seq}


async def _project_copy(pg: Any, falkor: Any, embedder: Any, document_id: str,
                        revision_id: str) -> None:
    """Mark the copy ready (its extraction is the original's) and project it.
    If projection fails the copy stays ready but unprojected, the team original
    stays searchable, and `kwim_api.documents.rebuild --team universe` projects it."""
    try:
        await pg.finish_revision(UNIVERSE, revision_id, status="ready")
        await project_current(pg, falkor, embedder, UNIVERSE, document_id, scope="universe")
    except Exception:
        log.exception("projecting universe document %s failed; run "
                      "kwim_api.documents.rebuild --team universe", document_id)
