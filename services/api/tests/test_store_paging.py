"""The real stores' paged list queries, against a stubbed connection.

The route tests stub the stores out entirely, so they cannot catch a store
method that never reaches its query - for example, a parameter that shadows the
module's `cursor()` connection helper. These call the real methods, first page
and later page, with the helper replaced by one that records the query.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

from kwim_api.stores import admin as admin_store
from kwim_api.stores import postgres as pg_store


class _RecordingCursor:
    def __init__(self, log: list):
        self._log = log

    async def execute(self, sql, params=None):
        self._log.append((sql, list(params or [])))

    async def fetchall(self):
        return []


def _recording_helper(log: list):
    @asynccontextmanager
    async def fake_cursor(pool, row_factory=None):
        yield _RecordingCursor(log)
    return fake_cursor


async def test_list_audit_first_page_reaches_the_query(monkeypatch):
    log: list = []
    monkeypatch.setattr(admin_store, "cursor", _recording_helper(log))
    store = admin_store.AdminStore()
    assert await store.list_audit(limit=10) == []
    sql, params = log[0]
    assert "FROM kwim_admin.audit_log" in sql and "seq <" not in sql
    assert params == [10]


async def test_list_audit_later_page_bounds_by_seq(monkeypatch):
    log: list = []
    monkeypatch.setattr(admin_store, "cursor", _recording_helper(log))
    store = admin_store.AdminStore()
    await store.list_audit(team="team-a", before_seq=42, limit=10)
    sql, params = log[0]
    assert "seq < %s" in sql
    assert params == ["team-a", 42, 10]


async def test_list_proposals_first_page_reaches_the_query(monkeypatch):
    log: list = []
    monkeypatch.setattr(pg_store, "cursor", _recording_helper(log))
    store = pg_store.PostgresStore()
    assert await store.list_proposals("team_a", limit=5) == []
    sql, params = log[0]
    assert "pending_proposals" in sql and "(created_at, proposal_id) <" not in sql
    assert params[-1] == 5


async def test_list_proposals_later_page_uses_the_keyset(monkeypatch):
    log: list = []
    monkeypatch.setattr(pg_store, "cursor", _recording_helper(log))
    store = pg_store.PostgresStore()
    after = {"created_at": datetime(2026, 1, 1, tzinfo=UTC), "id": "p-1"}
    await store.list_proposals("team_a", before=after, limit=5)
    sql, params = log[0]
    assert "(created_at, proposal_id) < (%s, %s)" in sql
    assert params[-3:] == [after["created_at"], "p-1", 5]
