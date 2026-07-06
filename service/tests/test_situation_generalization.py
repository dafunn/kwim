"""Situation generalization:
  - materialize_rule promotes all situation keys to node properties
    (reserved keys skipped, kept in situation_json only)
  - query_rules AND-matches an open situation dict (sanitize-and-bind)
  - GET /v1/wisdom/rules takes ?situation.<key>=<value> params
  - GET /v1/memory/context forwards the situation dict to query_rules
"""
from typing import Any

import pytest

from app.stores.falkor import FalkorStore


DEV = {"Authorization": "Bearer devkey"}


# ---------------------------------------------------------------------------
# Store layer (capture pattern - Cypher is inspected, not executed)
# ---------------------------------------------------------------------------

@pytest.fixture
def capture_falkor():
    """A FalkorStore whose graph.query() captures the Cypher instead of running it."""
    captured: list[dict] = []

    class _CaptureGraph:
        async def query(self, cypher: str, params: dict | None = None) -> Any:
            captured.append({"cypher": cypher, "params": params})

            class FakeRes:
                result_set: list[Any] = []

            return FakeRes()

    fs = FalkorStore.__new__(FalkorStore)
    fs._inited = {"kwim_acme"}  # skip init calls
    fs._db = type("FakeDB", (), {"select_graph": lambda self, name: _CaptureGraph()})()  # type: ignore[misc]
    return fs, captured


_BASE_RULE = {
    "id": "r1", "rule_type": "advisory", "status": "approved",
    "scope": "team", "evidence_count": 1, "commit_seq": 7,
    "approach": "do x",
}


async def test_materialize_rule_promotes_arbitrary_situation_keys(capture_falkor):
    fs, captured = capture_falkor
    rule = {**_BASE_RULE, "situation": {"repo": "test-repo", "profile": "test"}}
    await fs.materialize_rule("acme", rule, provenance={})
    cypher, params = captured[0]["cypher"], captured[0]["params"]
    assert "r.repo=$sit_repo" in cypher
    assert "r.profile=$sit_profile" in cypher
    assert params["sit_repo"] == "test-repo"
    assert params["sit_profile"] == "test"


async def test_materialize_rule_legacy_triple_promoted_same_shape(capture_falkor):
    fs, captured = capture_falkor
    rule = {**_BASE_RULE, "situation": {"project": "demoproject", "task_type": "compose"}}
    await fs.materialize_rule("acme", rule, provenance={})
    cypher, params = captured[0]["cypher"], captured[0]["params"]
    assert "r.project=$sit_project" in cypher
    assert "r.task_type=$sit_task_type" in cypher
    assert params["sit_project"] == "demoproject"


async def test_materialize_rule_skips_reserved_situation_keys(capture_falkor):
    fs, captured = capture_falkor
    rule = {**_BASE_RULE, "situation": {"status": "evil", "project": "ok"}}
    await fs.materialize_rule("acme", rule, provenance={})
    cypher, params = captured[0]["cypher"], captured[0]["params"]
    assert "sit_status" not in params           # reserved -> not promoted
    assert cypher.count("r.status=") == 1       # only the node's own status SET
    assert params["sit_project"] == "ok"          # non-reserved sibling still promoted
    # full situation is still the json truth
    assert "evil" in params["situation_json"]


async def test_materialize_rule_sanitizes_situation_keys(capture_falkor):
    fs, captured = capture_falkor
    rule = {**_BASE_RULE, "situation": {"weird-key!": "v"}}
    await fs.materialize_rule("acme", rule, provenance={})
    cypher, params = captured[0]["cypher"], captured[0]["params"]
    assert "r.weird_key_=$sit_weird_key_" in cypher
    assert params["sit_weird_key_"] == "v"


async def test_query_rules_graph_filters_on_arbitrary_keys(capture_falkor):
    fs, captured = capture_falkor
    await fs._query_rules_from_graph(
        "acme", {"repo": "test-repo", "profile": "test"}, 20, source_tag="team")
    cypher, params = captured[0]["cypher"], captured[0]["params"]
    assert "AND r.repo=$sit_repo" in cypher
    assert "AND r.profile=$sit_profile" in cypher
    assert params["sit_repo"] == "test-repo"


async def test_query_rules_graph_no_situation_no_filter(capture_falkor):
    fs, captured = capture_falkor
    await fs._query_rules_from_graph("acme", None, 20, source_tag="team")
    cypher = captured[0]["cypher"]
    assert "AND r." not in cypher.split("RETURN")[0].replace(
        "WHERE r.status='approved'", "")


async def test_query_rules_graph_ignores_reserved_keys(capture_falkor):
    fs, captured = capture_falkor
    await fs._query_rules_from_graph(
        "acme", {"verdict": "allow", "project": "b"}, 20, source_tag="team")
    cypher, params = captured[0]["cypher"], captured[0]["params"]
    assert "sit_verdict" not in params
    assert "AND r.project=$sit_project" in cypher


# ---------------------------------------------------------------------------
# Endpoints (TestClient, fakes wired onto app.main.State)
# ---------------------------------------------------------------------------

_RULE_ROW = {
    "id": "r1", "rule_type": "advisory", "situation": {"project": "demoproject"},
    "approach": "do x", "evidence_count": 3, "status": "approved",
    "scope": "team", "_source": "team",
}


class _FakeFalkor:
    def __init__(self):
        self.query_rules_calls: list[dict] = []

    async def query_rules(self, team, situation=None, limit=20):
        self.query_rules_calls.append({"team": team, "situation": situation, "limit": limit})
        return [_RULE_ROW]

    async def query_facts(self, team, fact_type, status, limit, about=None, source_kind=None):
        return []


class _FakePg:
    async def recent_episodic(self, team, session_id):
        return []


@pytest.fixture
def wired(client, monkeypatch):
    from app.main import State
    falkor = _FakeFalkor()
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    monkeypatch.setattr(State, "pg", _FakePg(), raising=False)
    return client, falkor


def test_wisdom_rules_situation_params(wired):
    client, falkor = wired
    r = client.get("/v1/wisdom/rules?situation.repo=test-repo&situation.profile=test",
                   headers=DEV)
    assert r.status_code == 200
    assert falkor.query_rules_calls[-1]["situation"] == {
        "repo": "test-repo", "profile": "test"}


def test_memory_context_forwards_situation(wired):
    client, falkor = wired
    r = client.get("/v1/memory/context?session_id=s1&situation.profile=test", headers=DEV)
    assert r.status_code == 200
    assert falkor.query_rules_calls[-1]["situation"] == {"profile": "test"}
