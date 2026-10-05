"""Admin write API for the console. Every change goes through a gate.py method,
and every route writes one audit row (`audited`). See docs/DESIGN.md, "The admin API".
"""
import asyncio
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import Field

from .. import rebuild as rebuild_mod
from ..admin_auth import AdminContext, CurrentOperator
from ..embedder import Embedder
from ..forget import execute_forget, plan_forget, preflight
from ..models import (
    AdvisoryProposal,
    ConstraintProposal,
    FactProposal,
    RejectRequest,
    SemanticWrite,
    StrictRequest,
)
from ..runtime import State
from ..stores.falkor import FalkorStore
from ..stores.postgres import PostgresStore
from .admin_read import _team_path

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# Preview token TTL: 5 minutes, single-use via GETDEL.
_PREVIEW_TTL = 300


# --- Request models (StrictRequest: unknown fields are a 422) -----------------


class AmendFactRequest(StrictRequest):
    statement: str | None = None
    fact_type: str | None = None
    about: list[str] | None = None
    decay_class: str | None = None
    reason: str


class SupersedeFactRequest(StrictRequest):
    statement: str
    fact_type: str
    about: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    source_kind: str | None = None
    reason: str


class AmendRuleRequest(StrictRequest):
    situation: dict[str, Any] | None = None
    approach: str | None = None
    action_pattern: str | None = None
    verdict: str | None = None
    authority: str | None = None
    severity: str | None = None
    check_tier: str | None = None
    reason: str


class SupersedeRuleRequest(StrictRequest):
    rule_type: str = "advisory"
    situation: dict[str, Any] = Field(default_factory=dict)
    approach: str
    evidence: list[str] = Field(default_factory=list)
    reason: str


class AmendSemanticRequest(StrictRequest):
    content: str | None = None
    metadata: dict[str, Any] | None = None
    reason: str


class RetractRequest(StrictRequest):
    object_type: str
    reason: str


class ConfirmRequest(StrictRequest):
    object_type: str


class ForgetPreviewRequest(StrictRequest):
    object_ids: list[str] | None = None
    select: dict[str, Any] | None = None


class ForgetExecuteRequest(StrictRequest):
    preview_token: str
    confirm_count: int


class BulkRejectRequest(StrictRequest):
    source_kind: str | None = None
    older_than: str | None = None
    confirm_count: int
    reason: str


class RebuildRequest(StrictRequest):
    skip_semantic: bool = False
    in_place: bool = False


# --- Helpers ------------------------------------------------------------------
# Routes validate the team inside the audited() block, so a denial is recorded.


@asynccontextmanager
async def audited(operator: AdminContext, action: str, team: str, *,
                  object_type: str | None = None, object_id: str | None = None,
                  detail: dict | None = None):
    """Write one audit row on exit. result: ok (no raise), denied (4xx), error (5xx+).
    Re-raises the exception after recording."""
    try:
        yield
    except HTTPException as exc:
        result = "denied" if exc.status_code < 500 else "error"
        await State.admin.record(
            operator_id=operator.operator_id, action=action, result=result,
            team=team, object_type=object_type, object_id=object_id, detail=detail)
        raise
    except Exception:
        await State.admin.record(
            operator_id=operator.operator_id, action=action, result="error",
            team=team, object_type=object_type, object_id=object_id, detail=detail)
        raise
    else:
        await State.admin.record(
            operator_id=operator.operator_id, action=action, result="ok",
            team=team, object_type=object_type, object_id=object_id, detail=detail)


def _amend_result(result: dict, operator: AdminContext) -> dict:
    """Map gate.amend_object's return to the right HTTP response."""
    if result["status"] == "not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown object id")
    if result["status"] == "not_current":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"object is not current (status={result.get('object_status')})")
    if result["status"] == "invalid_field":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=result.get("detail", "invalid field"))
    return {"object_id": result["object_id"], "seq": result["seq"],
            "operation": "amend"}


# --- Create -------------------------------------------------------------------


@router.post("/teams/{team}/facts", status_code=status.HTTP_201_CREATED)
async def admin_create_fact(team: str, body: FactProposal, request: Request,
                            operator: AdminContext = CurrentOperator):
    proposal_id = str(uuid.uuid4())
    proposal = {"proposal_id": proposal_id, "team": team, "object_type": "fact",
                "proposed_by": operator.username, "body": body.model_dump()}
    async with audited(operator, "fact.create", team, object_type="fact",
                       detail={"statement": body.statement[:200]}):
        await _team_path(team, operator)
        doc = await request.app.state.gate.commit_proposal(
            team, proposal_id, "fact", body.model_dump(), proposal,
            gate_decision="human_approved",
            extra_provenance={"created_by": operator.username,
                              "created_via": "admin_console"})
        return {"object_id": doc["object_id"], "seq": doc["seq"]}


@router.post("/teams/{team}/rules")
async def admin_create_rule(team: str, body: AdvisoryProposal | ConstraintProposal,
                            request: Request, response: Response,
                            operator: AdminContext = CurrentOperator):
    rule_body = body.model_dump()
    proposal_id = str(uuid.uuid4())
    proposal = {"proposal_id": proposal_id, "team": team, "object_type": "rule",
                "proposed_by": operator.username, "body": rule_body}

    if body.rule_type == "constraint":
        # Constraint rules always go to review.
        async with audited(operator, "rule.create", team, object_type="rule",
                           detail={"rule_type": "constraint"}):
            await _team_path(team, operator)
            gate = request.app.state.gate
            await gate._route_to_review(team, proposal_id, "rule", rule_body, proposal)
            response.status_code = status.HTTP_202_ACCEPTED
            return {"proposal_id": proposal_id, "status": "pending_review"}

    async with audited(operator, "rule.create", team, object_type="rule",
                       detail={"rule_type": "advisory"}):
        await _team_path(team, operator)
        doc = await request.app.state.gate.commit_proposal(
            team, proposal_id, "rule", rule_body, proposal,
            gate_decision="human_approved",
            extra_provenance={"created_by": operator.username,
                              "created_via": "admin_console"})
        return {"object_id": doc["object_id"], "seq": doc["seq"]}


@router.post("/teams/{team}/semantic", status_code=status.HTTP_201_CREATED)
async def admin_create_semantic(team: str, body: SemanticWrite, request: Request,
                                operator: AdminContext = CurrentOperator):
    item_id = body.id or str(uuid.uuid4())
    async with audited(operator, "semantic.create", team, object_type="semantic",
                       object_id=item_id):
        await _team_path(team, operator)
        result = await request.app.state.gate.commit_semantic(
            team, item_id, body.content, body.metadata,
            proposed_by=operator.username)
        return {"id": result["object_id"], "seq": result["seq"]}


# --- Edit: amend and supersede -------------------------------------------------


@router.patch("/teams/{team}/facts/{fact_id}/amend")
async def admin_amend_fact(team: str, fact_id: str, body: AmendFactRequest,
                           request: Request, operator: AdminContext = CurrentOperator):
    new_payload = {k: v for k, v in body.model_dump().items()
                   if k != "reason" and v is not None}
    async with audited(operator, "fact.amend", team, object_type="fact",
                       object_id=fact_id, detail={"reason": body.reason}):
        await _team_path(team, operator)
        result = await request.app.state.gate.amend_object(
            team, fact_id, "fact", new_payload,
            operator.username, "admin_console", reason=body.reason)
        return _amend_result(result, operator)


@router.post("/teams/{team}/facts/{fact_id}/supersede",
             status_code=status.HTTP_201_CREATED)
async def admin_supersede_fact(team: str, fact_id: str, body: SupersedeFactRequest,
                               request: Request,
                               operator: AdminContext = CurrentOperator):
    proposal_id = str(uuid.uuid4())
    new_body = body.model_dump(exclude={"reason"})
    new_body["supersedes"] = fact_id
    proposal = {"proposal_id": proposal_id, "team": team, "object_type": "fact",
                "proposed_by": operator.username, "body": new_body}
    async with audited(operator, "fact.supersede", team, object_type="fact",
                       object_id=fact_id, detail={"reason": body.reason}):
        await _team_path(team, operator)
        doc = await request.app.state.gate.commit_proposal(
            team, proposal_id, "fact", new_body, proposal,
            gate_decision="human_approved",
            extra_provenance={"created_by": operator.username,
                              "created_via": "admin_console",
                              "reason": body.reason})
        return {"object_id": doc["object_id"], "seq": doc["seq"],
                "supersedes": fact_id}


@router.patch("/teams/{team}/rules/{rule_id}/amend")
async def admin_amend_rule(team: str, rule_id: str, body: AmendRuleRequest,
                           request: Request, operator: AdminContext = CurrentOperator):
    new_payload = {k: v for k, v in body.model_dump().items()
                   if k != "reason" and v is not None}
    async with audited(operator, "rule.amend", team, object_type="rule",
                       object_id=rule_id, detail={"reason": body.reason}):
        await _team_path(team, operator)
        result = await request.app.state.gate.amend_object(
            team, rule_id, "rule", new_payload,
            operator.username, "admin_console", reason=body.reason)
        return _amend_result(result, operator)


@router.post("/teams/{team}/rules/{rule_id}/supersede",
             status_code=status.HTTP_201_CREATED)
async def admin_supersede_rule(team: str, rule_id: str, body: SupersedeRuleRequest,
                               request: Request,
                               operator: AdminContext = CurrentOperator):
    proposal_id = str(uuid.uuid4())
    new_body = body.model_dump(exclude={"reason"})
    new_body["supersedes"] = rule_id
    proposal = {"proposal_id": proposal_id, "team": team, "object_type": "rule",
                "proposed_by": operator.username, "body": new_body}
    async with audited(operator, "rule.supersede", team, object_type="rule",
                       object_id=rule_id, detail={"reason": body.reason}):
        await _team_path(team, operator)
        doc = await request.app.state.gate.commit_proposal(
            team, proposal_id, "rule", new_body, proposal,
            gate_decision="human_approved",
            extra_provenance={"created_by": operator.username,
                              "created_via": "admin_console",
                              "reason": body.reason})
        return {"object_id": doc["object_id"], "seq": doc["seq"],
                "supersedes": rule_id}


@router.patch("/teams/{team}/semantic/{item_id}/amend")
async def admin_amend_semantic(team: str, item_id: str, body: AmendSemanticRequest,
                               request: Request,
                               operator: AdminContext = CurrentOperator):
    new_payload = {k: v for k, v in body.model_dump().items()
                   if k != "reason" and v is not None}
    async with audited(operator, "semantic.amend", team, object_type="semantic",
                       object_id=item_id, detail={"reason": body.reason}):
        await _team_path(team, operator)
        result = await request.app.state.gate.amend_object(
            team, item_id, "semantic", new_payload,
            operator.username, "admin_console", reason=body.reason)
        return _amend_result(result, operator)


# --- Status transitions --------------------------------------------------------


@router.post("/teams/{team}/objects/{object_id}/retract")
async def admin_retract_object(team: str, object_id: str, body: RetractRequest,
                               request: Request,
                               operator: AdminContext = CurrentOperator):
    async with audited(operator, "object.retract", team,
                       object_type=body.object_type, object_id=object_id,
                       detail={"reason": body.reason}):
        await _team_path(team, operator)
        result = await request.app.state.gate.retract_object(
            team, object_id, operator.username, "admin_console", body.object_type)
        if result["status"] == "not_found":
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown object id")
        if result["status"] == "already_retracted":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="object already retracted")
        return {"seq": result["seq"]}


@router.post("/teams/{team}/objects/{object_id}/confirm")
async def admin_confirm_object(team: str, object_id: str, body: ConfirmRequest,
                               request: Request,
                               operator: AdminContext = CurrentOperator):
    async with audited(operator, "object.confirm", team,
                       object_type=body.object_type, object_id=object_id):
        await _team_path(team, operator)
        result = await request.app.state.gate.confirm_object(
            team, object_id, operator.username, "admin_console", body.object_type)
        if result["status"] == "not_found":
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown object id")
        return {"seq": result["seq"]}


# --- Forget (irreversible, two-step) -------------------------------------------


async def _resolve_forget_plan(team: str, selection: dict) -> list[dict]:
    """Selection -> object ids -> plan, as the CLI does. Preview and execute both
    call it, so execute works from current state."""
    ids: list[str] = list(selection.get("object_ids") or [])
    sel = selection.get("select")
    if sel:
        otypes = [sel["type"]] if sel.get("type") else ["fact", "rule"]
        for ot in otypes:
            ids += await State.falkor.select_forget_ids(
                team, object_type=ot,
                fact_type=sel.get("fact_type"),
                source_kind=sel.get("source_kind"),
                status=sel.get("status"),
                statement_contains=sel.get("statement_contains"))
    if not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="no object_ids or select filters provided")
    return await plan_forget(State.falkor, team, ids, force_shared=False)


@router.post("/teams/{team}/forget/preview")
async def admin_forget_preview(team: str, body: ForgetPreviewRequest,
                               operator: AdminContext = CurrentOperator):
    async with audited(operator, "object.forget", team,
                       detail={"phase": "preview"}):
        await _team_path(team, operator)
        selection = {"object_ids": list(body.object_ids or []), "select": body.select}
        plan = await _resolve_forget_plan(team, selection)
        if not plan:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail="no resolvable objects found")

        pre = await preflight(State.pg, team)
        if not pre.get("ok"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Postgres role {pre.get('role')} lacks DELETE - cannot "
                       f"forget safely. Grant DELETE and retry.")

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=_PREVIEW_TTL)
        # The token holds the selection, not the plan.
        await State.falkor.forget_preview_set(
            token, {"team": team, "count": len(plan), "selection": selection},
            _PREVIEW_TTL)

        return {
            "preview_token": token,
            "expires_at": expires_at.isoformat(),
            "count": len(plan),
            "plan": plan,
            "preflight": {"role": pre.get("role"), "can_delete": pre.get("ok")},
        }


@router.post("/teams/{team}/forget")
async def admin_forget_execute(team: str, body: ForgetExecuteRequest,
                               operator: AdminContext = CurrentOperator):
    async with audited(operator, "object.forget", team,
                       detail={"phase": "execute", "confirm_count": body.confirm_count}):
        await _team_path(team, operator)
        # Atomic get-and-delete: single-use enforced by the store.
        preview = await State.falkor.forget_preview_getdel(body.preview_token)
        if preview is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="preview token expired or already used - re-preview")

        if preview["team"] != team:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="preview token is for a different team")

        # Compare the typed count with a plan derived now.
        plan = await _resolve_forget_plan(team, preview["selection"])
        if body.confirm_count != len(plan):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"confirm_count mismatch: {len(plan)} object(s) match now, "
                       f"submitted {body.confirm_count} - the set changed since the "
                       f"preview; re-preview and review the new plan")

        pre = await preflight(State.pg, team)
        if not pre.get("ok"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Postgres role {pre.get('role')} lacks DELETE - cannot "
                       f"forget safely. Grant DELETE and retry.")

        report = await execute_forget(State.falkor, State.pg, team, plan)
        shared = [s for p in plan for s in p.get("episodics_shared", [])]
        return {
            "forgotten": report["objects"],
            "commit_log_rows": report["commit_log_rows"],
            "episodic_events": report["episodic_events"],
            "shared_skipped": shared,
        }


# --- Proposals ------------------------------------------------------------------


@router.post("/teams/{team}/proposals/{proposal_id}/approve")
async def admin_approve_proposal(team: str, proposal_id: str, request: Request,
                                 operator: AdminContext = CurrentOperator):
    async with audited(operator, "proposal.approve", team, object_id=proposal_id):
        await _team_path(team, operator)
        row = await State.pg.claim_pending(
            team, proposal_id, "approved", operator.username, "admin_console")
        if row is None:
            existing = await State.pg.get_pending(team, proposal_id)
            if existing is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    detail="unknown proposal id")
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="proposal already resolved")

        doc = await request.app.state.gate.commit_proposal(
            team, proposal_id, row["object_type"], row["body"], row["bus_message"],
            gate_decision="human_approved",
            extra_provenance={"approved_by": operator.username,
                              "approved_via": "admin_console"})
        return {"status": "committed", "object_id": doc["object_id"],
                "seq": doc["seq"]}


@router.post("/teams/{team}/proposals/{proposal_id}/reject")
async def admin_reject_proposal(team: str, proposal_id: str,
                                body: RejectRequest = RejectRequest(),
                                operator: AdminContext = CurrentOperator):
    async with audited(operator, "proposal.reject", team, object_id=proposal_id,
                       detail={"reason": body.reason}):
        await _team_path(team, operator)
        row = await State.pg.claim_pending(
            team, proposal_id, "rejected", operator.username, "admin_console",
            body.reason)
        if row is None:
            existing = await State.pg.get_pending(team, proposal_id)
            if existing is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    detail="unknown proposal id")
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="proposal already resolved")

        detail = body.reason or "rejected by reviewer"
        await State.falkor.proposal_set(proposal_id, {
            "id": proposal_id, "object_type": row["object_type"],
            "status": "rejected", "detail": detail,
        })
        return {"status": "rejected"}


@router.post("/teams/{team}/proposals/bulk-reject")
async def admin_bulk_reject(team: str, body: BulkRejectRequest,
                            operator: AdminContext = CurrentOperator):
    async with audited(operator, "proposal.bulk_reject", team,
                       detail={"source_kind": body.source_kind,
                               "older_than": body.older_than,
                               "confirm_count": body.confirm_count,
                               "reason": body.reason}):
        # Parsed inside the audit block, so a malformed request is recorded.
        older_than = None
        if body.older_than is not None:
            try:
                older_than = datetime.fromisoformat(body.older_than)
            except ValueError:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"older_than is not a valid ISO 8601 timestamp: "
                           f"{body.older_than!r}") from None

        await _team_path(team, operator)
        stats = await State.pg.pending_stats(team, source_kind=body.source_kind)
        if body.confirm_count != stats["count"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"confirm_count mismatch: {stats['count']} pending, "
                       f"submitted {body.confirm_count}")

        n = await State.pg.reject_pending(
            team, source_kind=body.source_kind, reason=body.reason,
            resolved_by=operator.username, resolved_via="admin_console",
            older_than=older_than)
        return {"rejected": n}


# --- Rebuild --------------------------------------------------------------------


async def _run_rebuild(job_id: str, team: str, skip_semantic: bool,
                       in_place: bool) -> None:
    """Run a rebuild with its own store instances, then update the job row."""
    pg = PostgresStore()
    falkor = FalkorStore()
    embedder = None if skip_semantic else Embedder()
    try:
        await pg.connect()
        await falkor.connect()
        ok = await rebuild_mod.rebuild_team(
            team, pg, falkor, embedder, in_place, yes=True)
        await State.admin.finish_job(
            job_id, "succeeded" if ok else "failed",
            detail={"rebuild_ok": ok})
    except Exception as exc:
        log.exception("rebuild job %s (team=%s) failed", job_id, team)
        try:
            await State.admin.finish_job(
                job_id, "failed", detail={"error": str(exc)})
        except Exception:
            log.warning("could not mark job %s as failed", job_id, exc_info=True)
    finally:
        if embedder:
            await embedder.close()
        await falkor.close()
        await pg.close()


@router.post("/teams/{team}/rebuild", status_code=status.HTTP_202_ACCEPTED)
async def admin_trigger_rebuild(team: str, body: RebuildRequest,
                                operator: AdminContext = CurrentOperator):
    async with audited(operator, "team.rebuild", team,
                       detail={"skip_semantic": body.skip_semantic,
                               "in_place": body.in_place}):
        await _team_path(team, operator)
        try:
            job = await State.admin.create_job(
                kind="rebuild", team=team, operator_id=operator.operator_id,
                detail={"skip_semantic": body.skip_semantic,
                        "in_place": body.in_place})
        except psycopg.errors.IntegrityError:
            running = await State.admin.list_jobs(team=team, status="running")
            job_id = str(running[0]["id"]) if running else "unknown"
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"a rebuild for team {team!r} is already running "
                       f"(job_id={job_id})")

        job_id = str(job["id"])
        asyncio.get_running_loop().create_task(
            _run_rebuild(job_id, team, body.skip_semantic, body.in_place))
        return {"job_id": job_id}


# --- Jobs ------------------------------------------------------------------------


@router.get("/jobs/{job_id}")
async def admin_get_job(job_id: str, operator: AdminContext = CurrentOperator):
    job = await State.admin.get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown job id")
    return {
        "job_id": str(job["id"]), "kind": job["kind"], "team": job["team"],
        "status": job["status"],
        "started_at": job["started_at"].isoformat() if job["started_at"] else None,
        "finished_at": (job["finished_at"].isoformat()
                        if job["finished_at"] else None),
        "detail": job["detail"],
    }


@router.get("/jobs")
async def admin_list_jobs(team: str | None = None, status: str | None = None,
                          operator: AdminContext = CurrentOperator):
    jobs = await State.admin.list_jobs(team=team, status=status)
    return {"items": [
        {"job_id": str(j["id"]), "kind": j["kind"], "team": j["team"],
         "status": j["status"],
         "started_at": j["started_at"].isoformat() if j["started_at"] else None,
         "finished_at": (j["finished_at"].isoformat() if j["finished_at"] else None),
         "detail": j["detail"]}
        for j in jobs
    ]}
