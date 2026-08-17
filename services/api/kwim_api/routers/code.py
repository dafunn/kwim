"""Code graph - reads over kwim_<team>_code.

Read-only queries against the team's code graph. Every call emits an episodic
`code_tool_observation` event, which the distiller can promote into governed
K/W (hubs, high-fan-in, cross-repo interfaces).
"""
import logging

from fastapi import APIRouter, HTTPException, Query, status

from ..auth import CurrentTeam, TeamContext
from ..models import CodeArchitecture, CodeChange, CodeFunction
from ..runtime import State
from .common import best_effort

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/code", tags=["code"])


async def _emit_code_observation(team: str, tool: str, args: dict, summary: dict) -> None:
    """Land a code-graph read in episodic memory.

    Best-effort: a failed emit never fails the read. The guard spans the whole
    append-then-publish pair, so it is written out rather than using
    `common.best_effort`.
    """
    try:
        event = {
            "agent_id": "code-tool", "session_id": tool,
            "event_type": "code_tool_observation",
            "event_data": {"tool": tool, "args": args, "result": summary},
        }
        event_id = await State.pg.append_episodic(team, event)
        await State.bus.publish(team, "episodic", {"event_id": event_id, **event})
    except Exception:
        log.warning("code observation emit failed (tool=%s)", tool)


@router.get("/search", response_model=list[CodeFunction])
async def code_search(
    q: str | None = None, name: str | None = None,
    repos: list[str] | None = Query(None), limit: int = 10,
    team: TeamContext = CurrentTeam,
):
    """Find functions by semantic query (`q`, embedded) or exact `name`, repo-scoped."""
    qvec = None
    if q:
        embedded = await best_effort(
            lambda: State.embedder.embed([q]), fallback=None,
            what=f"code_search embed (q={q!r})")
        qvec = embedded[0] if embedded else None
    rows = await State.falkor.code_search(
        team.team, qvec=qvec, name=name, repos=repos, limit=limit)
    await _emit_code_observation(team.team, "search", {"q": q, "name": name, "repos": repos},
                                 {"n": len(rows)})
    return [CodeFunction(**r) for r in rows]


@router.get("/trace/{fn_id}", response_model=list[CodeFunction])
async def code_trace(
    fn_id: str, direction: str = "outbound", depth: int = 2,
    min_confidence: float = 0.0, team: TeamContext = CurrentTeam,
):
    """Call-chain trace. direction=outbound (callees/deps) | inbound (callers/impact).
    Filter out low-trust edges with min_confidence."""
    if direction not in ("outbound", "inbound"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail="direction must be 'outbound' or 'inbound'")
    rows = await State.falkor.code_trace_calls(
        team.team, fn_id=fn_id, direction=direction, depth=depth, min_confidence=min_confidence)
    await _emit_code_observation(team.team, "trace",
                                 {"fn_id": fn_id, "direction": direction, "depth": depth},
                                 {"n": len(rows)})
    return [CodeFunction(**r) for r in rows]


@router.get("/architecture", response_model=CodeArchitecture)
async def code_architecture(repos: list[str] | None = Query(None), team: TeamContext = CurrentTeam):
    """High-level structure: communities (modules) + their representative hub functions."""
    arch = await State.falkor.code_architecture(team.team, repos=repos)
    await _emit_code_observation(team.team, "architecture", {"repos": repos},
                                 {"communities": len(arch.get("communities", []))})
    return CodeArchitecture(**arch)


@router.get("/changes", response_model=list[CodeChange])
async def code_changes(repo: str, commit: str, team: TeamContext = CurrentTeam):
    """Files whose indexed commit differs from `commit` - the impact surface."""
    rows = await State.falkor.code_changed_since(team.team, commit=commit, repo=repo)
    await _emit_code_observation(team.team, "changes", {"repo": repo, "commit": commit},
                                 {"n": len(rows)})
    return [CodeChange(**r) for r in rows]
