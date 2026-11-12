"""Skills for the admin console: create, revise, retire, restore, owner,
promotion, and the version history. An operator's write commits directly and is
audited; agents' proposals are reviewed through the proposal routes.
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..admin_auth import AdminContext, CurrentOperator
from ..models import StrictRequest
from ..runtime import State
from ..skills import service
from .admin_read import _team_path
from .admin_write import audited

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class SkillCreateRequest(StrictRequest):
    text: str
    owner: str | None = None


class SkillReviseRequest(StrictRequest):
    text: str


class SkillOwnerRequest(StrictRequest):
    owner: str


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _skill_out(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(skill["id"]), "name": skill["name"], "status": skill["status"],
        "version": skill["current_version"], "description": skill["description"],
        "agents": skill["agents"], "tags": skill["tags"], "refs": skill["refs"],
        "owner": skill["owner"],
        "promoted_to": str(skill["promoted_to"]) if skill.get("promoted_to") else None,
        "promoted_from_team": skill.get("promoted_from_team"),
        "created_at": _iso(skill["created_at"]), "created_by": skill["created_by"],
        "updated_at": _iso(skill["updated_at"]),
    }


def _version_out(v: dict[str, Any], *, text: bool) -> dict[str, Any]:
    out = {"version": v["version"], "description": v["description"], "source": v["source"],
           "author": v["author"], "approver": v["approver"],
           "proposal_id": str(v["proposal_id"]) if v.get("proposal_id") else None,
           "created_at": _iso(v["version_created_at"])}
    if text:
        out["text"] = v["text"]
    return out


async def _check_owner(owner: str) -> None:
    row = await State.admin.get_operator_by_username(owner)
    if row is None or not row["is_active"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"owner {owner!r} is not an active operator")


@router.get("/teams/{team}/skills")
async def admin_list_skills(team: str, include_retired: bool = Query(True),
                            operator: AdminContext = CurrentOperator):
    await _team_path(team, operator)
    pending = await State.pg.pending_skill_proposals(team)
    return {"items": [{**_skill_out(s), "pending_revision": service._pending(pending.get(s["name"]))}
                      for s in await State.pg.list_skills(team, include_retired=include_retired)]}


@router.get("/teams/{team}/skills/{name}")
async def admin_skill_detail(team: str, name: str, operator: AdminContext = CurrentOperator):
    await _team_path(team, operator)
    skill = await service._get_or_404(State.pg, team, name)
    versions = await State.pg.skill_versions(team, str(skill["id"]))
    pending = (await State.pg.pending_skill_proposals(team)).get(name)
    return {**_skill_out(skill), "text": skill["text"],
            "pending_revision": service._pending(pending),
            "versions": [_version_out(v, text=False) for v in versions]}


@router.get("/teams/{team}/skills/{name}/versions/{version}")
async def admin_skill_version(team: str, name: str, version: int,
                              operator: AdminContext = CurrentOperator):
    await _team_path(team, operator)
    skill = await service._get_or_404(State.pg, team, name)
    v = await State.pg.get_skill_version(team, str(skill["id"]), version)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"skill {name!r} has no version {version}")
    return {"name": name, **_version_out(v, text=True)}


@router.post("/teams/{team}/skills", status_code=status.HTTP_201_CREATED)
async def admin_create_skill(team: str, body: SkillCreateRequest,
                             operator: AdminContext = CurrentOperator):
    async with audited(operator, "skill.create", team, object_type="skill"):
        await _team_path(team, operator)
        if body.owner:
            await _check_owner(body.owner)
        result = await service.create(State.pg, State.docs_embedder, team, body.text,
                                      by=operator.username, via="admin_console",
                                      owner=body.owner)
        return {**_skill_out(result["skill"]), "seq": result["seq"]}


@router.post("/teams/{team}/skills/{name}/revisions")
async def admin_revise_skill(team: str, name: str, body: SkillReviseRequest,
                             operator: AdminContext = CurrentOperator):
    async with audited(operator, "skill.revise", team, object_type="skill", object_id=name):
        await _team_path(team, operator)
        result = await service.revise(State.pg, State.docs_embedder, team, name, body.text,
                                      by=operator.username, via="admin_console")
        return {**_skill_out(result["skill"]), "seq": result["seq"]}


@router.post("/teams/{team}/skills/{name}/retire")
async def admin_retire_skill(team: str, name: str, operator: AdminContext = CurrentOperator):
    async with audited(operator, "skill.retire", team, object_type="skill", object_id=name):
        await _team_path(team, operator)
        result = await service.set_status(State.pg, team, name, "retired",
                                          by=operator.username, via="admin_console")
        return {"name": name, "status": "retired", "seq": result["seq"]}


@router.post("/teams/{team}/skills/{name}/restore")
async def admin_restore_skill(team: str, name: str, operator: AdminContext = CurrentOperator):
    async with audited(operator, "skill.restore", team, object_type="skill", object_id=name):
        await _team_path(team, operator)
        result = await service.set_status(State.pg, team, name, "current",
                                          by=operator.username, via="admin_console")
        return {"name": name, "status": "current", "seq": result["seq"]}


@router.put("/teams/{team}/skills/{name}/owner")
async def admin_set_skill_owner(team: str, name: str, body: SkillOwnerRequest,
                                operator: AdminContext = CurrentOperator):
    async with audited(operator, "skill.owner", team, object_type="skill", object_id=name,
                       detail={"owner": body.owner}):
        await _team_path(team, operator)
        await _check_owner(body.owner)
        skill = await service.set_owner(State.pg, team, name, body.owner)
        return {"name": name, "owner": skill["owner"]}


@router.post("/teams/{team}/skills/{name}/promote")
async def admin_promote_skill(team: str, name: str, request: Request,
                              operator: AdminContext = CurrentOperator):
    """Copy the skill into the universe; when the universe has the name, the
    copy becomes its next version."""
    async with audited(operator, "skill.promote", team, object_type="skill", object_id=name):
        await _team_path(team, operator)
        return await service.promote(State.pg, State.falkor, request.app.state.gate,
                                     State.docs_embedder, team, name,
                                     by=operator.username, via="admin_console", operator=True)
