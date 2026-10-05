"""OpenTelemetry setup for the distiller. Call configure() once at startup.

Spans go to OTLP over gRPC when OTEL_EXPORTER_OTLP_ENDPOINT is set, and to stdout
otherwise.
"""

import os

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def configure(service_name: str | None = None) -> None:
    name = service_name or os.environ.get("OTEL_SERVICE_NAME", "kwim-agent")
    resource = Resource.create({"service.name": name})
    provider = TracerProvider(resource=resource)
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter()
    else:
        exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # Instrument the module-level httpx client that langchain-openai uses.
    HTTPXClientInstrumentor().instrument()
