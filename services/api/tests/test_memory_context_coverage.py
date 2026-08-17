"""Pure-logic tests for memory_context coverage markers and knowledge slot filling.

Tests the key behaviours from the spec:
  - subject present + matching fact -> knowledge populated, covered=true
  - subject present + no match -> knowledge=[], covered=false, queried=true
  - subject absent -> covered=false, queried=false
  - wisdom/recent coverage counts match returned lists
  - knowledge-query error -> fail-soft empty + covered=false, bundle still returns
"""
import pytest

from kwim_api.auth import TeamContext
from kwim_api.routers.memory import memory_context as _memory_context_handler

# ---------------------------------------------------------------------------
# Fake backends
# ---------------------------------------------------------------------------

class _FakeFalkor:
    def __init__(self, facts=None, rules=None, raise_on_facts=False):
        self._facts = facts or []
        self._rules = rules or []
        self._raise_on_facts = raise_on_facts
        self.query_facts_calls: list[dict] = []
        self.query_rules_calls: list[dict] = []

    async def query_facts(self, team, fact_type, status, limit, about=None, source_kind=None):
        self.query_facts_calls.append({"about": about, "source_kind": source_kind})
        if self._raise_on_facts:
            raise RuntimeError("db error")
        facts = self._facts
        if source_kind:
            facts = [f for f in facts if f.get("source_kind") == source_kind]
        if about:
            facts = [f for f in facts
                     if any(a.lower() in [x.lower() for x in f.get("about", [])] for a in about)]
        return facts

    async def query_rules(self, team, situation=None, limit=20):
        self.query_rules_calls.append({"situation": situation})
        return self._rules


class _FakePg:
    def __init__(self, events=None):
        self._events = events or []

    async def recent_episodic(self, team, session_id):
        return self._events


class _FakeState:
    def __init__(self, falkor, pg):
        self.falkor = falkor
        self.pg = pg


@pytest.fixture
def call(monkeypatch):
    """Return an async caller that swaps the memory router's State for fakes and invokes the handler."""
    import kwim_api.routers.memory as main_mod

    async def _call(subject=None,
                    facts=None, rules=None, raise_on_facts=False, recent=None):
        monkeypatch.setattr(main_mod, "State", _FakeState(
            falkor=_FakeFalkor(facts=facts, rules=rules, raise_on_facts=raise_on_facts),
            pg=_FakePg(events=recent or []),
        ))
        team = TeamContext(team="acme", key_id="devkey")
        return await _memory_context_handler(
            session_id="s1", subject=subject, team=team,
        )

    return _call


async def test_subject_present_matching_fact(call):
    fact = {"id": "f1", "statement": "foo is a product line.", "fact_type": "product",
            "status": "current", "created_at": "2026-01-01", "about": ["foo"]}
    result = await call(subject="foo", facts=[fact])

    assert len(result["knowledge"]) == 1
    assert result["knowledge"][0]["statement"] == fact["statement"]
    assert result["coverage"]["knowledge"]["covered"] is True
    assert result["coverage"]["knowledge"]["n"] == 1
    assert result["coverage"]["knowledge"]["queried"] is True


async def test_subject_present_no_match(call):
    result = await call(subject="Unknown Product", facts=[])
    assert result["knowledge"] == []
    assert result["coverage"]["knowledge"]["covered"] is False
    assert result["coverage"]["knowledge"]["n"] == 0
    assert result["coverage"]["knowledge"]["queried"] is True


async def test_subject_absent(call):
    result = await call(subject=None, facts=[])
    assert result["knowledge"] == []
    assert result["coverage"]["knowledge"]["covered"] is False
    assert result["coverage"]["knowledge"]["queried"] is False


async def test_wisdom_and_recent_counts_match_lists(call):
    rule = {"id": "r1", "rule_type": "advisory", "situation": {}, "approach": "Find ways to include cats.",
            "evidence_count": 2, "status": "approved"}
    event = {"id": "e1", "type": "workflow_started"}
    result = await call(rules=[rule], recent=[event])

    assert result["coverage"]["wisdom"]["n"] == 1
    assert result["coverage"]["wisdom"]["covered"] is True
    assert result["coverage"]["recent"]["n"] == 1
    assert result["coverage"]["recent"]["covered"] is True


async def test_knowledge_query_error_fail_soft(call):
    result = await call(subject="Broken Subject", raise_on_facts=True)
    assert result["knowledge"] == []
    assert result["coverage"]["knowledge"]["covered"] is False
    assert "recent" in result
    assert "wisdom" in result
