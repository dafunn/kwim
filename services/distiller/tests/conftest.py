"""Test harness for the distiller unit tests.

The distiller is a self-contained slim job: its real footprint is `app.py` + the
KWIM client package (`kwim.py` + `llm_router.py`, from clients/python) + one
distiller-local helper (`otel.py`). The unit tests exercise the policy/watermark logic
against fakes for the KWIM client surface and a fake LLM - they never touch real
OTLP, an LLM, or langchain. So we:

  * put the distiller dir (for `app`, flat imports) and clients/python (for the
    real `kwim` + `secret_reader`) on sys.path, and
  * stub the infra boundaries the tests don't exercise (`otel`, `llm_router`,
    `langchain_core.messages`) before importing app.py, keeping the test deps to
    just the pytest stack + httpx.

Run from services/distiller/:  ../../service/.venv/bin/python -m pytest
(kept as its own pytest invocation so its top-level `app` doesn't collide with
the service's `app` package on sys.path).
"""
import sys
import types
from pathlib import Path

import pytest

DISTILLER_DIR = Path(__file__).resolve().parent.parent          # services/distiller
REPO_ROOT = DISTILLER_DIR.parent.parent                         # repo root
CLIENTS = REPO_ROOT / "clients" / "python"                      # real kwim + secret_reader

for _p in (str(CLIENTS), str(DISTILLER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- Stub infra boundaries before importing app.py -------------------------
# otel.configure() runs at app import; a no-op avoids needing opentelemetry.
_otel = types.ModuleType("otel")
_otel.configure = lambda *a, **k: None
sys.modules["otel"] = _otel

# app.py does `from llm_router import make_llm` at import; tests monkeypatch
# distiller_app.make_llm, so this only needs to import cleanly.
_llm_router = types.ModuleType("llm_router")
_llm_router.make_llm = lambda *a, **k: None
sys.modules["llm_router"] = _llm_router

# Light stand-ins for the message classes _distill builds - the fake LLM only
# reads `.content`, so this keeps langchain out of the test deps.
_lc = types.ModuleType("langchain_core")
_lc_messages = types.ModuleType("langchain_core.messages")


class _Msg:
    def __init__(self, content=""):
        self.content = content


class SystemMessage(_Msg):
    pass


class HumanMessage(_Msg):
    pass


_lc_messages.SystemMessage = SystemMessage
_lc_messages.HumanMessage = HumanMessage
sys.modules["langchain_core"] = _lc
sys.modules["langchain_core.messages"] = _lc_messages


@pytest.fixture
def distiller_app(monkeypatch):
    """The distiller app module (services/distiller/app.py).

    Tests monkeypatch its module-level names (read_episodic / knowledge_propose /
    wisdom_propose / _post / make_llm), which app.run() references as globals.

    `require_available` - run()'s credential preflight - is stubbed here with the
    other infra boundaries: these tests fake the whole KWIM client surface, so
    there is no key or base URL to find. Tests that exercise the preflight itself
    (TestPreflight) re-patch it or call the real one directly.
    """
    import app

    monkeypatch.setattr(app, "require_available", lambda: "test-key")
    return app
