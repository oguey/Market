"""Distributed tracing (OpenTelemetry) shared by every service.

Enabled only when OTEL_EXPORTER_OTLP_ENDPOINT is set (docker-compose does it); otherwise this is a no-op, so
tests and local runs without a collector are unaffected. Tracing never makes a request fail: if the collector is
down, spans are dropped and a warning is logged.

What is traced:
  - every incoming HTTP request (FastAPI), except /health and /metrics which are polled every few seconds;
  - every outgoing HTTP call (httpx), with the W3C `traceparent` header added so the next service continues the
    same trace (orders -> catalog / inventory / payments);
  - every SQL query (psycopg).
"""
import logging
import os

log = logging.getLogger("tracing")


def setup(app, service: str, exporter=None) -> None:
    """`exporter` is only for tests (an in-memory span exporter)."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint and exporter is None:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="health,metrics")
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        PsycopgInstrumentor().instrument(tracer_provider=provider)
        log.info("tracing enabled service=%s", service)
    except Exception as exc:  # never break the service because of tracing
        log.warning("tracing disabled: %s", exc)
