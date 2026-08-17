"""Wisdom surface - governed rules: applicable-rule query, propose, deterministic
constraint check, universe promotion, and operator seeding (docs/contract.md).
"""
import datetime
import re
import uuid

from fastapi import APIRouter, HTTPException, Request, status

from ..auth import CurrentTeam, TeamContext
from ..config import settings
from ..models import (
    Accepted,
    AdvisoryProposal,
    CheckRequest,
    CheckResult,
    ConstraintProposal,
    Rule,
    SeedRule,
)
from ..runtime import State
from .common import _situation_params

# Severity ordering for wisdom.check verdict resolution (higher index = higher severity).
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

router = APIRouter(prefix="/v1/wisdom", tags=["wisdom"])


@router.get("/rules", response_model=list[Rule])
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


@router.post("/propose", response_model=Accepted, status_code=status.HTTP_202_ACCEPTED)
async def wisdom_propose(proposal: AdvisoryProposal | ConstraintProposal,
                         team: TeamContext = CurrentTeam):
    pid = str(uuid.uuid4())
    await State.falkor.proposal_set(pid, {"id": pid, "object_type": "rule", "status": "accepted"})
    await State.bus.publish(team.team, "wisdom.proposed", {
        "proposal_id": pid, "team": team.team, "object_type": "rule",
        "proposed_by": team.key_id, "body": proposal.model_dump(),
    })
    return Accepted(proposal_id=pid)


@router.post("/check", response_model=CheckResult)
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


@router.post("/promote/{rule_id}", status_code=status.HTTP_200_OK)
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


@router.post("/seed", status_code=status.HTTP_201_CREATED)
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
