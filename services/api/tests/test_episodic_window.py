"""Tests for the OUT-crossing distiller's read gap:
  - PostgresStore.read_episodic - composite (occurred_at, id) cursor query construction.
  - GET /v1/memory/episodic - windowed, team-scoped batch read endpoint.
"""
import datetime

import pytest

from kwim_api.stores.postgres import PostgresStore

_NOW = datetime.datetime(2026, 6, 11, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# read_episodic - SQL construction (fake cursor/pool, no real Postgres)
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.executed: tuple[str, list] | None = None

    async def execute(self, sql: str, params: list) -> None:
        self.executed = (sql, params)

    async def fetchall(self) -> list[dict]:
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows: list[dict]):
        self.last_cursor: _FakeCursor | None = None
        self._rows = rows

    def cursor(self, row_factory=None):
        self.last_cursor = _FakeCursor(self._rows)
        return self.last_cursor


async def _read(rows=None, **kwargs):
    """Run read_episodic against a fake pool; return (result, sql, params)."""
    store = PostgresStore()
    store._pool = _FakePool(rows=rows or [])  # type: ignore[attr-defined]
    result = await store.read_episodic("acme", **kwargs)
    sql, params = store._pool.last_cursor.executed
    return result, sql, params


async def test_read_from_start_no_cursor_clause():
    _, sql, params = await _read(limit=500)
    assert "NOT archived" in sql
    assert "(occurred_at, id) >" not in sql
    assert "ORDER BY occurred_at ASC, id ASC" in sql
    assert params == [500]


async def test_read_windowed_all_filters_param_order():
    _, sql, params = await _read(
        since_ts=_NOW, since_id="evt-1", limit=10,
        event_type="research_complete", agent_id="researcher",
    )
    assert "(occurred_at, id) > (%s, %s)" in sql
    assert "event_type = %s" in sql
    assert "agent_id = %s" in sql
    assert params == [_NOW, "evt-1", "research_complete", "researcher", 10]


async def test_read_cursor_only_param_order():
    _, _, params = await _read(since_ts=_NOW, since_id="evt-1", limit=50)
    assert params == [_NOW, "evt-1", 50]


async def test_read_rows_pass_through_untouched():
    canned = [{"id": "evt-2", "agent_id": "researcher", "session_id": "s1",
               "event_type": "research_complete", "event_data": {}, "occurred_at": _NOW}]
    result, _, _ = await _read(rows=canned, limit=10)
    assert result == canned


async def test_read_desc_no_cursor():
    _, sql, params = await _read(limit=1, event_type="distiller_watermark", order="desc")
    assert "(occurred_at, id) >" not in sql and "(occurred_at, id) <" not in sql
    assert "ORDER BY occurred_at DESC, id DESC" in sql
    assert params == ["distiller_watermark", 1]


async def test_read_desc_with_cursor_exclusive_upper_bound():
    _, sql, params = await _read(since_ts=_NOW, since_id="evt-9", limit=10, order="desc")
    assert "(occurred_at, id) < (%s, %s)" in sql
    assert params == [_NOW, "evt-9", 10]


# ---------------------------------------------------------------------------
# GET /v1/memory/episodic - endpoint
# ---------------------------------------------------------------------------

_T1 = datetime.datetime(2026, 6, 11, 10, 0, 0, tzinfo=datetime.UTC)
_T2 = datetime.datetime(2026, 6, 11, 11, 0, 0, tzinfo=datetime.UTC)


class _FakePg:
    def __init__(self):
        self.calls: list[dict] = []
        self.rows_by_team: dict[str, list[dict]] = {}

    async def read_episodic(self, team, since_ts=None, since_id=None,
                            limit=500, event_type=None, agent_id=None, order="asc"):
        self.calls.append({"team": team, "since_ts": since_ts, "since_id": since_id,
                           "limit": limit, "event_type": event_type, "agent_id": agent_id,
                           "order": order})
        rows = self.rows_by_team.get(team, [])
        if order == "desc":
            rows = list(reversed(rows))
        return rows[:limit]


@pytest.fixture
def episodic(client, monkeypatch):
    """Wire a fake pg onto State and return (client, fake_pg)."""
    from kwim_api.runtime import State

    fake_pg = _FakePg()
    monkeypatch.setattr(State, "pg", fake_pg, raising=False)
    return client, fake_pg


DEV = {"Authorization": "Bearer devkey"}


def test_episodic_non_empty_window_and_cursor(episodic):
    cli, fake_pg = episodic
    fake_pg.rows_by_team["acme"] = [
        {"id": "evt-1", "agent_id": "researcher", "session_id": "s1",
         "event_type": "research_complete", "event_data": {"a": 1}, "occurred_at": _T1},
        {"id": "evt-2", "agent_id": "researcher", "session_id": "s1",
         "event_type": "research_complete", "event_data": {"a": 2}, "occurred_at": _T2},
    ]
    r = cli.get("/v1/memory/episodic", headers=DEV)
    assert r.status_code == 200
    body = r.json()
    assert len(body["events"]) == 2
    assert [e["id"] for e in body["events"]] == ["evt-1", "evt-2"]
    assert body["next_cursor"]["id"] == "evt-2"
    assert body["next_cursor"]["ts"] == _T2.isoformat()


def test_episodic_empty_window_null_cursor(episodic):
    cli, fake_pg = episodic
    fake_pg.rows_by_team["acme"] = []
    r = cli.get("/v1/memory/episodic", headers=DEV)
    assert r.status_code == 200
    body = r.json()
    assert body["events"] == []
    assert body["next_cursor"] is None


def test_episodic_query_params_pass_through(episodic):
    cli, fake_pg = episodic
    r = cli.get(
        "/v1/memory/episodic"
        "?since_ts=2026-06-11T10:00:00%2B00:00&since_id=evt-1&limit=10"
        "&event_type=research_complete&agent_id=researcher",
        headers=DEV,
    )
    assert r.status_code == 200
    call = fake_pg.calls[-1]
    assert call["team"] == "acme"
    assert call["since_ts"] == _T1
    assert call["since_id"] == "evt-1"
    assert call["limit"] == 10
    assert call["event_type"] == "research_complete"
    assert call["agent_id"] == "researcher"


def test_episodic_team_isolation(episodic):
    cli, fake_pg = episodic
    fake_pg.rows_by_team["acme"] = [
        {"id": "evt-1", "agent_id": "researcher", "session_id": "s1",
         "event_type": "research_complete", "event_data": {}, "occurred_at": _T1},
    ]
    fake_pg.rows_by_team["otherteam"] = [
        {"id": "evt-other", "agent_id": "researcher", "session_id": "s9",
         "event_type": "research_complete", "event_data": {}, "occurred_at": _T1},
    ]
    r_team = cli.get("/v1/memory/episodic", headers=DEV)
    r_other = cli.get("/v1/memory/episodic", headers={"Authorization": "Bearer otherkey"})
    assert [e["id"] for e in r_team.json()["events"]] == ["evt-1"]
    assert [e["id"] for e in r_other.json()["events"]] == ["evt-other"]


@pytest.mark.parametrize("query,expected_status", [
    ("?since_ts=not-a-timestamp&since_id=evt-1", 422),      # malformed since_ts
    ("?since_ts=2026-06-11T10:00:00%2B00:00", 422),         # since_ts without since_id
    ("?since_id=evt-1", 422),                                # since_id without since_ts
    ("?limit=0", 422),                                       # below range
    ("?order=sideways", 422),                                # invalid order
])
def test_episodic_validation_rejects(episodic, query, expected_status):
    cli, fake_pg = episodic
    r = cli.get(f"/v1/memory/episodic{query}", headers=DEV)
    assert r.status_code == expected_status
    assert len(fake_pg.calls) == 0  # rejected before reaching the store


def test_episodic_limit_bounds(episodic):
    cli, _ = episodic
    from kwim_api.routers.memory import _EPISODIC_MAX_LIMIT

    r = cli.get(f"/v1/memory/episodic?limit={_EPISODIC_MAX_LIMIT + 1}", headers=DEV)
    assert r.status_code == 422
    r = cli.get(f"/v1/memory/episodic?limit={_EPISODIC_MAX_LIMIT}", headers=DEV)
    assert r.status_code == 200


def test_episodic_order_desc_watermark_lookup(episodic):
    cli, fake_pg = episodic
    fake_pg.rows_by_team["acme"] = [
        {"id": "evt-1", "agent_id": "distiller", "session_id": "distiller",
         "event_type": "distiller_watermark",
         "event_data": {"last_ts": _T1.isoformat(), "last_id": "evt-a"}, "occurred_at": _T1},
        {"id": "evt-2", "agent_id": "distiller", "session_id": "distiller",
         "event_type": "distiller_watermark",
         "event_data": {"last_ts": _T2.isoformat(), "last_id": "evt-b"}, "occurred_at": _T2},
    ]
    r = cli.get("/v1/memory/episodic?event_type=distiller_watermark&limit=1&order=desc", headers=DEV)
    assert r.status_code == 200
    assert [e["id"] for e in r.json()["events"]] == ["evt-2"]
    call = fake_pg.calls[-1]
    assert call["order"] == "desc"
    assert (call["since_ts"], call["since_id"]) == (None, None)
