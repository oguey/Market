"""Logging, Prometheus metrics and health endpoint shared by every service."""
import logging
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP latency", ["service", "route"])
CIRCUIT_STATE = Gauge("circuit_breaker_state", "0=closed 1=half_open 2=open", ["dependency"])


def setup(app: FastAPI, service: str, health_check: Callable[[], None]) -> None:
    logging.basicConfig(level=logging.INFO,
                        format=f"%(asctime)s %(levelname)s service={service} %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per outgoing call is too noisy

    @app.middleware("http")
    async def metrics(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")  # template, not raw path
        REQUESTS.labels(service, request.method, route, response.status_code).inc()
        LATENCY.labels(service, route).observe(time.perf_counter() - start)
        return response

    @app.get("/metrics", include_in_schema=False)
    def prometheus() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health", include_in_schema=False)
    def health():
        try:
            health_check()
        except Exception as exc:  # report, never crash the probe
            return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)
        return {"status": "ok"}
