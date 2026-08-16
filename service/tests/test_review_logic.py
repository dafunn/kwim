"""Tests for the human-review surface.

Covers:
  - claim semantics (atomic resolution - first wins, second -> None -> 409)
  - the proposal summary builder (fact/advisory/constraint, truncation)
  - mm-action auth (secret check, fail-closed when unset, team identifier validation)
  - provenance merge (extra_provenance doesn't override proposed_by/learned_from/supported_by)
  - regression: Gate._decide / Gate._split unchanged after the commit_proposal refactor

Auth/secret env come from conftest's superset:
  devkey -> acme (review-capable), otherkey -> otherteam (not), KWIM_MM_ACTION_SECRET=topsecret.
"""
import asyncio
import datetime

import pytest

from app.config import settings
from app.gate import Gate, summarize_proposal

_NOW = datetime.datetime(2026, 6, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)
DEV = {"Authorization": "Bearer devkey"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakePg:
    """In-memory pending_proposals, mirroring the atomic-claim semantics of
    PostgresStore.claim_pending (UPDATE ... WHERE resolved_at IS NULL RETURNING *)."""

    def __init__(self):
        self.pending: dict[str, dict] = {}

    async def insert_pending(self, team, row):
        self.pending[row["proposal_id"]] = {
            "proposal_id": row["proposal_id"], "object_type": row["object_type"],
            "proposed_by": row.get("proposed_by"), "body": row["body"],
            "bus_message": row["bus_message"], "created_at": _NOW, "team": team,
            "resolved_at": None, "resolution": None, "resolved_by": None,
            "resolved_via": None, "reject_reason": None,
        }

    async def list_pending(self, team, limit=50):
        return [dict(r) for r in self.pending.values()
                if r["resolved_at"] is None and r["team"] == team][:limit]

    async def get_pending(self, team, proposal_id):
        row = self.pending.get(proposal_id)
        return dict(row) if row else None

    async def claim_pending(self, team, proposal_id, resolution, resolved_by, resolved_via, reject_reason=None):
        row = self.pending.get(proposal_id)
        if row is None or row["resolved_at"] is not None:
            return None
        row["resolved_at"] = _NOW
        row["resolution"] = resolution
        row["resolved_by"] = resolved_by
        row["resolved_via"] = resolved_via
        row["reject_reason"] = reject_reason
        return dict(row)


class _FakeFalkor:
    def __init__(self):
        self.proposal_sets: list[dict] = []

    async def proposal_set(self, pid, doc):
        self.proposal_sets.append(doc)


class _FakeGate:
    def __init__(self):
        self.commit_calls: list[dict] = []
        self.retract_calls: list[dict] = []
        self.confirm_calls: list[dict] = []
        self.forget_calls: list[dict] = []
        self.forget_episodic_calls: list[dict] = []
        # objects: {object_id: (object_type, status)} - controls retract/confirm/forget outcomes
        self.objects: dict[str, tuple[str, str]] = {}

    async def commit_proposal(self, team, pid, ptype, body, proposal,
                              gate_decision="auto_committed", extra_provenance=None):
        self.commit_calls.append({
            "team": team, "pid": pid, "ptype": ptype, "body": body, "proposal": proposal,
            "gate_decision": gate_decision, "extra_provenance": extra_provenance,
        })
        return {"id": pid, "object_type": ptype, "status": "committed",
                "detail": "object_id=obj-1 seq=42", "object_id": "obj-1", "seq": 42}

    async def retract_object(self, team, object_id, by, via, object_type=None):
        self.retract_calls.append({"team": team, "object_id": object_id, "by": by, "via": via, "object_type": object_type})
        obj = self.objects.get(object_id)
        if obj is None:
            return {"status": "not_found"}
        resolved_type, current_status = obj
        if current_status == "retracted":
            return {"status": "already_retracted"}
        self.objects[object_id] = (resolved_type, "retracted")
        return {"status": "retracted", "object_id": object_id, "object_type": resolved_type, "seq": 99}

    async def confirm_object(self, team, object_id, by, via, object_type=None):
        self.confirm_calls.append({"team": team, "object_id": object_id, "by": by, "via": via, "object_type": object_type})
        obj = self.objects.get(object_id)
        if obj is None:
            return {"status": "not_found"}
        resolved_type, _ = obj
        return {"status": "confirmed", "object_id": object_id, "object_type": resolved_type, "seq": 100}

    async def forget_object(self, team, object_id, by, via, object_type=None):
        self.forget_calls.append({"team": team, "object_id": object_id, "by": by, "via": via, "object_type": object_type})
        obj = self.objects.get(object_id)
        if obj is None:
            return {"status": "not_found"}
        resolved_type, _ = obj
        del self.objects[object_id]  # gone from every store, not just retracted
        return {"status": "forgotten", "object_id": object_id, "type": resolved_type,
                "shared_skipped": [], "objects": 1, "commit_log_rows": 3, "episodic_events": 2}

    async def forget_episodics(self, team, episodic_ids, by, via):
        self.forget_episodic_calls.append({"team": team, "episodic_ids": list(episodic_ids), "by": by, "via": via})
        if not episodic_ids:
            return {"status": "no_delete", "episodic_events": 0, "shared_skipped": []}
        return {"status": "forgotten", "episodic_events": len(episodic_ids), "shared_skipped": []}


class _Review:
    def __init__(self, client, pg, falkor, gate):
        self.client = client
        self.pg = pg
        self.falkor = falkor
        self.gate = gate

    def seed(self, pid, object_type, body, proposed_by="agent1", team="acme"):
        asyncio.run(self.pg.insert_pending(team, {
            "proposal_id": pid, "object_type": object_type, "proposed_by": proposed_by,
            "body": body, "bus_message": {"proposal_id": pid, "team": team,
                                          "object_type": object_type, "proposed_by": proposed_by,
                                          "body": body},
        }))


@pytest.fixture
def review(client, monkeypatch):
    from app.main import State, app

    pg, falkor, gate = _FakePg(), _FakeFalkor(), _FakeGate()
    monkeypatch.setattr(State, "pg", pg, raising=False)
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    monkeypatch.setattr(app.state, "gate", gate, raising=False)
    return _Review(client, pg, falkor, gate)


# ---------------------------------------------------------------------------
# GET /pending - team-scoped read, no review key required
# ---------------------------------------------------------------------------

def test_pending_is_team_scoped(review):
    review.seed("p-pending-acme", "fact", {"statement": "m-fact", "fact_type": "observation"}, team="acme")
    review.seed("p-pending-other", "fact", {"statement": "other-fact", "fact_type": "observation"}, team="otherteam")

    # Non-review team key can read its own queue (200, not 403).
    r = review.client.get("/v1/review/pending", headers={"Authorization": "Bearer otherkey"})
    assert r.status_code == 200
    assert {p["proposal_id"] for p in r.json()} == {"p-pending-other"}

    r = review.client.get("/v1/review/pending", headers=DEV)
    assert r.status_code == 200
    assert {p["proposal_id"] for p in r.json()} == {"p-pending-acme"}


# ---------------------------------------------------------------------------
# Claim semantics - first wins, second -> 409, resolver recorded
# ---------------------------------------------------------------------------

def test_claim_semantics(review):
    review.seed("p-claim-1", "rule", {"rule_type": "constraint", "action_pattern": "post:.*", "verdict": "deny"})

    r1 = review.client.post("/v1/review/p-claim-1/approve", headers=DEV)
    assert r1.status_code == 200
    assert r1.json() == {"status": "committed", "object_id": "obj-1", "seq": 42}

    row = review.pg.pending["p-claim-1"]
    assert row["resolution"] == "approved"
    assert row["resolved_by"] == "devkey"
    assert row["resolved_via"] == "api"

    r2 = review.client.post("/v1/review/p-claim-1/approve", headers=DEV)
    assert r2.status_code == 409

    r3 = review.client.post("/v1/review/unknown-id/approve", headers=DEV)
    assert r3.status_code == 404

    # Capability gate: non-review key -> 403, never reaches the store.
    review.seed("p-claim-2", "fact", {"statement": "x", "fact_type": "observation"})
    r4 = review.client.post("/v1/review/p-claim-2/approve", headers={"Authorization": "Bearer otherkey"})
    assert r4.status_code == 403
    assert review.pg.pending["p-claim-2"]["resolved_at"] is None


# ---------------------------------------------------------------------------
# Reject flow - resolution + falkor refresh
# ---------------------------------------------------------------------------

def test_reject_flow(review):
    review.seed("p-reject-1", "fact", {"statement": "y", "fact_type": "observation"})

    r = review.client.post("/v1/review/p-reject-1/reject", json={"reason": "duplicate"}, headers=DEV)
    assert r.status_code == 200
    assert r.json() == {"status": "rejected"}

    row = review.pg.pending["p-reject-1"]
    assert row["resolution"] == "rejected"
    assert row["reject_reason"] == "duplicate"

    last_set = review.falkor.proposal_sets[-1]
    assert last_set["status"] == "rejected"
    assert last_set["detail"] == "duplicate"

    r2 = review.client.post("/v1/review/p-reject-1/reject", headers=DEV)
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def test_summary_builder():
    assert summarize_proposal("fact", {"statement": "Trend A recurs", "fact_type": "observation"}) == "Trend A recurs"
    assert summarize_proposal("rule", {"rule_type": "advisory", "approach": "Lead with trend A"}) == "Lead with trend A"
    assert summarize_proposal("rule", {"rule_type": "constraint", "action_pattern": "post:.*",
                                       "verdict": "deny"}) == "post:.* -> deny"

    summary = summarize_proposal("fact", {"statement": "x" * 600, "fact_type": "observation"})
    assert len(summary) == 500
    assert summary.endswith("...")


# ---------------------------------------------------------------------------
# mm-action auth
# ---------------------------------------------------------------------------

def test_mm_action_auth_and_flows(review):
    review.seed("p-mm-1", "fact", {"statement": "z", "fact_type": "observation"})

    def mm(**context):
        return review.client.post("/v1/review/mm-action", json={"user_name": context.pop("user_name", "alice"),
                                                                 "context": context})

    # Missing secret -> 403.
    assert mm(proposal_id="p-mm-1", team="acme", decision="approve").status_code == 403

    # Wrong secret -> 403, proposal untouched.
    assert mm(proposal_id="p-mm-1", team="acme", decision="approve", secret="wrong").status_code == 403
    assert review.pg.pending["p-mm-1"]["resolved_at"] is None

    # Invalid team identifier -> 403.
    assert mm(proposal_id="p-mm-1", team="bad-team; --", decision="approve", secret="topsecret").status_code == 403

    # Unset secret env -> always-reject (fail-closed), even with an empty provided secret.
    object.__setattr__(settings, "mm_action_secret", "")
    try:
        assert mm(proposal_id="p-mm-1", team="acme", decision="approve", secret="").status_code == 403
    finally:
        object.__setattr__(settings, "mm_action_secret", "topsecret")

    # Correct secret -> approve; reviewer identity = user_name, via=mattermost.
    r = mm(proposal_id="p-mm-1", team="acme", decision="approve", secret="topsecret")
    assert r.status_code == 200
    assert "obj-1" in r.json()["update"]["message"] and "42" in r.json()["update"]["message"]

    row = review.pg.pending["p-mm-1"]
    assert row["resolved_by"] == "alice"
    assert row["resolved_via"] == "mattermost"

    last_call = review.gate.commit_calls[-1]
    assert last_call["extra_provenance"]["approved_by"] == "alice"
    assert last_call["extra_provenance"]["approved_via"] == "mattermost"

    # Double-click -> already resolved -> warning update, not an error.
    r = mm(user_name="bob", proposal_id="p-mm-1", team="acme", decision="approve", secret="topsecret")
    assert r.status_code == 200
    assert r.json()["update"]["message"].startswith(":warning:")


def test_mm_action_forget_pending(review):
    review.seed("p-forget-1", "fact",
                {"statement": "garbage", "fact_type": "observation", "evidence": ["ep-aaa", "ep-bbb"]})
    r = review.client.post("/v1/review/mm-action", json={
        "user_name": "carol",
        "context": {"proposal_id": "p-forget-1", "team": "acme", "decision": "forget", "secret": "topsecret"},
    })
    assert r.status_code == 200
    msg = r.json()["update"]["message"]
    assert msg.startswith(":fire:")
    # proposal rejected and its source episodics forwarded to the inline forget path
    assert review.pg.pending["p-forget-1"]["resolution"] == "rejected"
    last = review.gate.forget_episodic_calls[-1]
    assert last["episodic_ids"] == ["ep-aaa", "ep-bbb"]
    assert "2 source episodic" in msg


# ---------------------------------------------------------------------------
# committed-action - Confirm/Retract on committed objects
# ---------------------------------------------------------------------------

def test_committed_action_flows(review):
    review.gate.objects["fact-1"] = ("fact", "current")
    review.gate.objects["fact-2"] = ("fact", "current")

    def ca(**context):
        return review.client.post("/v1/review/committed-action",
                                  json={"user_name": context.pop("user_name", "alice"), "context": context})

    # Missing/wrong secret + invalid team -> 403.
    assert ca(object_id="fact-1", object_type="fact", team="acme", decision="confirm").status_code == 403
    assert ca(object_id="fact-1", object_type="fact", team="bad-team; --",
              decision="confirm", secret="topsecret").status_code == 403

    # Invalid decision -> 400.
    assert ca(object_id="fact-1", object_type="fact", team="acme",
              decision="approve", secret="topsecret").status_code == 400

    # Confirm -> 200, calls gate.confirm_object.
    r = ca(object_id="fact-1", object_type="fact", team="acme", decision="confirm", secret="topsecret")
    assert r.status_code == 200
    assert "fact-1" in r.json()["update"]["message"]
    last_confirm = review.gate.confirm_calls[-1]
    assert last_confirm["by"] == "alice"
    assert last_confirm["via"] == "mattermost"
    assert last_confirm["object_type"] == "fact"

    # Retract -> 200; retract again -> already_retracted warning.
    r = ca(user_name="bob", object_id="fact-2", object_type="fact", team="acme", decision="retract", secret="topsecret")
    assert r.status_code == 200
    assert "fact-2" in r.json()["update"]["message"]
    r = ca(user_name="bob", object_id="fact-2", object_type="fact", team="acme", decision="retract", secret="topsecret")
    assert r.status_code == 200
    assert r.json()["update"]["message"].startswith(":warning:")

    # Unknown object -> not_found warning.
    r = ca(object_id="no-such-object", object_type="fact", team="acme", decision="retract", secret="topsecret")
    assert r.status_code == 200
    assert r.json()["update"]["message"].startswith(":warning:")

    # Forget -> inline hard-delete via gate.forget_object; object gone from memory.
    review.gate.objects["fact-forget"] = ("fact", "current")
    r = ca(user_name="carol", object_id="fact-forget", object_type="fact", team="acme", decision="forget", secret="topsecret")
    assert r.status_code == 200
    msg = r.json()["update"]["message"]
    assert msg.startswith(":fire:")
    assert "fact-forget" not in review.gate.objects  # removed entirely, not just retracted
    last_forget = review.gate.forget_calls[-1]
    assert last_forget["by"] == "carol"
    assert last_forget["object_type"] == "fact"


# ---------------------------------------------------------------------------
# REST retract - POST /v1/review/committed/{object_id}/retract
# ---------------------------------------------------------------------------

def test_rest_retract(review):
    review.gate.objects["fact-3"] = ("fact", "current")

    # Non-review key -> 403.
    r = review.client.post("/v1/review/committed/fact-3/retract", headers={"Authorization": "Bearer otherkey"})
    assert r.status_code == 403

    # Review key, known object -> 200.
    r = review.client.post("/v1/review/committed/fact-3/retract", headers=DEV)
    assert r.status_code == 200
    assert r.json()["status"] == "retracted"
    assert r.json()["object_id"] == "fact-3"
    last_retract = review.gate.retract_calls[-1]
    assert last_retract["by"] == "devkey"
    assert last_retract["via"] == "api"

    # Already retracted -> 409.
    r = review.client.post("/v1/review/committed/fact-3/retract", headers=DEV)
    assert r.status_code == 409

    # Unknown object -> 404.
    r = review.client.post("/v1/review/committed/no-such-object/retract", headers=DEV)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Provenance merge - extra_provenance doesn't override proposer attribution
# ---------------------------------------------------------------------------

def test_provenance_merge():
    proposal = {"proposal_id": "p-prov-1", "team": "acme", "object_type": "fact",
                "proposed_by": "agent1",
                "body": {"statement": "s", "fact_type": "observation", "evidence": ["ev1", "ev2"]}}
    payload, provenance = Gate._split("fact", proposal["body"], proposal)
    merged = {**provenance, **{"approved_by": "devkey", "approved_via": "api"}}

    assert merged["proposed_by"] == "agent1"
    assert merged["supported_by"] == ["ev1", "ev2"]
    assert merged["approved_by"] == "devkey"
    assert merged["approved_via"] == "api"
    # about is a payload field, not provenance.
    assert payload.get("about") == []
    assert "about" not in provenance

    prop_with_about = {"proposal_id": "p-about-1", "team": "acme", "object_type": "fact",
                       "proposed_by": "agent2",
                       "body": {"statement": "s2", "fact_type": "observation",
                                "about": ["foo"], "evidence": []}}
    p2, prov2 = Gate._split("fact", prop_with_about["body"], prop_with_about)
    assert p2.get("about") == ["foo"]
    assert "about" not in prov2

    # decay_class fallback (observation -> slow), override, and invalid override.
    assert payload.get("decay_class") == "slow"

    prop_decay = {"proposal_id": "p-decay-1", "team": "acme", "object_type": "fact", "proposed_by": "agent3",
                  "body": {"statement": "s3", "fact_type": "observation", "decay_class": "fast", "evidence": []}}
    p3, _ = Gate._split("fact", prop_decay["body"], prop_decay)
    assert p3.get("decay_class") == "fast"

    prop_bad = {"proposal_id": "p-decay-2", "team": "acme", "object_type": "fact", "proposed_by": "agent4",
                "body": {"statement": "s4", "fact_type": "entity_attribute", "decay_class": "bogus", "evidence": []}}
    p4, _ = Gate._split("fact", prop_bad["body"], prop_bad)
    assert p4.get("decay_class") == "permanent"

    rule_proposal = {"proposal_id": "p-prov-2", "team": "acme", "object_type": "rule", "proposed_by": "agent1",
                     "body": {"rule_type": "advisory", "situation": {}, "approach": "x", "evidence": ["ev3"]}}
    _, rule_provenance = Gate._split("rule", rule_proposal["body"], rule_proposal)
    merged_rule = {**rule_provenance, **{"approved_by": "devkey", "approved_via": "mattermost"}}
    assert merged_rule["learned_from"] == ["ev3"]
    assert merged_rule["proposed_by"] == "agent1"


# ---------------------------------------------------------------------------
# Regression - Gate._decide unchanged after the commit_proposal refactor
# ---------------------------------------------------------------------------

def test_decide_regression():
    gate = Gate(None, None, None)  # pure-logic methods only - no backends touched

    assert gate._decide("fact", {}) == ("commit", "auto_committed")
    assert gate._decide("rule", {"rule_type": "constraint"}) == ("review", "human_approved")

    threshold = settings.gate_auto_commit_threshold
    assert gate._decide("rule", {"rule_type": "advisory"}, threshold - 1) == ("review", "human_approved")
    assert gate._decide("rule", {"rule_type": "advisory"}, threshold) == ("commit", "auto_committed")
