"""Proposal status - the async write receipt handed back by knowledge/wisdom propose."""
from fastapi import APIRouter, HTTPException, status

from ..auth import CurrentTeam, TeamContext
from ..models import ProposalStatus
from ..runtime import State

router = APIRouter(prefix="/v1/proposals", tags=["proposals"])


@router.get("/{proposal_id}", response_model=ProposalStatus)
async def proposal_status(proposal_id: str, team: TeamContext = CurrentTeam):
    doc = await State.falkor.proposal_get(proposal_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal id")
    return ProposalStatus(**doc)
