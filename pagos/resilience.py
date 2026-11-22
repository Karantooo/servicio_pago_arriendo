import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
import logging
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
from pagos.providers.base import ProviderUnavailable
from pagos.observability import (
    SERVICE_NAME,
    bulkhead_rejections_total,
    request_id_context,
    set_circuit_breaker_state,
)

logger = logging.getLogger(__name__)


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
                    previous_state = self.state
                    self.state = "HALF_OPEN"
                    self._record_state_change(previous_state, self.state)
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
            previous_state = self.state
            self.failures = 0
            self.state = "CLOSED"
            self.opened_at = None
            self.half_open_probe_in_flight = False
            self._record_state_change(previous_state, self.state)

    def on_failure(self) -> None:
        with self._lock:
            previous_state = self.state
            if self.state == "HALF_OPEN":
                self.state = "OPEN"
                self.opened_at = time.monotonic()
                self.half_open_probe_in_flight = False
                self._record_state_change(previous_state, self.state)
                return
            self.failures += 1
            if self.failures >= self.threshold:
                self.state = "OPEN"
                self.opened_at = time.monotonic()
                self.half_open_probe_in_flight = False
            self._record_state_change(previous_state, self.state)

    @staticmethod
    def _record_state_change(previous: str, current: str) -> None:
        if previous == current:
            return
        set_circuit_breaker_state(current)
        logger.warning(
            "Payment provider circuit breaker state changed",
            extra={
                "event": "circuit_breaker_state_changed",
                "details": {"previous_state": previous, "state": current},
            },
        )

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
            bulkhead_rejections_total.labels(service=SERVICE_NAME).inc()
            logger.warning(
                "Payment provider bulkhead rejected operation",
                extra={
                    "event": "bulkhead_rejected",
                    "details": {
                        "capacity": self.capacity,
                        "request_id": request_id_context.get(),
                    },
                },
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Bulkhead capacity exceeded",
            )
        try:
            context = copy_context()
            future = self._executor.submit(context.run, func, *args, **kwargs)
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
    started = time.perf_counter()
    future = bulkhead.submit(func, *args, **kwargs)
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(future),
            timeout=TIMEOUT_SECONDS,
        )
    except ProviderUnavailable:
        logger.exception(
            "Payment provider reported itself unavailable",
            extra={
                "event": "payment_provider_unavailable",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "details": {"request_id": request_id_context.get()},
            },
        )
        raise
    except TimeoutError as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        logger.warning(
            "Payment provider call timed out",
            extra={
                "event": "payment_provider_timeout",
                "duration_ms": duration_ms,
                "details": {
                    "timeout_seconds": TIMEOUT_SECONDS,
                    "request_id": request_id_context.get(),
                },
            },
        )
        raise TimeoutError("Timeout en la pasarela de pagos") from exc
