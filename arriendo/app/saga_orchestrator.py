import logging
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import grpc_client
from app.bd_models import Rental, Saga

logger = logging.getLogger(__name__)


def create_rental(
    db: Session,
    client_id: int,
    equipment_id: int,
    start_date: datetime,
    end_date: datetime,
) -> Rental:
    rental = Rental(
        client_id=client_id,
        equipment_id=equipment_id,
        start_date=start_date,
        end_date=end_date,
        status="PENDING",
    )
    db.add(rental)
    db.flush()

    saga = Saga(
        id=str(uuid4()),
        rental_id=rental.id,
        operation="CREATE",
        status="STARTED",
        current_step="RESERVE_EQUIPMENT",
    )
    db.add(saga)
    db.commit()
    db.refresh(rental)

    try:
        grpc_client.reservar_unidad(equipment_id)
    except HTTPException as error:
        _record_remote_failure(db, saga.id, rental.id, error)
        raise

    try:
        saga.status = "IN_PROGRESS"
        saga.current_step = "EQUIPMENT_RESERVED"
        db.commit()

        rental.status = "ACTIVE"
        saga.status = "COMPLETED"
        saga.current_step = "DONE"
        db.commit()
    except SQLAlchemyError as persistence_error:
        db.rollback()
        compensation_status = _compensate_create(
            db, saga.id, rental.id, equipment_id, persistence_error
        )
        message = (
            "La reserva de Equipos fue compensada."
            if compensation_status == "COMPENSATED"
            else "No se pudo completar la compensación; la Saga requiere revisión."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SAGA_PERSISTENCE_FAILED",
                "message": f"No se pudo persistir el resultado del arriendo. {message}",
            },
        ) from persistence_error
    db.refresh(rental)
    return rental


def finish_rental(db: Session, rental_id: int, final_status: str) -> Rental:
    intermediate_status = (
        "CANCELLING" if final_status == "CANCELLED" else "RETURNING"
    )
    operation = "CANCEL" if final_status == "CANCELLED" else "RETURN"
    rental = db.execute(
        update(Rental)
        .where(Rental.id == rental_id, Rental.status == "ACTIVE")
        .values(status=intermediate_status)
        .returning(Rental)
    ).scalar_one_or_none()

    if rental is None:
        db.rollback()
        _raise_missing_or_inactive_rental(db, rental_id)

    saga = Saga(
        id=str(uuid4()),
        rental_id=rental_id,
        operation=operation,
        status="STARTED",
        current_step="RELEASE_EQUIPMENT",
    )
    db.add(saga)
    db.commit()
    db.refresh(rental)

    equipment_id = rental.equipment_id
    try:
        grpc_client.liberar_unidad(rental.equipment_id)
    except HTTPException as error:
        _record_remote_failure(db, saga.id, rental.id, error)
        raise

    try:
        rental.status = final_status
        saga.status = "COMPLETED"
        saga.current_step = "DONE"
        db.commit()
    except SQLAlchemyError as persistence_error:
        db.rollback()
        compensation_status = _compensate_finish(
            db,
            saga.id,
            rental_id,
            equipment_id,
            intermediate_status,
            persistence_error,
        )
        message = (
            "La unidad fue reservada nuevamente."
            if compensation_status == "COMPENSATED"
            else "No se pudo completar la compensación; la Saga requiere revisión."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SAGA_PERSISTENCE_FAILED",
                "message": f"No se pudo persistir el cierre del arriendo. {message}",
            },
        ) from persistence_error
    db.refresh(rental)
    return rental


def _record_remote_failure(
    db: Session,
    saga_id: str,
    rental_id: int,
    error: HTTPException,
) -> None:
    db.rollback()
    saga = db.get(Saga, saga_id)
    rental = db.get(Rental, rental_id)
    if saga is None or rental is None:
        raise RuntimeError("No se encontró la Saga o el arriendo al registrar el error.")

    saga.error = _http_error_message(error)
    if error.status_code >= 500:
        saga.status = "IN_DOUBT"
    else:
        saga.status = "FAILED"
        if saga.operation == "CREATE":
            rental.status = "FAILED"
        else:
            rental.status = "ACTIVE"
    db.commit()


def _compensate_create(
    db: Session,
    saga_id: str,
    rental_id: int,
    equipment_id: int,
    persistence_error: SQLAlchemyError,
) -> str:
    _mark_compensating(db, saga_id, persistence_error)
    try:
        grpc_client.liberar_unidad(equipment_id)
    except HTTPException as compensation_error:
        _record_compensation_failure(
            db,
            saga_id,
            rental_id,
            persistence_error,
            compensation_error,
            rental_status="FAILED",
        )
        return "COMPENSATION_FAILED"

    saga = db.get(Saga, saga_id)
    rental = db.get(Rental, rental_id)
    if saga is None or rental is None:
        raise RuntimeError("No se encontró la Saga o el arriendo tras la compensación.")
    rental.status = "FAILED"
    saga.status = "COMPENSATED"
    saga.current_step = "COMPENSATION_COMPLETED"
    saga.error = str(persistence_error)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("No se pudo persistir la compensación de la Saga %s.", saga_id)
        return "COMPENSATION_FAILED"
    return "COMPENSATED"


def _compensate_finish(
    db: Session,
    saga_id: str,
    rental_id: int,
    equipment_id: int,
    intermediate_status: str,
    persistence_error: SQLAlchemyError,
) -> str:
    _mark_compensating(db, saga_id, persistence_error)
    try:
        grpc_client.reservar_unidad(equipment_id)
    except HTTPException as compensation_error:
        _record_compensation_failure(
            db,
            saga_id,
            rental_id,
            persistence_error,
            compensation_error,
            rental_status=intermediate_status,
        )
        return "COMPENSATION_FAILED"

    saga = db.get(Saga, saga_id)
    rental = db.get(Rental, rental_id)
    if saga is None or rental is None:
        raise RuntimeError("No se encontró la Saga o el arriendo tras la compensación.")
    rental.status = "ACTIVE"
    saga.status = "COMPENSATED"
    saga.current_step = "COMPENSATION_COMPLETED"
    saga.error = str(persistence_error)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("No se pudo persistir la compensación de la Saga %s.", saga_id)
        return "COMPENSATION_FAILED"
    return "COMPENSATED"


def _mark_compensating(
    db: Session,
    saga_id: str,
    persistence_error: SQLAlchemyError,
) -> None:
    db.rollback()
    saga = db.get(Saga, saga_id)
    if saga is None:
        raise RuntimeError("No se encontró la Saga al iniciar su compensación.")
    saga.status = "COMPENSATING"
    saga.current_step = "COMPENSATE_EQUIPMENT"
    saga.error = str(persistence_error)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("No se pudo registrar el inicio de compensación de la Saga %s.", saga_id)


def _record_compensation_failure(
    db: Session,
    saga_id: str,
    rental_id: int,
    persistence_error: SQLAlchemyError,
    compensation_error: HTTPException,
    rental_status: str,
) -> None:
    db.rollback()
    saga = db.get(Saga, saga_id)
    rental = db.get(Rental, rental_id)
    if saga is None or rental is None:
        raise RuntimeError("No se encontró la Saga o el arriendo tras fallar la compensación.")
    rental.status = rental_status
    saga.status = "COMPENSATION_FAILED"
    saga.current_step = "COMPENSATE_EQUIPMENT"
    saga.error = (
        f"Error de persistencia: {persistence_error}; "
        f"error de compensación: {_http_error_message(compensation_error)}"
    )
    db.commit()


def _raise_missing_or_inactive_rental(db: Session, rental_id: int) -> None:
    rental = db.get(Rental, rental_id)
    if rental is None:
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
            "message": f"No se puede cambiar un arriendo en estado {rental.status}.",
        },
    )


def _http_error_message(error: HTTPException) -> str:
    if isinstance(error.detail, dict):
        return str(error.detail.get("message", error.detail))
    return str(error.detail)
