from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from rag import tracing


def test_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing.setup()
    assert tracing._provider is None
    tracing.shutdown()  # no-op


def test_on_with_an_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo.invalid:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "rag-api")
    monkeypatch.setattr(trace, "set_tracer_provider", lambda p: None)  # keep the global clean
    tracing.setup()
    try:
        assert isinstance(tracing._provider, TracerProvider)
        assert tracing._provider.resource.attributes["service.name"] == "rag-api"
    finally:
        tracing.shutdown()  # nothing buffered: returns without exporting
        tracing._provider = None
