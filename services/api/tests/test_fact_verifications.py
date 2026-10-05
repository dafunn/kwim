"""Durability of last_verified_at across a rebuild.

Covers the fact_verifications table, the reaffirm endpoint writing both stores,
the rebuild reapplying the stamp, and forget removing it.
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from kwim_api.auth import TeamContext
from kwim_api.routers.knowledge import knowledge_reaffirm as _reaffirm_handler
from kwim_api.stores.postgres import PostgresStore

_VERIFIED = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_VERIFIED_MS = int(_VERIFIED.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _RaisingCursor:
    """Cursor whose execute raises UndefinedTable, as an unprovisioned schema would."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **kw):
        raise psycopg.errors.UndefinedTable("relation does not exist")


class _RaisingPool:
    @asynccontextmanager
    async def connection(self):
        yield self

    def cursor(self, **kw):
        return _RaisingCursor()


class _FakeFalkor:
    def __init__(self, current_ids=()):
        self.current = set(current_ids)
        self.stamped: list[dict] = []
        self.forgotten: list[tuple] = []

    async def reaffirm_fact(self, team, fact_id, verified_at=None, graph_name=None):
        if fact_id not in self.current:
            return False
        self.stamped.append({"fact_id": fact_id, "verified_at": verified_at,
                             "graph_name": graph_name})
        return True

    async def forget_node(self, team, object_type, object_id):
        self.forgotten.append((object_type, object_id))


class _FakePg:
    def __init__(self, verifications=None):
        self._verifications = verifications or []
        self.recorded: list[dict] = []
        self.deleted: list[list[str]] = []
        self.commit_deleted: list[str] = []

    async def record_verification(self, team, fact_id, verified_at, verified_by=None):
        self.recorded.append({"fact_id": fact_id, "verified_at": verified_at,
                              "verified_by": verified_by})
        return True

    async def read_verifications(self, team):
        return list(self._verifications)

    async def delete_verifications(self, team, fact_ids):
        self.deleted.append(list(fact_ids))
        return len(fact_ids)

    async def delete_commit_log(self, team, object_id):
        self.commit_deleted.append(object_id)
        return 1

    async def delete_episodic(self, team, ids):
        return len(ids)


class _FakeState:
    def __init__(self, falkor, pg):
        self.falkor = falkor
        self.pg = pg


# ---------------------------------------------------------------------------
# Postgres store: fail-soft when the team schema predates the table
# ---------------------------------------------------------------------------

async def test_record_verification_missing_table_is_soft():
    store = PostgresStore()
    store._pool = _RaisingPool()
    assert await store.record_verification("acme", "f1", _VERIFIED) is False


async def test_read_verifications_missing_table_is_soft():
    store = PostgresStore()
    store._pool = _RaisingPool()
    assert await store.read_verifications("acme") == []


async def test_delete_verifications_missing_table_is_soft():
    store = PostgresStore()
    store._pool = _RaisingPool()
    assert await store.delete_verifications("acme", ["f1"]) == 0


async def test_delete_verifications_empty_is_noop():
    store = PostgresStore()
    store._pool = None  # never touched: the empty list short-circuits first
    assert await store.delete_verifications("acme", []) == 0


# ---------------------------------------------------------------------------
# The reaffirm endpoint writes both stores
# ---------------------------------------------------------------------------

@pytest.fixture
def reaffirm(monkeypatch):
    import kwim_api.routers.knowledge as mod

    async def _call(falkor, pg, fact_id):
        monkeypatch.setattr(mod, "State", _FakeState(falkor, pg))
        return await _reaffirm_handler(fact_id, team=TeamContext(team="acme", key_id="devkey"))

    return _call


async def test_reaffirm_records_durable_row(reaffirm):
    fk, pg = _FakeFalkor(current_ids=["f1"]), _FakePg()
    await reaffirm(fk, pg, "f1")
    assert [r["fact_id"] for r in pg.recorded] == ["f1"]
    assert pg.recorded[0]["verified_by"] == "devkey"


async def test_reaffirm_uses_one_instant_for_both_stores(reaffirm):
    fk, pg = _FakeFalkor(current_ids=["f1"]), _FakePg()
    await reaffirm(fk, pg, "f1")
    graph_ms = fk.stamped[0]["verified_at"]
    pg_ms = int(pg.recorded[0]["verified_at"].timestamp() * 1000)
    assert graph_ms == pg_ms


async def test_reaffirm_unknown_fact_writes_no_row(reaffirm):
    fk, pg = _FakeFalkor(current_ids=[]), _FakePg()
    with pytest.raises(Exception) as exc:
        await reaffirm(fk, pg, "nope")
    assert getattr(exc.value, "status_code", None) == 404
    assert pg.recorded == []


# ---------------------------------------------------------------------------
# Replay reapplies the durable value
# ---------------------------------------------------------------------------

async def test_apply_verifications_restamps_replayed_facts():
    from kwim_api.rebuild import _apply_verifications

    fk = _FakeFalkor(current_ids=["f1", "f2"])
    pg = _FakePg(verifications=[
        {"fact_id": "f1", "last_verified_at": _VERIFIED, "verified_by": "devkey"},
        {"fact_id": "f2", "last_verified_at": _VERIFIED, "verified_by": None},
    ])
    applied = await _apply_verifications(pg, fk, "acme", "kwim_acme_rebuild")
    assert applied == 2
    assert {s["verified_at"] for s in fk.stamped} == {_VERIFIED_MS}
    assert {s["graph_name"] for s in fk.stamped} == {"kwim_acme_rebuild"}


async def test_apply_verifications_skips_non_current_facts():
    from kwim_api.rebuild import _apply_verifications

    fk = _FakeFalkor(current_ids=["f1"])          # f2 replayed as superseded
    pg = _FakePg(verifications=[
        {"fact_id": "f1", "last_verified_at": _VERIFIED},
        {"fact_id": "f2", "last_verified_at": _VERIFIED},
    ])
    assert await _apply_verifications(pg, fk, "acme", None) == 1
    assert [s["fact_id"] for s in fk.stamped] == ["f1"]


async def test_apply_verifications_no_rows():
    from kwim_api.rebuild import _apply_verifications

    fk = _FakeFalkor(current_ids=["f1"])
    assert await _apply_verifications(_FakePg(), fk, "acme", None) == 0
    assert fk.stamped == []


async def test_replayed_reaffirmed_fact_stays_fresh():
    """The user-visible symptom: a recently verified but old fact must not go stale.

    created_at is old and last_verified_at recent.
    """
    from kwim_api.freshness import _to_dt, compute_freshness
    from kwim_api.rebuild import _apply_verifications

    recent = datetime.now(UTC) - timedelta(days=1)
    fk = _FakeFalkor(current_ids=["f1"])
    pg = _FakePg(verifications=[{"fact_id": "f1", "last_verified_at": recent}])
    await _apply_verifications(pg, fk, "acme", None)

    as_of = _to_dt(fk.stamped[0]["verified_at"]).isoformat()
    assert compute_freshness(as_of, "slow", 90, 48) == "fresh"


# ---------------------------------------------------------------------------
# Forget removes the verification row too
# ---------------------------------------------------------------------------

async def test_forget_deletes_fact_verification():
    from kwim_api.forget import execute_forget

    fk, pg = _FakeFalkor(), _FakePg()
    plan = [{"id": "f1", "type": "fact", "status": "current", "label": "x",
             "episodics_to_delete": [], "episodics_shared": []}]
    report = await execute_forget(fk, pg, "acme", plan)
    assert pg.deleted == [["f1"]]
    assert report["verification_rows"] == 1


async def test_forget_rule_skips_verification_delete():
    from kwim_api.forget import execute_forget

    fk, pg = _FakeFalkor(), _FakePg()
    plan = [{"id": "r1", "type": "rule", "status": "approved", "label": "x",
             "episodics_to_delete": [], "episodics_shared": []}]
    report = await execute_forget(fk, pg, "acme", plan)
    assert pg.deleted == []
    assert report["verification_rows"] == 0


# ---------------------------------------------------------------------------
# Preflight covers the new table
# ---------------------------------------------------------------------------

async def test_preflight_fails_without_verification_delete():
    from kwim_api.forget import preflight

    class _Pg:
        async def delete_preflight(self, team):
            return {"role": "kwim_user", "commit_log": True, "episodic": True,
                    "verifications": False}

    assert (await preflight(_Pg(), "acme"))["ok"] is False


async def test_preflight_tolerates_absent_verification_table():
    from kwim_api.forget import preflight

    class _Pg:
        async def delete_preflight(self, team):
            # A schema predating the table reports True (nothing to delete).
            return {"role": "kwim_user", "commit_log": True, "episodic": True,
                    "verifications": True}

    assert (await preflight(_Pg(), "acme"))["ok"] is True
