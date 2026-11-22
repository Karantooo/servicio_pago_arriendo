import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time
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
        self._lock = threading.Lock()

    def before_call(self) -> None:
        with self._lock:
            if self.state == "OPEN":
                if self.opened_at is not None and (
                    time.monotonic() - self.opened_at
                ) >= self.open_duration_seconds:
                    self.state = "HALF_OPEN"
                else:
                    raise CircuitOpenError(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "error": "PAYMENT_CIRCUIT_OPEN",
                            "message": "Circuit Breaker abierto. Reintente en unos segundos.",
                        },
                    )
            if self.state == "HALF_OPEN":
                if self.half_open_probe_in_flight:
                    raise CircuitOpenError(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "error": "PAYMENT_CIRCUIT_OPEN",
                            "message": "Circuit Breaker en prueba. Ya hay una solicitud en vuelo.",
                        },
                    )
                self.half_open_probe_in_flight = True

    def on_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.state = "CLOSED"
            self.opened_at = None
            self.half_open_probe_in_flight = False

    def on_failure(self) -> None:
        with self._lock:
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

    def release_probe(self) -> None:
        with self._lock:
            if self.state == "HALF_OPEN":
                self.half_open_probe_in_flight = False


class CircuitOpenError(HTTPException):
    pass


class Bulkhead:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("La capacidad del Bulkhead debe ser positiva.")
        self.capacity = capacity
        self._slots = threading.BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(
            max_workers=capacity,
            thread_name_prefix="payment-provider",
        )

    def submit(self, func, *args, **kwargs) -> Future:
        if not self._slots.acquire(blocking=False):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Bulkhead capacity exceeded",
            )
        try:
            future = self._executor.submit(func, *args, **kwargs)
        except Exception:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        return future


circuit_breaker = CircuitBreaker(
    threshold=CIRCUIT_FAILURE_THRESHOLD,
    open_duration_seconds=CIRCUIT_OPEN_SECONDS,
)
bulkhead = Bulkhead(BULKHEAD_CAPACITY)


async def invoke_provider_call(func, *args, **kwargs):
    future = bulkhead.submit(func, *args, **kwargs)
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(future),
            timeout=TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise TimeoutError("Timeout en la pasarela de pagos") from exc
