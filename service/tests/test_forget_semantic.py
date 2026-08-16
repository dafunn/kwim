"""Pure-logic tests for the semantic forget slice.

Semantic memory is the one store with no undo: `POST /v1/memory/semantic` upserts
but never deletes, the HTTP API has no delete verb, and semantic items are not
derived from commit_log so a `rebuild` cannot restore them. That makes the
delete path worth pinning down precisely.

Covers:
  - FalkorStore.get_semantic_for_forget  - resolve / not-found / Cypher shape.
  - FalkorStore.forget_semantic_node     - DETACH DELETE, read-back verification,
                                           and the absence of forget_node's
                                           orphaned-:Evidence sweep.
  - app.forget_semantic plan/execute     - unresolvable ids skipped, deleted vs
                                           failed accounting.
  - the CLI                              - dry-run deletes nothing; --confirm-count
                                           drift aborts without deleting.
"""
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Scriptable fake graph (same shape as test_semantic_memory's capture_falkor,
# but the result_set per call is scriptable so delete-then-verify can be driven)
# ---------------------------------------------------------------------------

class _ScriptedGraph:
    """Returns a queued result_set per query, capturing the Cypher as it goes."""

    def __init__(self, results: list[list[Any]], captured: list[dict]):
        self._results = list(results)
        self._captured = captured

    async def query(self, cypher: str, params: dict | None = None) -> Any:
        self._captured.append({"cypher": cypher, "params": params})
        rs = self._results.pop(0) if self._results else []

        class FakeRes:
            result_set = rs

        return FakeRes()


def _store(results: list[list[Any]]):
    """A FalkorStore whose graph replays `results`, one entry per query call."""
    from app.stores.falkor import FalkorStore

    captured: list[dict] = []
    graph = _ScriptedGraph(results, captured)
    fs = FalkorStore.__new__(FalkorStore)
    fs._inited = {"kwim_acme"}  # skip schema init
    fs._db = type("FakeDB", (), {"select_graph": lambda self, name: graph})()  # type: ignore[misc]
    return fs, captured


# ---------------------------------------------------------------------------
# FalkorStore.get_semantic_for_forget
# ---------------------------------------------------------------------------

async def test_get_semantic_for_forget_resolves_id_and_content():
    fs, captured = _store([[["chunk-1", "the runbook says X"]]])
    item = await fs.get_semantic_for_forget("acme", "chunk-1")
    assert item == {"id": "chunk-1", "content": "the runbook says X"}
    assert "MATCH (n:SemanticItem {id:$id})" in captured[0]["cypher"]
    assert captured[0]["params"] == {"id": "chunk-1"}


async def test_get_semantic_for_forget_returns_none_when_absent():
    fs, _ = _store([[]])
    assert await fs.get_semantic_for_forget("acme", "nope") is None


async def test_get_semantic_for_forget_tolerates_null_content():
    """A node written with empty content must resolve, not crash the plan."""
    fs, _ = _store([[["chunk-1", None]]])
    assert await fs.get_semantic_for_forget("acme", "chunk-1") == {"id": "chunk-1", "content": ""}


# ---------------------------------------------------------------------------
# FalkorStore.forget_semantic_node
# ---------------------------------------------------------------------------

async def test_forget_semantic_node_deletes_and_verifies():
    # 1st query: the DELETE. 2nd: the read-back, empty => gone.
    fs, captured = _store([[], []])
    assert await fs.forget_semantic_node("acme", "chunk-1") is True

    assert len(captured) == 2
    assert "DETACH DELETE" in captured[0]["cypher"]
    assert "(n:SemanticItem {id:$id})" in captured[0]["cypher"]
    assert captured[0]["params"] == {"id": "chunk-1"}
    # The second call must be a read-back, not another delete.
    assert "RETURN n.id" in captured[1]["cypher"]
    assert "DELETE" not in captured[1]["cypher"]


async def test_forget_semantic_node_reports_false_if_still_present():
    """A delete that silently didn't take must not be reported as success -
    there is no rebuild to fall back on here."""
    fs, _ = _store([[], [["chunk-1"]]])  # read-back still finds it
    assert await fs.forget_semantic_node("acme", "chunk-1") is False


async def test_forget_semantic_node_does_not_sweep_evidence():
    """forget_node sweeps orphaned :Evidence; the semantic path must not - a
    :SemanticItem carries no SUPPORTED_BY edges, so sweeping would be touching
    other objects' evidence for no reason."""
    fs, captured = _store([[], []])
    await fs.forget_semantic_node("acme", "chunk-1")
    assert not any("Evidence" in c["cypher"] for c in captured)


async def test_forget_semantic_node_scopes_delete_to_the_label():
    """Guard against the DETACH DELETE ever being widened to a bare MATCH (n)."""
    fs, captured = _store([[], []])
    await fs.forget_semantic_node("acme", "chunk-1")
    delete_cypher = captured[0]["cypher"]
    assert ":SemanticItem" in delete_cypher
    assert "{id:$id}" in delete_cypher


# ---------------------------------------------------------------------------
# app.forget_semantic - plan / execute
# ---------------------------------------------------------------------------

class _FakeStore:
    """Stands in for FalkorStore at the module level."""

    def __init__(self, present: dict[str, str], undeletable: set[str] | None = None):
        self.present = dict(present)
        self.undeletable = undeletable or set()
        self.deleted: list[str] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def get_semantic_for_forget(self, team: str, item_id: str):
        if item_id not in self.present:
            return None
        return {"id": item_id, "content": self.present[item_id]}

    async def forget_semantic_node(self, team: str, item_id: str) -> bool:
        if item_id in self.undeletable:
            return False
        self.deleted.append(item_id)
        self.present.pop(item_id, None)
        return True


async def test_plan_skips_unresolvable_ids():
    from app.forget_semantic import plan_forget_semantic

    store = _FakeStore({"a": "alpha"})
    plan = await plan_forget_semantic(store, "acme", ["a", "missing"])
    assert [p["id"] for p in plan] == ["a"]


async def test_execute_counts_deleted_and_failed():
    from app.forget_semantic import execute_forget_semantic

    store = _FakeStore({"a": "alpha", "b": "beta"}, undeletable={"b"})
    plan = [{"id": "a", "content": "alpha"}, {"id": "b", "content": "beta"}]
    report = await execute_forget_semantic(store, "acme", plan)
    assert report == {"semantic_items": 1, "failed": ["b"]}
    assert store.deleted == ["a"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch):
    """Run app.forget_semantic.main against a fake store; yields the store."""
    import app.forget_semantic as fs_mod

    store = _FakeStore({"a": "alpha", "b": "beta"})
    monkeypatch.setattr(fs_mod, "FalkorStore", lambda: store)
    return fs_mod, store


def test_cli_dry_run_deletes_nothing(cli):
    fs_mod, store = cli
    rc = fs_mod.main(["--team", "acme", "--ids", "a,b"])
    assert rc == 0
    assert store.deleted == []


def test_cli_commit_with_matching_confirm_count_deletes(cli):
    fs_mod, store = cli
    rc = fs_mod.main(["--team", "acme", "--ids", "a,b", "--commit", "--confirm-count", "2"])
    assert rc == 0
    assert sorted(store.deleted) == ["a", "b"]


def test_cli_confirm_count_drift_aborts_without_deleting(cli):
    """The plan resolved 1 item but the operator reviewed 2 - abort, since the
    graph changed since the dry-run they approved."""
    fs_mod, store = cli
    rc = fs_mod.main(["--team", "acme", "--ids", "a,missing", "--commit", "--confirm-count", "2"])
    assert rc == 1
    assert store.deleted == []


def test_cli_reports_failure_when_a_node_survives(cli, monkeypatch):
    import app.forget_semantic as fs_mod

    store = _FakeStore({"a": "alpha"}, undeletable={"a"})
    monkeypatch.setattr(fs_mod, "FalkorStore", lambda: store)
    rc = fs_mod.main(["--team", "acme", "--ids", "a", "--commit", "--confirm-count", "1"])
    assert rc == 1


def test_cli_no_targets_is_a_clean_exit(cli):
    fs_mod, store = cli
    rc = fs_mod.main(["--team", "acme", "--ids", "missing"])
    assert rc == 0
    assert store.deleted == []


def test_cli_closes_the_store(cli):
    fs_mod, store = cli
    fs_mod.main(["--team", "acme", "--ids", "a"])
    assert store.closed, "the store must be closed even on the dry-run path"
