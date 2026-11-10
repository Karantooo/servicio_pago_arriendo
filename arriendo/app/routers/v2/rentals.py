import json
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.auth import admin_validation, client_validation
from app.bd_models import Client, Rental, Saga
from app.database import get_db
from app.models import (
    BadGatewayError,
    BadRequestError,
    ConflictError,
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
from app.saga_orchestrator import create_rental, finish_rental

router = APIRouter(prefix="/v2/rentals", tags=["Rentals v2"])
CACHE_HEADER = "CACHE_STATUS"


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
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation),
) -> RentalCreationResponse:
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

    return SagaResponse(
        id=saga.id,
        rentalId=saga.rental_id,
        operation=saga.operation,
        status=saga.status,
        currentStep=saga.current_step,
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
