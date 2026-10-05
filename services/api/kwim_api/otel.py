"""OpenTelemetry setup. configure(app) runs once after the app is created and does
nothing when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
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
    # Read from the live environment, as the OTLP exporter does.
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
