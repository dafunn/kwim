"""Skills for agents: search on each turn, load one, list, propose, promote.
Reads cover the team's skills and the universe's; a team skill hides a
universe skill of the same name.
"""
from typing import Literal

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel

from ..auth import CurrentTeam, TeamContext, require_capability
from ..config import settings
from ..runtime import State
from ..skills import service

router = APIRouter(prefix="/v1/skills", tags=["skills"])


class SkillProposeRequest(BaseModel):
    text: str
    supersedes: str | None = None
    scope: Literal["team", "universe"] = "team"


@router.get("/search")
async def skills_search(
    q: str = Query(..., min_length=1, max_length=2000),
    limit: int = Query(settings.skills_search_limit, ge=1, le=20),
    agent: str | None = None,
    tag: str | None = None,
    team: TeamContext = CurrentTeam,
):
    """Skills that match `q`, best first: names and descriptions, not bodies.
    With `agent`, only skills offered to that persona."""
    require_capability(team, "read")
    return await service.search(State.pg, State.docs_embedder, team.team, q, limit=limit,
                                agent=agent, tag=tag)


@router.get("")
async def skills_list(agent: str | None = None, tag: str | None = None,
                      team: TeamContext = CurrentTeam):
    require_capability(team, "read")
    return {"items": await service.list_offered(State.pg, team.team, agent=agent, tag=tag)}


@router.post("/propose", status_code=status.HTTP_202_ACCEPTED)
async def skills_propose(body: SkillProposeRequest, request: Request,
                         team: TeamContext = CurrentTeam):
    """Propose a new skill, or with `supersedes` a revision (`scope: universe`
    for a universe skill). Always goes to human review."""
    require_capability(team, "propose")
    return await service.propose(State.pg, State.falkor, request.app.state.gate, team.team,
                                 body.text, supersedes=body.supersedes, scope=body.scope,
                                 proposed_by=team.key_id)


@router.get("/{name}")
async def skills_get(name: str, team: TeamContext = CurrentTeam):
    """The skill to follow: its body, refs and version."""
    require_capability(team, "read")
    return await service.get(State.pg, team.team, name)


@router.post("/{name}/promote")
async def skills_promote(name: str, request: Request, team: TeamContext = CurrentTeam):
    """Copy the team's skill into the universe. Requires `promote`. When the
    universe already has the name, the promotion waits for operator review."""
    require_capability(team, "promote")
    return await service.promote(State.pg, State.falkor, request.app.state.gate,
                                 State.docs_embedder, team.team, name, by=team.key_id,
                                 via="api", operator=False)
