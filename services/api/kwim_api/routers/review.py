"""Human-review surface: REST + mattermost over <team>.pending_proposals.

REST is first-class; the mattermost action callback (`/mm-action`) performs
the same internal claim -> commit/reject flow as the REST approve/reject
endpoints, just with a different reviewer-identity source and response shape.
"""
import hmac
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from ..auth import CurrentTeam, TeamContext
from ..config import settings
from ..gate import summarize_proposal
from ..models import PendingProposal, RejectRequest
from ..runtime import State
from ..stores.postgres import _IDENT

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/review", tags=["review"])


def _require_review_key(team: TeamContext) -> None:
    """Capability gate - mirrors KWIM_PROMOTE_KEYS (routers/wisdom.py wisdom_promote).

    Fail-closed: an unset/empty KWIM_REVIEW_KEYS means nobody can review,
    not "any team key can review."
    """
    allowed_prefixes = [p.strip() for p in settings.review_keys.split(",") if p.strip()]
    if not allowed_prefixes:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="review not configured - set KWIM_REVIEW_KEYS to grant review capability")
    if team.key_id not in allowed_prefixes:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="key not in KWIM_REVIEW_KEYS - review not permitted")


@router.get("/pending", response_model=list[PendingProposal])
async def review_pending(limit: int = 50, team: TeamContext = CurrentTeam):
    """Team-scoped read: any team key can read its own queue.

    Reading your own team's pending proposals is no more sensitive than reading
    your own knowledge/wisdom (already allowed by the team key). Approve/reject
    stay gated by `_require_review_key` - that's the governance authority.
    """
    rows = await State.pg.list_pending(team.team, limit=limit)
    return [
        PendingProposal(
            proposal_id=str(r["proposal_id"]),
            object_type=r["object_type"],
            proposed_by=r["proposed_by"],
            created_at=r["created_at"],
            summary=summarize_proposal(r["object_type"], r["body"]),
            body=r["body"],
        )
        for r in rows
    ]


@router.post("/{proposal_id}/approve")
async def review_approve(proposal_id: str, request: Request, team: TeamContext = CurrentTeam):
    _require_review_key(team)
    row = await State.pg.claim_pending(team.team, proposal_id, "approved", team.key_id, "api")
    if row is None:
        await _raise_claim_failure(team.team, proposal_id)

    doc = await request.app.state.gate.commit_proposal(
        team.team, proposal_id, row["object_type"], row["body"], row["bus_message"],
        gate_decision="human_approved",
        extra_provenance={"approved_by": team.key_id, "approved_via": "api"},
    )
    return {"status": "committed", "object_id": doc["object_id"], "seq": doc["seq"]}


@router.post("/{proposal_id}/reject")
async def review_reject(proposal_id: str, body: RejectRequest = RejectRequest(), team: TeamContext = CurrentTeam):
    _require_review_key(team)
    row = await State.pg.claim_pending(
        team.team, proposal_id, "rejected", team.key_id, "api", body.reason)
    if row is None:
        await _raise_claim_failure(team.team, proposal_id)

    detail = body.reason or "rejected by reviewer"
    await State.falkor.proposal_set(proposal_id, {
        "id": proposal_id, "object_type": row["object_type"],
        "status": "rejected", "detail": detail,
    })
    return {"status": "rejected"}


async def _raise_claim_failure(team: str, proposal_id: str) -> None:
    """claim_pending returned None: distinguish 404 (unknown) from 409 (already resolved)."""
    existing = await State.pg.get_pending(team, proposal_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal id")
    raise HTTPException(status.HTTP_409_CONFLICT, detail="proposal already resolved")


@router.post("/mm-action")
async def review_mm_action(request: Request) -> dict[str, Any]:
    """mattermost interactive-button callback.

    Not behind current_team - mattermost has no team bearer key. Authenticated
    by a shared secret embedded in the button's context (fail-closed: an unset
    KWIM_MM_ACTION_SECRET means this endpoint always 403s).
    """
    payload = await request.json()
    context = payload.get("context") or {}

    secret = context.get("secret", "")
    if not settings.mm_action_secret or not hmac.compare_digest(str(secret), settings.mm_action_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid mm-action secret")

    team = context.get("team", "")
    if not isinstance(team, str) or not _IDENT.match(team):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid team")

    proposal_id = context.get("proposal_id", "")
    decision = context.get("decision", "")
    user_name = payload.get("user_name") or payload.get("user_id") or "unknown"

    if decision not in ("approve", "reject", "forget"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid decision")

    if decision == "approve":
        row = await State.pg.claim_pending(team, proposal_id, "approved", user_name, "mattermost")
        if row is None:
            return {"update": {"message": ":warning: proposal already resolved or not found"}}
        doc = await request.app.state.gate.commit_proposal(
            team, proposal_id, row["object_type"], row["body"], row["bus_message"],
            gate_decision="human_approved",
            extra_provenance={"approved_by": user_name, "approved_via": "mattermost"},
        )
        return {"update": {"message":
            f":white_check_mark: approved by @{user_name} - object {doc['object_id']}, seq {doc['seq']}"}}

    # reject or forget - a pending proposal has no committed node, so both reject it
    # (rejecting prevents commit). forget additionally hard-deletes the non-shared
    # source episodics inline, so the garbage can't be re-derived on a rebuild.
    row = await State.pg.claim_pending(team, proposal_id, "rejected", user_name, "mattermost")
    if row is None:
        return {"update": {"message": ":warning: proposal already resolved or not found"}}
    await State.falkor.proposal_set(proposal_id, {
        "id": proposal_id, "object_type": row["object_type"],
        "status": "rejected", "detail": f"{decision} by @{user_name} via mattermost",
    })
    if decision == "forget":
        evidence = (row.get("body") or {}).get("evidence") or []
        result = await request.app.state.gate.forget_episodics(
            team, evidence, user_name, "mattermost")
        return {"update": {"message": _forget_pending_message(user_name, evidence, result)}}
    return {"update": {"message": f":x: rejected by @{user_name}"}}


def _forget_pending_message(user_name: str, evidence: list[str], result: dict[str, Any]) -> str:
    """Chat feedback for forgetting a pending proposal (rejected + source episodics)."""
    if not evidence:
        return (f":fire: forgotten by @{user_name} - proposal rejected "
                f"(no source episodics recorded).")
    kept = len(result.get("shared_skipped") or [])
    kept_note = f", kept {kept} shared episodic(s)" if kept else ""
    if result["status"] == "preflight_failed":
        pre = result.get("preflight", {})
        return (f":warning: proposal rejected, but could NOT delete source episodics - Postgres role "
                f"`{pre.get('role')}` lacks DELETE. Source(s): {', '.join(evidence)}.")
    if result["status"] == "no_delete":
        return (f":fire: forgotten by @{user_name} - proposal rejected; all {len(evidence)} source "
                f"episodic(s) still support live objects, kept.")
    return (f":fire: forgotten by @{user_name} - proposal rejected; deleted "
            f"{result['episodic_events']} source episodic(s){kept_note}. Irreversible.")


@router.post("/committed-action")
async def review_committed_action(request: Request) -> dict[str, Any]:
    """mattermost interactive-button callback for committed objects.

    Distinct from `/mm-action`: buttons here carry an `object_id`/`object_type`
    (a committed graph node), not a `proposal_id` (a pending_proposals row).
    Same hmac-secret auth as `/mm-action`.
    """
    payload = await request.json()
    context = payload.get("context") or {}

    secret = context.get("secret", "")
    if not settings.mm_action_secret or not hmac.compare_digest(str(secret), settings.mm_action_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid mm-action secret")

    team = context.get("team", "")
    if not isinstance(team, str) or not _IDENT.match(team):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid team")

    object_id = context.get("object_id", "")
    object_type = context.get("object_type")
    decision = context.get("decision", "")
    user_name = payload.get("user_name") or payload.get("user_id") or "unknown"

    if decision not in ("confirm", "retract", "forget"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid decision")

    gate = request.app.state.gate
    if decision == "confirm":
        result = await gate.confirm_object(team, object_id, user_name, "mattermost", object_type)
        if result["status"] == "not_found":
            return {"update": {"message": ":warning: object not found"}}
        return {"update": {"message": f":white_check_mark: confirmed by @{user_name} - object {object_id}"}}

    if decision == "retract":
        result = await gate.retract_object(team, object_id, user_name, "mattermost", object_type)
        if result["status"] == "not_found":
            return {"update": {"message": ":warning: object not found"}}
        if result["status"] == "already_retracted":
            return {"update": {"message": ":warning: object already retracted"}}
        return {"update": {"message": f":wastebasket: retracted by @{user_name} - object {object_id}"}}

    # forget - irreversibly remove the committed object from every store, inline.
    # The reviewer click is the confirmation; the shared-evidence guard still protects
    # episodics that support other live objects (see gate.forget_object).
    result = await gate.forget_object(team, object_id, user_name, "mattermost", object_type)
    if result["status"] == "not_found":
        return {"update": {"message": ":warning: object not found"}}
    if result["status"] == "preflight_failed":
        pre = result.get("preflight", {})
        return {"update": {"message":
            f":warning: cannot forget {object_id} - Postgres role `{pre.get('role')}` lacks DELETE on "
            f"commit_log/episodic_events; object left intact. Grant DELETE and retry."}}
    kept = len(result.get("shared_skipped") or [])
    kept_note = f", kept {kept} shared episodic(s)" if kept else ""
    return {"update": {"message":
        f":fire: forgotten by @{user_name} - object {object_id} removed from memory "
        f"({result['commit_log_rows']} commit-log row(s), {result['episodic_events']} source "
        f"episodic(s) deleted{kept_note}). Irreversible."}}


@router.post("/committed/{object_id}/retract")
async def review_committed_retract(object_id: str, request: Request, team: TeamContext = CurrentTeam):
    """REST parity for retraction - scriptable, not Mattermost-only."""
    _require_review_key(team)

    result = await request.app.state.gate.retract_object(team.team, object_id, team.key_id, "api")
    if result["status"] == "not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown object id")
    if result["status"] == "already_retracted":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="object already retracted")
    return {"status": "retracted", "object_id": result["object_id"], "seq": result["seq"]}
