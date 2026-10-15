"""otel.configure(): spans go to OTLP when a collector is set, and nowhere otherwise.

conftest.py replaces `otel` with a stub for the other tests, so this loads the
real module from its file.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "otel.py"


def _load_otel():
    spec = importlib.util.spec_from_file_location("distiller_otel_under_test", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_endpoint_installs_nothing(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    otel = _load_otel()
    calls = []
    monkeypatch.setattr(otel.trace, "set_tracer_provider", lambda p: calls.append(p))
    otel.configure()
    assert calls == []


def test_endpoint_installs_an_otlp_exporter(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4317")
    otel = _load_otel()
    providers = []
    monkeypatch.setattr(otel.trace, "set_tracer_provider", lambda p: providers.append(p))
    monkeypatch.setattr(otel.HTTPXClientInstrumentor, "instrument", lambda self: None)
    otel.configure()
    assert len(providers) == 1
    processors = providers[0]._active_span_processor._span_processors
    exporters = [type(p.span_exporter).__name__ for p in processors]
    assert exporters == ["OTLPSpanExporter"]
