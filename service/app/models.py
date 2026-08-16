"""Request/response models = the typed contract (docs/contract.md).

These drive FastAPI's OpenAPI, so the wire contract is generated from here.
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Knowledge ---------------------------------------------------------------

class FactProposal(BaseModel):
    statement: str
    fact_type: str
    evidence: list[str] = Field(default_factory=list)      # episodic_event ids
    supersedes: str | None = None                          # fact id
    about: list[str] = Field(default_factory=list)         # entity refs
    source_kind: Literal["agent_proposal", "repo_sync", "distiller"] = "agent_proposal"
    decay_class: str | None = None                         # override; validated at gate


class Fact(BaseModel):
    id: str
    statement: str
    fact_type: str
    status: Literal["current", "superseded", "retracted"]
    created_at: str
    about: list[str] = Field(default_factory=list)
    decay_class: str = "slow"
    as_of: str = ""
    freshness: str = "fresh"
    last_verified_at: str | None = None
    source_kind: str | None = None


class FactMatch(Fact):
    """A fact returned by semantic search (knowledge/search).

    `score` is a cosine distance - lower = closer, identical vector -> 0.0 - the same
    convention as SemanticItem.score, so callers rank ascending.
    """
    score: float


class FactProvenance(BaseModel):
    proposed_by: str | None = None
    supported_by: list[str] = Field(default_factory=list)   # episodic_event_id refs
    supersedes: str | None = None                           # fact id this one replaced


class FactDetail(BaseModel):                                # GET /knowledge/facts/{id}
    fact: Fact
    provenance: FactProvenance


class AuditVersion(BaseModel):                              # one node in the audit chain
    id: str
    statement: str
    status: str
    created_at: str | None = None
    commit_seq: int | None = None
    proposed_by: str | None = None
    supported_by: list[str] = Field(default_factory=list)   # episodic_event_id refs


class FactAudit(BaseModel):                                 # GET /knowledge/audit/{id}
    fact_id: str
    chain: list[AuditVersion]                               # version history, newest-first


# --- Wisdom ------------------------------------------------------------------

class AdvisoryProposal(BaseModel):
    rule_type: Literal["advisory"] = "advisory"
    situation: dict[str, Any]                              # matchable: team-defined keys, e.g. task_type, platform
    approach: str
    evidence: list[str] = Field(default_factory=list)
    reinforces: str | None = None                          # existing rule id - accrue evidence, don't create
    source_kind: Literal["agent_proposal", "repo_sync", "distiller"] = "agent_proposal"


class ConstraintProposal(BaseModel):
    rule_type: Literal["constraint"] = "constraint"
    action_pattern: str
    verdict: Literal["allow", "deny", "escalate"]
    authority: str
    severity: str
    check_tier: Literal["deterministic", "classifier"]
    source_kind: Literal["agent_proposal", "repo_sync", "distiller"] = "agent_proposal"


class Rule(BaseModel):
    id: str
    rule_type: Literal["advisory", "constraint"]
    situation: dict[str, Any] | None = None
    approach: str | None = None
    evidence_count: int = 0
    status: Literal["pending", "approved", "deprecated", "retracted"]
    scope: Literal["team", "universe"] = "team"            # team-local or promoted to the shared universe graph
    source: Literal["team", "universe"] = "team"           # which graph this row came from (read path only)
    action_pattern: str | None = None                      # constraint only
    verdict: str | None = None
    authority: str | None = None
    severity: str | None = None
    check_tier: str | None = None


class SeedRule(BaseModel):
    """Full rule payload for POST /v1/wisdom/seed (operator-gated direct commit).

    Unlike the `Rule` read-model, this carries the constraint enforcement fields
    (action_pattern/verdict/authority/severity/check_tier) so a seeded
    constraint isn't stripped to an empty husk.
    """
    id: str
    rule_type: Literal["advisory", "constraint"] = "constraint"
    situation: dict[str, Any] | None = None
    approach: str | None = None                            # advisory
    evidence_count: int = 0
    action_pattern: str | None = None                      # constraint
    verdict: str | None = None
    authority: str | None = None
    severity: str | None = None
    check_tier: str | None = None


class CheckRequest(BaseModel):
    action: dict[str, Any]                                 # the action about to be taken


class CheckResult(BaseModel):
    verdict: Literal["allow", "deny", "escalate"]
    matched_rule: str | None = None
    reason: str | None = None
    check_tier: Literal["deterministic", "classifier"] | None = None


# --- Memory ------------------------------------------------------------------

class EpisodicEvent(BaseModel):
    agent_id: str
    session_id: str
    event_type: str
    event_data: dict[str, Any] = Field(default_factory=dict)


class EpisodicEventOut(BaseModel):
    id: str
    agent_id: str
    session_id: str
    event_type: str
    event_data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str


class EpisodicCursor(BaseModel):
    ts: str
    id: str


class EpisodicWindow(BaseModel):
    events: list[EpisodicEventOut]
    next_cursor: EpisodicCursor | None = None


class WorkingWrite(BaseModel):
    value: Any
    ttl_seconds: int | None = None


class SemanticItem(BaseModel):
    id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticWrite(BaseModel):
    id: str | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Shared async / status ---------------------------------------------------

class Accepted(BaseModel):
    proposal_id: str
    status: Literal["accepted"] = "accepted"


class EventAccepted(BaseModel):
    event_id: str


class ProposalStatus(BaseModel):
    id: str
    object_type: Literal["fact", "rule"]
    status: Literal["accepted", "committed", "rejected", "pending_review"]
    detail: str | None = None


# --- Code graph ---------------------------------------------------------------

class CodeFunction(BaseModel):
    id: str
    name: str
    signature: str = ""
    summary: str = ""
    repo: str = ""
    path: str = ""
    score: float | None = None              # vector distance (lower = closer), search only
    path_confidence: float | None = None    # weakest edge along a trace path


class CodeArchitecture(BaseModel):
    communities: list[dict[str, Any]] = Field(default_factory=list)


class CodeChange(BaseModel):
    repo: str
    path: str
    commit: str


# --- Human review ------------------------------------------------------------

class PendingProposal(BaseModel):
    proposal_id: str
    object_type: Literal["fact", "rule"]
    proposed_by: str | None
    created_at: datetime
    summary: str
    body: dict[str, Any]


class RejectRequest(BaseModel):
    reason: str | None = None
