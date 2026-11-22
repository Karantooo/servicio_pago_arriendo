import json
from fastapi import APIRouter, HTTPException, Depends, status, Query, Path, Response
from datetime import timezone
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy import update

from app.database import get_db
from app.bd_models import Client, Rental
from app.grpc_client import reservar_unidad, liberar_unidad
from app.auth import client_validation, admin_validation

from app.models import (
    RentalRequest,
    RentalResponse,
    BadRequestError,
    NotAuthorizedError,
    ForbiddenError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    GatewayTimeoutError,
    BadGatewayError,
    InternalServerError,
)

from app.redis import RentalCache, get_rental_cache

router = APIRouter(prefix='/v1/rentals', tags=['Rentals'])
CACHE_HEADER = "CACHE_STATUS"   # Solo se usa si ACTIVATE_REDIS es True

@router.post(
    '',
    response_model=RentalResponse,
    status_code=201,
    responses={
        '400': {'model': BadRequestError},
        '401': {'model': NotAuthorizedError},
        '403': {'model': ForbiddenError},
        '404': {'model': NotFoundError},
        '409': {'model': ConflictError},
        '500': {'model': InternalServerError},
        '502': {'model': BadGatewayError},
        '503': {'model': ServiceUnavailableError},
        '504': {'model': GatewayTimeoutError},
    }
)
def create_rental(
    rental_in: RentalRequest,
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation)
) -> RentalResponse:
    # 1. Validar fechas
    if rental_in.endDate <= rental_in.startDate:    #QUESTION: Esta bien que sea <= o deberia ser < ?
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "endDate debe ser posterior a startDate."},
        )

    # 2. Verificar que el cliente exista
    client = db.query(Client).filter(Client.id == rental_in.clientId).first()
    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CLIENT_NOT_FOUND", "message": f"El cliente {rental_in.clientId} no existe."}
        )

    reservar_unidad(rental_in.equipmentId)

    # 3. Guardar en la base de datos local
    db_rental = Rental(
        client_id=rental_in.clientId,
        equipment_id=rental_in.equipmentId,
        start_date=rental_in.startDate,
        end_date=rental_in.endDate,
        status="ACTIVE"
    )
    
    db.add(db_rental)
    try:
        db.commit()
    except Exception:
        db.rollback()
        try:
            liberar_unidad(rental_in.equipmentId)
        except HTTPException as compensation_error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": "RENTAL_COMPENSATION_FAILED",
                    "message": (
                        "No se pudo guardar el arriendo ni liberar "
                        "la unidad reservada."
                    ),
                },
            ) from compensation_error
        raise
    db.refresh(db_rental)

    cache.invalidate()
    
    return build_rental_response(db_rental)


@router.get(
    '',
    response_model=List[RentalResponse],
    responses={
        '400': {'model': BadRequestError},
        '401': {'model': NotAuthorizedError},
        '403': {'model': ForbiddenError},
    }
)
def get_all_rentals(
    response: Response,
    limit: int = Query(20, ge=1, le=100, description="Límite de resultados"),
    offset: int = Query(0, ge=0, description="Desplazamiento para paginación"),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    client_auth: dict = Depends(client_validation)
) -> List[RentalResponse]:
    cache_key = f"rentals:list:{limit}:{offset}"
    cached = cache.get(cache_key)
    if cached:
        results = [RentalResponse.model_validate(r) for r in json.loads(cached)]
        if cache.enabled:
            response.headers[CACHE_HEADER] = "HIT"
        return results

    rentals = db.query(Rental).order_by(Rental.id).offset(offset).limit(limit).all()
    results = [build_rental_response(r) for r in rentals]

    cache.set(
        cache_key,
        json.dumps([r.model_dump(mode="json") for r in results]),
    )
    if cache.enabled:
        response.headers[CACHE_HEADER] = "MISS"
    
    return results

@router.get(
    '/{rentalId}',
    response_model=RentalResponse,
    responses={
        '400': {'model': BadRequestError},
        '401': {'model': NotAuthorizedError},
        '403': {'model': ForbiddenError},
        '404': {'model': NotFoundError},
    }
)
def get_rental_by_id(
    response: Response,
    rentalId: int = Path(..., ge=1, description="ID del arriendo"),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    client_auth: dict = Depends(client_validation)
) -> RentalResponse:
    cache_key = f"rental:{rentalId}"
    cached = cache.get(cache_key)
    if cached:
        result = RentalResponse.model_validate(json.loads(cached))
        if cache.enabled:
            response.headers[CACHE_HEADER] = "HIT"
        return result

    rental = db.query(Rental).filter(Rental.id == rentalId).first()
    if not rental:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RENTAL_NOT_FOUND", "message": f"Arriendo {rentalId} no encontrado."}
        )

    result = build_rental_response(rental)

    cache.set(cache_key, json.dumps(result.model_dump(mode="json")))
    if cache.enabled:
        response.headers[CACHE_HEADER] = "MISS"

    return result

@router.post(
    '/{rentalId}/cancel',
    response_model=RentalResponse,
    responses={
        '400': {'model': BadRequestError},
        '401': {'model': NotAuthorizedError},
        '403': {'model': ForbiddenError},
        '404': {'model': NotFoundError},
        '409': {'model': ConflictError},
        '500': {'model': InternalServerError},
        '502': {'model': BadGatewayError},
        '503': {'model': ServiceUnavailableError},
        '504': {'model': GatewayTimeoutError},
    },
    tags=['Rentals'],
)
def cancel_rental(
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation)
) -> RentalResponse:
    return _finish_rental(rentalId, "CANCELLED", db, cache)


@router.post(
    '/{rentalId}/return',
    response_model=RentalResponse,
    responses={
        '400': {'model': BadRequestError},
        '401': {'model': NotAuthorizedError},
        '403': {'model': ForbiddenError},
        '404': {'model': NotFoundError},
        '409': {'model': ConflictError},
        '500': {'model': InternalServerError},
        '502': {'model': BadGatewayError},
        '503': {'model': ServiceUnavailableError},
        '504': {'model': GatewayTimeoutError},
    },
    tags=['Rentals'],
)
def return_rental(
    rentalId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    cache: RentalCache = Depends(get_rental_cache),
    admin_auth: dict = Depends(admin_validation)
) -> RentalResponse:
    return _finish_rental(rentalId, "COMPLETED", db, cache)


def _finish_rental(
    rental_id: int,
    final_status: str,
    db: Session,
    cache: RentalCache,
) -> RentalResponse:
    rental = db.execute(
        update(Rental)
        .where(Rental.id == rental_id, Rental.status == "ACTIVE")
        .values(status=final_status)
        .returning(Rental)
    ).scalar_one_or_none()

    if rental is None:
        db.rollback()
        current = db.get(Rental, rental_id)

        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "RENTAL_NOT_FOUND",
                    "message": f"Arriendo {rental_id} no encontrado.",
                },
            )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INVALID_STATE_TRANSITION",
                "message": (
                    f"No se puede cambiar un arriendo "
                    f"que está en estado {current.status}."
                ),
            },
        )

    response = build_rental_response(rental)

    try:
        if rental.equipment_reservation_id is None:
            liberar_unidad(rental.equipment_id)
        else:
            liberar_unidad(rental.equipment_id, rental.equipment_reservation_id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    cache.invalidate(rental_id)
    return response

def build_rental_response(rental: Rental) -> RentalResponse:
    start_date = (
        rental.start_date.replace(tzinfo=timezone.utc)
        if rental.start_date.tzinfo is None
        else rental.start_date
    )
    end_date = (
        rental.end_date.replace(tzinfo=timezone.utc)
        if rental.end_date.tzinfo is None
        else rental.end_date
    )
    return RentalResponse(
        id=rental.id,
        clientId=rental.client_id,
        equipmentId=rental.equipment_id,
        startDate=start_date,
        endDate=end_date,
        status=rental.status
    )