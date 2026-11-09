"""OpenTelemetry tracing for the API, exported over OTLP/HTTP (to Tempo).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set: without a provider the
OpenTelemetry API is a no-op, so unit tests, CI's retrieval evaluation and
the local kind profile (no Tempo) run exactly as before. The exporter and
the service name come from the standard OTEL_* variables (k8s/rag/api.yaml).

Spans are exported in batches from a background thread, so a slow or
missing collector doesn't add latency to /ask; spans that can't be sent are
dropped and logged.
"""
import os

from opentelemetry import trace

_provider = None


def setup() -> None:
    global _provider
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Resource.create() reads OTEL_SERVICE_NAME; the exporter appends
    # /v1/traces to OTEL_EXPORTER_OTLP_ENDPOINT.
    _provider = TracerProvider(resource=Resource.create())
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_provider)


def shutdown() -> None:
    """Flush pending spans on API shutdown."""
    if _provider is not None:
        _provider.shutdown()
