"""Test harness for the distiller unit tests.

The tests run the distiller against a fake KWIM client and a fake LLM. `otel`,
`llm_router` and `langchain_core.messages` are stubbed before `distiller` is
imported. Import paths come from `pythonpath` in the repo-root pytest.ini.
"""
import sys
import types

import pytest

# --- Stub infra boundaries before importing distiller.py -------------------
# otel.configure() runs at import; a no-op avoids needing opentelemetry.
_otel = types.ModuleType("otel")
_otel.configure = lambda *a, **k: None
sys.modules["otel"] = _otel

# Imported by distiller.py; tests replace distiller_app.make_llm.
_llm_router = types.ModuleType("llm_router")
_llm_router.make_llm = lambda *a, **k: None
sys.modules["llm_router"] = _llm_router

# Stand-ins for the message classes _distill builds; the fake LLM reads `.content`.
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
    """The distiller module (services/distiller/distiller.py).

    Tests replace its module-level names (read_episodic, knowledge_propose,
    wisdom_propose, _post, make_llm). `require_available` is stubbed;
    TestPreflight tests the real one.
    """
    import distiller

    monkeypatch.setattr(distiller, "require_available", lambda: "test-key")
    return distiller
