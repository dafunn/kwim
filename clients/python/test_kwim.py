"""Fail-soft unit tests for the KWIM client.

No live KWIM needed. Covers:
  - KWIM_BASE_URL unset so every method returns its safe default, no raise.
  - Mocked httpx success, methods return parsed responses.
  - Mocked httpx failure, methods degrade to safe defaults.

Run:
  cd clients/python
  python test_kwim.py
"""
import asyncio
import sys
from typing import Any

# Ensure the module under test is importable from this directory.
sys.path.insert(0, __import__("os").path.dirname(__file__))

import kwim

_failures: list[str] = []


def assert_eq(label: str, got: Any, expected: Any) -> None:
    if got != expected:
        _failures.append(f"FAIL {label}: got {got!r}, expected {expected!r}")
    else:
        print(f"  OK  {label}")


def assert_true(label: str, cond: bool) -> None:
    if not cond:
        _failures.append(f"FAIL {label}: condition was False")
    else:
        print(f"  OK  {label}")


def _run(coro):
    return asyncio.run(coro)


# The fail-soft empty bundle memory_context degrades to.
_EMPTY_COVERAGE = {"covered": False, "n": 0}
_EMPTY_BUNDLE = {
    "recent": [], "knowledge": [], "wisdom": [],
    "coverage": {
        "knowledge": {**_EMPTY_COVERAGE, "queried": False},
        "wisdom": _EMPTY_COVERAGE,
        "recent": _EMPTY_COVERAGE,
    },
}


# ---------------------------------------------------------------------------
# Case 1: KWIM unconfigured -> safe defaults
# ---------------------------------------------------------------------------
print("\n=== Unconfigured KWIM (no KWIM_BASE_URL) ===")

# Snapshot and clear config so the module behaves as if unconfigured.
_original_url = kwim.KWIM_BASE_URL
kwim.KWIM_BASE_URL = ""
kwim._api_key = "dummy-key"
kwim._key_tried = True

try:
    assert_eq("knowledge_query -> []", _run(kwim.knowledge_query()), [])
    assert_eq("wisdom_rules -> []", _run(kwim.wisdom_rules()), [])
    assert_eq("wisdom_check -> allow degraded",
              _run(kwim.wisdom_check({"content": "x"})),
              {"verdict": "allow", "_degraded": True})
    assert_eq("memory_context -> empty bundle",
              _run(kwim.memory_context("sess-1")),
              _EMPTY_BUNDLE)
    assert_eq("memory_semantic -> []", _run(kwim.memory_semantic("hello")), [])
    assert_eq("memory_semantic no-q -> []", _run(kwim.memory_semantic(kind="x")), [])
    assert_eq("knowledge_propose -> None", _run(kwim.knowledge_propose("s", "f")), None)
finally:
    kwim.KWIM_BASE_URL = _original_url
    kwim._api_key = None
    kwim._key_tried = False


# ---------------------------------------------------------------------------
# Case 2: Mocked httpx success
# ---------------------------------------------------------------------------
print("\n=== Mocked httpx success ===")

class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, response: Any, *, timeout: float = 0):
        self._response = response
        self.timeout = timeout
        self.calls: list[dict] = []

    async def get(self, url: str, *, headers: dict | None = None, params: dict | None = None) -> Any:
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self._response

    async def post(self, url: str, *, headers: dict | None = None, json: dict | None = None) -> Any:
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


# Monkey-patch httpx.AsyncClient for this test.
_real_async_client = kwim.httpx.AsyncClient
kwim.KWIM_BASE_URL = "http://kwim.test"
kwim._api_key = "test-key"
kwim._key_tried = True

try:
    # knowledge_query
    fake = _FakeClient(_FakeResponse([{"id": "f1", "statement": "s1"}]))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.knowledge_query(fact_type="ft1", limit=5))
    assert_eq("knowledge_query returns list", result, [{"id": "f1", "statement": "s1"}])
    assert_eq("knowledge_query passes params", fake.calls[-1]["params"]["fact_type"], "ft1")

    # wisdom_rules
    fake = _FakeClient(_FakeResponse([{"id": "r1", "rule_type": "constraint"}]))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.wisdom_rules(profile="p", repo="foo"))
    assert_eq("wisdom_rules returns list", result, [{"id": "r1", "rule_type": "constraint"}])
    assert_eq("wisdom_rules passes situation.profile",
              fake.calls[-1]["params"]["situation.profile"], "p")
    assert_eq("wisdom_rules passes second situation key",
              fake.calls[-1]["params"]["situation.repo"], "foo")

    # wisdom_check
    fake = _FakeClient(_FakeResponse({"verdict": "deny", "matched_rule": "c1"}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.wisdom_check({"content": "bad"}))
    assert_eq("wisdom_check returns dict", result["verdict"], "deny")
    assert_eq("wisdom_check posts action", fake.calls[-1]["json"]["action"]["content"], "bad")

    # memory_context
    fake = _FakeClient(_FakeResponse({"recent": ["a"], "knowledge": [], "wisdom": []}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.memory_context("sess-1", subject="foo", profile="p", task_type="t"))
    assert_eq("memory_context returns bundle", result["recent"], ["a"])
    assert_eq("memory_context passes session_id", fake.calls[-1]["params"]["session_id"], "sess-1")
    assert_eq("memory_context passes subject typed", fake.calls[-1]["params"]["subject"], "foo")
    assert_eq("memory_context passes situation.profile", fake.calls[-1]["params"]["situation.profile"], "p")
    assert_eq("memory_context passes situation.task_type", fake.calls[-1]["params"]["situation.task_type"], "t")

    # memory_semantic
    fake = _FakeClient(_FakeResponse([{"id": "m1", "content": "hello"}]))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.memory_semantic("q", limit=3, kind="notes"))
    assert_eq("memory_semantic returns list", result, [{"id": "m1", "content": "hello"}])
    assert_eq("memory_semantic passes meta filter", fake.calls[-1]["params"]["meta.kind"], "notes")

    # memory_semantic metadata-only mode (no q -> param omitted entirely)
    fake = _FakeClient(_FakeResponse([{"id": "g1"}]))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.memory_semantic(kind="playbook"))
    assert_eq("memory_semantic no-q returns list", result, [{"id": "g1"}])
    assert_eq("memory_semantic no-q passes meta filter", fake.calls[-1]["params"]["meta.kind"], "playbook")
    assert_true("memory_semantic no-q omits q param", "q" not in fake.calls[-1]["params"])

    # knowledge_propose
    fake = _FakeClient(_FakeResponse({"proposal_id": "p1", "status": "accepted"}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.knowledge_propose("stmt", "ftype", evidence=["ev1"]))
    assert_eq("knowledge_propose returns response", result["proposal_id"], "p1")
    assert_eq("knowledge_propose posts statement", fake.calls[-1]["json"]["statement"], "stmt")

finally:
    kwim.httpx.AsyncClient = _real_async_client
    kwim.KWIM_BASE_URL = _original_url
    kwim._api_key = None
    kwim._key_tried = False


# ---------------------------------------------------------------------------
# Case 3: Mocked httpx failure -> fail-soft defaults
# ---------------------------------------------------------------------------
print("\n=== Mocked httpx failure (fail-soft) ===")

class _BrokenClient:
    async def get(self, *a, **kw):
        raise RuntimeError("network down")

    async def post(self, *a, **kw):
        raise RuntimeError("network down")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


kwim.KWIM_BASE_URL = "http://kwim.test"
kwim._api_key = "test-key"
kwim._key_tried = True
kwim.httpx.AsyncClient = lambda **kw: _BrokenClient()

try:
    assert_eq("knowledge_query failure -> []", _run(kwim.knowledge_query()), [])
    assert_eq("wisdom_rules failure -> []", _run(kwim.wisdom_rules()), [])
    assert_eq("wisdom_check failure -> allow degraded",
              _run(kwim.wisdom_check({})),
              {"verdict": "allow", "_degraded": True})
    assert_eq("memory_context failure -> empty bundle",
              _run(kwim.memory_context("sess")),
              _EMPTY_BUNDLE)
    assert_eq("memory_semantic failure -> []", _run(kwim.memory_semantic("q")), [])
    assert_eq("knowledge_propose failure -> None", _run(kwim.knowledge_propose("s", "f")), None)
finally:
    kwim.httpx.AsyncClient = _real_async_client
    kwim.KWIM_BASE_URL = _original_url
    kwim._api_key = None
    kwim._key_tried = False


# ---------------------------------------------------------------------------
# Case 4: Strict paths -> loud failure
#
# Fail-soft is right for agents and fatal for KWIM-only jobs.
# ---------------------------------------------------------------------------
print("\n=== Strict paths (require_available / read_episodic strict) ===")


def _expect_unavailable(label, fn):
    try:
        fn()
    except kwim.KwimUnavailable:
        assert_true(label, True)
    except Exception as exc:  # noqa: BLE001 - any other type is a failure
        assert_true(f"{label} (raised {type(exc).__name__} instead)", False)
    else:
        assert_true(f"{label} (did not raise)", False)


kwim.KWIM_BASE_URL = ""
kwim._api_key = None
kwim._key_tried = True

try:
    _expect_unavailable("require_available raises when KWIM_BASE_URL unset",
                        kwim.require_available)

    kwim.KWIM_BASE_URL = "http://kwim.test"
    _expect_unavailable("require_available raises when the key is unreadable",
                        kwim.require_available)

    # The message must name the path it looked in
    try:
        kwim.require_available()
    except kwim.KwimUnavailable as exc:
        assert_true("require_available names the key path it tried",
                    "kwim-api-key" in str(exc))
        assert_true("require_available names KWIM_SECRETS_DIR as the fix",
                    "KWIM_SECRETS_DIR" in str(exc))

    kwim._api_key = "test-key"
    assert_eq("require_available returns the key once readable",
              kwim.require_available(), "test-key")

    # A failed read must not masquerade as an empty window.
    kwim.httpx.AsyncClient = lambda **kw: _BrokenClient()
    assert_eq("read_episodic fail-soft still yields the empty sentinel",
              _run(kwim.read_episodic()),
              {"events": [], "next_cursor": None})
    _expect_unavailable("read_episodic(strict=True) raises on a failed read",
                        lambda: _run(kwim.read_episodic(strict=True)))
finally:
    kwim.httpx.AsyncClient = _real_async_client
    kwim.KWIM_BASE_URL = _original_url
    kwim._api_key = None
    kwim._key_tried = False


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
print()
if _failures:
    print(f"FAILED ({len(_failures)} failure(s)):")
    for f in _failures:
        print(f"  {f}")
    sys.exit(1)
else:
    print("All tests passed.")
