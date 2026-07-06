"""OTel SDK initialisation for KWIM LangGraph services.

Call configure() once at application startup, before any LLM calls are made.

If OTEL_EXPORTER_OTLP_ENDPOINT is set, spans are exported via OTLP to the
collector - gRPC, OTLPSpanExporter() reads the endpoint from the env itself.
Otherwise falls back to the original ConsoleSpanExporter (stdout JSON -> promtail -> Loki),
so a service with no collector configured doesn't spam OTLP-export errors to
stdout.
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor


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
