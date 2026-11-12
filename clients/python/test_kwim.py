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


# The fail-soft result documents_search degrades to.
_EMPTY_SEARCH = {"results": [], "coverage": {}, "_degraded": True}

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
    assert_eq("documents_list -> []", _run(kwim.documents_list()), [])
    assert_eq("documents_search -> degraded empty",
              _run(kwim.documents_search("how do I replace the seal")),
              _EMPTY_SEARCH)
    assert_eq("documents_section -> None", _run(kwim.documents_section("d1", "s1")), None)
    assert_eq("documents_figure -> None", _run(kwim.documents_figure("d1", "f1")), None)
    assert_eq("documents_promote -> None", _run(kwim.documents_promote("d1")), None)
    unconfigured = _run(kwim.skills_search("check the cpu"))
    assert_eq("skills_search -> unavailable, no results", unconfigured["results"], [])
    assert_eq("skills_search -> coverage says unavailable",
              unconfigured["coverage"]["available"], False)
    assert_true("skills_search -> reason names the configuration",
                "not configured" in unconfigured["coverage"]["reason"])
    assert_eq("skill_get -> None", _run(kwim.skill_get("check-host-cpu")), None)
    assert_eq("skills_list -> []", _run(kwim.skills_list()), [])
    assert_eq("skill_propose -> None", _run(kwim.skill_propose("---\nname: x\n---\nbody")), None)
    assert_eq("skill_promote -> None", _run(kwim.skill_promote("x")), None)
finally:
    kwim.KWIM_BASE_URL = _original_url
    kwim._api_key = None
    kwim._key_tried = False


# ---------------------------------------------------------------------------
# Case 2: Mocked httpx success
# ---------------------------------------------------------------------------
print("\n=== Mocked httpx success ===")

class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200, content: bytes = b""):
        self._payload = payload
        self.status_code = status
        self.content = content

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

    # documents_list
    fake = _FakeClient(_FakeResponse({"items": [{"id": "d1", "title": "Pump manual"}]}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.documents_list(tag="pumps"))
    assert_eq("documents_list returns items", result, [{"id": "d1", "title": "Pump manual"}])
    assert_eq("documents_list GETs /v1/documents", fake.calls[-1]["url"], "http://kwim.test/v1/documents")
    assert_eq("documents_list passes tag", fake.calls[-1]["params"]["tag"], "pumps")

    # documents_search
    found = {"results": [{"document_id": "d1", "section_id": "s3", "heading": "Replace the seal"}],
             "coverage": {"vector": True, "fulltext": True, "reranked": True}}
    fake = _FakeClient(_FakeResponse(found))
    timeouts: list[float] = []
    def _client(**kw):
        timeouts.append(kw.get("timeout"))
        return fake
    kwim.httpx.AsyncClient = _client
    result = _run(kwim.documents_search("replace the seal", limit=3, document_ids=["d1"], tag="pumps"))
    assert_eq("documents_search returns results and coverage", result, found)
    assert_eq("documents_search GETs /v1/documents/search",
              fake.calls[-1]["url"], "http://kwim.test/v1/documents/search")
    assert_eq("documents_search passes q, limit, document and tag", fake.calls[-1]["params"],
              {"q": "replace the seal", "limit": 3, "document": ["d1"], "tag": "pumps"})
    assert_eq("documents_search allows time for reranking", timeouts[-1], 30.0)

    # documents_search with a response that is not a search result
    fake = _FakeClient(_FakeResponse({"detail": "unexpected"}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    assert_eq("documents_search malformed -> degraded empty",
              _run(kwim.documents_search("q")), _EMPTY_SEARCH)

    # documents_section
    fake = _FakeClient(_FakeResponse({"section_id": "s3", "text": "1. Drain the coolant."}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.documents_section("d1", "s3"))
    assert_eq("documents_section returns the section", result["text"], "1. Drain the coolant.")
    assert_eq("documents_section GETs the section path",
              fake.calls[-1]["url"], "http://kwim.test/v1/documents/d1/sections/s3")

    # documents_figure
    fake = _FakeClient(_FakeResponse(None, content=b"\x89PNG"))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.documents_figure("d1", "f1"))
    assert_eq("documents_figure returns the image bytes", result, b"\x89PNG")
    assert_eq("documents_figure GETs the figure path",
              fake.calls[-1]["url"], "http://kwim.test/v1/documents/d1/figures/f1")
    assert_eq("documents_figure sends the team key",
              fake.calls[-1]["headers"]["Authorization"], "Bearer test-key")

    # documents_promote
    fake = _FakeClient(_FakeResponse({"status": "promoted", "universe_document_id": "u1"}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.documents_promote("d1"))
    assert_eq("documents_promote returns the response", result["universe_document_id"], "u1")
    assert_eq("documents_promote POSTs the promote path",
              fake.calls[-1]["url"], "http://kwim.test/v1/documents/d1/promote")

    # skills_search
    found = {"results": [{"name": "check-host-cpu", "description": "CPU", "scope": "team",
                          "version": 1, "pending_revision": None}],
             "coverage": {"available": True, "vector": True, "fulltext": True, "reason": None}}
    fake = _FakeClient(_FakeResponse(found))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.skills_search("what is using the cpu", limit=3, agent="ops", tag="host"))
    assert_eq("skills_search returns results and coverage", result, found)
    assert_eq("skills_search GETs /v1/skills/search",
              fake.calls[-1]["url"], "http://kwim.test/v1/skills/search")
    assert_eq("skills_search passes q, limit, agent and tag", fake.calls[-1]["params"],
              {"q": "what is using the cpu", "limit": 3, "agent": "ops", "tag": "host"})

    # skills_search with no match is available, with no results
    fake = _FakeClient(_FakeResponse({"results": [], "coverage": {"available": True}}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.skills_search("renew a certificate"))
    assert_eq("skills_search no match -> available", result["coverage"]["available"], True)

    # skills_search with a response that is not a search result
    fake = _FakeClient(_FakeResponse({"detail": "unexpected"}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.skills_search("q"))
    assert_eq("skills_search malformed -> unavailable", result["coverage"]["available"], False)

    # skill_get
    fake = _FakeClient(_FakeResponse({"name": "check-host-cpu", "body": "Run the playbook."}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.skill_get("check-host-cpu"))
    assert_eq("skill_get returns the skill", result["body"], "Run the playbook.")
    assert_eq("skill_get GETs the skill path",
              fake.calls[-1]["url"], "http://kwim.test/v1/skills/check-host-cpu")

    # skills_list
    fake = _FakeClient(_FakeResponse({"items": [{"name": "check-host-cpu"}]}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.skills_list(agent="ops"))
    assert_eq("skills_list returns items", result, [{"name": "check-host-cpu"}])
    assert_eq("skills_list passes agent", fake.calls[-1]["params"]["agent"], "ops")

    # skill_propose
    fake = _FakeClient(_FakeResponse({"proposal_id": "p1", "status": "pending_review"}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.skill_propose("TEXT", supersedes="check-host-cpu", scope="universe"))
    assert_eq("skill_propose returns the proposal", result["status"], "pending_review")
    assert_eq("skill_propose posts text, supersedes and scope", fake.calls[-1]["json"],
              {"text": "TEXT", "scope": "universe", "supersedes": "check-host-cpu"})

    # skill_promote
    fake = _FakeClient(_FakeResponse({"status": "promoted"}))
    kwim.httpx.AsyncClient = lambda **kw: fake
    result = _run(kwim.skill_promote("check-host-cpu"))
    assert_eq("skill_promote POSTs the promote path",
              fake.calls[-1]["url"], "http://kwim.test/v1/skills/check-host-cpu/promote")

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
    assert_eq("documents_list failure -> []", _run(kwim.documents_list()), [])
    assert_eq("documents_search failure -> degraded empty",
              _run(kwim.documents_search("q")), _EMPTY_SEARCH)
    assert_eq("documents_section failure -> None", _run(kwim.documents_section("d", "s")), None)
    assert_eq("documents_figure failure -> None", _run(kwim.documents_figure("d", "f")), None)
    assert_eq("documents_promote failure -> None", _run(kwim.documents_promote("d")), None)
    down = _run(kwim.skills_search("q"))
    assert_eq("skills_search failure -> unavailable", down["coverage"]["available"], False)
    assert_true("skills_search failure -> reason says unreachable",
                "unreachable" in down["coverage"]["reason"])
    assert_eq("skill_get failure -> None", _run(kwim.skill_get("x")), None)
    assert_eq("skills_list failure -> []", _run(kwim.skills_list()), [])
    assert_eq("skill_propose failure -> None", _run(kwim.skill_propose("t")), None)
    assert_eq("skill_promote failure -> None", _run(kwim.skill_promote("x")), None)
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
    except Exception as exc:
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
