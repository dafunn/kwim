"""Knowledge surface - governed facts: query, semantic search, provenance, audit,
reaffirm, and propose (docs/contract.md).
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status

from ..auth import CurrentTeam, TeamContext
from ..models import (
    Accepted,
    AuditVersion,
    Fact,
    FactAudit,
    FactDetail,
    FactMatch,
    FactProposal,
    FactProvenance,
)
from ..runtime import State
from .common import _enrich_fact, _enrich_facts

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


@router.get("/query", response_model=list[Fact])
async def knowledge_query(team: TeamContext = CurrentTeam,
                          fact_type: str | None = None, status_: str = "current", limit: int = 50,
                          about: list[str] | None = Query(None),
                          source_kind: str | None = None):
    rows = await State.falkor.query_facts(team.team, fact_type, status_, limit,
                                          about=about, source_kind=source_kind)
    return [Fact(**r) for r in _enrich_facts(rows)]


@router.get("/search", response_model=list[FactMatch])
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

    Facts with no embedding cannot match - see `kwim_api.backfill_embeddings`.
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


@router.get("/facts/{fact_id}", response_model=FactDetail)
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


@router.post("/facts/{fact_id}/reaffirm", status_code=status.HTTP_204_NO_CONTENT)
async def knowledge_reaffirm(fact_id: str, team: TeamContext = CurrentTeam):
    """Non-governance freshness touch: stamp last_verified_at = now.

    Distinct from human confirm/retract - this is a machine assertion that the
    source still vouches for the fact. 404 if the fact does not exist.
    """
    found = await State.falkor.reaffirm_fact(team.team, fact_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"fact {fact_id} not found")


@router.get("/audit/{fact_id}", response_model=FactAudit)
async def knowledge_audit(fact_id: str, at: str | None = None, team: TeamContext = CurrentTeam):
    # ?at= (point-in-time) is deferred for now - we return the full version chain.
    # True at= needs the commit_log as the authoritative time source (see wisdom/
    # data-model notes); the graph has no valid_from/superseded_at.
    chain = await State.falkor.audit_fact(team.team, fact_id)
    if not chain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"fact {fact_id} not found")
    return FactAudit(fact_id=fact_id, chain=[AuditVersion(**v) for v in chain])


@router.post("/propose", response_model=Accepted, status_code=status.HTTP_202_ACCEPTED)
async def knowledge_propose(proposal: FactProposal, team: TeamContext = CurrentTeam):
    pid = str(uuid.uuid4())
    await State.falkor.proposal_set(pid, {"id": pid, "object_type": "fact", "status": "accepted"})
    await State.bus.publish(team.team, "knowledge.proposed", {
        "proposal_id": pid, "team": team.team, "object_type": "fact",
        "proposed_by": proposal.source_kind == "repo_sync" and "repo-sync" or team.key_id,
        "body": proposal.model_dump(),
    })
    return Accepted(proposal_id=pid)
