"""Unit tests for the packaged LLM factory (llm_router).

No langchain or live gateway needed - langchain_openai is stubbed and the secret
reader is monkeypatched. Covers model resolution (incl. fail-if-unset), tag
serialization, LITELLM_TAGS parsing, and make_llm header/key wiring.

Run:
  cd clients/python
  python test_llm_router.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))

import llm_router

_failures: list[str] = []


def assert_eq(label, got, expected):
    if got != expected:
        _failures.append(f"FAIL {label}: got {got!r}, expected {expected!r}")
    else:
        print(f"  OK  {label}")


def assert_raises(label, fn, exc=Exception):
    try:
        fn()
    except exc:
        print(f"  OK  {label}")
    else:
        _failures.append(f"FAIL {label}: expected {exc.__name__}")


def _clear(*names):
    for n in names:
        os.environ.pop(n, None)


# --- resolve_model: config-driven, no hardcoded default --------------------
_clear("DEFAULT_LLM_MODEL")
assert_raises("resolve_model raises when unset", lambda: llm_router.resolve_model(None), RuntimeError)
assert_eq("resolve_model explicit wins", llm_router.resolve_model("auto"), "auto")
os.environ["DEFAULT_LLM_MODEL"] = "foo-7b"
assert_eq("resolve_model uses DEFAULT_LLM_MODEL", llm_router.resolve_model(None), "foo-7b")
assert_eq("resolve_model explicit beats DEFAULT", llm_router.resolve_model("bar-3b"), "bar-3b")
_clear("DEFAULT_LLM_MODEL")

# --- _env_tags: LITELLM_TAGS parsing --------------------------------------
os.environ["LITELLM_TAGS"] = "host:foo, cluster:us-east , junk , :noval, nokey:"
assert_eq("_env_tags parses + drops malformed", llm_router._env_tags(),
          {"host": "foo", "cluster": "us-east"})
_clear("LITELLM_TAGS")
assert_eq("_env_tags empty when unset", llm_router._env_tags(), {})

# --- _litellm_tags: header serialization ----------------------------------
assert_eq("tags agent only", llm_router._litellm_tags("distiller", None), "agent:distiller")
assert_eq("tags agent+multi", llm_router._litellm_tags("worker", {"host": "x", "cluster": "y"}),
          "agent:worker,host:x,cluster:y")
assert_eq("tags none -> None", llm_router._litellm_tags(None, {}), None)
assert_eq("tags skip empty val", llm_router._litellm_tags("x", {"host": ""}), "agent:x")

# --- make_llm wiring (stub langchain + secret reader) ----------------------
_captured: dict = {}


class _FakeChatOpenAI:
    def __init__(self, **kwargs):
        _captured.update(kwargs)


sys.modules["langchain_openai"] = types.SimpleNamespace(ChatOpenAI=_FakeChatOpenAI)
llm_router.read_secret = lambda name: f"secret::{name}"

os.environ["DEFAULT_LLM_MODEL"] = "baz-1b"
os.environ["LITELLM_TAGS"] = "cluster:us-east"
llm_router.make_llm(agent="worker", tags={"host": "foo"})
assert_eq("make_llm resolves model", _captured["model"], "baz-1b")
assert_eq("make_llm reads key (default secret)", _captured["api_key"], "secret::litellm-key")
assert_eq("make_llm merges env+call tags", _captured["default_headers"],
          {"x-litellm-tags": "agent:worker,cluster:us-east,host:foo"})
_clear("DEFAULT_LLM_MODEL", "LITELLM_TAGS")

if _failures:
    print("\n".join(_failures))
    print(f"\n{len(_failures)} failure(s)")
    sys.exit(1)
print("\nAll llm_router tests passed.")
