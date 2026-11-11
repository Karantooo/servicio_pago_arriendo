import os
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import ValidationError

from app.models import PaymentCheckout


PAYMENTS_API_URL = os.getenv(
    "PAYMENTS_API_URL", "http://payments-api:8003"
).rstrip("/")
PAYMENTS_TIMEOUT_SECONDS = float(
    os.getenv("PAYMENTS_TIMEOUT_SECONDS", "3")
)

if PAYMENTS_TIMEOUT_SECONDS <= 0:
    raise ValueError("PAYMENTS_TIMEOUT_SECONDS debe ser mayor que cero")


class PaymentStartRejected(Exception):
    """Pagos rechazó los datos y no inició el cobro."""


class PaymentStartUncertain(Exception):
    """No se pudo confirmar si Pagos inició el cobro."""


class PaymentLookupUncertain(Exception):
    """No se pudo confirmar el estado actual de un pago."""


@dataclass(frozen=True)
class PaymentState:
    payment_id: int
    rental_id: int
    amount_total: int
    status: Literal["PENDING", "CHARGED", "FAILED", "CANCELLED", "UNKNOWN"]
    redirect_url: str | None
    token: str | None


def consultar_pago(payment_id: int) -> PaymentState:
    try:
        response = httpx.get(
            f"{PAYMENTS_API_URL}/payments/{payment_id}",
            timeout=PAYMENTS_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as error:
        raise PaymentLookupUncertain("No se recibió una respuesta de Pagos.") from error

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
            or state.status not in {"PENDING", "CHARGED", "FAILED", "CANCELLED", "UNKNOWN"}
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
    try:
        response = httpx.post(
            f"{PAYMENTS_API_URL}/payments/charge",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "rental_id": rental_id,
                "customer_id": customer_id,
                "amount_rent": amount_rent,
                "amount_deposit": amount_deposit,
            },
            timeout=PAYMENTS_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as error:
        raise PaymentStartUncertain(
            "No se recibió una respuesta de Pagos."
        ) from error

    if response.status_code == 400:
        raise PaymentStartRejected(
            "Pagos rechazó los datos de la solicitud."
        )

    if response.status_code not in (200, 201):
        raise PaymentStartUncertain(
            f"Pagos respondió HTTP {response.status_code}; "
            "se debe consultar el estado antes de reintentar."
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

    if require_checkout and checkout.status == "PENDING" and (
        not checkout.redirectUrl or not checkout.token
    ):
        raise PaymentStartUncertain(
            "Pagos inició el cobro, pero faltan los datos para ir a Webpay."
        )

    return checkout
