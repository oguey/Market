import httpx
import pytest

from common.resilience import CircuitBreaker, DependencyError, ServiceClient


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def client_with(handler, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://svc")
    return ServiceClient("svc", "", http=http, backoff=0, **kw)


def test_retries_5xx_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200)

    assert client_with(handler, retries=2).request("GET", "/x").status_code == 200
    assert len(calls) == 3


def test_4xx_is_returned_not_retried():
    calls = []
    resp = client_with(lambda r: calls.append(1) or httpx.Response(409), retries=2).request("GET", "/x")
    assert resp.status_code == 409 and len(calls) == 1


def test_circuit_opens_then_half_opens_then_closes():
    clock, healthy = Clock(), {"up": False}

    def handler(req):
        if not healthy["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200)

    breaker = CircuitBreaker("svc", failure_threshold=2, reset_timeout=10, clock=clock)
    client = client_with(handler, retries=0, breaker=breaker)
    for _ in range(2):
        with pytest.raises(DependencyError, match="ConnectError"):
            client.request("GET", "/x")
    assert breaker.state == "open"
    with pytest.raises(DependencyError, match="circuit open"):  # fails fast, no network call
        client.request("GET", "/x")

    clock.t = 11
    assert breaker.state == "half_open"
    healthy["up"] = True
    assert client.request("GET", "/x").status_code == 200
    assert breaker.state == "closed"


def test_failed_trial_call_reopens_circuit():
    clock = Clock()
    breaker = CircuitBreaker("svc", failure_threshold=1, reset_timeout=10, clock=clock)
    client = client_with(lambda r: httpx.Response(500), retries=0, breaker=breaker)
    with pytest.raises(DependencyError):
        client.request("GET", "/x")
    clock.t = 11
    with pytest.raises(DependencyError, match="HTTP 500"):
        client.request("GET", "/x")
    assert breaker.state == "open"


def test_bulkhead_rejects_when_full():
    client = client_with(lambda r: httpx.Response(200), max_concurrency=1)
    client._bulkhead.acquire()  # simulate one call in flight
    with pytest.raises(DependencyError, match="bulkhead full"):
        client.request("GET", "/x")
