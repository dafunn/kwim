"""Knowledge surface - governed facts: query, semantic search, provenance, audit,
reaffirm, and propose (docs/contract.md).
"""
import logging
import uuid
from datetime import UTC, datetime

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
    """Semantic search over current facts, ranked by cosine distance (`score`,
    lower is closer), each with its freshness. `about` and `fact_type` filter
    before scoring. See docs/DESIGN.md, "Retrieval".
    """
    try:
        qvec = (await State.embedder.embed([q]))[0]
    except Exception as exc:
        # 503 rather than an empty list.
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
    """Stamp last_verified_at = now on a current fact, in the graph (which also
    checks it exists; 404 if not) and then in fact_verifications."""
    now = datetime.now(UTC)
    found = await State.falkor.reaffirm_fact(
        team.team, fact_id, verified_at=int(now.timestamp() * 1000))
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"fact {fact_id} not found")
    await State.pg.record_verification(team.team, fact_id, now, verified_by=team.key_id)


@router.get("/audit/{fact_id}", response_model=FactAudit)
async def knowledge_audit(fact_id: str, at: str | None = None, team: TeamContext = CurrentTeam):
    # Returns the full version chain; there is no point-in-time `at` parameter.
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
