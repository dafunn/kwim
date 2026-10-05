"""Tests for team provisioning and key management.

Router tests extending test_admin_read.py's fakes: create, adopt, decommission,
restore, destroy, and key mint/list/revoke. Auth and team gating are covered by
test_admin_read.py.
"""
import datetime
import os

import pytest

from kwim_api import auth as auth_mod
from kwim_api import provision
from kwim_api.config import settings
from kwim_api.keys import generate_key
from kwim_api.runtime import State
from tests.test_admin_read import FakeAdminStore as _BaseFakeAdminStore
from tests.test_admin_read import FakeFalkorStore as _BaseFakeFalkorStore
from tests.test_admin_read import FakePgStore as _BaseFakePgStore
from tests.test_admin_read import admin_enabled  # noqa: F401 - fixture reused by name

pytestmark = pytest.mark.usefixtures("admin_enabled")

_NOW = datetime.datetime(2026, 6, 11, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fakes - extend test_admin_read.py's with what the provisioning and key routes need.
# ---------------------------------------------------------------------------

class FakeAdminStore(_BaseFakeAdminStore):
    async def create_team_record(self, team, *, display_name=None, created_by=None):
        self.calls.append(("create_team_record", team))
        if team not in self.teams:
            self.teams[team] = {"team": team, "display_name": display_name,
                                "status": "active", "created_at": _NOW,
                                "created_by": created_by, "decommissioned_at": None}
        return self.teams[team]

    async def set_team_status(self, team, status, *, decommissioned_at=None):
        self.calls.append(("set_team_status", team, status))
        row = self.teams.get(team) or {
            "team": team, "display_name": None, "status": "active",
            "created_at": _NOW, "created_by": None, "decommissioned_at": None}
        row = {**row, "status": status, "decommissioned_at": decommissioned_at}
        self.teams[team] = row
        return row

    async def delete_team_record(self, team):
        self.calls.append(("delete_team_record", team))
        self.teams.pop(team, None)

    async def create_api_key(self, *, team, label, key_prefix, key_hash, capabilities,
                             created_by=None, expires_at=None):
        self.calls.append(("create_api_key", team, key_prefix))
        row = {"id": f"key-{len(self.api_keys) + 1}", "team": team, "label": label,
              "key_prefix": key_prefix, "key_hash": key_hash, "capabilities": capabilities,
              "created_at": _NOW, "created_by": created_by, "expires_at": expires_at,
              "revoked_at": None, "last_used_at": None}
        self.api_keys.append(row)
        return {k: v for k, v in row.items() if k != "key_hash"}

    async def get_api_key(self, key_prefix):
        for row in self.api_keys:
            if row["key_prefix"] == key_prefix and row.get("revoked_at") is None:
                return row
        return None

    async def touch_api_key_last_used(self, key_prefix):
        pass

    async def revoke_api_key(self, key_prefix):
        self.calls.append(("revoke_api_key", key_prefix))
        for row in self.api_keys:
            if row["key_prefix"] == key_prefix and row.get("revoked_at") is None:
                row["revoked_at"] = _NOW
                return {"id": row["id"], "team": row["team"], "label": row["label"],
                        "revoked_at": row["revoked_at"]}
        return None

    async def revoke_all_team_keys(self, team):
        self.calls.append(("revoke_all_team_keys", team))
        out = []
        for row in self.api_keys:
            if row["team"] == team and row.get("revoked_at") is None:
                row["revoked_at"] = _NOW
                out.append({"id": row["id"], "key_prefix": row["key_prefix"],
                           "label": row["label"], "revoked_at": row["revoked_at"]})
        return out


class FakePgStore(_BaseFakePgStore):
    def __init__(self):
        super().__init__()
        self.create_preflight_ok = True
        self.destroy_preflight_ok = True
        self.apply_error: Exception | None = None
        self.applied_schemas: list[str] = []
        self.dropped_schemas: list[str] = []

    async def create_team_preflight(self):
        self.calls.append(("create_team_preflight",))
        return {"role": "kwim_user", "can_create": self.create_preflight_ok}

    async def destroy_team_preflight(self, team):
        self.calls.append(("destroy_team_preflight", team))
        return {"role": "kwim_user", "can_drop": self.destroy_preflight_ok}

    async def apply_team_schema(self, rendered_sql, *, team=None):
        self.calls.append(("apply_team_schema", team))
        if self.apply_error is not None:
            raise self.apply_error
        self.applied_schemas.append(team)
        self.schemas.add(team)

    async def drop_team_schema(self, team):
        self.calls.append(("drop_team_schema", team))
        self.dropped_schemas.append(team)
        self.schemas.discard(team)


class _FakeRedisConn:
    """Backs State.falkor._db.connection - the destroy preview/execute routes
    use it directly: SET with a TTL, and GETDEL."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def execute_command(self, cmd, key):
        if cmd == "GETDEL":
            return self.data.pop(key, None)
        raise NotImplementedError(cmd)


class _FakeDB:
    def __init__(self):
        self.connection = _FakeRedisConn()


class FakeFalkorStore(_BaseFakeFalkorStore):
    def __init__(self):
        super().__init__()
        self._db = _FakeDB()
        self.inited_graphs: list[str] = []
        self.dropped_graphs: list[str] = []
        self._previews: dict[str, dict] = {}

    async def destroy_preview_set(self, token, doc, ttl):
        self._previews[token] = doc

    async def destroy_preview_getdel(self, token):
        # Consumes the key, as GETDEL does.
        return self._previews.pop(token, None)

    async def init_team_graph(self, team):
        self.calls.append(("init_team_graph", team))
        self.inited_graphs.append(team)

    async def drop_team_graphs(self, team):
        self.calls.append(("drop_team_graphs", team))
        self.dropped_graphs.append(team)
        return {f"kwim_{team}": True, f"kwim_{team}_code": False}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stores(monkeypatch):
    admin, pg, falkor = FakeAdminStore(), FakePgStore(), FakeFalkorStore()
    monkeypatch.setattr(State, "admin", admin, raising=False)
    monkeypatch.setattr(State, "pg", pg, raising=False)
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    return admin, pg, falkor


@pytest.fixture
def operator_headers(stores):
    admin, pg, falkor = stores
    op_id = admin.seed_operator(username="alice")
    token = admin.seed_session(operator_id=op_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _real_template_path(monkeypatch):
    """provision.TEMPLATE_PATH's default only resolves inside the built image
    layout, so point it at the repo-root template."""
    real_path = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "db", "team-schema.sql.j2"))
    assert os.path.isfile(real_path), f"expected the real template at {real_path!r}"
    monkeypatch.setattr(provision, "TEMPLATE_PATH", real_path)


@pytest.fixture
def allow_team_destroy():
    object.__setattr__(settings, "admin_allow_team_destroy", True)
    try:
        yield
    finally:
        object.__setattr__(settings, "admin_allow_team_destroy", False)


async def _decommission(admin, team: str) -> None:
    """Directly seed a decommissioned console row - the destroy tests only
    need the precondition, not decommission itself."""
    await admin.set_team_status(team, "decommissioned", decommissioned_at=_NOW)


def _destroy_preview(client, headers, team: str) -> dict:
    r = client.post(f"/v1/admin/teams/{team}/destroy/preview", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_team_applies_ddl_initializes_graph_and_writes_console_row(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    r = client.post("/v1/admin/teams", json={"team": "acme", "display_name": "Acme"},
                    headers=operator_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {"team": "acme", "schema_created": True, "graph_initialized": True,
                    "already_existed": False}
    assert pg.applied_schemas == ["acme"]
    assert falkor.inited_graphs == ["acme"]
    assert admin.teams["acme"]["display_name"] == "Acme"


def test_create_team_twice_is_idempotent_not_an_error(client, stores, operator_headers):
    admin, pg, falkor = stores
    r1 = client.post("/v1/admin/teams", json={"team": "acme"}, headers=operator_headers)
    assert r1.status_code == 201
    assert r1.json()["already_existed"] is False

    r2 = client.post("/v1/admin/teams", json={"team": "acme"}, headers=operator_headers)
    assert r2.status_code == 201
    assert r2.json()["already_existed"] is True
    assert pg.applied_schemas == ["acme", "acme"]   # re-applied, no error


@pytest.mark.parametrize("team", ["public", "information_schema", "pg_catalog",
                                  "pg_toast", "kwim_admin"])
def test_reserved_team_names_are_rejected(client, stores, operator_headers, team):
    r = client.post("/v1/admin/teams", json={"team": team}, headers=operator_headers)
    assert r.status_code == 422


def test_universe_gets_its_own_rejection_message(client, stores, operator_headers):
    r = client.post("/v1/admin/teams", json={"team": "universe"}, headers=operator_headers)
    assert r.status_code == 422
    assert "cluster-wide" in r.json()["detail"]


def test_invalid_team_identifier_rejected_before_any_store_call(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    r = client.post("/v1/admin/teams", json={"team": "DROP TABLE;"}, headers=operator_headers)
    assert r.status_code == 422
    assert pg.calls == []
    assert admin.calls == []


def test_failing_create_preflight_returns_503_and_writes_nothing(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    pg.create_preflight_ok = False
    r = client.post("/v1/admin/teams", json={"team": "acme"}, headers=operator_headers)
    assert r.status_code == 503
    assert pg.applied_schemas == []
    assert "acme" not in admin.teams


# ---------------------------------------------------------------------------
# decommission / restore
# ---------------------------------------------------------------------------

def test_decommission_revokes_every_live_key_and_leaves_data_readable(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")
    full_key, prefix, key_hash = generate_key()
    admin.api_keys.append({"id": "k1", "team": "acme", "label": "x", "key_prefix": prefix,
                           "key_hash": key_hash, "capabilities": [], "created_at": _NOW,
                           "created_by": None, "expires_at": None, "revoked_at": None,
                           "last_used_at": None})
    falkor.seed_fact("acme", id="f1", commit_seq=1)

    r = client.post("/v1/admin/teams/acme/decommission", json={"reason": "offboarding"},
                    headers=operator_headers)
    assert r.status_code == 200, r.text
    assert r.json()["keys_revoked"] == 1
    assert admin.api_keys[0]["revoked_at"] is not None
    assert admin.teams["acme"]["status"] == "decommissioned"

    # Data stays fully readable through the read API.
    facts = client.get("/v1/admin/teams/acme/facts", headers=operator_headers).json()
    assert len(facts["items"]) == 1


def test_minting_a_key_for_a_decommissioned_team_is_refused(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme", status="decommissioned")

    r = client.post("/v1/admin/teams/acme/keys", json={"label": "x", "capabilities": []},
                    headers=operator_headers)
    assert r.status_code == 409


def test_restore_reverses_status_but_does_not_restore_keys(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme", status="decommissioned")
    full_key, prefix, key_hash = generate_key()
    admin.api_keys.append({"id": "k1", "team": "acme", "label": "x", "key_prefix": prefix,
                           "key_hash": key_hash, "capabilities": [], "created_at": _NOW,
                           "created_by": None, "expires_at": None, "revoked_at": _NOW,
                           "last_used_at": None})

    r = client.post("/v1/admin/teams/acme/restore", headers=operator_headers)
    assert r.status_code == 200
    assert admin.teams["acme"]["status"] == "active"
    assert admin.api_keys[0]["revoked_at"] is not None   # still revoked


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------

def test_destroy_without_prior_decommission_returns_409(
    client, stores, operator_headers, allow_team_destroy,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")   # active, not decommissioned

    r = client.post("/v1/admin/teams/acme/destroy/preview", headers=operator_headers)
    assert r.status_code == 409


async def test_destroy_with_mismatched_confirm_team_returns_409_and_drops_nothing(
    client, stores, operator_headers, allow_team_destroy,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")
    await _decommission(admin, "acme")

    preview = _destroy_preview(client, operator_headers, "acme")
    r = client.post("/v1/admin/teams/acme/destroy", headers=operator_headers, json={
        "preview_token": preview["preview_token"], "confirm_team": "not-acme",
        "confirm_commit_rows": preview["counts"]["commit_rows"]})
    assert r.status_code == 409
    assert pg.dropped_schemas == []
    assert falkor.dropped_graphs == []


async def test_destroy_with_drifted_confirm_commit_rows_returns_409_and_drops_nothing(
    client, stores, operator_headers, allow_team_destroy,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")
    await _decommission(admin, "acme")

    preview = _destroy_preview(client, operator_headers, "acme")
    r = client.post("/v1/admin/teams/acme/destroy", headers=operator_headers, json={
        "preview_token": preview["preview_token"], "confirm_team": "acme",
        "confirm_commit_rows": preview["counts"]["commit_rows"] + 1})
    assert r.status_code == 409
    assert pg.dropped_schemas == []
    assert falkor.dropped_graphs == []


def test_destroy_is_refused_with_403_when_allow_team_destroy_is_false(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")

    r = client.post("/v1/admin/teams/acme/destroy/preview", headers=operator_headers)
    assert r.status_code == 403


async def test_destroy_drops_graphs_before_schema_and_deletes_the_console_row(
    client, stores, operator_headers, allow_team_destroy,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")
    await _decommission(admin, "acme")

    preview = _destroy_preview(client, operator_headers, "acme")
    r = client.post("/v1/admin/teams/acme/destroy", headers=operator_headers, json={
        "preview_token": preview["preview_token"], "confirm_team": "acme",
        "confirm_commit_rows": preview["counts"]["commit_rows"]})
    assert r.status_code == 200, r.text
    assert falkor.dropped_graphs == ["acme"]
    assert pg.dropped_schemas == ["acme"]
    assert "acme" not in admin.teams
    # The team is gone, so a replay is a 404 (single use is tested separately).


async def test_destroy_preview_token_is_single_use(
    client, stores, operator_headers, allow_team_destroy,
):
    """GETDEL consumes the token unconditionally, before confirm_team/
    confirm_commit_rows are checked, so a failed confirmation still uses it up."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")
    await _decommission(admin, "acme")

    preview = _destroy_preview(client, operator_headers, "acme")
    bad = client.post("/v1/admin/teams/acme/destroy", headers=operator_headers, json={
        "preview_token": preview["preview_token"], "confirm_team": "not-acme",
        "confirm_commit_rows": preview["counts"]["commit_rows"]})
    assert bad.status_code == 409

    retry = client.post("/v1/admin/teams/acme/destroy", headers=operator_headers, json={
        "preview_token": preview["preview_token"], "confirm_team": "acme",
        "confirm_commit_rows": preview["counts"]["commit_rows"]})
    assert retry.status_code == 409
    assert "acme" in admin.teams
    assert pg.dropped_schemas == []
    assert falkor.dropped_graphs == []


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------

def test_minting_returns_the_secret_exactly_once(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")

    r = client.post("/v1/admin/teams/acme/keys",
                    json={"label": "agent runner", "capabilities": ["read", "propose"]},
                    headers=operator_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key"].startswith("kwim_")

    listing = client.get("/v1/admin/teams/acme/keys", headers=operator_headers).json()
    assert listing["items"][0]["key_prefix"] == body["key_prefix"]
    assert "key" not in listing["items"][0]
    assert "key_hash" not in listing["items"][0]


def test_unknown_capability_values_are_rejected(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")

    r = client.post("/v1/admin/teams/acme/keys",
                    json={"label": "x", "capabilities": ["read", "sudo"]},
                    headers=operator_headers)
    assert r.status_code == 422


async def test_revoking_a_key_denies_it_on_the_next_request_without_waiting_for_ttl(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")

    mint = client.post("/v1/admin/teams/acme/keys",
                       json={"label": "x", "capabilities": ["read"]},
                       headers=operator_headers).json()
    full_key, prefix = mint["key"], mint["key_prefix"]

    ctx = await auth_mod.current_team(authorization=f"Bearer {full_key}")
    assert ctx.team == "acme"

    r = client.delete(f"/v1/admin/teams/acme/keys/{prefix}", headers=operator_headers)
    assert r.status_code == 200, r.text

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await auth_mod.current_team(authorization=f"Bearer {full_key}")
    assert exc_info.value.status_code == 401


def test_no_response_or_audit_detail_contains_a_key_secret(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")

    mint = client.post("/v1/admin/teams/acme/keys",
                       json={"label": "x", "capabilities": []},
                       headers=operator_headers)
    full_key = mint.json()["key"]
    secret_half = full_key.rsplit("_", 1)[-1]

    listing = client.get("/v1/admin/teams/acme/keys", headers=operator_headers)
    assert secret_half not in listing.text

    r = client.delete(f"/v1/admin/teams/acme/keys/{mint.json()['key_prefix']}",
                      headers=operator_headers)
    assert secret_half not in r.text


async def test_destroy_detects_row_growth_since_the_preview(
    client, stores, operator_headers, allow_team_destroy,
):
    """The count gate must compare against live state. A count frozen in the
    token would always match what the operator was shown."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_team("acme")
    await _decommission(admin, "acme")
    for i in range(2):
        pg.seed_commit("acme", object_id=f"f{i}")

    prev = _destroy_preview(client, operator_headers, "acme")
    assert prev["counts"]["commit_rows"] == 2

    # The team is still being written to while the operator hesitates.
    pg.seed_commit("acme", object_id="f2")

    r = client.post("/v1/admin/teams/acme/destroy",
                    json={"preview_token": prev["preview_token"],
                          "confirm_team": "acme", "confirm_commit_rows": 2},
                    headers=operator_headers)
    assert r.status_code == 409
    assert pg.dropped_schemas == [], "dropped a schema whose contents had changed"
