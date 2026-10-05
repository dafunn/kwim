"""Tests for the kwim_admin-backed key store and operator/session resolution.

Covers current_team resolving
store-format keys against a fake AdminStore, the two failure cases that must
behave differently (a well-formed-but-absent key vs. a missing schema), cache
invalidation, and the require_capability gate.

Also covers admin_auth.py's current_operator, the /v1/admin/session
login/logout route, principal separation (a team key must never satisfy
current_operator and an operator session must never satisfy current_team), and
the 503s returned when admin is disabled or its schema is missing.
"""
import datetime
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException

from kwim_api import auth
from kwim_api.admin_auth import AdminContext, current_operator, hash_token
from kwim_api.auth import TeamContext, current_team, invalidate_key_cache, require_capability
from kwim_api.config import settings
from kwim_api.keys import generate_key, hash_password, parse_key
from kwim_api.runtime import State

_NOW = datetime.datetime(2026, 6, 11, 12, 0, 0, tzinfo=datetime.UTC)


class FakeAdminStore:
    """In-memory kwim_admin.*, mirroring AdminStore's WHERE-clause semantics for
    api_keys, operators, and sessions."""

    def __init__(self):
        self.rows: dict[str, dict] = {}          # api keys, by key_prefix
        self.schema_missing = False
        self.touched: list[str] = []
        self.operators: dict[str, dict] = {}     # by username
        self.operators_by_id: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}      # by token_hash
        self.audit: list[dict] = []
        self._next_id = 1

    # --- api keys ---

    def seed(self, *, team, key_prefix, key_hash, capabilities=(), expires_at=None,
            revoked_at=None):
        self.rows[key_prefix] = {
            "team": team, "label": "test", "key_prefix": key_prefix, "key_hash": key_hash,
            "capabilities": list(capabilities), "expires_at": expires_at,
            "revoked_at": revoked_at, "last_used_at": None,
        }

    async def get_api_key(self, key_prefix):
        self._check_schema()
        row = self.rows.get(key_prefix)
        if row is None or row["revoked_at"] is not None:
            return None
        if row["expires_at"] is not None and row["expires_at"] <= _NOW:
            return None
        return row

    async def touch_api_key_last_used(self, key_prefix):
        self.touched.append(key_prefix)
        if key_prefix in self.rows:
            self.rows[key_prefix]["last_used_at"] = _NOW

    # --- operators ---

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    def seed_operator(self, *, username, password, is_active=True):
        op_id = self._new_id("op")
        row = {"id": op_id, "username": username, "password_hash": hash_password(password),
              "is_active": is_active}
        self.operators[username] = row
        self.operators_by_id[op_id] = row
        return op_id

    async def get_operator_by_username(self, username):
        self._check_schema()
        row = self.operators.get(username)
        return dict(row) if row else None

    async def touch_operator_login(self, operator_id):
        pass

    # --- sessions ---

    def seed_session(self, *, operator_id, expires_at, revoked_at=None):
        # Real "now": the session max-age cap is computed from the wall clock.
        token = f"session-token-{self._new_id('tok')}"
        self.sessions[hash_token(token)] = {
            "id": self._new_id("sess"), "operator_id": operator_id,
            "created_at": datetime.datetime.now(datetime.UTC),
            "expires_at": expires_at, "revoked_at": revoked_at,
        }
        return token

    async def create_session(self, *, operator_id, token_hash, expires_at, user_agent=None):
        self._check_schema()
        created_at = datetime.datetime.now(datetime.UTC)
        row = {"id": self._new_id("sess"), "operator_id": operator_id, "created_at": created_at,
              "expires_at": expires_at, "revoked_at": None}
        self.sessions[token_hash] = row
        return {"id": row["id"], "operator_id": operator_id, "created_at": created_at,
               "expires_at": expires_at}

    async def get_session(self, token_hash):
        self._check_schema()
        row = self.sessions.get(token_hash)
        if row is None or row["revoked_at"] is not None:
            return None
        if row["expires_at"] <= datetime.datetime.now(datetime.UTC):
            return None
        op = self.operators_by_id.get(row["operator_id"])
        if op is None or not op["is_active"]:
            return None
        return {"id": row["id"], "operator_id": row["operator_id"], "username": op["username"],
               "created_at": row["created_at"], "expires_at": row["expires_at"]}

    async def refresh_session(self, token_hash, expires_at):
        if token_hash in self.sessions:
            self.sessions[token_hash]["expires_at"] = expires_at

    async def revoke_session(self, token_hash):
        row = self.sessions.get(token_hash)
        if row is None or row["revoked_at"] is not None:
            return None
        row["revoked_at"] = _NOW
        return {"id": row["id"], "operator_id": row["operator_id"]}

    # --- audit ---

    async def record(self, **kwargs):
        self.audit.append(kwargs)

    def _check_schema(self):
        if self.schema_missing:
            raise psycopg.errors.UndefinedTable("relation kwim_admin does not exist")


@pytest.fixture
def admin_store(monkeypatch):
    fake = FakeAdminStore()
    monkeypatch.setattr(State, "admin", fake, raising=False)
    return fake


@pytest.fixture
def admin_enabled():
    """settings is a frozen dataclass singleton shared by the whole test session -
    set admin_enabled for one test with object.__setattr__ and restore it."""
    object.__setattr__(settings, "admin_enabled", True)
    try:
        yield
    finally:
        object.__setattr__(settings, "admin_enabled", False)


def _bearer(full_key: str) -> str:
    return f"Bearer {full_key}"


async def _current_operator(authorization: str) -> AdminContext:
    """current_operator needs a Request for the cookie fallback; these tests
    use the Authorization header, so a stand-in with a cookies dict is enough."""
    return await current_operator(request=SimpleNamespace(cookies={}), authorization=authorization)


# ---------------------------------------------------------------------------
# Resolution and capabilities
# ---------------------------------------------------------------------------

async def test_valid_key_resolves_to_team_and_capabilities(admin_store):
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash,
                     capabilities=["review", "promote"])

    ctx = await current_team(authorization=_bearer(full_key))

    assert ctx == TeamContext(team="acme", key_id=prefix,
                              capabilities=frozenset({"review", "promote"}))
    # last_used_at is bumped on the cache-filling call - throttled by the cache,
    # not once per authenticated request.
    assert admin_store.touched == [prefix]


async def test_revoked_key_returns_401(admin_store):
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash, revoked_at=_NOW)

    with pytest.raises(HTTPException) as exc_info:
        await current_team(authorization=_bearer(full_key))
    assert exc_info.value.status_code == 401


async def test_expired_key_returns_401(admin_store):
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash,
                     expires_at=_NOW - datetime.timedelta(seconds=1))

    with pytest.raises(HTTPException) as exc_info:
        await current_team(authorization=_bearer(full_key))
    assert exc_info.value.status_code == 401


async def test_wrong_secret_returns_401(admin_store):
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash)

    _, secret = parse_key(full_key)
    flipped = ("a" if secret[0] != "a" else "b") + secret[1:]
    tampered = f"kwim_{prefix}_{flipped}"

    with pytest.raises(HTTPException) as exc_info:
        await current_team(authorization=_bearer(tampered))
    assert exc_info.value.status_code == 401


async def test_well_formed_key_absent_from_store_is_401_no_fallback(admin_store, monkeypatch):
    """A store miss (schema present, row absent) is a straight 401 - it must not
    fall back to KWIM_API_KEYS, even with a legacy mapping for the same value."""
    full_key, _prefix, _key_hash = generate_key()  # never seeded in the store
    monkeypatch.setitem(auth._KEY_MAP, full_key, "sneaky-team")

    with pytest.raises(HTTPException) as exc_info:
        await current_team(authorization=_bearer(full_key))
    assert exc_info.value.status_code == 401


async def test_missing_schema_falls_back_to_env_map(admin_store):
    """Agent traffic survives a deploy that runs ahead of the provisioner."""
    admin_store.schema_missing = True

    ctx = await current_team(authorization="Bearer devkey")

    assert ctx.team == "acme"


async def test_wrong_secret_still_401s_against_a_cached_prefix(admin_store):
    """The cache must skip the Postgres round trip, never the secret comparison.
    A cache hit with the right prefix and a wrong secret is a 401."""
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash)

    first = await current_team(authorization=_bearer(full_key))
    assert first.team == "acme"  # cache is now populated for this prefix

    _, secret = parse_key(full_key)
    flipped = ("a" if secret[0] != "a" else "b") + secret[1:]
    tampered = f"kwim_{prefix}_{flipped}"

    with pytest.raises(HTTPException) as exc_info:
        await current_team(authorization=_bearer(tampered))
    assert exc_info.value.status_code == 401


async def test_revocation_clears_the_cache_without_waiting_for_the_ttl(admin_store):
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash)

    first = await current_team(authorization=_bearer(full_key))
    assert first.team == "acme"

    admin_store.rows[prefix]["revoked_at"] = _NOW
    invalidate_key_cache(prefix)

    with pytest.raises(HTTPException) as exc_info:
        await current_team(authorization=_bearer(full_key))
    assert exc_info.value.status_code == 401


def test_require_capability_gates_on_capability_membership():
    granted = TeamContext(team="acme", key_id="PREFIXPREFIX1", capabilities=frozenset({"review"}))
    ungranted = TeamContext(team="acme", key_id="PREFIXPREFIX1", capabilities=frozenset())

    require_capability(granted, "review")  # does not raise

    with pytest.raises(HTTPException) as exc_info:
        require_capability(ungranted, "review")
    assert exc_info.value.status_code == 403


def test_review_route_403_without_review_capability(client, admin_store):
    """require_capability short-circuits before touching any store, so a key
    without `review` needs no fake pg/falkor/gate to prove the 403."""
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash, capabilities=[])

    r = client.post("/v1/review/some-proposal/approve",
                    headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# The auth path must survive its own bookkeeping
# ---------------------------------------------------------------------------

async def test_last_used_write_failure_does_not_fail_the_request(admin_store):
    """last_used_at is bookkeeping nobody reads in real time. A failed write on
    the request path must not turn a valid key into a 500."""
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash)

    async def boom(_prefix):
        raise psycopg.OperationalError("connection reset during bookkeeping UPDATE")

    admin_store.touch_api_key_last_used = boom
    ctx = await current_team(f"Bearer {full_key}")
    assert ctx.team == "acme"


async def test_legacy_fallback_logs_once_per_key(caplog, monkeypatch):
    """Before migration every request takes the legacy path; a line per call
    would bury the signal rather than give it."""
    monkeypatch.setattr(auth, "_KEY_MAP", {"legacykey": "acme"})
    monkeypatch.setattr(auth, "_legacy_reported", set())
    with caplog.at_level("INFO", logger="kwim_api.auth"):
        for _ in range(5):
            assert (await current_team("Bearer legacykey")).team == "acme"
    lines = [r for r in caplog.records if "legacy KWIM_API_KEYS" in r.getMessage()]
    assert len(lines) == 1, f"expected one line, got {len(lines)}"


# ---------------------------------------------------------------------------
# Operator sessions (admin_auth.py)
# ---------------------------------------------------------------------------

async def test_current_operator_resolves_a_live_session(admin_store, admin_enabled):
    op_id = admin_store.seed_operator(username="alice", password="hunter2")
    token = admin_store.seed_session(
        operator_id=op_id, expires_at=_future(hours=1))

    ctx = await _current_operator(f"Bearer {token}")

    assert ctx == AdminContext(operator_id=op_id, username="alice")


async def test_current_operator_refreshes_the_session_on_use(admin_store, admin_enabled):
    op_id = admin_store.seed_operator(username="alice", password="hunter2")
    token = admin_store.seed_session(operator_id=op_id, expires_at=_future(minutes=1))
    original_expiry = admin_store.sessions[hash_token(token)]["expires_at"]

    await _current_operator(f"Bearer {token}")

    assert admin_store.sessions[hash_token(token)]["expires_at"] > original_expiry


async def test_expired_session_returns_401(admin_store, admin_enabled):
    op_id = admin_store.seed_operator(username="alice", password="hunter2")
    token = admin_store.seed_session(operator_id=op_id, expires_at=_past(seconds=1))

    with pytest.raises(HTTPException) as exc_info:
        await _current_operator(f"Bearer {token}")
    assert exc_info.value.status_code == 401


async def test_login_returns_a_token_that_authenticates_and_logout_revokes_it(
    client, admin_store, admin_enabled,
):
    admin_store.seed_operator(username="alice", password="hunter2")

    r = client.post("/v1/admin/session", json={"username": "alice", "password": "hunter2"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert "kwim_admin_session" in r.headers.get("set-cookie", "")

    ctx = await _current_operator(f"Bearer {token}")
    assert ctx.username == "alice"
    assert admin_store.audit[-1]["action"] == "admin.login"
    assert admin_store.audit[-1]["result"] == "ok"

    r = client.delete("/v1/admin/session", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204

    with pytest.raises(HTTPException) as exc_info:
        await _current_operator(f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert admin_store.audit[-1]["action"] == "admin.logout"


def test_failed_logins_lock_out_after_the_threshold(client, admin_store, admin_enabled):
    admin_store.seed_operator(username="alice", password="hunter2")

    for _ in range(settings.admin_login_max_failures):
        r = client.post("/v1/admin/session", json={"username": "alice", "password": "wrong"})
        assert r.status_code == 401

    r = client.post("/v1/admin/session", json={"username": "alice", "password": "wrong"})
    assert r.status_code == 429

    # The correct password is locked out too.
    r = client.post("/v1/admin/session", json={"username": "alice", "password": "hunter2"})
    assert r.status_code == 429


def test_login_against_nonexistent_username_matches_wrong_password_response(
    client, admin_store, admin_enabled,
):
    admin_store.seed_operator(username="alice", password="hunter2")

    wrong_password = client.post("/v1/admin/session",
                                 json={"username": "alice", "password": "wrong"})
    nonexistent_user = client.post("/v1/admin/session",
                                   json={"username": "nobody", "password": "wrong"})

    assert wrong_password.status_code == nonexistent_user.status_code == 401
    assert wrong_password.json() == nonexistent_user.json()


# ---------------------------------------------------------------------------
# Principal separation, both directions
# ---------------------------------------------------------------------------

async def test_team_key_does_not_satisfy_current_operator(admin_store, admin_enabled):
    """The wrong kind of credential is a 401, not a 403 - it isn't a permission
    failure, current_operator doesn't even look at kwim_admin.api_keys."""
    full_key, prefix, key_hash = generate_key()
    admin_store.seed(team="acme", key_prefix=prefix, key_hash=key_hash)

    with pytest.raises(HTTPException) as exc_info:
        await _current_operator(f"Bearer {full_key}")
    assert exc_info.value.status_code == 401


async def test_operator_session_does_not_satisfy_current_team(admin_store):
    op_id = admin_store.seed_operator(username="alice", password="hunter2")
    token = admin_store.seed_session(operator_id=op_id, expires_at=_future(hours=1))

    with pytest.raises(HTTPException) as exc_info:
        await current_team(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# admin_enabled and missing-schema 503s
# ---------------------------------------------------------------------------

def test_admin_disabled_by_default_blocks_login(client, admin_store):
    """admin_enabled defaults to false - no admin_enabled fixture here."""
    r = client.post("/v1/admin/session", json={"username": "alice", "password": "x"})
    assert r.status_code == 503


async def test_admin_disabled_blocks_current_operator(admin_store):
    with pytest.raises(HTTPException) as exc_info:
        await _current_operator("Bearer whatever")
    assert exc_info.value.status_code == 503


def test_missing_schema_503s_login_naming_the_provisioning_step(client, admin_store, admin_enabled):
    admin_store.schema_missing = True

    r = client.post("/v1/admin/session", json={"username": "alice", "password": "x"})

    assert r.status_code == 503
    assert "admin-schema.sql" in r.json()["detail"]


async def test_missing_schema_503s_current_operator(admin_store, admin_enabled):
    admin_store.schema_missing = True

    with pytest.raises(HTTPException) as exc_info:
        await _current_operator("Bearer whatever")
    assert exc_info.value.status_code == 503
    assert "admin-schema.sql" in exc_info.value.detail


def _future(**kwargs) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(**kwargs)


def _past(**kwargs) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(**kwargs)


# ---------------------------------------------------------------------------
# Lockout bucketing and bookkeeping
# ---------------------------------------------------------------------------

def test_one_client_cannot_lock_out_another_behind_a_proxy(client, admin_store,
                                                           admin_enabled):
    """Behind an ingress every request carries the proxy address. If the counter
    bucketed on that, failures from one client would lock out every operator."""
    admin_store.seed_operator(username="alice", password="hunter2")
    attacker = {"X-Forwarded-For": "203.0.113.9"}
    operator = {"X-Forwarded-For": "203.0.113.10"}

    for i in range(settings.admin_login_max_failures + 2):
        client.post("/v1/admin/session",
                    json={"username": f"probe{i}", "password": "wrong"}, headers=attacker)

    r = client.post("/v1/admin/session",
                    json={"username": "alice", "password": "hunter2"}, headers=operator)
    assert r.status_code == 200, "an unrelated client was locked out by another's failures"


def test_lockout_state_does_not_accumulate(client, admin_store, admin_enabled):
    """The username half of the key is attacker-supplied, so a table that only
    ever grew would leak memory on unauthenticated input."""
    from kwim_api.routers import admin as admin_router

    admin_router._failures.clear()
    for i in range(50):
        client.post("/v1/admin/session",
                    json={"username": f"nobody-{i}", "password": "wrong"},
                    headers={"X-Forwarded-For": f"198.51.100.{i % 200}"})
    live = {k: v for k, v in admin_router._failures.items() if v}
    assert len(admin_router._failures) == len(live), "empty windows were retained"


def test_denied_login_does_not_record_an_unknown_username(client, admin_store,
                                                          admin_enabled):
    """A password typed into the username field is a common slip. It must not be
    what lands in the audit log."""
    client.post("/v1/admin/session",
                json={"username": "hunter2-my-actual-password", "password": "x"})
    denied = [r for r in admin_store.audit if r["result"] == "denied"]
    assert denied, "a failed login should be audited"
    assert denied[-1]["detail"] == {"username": "<unknown>"}


def test_denied_login_records_a_real_username(client, admin_store, admin_enabled):
    """The signal worth keeping is which real account is under attack."""
    admin_store.seed_operator(username="alice", password="hunter2")
    client.post("/v1/admin/session", json={"username": "alice", "password": "wrong"})
    denied = [r for r in admin_store.audit if r["result"] == "denied"]
    assert denied[-1]["detail"] == {"username": "alice"}


def test_lockout_is_audited(client, admin_store, admin_enabled):
    admin_store.seed_operator(username="alice", password="hunter2")
    for _ in range(settings.admin_login_max_failures + 1):
        client.post("/v1/admin/session", json={"username": "alice", "password": "wrong"})
    assert any(r["detail"].get("reason") == "locked_out"
               for r in admin_store.audit if r["result"] == "denied")


# ---------------------------------------------------------------------------
# Cookie form: name and Secure follow admin.secure_cookie. The assertions check
# the full name, since the bare one matches both forms.
# ---------------------------------------------------------------------------

def _set_secure_cookie(monkeypatch, value: bool) -> None:
    """Settings is a frozen dataclass, so swap each module's reference to a
    replaced copy rather than mutating the shared instance."""
    import dataclasses

    from kwim_api import admin_auth
    from kwim_api.routers import admin as admin_router

    replaced = dataclasses.replace(settings, admin_secure_cookie=value)
    monkeypatch.setattr(admin_auth, "settings", replaced)
    monkeypatch.setattr(admin_router, "settings", replaced)


def test_secure_cookie_uses_the_host_prefix_and_secure(client, admin_store,
                                                       admin_enabled, monkeypatch):
    _set_secure_cookie(monkeypatch, True)
    admin_store.seed_operator(username="alice", password="hunter2")

    r = client.post("/v1/admin/session", json={"username": "alice", "password": "hunter2"})
    set_cookie = r.headers.get("set-cookie", "")

    assert "__Host-kwim_admin_session=" in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie.replace("samesite", "SameSite")
    # __Host- is only honoured by browsers with Path=/ and no Domain.
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie


def test_insecure_cookie_drops_both_the_prefix_and_secure(client, admin_store,
                                                          admin_enabled, monkeypatch):
    """The fallback has to actually work over plain HTTP: a __Host- name or a
    Secure attribute is dropped by browsers there."""
    _set_secure_cookie(monkeypatch, False)
    admin_store.seed_operator(username="bob", password="hunter2")

    r = client.post("/v1/admin/session", json={"username": "bob", "password": "hunter2"})
    set_cookie = r.headers.get("set-cookie", "")

    assert "kwim_admin_session=" in set_cookie
    assert "__Host-" not in set_cookie
    assert "Secure" not in set_cookie
    assert "HttpOnly" in set_cookie


def test_the_resolver_reads_whichever_name_is_configured(admin_store, admin_enabled,
                                                         monkeypatch):
    """Login and current_operator must agree on the name, or a cookie-authenticated
    session silently stops resolving."""
    from kwim_api.admin_auth import cookie_name

    _set_secure_cookie(monkeypatch, True)
    assert cookie_name() == "__Host-kwim_admin_session"
    _set_secure_cookie(monkeypatch, False)
    assert cookie_name() == "kwim_admin_session"
