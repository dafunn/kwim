"""Document upload and browse for the admin console. The request body is the
file itself (Content-Type names its type); metadata goes in query parameters.
Each upload writes a commit_log row and starts an ingest job.
"""
import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, status

from ..admin_auth import AdminContext, CurrentOperator
from ..config import settings
from ..documents import ingest, lifecycle
from ..documents.storage import UploadTooLarge
from ..models import StrictRequest
from ..runtime import State
from .admin_read import _team_path
from .admin_write import audited

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

_JOB_KIND = "document_ingest"


def _max_bytes() -> int:
    return settings.documents_max_upload_mb * 1024 * 1024


def _content_type(request: Request) -> str:
    """The upload's MIME type, without parameters, checked against the allowed list."""
    raw = request.headers.get("content-type", "")
    mime = raw.split(";", 1)[0].strip().lower()
    if mime not in settings.documents_allowed_types:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported Content-Type {mime or '(none)'!r}; allowed: "
                   f"{', '.join(settings.documents_allowed_types)}")
    return mime


def _check_declared_size(request: Request) -> None:
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > _max_bytes():
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"upload exceeds {settings.documents_max_upload_mb} MB")


def _tags(raw: str | None) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _doc_out(d: dict[str, Any]) -> dict[str, Any]:
    out = {k: _iso(d[k]) for k in ("title", "tags", "status", "created_at", "created_by",
                                   "updated_at")}
    out["document_id"] = str(d["id"])
    out["current_revision"] = str(d["current_revision"]) if d["current_revision"] else None
    return out


def _rev_out(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: _iso(r[k]) for k in ("revision_number", "label", "sha256", "mime_type",
                                   "filename", "byte_size", "page_count", "extractor",
                                   "extractor_version", "status", "error", "uploaded_at",
                                   "uploaded_by", "ingested_at")}
    out["revision_id"] = str(r["id"])
    return out


async def _start_job(team: str, operator: AdminContext, detail: dict) -> str:
    try:
        job = await State.admin.create_job(
            kind=_JOB_KIND, team=team, operator_id=operator.operator_id, detail=detail)
    except psycopg.errors.IntegrityError:
        running = await State.admin.list_jobs(team=team, status="running")
        job_id = next((str(j["id"]) for j in running if j["kind"] == _JOB_KIND), "unknown")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"a document ingest for team {team!r} is already running "
                   f"(job_id={job_id})")
    return str(job["id"])


async def _receive(request: Request, team: str, job_id: str) -> tuple[str, int]:
    """Stream the body to storage. On failure the job is marked failed."""
    try:
        return await ingest.storage().save_original(
            team, request.stream(), _max_bytes())
    except UploadTooLarge:
        await State.admin.finish_job(job_id, "failed", detail={"error": "upload too large"})
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"upload exceeds {settings.documents_max_upload_mb} MB")
    except Exception as exc:
        await State.admin.finish_job(job_id, "failed", detail={"error": str(exc)})
        raise


async def _record_revision(team: str, operator: AdminContext, job_id: str, *,
                           document: dict, operation: str, sha256: str, size: int,
                           mime: str, filename: str | None, label: str | None) -> dict:
    """Write the revision row and its commit_log row, then start the ingest."""
    try:
        revision = await State.pg.add_revision(
            team, document_id=str(document["id"]), sha256=sha256, mime_type=mime,
            filename=filename, byte_size=size, label=label, uploaded_by=operator.username)
        seq = await State.pg.append_commit(team, {
            "object_type": "document", "object_id": str(document["id"]),
            "operation": operation,
            "payload": {"title": document["title"], "tags": document["tags"],
                        "revision_id": str(revision["id"]),
                        "revision_number": revision["revision_number"],
                        "label": label, "sha256": sha256, "mime_type": mime,
                        "filename": filename, "byte_size": size},
            "provenance": {"uploaded_by": operator.username,
                           "uploaded_via": "admin_console"},
            "proposed_by": operator.username, "source_kind": None,
            "gate_decision": "human_approved",
        })
    except Exception as exc:
        await State.admin.finish_job(job_id, "failed", detail={"error": str(exc)})
        raise
    asyncio.get_running_loop().create_task(
        ingest.run_ingest(job_id, team, str(document["id"]), str(revision["id"])))
    return {"document_id": str(document["id"]), "revision_id": str(revision["id"]),
            "revision_number": revision["revision_number"], "sha256": sha256,
            "byte_size": size, "seq": seq, "job_id": job_id}


@router.post("/teams/{team}/documents", status_code=status.HTTP_202_ACCEPTED)
async def admin_upload_document(
    team: str, request: Request,
    title: str | None = Query(None, max_length=500),
    tags: str | None = Query(None),
    filename: str | None = Query(None, max_length=500),
    label: str | None = Query(None, max_length=200),
    operator: AdminContext = CurrentOperator,
):
    """Upload a new document. Returns once the file is stored; ingestion runs as
    a job (GET /v1/admin/jobs/{job_id})."""
    async with audited(operator, "document.upload", team, object_type="document",
                       detail={"title": title or filename, "filename": filename}):
        await _team_path(team, operator)
        mime = _content_type(request)
        _check_declared_size(request)
        doc_title = (title or filename or "").strip()
        if not doc_title:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail="title or filename is required")
        job_id = await _start_job(team, operator, {"filename": filename})
        sha256, size = await _receive(request, team, job_id)
        try:
            document = await State.pg.create_document(
                team, title=doc_title, tags=_tags(tags), created_by=operator.username)
        except Exception as exc:
            await State.admin.finish_job(job_id, "failed", detail={"error": str(exc)})
            raise
        return await _record_revision(
            team, operator, job_id, document=document, operation="commit",
            sha256=sha256, size=size, mime=mime, filename=filename, label=label)


@router.post("/teams/{team}/documents/{document_id}/revisions",
             status_code=status.HTTP_202_ACCEPTED)
async def admin_upload_revision(
    team: str, document_id: str, request: Request,
    filename: str | None = Query(None, max_length=500),
    label: str | None = Query(None, max_length=200),
    operator: AdminContext = CurrentOperator,
):
    """Upload a new revision of an existing document."""
    async with audited(operator, "document.revise", team, object_type="document",
                       object_id=document_id, detail={"filename": filename}):
        await _team_path(team, operator)
        mime = _content_type(request)
        _check_declared_size(request)
        document = await State.pg.get_document(team, document_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail=f"document {document_id} not found")
        if document["status"] == "retracted":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail=f"document {document_id} is retracted")
        job_id = await _start_job(team, operator,
                                  {"document_id": document_id, "filename": filename})
        sha256, size = await _receive(request, team, job_id)
        await State.pg.set_document_ingesting(team, document_id)
        return await _record_revision(
            team, operator, job_id, document=document, operation="revise",
            sha256=sha256, size=size, mime=mime, filename=filename, label=label)


@router.get("/teams/{team}/documents")
async def admin_list_documents(
    team: str, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    operator: AdminContext = CurrentOperator,
):
    await _team_path(team, operator)
    docs = await State.pg.list_documents(team, limit=limit, offset=offset)
    return {"items": [_doc_out(d) for d in docs]}


@router.get("/teams/{team}/documents/{document_id}")
async def admin_document_detail(team: str, document_id: str,
                                operator: AdminContext = CurrentOperator):
    await _team_path(team, operator)
    document = await State.pg.get_document(team, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"document {document_id} not found")
    out = _doc_out(document)
    out["revisions"] = [_rev_out(r) for r in document["revisions"]]
    return out


# --- Lifecycle -----------------------------------------------------------------

_PREVIEW_TTL = 300


class RetractDocumentRequest(StrictRequest):
    reason: str | None = None


class ForgetDocumentRequest(StrictRequest):
    preview_token: str


@router.post("/teams/{team}/documents/{document_id}/retract")
async def admin_retract_document(team: str, document_id: str,
                                 body: RetractDocumentRequest = RetractDocumentRequest(),
                                 operator: AdminContext = CurrentOperator):
    """Remove a document from search; its rows and files are kept."""
    async with audited(operator, "document.retract", team, object_type="document",
                       object_id=document_id, detail={"reason": body.reason}):
        await _team_path(team, operator)
        return await lifecycle.retract(State.pg, State.falkor, team, document_id,
                                       by=operator.username, via="admin_console",
                                       reason=body.reason)


@router.post("/teams/{team}/documents/{document_id}/forget/preview")
async def admin_forget_document_preview(team: str, document_id: str,
                                        operator: AdminContext = CurrentOperator):
    """What forgetting the document removes, and a single-use token to do it."""
    async with audited(operator, "document.forget_preview", team, object_type="document",
                       object_id=document_id):
        await _team_path(team, operator)
        plan = await lifecycle.forget_plan(State.pg, team, document_id)
        token = secrets.token_urlsafe(32)
        await State.falkor.forget_preview_set(
            token, {"kind": "document", "team": team, "document_id": document_id},
            _PREVIEW_TTL)
        return {**plan, "preview_token": token,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=_PREVIEW_TTL)).isoformat()}


@router.post("/teams/{team}/documents/{document_id}/forget")
async def admin_forget_document(team: str, document_id: str, body: ForgetDocumentRequest,
                                operator: AdminContext = CurrentOperator):
    """Delete the document everywhere. Needs the token from forget/preview."""
    async with audited(operator, "document.forget", team, object_type="document",
                       object_id=document_id):
        await _team_path(team, operator)
        preview = await State.falkor.forget_preview_getdel(body.preview_token)
        if preview is None or preview.get("kind") != "document" or (
                preview.get("team"), preview.get("document_id")) != (team, document_id):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="preview token expired, already used, or for another "
                                       "document - re-run forget/preview")
        return await lifecycle.forget(State.pg, State.falkor, ingest.storage(), team,
                                      document_id)


@router.post("/teams/{team}/documents/{document_id}/promote")
async def admin_promote_document(team: str, document_id: str,
                                 operator: AdminContext = CurrentOperator):
    """Copy a ready team document into the universe."""
    async with audited(operator, "document.promote", team, object_type="document",
                       object_id=document_id):
        await _team_path(team, operator)
        return await lifecycle.promote(State.pg, State.falkor, State.docs_embedder,
                                       ingest.storage(), team, document_id,
                                       by=operator.username, via="admin_console")
