"""OTel SDK initialisation for the KWIM service

Call configure(app) once at application startup, right after the FastAPI app
is instantiated.

Fail-soft: if OTEL_EXPORTER_OTLP_ENDPOINT is unset, this is a no-op - no
TracerProvider is installed and no instrumentation is applied.
KWIM service has no collector dependency by default.
"""

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure(app: FastAPI, service_name: str | None = None) -> None:
    # Read the env live (not a frozen settings snapshot): the OTLP exporter reads
    # OTEL_EXPORTER_OTLP_ENDPOINT from the env itself, and configure() may run before
    # the value is bound. See config.py for the OTEL_* inventory note.
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    name = service_name or os.environ.get("OTEL_SERVICE_NAME", "kwim-service")
    resource = Resource.create({"service.name": name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
