import asyncio
import threading
import time
from contextlib import contextmanager
from typing import Literal

from fastapi import HTTPException, status

from pagos.config import (
    BULKHEAD_CAPACITY,
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_OPEN_SECONDS,
    TIMEOUT_SECONDS,
)


class CircuitBreaker:
    def __init__(self, threshold: int, open_duration_seconds: float) -> None:
        self.threshold = threshold
        self.open_duration_seconds = open_duration_seconds
        self.failures = 0
        self.state: Literal["CLOSED", "OPEN", "HALF_OPEN"] = "CLOSED"
        self.opened_at: float | None = None
        self.half_open_probe_in_flight = False

    def before_call(self) -> None:
        if self.state == "OPEN":
            if self.opened_at is not None and (
                time.monotonic() - self.opened_at
            ) >= self.open_duration_seconds:
                self.state = "HALF_OPEN"
                self.half_open_probe_in_flight = True
            else:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                        "message": "Circuit Breaker abierto. Reintente en unos segundos.",
                    },
                )
        elif self.state == "HALF_OPEN":
            if self.half_open_probe_in_flight:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                        "message": "Circuit Breaker en prueba. Ya hay una solicitud en vuelo.",
                    },
                )
            self.half_open_probe_in_flight = True

    def on_success(self) -> None:
        self.failures = 0
        self.state = "CLOSED"
        self.opened_at = None
        self.half_open_probe_in_flight = False

    def on_failure(self) -> None:
        if self.state == "HALF_OPEN":
            self.state = "OPEN"
            self.opened_at = time.monotonic()
            self.half_open_probe_in_flight = False
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.state = "OPEN"
            self.opened_at = time.monotonic()
            self.half_open_probe_in_flight = False


class Bulkhead:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._semaphore = threading.Semaphore(capacity)

    @contextmanager
    def acquire(self):
        if not self._semaphore.acquire(blocking=False):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "PAYMENT_BULKHEAD_EXCEEDED",
                    "message": "Bulkhead saturado. Intente nuevamente más tarde.",
                },
            )
        try:
            yield
        finally:
            self._semaphore.release()


circuit_breaker = CircuitBreaker(
    threshold=CIRCUIT_FAILURE_THRESHOLD,
    open_duration_seconds=CIRCUIT_OPEN_SECONDS,
)
bulkhead = Bulkhead(BULKHEAD_CAPACITY)


async def invoke_provider_call(func, *args, **kwargs):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise TimeoutError("Timeout en la pasarela de pagos") from exc
