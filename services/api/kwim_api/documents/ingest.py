"""The document ingest job: extract a stored revision into the intermediate
format, chunk and embed it, project it into the team's docs graph, record the
outcome, then apply revision retention.

Extractors are registered in EXTRACTORS by MIME type. Each takes the path of the
stored original and an empty directory for rendered figures, and returns an
`Extraction`. Figure files are moved to the documents volume and recorded in
document_figures.
"""
import asyncio
import logging
import shutil
from collections.abc import Callable
from typing import Any

from ..config import settings
from ..embedder import Embedder
from ..runtime import State
from ..stores.falkor import FalkorStore
from ..stores.postgres import PostgresStore
from .extract_markdown import extract_markdown
from .extract_pdf import extract_pdf
from .ingest_types import Extraction
from .project import project_document
from .storage import DocumentStorage

log = logging.getLogger(__name__)


EXTRACTORS: dict[str, Callable[[str, str], Extraction]] = {
    "application/pdf": extract_pdf,
    "text/markdown": extract_markdown,
}


def storage() -> DocumentStorage:
    return DocumentStorage(settings.documents_storage_path)


def document_embedder() -> Embedder | None:
    """The document embedder, or None when KWIM_DOCS_EMBEDDER_URL is unset."""
    if not settings.documents_embedder_url:
        return None
    return Embedder(url=settings.documents_embedder_url,
                    timeout_s=settings.documents_embed_timeout_s,
                    max_batch=settings.documents_embedder_max_batch)


async def run_ingest(job_id: str, team: str, document_id: str, revision_id: str) -> None:
    """Run one ingest with its own store connections, then finish the job row."""
    pg = PostgresStore()
    falkor = FalkorStore()
    embedder = document_embedder()
    try:
        await pg.connect()
        await falkor.connect()
        detail = await ingest_revision(pg, team, document_id, revision_id,
                                       falkor=falkor, embedder=embedder)
        await State.admin.finish_job(
            job_id, "succeeded" if detail["status"] == "ready" else "failed",
            detail=detail)
    except Exception as exc:
        log.exception("ingest job %s (team=%s revision=%s) failed", job_id, team, revision_id)
        try:
            await pg.finish_revision(team, revision_id, status="failed", error=str(exc))
        except Exception:
            log.warning("could not mark revision %s as failed", revision_id, exc_info=True)
        try:
            await State.admin.finish_job(job_id, "failed", detail={
                "document_id": document_id, "revision_id": revision_id, "error": str(exc)})
        except Exception:
            log.warning("could not mark job %s as failed", job_id, exc_info=True)
    finally:
        if embedder is not None:
            await embedder.close()
        await falkor.close()
        await pg.close()


async def ingest_revision(pg: PostgresStore, team: str, document_id: str,
                          revision_id: str, *, falkor: FalkorStore | None = None,
                          embedder: Embedder | None = None) -> dict[str, Any]:
    """Extract one revision, project it, and record the result. Without `falkor`
    nothing is projected; without `embedder` chunks have no vectors. Returns the
    job detail."""
    detail: dict[str, Any] = {"document_id": document_id, "revision_id": revision_id}
    revision = await pg.get_revision(team, revision_id)
    if revision is None:
        raise LookupError(f"revision {revision_id} not found")

    store = storage()
    if not store.has_original(team, revision["sha256"]):
        return await _fail(pg, team, revision_id, detail,
                           f"stored original {revision['sha256']} is missing")

    extractor = EXTRACTORS.get(revision["mime_type"])
    if extractor is None:
        return await _fail(pg, team, revision_id, detail,
                           f"no extractor for {revision['mime_type']}")

    sha = revision["sha256"]
    path = store.original_path(team, sha)
    scratch = await asyncio.to_thread(store.scratch_dir, team)
    try:
        try:
            result = await asyncio.to_thread(extractor, path, scratch)
        except Exception as exc:
            return await _fail(pg, team, revision_id, detail, f"extraction failed: {exc}")
        figures = await asyncio.to_thread(_store_figures, store, team, sha,
                                          result.extracted.get("figures", []))
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, True)

    document = await pg.get_document(team, document_id)
    projected = await project_document(
        falkor, embedder, team, document=document, revision=revision,
        extracted=result.extracted, page_count=result.page_count)
    if projected["warning"]:
        result.warnings.append(projected["warning"])

    await pg.replace_figures(team, revision_id, figures)
    await pg.finish_revision(
        team, revision_id, status="ready", extracted=result.extracted,
        extractor=result.extractor, extractor_version=result.extractor_version,
        page_count=result.page_count)
    detail.update(status="ready", extractor=result.extractor,
                  page_count=result.page_count, figures=len(figures),
                  chunks=projected["chunks"], embedded=projected["embedded"],
                  projected=falkor is not None, warnings=result.warnings)
    detail["pruned_revisions"] = await prune(pg, store, team, document_id)
    return detail


def _store_figures(store: DocumentStorage, team: str, sha: str,
                   figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move rendered figures onto the volume, replacing each figure's `file` with
    its `object_key`. Returns the figures that have an image."""
    stored = []
    for fig in figures:
        file = fig.pop("file", None)
        fig["object_key"] = store.store_figure(team, sha, fig["id"], file) if file else None
        if fig["object_key"]:
            stored.append(fig)
    return stored


async def prune(pg: PostgresStore, store: DocumentStorage, team: str,
                document_id: str) -> int:
    """Apply documents.max_revisions; delete files no revision refers to any more."""
    removed = await pg.prune_revisions(team, document_id, settings.documents_max_revisions)
    for sha in set(removed):
        if not await pg.sha256_in_use(team, sha):
            await store.delete_original(team, sha)
            await asyncio.to_thread(store.delete_figures, team, sha)
    return len(removed)


async def _fail(pg: PostgresStore, team: str, revision_id: str, detail: dict[str, Any],
                error: str) -> dict[str, Any]:
    await pg.finish_revision(team, revision_id, status="failed", error=error)
    detail.update(status="failed", error=error)
    return detail
