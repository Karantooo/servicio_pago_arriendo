import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from entregas_api.apis.deliveries_api_base import BaseDeliveriesApi
from entregas_api.database import SessionLocal
from entregas_api.db_models import Delivery, DeliveryEventRecord, IdempotencyKey
from entregas_api.models.cancel_request import CancelRequest
from entregas_api.models.deliver_request import DeliverRequest
from entregas_api.models.delivery_event import DeliveryEvent
from entregas_api.models.delivery_request import DeliveryRequest
from entregas_api.models.delivery_response import DeliveryResponse
from entregas_api.models.delivery_status import DeliveryStatus
from entregas_api.models.mark_paid_request import MarkPaidRequest
from entregas_api.models.payment_status import PaymentStatus
from entregas_api.models.rental_sync_status import RentalSyncStatus
from entregas_api.models.reschedule_request import RescheduleRequest
from entregas_api.rentals_client import RentalsClient, parse_rental_datetime

logger = logging.getLogger(__name__)


def _http_error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _bad_request(message: str) -> HTTPException:
    return _http_error("BAD_REQUEST", message, status.HTTP_400_BAD_REQUEST)


def _conflict(code: str, message: str) -> HTTPException:
    return _http_error(code, message, status.HTTP_409_CONFLICT)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _bad_request(f"{field} debe incluir una zona horaria.")
    return value.astimezone(timezone.utc)


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _to_response(delivery: Delivery) -> DeliveryResponse:
    return DeliveryResponse(
        id=delivery.id,
        rental_id=delivery.rental_id,
        client_id=delivery.client_id,
        equipment_id=delivery.equipment_id,
        status=DeliveryStatus(delivery.status),
        payment_status=PaymentStatus(delivery.payment_status),
        payment_id=delivery.payment_id,
        scheduled_at=_utc(delivery.scheduled_at),
        delivered_at=_utc(delivery.delivered_at),
        received_by=delivery.received_by,
        notes=delivery.notes,
        cancel_reason=delivery.cancel_reason,
        rental_sync_status=RentalSyncStatus(delivery.rental_sync_status),
        created_at=_utc(delivery.created_at),
        updated_at=_utc(delivery.updated_at),
    )


def _get_delivery(db: Session, delivery_id: int) -> Delivery:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise _http_error(
            "RESOURCE_NOT_FOUND",
            "La entrega solicitada no existe.",
            status.HTTP_404_NOT_FOUND,
        )
    return delivery


def _add_event(
    db: Session,
    delivery_id: int,
    event_type: str,
    detail: str | None = None,
    created_at: datetime | None = None,
) -> None:
    db.add(
        DeliveryEventRecord(
            delivery_id=delivery_id,
            type=event_type,
            detail=detail,
            created_at=created_at or _now(),
        )
    )


def _commit(db: Session, message: str) -> None:
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        logger.exception("%s", message)
        raise _http_error(
            "DELIVERY_PERSISTENCE_ERROR",
            "No se pudo guardar el estado de la entrega.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from error


def _rental_is_ready(rental: dict[str, Any], request: DeliveryRequest) -> datetime:
    if rental["clientId"] != request.client_id or rental["equipmentId"] != request.equipment_id:
        raise _conflict(
            "RENTAL_DATA_MISMATCH",
            "clientId o equipmentId no coinciden con el arriendo.",
        )
    if rental["status"] in {"CANCELLED", "COMPLETED", "FAILED"}:
        raise _conflict(
            "RENTAL_NOT_READY_FOR_DELIVERY",
            f"No se puede agendar una entrega para un arriendo en estado {rental['status']}.",
        )

    scheduled_at = _require_aware(request.scheduled_at, "scheduledAt")
    if scheduled_at <= _now():
        raise _bad_request("scheduledAt debe ser una fecha futura.")
    end_date = parse_rental_datetime(rental["endDate"])
    if scheduled_at > end_date:
        raise _bad_request("scheduledAt no puede ser posterior al fin del arriendo.")
    return scheduled_at


def _require_confirmed_payment(saga: dict[str, Any], payment_id: int | None) -> None:
    if (
        saga.get("operation") != "CREATE"
        or saga.get("paymentStatus") != "CHARGED"
        or saga.get("refundStatus") is not None
        or payment_id is None
        or saga.get("paymentId") != payment_id
    ):
        raise _conflict(
            "PAYMENT_REQUIRED",
            "La entrega requiere el cobro confirmado de este arriendo.",
        )


def _validate_paid_saga(saga: dict[str, Any], payment_id: int | None) -> None:
    step = saga.get("currentStep")
    status = saga.get("status")
    ready = (
        status in {"IN_PROGRESS", "COMPLETED"}
        and step in {"PAYMENT_CONFIRMED", "WAITING_FOR_DELIVERY"}
    ) or (status == "IN_DOUBT" and step == "DELIVERY_SCHEDULING_IN_DOUBT")
    if saga.get("operation") != "CREATE" or not ready:
        raise _conflict(
            "RENTAL_NOT_READY_FOR_DELIVERY",
            "La Saga del arriendo no está esperando la entrega.",
        )
    _require_confirmed_payment(saga, payment_id)


def _request_fingerprint(request: DeliveryRequest) -> str:
    payload = request.model_dump(mode="json", by_alias=True)
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _return_existing_idempotent(
    db: Session, key: str, fingerprint: str
) -> DeliveryResponse | None:
    key_record = db.get(IdempotencyKey, key)
    if key_record is None:
        return None
    if key_record.request_fingerprint != fingerprint:
        raise _conflict(
            "IDEMPOTENCY_KEY_REUSED",
            "La clave de idempotencia ya se usó con otros datos.",
        )
    if key_record.delivery_id is None:
        raise _conflict(
            "IDEMPOTENCY_IN_PROGRESS",
            "La solicitud original todavía está en curso.",
        )
    delivery = db.get(Delivery, key_record.delivery_id)
    if delivery is None:
        raise _http_error(
            "DELIVERY_PERSISTENCE_ERROR",
            "La clave de idempotencia apunta a una entrega inexistente.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return _to_response(delivery)


class DeliveriesApi(BaseDeliveriesApi):
    async def get_all_deliveries(
        self,
        limit: int | None,
        offset: int | None,
        status_filter: DeliveryStatus | None,
        payment_status: PaymentStatus | None,
        scheduled_from: datetime | None,
        scheduled_to: datetime | None,
    ) -> list[DeliveryResponse]:
        with SessionLocal() as db:
            query = db.query(Delivery)
            if status_filter is not None:
                query = query.filter(Delivery.status == _enum_value(status_filter))
            if payment_status is not None:
                query = query.filter(
                    Delivery.payment_status == _enum_value(payment_status)
                )
            if scheduled_from is not None:
                query = query.filter(
                    Delivery.scheduled_at >= _require_aware(scheduled_from, "scheduledFrom")
                )
            if scheduled_to is not None:
                query = query.filter(
                    Delivery.scheduled_at <= _require_aware(scheduled_to, "scheduledTo")
                )
            rows = (
                query.order_by(Delivery.scheduled_at, Delivery.id)
                .offset(offset or 0)
                .limit(limit or 20)
                .all()
            )
            return [_to_response(row) for row in rows]

    async def create_delivery(
        self, idempotency_key: str, delivery_request: DeliveryRequest
    ) -> DeliveryResponse:
        if delivery_request.payment_status != PaymentStatus.PAID or delivery_request.payment_id is None:
            raise _conflict("PAYMENT_REQUIRED", "Indique el pago confirmado del arriendo.")
        fingerprint = _request_fingerprint(delivery_request)
        with SessionLocal() as db:
            existing = _return_existing_idempotent(db, idempotency_key, fingerprint)
            if existing is not None:
                return existing

        rentals = RentalsClient()
        rental = await rentals.get_rental(delivery_request.rental_id)
        scheduled_at = _rental_is_ready(rental, delivery_request)
        _validate_paid_saga(
            await rentals.get_saga(delivery_request.rental_id),
            delivery_request.payment_id,
        )

        with SessionLocal() as db:
            row = Delivery(
                rental_id=delivery_request.rental_id,
                client_id=delivery_request.client_id,
                equipment_id=delivery_request.equipment_id,
                status="SCHEDULED",
                payment_status=_enum_value(delivery_request.payment_status),
                payment_id=delivery_request.payment_id,
                scheduled_at=scheduled_at,
                notes=delivery_request.notes,
                rental_sync_status="NOT_REQUIRED",
            )
            db.add(row)
            try:
                db.flush()
                db.add(
                    IdempotencyKey(
                        key=idempotency_key,
                        request_fingerprint=fingerprint,
                        delivery_id=row.id,
                    )
                )
                _add_event(db, row.id, "SCHEDULED", delivery_request.notes)
                db.commit()
            except IntegrityError as error:
                db.rollback()
                idempotent = _return_existing_idempotent(
                    db, idempotency_key, fingerprint
                )
                if idempotent is not None:
                    return idempotent
                if (
                    db.query(Delivery)
                    .filter(Delivery.rental_id == delivery_request.rental_id)
                    .first()
                    is not None
                ):
                    raise _conflict(
                        "DELIVERY_ALREADY_EXISTS",
                        "Ya existe una entrega para este arriendo.",
                    ) from error
                raise _http_error(
                    "DELIVERY_PERSISTENCE_ERROR",
                    "No se pudo guardar el estado de la entrega.",
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                ) from error
            except SQLAlchemyError as error:
                db.rollback()
                logger.exception("No se pudo crear la entrega.")
                raise _http_error(
                    "DELIVERY_PERSISTENCE_ERROR",
                    "No se pudo guardar el estado de la entrega.",
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                ) from error
            db.refresh(row)
            return _to_response(row)

    async def get_delivery_by_id(self, deliveryId: int) -> DeliveryResponse:
        with SessionLocal() as db:
            return _to_response(_get_delivery(db, deliveryId))

    async def get_delivery_by_rental(self, rentalId: int) -> DeliveryResponse:
        with SessionLocal() as db:
            delivery = (
                db.query(Delivery).filter(Delivery.rental_id == rentalId).first()
            )
            if delivery is None:
                raise _http_error(
                    "RESOURCE_NOT_FOUND",
                    "El arriendo no tiene una entrega registrada.",
                    status.HTTP_404_NOT_FOUND,
                )
            return _to_response(delivery)

    async def get_delivery_events(self, deliveryId: int) -> list[DeliveryEvent]:
        with SessionLocal() as db:
            _get_delivery(db, deliveryId)
            events = (
                db.query(DeliveryEventRecord)
                .filter(DeliveryEventRecord.delivery_id == deliveryId)
                .order_by(DeliveryEventRecord.created_at, DeliveryEventRecord.id)
                .all()
            )
            return [
                DeliveryEvent(
                    id=event.id,
                    delivery_id=event.delivery_id,
                    type=event.type,
                    detail=event.detail,
                    created_at=_utc(event.created_at),
                )
                for event in events
            ]

    async def reschedule_delivery(
        self, deliveryId: int, reschedule_request: RescheduleRequest
    ) -> DeliveryResponse:
        new_scheduled_at = _require_aware(
            reschedule_request.scheduled_at, "scheduledAt"
        )
        rentals = RentalsClient()
        with SessionLocal() as db:
            delivery = _get_delivery(db, deliveryId)
            if delivery.status != "SCHEDULED":
                raise _conflict(
                    "INVALID_STATE_TRANSITION",
                    "Solo se puede reagendar una entrega en estado SCHEDULED.",
                )
            rental_id = delivery.rental_id
            payment_id = delivery.payment_id
            client_id = delivery.client_id
            equipment_id = delivery.equipment_id

        rental_data = await rentals.get_rental(rental_id)
        if (
            rental_data["clientId"] != client_id
            or rental_data["equipmentId"] != equipment_id
        ):
            raise _conflict(
                "RENTAL_DATA_MISMATCH",
                "Los datos del arriendo ya no coinciden con la entrega.",
            )
        if rental_data["status"] in {"CANCELLED", "COMPLETED", "FAILED"}:
            raise _conflict(
                "RENTAL_NOT_READY_FOR_DELIVERY",
                "El arriendo no admite cambios en la fecha de entrega.",
            )
        if new_scheduled_at <= _now():
            raise _bad_request("scheduledAt debe ser una fecha futura.")
        if new_scheduled_at > parse_rental_datetime(rental_data["endDate"]):
            raise _bad_request(
                "scheduledAt no puede ser posterior al fin del arriendo."
            )

        saga = await rentals.get_saga(rental_id)
        if saga.get("status") not in {"IN_PROGRESS", "IN_DOUBT", "COMPLETED"}:
            raise _conflict("RENTAL_NOT_READY_FOR_DELIVERY", "La Saga no admite reagendar la entrega.")
        _require_confirmed_payment(saga, payment_id)
        with SessionLocal() as db:
            delivery = _get_delivery(db, deliveryId)
            if delivery.status != "SCHEDULED":
                raise _conflict(
                    "INVALID_STATE_TRANSITION",
                    "Solo se puede reagendar una entrega en estado SCHEDULED.",
                )
            if rental_data["clientId"] != delivery.client_id or rental_data[
                "equipmentId"
            ] != delivery.equipment_id:
                raise _conflict(
                    "RENTAL_DATA_MISMATCH",
                    "Los datos del arriendo ya no coinciden con la entrega.",
                )
            if delivery.payment_id != payment_id:
                raise _conflict("PAYMENT_REQUIRED", "El pago de la entrega cambió durante la validación.")
            old_scheduled_at = _utc(delivery.scheduled_at)
            delivery.scheduled_at = new_scheduled_at
            detail = (
                f"Reagendada de {old_scheduled_at.isoformat()} "
                f"a {new_scheduled_at.isoformat()}"
            )
            if reschedule_request.reason:
                detail = f"{detail}. Motivo: {reschedule_request.reason}"
            _add_event(db, delivery.id, "RESCHEDULED", detail)
            _commit(db, "No se pudo reagendar la entrega.")
            db.refresh(delivery)
            return _to_response(delivery)

    async def mark_delivery_paid(
        self, deliveryId: int, mark_paid_request: MarkPaidRequest | None
    ) -> DeliveryResponse:
        with SessionLocal() as db:
            delivery = _get_delivery(db, deliveryId)
            if delivery.status == "CANCELLED":
                raise _conflict(
                    "INVALID_STATE_TRANSITION",
                    "No se puede marcar como pagada una entrega cancelada.",
                )
            if delivery.payment_status == "PAID":
                return _to_response(delivery)
            delivery.payment_status = "PAID"
            if mark_paid_request and mark_paid_request.payment_id is not None:
                delivery.payment_id = mark_paid_request.payment_id
            _add_event(
                db,
                delivery.id,
                "PAID",
                f"Pago {delivery.payment_id}" if delivery.payment_id else None,
            )
            _commit(db, "No se pudo registrar el pago de la entrega.")
            db.refresh(delivery)
            return _to_response(delivery)

    async def deliver_delivery(
        self, deliveryId: int, deliver_request: DeliverRequest | None
    ) -> DeliveryResponse:
        with SessionLocal() as db:
            delivery = _get_delivery(db, deliveryId)
            if delivery.status == "DELIVERED":
                if delivery.rental_sync_status == "SYNC_FAILED":
                    raise _http_error(
                        "RENTALS_SERVICE_UNAVAILABLE",
                        "La sincronización con Arriendos falló; use reconcile-rental.",
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                if delivery.rental_sync_status == "SYNCED":
                    return _to_response(delivery)
                event = (
                    db.query(DeliveryEventRecord)
                    .filter(
                        DeliveryEventRecord.delivery_id == delivery.id,
                        DeliveryEventRecord.type == "DELIVERED",
                    )
                    .order_by(DeliveryEventRecord.created_at.desc())
                    .first()
                )
                if event is None:
                    raise _http_error(
                        "DELIVERY_PERSISTENCE_ERROR",
                        "No se encontró el evento final de la entrega.",
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
                return await self._synchronize_rental(
                    db,
                    delivery,
                    "DELIVERED",
                    _utc(event.created_at) or _now(),
                    None,
                )
            if delivery.status != "SCHEDULED":
                raise _conflict(
                    "INVALID_STATE_TRANSITION",
                    "Solo se puede entregar una entrega en estado SCHEDULED.",
                )
            if delivery.payment_status != "PAID":
                raise _conflict(
                    "PAYMENT_REQUIRED",
                    "La entrega debe estar pagada antes de entregarse.",
                )

            now = _now()
            delivery.status = "DELIVERED"
            delivery.delivered_at = now
            delivery.rental_sync_status = "PENDING_SYNC"
            if deliver_request is not None:
                delivery.received_by = deliver_request.received_by
                if deliver_request.notes is not None:
                    delivery.notes = deliver_request.notes
            _add_event(
                db,
                delivery.id,
                "DELIVERED",
                delivery.received_by,
                created_at=now,
            )
            _commit(db, "No se pudo registrar la entrega.")
            db.refresh(delivery)
            return await self._synchronize_rental(
                db,
                delivery,
                "DELIVERED",
                now,
                None,
            )

    async def cancel_delivery(
        self, deliveryId: int, cancel_request: CancelRequest | None
    ) -> DeliveryResponse:
        with SessionLocal() as db:
            delivery = _get_delivery(db, deliveryId)
            if delivery.status == "CANCELLED":
                if delivery.rental_sync_status == "SYNC_FAILED":
                    raise _http_error(
                        "RENTALS_SERVICE_UNAVAILABLE",
                        "La sincronización con Arriendos falló; use reconcile-rental.",
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                if delivery.rental_sync_status == "SYNCED":
                    return _to_response(delivery)
                event = (
                    db.query(DeliveryEventRecord)
                    .filter(
                        DeliveryEventRecord.delivery_id == delivery.id,
                        DeliveryEventRecord.type == "CANCELLED",
                    )
                    .order_by(DeliveryEventRecord.created_at.desc())
                    .first()
                )
                if event is None:
                    raise _http_error(
                        "DELIVERY_PERSISTENCE_ERROR",
                        "No se encontró el evento final de la entrega.",
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
                return await self._synchronize_rental(
                    db,
                    delivery,
                    "CANCELLED",
                    _utc(event.created_at) or _now(),
                    delivery.cancel_reason,
                )
            if delivery.status != "SCHEDULED":
                raise _conflict(
                    "INVALID_STATE_TRANSITION",
                    "Solo se puede cancelar una entrega en estado SCHEDULED.",
                )

            now = _now()
            reason = cancel_request.reason if cancel_request else None
            delivery.status = "CANCELLED"
            delivery.cancel_reason = reason
            delivery.rental_sync_status = "PENDING_SYNC"
            _add_event(
                db,
                delivery.id,
                "CANCELLED",
                reason,
                created_at=now,
            )
            _commit(db, "No se pudo cancelar la entrega.")
            db.refresh(delivery)
            return await self._synchronize_rental(
                db,
                delivery,
                "CANCELLED",
                now,
                reason,
            )

    async def reconcile_delivery_rental(
        self, deliveryId: int
    ) -> DeliveryResponse:
        with SessionLocal() as db:
            delivery = _get_delivery(db, deliveryId)
            if delivery.rental_sync_status != "SYNC_FAILED":
                raise _conflict(
                    "NOTHING_TO_RECONCILE",
                    "No hay un resultado pendiente de sincronizar.",
                )
            event_type = delivery.status
            event = (
                db.query(DeliveryEventRecord)
                .filter(
                    DeliveryEventRecord.delivery_id == delivery.id,
                    DeliveryEventRecord.type == event_type,
                )
                .order_by(DeliveryEventRecord.created_at.desc())
                .first()
            )
            if event is None:
                raise _http_error(
                    "DELIVERY_PERSISTENCE_ERROR",
                    "No se encontró el evento final de la entrega.",
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            delivery.rental_sync_status = "PENDING_SYNC"
            _commit(db, "No se pudo iniciar la conciliación con Arriendos.")
            db.refresh(delivery)
            return await self._synchronize_rental(
                db,
                delivery,
                event_type,
                _utc(event.created_at) or _now(),
                delivery.cancel_reason if event_type == "CANCELLED" else None,
            )

    async def _synchronize_rental(
        self,
        db: Session,
        delivery: Delivery,
        final_status: str,
        occurred_at: datetime,
        reason: str | None,
    ) -> DeliveryResponse:
        payload = {
            "deliveryId": delivery.id,
            "status": final_status,
            "reason": reason,
            "occurredAt": occurred_at.isoformat(),
        }
        try:
            await RentalsClient().notify_delivery_status(delivery.rental_id, payload)
        except HTTPException:
            delivery.rental_sync_status = "SYNC_FAILED"
            _commit(db, "No se pudo persistir el fallo de sincronización con Arriendos.")
            db.refresh(delivery)
            raise

        delivery.rental_sync_status = "SYNCED"
        _commit(db, "No se pudo persistir la sincronización con Arriendos.")
        db.refresh(delivery)
        return _to_response(delivery)
