"""Tracing: one trace must follow a request from service to service (no database needed)."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from common import observability, tracing


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app: FastAPI, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return server


@pytest.fixture
def two_services():
    exporter = InMemorySpanExporter()
    downstream, upstream = FastAPI(), FastAPI()

    @downstream.get("/v1/stock")
    def stock():
        return {"ok": True}

    @upstream.get("/v1/orders")
    def orders():
        return httpx.get(f"http://127.0.0.1:{down_port}/v1/stock").json()

    for app, name in ((downstream, "inventory"), (upstream, "orders")):
        observability.setup(app, name, lambda: None)  # metrics + health, as in the real services
        tracing.setup(app, name, exporter=exporter)
    down_port, up_port = _free_port(), _free_port()
    servers = [_serve(downstream, down_port), _serve(upstream, up_port)]
    yield exporter, up_port
    for s in servers:
        s.should_exit = True


def test_one_trace_spans_both_services(two_services):
    exporter, port = two_services
    assert httpx.get(f"http://127.0.0.1:{port}/v1/orders").status_code == 200
    time.sleep(0.3)
    spans = exporter.get_finished_spans()
    by_service = {}
    for s in spans:
        by_service.setdefault(s.resource.attributes["service.name"], []).append(s)
    assert {"orders", "inventory"} <= by_service.keys()
    assert len({s.context.trace_id for s in spans}) == 1, "orders and inventory must share one trace id"


def test_downstream_span_is_child_of_the_http_call(two_services):
    exporter, port = two_services
    httpx.get(f"http://127.0.0.1:{port}/v1/orders")
    time.sleep(0.3)
    spans = exporter.get_finished_spans()
    inventory_server = next(s for s in spans if s.resource.attributes["service.name"] == "inventory"
                            and "stock" in s.name)
    parent_ids = {s.context.span_id for s in spans}
    assert inventory_server.parent is not None and inventory_server.parent.span_id in parent_ids


def test_health_and_metrics_are_not_traced(two_services):
    exporter, port = two_services
    httpx.get(f"http://127.0.0.1:{port}/health")
    httpx.get(f"http://127.0.0.1:{port}/metrics")
    time.sleep(0.2)
    assert not [s for s in exporter.get_finished_spans() if "health" in s.name or "metrics" in s.name]


def test_tracing_is_a_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = FastAPI()
    tracing.setup(app, "orders")  # must not raise, must not instrument anything
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
