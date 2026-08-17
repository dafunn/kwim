"""Tests for OTel SDK initialisation:

  1. configure() is a no-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset - no
     TracerProvider installed, no FastAPI instrumentation applied.
  2. configure() with the env var set installs a TracerProvider and instruments
     the app such that an inbound `traceparent` header produces a server span
     under that trace id (the agent->KWIM join).

Both phases run in one test, in order, on purpose: trace.set_tracer_provider()
refuses to override a real provider once set, so the no-op case must be observed
before the endpoint case installs a real provider. conftest pops the OTEL env at
session start (and kwim_api.main's import-time configure() is a no-op without an
endpoint), so no real provider exists until part 2 installs it here.
"""
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kwim_api import otel


def test_configure_noop_then_traceparent_join(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    provider_before = trace.get_tracer_provider()

    # --- Part 1: no-op when endpoint unset ---
    app = FastAPI()
    middleware_count_before = len(app.user_middleware)
    otel.configure(app)
    assert trace.get_tracer_provider() is provider_before          # provider untouched
    assert len(app.user_middleware) == middleware_count_before     # no instrumentation added

    # --- Part 2: endpoint set -> inbound traceparent joins the trace ---
    # configure() also installs a real OTLP BatchSpanProcessor pointed at the
    # (unreachable) endpoint. Its background export retries are harmless daemon-
    # thread noise, so silence that exporter's logger; assertions use a separate
    # in-memory exporter. (We deliberately don't shut the provider down - its
    # flush would block ~6s trying to reach the dead endpoint.)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "kwim-service-test")
    logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").setLevel(logging.CRITICAL)

    app2 = FastAPI()

    @app2.get("/ping")
    def ping():
        return {"ok": True}

    otel.configure(app2)
    provider = trace.get_tracer_provider()
    assert provider is not provider_before                         # provider installed

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    client = TestClient(app2)
    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    resp = client.get("/ping", headers={"traceparent": traceparent})
    assert resp.status_code == 200

    server_spans = [s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    assert len(server_spans) == 1
    assert format(server_spans[0].context.trace_id, "032x") == "0123456789abcdef0123456789abcdef"
    assert server_spans[0].resource.attributes["service.name"] == "kwim-service-test"
