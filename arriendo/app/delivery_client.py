import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx


DELIVERIES_API_URL = os.getenv(
    "DELIVERIES_API_URL", "http://entregas-api:8081"
).rstrip("/")
DELIVERIES_API_KEY = os.getenv("DELIVERIES_API_KEY") or os.getenv(
    "ADMIN_API_KEY", ""
)
DELIVERIES_TIMEOUT_SECONDS = float(
    os.getenv("DELIVERIES_TIMEOUT_SECONDS", "5")
)

if DELIVERIES_TIMEOUT_SECONDS <= 0:
    raise ValueError(
        "DELIVERIES_TIMEOUT_SECONDS debe ser mayor que cero"
    )


class DeliverySchedulingRejected(Exception):
    """Entregas rechazo la solicitud de agendamiento."""


class DeliverySchedulingUncertain(Exception):
    """No se pudo confirmar si Entregas agendo la entrega."""


@dataclass(frozen=True)
class ScheduledDelivery:
    delivery_id: int
    status: str


def scheduled_at_for_rental(
    start_date: datetime,
    end_date: datetime,
) -> datetime:
    now = datetime.now(timezone.utc)
    start = (
        start_date.replace(tzinfo=timezone.utc)
        if start_date.tzinfo is None
        else start_date
    )
    end = (
        end_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None
        else end_date
    )

    if start > now:
        return start

    fallback = now + timedelta(minutes=1)
    if fallback <= end:
        return fallback

    raise DeliverySchedulingRejected(
        "La fecha de inicio o fin del arriendo ya no permite agendar una entrega futura."
    )


def agendar_entrega(
    rental_id: int,
    client_id: int,
    equipment_id: int,
    scheduled_at: datetime,
    payment_id: int,
    idempotency_key: str,
) -> ScheduledDelivery:
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    scheduled_at = scheduled_at.astimezone(timezone.utc)

    try:
        response = httpx.post(
            f"{DELIVERIES_API_URL}/v1/deliveries",
            headers={
                "Idempotency-Key": idempotency_key,
                "API-Key": DELIVERIES_API_KEY,
            },
            json={
                "rentalId": rental_id,
                "clientId": client_id,
                "equipmentId": equipment_id,
                "scheduledAt": scheduled_at.isoformat().replace("+00:00", "Z"),
                "paymentStatus": "PAID",
                "paymentId": payment_id,
            },
            timeout=DELIVERIES_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as error:
        raise DeliverySchedulingUncertain(
            "No se recibió una respuesta de Entregas."
        ) from error

    if response.status_code == 409 and _delivery_error_code(response) in {
        "IDEMPOTENCY_KEY_REUSED",
        "IDEMPOTENCY_IN_PROGRESS",
        "DELIVERY_ALREADY_EXISTS",
        "RENTAL_NOT_READY_FOR_DELIVERY",
    }:
        raise DeliverySchedulingUncertain(
            _delivery_error_message(response, "El estado de la entrega requiere conciliación.")
        )

    if response.status_code in {400, 409}:
        raise DeliverySchedulingRejected(
            _delivery_error_message(response, "Entregas rechazo el agendamiento.")
        )

    if response.status_code not in {200, 201}:
        raise DeliverySchedulingUncertain(
            f"Entregas respondió HTTP {response.status_code}; "
            "se debe consultar el resultado antes de reintentar."
        )

    try:
        data = response.json()
        delivery_id = int(data["id"])
        delivery_status = str(data["status"])
        if delivery_id <= 0 or delivery_status not in {"SCHEDULED", "DELIVERED", "CANCELLED"}:
            raise ValueError("Respuesta de entrega inválida")
    except (ValueError, KeyError, TypeError) as error:
        raise DeliverySchedulingUncertain(
            "Entregas respondió, pero no se pudo interpretar el resultado."
        ) from error

    return ScheduledDelivery(
        delivery_id=delivery_id,
        status=delivery_status,
    )


def cancelar_entrega(delivery_id: int) -> None:
    try:
        response = httpx.post(
            f"{DELIVERIES_API_URL}/v1/deliveries/{delivery_id}/cancel",
            headers={"API-Key": DELIVERIES_API_KEY},
            json={"reason": "Cancelación del arriendo"},
            timeout=DELIVERIES_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            data = response.json()
            if data["id"] == delivery_id and data["status"] == "CANCELLED":
                return
    except (httpx.RequestError, ValueError, KeyError, TypeError):
        pass

    # La entrega pudo cancelarse aunque se perdiera la respuesta o su callback.
    if consultar_entrega(delivery_id).status != "CANCELLED":
        raise DeliverySchedulingUncertain("Entregas no confirmó la cancelación.")


def consultar_entrega(delivery_id: int) -> ScheduledDelivery:
    try:
        response = httpx.get(
            f"{DELIVERIES_API_URL}/v1/deliveries/{delivery_id}",
            headers={"API-Key": DELIVERIES_API_KEY},
            timeout=DELIVERIES_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as error:
        raise DeliverySchedulingUncertain(
            "No se recibió una respuesta de Entregas."
        ) from error

    if response.status_code != 200:
        raise DeliverySchedulingUncertain(
            f"Entregas respondió HTTP {response.status_code} "
            "al consultar la entrega."
        )

    try:
        data = response.json()
        result = ScheduledDelivery(
            delivery_id=int(data["id"]),
            status=str(data["status"]),
        )
        if result.delivery_id != delivery_id:
            raise ValueError("El ID de entrega no coincide")
    except (ValueError, KeyError, TypeError) as error:
        raise DeliverySchedulingUncertain(
            "Entregas respondió, pero no se pudo interpretar la entrega."
        ) from error
    return result


def _delivery_error_message(response: httpx.Response, fallback: str) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        if isinstance(data, dict) and data.get("detail"):
            detail = data["detail"]
            if isinstance(detail, dict) and detail.get("message"):
                return str(detail["message"])
    except ValueError:
        pass
    return fallback


def _delivery_error_code(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    detail = data.get("detail")
    if isinstance(detail, dict):
        return detail.get("code")
    return data.get("code")
