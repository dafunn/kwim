"""KWIM service - the framework-agnostic contract (docs/contract.md).

Wires the contract surface to the backends and the governance gate. Endpoints not
yet implemented return 501 with a clear marker.

Run (dev):  uvicorn app.main:app
  env: KWIM_PG_DSN, KWIM_FALKOR_URL, KWIM_RABBITMQ_URL, KWIM_API_KEYS="devkey:acme"
"""
import datetime
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, status

from .auth import CurrentTeam, TeamContext
from .config import settings
from . import otel
from .embedder import Embedder
from .freshness import _to_dt, compute_freshness, worst_freshness
from .gate import Gate
from .models import (
    Accepted, AdvisoryProposal, AuditVersion, CheckRequest, CheckResult,
    CodeArchitecture, CodeChange, CodeFunction, ConstraintProposal,
    EpisodicCursor, EpisodicEvent, EpisodicEventOut, EpisodicWindow, EventAccepted, Fact,
    FactAudit, FactDetail, FactMatch, FactProposal, FactProvenance,
    ProposalStatus, Rule, SeedRule, SemanticItem, SemanticWrite, WorkingWrite,
)
from .review import review
from .semantic_consumer import SemanticConsumer
from .stores.bus import Bus
from .stores.falkor import FalkorStore
from .stores.postgres import PostgresStore

# Severity ordering for wisdom.check verdict resolution (higher index = higher severity).
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_FRESHNESS_SORT = {"fresh": 0, "aging": 1, "stale": 2}


def _enrich_fact(r: dict) -> dict:
    """Add freshness + as_of to one fact row.

    `as_of` is the later of `created_at` and `last_verified_at` (if present),
    so a re-verified fact does not age while the pipeline is still vouching for it.
    """
    dc = r.get("decay_class") or "slow"
    created_dt = _to_dt(r.get("created_at"))
    verified_dt = _to_dt(r.get("last_verified_at"))
    as_of_dt = max((dt for dt in (created_dt, verified_dt) if dt is not None), default=None)
    as_of = as_of_dt.isoformat() if as_of_dt is not None else ""
    f = compute_freshness(as_of, dc, settings.halflife_slow_days, settings.halflife_fast_hours)
    return {**r, "decay_class": dc, "as_of": as_of, "freshness": f}


def _enrich_facts(rows: list[dict]) -> list[dict]:
    """Enrich every row and sort fresh-first - the ranking knowledge/query and the
    context bundle use. The sort is stable, so rows already ordered by relevance
    keep that order within a freshness band.

    knowledge/search deliberately does not use this: there, distance ordering is
    the answer, so it enriches per-row and leaves the ranking alone.
    """
    enriched = [_enrich_fact(r) for r in rows]
    enriched.sort(key=lambda x: _FRESHNESS_SORT.get(x["freshness"], 0))
    return enriched


def _situation_params(request: Request) -> dict[str, str]:
    """Collect ?situation.<key>=<val> params into an open situation dict."""
    situation: dict[str, str] = {}
    # When a handler is called directly (tests), `request` is the Ellipsis
    # sentinel, not a Request - same caveat as memory_context's `repos` param.
    qp = getattr(request, "query_params", None)
    if qp is not None:
        for key, val in qp.multi_items():
            if key.startswith("situation."):
                situation[key[10:]] = val  # strip "situation." prefix
    return situation


class State:
    pg: PostgresStore
    falkor: FalkorStore
    bus: Bus
    embedder: Embedder


@asynccontextmanager
async def lifespan(app: FastAPI):
    State.pg = PostgresStore()
    State.falkor = FalkorStore()
    State.bus = Bus()
    State.embedder = Embedder()
    await State.pg.connect()
    await State.falkor.connect()
    await State.bus.connect()
    # Gate consumes proposals on its own channel within this process; split into
    # its own deployment if it needs to scale independently.
    gate_channel = await State.bus._conn.channel()
    app.state.gate = Gate(State.pg, State.falkor, gate_channel, State.embedder)
    await app.state.gate.run()
    # Semantic consumer mirrors the gate: durable queue on kwim.*.episodic,
    # embeds text events and writes :SemanticItem nodes.
    semantic_channel = await State.bus._conn.channel()
    app.state.semantic_consumer = SemanticConsumer(State.falkor, State.embedder, semantic_channel)
    await app.state.semantic_consumer.run()
    try:
        yield
    finally:
        await State.bus.close()
        await State.falkor.close()
        await State.pg.close()
        await State.embedder.close()


app = FastAPI(
    title="KWIM", version="0.2.0",
    summary="Knowledge - Wisdom - Intelligence - Memory - the contract teams code against.",
    lifespan=lifespan,
)
otel.configure(app)


def _todo(what: str):
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=f"{what}: wired in a later slice")


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "service": "kwim", "version": app.version}


# --- Knowledge ---------------------------------------------------------------
knowledge = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])

@knowledge.get("/query", response_model=list[Fact])
async def knowledge_query(team: TeamContext = CurrentTeam,
                          fact_type: str | None = None, status_: str = "current", limit: int = 50,
                          about: list[str] | None = Query(None),
                          source_kind: str | None = None):
    rows = await State.falkor.query_facts(team.team, fact_type, status_, limit,
                                          about=about, source_kind=source_kind)
    return [Fact(**r) for r in _enrich_facts(rows)]

@knowledge.get("/search", response_model=list[FactMatch])
async def knowledge_search(q: str, limit: int = 10, fact_type: str | None = None,
                           about: list[str] | None = Query(None),
                           team: TeamContext = CurrentTeam):
    """Semantic search over governed facts - Tier 1 retrieval for Knowledge.

    The retrieval counterpart to /query. /query needs the caller to already know the
    tag it wants; this one takes free text and answers "what do we know about this?"
    - the case where the agent does not know what it is looking for.

    Results are ranked by cosine distance (`score`, lower = closer) and are not
    re-sorted by freshness; each carries its own freshness marker so the caller
    can judge.
    `about` / `fact_type` narrow the candidate set before scoring, with the same
    case-insensitive semantics /query uses.

    Facts with no embedding cannot match - see `app.backfill_embeddings`.
    """
    try:
        qvec = (await State.embedder.embed([q]))[0]
    except Exception as exc:
        # 503, never an empty list. A silent [] here is indistinguishable from
        # "we know nothing about that", which is the one answer this endpoint
        # must never give by accident.
        log.warning("knowledge_search: embed failed for q=%r: %s", q, exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="embedder unavailable - semantic search cannot run") from exc
    rows = await State.falkor.search_facts(team.team, qvec, limit=limit,
                                           about=about, fact_type=fact_type)
    return [FactMatch(**_enrich_fact(r)) for r in rows]

@knowledge.get("/facts/{fact_id}", response_model=FactDetail)
async def knowledge_fact(fact_id: str, team: TeamContext = CurrentTeam):
    row = await State.falkor.get_fact_provenance(team.team, fact_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"fact {fact_id} not found")
    return FactDetail(
        fact=Fact(id=row["id"], statement=row["statement"], fact_type=row["fact_type"],
                  status=row["status"], created_at=row["created_at"],
                  source_kind=row.get("source_kind"),
                  last_verified_at=row.get("last_verified_at")),
        provenance=FactProvenance(proposed_by=row["proposed_by"],
                                  supported_by=row["supported_by"], supersedes=row["supersedes"]),
    )

@knowledge.post("/facts/{fact_id}/reaffirm", status_code=status.HTTP_204_NO_CONTENT)
async def knowledge_reaffirm(fact_id: str, team: TeamContext = CurrentTeam):
    """Non-governance freshness touch: stamp last_verified_at = now.

    Distinct from human confirm/retract - this is a machine assertion that the
    source still vouches for the fact. 404 if the fact does not exist.
    """
    found = await State.falkor.reaffirm_fact(team.team, fact_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"fact {fact_id} not found")


@knowledge.get("/audit/{fact_id}", response_model=FactAudit)
async def knowledge_audit(fact_id: str, at: str | None = None, team: TeamContext = CurrentTeam):
    # ?at= (point-in-time) is deferred for now - we return the full version chain.
    # True at= needs the commit_log as the authoritative time source (see wisdom/
    # data-model notes); the graph has no valid_from/superseded_at.
    chain = await State.falkor.audit_fact(team.team, fact_id)
    if not chain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"fact {fact_id} not found")
    return FactAudit(fact_id=fact_id, chain=[AuditVersion(**v) for v in chain])

@knowledge.post("/propose", response_model=Accepted, status_code=status.HTTP_202_ACCEPTED)
async def knowledge_propose(proposal: FactProposal, team: TeamContext = CurrentTeam):
    pid = str(uuid.uuid4())
    await State.falkor.proposal_set(pid, {"id": pid, "object_type": "fact", "status": "accepted"})
    await State.bus.publish(team.team, "knowledge.proposed", {
        "proposal_id": pid, "team": team.team, "object_type": "fact",
        "proposed_by": proposal.source_kind == "repo_sync" and "repo-sync" or team.key_id,
        "body": proposal.model_dump(),
    })
    return Accepted(proposal_id=pid)


# --- Wisdom ------------------------------------------------------------------
wisdom = APIRouter(prefix="/v1/wisdom", tags=["wisdom"])

@wisdom.get("/rules", response_model=list[Rule])
async def wisdom_rules(request: Request, team: TeamContext = CurrentTeam,
                       limit: int = 20):
    """Applicable rules for a situation.

    The situation is an open set of team-defined key/values passed as
    ?situation.<key>=<value> params, AND-matched against rule situations.
    """
    situation = _situation_params(request)
    rows = await State.falkor.query_rules(team.team, situation, limit)
    return [
        Rule(
            id=r["id"],
            rule_type=r["rule_type"],
            situation=r["situation"],
            approach=r["approach"],
            evidence_count=r["evidence_count"],
            status=r["status"],
            scope=r.get("scope", "team"),
            source=r.get("_source", "team"),
            # constraint enforcement fields - the store returns these; pass them
            # through so wisdom/check has rule content.
            action_pattern=r.get("action_pattern"),
            verdict=r.get("verdict"),
            authority=r.get("authority"),
            severity=r.get("severity"),
            check_tier=r.get("check_tier"),
        )
        for r in rows
    ]

@wisdom.post("/propose", response_model=Accepted, status_code=status.HTTP_202_ACCEPTED)
async def wisdom_propose(proposal: AdvisoryProposal | ConstraintProposal,
                         team: TeamContext = CurrentTeam):
    pid = str(uuid.uuid4())
    await State.falkor.proposal_set(pid, {"id": pid, "object_type": "rule", "status": "accepted"})
    await State.bus.publish(team.team, "wisdom.proposed", {
        "proposal_id": pid, "team": team.team, "object_type": "rule",
        "proposed_by": team.key_id, "body": proposal.model_dump(),
    })
    return Accepted(proposal_id=pid)

@wisdom.post("/check", response_model=CheckResult)
async def wisdom_check(req: CheckRequest, team: TeamContext = CurrentTeam):
    """Deterministic constraint enforcement - sync, no LLM, target <50ms.

    Loads approved deterministic constraints (team + universe merged), evaluates
    each rule's action_pattern regex against action["content"], and resolves
    to one verdict:
      - critical severity -> escalate (regardless of stored verdict).
      - else highest-severity match's stored verdict wins.
      - no match -> allow.
    classifier-tier constraints are skipped in v1 (needs the embedder).
    """
    # Load approved deterministic constraints only; empty list if none yet.
    all_rules = await State.falkor.query_rules(team.team, limit=settings.rule_scan_limit)
    constraints = [
        r for r in all_rules
        if r["rule_type"] == "constraint"
        and r.get("check_tier") == "deterministic"
        and r.get("action_pattern")
    ]

    target = req.action.get("content", "")
    matches: list[dict] = []
    for r in constraints:
        try:
            if re.search(r["action_pattern"], target):
                matches.append(r)
        except re.error:
            # Malformed stored pattern: skip rather than crash (shouldn't happen
            # after gate validation, but be defensive on the hot path).
            continue

    if not matches:
        return CheckResult(verdict="allow", check_tier="deterministic")

    # Severity-wins resolution: critical always escalates; else highest severity's
    # stored verdict wins. Ties broken by iteration order (stable after evidence_count
    # sort from query_rules - higher evidence first, so more-trusted rule wins).
    best = max(matches, key=lambda r: _SEVERITY_ORDER.get(r.get("severity", ""), 0))
    if _SEVERITY_ORDER.get(best.get("severity", ""), 0) >= _SEVERITY_ORDER["critical"]:
        verdict = "escalate"
    else:
        verdict = best.get("verdict", "allow")
    return CheckResult(
        verdict=verdict,
        matched_rule=best["id"],
        reason=best.get("authority") or None,
        check_tier="deterministic",
    )


@wisdom.post("/promote/{rule_id}", status_code=status.HTTP_200_OK)
async def wisdom_promote(rule_id: str, team: TeamContext = CurrentTeam):
    """Promote an approved team rule to the shared universe graph. Human-only.

    Gated by KWIM_PROMOTE_KEYS: the caller's key-id prefix must appear in that
    comma-separated list. Proper RBAC is a later addition.

    Creates a new universe object (new id) copying rule content with scope=universe
    and provenance = the promotion record only. No team-private evidence crosses.
    """
    # --- capability gate ---
    allowed_prefixes = [p.strip() for p in settings.promote_keys.split(",") if p.strip()]
    if not allowed_prefixes:
        # Fail-closed: promotion is a human-only, high-bar governance action that writes
        # to the shared universe graph. Unless an operator has explicitly granted promote
        # capability via KWIM_PROMOTE_KEYS, nobody promotes (an unset var must not mean
        # "any team key, including agent keys, can push to universe").
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="promotion not configured - set KWIM_PROMOTE_KEYS to grant promote capability")
    if team.key_id not in allowed_prefixes:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="key not in KWIM_PROMOTE_KEYS - promotion not permitted")

    # Fetch the source rule from the team graph (must be approved).
    source_rows = await State.falkor.query_rules(team.team, limit=settings.rule_scan_limit)
    source = next((r for r in source_rows if r["id"] == rule_id
                   and r.get("_source") == "team" and r["status"] == "approved"), None)
    if source is None:
        # Try direct lookup in case the rule is approved but not in default filters.
        node = await State.falkor.get_rule(team.team, rule_id)
        if node is None or node.get("status") != "approved":
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail=f"rule {rule_id} not found or not approved in team {team.team!r}")
        # We have the node but not the full payload - can't promote without content.
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"rule {rule_id} not found in team graph query results")

    universe_id = str(uuid.uuid4())
    promoted_at = datetime.datetime.utcnow().isoformat() + "Z"
    provenance = {
        "proposed_by": team.key_id,          # the promoting human's key-id handle
        "promoted_from_team": team.team,
        "promoted_from_id": rule_id,
        "promoted_by": team.key_id,
        "promoted_at": promoted_at,
        "learned_from": [],                  # no team-private evidence crosses
    }

    # Build the universe rule payload (same content, new scope + provenance).
    payload = {
        "rule_type": source["rule_type"],
        "situation": source.get("situation"),
        "approach": source.get("approach"),
        "action_pattern": source.get("action_pattern"),
        "verdict": source.get("verdict"),
        "authority": source.get("authority"),
        "severity": source.get("severity"),
        "check_tier": source.get("check_tier"),
        "scope": "universe",
        "evidence_count": source.get("evidence_count", 0),
    }

    # 1. Durable record in universe.commit_log.
    seq = await State.pg.append_commit("universe", {
        "object_type": "rule", "object_id": universe_id, "operation": "commit",
        "payload": payload, "provenance": provenance,
        "proposed_by": team.key_id,
        "source_kind": "promotion",          # distinct provenance kind (commit_log check permits it)
        "gate_decision": "human_approved",
    })

    # 2. Upsert into kwim_universe.
    await State.falkor.materialize_rule(
        "universe",
        {**payload, "id": universe_id, "commit_seq": seq, "status": "approved",
         "promoted_from_id": rule_id, "promoted_from_team": team.team},
        provenance,
    )

    # 3. Tag the team original so the dedup merge can suppress it.
    await State.falkor.tag_rule_promoted(team.team, rule_id)

    return {
        "status": "promoted",
        "universe_rule_id": universe_id,
        "promoted_from": rule_id,
        "team": team.team,
        "seq": seq,
    }


@wisdom.post("/seed", status_code=status.HTTP_201_CREATED)
async def wisdom_seed(body: SeedRule, team: TeamContext = CurrentTeam):
    """Operator-gated direct commit of an approved rule to a team graph.

    This is the escape hatch for human-curated seeding: it bypasses the
    proposal/approval cycle and writes directly to FalkorDB with
    status='approved'. Gated by the same KWIM_PROMOTE_KEYS capability
    used for universe promotion (human-only, high-bar).

    The rule idempotency key is the caller-supplied `id`. Overwrites on re-seed.
    Mirrors the promote endpoint's commit path: append_commit needs `gate_decision`
    and materialize_rule needs `commit_seq` (both hard keys) - pass both.
    """
    # --- capability gate (same as promote) ---
    allowed_prefixes = [p.strip() for p in settings.promote_keys.split(",") if p.strip()]
    if not allowed_prefixes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="seeding not configured - set KWIM_PROMOTE_KEYS to grant seed capability",
        )
    if team.key_id not in allowed_prefixes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="key not in KWIM_PROMOTE_KEYS - seeding not permitted",
        )

    payload = body.model_dump()
    provenance = {"seeded_by": team.key_id,
                  "seeded_at": datetime.datetime.utcnow().isoformat() + "Z"}
    seq = await State.pg.append_commit(team.team, {
        "object_type": "rule", "object_id": body.id, "operation": "commit",
        "payload": payload, "provenance": provenance,
        "proposed_by": team.key_id,
        "source_kind": "agent_proposal",       # commit_log CHECK: agent_proposal|repo_sync|promotion
        "gate_decision": "human_approved",      # operator-curated = human-approved
    })
    await State.falkor.materialize_rule(
        team.team,
        {**payload, "id": body.id, "commit_seq": seq, "status": "approved", "scope": "team"},
        provenance,
    )
    return {"status": "seeded", "rule_id": body.id, "seq": seq}


# --- Memory ------------------------------------------------------------------
memory = APIRouter(prefix="/v1/memory", tags=["memory"])

@memory.post("/episodic", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
async def memory_episodic(event: EpisodicEvent, team: TeamContext = CurrentTeam):
    # Durable write straight to Postgres (it IS the system-of-record); also emit on
    # the bus for any downstream consumers (e.g. future Wisdom distillation).
    event_id = await State.pg.append_episodic(team.team, event.model_dump())
    await State.bus.publish(team.team, "episodic", {"event_id": event_id, **event.model_dump()})
    return EventAccepted(event_id=event_id)

_EPISODIC_MAX_LIMIT = settings.episodic_max_limit


@memory.get("/episodic", response_model=EpisodicWindow)
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

@memory.get("/context")
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
        try:
            qvec = (await State.embedder.embed([subject]))[0]
        except Exception:
            log.warning("memory_context: embed failed for subject=%r", subject)

    knowledge: list[dict] = []
    knowledge_queried = False
    tag_n = semantic_n = 0
    if subject:
        knowledge_queried = True
        tag_rows: list[dict] = []
        try:
            tag_rows = await State.falkor.query_facts(
                team.team, fact_type=None, status="current",
                limit=settings.fact_query_limit, about=[subject])
        except Exception:
            log.warning("memory_context: knowledge tag query failed for subject=%r", subject)
        tag_n = len(tag_rows)

        # `subject` doubles as a tag and as free text. The tag hit is exact and
        # high-precision, so it leads; the KNN is what answers a subject the caller
        # could not have known the tag for. Union in that order, and the added
        # retrieval can never cost an exact match.
        sem_rows: list[dict] = []
        if qvec is not None:
            try:
                sem_rows = await State.falkor.search_facts(
                    team.team, qvec, limit=settings.fact_query_limit)
            except Exception:
                log.warning("memory_context: knowledge search failed for subject=%r", subject)
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

@memory.get("/semantic", response_model=list[SemanticItem])
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


@memory.post("/semantic", response_model=SemanticItem, status_code=status.HTTP_201_CREATED)
async def memory_semantic_write(body: SemanticWrite, team: TeamContext = CurrentTeam):
    """Explicit semantic write (deferred; direct write path).

    Accepts content + optional metadata, embeds it, and writes to FalkorDB.
    If `id` is omitted, generates a UUID.

    TODO: this writes no Postgres record, so `app.rebuild` cannot replay these
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


@memory.get("/working/{session}/{key}")
async def memory_working_get(session: str, key: str, team: TeamContext = CurrentTeam):
    v = await State.falkor.working_get(team.team, session, key)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such working-memory key")
    return {"value": v}

@memory.put("/working/{session}/{key}")
async def memory_working_put(session: str, key: str, body: WorkingWrite,
                             team: TeamContext = CurrentTeam):
    await State.falkor.working_set(team.team, session, key, str(body.value), body.ttl_seconds)
    return {"status": "ok"}


# --- Proposals (async write status) ------------------------------------------
proposals = APIRouter(prefix="/v1/proposals", tags=["proposals"])

@proposals.get("/{proposal_id}", response_model=ProposalStatus)
async def proposal_status(proposal_id: str, team: TeamContext = CurrentTeam):
    doc = await State.falkor.proposal_get(proposal_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal id")
    return ProposalStatus(**doc)


# --- Code graph - reads over kwim_<team>_code --------------------------------
# Read-only queries against the team's code graph. Every call emits an episodic
# `code_tool_observation` event, which the distiller can promote into governed
# K/W (hubs, high-fan-in, cross-repo interfaces).
code = APIRouter(prefix="/v1/code", tags=["code"])


async def _emit_code_observation(team: str, tool: str, args: dict, summary: dict) -> None:
    """Land a code-graph read in episodic memory (best-effort - never fail the read)."""
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


@code.get("/search", response_model=list[CodeFunction])
async def code_search(
    q: str | None = None, name: str | None = None,
    repos: list[str] | None = Query(None), limit: int = 10,
    team: TeamContext = CurrentTeam,
):
    """Find functions by semantic query (`q`, embedded) or exact `name`, repo-scoped."""
    qvec = None
    if q:
        try:
            qvec = (await State.embedder.embed([q]))[0]
        except Exception:
            log.warning("code_search: embed failed for q=%r", q)
    rows = await State.falkor.code_search(
        team.team, qvec=qvec, name=name, repos=repos, limit=limit)
    await _emit_code_observation(team.team, "search", {"q": q, "name": name, "repos": repos},
                                 {"n": len(rows)})
    return [CodeFunction(**r) for r in rows]


@code.get("/trace/{fn_id}", response_model=list[CodeFunction])
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


@code.get("/architecture", response_model=CodeArchitecture)
async def code_architecture(repos: list[str] | None = Query(None), team: TeamContext = CurrentTeam):
    """High-level structure: communities (modules) + their representative hub functions."""
    arch = await State.falkor.code_architecture(team.team, repos=repos)
    await _emit_code_observation(team.team, "architecture", {"repos": repos},
                                 {"communities": len(arch.get("communities", []))})
    return CodeArchitecture(**arch)


@code.get("/changes", response_model=list[CodeChange])
async def code_changes(repo: str, commit: str, team: TeamContext = CurrentTeam):
    """Files whose indexed commit differs from `commit` - the impact surface."""
    rows = await State.falkor.code_changed_since(team.team, commit=commit, repo=repo)
    await _emit_code_observation(team.team, "changes", {"repo": repo, "commit": commit},
                                 {"n": len(rows)})
    return [CodeChange(**r) for r in rows]


for r in (knowledge, wisdom, memory, proposals, review, code):
    app.include_router(r)
