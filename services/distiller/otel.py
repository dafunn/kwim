"""OpenTelemetry setup for the distiller. configure() runs once at startup and
does nothing when OTEL_EXPORTER_OTLP_ENDPOINT is unset, as kwim-service's does.
"""

import os

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure(service_name: str | None = None) -> None:
    # Without a collector there is nowhere to send spans; printing them instead
    # buries the distiller's own log lines.
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    name = service_name or os.environ.get("OTEL_SERVICE_NAME", "kwim-agent")
    resource = Resource.create({"service.name": name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # Instrument the module-level httpx client that langchain-openai uses.
    HTTPXClientInstrumentor().instrument()
