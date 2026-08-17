"""Memory surface - episodic append/window, the assembled turn context, semantic
read/write, and working-memory KV (docs/contract.md).
"""
import datetime
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..auth import CurrentTeam, TeamContext
from ..config import settings
from ..freshness import worst_freshness
from ..models import (
    EpisodicCursor,
    EpisodicEvent,
    EpisodicEventOut,
    EpisodicWindow,
    EventAccepted,
    SemanticItem,
    SemanticWrite,
    WorkingWrite,
)
from ..runtime import State
from .common import _enrich_facts, _situation_params, best_effort

log = logging.getLogger(__name__)

_EPISODIC_MAX_LIMIT = settings.episodic_max_limit

router = APIRouter(prefix="/v1/memory", tags=["memory"])


@router.post("/episodic", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
async def memory_episodic(event: EpisodicEvent, team: TeamContext = CurrentTeam):
    # Durable write straight to Postgres, the system-of-record; also emit on the
    # bus for any downstream consumers (e.g. future Wisdom distillation).
    event_id = await State.pg.append_episodic(team.team, event.model_dump())
    await State.bus.publish(team.team, "episodic", {"event_id": event_id, **event.model_dump()})
    return EventAccepted(event_id=event_id)


@router.get("/episodic", response_model=EpisodicWindow)
async def memory_episodic_window(
    since_ts: str | None = None, since_id: str | None = None,
    limit: int = settings.episodic_default_limit, event_type: str | None = None, agent_id: str | None = None,
    order: str = "asc",
    team: TeamContext = CurrentTeam,
):
    """Windowed, team-scoped read over episodic events on the (occurred_at, id) cursor.

    `since_ts`/`since_id` form an exclusive composite cursor (the watermark); omit both
    to read from the start (`order=asc`, default) or end (`order=desc`). `order=desc`
    returns newest-first and treats the cursor as an exclusive upper bound - e.g.
    `?event_type=distiller_watermark&limit=1&order=desc` fetches the single latest
    watermark event in O(1) regardless of how many have accumulated. Used by the
    distiller and other batch readers - a query, not a gate-write, so it's a plain
    authenticated read.
    """
    if (since_ts is None) != (since_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail="since_ts and since_id must be provided together")
    if limit < 1 or limit > _EPISODIC_MAX_LIMIT:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail=f"limit must be between 1 and {_EPISODIC_MAX_LIMIT}")
    if order not in ("asc", "desc"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail=f"order must be 'asc' or 'desc', got {order!r}")
    if since_ts is not None:
        try:
            parsed_ts = datetime.datetime.fromisoformat(since_ts)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                 detail=f"since_ts is not a valid ISO8601 timestamp: {since_ts!r}")
    else:
        parsed_ts = None
    rows = await State.pg.read_episodic(
        team.team, since_ts=parsed_ts, since_id=since_id,
        limit=limit, event_type=event_type, agent_id=agent_id, order=order,
    )
    events = [
        EpisodicEventOut(
            id=str(r["id"]), agent_id=r["agent_id"], session_id=r["session_id"],
            event_type=r["event_type"], event_data=r["event_data"],
            occurred_at=r["occurred_at"].isoformat(),
        )
        for r in rows
    ]
    next_cursor = EpisodicCursor(ts=events[-1].occurred_at, id=events[-1].id) if events else None
    return EpisodicWindow(events=events, next_cursor=next_cursor)


@router.get("/context")
async def memory_context(
    session_id: str,
    subject: str | None = None,
    repos: list[str] | None = Query(None),
    request: Request = ...,  # FastAPI auto-injects
    team: TeamContext = CurrentTeam,
):
    """Assemble working context for a turn.

    Wisdom enrichment: calls query_rules with the open situation dict
    (?situation.<key>=<value> params) and packs approved rules, ranked by
    evidence_count.

    Knowledge enrichment: when `subject` is present, the knowledge slot is the union
    of an exact `about` tag match and a semantic KNN over the same facts, so a
    free-text subject the caller has no tag for still retrieves. Coverage markers
    signal whether each slot was queried and non-empty (the absence trigger for
    callers), and break the knowledge count down by how it was found.
    """
    situation = _situation_params(request)
    recent = await State.pg.recent_episodic(team.team, session_id)
    wisdom_rows = await State.falkor.query_rules(
        team.team, situation, limit=settings.rule_query_limit)
    wisdom = [
        {
            "id": r["id"],
            "rule_type": r["rule_type"],
            "situation": r.get("situation"),
            "approach": r.get("approach"),
            "evidence_count": r.get("evidence_count", 0),
            "status": r["status"],
            "scope": r.get("scope", "team"),
            "source": r.get("_source", "team"),
        }
        for r in wisdom_rows
    ]

    # One embed of `subject` serves both semantic slots (knowledge KNN + code).
    qvec: list[float] | None = None
    if subject:
        embedded = await best_effort(
            lambda: State.embedder.embed([subject]), fallback=None,
            what=f"memory_context embed (subject={subject!r})")
        qvec = embedded[0] if embedded else None

    knowledge: list[dict] = []
    knowledge_queried = False
    tag_n = semantic_n = 0
    if subject:
        knowledge_queried = True
        tag_rows = await best_effort(
            lambda: State.falkor.query_facts(
                team.team, fact_type=None, status="current",
                limit=settings.fact_query_limit, about=[subject]),
            fallback=[], what=f"memory_context knowledge tag query (subject={subject!r})")
        tag_n = len(tag_rows)

        # `subject` doubles as a tag and as free text. The tag hit is exact and
        # high-precision, so it leads; the KNN is what answers a subject the caller
        # could not have known the tag for. Union in that order, and the added
        # retrieval can never cost an exact match.
        sem_rows: list[dict] = []
        if qvec is not None:
            sem_rows = await best_effort(
                lambda: State.falkor.search_facts(
                    team.team, qvec, limit=settings.fact_query_limit),
                fallback=[], what=f"memory_context knowledge search (subject={subject!r})")
        seen = {r["id"] for r in tag_rows}
        # Distance cutoff, and drop `score` on the way in: the context bundle is one
        # flat list of facts, so every row must have the same shape whichever half
        # it came from. The per-half counts live in coverage instead.
        new_sem = [{k: v for k, v in r.items() if k != "score"} for r in sem_rows
                   if r["id"] not in seen
                   and r.get("score", 1.0) <= settings.context_semantic_max_dist]
        semantic_n = len(new_sem)
        knowledge = _enrich_facts((tag_rows + new_sem)[: settings.fact_query_limit])

    k_freshness = worst_freshness([f["freshness"] for f in knowledge]) if knowledge else "fresh"

    # Code slot: a curated, repo-scoped warm-start map - signatures + summaries,
    # never file bodies. Ranked by semantic match, tie-broken toward higher-confidence
    # structure. Small by design for weak models.
    code: list[dict] = []
    code_cov = {"covered": False, "n": 0, "queried": False, "repos_missing": [], "reason": None}
    # `repos` is a Query(...) param; when the handler is called directly (tests) its
    # default is the sentinel, not None - so gate on it actually being a list.
    if subject and isinstance(repos, list) and repos:
        code_cov["queried"] = True
        # qvec is the shared `subject` embedding computed above; None means the
        # embedder was down, and code_search degrades to structure-only ranking.
        # Which requested repos are actually indexed? Unindexed -> deterministic signal.
        indexed = await State.falkor.code_indexed_repos(team.team)
        missing = [r for r in repos if r not in indexed]
        present = [r for r in repos if r in indexed]
        code_cov["repos_missing"] = missing
        if present:
            rows = await State.falkor.code_search(
                team.team, qvec=qvec, repos=present, limit=settings.code_slot_limit)
            code = [
                {"id": r["id"], "name": r["name"], "signature": r["signature"],
                 "summary": r["summary"], "repo": r["repo"], "path": r["path"]}
                for r in rows
            ]
        code_cov["covered"] = len(code) > 0
        code_cov["n"] = len(code)
        if missing:
            code_cov["reason"] = "repo_not_indexed"

    return {
        "recent": recent,
        "knowledge": knowledge,
        "wisdom": wisdom,
        "code": code,
        "coverage": {
            "knowledge": {
                "covered": len(knowledge) > 0, "n": len(knowledge),
                "queried": knowledge_queried, "freshness": k_freshness,
                # How the slot filled: exact `about` tag hits vs facts the semantic
                # KNN added. semantic_n > 0 with tag_n == 0 is the case that used to
                # come back empty.
                "tag_n": tag_n, "semantic_n": semantic_n,
            },
            "wisdom": {"covered": len(wisdom) > 0, "n": len(wisdom)},
            "recent": {"covered": len(recent) > 0, "n": len(recent)},
            "code": code_cov,
        },
    }


@router.get("/semantic", response_model=list[SemanticItem])
async def memory_semantic(
    q: str | None = None,
    limit: int = 10,
    request: Request = ...,  # FastAPI auto-injects
    team: TeamContext = CurrentTeam,
):
    """Semantic memory query.

    Supports two modes:
    - `q` provided: KNN vector search over embeddings, optionally filtered by metadata.
    - `q` omitted: metadata-only filter (returns matching items, score=0.0).

    Metadata filters are open ?meta.<key>=<value> params.
    """
    filters: dict[str, Any] = {}
    # Parse any meta.* query params.
    qp = getattr(request, "query_params", None)
    if qp is not None:
        for key, val in qp.multi_items():
            if key.startswith("meta."):
                meta_key = key[5:]  # strip "meta." prefix
                filters[meta_key] = val

    if q:
        qvec = await State.embedder.embed([q])
        rows = await State.falkor.query_semantic(team.team, qvec[0], limit, filters)
    else:
        rows = await State.falkor.get_by_metadata(team.team, filters)
        # Add score=0.0 for consistent response shape
        for r in rows:
            r["score"] = 0.0
    return [SemanticItem(**r) for r in rows]


@router.post("/semantic", response_model=SemanticItem, status_code=status.HTTP_201_CREATED)
async def memory_semantic_write(body: SemanticWrite, team: TeamContext = CurrentTeam):
    """Explicit semantic write (deferred; direct write path).

    Accepts content + optional metadata, embeds it, and writes to FalkorDB.
    If `id` is omitted, generates a UUID.

    TODO: this writes no Postgres record, so `kwim_api.rebuild` cannot replay these
    items and a rebuild drops them. Persist the write, then replay it in
    `rebuild._rebuild_semantic`.
    """
    item_id = body.id or str(uuid.uuid4())
    qvec = await State.embedder.embed([body.content])
    item = {
        "id": item_id,
        "content": body.content,
        "embedding": qvec[0],
        "metadata": body.metadata,
    }
    await State.falkor.materialize_semantic(team.team, item)
    return SemanticItem(id=item_id, content=body.content, score=0.0, metadata=body.metadata)


@router.get("/working/{session}/{key}")
async def memory_working_get(session: str, key: str, team: TeamContext = CurrentTeam):
    v = await State.falkor.working_get(team.team, session, key)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such working-memory key")
    return {"value": v}


@router.put("/working/{session}/{key}")
async def memory_working_put(session: str, key: str, body: WorkingWrite,
                             team: TeamContext = CurrentTeam):
    await State.falkor.working_set(team.team, session, key, str(body.value), body.ttl_seconds)
    return {"status": "ok"}
