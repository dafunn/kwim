"""Tests for the gate verify-before-commit checks

Covers:
  1. Evidence counting (dedup + NELL-style distinct-session count).
  2. Screen routing (dup reject / near review / clear commit / supersedes-excluded /
     embedder-down fail-open).
  3. Reinforce path uses deduped-valid count (dedup-and-proceed).
  4. Idempotent re-distill (stable object_id short-circuit).
  5. Regression: constraint -> review, fact-with-no-similar -> commit unchanged.
  6. retract_object / confirm_object.

Gate tunables (KWIM_GATE_VERIFY / DUP_DIST / REVIEW_DIST) come from conftest's env
superset.
"""
from app.config import settings
from app.gate import Gate


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakePg:
    """Postgres fake: evidence_meta returns only rows for known ids."""

    def __init__(self, known: dict[str, str]):
        self.known = known                       # {event_id: session_id}
        self.commit_appended: list[dict] = []
        self.pending_inserted: list[dict] = []

    async def evidence_meta(self, team, ids):
        return [{"id": eid, "session_id": self.known[eid], "agent_id": "agent1"}
                for eid in ids if eid in self.known]

    async def append_commit(self, team, row):
        self.commit_appended.append(row)
        return len(self.commit_appended)

    async def insert_pending(self, team, row):
        self.pending_inserted.append(row)


class _FakeFalkor:
    def __init__(self, neighbors=None, objects=None):
        self._neighbors = neighbors or []        # [{id, statement, status, score}]
        self.proposal_sets: list[dict] = []
        self.materialized_facts: list[dict] = []
        self.reinforced: list[dict] = []
        self.objects = objects or {}             # {object_id: (object_type, status)}
        self.retracted: list[dict] = []
        self.confirmed: list[dict] = []

    async def query_similar_facts(self, team, vector, k=5, about=None, fact_type=None):
        neighbors = self._neighbors
        if about and fact_type:
            # Mirror the ALL-overlap rule: candidate.about must contain every proposal.about ref.
            neighbors = [
                n for n in neighbors
                if n.get("fact_type") == fact_type
                and all(t in (n.get("about") or []) for t in about)
            ]
        return neighbors[:k]

    async def proposal_set(self, pid, doc):
        self.proposal_sets.append(doc)

    async def materialize_fact(self, team, fact, provenance, graph_name=None, embedding=None):
        self.materialized_facts.append({"fact": fact, "embedding": embedding})

    async def materialize_rule(self, team, rule, provenance, graph_name=None):
        pass

    async def reinforce_rule(self, team, rule_id, new_ev, seq, graph_name=None):
        self.reinforced.append({"rule_id": rule_id, "evidence": new_ev, "seq": seq})

    async def get_rule(self, team, rule_id):
        return {"id": rule_id, "status": "approved"}

    async def proposal_get(self, pid):
        return None

    async def insert_pending(self, team, row):
        pass

    async def find_object(self, team, object_id, object_type=None):
        obj = self.objects.get(object_id)
        if obj is None:
            return None
        if object_type and obj[0] != object_type:
            return None
        return obj

    async def retract_object(self, team, object_type, object_id, graph_name=None):
        self.retracted.append({"team": team, "object_type": object_type, "object_id": object_id})
        if object_id in self.objects:
            self.objects[object_id] = (self.objects[object_id][0], "retracted")

    async def confirm_object(self, team, object_type, object_id, by, at, graph_name=None):
        self.confirmed.append({"team": team, "object_type": object_type, "object_id": object_id, "by": by, "at": at})


class _FakeEmbedder:
    def __init__(self, vec=None, raises=False):
        self._vec = vec or [0.1] * 384
        self._raises = raises
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(texts)
        if self._raises:
            raise RuntimeError("embedder down")
        return [self._vec for _ in texts]


def _make_gate(pg=None, falkor=None, embedder=None):
    return Gate(pg or _FakePg({}), falkor or _FakeFalkor(), None, embedder)


def _proposal(ptype="fact", body=None, pid="p-test", team="acme", proposed_by="agent1"):
    return {"proposal_id": pid, "team": team, "object_type": ptype,
            "proposed_by": proposed_by, "body": body or {}}


# UUID-shaped ids (episodic_events.id is uuid; the gate validates that).
EV1 = "11111111-1111-1111-1111-111111111111"
EV2 = "22222222-2222-2222-2222-222222222222"
EV3 = "33333333-3333-3333-3333-333333333333"
EV4 = "44444444-4444-4444-4444-444444444444"
EV5 = "55555555-5555-5555-5555-555555555555"
EVA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
EVB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
EVC = "cccccccc-cccc-cccc-cccc-cccccccccccc"
EV_UNK = "99999999-9999-9999-9999-999999999999"


# ---------------------------------------------------------------------------
# 1. Evidence counting
# ---------------------------------------------------------------------------

async def test_evidence_below_session_threshold_reviews():
    # 5 raw refs, 2 distinct sessions -> below threshold (default 3) -> review.
    known = {EV1: "s1", EV2: "s1", EV3: "s1", EV4: "s2", EV5: "s2"}
    gate = _make_gate(pg=_FakePg(known))
    valid_ids, session_count, problem = await gate._check_evidence(
        "acme", {"evidence": [EV1, EV2, EV3, EV4, EV5]})
    assert len(valid_ids) == 5
    assert session_count == 2
    assert problem is None
    assert gate._decide("rule", {"rule_type": "advisory"}, session_count)[0] == "review"


async def test_evidence_meets_session_threshold_commits():
    known = {EVA: "sA", EVB: "sB", EVC: "sC"}
    gate = _make_gate(pg=_FakePg(known))
    _, sc, prob = await gate._check_evidence("acme", {"evidence": [EVA, EVB, EVC]})
    assert sc == 3
    assert prob is None
    assert gate._decide("rule", {"rule_type": "advisory"}, sc)[0] == "commit"


async def test_evidence_duplicates_collapse_before_counting():
    gate = _make_gate(pg=_FakePg({EV1: "s1", EV2: "s2"}))
    valid, sc, _ = await gate._check_evidence("acme", {"evidence": [EV1, EV1, EV2, EV1]})  # 4 raw, 2 unique
    assert len(valid) == 2
    assert sc == 2


async def test_evidence_unknown_and_malformed_ids_flagged_no_crash():
    # Malformed id must not crash the ::uuid[] cast
    gate = _make_gate(pg=_FakePg({EV1: "s1"}))
    valid, sc, prob = await gate._check_evidence(
        "acme", {"evidence": [EV1, EV_UNK, "9f007edb"]})  # known, unknown-uuid, malformed
    assert valid == [EV1]
    assert prob is not None and EV_UNK in prob
    assert "9f007edb" in prob
    assert sc == 1


async def test_evidence_empty_is_noop():
    gate = _make_gate(pg=_FakePg({}))
    valid, sc, prob = await gate._check_evidence("acme", {})
    assert prob is None
    assert sc == 0


# ---------------------------------------------------------------------------
# 2. Screen routing
# ---------------------------------------------------------------------------

async def test_screen_dup_rejects():
    # d=0.01 -> dup reject (<= 0.05).
    falkor = _FakeFalkor(neighbors=[{"id": "fact-A", "statement": "x", "status": "current", "score": 0.01}])
    gate = _make_gate(falkor=falkor, embedder=_FakeEmbedder())
    doc, vec = await gate._screen_fact("acme", "p1", "fact",
                                       {"statement": "near-identical statement"}, _proposal())
    assert doc is not None
    assert doc["status"] == "rejected"
    assert "fact-A" in doc["detail"]
    assert vec is None
    assert len(falkor.proposal_sets) == 1


async def test_screen_near_reviews():
    # d=0.15 -> review-band (0.05 < d <= 0.25).
    falkor = _FakeFalkor(neighbors=[{"id": "fact-B", "statement": "y", "status": "current", "score": 0.15}])
    gate = _make_gate(pg=_FakePg({}), falkor=falkor, embedder=_FakeEmbedder())
    doc, vec = await gate._screen_fact("acme", "p2", "fact",
                                       {"statement": "similar statement"}, _proposal(pid="p2"))
    assert doc is not None
    assert doc["status"] == "pending_review"
    assert "fact-B" in doc["detail"]
    assert vec is None


async def test_screen_far_commits_with_embedding():
    # d=0.40 -> clear to commit (> 0.25).
    falkor = _FakeFalkor(neighbors=[{"id": "fact-C", "statement": "z", "status": "current", "score": 0.40}])
    gate = _make_gate(falkor=falkor, embedder=_FakeEmbedder([0.5] * 384))
    doc, vec = await gate._screen_fact("acme", "p3", "fact",
                                       {"statement": "different statement"}, _proposal(pid="p3"))
    assert doc is None
    assert len(vec or []) == 384


async def test_screen_supersedes_target_excluded():
    falkor = _FakeFalkor(neighbors=[{"id": "fact-OLD", "statement": "old", "status": "current", "score": 0.01}])
    gate = _make_gate(pg=_FakePg({}), falkor=falkor, embedder=_FakeEmbedder())
    doc, vec = await gate._screen_fact(
        "acme", "p4", "fact",
        {"statement": "superseding statement", "supersedes": "fact-OLD"}, _proposal(pid="p4"))
    assert doc is None
    assert len(vec or []) == 384


async def test_screen_embedder_down_fails_open():
    gate = _make_gate(falkor=_FakeFalkor(), embedder=_FakeEmbedder(raises=True))
    doc, vec = await gate._screen_fact("acme", "p5", "fact",
                                       {"statement": "any statement"}, _proposal(pid="p5"))
    assert doc is None
    assert vec is None


async def test_screen_no_neighbors_commits():
    gate = _make_gate(falkor=_FakeFalkor(neighbors=[]), embedder=_FakeEmbedder([0.3] * 384))
    doc, vec = await gate._screen_fact("acme", "p6", "fact",
                                       {"statement": "novel fact"}, _proposal(pid="p6"))
    assert doc is None
    assert len(vec or []) == 384


# ---------------------------------------------------------------------------
# 2b. Entity-scoped screening regression
# ---------------------------------------------------------------------------

# Realistic about sets share broad category refs; the screen must require ALL
# proposal refs to be present in the candidate (not ANY shared ref).
_HOSTA_ABOUT = ["host-a", "group-x", "site-a"]
_HOSTB_ABOUT = ["host-b", "group-x", "site-a"]


async def test_screen_entity_scoped_different_entities_both_commit():
    # Distance ~0.018 is deep inside the reject range, but the candidate is about
    # host-a while the proposal is about host-b -> not a duplicate candidate.
    falkor = _FakeFalkor(neighbors=[{
        "id": "fact-hosta-inv",
        "statement": "host-a is a group-x host at 10.0.0.10.",
        "status": "current",
        "score": 0.018,
        "fact_type": "host_inventory",
        "about": _HOSTA_ABOUT,
    }])
    gate = _make_gate(falkor=falkor, embedder=_FakeEmbedder())
    doc, vec = await gate._screen_fact(
        "acme", "p7", "fact",
        {
            "statement": "host-b is a group-x host at 10.0.0.11.",
            "fact_type": "host_inventory",
            "about": _HOSTB_ABOUT,
        },
        _proposal(pid="p7"),
    )
    assert doc is None
    assert vec is not None


async def test_screen_different_fact_type_same_entity_commits():
    # Same entity, different fact_type -> not a duplicate candidate.
    falkor = _FakeFalkor(neighbors=[{
        "id": "fact-hosta-inv",
        "statement": "host-a is a group-x host at 10.0.0.10.",
        "status": "current",
        "score": 0.2486,
        "fact_type": "host_inventory",
        "about": _HOSTA_ABOUT,
    }])
    gate = _make_gate(falkor=falkor, embedder=_FakeEmbedder())
    doc, vec = await gate._screen_fact(
        "acme", "p8", "fact",
        {
            "statement": "host-a is also known as 10.0.0.10.",
            "fact_type": "host_alias",
            "about": _HOSTA_ABOUT,
        },
        _proposal(pid="p8"),
    )
    assert doc is None
    assert vec is not None


async def test_screen_true_duplicate_still_rejected():
    falkor = _FakeFalkor(neighbors=[{
        "id": "fact-hosta-inv",
        "statement": "host-a is a group-x host at 10.0.0.10.",
        "status": "current",
        "score": 0.01,
        "fact_type": "host_inventory",
        "about": _HOSTA_ABOUT,
    }])
    gate = _make_gate(falkor=falkor, embedder=_FakeEmbedder())
    doc, vec = await gate._screen_fact(
        "acme", "p9", "fact",
        {
            "statement": "host-a is a group-x host at 10.0.0.10.",
            "fact_type": "host_inventory",
            "about": _HOSTA_ABOUT,
        },
        _proposal(pid="p9"),
    )
    assert doc is not None
    assert doc["status"] == "rejected"
    assert "fact-hosta-inv" in doc["detail"]
    assert vec is None


async def test_screen_true_near_match_still_reviews():
    falkor = _FakeFalkor(neighbors=[{
        "id": "fact-hosta-inv",
        "statement": "host-a is a group-x host at 10.0.0.10.",
        "status": "current",
        "score": 0.15,
        "fact_type": "host_inventory",
        "about": _HOSTA_ABOUT,
    }])
    gate = _make_gate(pg=_FakePg({}), falkor=falkor, embedder=_FakeEmbedder())
    doc, vec = await gate._screen_fact(
        "acme", "p10", "fact",
        {
            "statement": "host-a is a group-x host at 10.0.0.10, managed centrally.",
            "fact_type": "host_inventory",
            "about": _HOSTA_ABOUT,
        },
        _proposal(pid="p10"),
    )
    assert doc is not None
    assert doc["status"] == "pending_review"
    assert "fact-hosta-inv" in doc["detail"]
    assert vec is None


# ---------------------------------------------------------------------------
# 3. Reinforce path: deduped-valid count (dedup-and-proceed)
# ---------------------------------------------------------------------------

async def test_reinforce_uses_deduped_valid_count():
    R1 = "d1111111-1111-1111-1111-111111111111"
    R2 = "d2222222-2222-2222-2222-222222222222"
    R3 = "d3333333-3333-3333-3333-333333333333"
    pg = _FakePg({R1: "s1", R3: "s3"})
    falkor = _FakeFalkor()
    gate = _make_gate(pg=pg, falkor=falkor)

    raw_evidence = [R1, R1, R2, R3, "bad-id"]  # dedup + drop unknown/malformed -> [R1, R3]
    body = {"rule_type": "advisory", "reinforces": "rule-X", "evidence": raw_evidence}
    doc = await gate._reinforce("acme", "p-reinforce", _proposal(ptype="rule", body=body, pid="p-reinforce"), body)

    assert doc["status"] == "committed"
    assert "+2" in doc["detail"]  # n=valid count (2, not 5)
    assert sorted(pg.commit_appended[-1]["provenance"]["learned_from"]) == sorted([R1, R3])
    assert sorted(falkor.reinforced[-1]["evidence"]) == sorted([R1, R3])


async def test_reinforce_all_unknown_still_commits_noop():
    gate = _make_gate(pg=_FakePg({}), falkor=_FakeFalkor())
    body = {"rule_type": "advisory", "reinforces": "rule-Y", "evidence": ["fake1", "fake2"]}
    doc = await gate._reinforce("acme", "p-none", _proposal(ptype="rule", body=body, pid="p-none"), body)
    assert doc["status"] == "committed"
    assert "+0" in doc["detail"]


# ---------------------------------------------------------------------------
# 3b. Idempotent re-distill - a stable object_id whose node exists is a no-op
# ---------------------------------------------------------------------------

_STABLE_BODY = {"object_id": "stable-hub-1", "statement": "X is a call hub - 7 functions call it",
                "fact_type": "code_hub", "source_kind": "repo_sync", "evidence": []}


async def test_redistill_existing_stable_id_is_noop():
    fk = _FakeFalkor(objects={"stable-hub-1": ("fact", "current")})
    doc = await _make_gate(falkor=fk, embedder=_FakeEmbedder()).handle(_proposal(body=_STABLE_BODY))
    assert doc["status"] == "noop"
    assert len(fk.materialized_facts) == 0


async def test_redistill_retracted_stable_id_not_resurrected():
    fk = _FakeFalkor(objects={"stable-hub-1": ("fact", "retracted")})
    doc = await _make_gate(falkor=fk, embedder=_FakeEmbedder()).handle(_proposal(body=_STABLE_BODY))
    assert doc["status"] == "noop"
    assert len(fk.materialized_facts) == 0


async def test_redistill_new_stable_id_commits():
    fk = _FakeFalkor()
    doc = await _make_gate(falkor=fk, embedder=_FakeEmbedder()).handle(
        _proposal(body={**_STABLE_BODY, "object_id": "stable-new"}))
    assert doc["status"] != "noop"
    assert len(fk.materialized_facts) == 1


async def test_redistill_no_object_id_never_short_circuits():
    fk = _FakeFalkor()
    doc = await _make_gate(falkor=fk, embedder=_FakeEmbedder()).handle(
        _proposal(body={"statement": "ordinary", "fact_type": "observation", "evidence": []}))
    assert doc["status"] != "noop"


async def test_redistill_stable_id_bypasses_screen():
    # Even a colliding neighbor (score 0.0) doesn't stop a stable-id fact committing.
    fk = _FakeFalkor(neighbors=[{"id": "other", "score": 0.0}])
    await _make_gate(falkor=fk, embedder=_FakeEmbedder()).handle(
        _proposal(body={**_STABLE_BODY, "object_id": "stable-bypass"}))
    assert len(fk.materialized_facts) == 1


async def test_non_stable_fact_still_screened():
    fk = _FakeFalkor(neighbors=[{"id": "other", "score": 0.0}])
    await _make_gate(falkor=fk, embedder=_FakeEmbedder()).handle(
        _proposal(body={"statement": "z", "fact_type": "observation", "evidence": []}))
    assert len(fk.materialized_facts) == 0


# ---------------------------------------------------------------------------
# 4. Regression: _decide table unchanged
# ---------------------------------------------------------------------------

def test_decide_regression():
    gate = Gate(None, None, None)

    assert gate._decide("rule", {"rule_type": "constraint"}, 999) == ("review", "human_approved")
    assert gate._decide("fact", {}) == ("commit", "auto_committed")

    threshold = settings.gate_auto_commit_threshold
    assert gate._decide("rule", {"rule_type": "advisory"}, threshold - 1) == ("review", "human_approved")
    assert gate._decide("rule", {"rule_type": "advisory"}, threshold) == ("commit", "auto_committed")
    assert gate._decide("rule", {"rule_type": "advisory"}, threshold + 1) == ("commit", "auto_committed")
    assert gate._decide("rule", {"rule_type": "advisory"}) == ("review", "human_approved")  # default sc=0


# ---------------------------------------------------------------------------
# 5. retract_object / confirm_object
# ---------------------------------------------------------------------------

async def test_retract_object_lifecycle():
    pg = _FakePg({})
    falkor = _FakeFalkor(objects={"fact-1": ("fact", "current")})
    gate = _make_gate(pg=pg, falkor=falkor)

    result = await gate.retract_object("acme", "fact-1", "alice", "api")
    assert result["status"] == "retracted"
    assert result["object_type"] == "fact"

    commit_row = pg.commit_appended[-1]
    assert commit_row["operation"] == "retract"
    assert commit_row["gate_decision"] == "human_retracted"
    assert commit_row["provenance"]["retracted_by"] == "alice"
    assert len(falkor.retracted) == 1

    # Already-retracted -> idempotent 409-equivalent.
    assert (await gate.retract_object("acme", "fact-1", "bob", "api"))["status"] == "already_retracted"
    # Unknown object -> not_found.
    assert (await gate.retract_object("acme", "unknown-id", "alice", "api"))["status"] == "not_found"


async def test_confirm_object_lifecycle():
    pg = _FakePg({})
    falkor = _FakeFalkor(objects={"rule-1": ("rule", "approved")})
    gate = _make_gate(pg=pg, falkor=falkor)

    result = await gate.confirm_object("acme", "rule-1", "alice", "mattermost")
    assert result["status"] == "confirmed"
    assert result["object_type"] == "rule"

    commit_row = pg.commit_appended[-1]
    assert commit_row["operation"] == "confirm"
    assert commit_row["gate_decision"] == "human_confirmed"
    assert commit_row["provenance"]["confirmed_by"] == "alice"
    assert len(falkor.confirmed) == 1
    assert falkor.confirmed[0]["by"] == "alice"

    # Unknown object -> not_found.
    assert (await gate.confirm_object("acme", "unknown-id", "alice", "api"))["status"] == "not_found"
