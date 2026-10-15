"""Capability enforcement on the team-key routes.

Every team-key route requires `read` or `propose`. A request carrying the
capability gets past the check and into the handler, where every store call
raises `_Reached`; one without it gets a 403 before any store is touched.
Legacy KWIM_API_KEYS keys carry `read` and `propose` whatever the promote and
review lists say.
"""
import pytest

from kwim_api.keys import generate_key
from kwim_api.runtime import State
from tests.test_admin_identity import FakeAdminStore

READ_ROUTES = [
    ("GET", "/v1/knowledge/query", None),
    ("GET", "/v1/knowledge/search?q=x", None),
    ("GET", "/v1/knowledge/facts/f1", None),
    ("GET", "/v1/knowledge/audit/f1", None),
    ("GET", "/v1/memory/episodic", None),
    ("GET", "/v1/memory/context?session_id=s1", None),
    ("GET", "/v1/memory/semantic?q=x", None),
    ("GET", "/v1/memory/working/s1/k1", None),
    ("GET", "/v1/wisdom/rules", None),
    ("POST", "/v1/wisdom/check", {"action": {"content": "x"}}),
    ("GET", "/v1/proposals/p1", None),
    ("GET", "/v1/code/search?name=f", None),
    ("GET", "/v1/code/trace/fn1", None),
    ("GET", "/v1/code/architecture", None),
    ("GET", "/v1/code/changes?repo=r&commit=c", None),
    ("GET", "/v1/review/pending", None),
]

PROPOSE_ROUTES = [
    ("POST", "/v1/knowledge/propose", {"statement": "s", "fact_type": "t"}),
    ("POST", "/v1/knowledge/facts/f1/reaffirm", None),
    ("POST", "/v1/wisdom/propose", {"situation": {"task_type": "t"}, "approach": "a"}),
    ("POST", "/v1/memory/episodic", {"agent_id": "a", "session_id": "s", "event_type": "e"}),
    ("POST", "/v1/memory/semantic", {"content": "c"}),
    ("PUT", "/v1/memory/working/s1/k1", {"value": "v"}),
]

_ALL = ["read", "propose", "review", "promote"]


class _Reached(Exception):
    """Raised by every store call: the request got past the capability check."""


class _Unreachable:
    def __getattr__(self, name):
        async def call(*args, **kwargs):
            raise _Reached(name)
        return call


@pytest.fixture
def stores(monkeypatch, client):
    for name in ("pg", "falkor", "bus", "embedder"):
        monkeypatch.setattr(State, name, _Unreachable(), raising=False)
    monkeypatch.setattr(client.app.state, "gate", _Unreachable(), raising=False)
    admin = FakeAdminStore()
    monkeypatch.setattr(State, "admin", admin, raising=False)
    return admin


def _store_key(admin, capabilities) -> str:
    full_key, prefix, key_hash = generate_key()
    admin.seed(team="acme", key_prefix=prefix, key_hash=key_hash, capabilities=capabilities)
    return full_key


# Past the check: a store call raised, or knowledge/search turned the embedder's
# failure into a 503.
_PASSED = ("reached", 503)


def _call(client, key, method, path, body):
    """The status code, or "reached" when the handler ran into a store."""
    try:
        r = client.request(method, path, json=body, headers={"Authorization": f"Bearer {key}"})
    except _Reached:
        return "reached"
    return r.status_code


@pytest.mark.parametrize("method,path,body", READ_ROUTES)
def test_read_route_requires_read(client, stores, method, path, body):
    without = _store_key(stores, [c for c in _ALL if c != "read"])
    assert _call(client, without, method, path, body) == 403
    assert _call(client, _store_key(stores, ["read"]), method, path, body) in _PASSED


@pytest.mark.parametrize("method,path,body", PROPOSE_ROUTES)
def test_propose_route_requires_propose(client, stores, method, path, body):
    without = _store_key(stores, [c for c in _ALL if c != "propose"])
    assert _call(client, without, method, path, body) == 403
    assert _call(client, _store_key(stores, ["propose"]), method, path, body) in _PASSED


@pytest.mark.parametrize("method,path,body", READ_ROUTES + PROPOSE_ROUTES)
def test_key_with_no_capabilities_is_refused(client, stores, method, path, body):
    assert _call(client, _store_key(stores, []), method, path, body) == 403


@pytest.mark.parametrize("method,path,body", READ_ROUTES + PROPOSE_ROUTES)
def test_legacy_key_reads_and_proposes_without_a_list_entry(client, stores, method, path, body):
    """otherkey is in neither the promote nor the review list."""
    assert _call(client, "otherkey", method, path, body) in _PASSED


@pytest.mark.parametrize("method,path,capability", [
    ("POST", "/v1/review/p1/approve", "review"),
    ("POST", "/v1/review/p1/reject", "review"),
    ("POST", "/v1/review/committed/o1/retract", "review"),
    ("POST", "/v1/wisdom/promote/r1", "promote"),
])
def test_read_and_propose_grant_neither_review_nor_promote(client, stores, method, path, capability):
    store = _store_key(stores, ["read", "propose"])
    assert _call(client, store, method, path, None) == 403
    assert _call(client, "otherkey", method, path, None) == 403
    assert _call(client, _store_key(stores, [capability]), method, path, None) in _PASSED
