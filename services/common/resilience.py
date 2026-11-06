"""Resilient HTTP client for service-to-service calls.

Every outgoing call goes through: bulkhead -> circuit breaker -> retry -> timeout.
Only *idempotent* endpoints are called (all keyed by order_id), so retrying is safe.
"""
import logging
import threading
import time
from typing import Callable

import httpx

from common.observability import CIRCUIT_STATE

log = logging.getLogger("resilience")


class DependencyError(Exception):
    """The dependency could not answer (down, timeout, 5xx, circuit open, bulkhead full)."""

    def __init__(self, service: str, reason: str):
        super().__init__(f"{service} unavailable: {reason}")
        self.service, self.reason = service, reason


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, name: str, failure_threshold: int = 3, reset_timeout: float = 10.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name, self.threshold, self.reset_timeout, self.clock = name, failure_threshold, reset_timeout, clock
        self.failures, self.opened_at = 0, 0.0
        self._state = self.CLOSED
        self._lock = threading.Lock()
        self._set(self.CLOSED)

    def _set(self, state: str) -> None:
        if state != self._state:
            log.warning("circuit %s: %s -> %s", self.name, self._state, state)
        self._state = state
        CIRCUIT_STATE.labels(self.name).set({self.CLOSED: 0, self.HALF_OPEN: 1, self.OPEN: 2}[state])

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN and self.clock() - self.opened_at >= self.reset_timeout:
                self._set(self.HALF_OPEN)  # let one trial call through
            return self._state

    def call(self, fn: Callable):
        if self.state == self.OPEN:
            raise DependencyError(self.name, "circuit open")
        try:
            result = fn()
        except DependencyError:
            with self._lock:
                self.failures += 1
                if self._state == self.HALF_OPEN or self.failures >= self.threshold:
                    self.opened_at = self.clock()
                    self._set(self.OPEN)
            raise
        with self._lock:
            self.failures = 0
            self._set(self.CLOSED)
        return result


class ServiceClient:
    def __init__(self, name: str, base_url: str, *, token: Callable[[], str] | None = None,
                 timeout: float = 2.0, retries: int = 2, backoff: float = 0.2, max_concurrency: int = 10,
                 breaker: CircuitBreaker | None = None, http: httpx.Client | None = None):
        self.name, self.token, self.retries, self.backoff = name, token, retries, backoff
        self.breaker = breaker or CircuitBreaker(name)
        self._bulkhead = threading.BoundedSemaphore(max_concurrency)
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout)

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Returns the response for 2xx/4xx (the caller decides). Raises DependencyError otherwise."""
        if not self._bulkhead.acquire(blocking=False):
            raise DependencyError(self.name, "bulkhead full")
        try:
            return self.breaker.call(lambda: self._with_retry(method, path, **kwargs))
        finally:
            self._bulkhead.release()

    def _with_retry(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self.token:
            kwargs.setdefault("headers", {})["Authorization"] = f"Bearer {self.token()}"
        reason = ""
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:  # connect error, timeout, ...
                reason = type(exc).__name__
                continue
            if resp.status_code < 500:
                return resp
            reason = f"HTTP {resp.status_code}"
        raise DependencyError(self.name, reason)

    def close(self) -> None:
        self._http.close()
