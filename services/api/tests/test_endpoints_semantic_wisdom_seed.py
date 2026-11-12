"""Endpoint tests
  - GET /v1/memory/semantic  (optional q, meta.* filters)
  - POST /v1/memory/semantic (explicit semantic write)
  - POST /v1/wisdom/seed     (operator-gated direct commit)
  - POST /v1/wisdom/promote/{rule_id}  (provenance timestamp)

Auth keys/promote allowlist come from conftest's env superset:
  devkey -> acme (key_id "devkey", not a promote key)
  promoter -> acme (key_id "promot", is a promote key)
"""
import datetime
import re
from typing import Any

import pytest


class _FakeFalkor:
    def __init__(self):
        self.semantic_items: list[dict] = []
        self.rules: list[dict] = []
        self.calls: list[tuple] = []

    async def materialize_semantic(self, team: str, item: dict) -> None:
        self.calls.append(("materialize_semantic", team, item))
        self.semantic_items.append(item)

    async def query_semantic(self, team: str, qvec: Any = None, limit: int = 10,
                             filters: dict | None = None) -> list[dict]:
        self.calls.append(("query_semantic", team, {"qvec": qvec is not None, "limit": limit, "filters": filters}))
        results = []
        for item in self.semantic_items:
            if filters:
                if not all(item.get("metadata", {}).get(k) == v for k, v in filters.items()):
                    continue
            results.append({**item, "score": 0.1})
        return results[:limit]

    async def get_by_metadata(self, team: str, filters: dict[str, Any]) -> list[dict]:
        self.calls.append(("get_by_metadata", team, filters))
        if not filters:
            return []
        return [{**item} for item in self.semantic_items
                if all(item.get("metadata", {}).get(k) == v for k, v in filters.items())]

    async def materialize_rule(self, team: str, rule: dict, provenance: dict, graph_name: Any = None) -> None:
        # Enforce hard keys the real materialize_rule requires
        assert "commit_seq" in rule, f"materialize_rule missing commit_seq in {rule.keys()}"
        self.calls.append(("materialize_rule", team, rule, provenance))
        self.rules.append(rule)

    async def query_rules(self, team: str, situation: dict | None = None, limit: int = 20) -> list[dict]:
        self.calls.append(("query_rules", team, limit))
        return [{**r} for r in self.rules]

    async def get_rule(self, team: str, rule_id: str) -> dict | None:
        self.calls.append(("get_rule", team, rule_id))
        return next((r for r in self.rules if r["id"] == rule_id), None)

    async def tag_rule_promoted(self, team: str, rule_id: str) -> None:
        self.calls.append(("tag_rule_promoted", team, rule_id))

    async def append_commit(self, team: str, record: dict) -> int:
        # Enforce hard keys the real append_commit requires
        assert "gate_decision" in record, f"append_commit missing gate_decision in {record.keys()}"
        return 42


class _FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakePg:
    def __init__(self):
        self.calls: list[tuple] = []

    async def append_commit(self, team: str, record: dict) -> int:
        self.calls.append(("append_commit", team, record))
        return 99


class _Wired:
    def __init__(self, client, falkor, embedder, pg):
        self.client = client
        self.falkor = falkor
        self.embedder = embedder
        self.pg = pg


@pytest.fixture
def wired(client, monkeypatch):
    """Fresh fakes wired onto kwim_api.runtime.State for the duration of one test."""
    from kwim_api.gate import Gate
    from kwim_api.runtime import State

    falkor, embedder, pg = _FakeFalkor(), _FakeEmbedder(), _FakePg()
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    monkeypatch.setattr(State, "embedder", embedder, raising=False)
    monkeypatch.setattr(State, "pg", pg, raising=False)
    # A real Gate over the fakes, for POST /v1/memory/semantic.
    monkeypatch.setattr(client.app.state, "gate", Gate(pg, falkor, None, embedder),
                        raising=False)
    return _Wired(client, falkor, embedder, pg)


DEV = {"Authorization": "Bearer devkey"}


# ---------------------------------------------------------------------------
# GET /v1/memory/semantic
# ---------------------------------------------------------------------------

def test_get_semantic_query_with_filters(wired):
    wired.falkor.semantic_items = [
        {"id": "s1", "content": "demoproject style doc",
         "metadata": {"project": "demoproject", "doc_type": "style"}, "embedding": [0.1]},
        {"id": "s2", "content": "altproject style doc",
         "metadata": {"project": "altproject", "doc_type": "style"}, "embedding": [0.2]},
    ]

    # With q + metadata filter -> vector query.
    r = wired.client.get("/v1/memory/semantic?q=style&meta.project=demoproject", headers=DEV)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert wired.falkor.calls[-1][0] == "query_semantic"
    assert wired.falkor.calls[-1][2]["filters"] == {"project": "demoproject"}

    # Without q -> metadata-only.
    r = wired.client.get("/v1/memory/semantic?meta.doc_type=style&meta.project=altproject", headers=DEV)
    assert r.status_code == 200
    assert wired.falkor.calls[-1][0] == "get_by_metadata"
    assert wired.falkor.calls[-1][2] == {"doc_type": "style", "project": "altproject"}

    # No q, no filters -> empty (get_by_metadata returns [] for empty filters).
    r = wired.client.get("/v1/memory/semantic", headers=DEV)
    assert r.status_code == 200
    assert r.json() == []

    # meta.* arbitrary filter.
    wired.falkor.semantic_items.append(
        {"id": "s3", "content": "custom",
         "metadata": {"project": "demoproject", "custom_key": "custom_val"}, "embedding": [0.3]})
    r = wired.client.get("/v1/memory/semantic?q=test&meta.custom_key=custom_val", headers=DEV)
    assert r.status_code == 200
    assert wired.falkor.calls[-1][2]["filters"] == {"custom_key": "custom_val"}


# ---------------------------------------------------------------------------
# POST /v1/memory/semantic
# ---------------------------------------------------------------------------

def test_post_semantic(wired):
    r = wired.client.post(
        "/v1/memory/semantic",
        json={"content": "test content", "metadata": {"project": "demoproject"}},
        headers=DEV,
    )
    assert r.status_code == 201
    data = r.json()
    assert "id" in data
    assert data["content"] == "test content"
    assert data["metadata"] == {"project": "demoproject"}
    assert data["score"] == 0.0
    assert wired.falkor.calls[0][0] == "materialize_semantic"
    assert wired.falkor.calls[0][2]["content"] == "test content"
    assert wired.falkor.calls[0][2]["metadata"] == {"project": "demoproject"}

    # With explicit id.
    r = wired.client.post("/v1/memory/semantic", json={"id": "my-id", "content": "explicit id"}, headers=DEV)
    assert r.status_code == 201
    assert r.json()["id"] == "my-id"


def test_post_semantic_is_durable(wired):
    """A direct semantic write must land in commit_log, or a rebuild drops it.

    """
    r = wired.client.post(
        "/v1/memory/semantic",
        json={"id": "s-durable", "content": "keep me", "metadata": {"k": "v"}},
        headers=DEV,
    )
    assert r.status_code == 201

    commits = [c for c in wired.pg.calls if c[0] == "append_commit"]
    assert len(commits) == 1
    row = commits[0][2]
    assert row["object_type"] == "semantic"
    assert row["object_id"] == "s-durable"
    assert row["operation"] == "commit"
    assert row["payload"] == {"content": "keep me", "metadata": {"k": "v"}}


def test_post_semantic_logs_before_materializing(wired):
    """Ordering matters: a node with no row is silently reverted by a rebuild."""
    wired.client.post("/v1/memory/semantic",
                      json={"id": "s-order", "content": "x"}, headers=DEV)
    assert wired.pg.calls[0][0] == "append_commit"
    assert wired.falkor.calls[0][0] == "materialize_semantic"


# ---------------------------------------------------------------------------
# POST /v1/wisdom/seed
# ---------------------------------------------------------------------------

def test_wisdom_seed_requires_promote_key(wired):
    # devkey is not a promote key -> 403.
    r = wired.client.post(
        "/v1/wisdom/seed",
        json={"id": "rule-1", "rule_type": "constraint", "status": "approved",
              "action_pattern": "test", "verdict": "block", "authority": "project",
              "severity": "high", "check_tier": "output"},
        headers=DEV,
    )
    assert r.status_code == 403


def test_wisdom_seed_with_promote_key(wired):
    r = wired.client.post(
        "/v1/wisdom/seed",
        json={"id": "rule-1", "rule_type": "constraint", "action_pattern": "test",
              "verdict": "block", "authority": "project", "severity": "high", "check_tier": "output"},
        headers={"Authorization": "Bearer promoter"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "seeded"
    assert data["rule_id"] == "rule-1"
    assert wired.falkor.calls[-1][0] == "materialize_rule"

    seeded = wired.falkor.calls[-1][2]
    assert seeded["status"] == "approved"
    # regression guard: constraint fields survive the SeedRule round-trip.
    assert seeded["action_pattern"] == "test"
    assert seeded["verdict"] == "block"
    assert seeded["authority"] == "project"
    assert seeded["severity"] == "high"
    assert seeded["check_tier"] == "output"
    # regression guard: commit_seq present + int.
    assert "commit_seq" in seeded
    assert isinstance(seeded["commit_seq"], int)
    # regression guard: pg.append_commit got gate_decision.
    assert wired.pg.calls[-1][2].get("gate_decision") == "human_approved"


# ---------------------------------------------------------------------------
# Provenance timestamps (seeded_at, promoted_at)
# ---------------------------------------------------------------------------

# UTC ISO 8601 with a `Z` suffix: 2026-10-29T14:03:12.345678Z. isoformat() omits
# the fraction when microsecond is 0.
_UTC_Z_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?Z")


def _assert_utc_z_now(value: str, before: datetime.datetime, after: datetime.datetime) -> None:
    """`value` has the `Z` format and is a UTC time taken during the request."""
    assert _UTC_Z_TIMESTAMP.fullmatch(value), f"timestamp {value!r} is not YYYY-MM-DDTHH:MM:SS[.ffffff]Z"
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert before.replace(microsecond=0) <= parsed <= after, (
        f"timestamp {value!r} is not the UTC time of the request ({before} .. {after})")


def test_wisdom_seed_seeded_at_is_utc_z(wired):
    before = datetime.datetime.now(datetime.UTC)
    r = wired.client.post(
        "/v1/wisdom/seed",
        json={"id": "rule-1", "rule_type": "constraint", "action_pattern": "test",
              "verdict": "block", "authority": "project", "severity": "high", "check_tier": "output"},
        headers={"Authorization": "Bearer promoter"},
    )
    after = datetime.datetime.now(datetime.UTC)
    assert r.status_code == 201

    committed = wired.pg.calls[-1][2]["provenance"]
    materialized = wired.falkor.calls[-1][3]
    assert committed == materialized
    assert set(committed) == {"seeded_by", "seeded_at"}
    _assert_utc_z_now(committed["seeded_at"], before, after)


def test_wisdom_promote_promoted_at_is_utc_z(wired):
    wired.falkor.rules = [{
        "id": "rule-1", "_source": "team", "status": "approved", "rule_type": "constraint",
        "action_pattern": "test", "verdict": "block", "authority": "project",
        "severity": "high", "check_tier": "output", "evidence_count": 3,
    }]
    before = datetime.datetime.now(datetime.UTC)
    r = wired.client.post("/v1/wisdom/promote/rule-1", headers={"Authorization": "Bearer promoter"})
    after = datetime.datetime.now(datetime.UTC)
    assert r.status_code == 200
    assert r.json()["status"] == "promoted"

    committed = wired.pg.calls[-1][2]["provenance"]
    materialize = next(c for c in wired.falkor.calls if c[0] == "materialize_rule")
    assert committed == materialize[3]
    assert committed["promoted_by"] == "promot"
    _assert_utc_z_now(committed["promoted_at"], before, after)
