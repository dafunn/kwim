"""Pure-logic tests for freshness/decay.

Covers:
  - compute_freshness threshold table (permanent always fresh;
    age/half_life < 0.5 -> fresh, < 1.0 -> aging, >= 1.0 -> stale)
  - resolve_decay_class: fact_type-map fallback, validated override wins,
    invalid override falls back to the map
  - memory_context retrieval: as_of/decay_class/freshness on each fact,
    fresh-first ranking, coverage aggregate freshness = worst-case
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.auth import TeamContext
from app.freshness import compute_freshness, resolve_decay_class, worst_freshness
from app.main import memory_context as _memory_context_handler


def _iso(ago: timedelta) -> str:
    return (datetime.now(timezone.utc) - ago).isoformat()


# ---------------------------------------------------------------------------
# compute_freshness threshold table
# ---------------------------------------------------------------------------

def test_permanent_always_fresh():
    assert compute_freshness(_iso(timedelta(days=10000)), "permanent", 90, 48) == "fresh"
    assert compute_freshness(_iso(timedelta(seconds=0)), "permanent", 90, 48) == "fresh"


def test_slow_decay_thresholds():
    hl_days = 90
    assert compute_freshness(_iso(timedelta(days=0.3 * hl_days)), "slow", hl_days, 48) == "fresh"
    assert compute_freshness(_iso(timedelta(days=0.7 * hl_days)), "slow", hl_days, 48) == "aging"
    assert compute_freshness(_iso(timedelta(days=1.5 * hl_days)), "slow", hl_days, 48) == "stale"


def test_fast_decay_thresholds():
    hl_hours = 48
    assert compute_freshness(_iso(timedelta(hours=0.3 * hl_hours)), "fast", 90, hl_hours) == "fresh"
    assert compute_freshness(_iso(timedelta(hours=0.7 * hl_hours)), "fast", 90, hl_hours) == "aging"
    assert compute_freshness(_iso(timedelta(hours=1.5 * hl_hours)), "fast", 90, hl_hours) == "stale"


def test_unparseable_as_of_is_stale():
    assert compute_freshness("not-a-date", "slow", 90, 48) == "stale"


def test_epoch_ms_created_at_parses_fresh():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert compute_freshness(str(now_ms), "slow", 90, 48) == "fresh"


def test_old_epoch_ms_created_at_is_stale():
    old_ms = int((datetime.now(timezone.utc) - timedelta(days=200)).timestamp() * 1000)
    assert compute_freshness(str(old_ms), "slow", 90, 48) == "stale"


# ---------------------------------------------------------------------------
# resolve_decay_class
# ---------------------------------------------------------------------------

def test_resolve_decay_class_fact_type_map_fallback():
    assert resolve_decay_class("entity_attribute") == "permanent"
    assert resolve_decay_class("product") == "slow"
    assert resolve_decay_class("trend") == "fast"
    assert resolve_decay_class("some_new_fact_type") == "slow"  # unknown -> slow default


def test_resolve_decay_class_validated_override_wins():
    assert resolve_decay_class("entity_attribute", "fast") == "fast"
    assert resolve_decay_class("some_new_fact_type", "permanent") == "permanent"


def test_resolve_decay_class_invalid_override_falls_back_to_map():
    assert resolve_decay_class("entity_attribute", "bogus") == "permanent"
    assert resolve_decay_class("some_new_fact_type", "bogus") == "slow"
    assert resolve_decay_class("trend", "") == "fast"


# ---------------------------------------------------------------------------
# worst_freshness
# ---------------------------------------------------------------------------

def test_worst_freshness():
    assert worst_freshness([]) == "fresh"
    assert worst_freshness(["fresh", "fresh"]) == "fresh"
    assert worst_freshness(["fresh", "stale", "aging"]) == "stale"
    assert worst_freshness(["fresh", "aging"]) == "aging"


# ---------------------------------------------------------------------------
# memory_context retrieval: as_of/decay_class/freshness, ranking, coverage
# ---------------------------------------------------------------------------

class _FakeFalkor:
    def __init__(self, facts: list[dict]):
        self._facts = facts

    async def query_facts(self, team, fact_type, status, limit, about=None, source_kind=None):
        facts = self._facts
        if source_kind:
            facts = [f for f in facts if f.get("source_kind") == source_kind]
        if fact_type:
            facts = [f for f in facts if f.get("fact_type") == fact_type]
        if about:
            facts = [f for f in facts
                     if any(a.lower() in [x.lower() for x in f.get("about", [])] for a in about)]
        return [f for f in facts if f.get("status") == status][:limit]

    async def query_rules(self, team, situation=None, limit=20):
        return []


class _FakePg:
    async def recent_episodic(self, team, session_id):
        return []


class _FakeState:
    def __init__(self, falkor):
        self.falkor = falkor
        self.pg = _FakePg()


@pytest.fixture
def call_memory_context(monkeypatch):
    """Return an async caller that swaps app.main.State for a fake wrapping `facts`."""
    import app.main as main_mod

    async def _call(facts):
        monkeypatch.setattr(main_mod, "State", _FakeState(falkor=_FakeFalkor(facts)))
        team = TeamContext(team="acme", key_id="devkey")
        return await _memory_context_handler(session_id="s1", subject="Widget", team=team)

    return _call


async def test_enrichment_fields_and_fresh_first_ranking(call_memory_context):
    stale_fact = {
        "id": "f1", "statement": "Stale trend fact.", "fact_type": "trend",
        "status": "current", "created_at": _iso(timedelta(hours=200)),
        "about": ["Widget"], "decay_class": "fast",
    }
    fresh_fact = {
        "id": "f2", "statement": "Fresh trend fact.", "fact_type": "trend",
        "status": "current", "created_at": _iso(timedelta(hours=1)),
        "about": ["Widget"], "decay_class": "fast",
    }
    # Insert stale first to prove sorting reorders, not just preserves input order.
    result = await call_memory_context([stale_fact, fresh_fact])
    knowledge = result["knowledge"]

    assert len(knowledge) == 2
    assert knowledge[0]["id"] == "f2"          # fresh-first
    assert knowledge[1]["id"] == "f1"
    assert knowledge[0]["freshness"] == "fresh"
    assert knowledge[1]["freshness"] == "stale"
    assert knowledge[0]["decay_class"] == "fast"
    assert knowledge[0]["as_of"] == fresh_fact["created_at"]
    assert result["coverage"]["knowledge"]["freshness"] == "stale"  # worst-case


async def test_missing_decay_class_defaults_to_slow(call_memory_context):
    fact = {
        "id": "f3", "statement": "No decay_class set.", "fact_type": "observation",
        "status": "current", "created_at": _iso(timedelta(days=1)),
        "about": ["Widget"], "decay_class": None,
    }
    result = await call_memory_context([fact])
    assert result["knowledge"][0]["decay_class"] == "slow"
    assert result["knowledge"][0]["freshness"] == "fresh"  # 1 day old, slow halflife
