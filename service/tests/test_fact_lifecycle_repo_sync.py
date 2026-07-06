"""Fact lifecycle for repo-sync pipelines: tombstone decommission + reaffirmation.

Service-side only. Adapter-side guard rails live elsewhere.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.auth import TeamContext
from app.freshness import _to_dt, compute_freshness
from app.gate import Gate
from app.main import _enrich_facts, knowledge_query as _knowledge_query_handler
from app.main import knowledge_reaffirm as _knowledge_reaffirm_handler


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakePg:
    def __init__(self):
        self.commit_appended: list[dict] = []

    async def evidence_meta(self, team, ids):
        return []

    async def append_commit(self, team, row):
        self.commit_appended.append(row)
        return len(self.commit_appended)

    async def insert_pending(self, team, row):
        pass


class _FakeFalkor:
    """Fake store with enough supersede/reaffirm behaviour for lifecycle tests."""

    def __init__(self, facts=None):
        self._facts = list(facts or [])
        self.proposal_sets: list[tuple] = []
        self.materialized_facts: list[dict] = []

    async def query_facts(self, team, fact_type, status, limit, about=None, source_kind=None):
        about_set = {str(a).lower() for a in (about or [])}
        results = []
        for f in self._facts:
            if f.get("status") != status:
                continue
            if fact_type and f.get("fact_type") != fact_type:
                continue
            if source_kind and f.get("source_kind") != source_kind:
                continue
            fact_about = {str(x).lower() for x in (f.get("about") or [])}
            if about_set and not (about_set & fact_about):
                continue
            results.append(dict(f))
            if len(results) >= limit:
                break
        return results

    async def proposal_set(self, pid, doc):
        self.proposal_sets.append((pid, doc))

    async def materialize_fact(
        self, team, fact, provenance, graph_name=None, embedding=None,
    ):
        if provenance.get("supersedes"):
            for f in self._facts:
                if f["id"] == provenance["supersedes"]:
                    f["status"] = "superseded"
        new_fact = {
            "id": fact["id"],
            "statement": fact["statement"],
            "fact_type": fact["fact_type"],
            "status": "current",
            "created_at": str(fact.get("commit_seq", 1)),
            "about": fact.get("about", []),
            "decay_class": fact.get("decay_class", "slow"),
            "source_kind": fact.get("source_kind"),
            "last_verified_at": None,
        }
        self._facts.append(new_fact)
        self.materialized_facts.append({"fact": fact, "embedding": embedding})

    async def find_object(self, team, object_id, object_type=None):
        for f in self._facts:
            if f["id"] == object_id:
                if object_type and object_type == "fact":
                    return "fact", f["status"]
                return "fact", f["status"]
        return None

    async def proposal_get(self, pid):
        return None

    async def reaffirm_fact(self, team, fact_id):
        for f in self._facts:
            if f["id"] == fact_id and f["status"] == "current":
                f["last_verified_at"] = str(int(datetime.now(timezone.utc).timestamp() * 1000))
                return True
        return False


class _FakeState:
    def __init__(self, falkor):
        self.falkor = falkor


def _make_gate(falkor):
    return Gate(_FakePg(), falkor, None, None)


# ---------------------------------------------------------------------------
# Freshness parse regression (already covered in test_freshness_logic.py,
# but _to_dt is the internal primitive that enables everything here)
# ---------------------------------------------------------------------------

def test_to_dt_handles_epoch_ms_and_iso():
    now = datetime.now(timezone.utc)
    epoch_ms = str(int(now.timestamp() * 1000))
    assert _to_dt(epoch_ms) is not None
    assert _to_dt(now.isoformat()) is not None
    assert _to_dt("not-a-date") is None


# ---------------------------------------------------------------------------
# Part 1 - Tombstone-supersession (decommission)
# ---------------------------------------------------------------------------

async def test_tombstone_supersede_end_to_end():
    existing_id = "host-hosta-inv"
    fk = _FakeFalkor(facts=[{
        "id": existing_id,
        "statement": "host-a is a group-x host at 10.0.0.10.",
        "fact_type": "host_inventory",
        "status": "current",
        "created_at": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "about": ["host-a", "group-x", "site-a"],
        "decay_class": "slow",
        "source_kind": "repo_sync",
    }])
    gate = _make_gate(fk)

    proposal = {
        "proposal_id": "p-tomb",
        "team": "acme",
        "object_type": "fact",
        "proposed_by": "repo-sync",
        "body": {
            "statement": "host-a decommissioned as of 2026-07-15 (was: host-a is a group-x host at 10.0.0.10.)",
            "fact_type": "host_inventory",
            "about": ["host-a", "group-x", "site-a"],
            "source_kind": "repo_sync",
            "supersedes": existing_id,
            "evidence": [],
        },
    }
    doc = await gate.handle(proposal)
    assert doc["status"] == "committed"

    original = next(f for f in fk._facts if f["id"] == existing_id)
    assert original["status"] == "superseded"

    tombstones = [f for f in fk._facts
                  if f["status"] == "current" and "decommissioned" in f["statement"]]
    assert len(tombstones) == 1
    assert tombstones[0]["fact_type"] == "host_inventory"
    assert tombstones[0]["about"] == ["host-a", "group-x", "site-a"]


# ---------------------------------------------------------------------------
# Part 2 - Reaffirmation
# ---------------------------------------------------------------------------

@pytest.fixture
def call_reaffirm(monkeypatch):
    """Return an async caller for the reaffirm endpoint with a fake store."""
    import app.main as main_mod

    async def _call(falkor, fact_id):
        monkeypatch.setattr(main_mod, "State", _FakeState(falkor))
        team = TeamContext(team="acme", key_id="devkey")
        return await _knowledge_reaffirm_handler(fact_id, team=team)

    return _call


async def test_reaffirm_stamps_last_verified_at(call_reaffirm):
    fk = _FakeFalkor(facts=[{
        "id": "f1", "statement": "x", "fact_type": "host_inventory",
        "status": "current", "created_at": "1000", "about": ["x"],
        "decay_class": "slow", "source_kind": "repo_sync",
    }])
    await call_reaffirm(fk, "f1")
    rows = await fk.query_facts("acme", None, "current", 10)
    assert rows[0]["last_verified_at"] is not None


async def test_reaffirm_unknown_fact_returns_404(call_reaffirm):
    fk = _FakeFalkor()
    with pytest.raises(Exception) as exc_info:
        await call_reaffirm(fk, "missing")
    assert exc_info.value.status_code == 404


async def test_last_verified_at_freshens_as_of():
    # created_at is old enough to be stale; last_verified_at is recent -> fresh.
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    fact = {
        "id": "f1", "statement": "x", "fact_type": "observation",
        "status": "current", "created_at": old, "about": [],
        "decay_class": "slow", "last_verified_at": recent,
    }
    enriched = _enrich_facts([fact])
    assert enriched[0]["freshness"] == "fresh"


async def test_created_at_used_when_last_verified_at_missing():
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    fact = {
        "id": "f1", "statement": "x", "fact_type": "observation",
        "status": "current", "created_at": recent, "about": [],
        "decay_class": "slow",
    }
    enriched = _enrich_facts([fact])
    assert enriched[0]["freshness"] == "fresh"


# ---------------------------------------------------------------------------
# source_kind filter/return
# ---------------------------------------------------------------------------

async def test_query_facts_source_kind_filter_and_return():
    fk = _FakeFalkor(facts=[
        {"id": "f1", "statement": "x", "fact_type": "host_inventory",
         "status": "current", "created_at": "1", "about": [],
         "decay_class": "slow", "source_kind": "repo_sync"},
        {"id": "f2", "statement": "y", "fact_type": "host_inventory",
         "status": "current", "created_at": "1", "about": [],
         "decay_class": "slow", "source_kind": "agent_proposal"},
    ])
    rows = await fk.query_facts("acme", None, "current", 10, source_kind="repo_sync")
    assert [r["id"] for r in rows] == ["f1"]
    assert rows[0]["source_kind"] == "repo_sync"


@pytest.fixture
def call_knowledge_query(monkeypatch):
    """Return an async caller for the knowledge query endpoint with a fake store."""
    import app.main as main_mod

    async def _call(falkor, **params):
        monkeypatch.setattr(main_mod, "State", _FakeState(falkor))
        team = TeamContext(team="acme", key_id="devkey")
        return await _knowledge_query_handler(
            team=team,
            fact_type=params.get("fact_type"),
            status_=params.get("status_", "current"),
            limit=params.get("limit", 50),
            about=params.get("about"),
        )

    return _call


async def test_knowledge_query_passes_source_kind(call_knowledge_query):
    fk = _FakeFalkor(facts=[
        {"id": "f1", "statement": "x", "fact_type": "host_inventory",
         "status": "current", "created_at": "1", "about": [],
         "decay_class": "slow", "source_kind": "repo_sync",
         "last_verified_at": None},
    ])
    # The public query endpoint does not expose source_kind filtering yet,
    # but the store returns it and the response model carries it.
    rows = await call_knowledge_query(fk)
    assert rows[0].source_kind == "repo_sync"
    assert rows[0].last_verified_at is None
