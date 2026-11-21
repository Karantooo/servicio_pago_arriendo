import logging
import json
from hashlib import sha256
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import admin_validation, client_validation
from app.bd_models import Client, Rental, Saga
from app.bd_models import RentalDeliveryNotification
from app.database import get_db
from app.models import (
    BadGatewayError,
    BadRequestError,
    ConflictError,
    DeliveryStatusNotification,
    ForbiddenError,
    GatewayTimeoutError,
    InternalServerError,
    NotAuthorizedError,
    NotFoundError,
    RentalRequest,
    RentalCreationResponse,
    RentalResponse,
    SagaResponse,
    ServiceUnavailableError,
)
from app.redis import RentalCache, get_rental_cache
from app.pricing import calculate_rental_amounts
from app.saga_orchestrator import create_rental, find_creation_by_key, finish_rental, reconcile_payment

router = APIRouter(prefix="/v2/rentals", tags=["Rentals v2"])
CACHE_HEADER = "CACHE_STATUS"
logger = logging.getLogger(__name__)


@router.post(
    "",
    response_model=RentalCreationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"model": NotFoundError},
        409: {"model": ConflictError},
        500: {"model": InternalServerError},
        502: {"model": BadGatewayError},
        503: {"model": ServiceUnavailableError},
        504: {"model": GatewayTimeoutError},
    },
)
def create_rental_endpoint(
    rental_in: RentalRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^\S+$"
    ),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation),
) -> RentalCreationResponse:
    request_fingerprint = sha256(
        json.dumps(
            rental_in.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    existing = find_creation_by_key(db, idempotency_key, request_fingerprint)
    if existing is not None:
        rental, checkout = existing
        return RentalCreationResponse(
            rental=_build_rental_response(rental), payment=checkout
        )

    if rental_in.endDate <= rental_in.startDate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": "endDate debe ser posterior a startDate.",
            },
        )

    client = db.query(Client).filter(Client.id == rental_in.clientId).first()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "CLIENT_NOT_FOUND",
                "message": f"El cliente {rental_in.clientId} no existe.",
            },
        )

    amount_rent, amount_deposit = calculate_rental_amounts(
        db, rental_in.equipmentId, rental_in.startDate, rental_in.endDate
    )

    try:
        rental, checkout = create_rental(
            db,
            rental_in.clientId,
            rental_in.equipmentId,
            rental_in.startDate,
            rental_in.endDate,
            amount_rent,
            amount_deposit,
            idempotency_key,
            request_fingerprint,
        )
        return RentalCreationResponse(
            rental=_build_rental_response(rental), payment=checkout
        )
    finally:
        cache.invalidate()


@router.get(
    "",
    response_model=List[RentalResponse],
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
    },
)
def get_all_rentals(
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    client_auth: dict = Depends(client_validation),
) -> List[RentalResponse]:
    cache_key = f"rentals:v2:list:{limit}:{offset}"
    cached = cache.get(cache_key)
    if cached:
        if cache.enabled:
            response.headers[CACHE_HEADER] = "HIT"
        return [RentalResponse.model_validate(row) for row in json.loads(cached)]

    rentals = db.query(Rental).order_by(Rental.id).offset(offset).limit(limit).all()
    results = [_build_rental_response(rental) for rental in rentals]
    cache.set(cache_key, json.dumps([row.model_dump(mode="json") for row in results]))
    if cache.enabled:
        response.headers[CACHE_HEADER] = "MISS"
    return results


@router.get(
    "/{rentalId}",
    response_model=RentalResponse,
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"model": NotFoundError},
    },
)
def get_rental_by_id(
    response: Response,
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    client_auth: dict = Depends(client_validation),
) -> RentalResponse:
    cache_key = f"rental:v2:{rentalId}"
    cached = cache.get(cache_key)
    if cached:
        if cache.enabled and response is not None:
            response.headers[CACHE_HEADER] = "HIT"
        return RentalResponse.model_validate(json.loads(cached))

    rental = db.query(Rental).filter(Rental.id == rentalId).first()
    if rental is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RENTAL_NOT_FOUND",
                "message": f"Arriendo {rentalId} no encontrado.",
            },
        )

    result = _build_rental_response(rental)
    cache.set(cache_key, json.dumps(result.model_dump(mode="json")))
    if cache.enabled and response is not None:
        response.headers[CACHE_HEADER] = "MISS"
    return result


@router.get(
    "/{rentalId}/saga",
    response_model=SagaResponse,
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"model": NotFoundError},
    },
)
def get_latest_saga(
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    client_auth: dict = Depends(client_validation),
) -> SagaResponse:
    rental = db.get(Rental, rentalId)
    if rental is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RENTAL_NOT_FOUND",
                "message": f"Arriendo {rentalId} no encontrado.",
            },
        )

    saga = (
        db.query(Saga)
        .filter(Saga.rental_id == rentalId)
        .order_by(Saga.created_at.desc(), Saga.id.desc())
        .first()
    )
    if saga is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SAGA_NOT_FOUND",
                "message": f"No hay una Saga registrada para el arriendo {rentalId}.",
            },
        )

    return _build_saga_response(saga)


@router.put(
    "/{rentalId}/delivery-status",
    response_model=RentalResponse,
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"model": NotFoundError},
        409: {"model": ConflictError},
        500: {"model": InternalServerError},
        502: {"model": BadGatewayError},
        503: {"model": ServiceUnavailableError},
        504: {"model": GatewayTimeoutError},
    },
)
def notify_delivery_status(
    notification: DeliveryStatusNotification,
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation),
) -> RentalResponse:
    try:
        rental = db.get(Rental, rentalId)
        if rental is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "RENTAL_NOT_FOUND",
                    "message": f"Arriendo {rentalId} no encontrado.",
                },
            )

        existing = (
            db.query(RentalDeliveryNotification)
            .filter(
                RentalDeliveryNotification.delivery_id == notification.deliveryId,
                RentalDeliveryNotification.status == notification.status,
            )
            .first()
        )
        if existing is not None:
            if existing.rental_id != rentalId:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "DELIVERY_DATA_MISMATCH",
                        "message": "El ID de entrega ya está asociado a otro arriendo.",
                    },
                )
            return _build_rental_response(rental)

        opposite_status = (
            "CANCELLED" if notification.status == "DELIVERED" else "DELIVERED"
        )
        previous_event = (
            db.query(RentalDeliveryNotification)
            .filter(
                RentalDeliveryNotification.delivery_id == notification.deliveryId,
                RentalDeliveryNotification.status == opposite_status,
            )
            .first()
        )
        if previous_event is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "INVALID_STATE_TRANSITION",
                    "message": "La entrega ya fue notificada con un estado final diferente.",
                },
            )

        if notification.status == "DELIVERED":
            if rental.status == "PENDING":
                saga = (
                    db.query(Saga)
                    .filter(Saga.rental_id == rentalId, Saga.operation == "CREATE")
                    .order_by(Saga.created_at.desc(), Saga.id.desc())
                    .first()
                )
                if (
                    saga is None
                    or saga.status not in {"IN_PROGRESS", "COMPLETED"}
                    or saga.current_step
                    not in {"PAYMENT_CONFIRMED", "WAITING_FOR_DELIVERY", "DELIVERY_SCHEDULED"}
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "RENTAL_NOT_READY_FOR_DELIVERY",
                            "message": "El arriendo aún no está listo para activar su entrega.",
                        },
                    )
                rental.status = "ACTIVE"
                saga.status = "COMPLETED"
                saga.current_step = "DONE"
                saga.error = None
            elif rental.status != "ACTIVE":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "INVALID_STATE_TRANSITION",
                        "message": f"No se puede activar un arriendo en estado {rental.status}.",
                    },
                )
        elif rental.status in {"PENDING", "ACTIVE"}:
            finish_rental(db, rentalId, "CANCELLED")
            rental = db.get(Rental, rentalId)
        elif rental.status != "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "INVALID_STATE_TRANSITION",
                    "message": f"No se puede cancelar un arriendo en estado {rental.status}.",
                },
            )

        db.add(
            RentalDeliveryNotification(
                rental_id=rentalId,
                delivery_id=notification.deliveryId,
                status=notification.status,
                reason=notification.reason,
                occurred_at=notification.occurredAt,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicate = (
                db.query(RentalDeliveryNotification)
                .filter(
                    RentalDeliveryNotification.delivery_id
                    == notification.deliveryId,
                    RentalDeliveryNotification.status == notification.status,
                )
                .first()
            )
            if duplicate is not None and duplicate.rental_id == rentalId:
                rental = db.get(Rental, rentalId)
                return _build_rental_response(rental)
            if duplicate is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "DELIVERY_DATA_MISMATCH",
                        "message": "El ID de entrega ya está asociado a otro arriendo.",
                    },
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "INVALID_STATE_TRANSITION",
                    "message": "La notificación de Entregas se procesó simultáneamente.",
                },
            )
        except SQLAlchemyError as error:
            db.rollback()
            logger.exception(
                "No se pudo persistir la notificación de entrega para el arriendo %s.",
                rentalId,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": "RENTAL_PERSISTENCE_ERROR",
                    "message": "No se pudo persistir la notificación de Entregas.",
                },
            ) from error
        db.refresh(rental)
        return _build_rental_response(rental)
    finally:
        cache.invalidate(rentalId)


@router.post(
    "/{rentalId}/reconcile-payment",
    response_model=SagaResponse,
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"model": NotFoundError},
        409: {"model": ConflictError},
        502: {"model": BadGatewayError},
        503: {"model": ServiceUnavailableError},
    },
)
def reconcile_payment_endpoint(
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation),
) -> SagaResponse:
    try:
        return _build_saga_response(reconcile_payment(db, rentalId))
    finally:
        cache.invalidate(rentalId)


def _build_saga_response(saga: Saga) -> SagaResponse:
    return SagaResponse(
        id=saga.id,
        rentalId=saga.rental_id,
        operation=saga.operation,
        status=saga.status,
        currentStep=saga.current_step,
        deliveryId=saga.delivery_id,
        refundId=saga.refund_id,
        refundStatus=saga.refund_status,
        equipmentReleaseStatus=saga.equipment_release_status,
        error=saga.error,
        createdAt=_utc(saga.created_at),
        updatedAt=_utc(saga.updated_at),
    )


@router.post(
    "/{rentalId}/cancel",
    response_model=RentalResponse,
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"model": NotFoundError},
        409: {"model": ConflictError},
        500: {"model": InternalServerError},
        502: {"model": BadGatewayError},
        503: {"model": ServiceUnavailableError},
        504: {"model": GatewayTimeoutError},
    },
)
def cancel_rental(
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation),
) -> RentalResponse:
    return _finish_rental_endpoint(rentalId, "CANCELLED", db, cache)


@router.post(
    "/{rentalId}/return",
    response_model=RentalResponse,
    responses={
        400: {"model": BadRequestError},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"model": NotFoundError},
        409: {"model": ConflictError},
        500: {"model": InternalServerError},
        502: {"model": BadGatewayError},
        503: {"model": ServiceUnavailableError},
        504: {"model": GatewayTimeoutError},
    },
)
def return_rental(
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation),
) -> RentalResponse:
    return _finish_rental_endpoint(rentalId, "COMPLETED", db, cache)


def _finish_rental_endpoint(
    rental_id: int,
    final_status: str,
    db: Session,
    cache: RentalCache,
) -> RentalResponse:
    try:
        rental = finish_rental(db, rental_id, final_status)
        return _build_rental_response(rental)
    finally:
        cache.invalidate(rental_id)


def _build_rental_response(rental: Rental) -> RentalResponse:
    return RentalResponse(
        id=rental.id,
        clientId=rental.client_id,
        equipmentId=rental.equipment_id,
        startDate=_utc(rental.start_date),
        endDate=_utc(rental.end_date),
        status=rental.status,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
