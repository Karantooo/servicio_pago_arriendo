import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

import httpx
from pydantic import ValidationError

from app.models import PaymentCheckout


PAYMENTS_API_URL = os.getenv(
    "PAYMENTS_API_URL", "http://payments-api:8003"
).rstrip("/")
PAYMENTS_TIMEOUT_SECONDS = float(
    os.getenv("PAYMENTS_TIMEOUT_SECONDS", "3")
)
PAYMENTS_PROVIDER_TIMEOUT_SECONDS = float(
    os.getenv("PAYMENTS_PROVIDER_TIMEOUT_SECONDS", "2")
)

if not 0 < PAYMENTS_PROVIDER_TIMEOUT_SECONDS < PAYMENTS_TIMEOUT_SECONDS:
    raise ValueError(
        "PAYMENTS_PROVIDER_TIMEOUT_SECONDS debe ser positivo y menor que "
        "PAYMENTS_TIMEOUT_SECONDS"
    )
PAYMENTS_CIRCUIT_FAILURE_THRESHOLD = int(
    os.getenv("PAYMENTS_CIRCUIT_FAILURE_THRESHOLD", "3")
)
PAYMENTS_CIRCUIT_OPEN_SECONDS = float(
    os.getenv("PAYMENTS_CIRCUIT_OPEN_SECONDS", "15")
)
if PAYMENTS_CIRCUIT_FAILURE_THRESHOLD < 1 or PAYMENTS_CIRCUIT_OPEN_SECONDS <= 0:
    raise ValueError("La configuración del Circuit Breaker de Pagos no es válida")


class PaymentCircuitOpen(Exception):
    """El Circuit Breaker de Arriendos no admite temporalmente llamadas a Pagos."""


class PaymentServiceCircuitBreaker:
    def __init__(self, threshold: int, open_seconds: float) -> None:
        self.threshold = threshold
        self.open_seconds = open_seconds
        self.failures = 0
        self.state: Literal["CLOSED", "OPEN", "HALF_OPEN"] = "CLOSED"
        self.opened_at: float | None = None
        self.probe_in_flight = False
        self._lock = threading.Lock()

    def before_call(self) -> None:
        with self._lock:
            if self.state == "OPEN":
                if (
                    self.opened_at is None
                    or time.monotonic() - self.opened_at < self.open_seconds
                ):
                    raise PaymentCircuitOpen("El circuito de Pagos está abierto.")
                self.state = "HALF_OPEN"
            if self.state == "HALF_OPEN":
                if self.probe_in_flight:
                    raise PaymentCircuitOpen(
                        "El circuito de Pagos está probando una solicitud."
                    )
                self.probe_in_flight = True

    def on_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.state = "CLOSED"
            self.opened_at = None
            self.probe_in_flight = False

    def on_failure(self) -> None:
        with self._lock:
            if self.state == "HALF_OPEN":
                self.state = "OPEN"
                self.opened_at = time.monotonic()
                self.probe_in_flight = False
                return
            self.failures += 1
            if self.failures >= self.threshold:
                self.state = "OPEN"
                self.opened_at = time.monotonic()
                self.probe_in_flight = False

    def release_probe(self) -> None:
        with self._lock:
            if self.state == "HALF_OPEN":
                self.probe_in_flight = False


payments_circuit_breaker = PaymentServiceCircuitBreaker(
    PAYMENTS_CIRCUIT_FAILURE_THRESHOLD,
    PAYMENTS_CIRCUIT_OPEN_SECONDS,
)


def _request_with_breaker(
    request: Callable[[], httpx.Response],
    make_uncertain: Callable[[str], Exception],
) -> httpx.Response:
    try:
        payments_circuit_breaker.before_call()
    except PaymentCircuitOpen as error:
        raise make_uncertain(
            "Pagos temporalmente no disponible; pago pendiente de reintento."
        ) from error
    try:
        response = request()
    except httpx.RequestError as error:
        payments_circuit_breaker.on_failure()
        raise make_uncertain(
            "No se recibió una respuesta de Pagos."
        ) from error
    if response.status_code >= 500 or response.status_code == 429:
        payments_circuit_breaker.on_failure()
    else:
        payments_circuit_breaker.on_success()
    return response


class PaymentStartRejected(Exception):
    """Pagos rechazó los datos y no inició el cobro."""

    def __init__(
        self,
        message: str,
        *,
        payment_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.payment_id = payment_id


class PaymentStartUncertain(Exception):
    """No se pudo confirmar si Pagos inició el cobro."""

    def __init__(
        self,
        message: str,
        *,
        payment_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.payment_id = payment_id


class PaymentLookupUncertain(Exception):
    """No se pudo confirmar el estado actual de un pago."""


class RefundRejected(Exception):
    """Pagos rechazo la solicitud de reembolso."""


class RefundUncertain(Exception):
    """No se pudo confirmar si el reembolso se realizo."""


@dataclass(frozen=True)
class PaymentState:
    payment_id: int
    rental_id: int
    amount_total: int
    status: Literal[
        "PENDING",
        "CHARGED",
        "FAILED",
        "CANCELLED",
        "UNKNOWN",
        "PAYMENT_PENDING_RETRY",
        "CONFIRMATION_PENDING_RETRY",
        "REFUND_PENDING",
        "REFUNDED",
    ]
    redirect_url: str | None
    token: str | None


@dataclass(frozen=True)
class RefundState:
    refund_id: int
    status: Literal["REFUND_PENDING", "REFUNDED", "UNKNOWN"]


def reembolsar_pago(
    payment_id: int,
    rental_id: int,
    idempotency_key: str,
) -> RefundState:
    response = _request_with_breaker(
        lambda: httpx.post(
            f"{PAYMENTS_API_URL}/payments/{payment_id}/refund",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "reason": "SAGA_COMPENSATION",
                "rental_id": rental_id,
            },
            timeout=PAYMENTS_TIMEOUT_SECONDS,
        ),
        RefundUncertain,
    )

    if response.status_code in {400, 404, 409}:
        raise RefundRejected(
            _refund_error_message(response, "Pagos rechazo el reembolso.")
        )

    if response.status_code not in {200, 201}:
        raise RefundUncertain(
            f"Pagos respondió HTTP {response.status_code} al solicitar el reembolso."
        )

    try:
        data = response.json()
        refund = RefundState(
            refund_id=int(data["refund_id"]),
            status=data["status"],
        )
        if refund.refund_id <= 0 or refund.status not in {
            "REFUND_PENDING",
            "REFUNDED",
            "UNKNOWN",
        }:
            raise ValueError("Respuesta de reembolso inválida")
    except (ValueError, KeyError, TypeError) as error:
        raise RefundUncertain(
            "Pagos respondió, pero no se pudo interpretar el reembolso."
        ) from error
    return refund


def _refund_error_message(response: httpx.Response, fallback: str) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            if data.get("message"):
                return str(data["message"])
            if data.get("detail"):
                detail = data["detail"]
                if isinstance(detail, dict) and detail.get("message"):
                    return str(detail["message"])
    except ValueError:
        pass
    return fallback


def consultar_pago(payment_id: int) -> PaymentState:
    response = _request_with_breaker(
        lambda: httpx.get(
            f"{PAYMENTS_API_URL}/payments/{payment_id}",
            timeout=PAYMENTS_TIMEOUT_SECONDS,
        ),
        PaymentLookupUncertain,
    )

    if response.status_code != 200:
        raise PaymentLookupUncertain(
            f"Pagos respondió HTTP {response.status_code} al consultar el cobro."
        )

    try:
        data = response.json()
        state = PaymentState(
            payment_id=int(data["payment_id"]),
            rental_id=int(data["rental_id"]),
            amount_total=int(data["amount_total"]),
            status=data["status"],
            redirect_url=data.get("redirect_url"),
            token=data.get("token"),
        )
        if (
            state.payment_id != payment_id
            or state.payment_id <= 0
            or state.rental_id <= 0
            or state.amount_total < 0
            or state.status not in {
                "PENDING",
                "CHARGED",
                "FAILED",
                "CANCELLED",
                "UNKNOWN",
                "PAYMENT_PENDING_RETRY",
                "CONFIRMATION_PENDING_RETRY",
                "REFUND_PENDING",
                "REFUNDED",
            }
        ):
            raise ValueError("Respuesta de pago inválida")
    except (ValueError, KeyError, TypeError) as error:
        raise PaymentLookupUncertain(
            "Pagos respondió, pero no se pudo interpretar el estado."
        ) from error
    return state


def iniciar_cobro(
    rental_id: int,
    customer_id: int,
    amount_rent: int,
    amount_deposit: int,
    idempotency_key: str,
    require_checkout: bool = True,
) -> PaymentCheckout:
    response = _request_with_breaker(
        lambda: httpx.post(
            f"{PAYMENTS_API_URL}/payments/charge",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "rental_id": rental_id,
                "customer_id": customer_id,
                "amount_rent": amount_rent,
                "amount_deposit": amount_deposit,
            },
            timeout=PAYMENTS_TIMEOUT_SECONDS,
        ),
        PaymentStartUncertain,
    )

    if response.status_code == 400:
        raise PaymentStartRejected(
            "Pagos rechazó los datos de la solicitud."
        )

    if response.status_code not in (200, 201):
        payment_id = None
        message = None
        try:
            body = response.json()
            detail = body.get("detail", {}) if isinstance(body, dict) else {}
            if isinstance(detail, dict) and detail.get("payment_id") is not None:
                payment_id = int(detail["payment_id"])
            if isinstance(detail, dict) and detail.get("message"):
                message = str(detail["message"])
        except (ValueError, TypeError):
            pass
        raise PaymentStartUncertain(
            message or (
                f"Pagos respondió HTTP {response.status_code}; "
                "se debe consultar el estado antes de reintentar."
            ),
            payment_id=payment_id,
        )

    try:
        data = response.json()
        checkout = PaymentCheckout(
            paymentId=data["payment_id"],
            status=data["status"],
            redirectUrl=data.get("redirect_url"),
            token=data.get("token"),
        )
    except (ValueError, KeyError, TypeError, ValidationError) as error:
        raise PaymentStartUncertain(
            "Pagos respondió, pero no se pudo interpretar el resultado."
        ) from error

    if checkout.paymentId is None:
        raise PaymentStartUncertain(
            "Pagos respondió sin un identificador de pago consultable."
        )

    if require_checkout and checkout.status == "PENDING" and (
        not checkout.redirectUrl or not checkout.token
    ):
        raise PaymentStartUncertain(
            "Pagos inició el cobro, pero faltan los datos para ir a Webpay."
        )

    return checkout
