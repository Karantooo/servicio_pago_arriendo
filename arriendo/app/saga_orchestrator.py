import logging
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import grpc_client
from app.bd_models import Rental, Saga
from app.models import PaymentCheckout
from app.payments_client import (
    PaymentLookupUncertain,
    PaymentStartRejected,
    PaymentStartUncertain,
    consultar_pago,
    iniciar_cobro,
)

logger = logging.getLogger(__name__)


def find_creation_by_key(
    db: Session, idempotency_key: str, request_fingerprint: str
) -> tuple[Rental, PaymentCheckout] | None:
    saga = (
        db.query(Saga)
        .filter(Saga.idempotency_key == idempotency_key, Saga.operation == "CREATE")
        .first()
    )
    if saga is None:
        return None
    if saga.request_fingerprint != request_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": "La clave de idempotencia ya se usó con otra solicitud.",
            },
        )
    if saga.status in {"FAILED", "COMPENSATED", "COMPENSATION_FAILED"} or saga.payment_id is None or saga.checkout_status is None:
        error_code = (
            "RENTAL_CREATION_UNCERTAIN"
            if saga.status in {"IN_DOUBT", "COMPENSATION_FAILED"}
            else "RENTAL_CREATION_FAILED"
            if saga.status in {"FAILED", "COMPENSATED"}
            else "RENTAL_CREATION_PENDING"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": error_code,
                "message": (
                    f"El arriendo {saga.rental_id} quedó en el paso "
                    f"{saga.current_step} (Saga {saga.id}); consulte su estado antes de reintentar."
                ),
            },
        )
    rental = db.get(Rental, saga.rental_id)
    if rental is None:
        raise RuntimeError("No se encontró el arriendo asociado a la clave de idempotencia.")
    return rental, PaymentCheckout(
        paymentId=saga.payment_id,
        status=saga.checkout_status,
        redirectUrl=saga.checkout_redirect_url,
        token=saga.checkout_token,
    )


def create_rental(
    db: Session,
    client_id: int,
    equipment_id: int,
    start_date: datetime,
    end_date: datetime,
    amount_rent: int,
    amount_deposit: int,
    idempotency_key: str,
    request_fingerprint: str,
) -> tuple[Rental, PaymentCheckout]:
    rental = Rental(
        client_id=client_id,
        equipment_id=equipment_id,
        start_date=start_date,
        end_date=end_date,
        amount_rent=amount_rent,
        amount_deposit=amount_deposit,
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
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    db.add(saga)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = find_creation_by_key(db, idempotency_key, request_fingerprint)
        if existing is None:
            raise
        return existing
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

    try:
        saga.current_step = "START_PAYMENT"
        db.commit()
    except SQLAlchemyError as persistence_error:
        db.rollback()
        compensation_status = _compensate_create(
            db, saga.id, rental.id, equipment_id, persistence_error
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SAGA_PERSISTENCE_FAILED",
                "message": (
                    "No se pudo registrar el inicio del pago; la reserva fue compensada."
                    if compensation_status == "COMPENSATED"
                    else "No se pudo registrar el inicio del pago ni confirmar la compensación."
                ),
            },
        ) from persistence_error
    try:
        checkout = iniciar_cobro(
            rental_id=rental.id,
            customer_id=client_id,
            amount_rent=rental.amount_rent,
            amount_deposit=rental.amount_deposit,
            idempotency_key=saga.idempotency_key,
        )
    except PaymentStartRejected as error:
        _compensate_rejected_payment(db, saga.id, rental.id, equipment_id, error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "PAYMENT_REJECTED", "message": str(error)},
        ) from error
    except PaymentStartUncertain as error:
        _record_payment_uncertainty(db, saga.id, error)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "PAYMENT_UNCERTAIN", "message": str(error)},
        ) from error

    try:
        saga.payment_id = checkout.paymentId
        saga.checkout_status = checkout.status
        saga.checkout_redirect_url = checkout.redirectUrl
        saga.checkout_token = checkout.token
        saga.current_step = (
            "PAYMENT_CONFIRMED" if checkout.status == "CHARGED" else "WAITING_PAYMENT"
        )
        if checkout.status not in {"PENDING", "CHARGED"}:
            saga.status = "IN_DOUBT"
        db.commit()
    except SQLAlchemyError as persistence_error:
        db.rollback()
        _record_payment_uncertainty(db, saga.id, persistence_error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SAGA_PERSISTENCE_FAILED",
                "message": "El pago pudo iniciarse; la Saga requiere conciliación.",
            },
        ) from persistence_error
    db.refresh(rental)
    return rental, checkout


def _record_payment_uncertainty(db: Session, saga_id: str, error: Exception) -> None:
    db.rollback()
    saga = db.get(Saga, saga_id)
    if saga is None:
        raise RuntimeError("No se encontró la Saga al registrar el pago incierto.")
    saga.status = "IN_DOUBT"
    saga.current_step = "START_PAYMENT"
    saga.error = str(error)
    db.commit()


def _compensate_rejected_payment(
    db: Session, saga_id: str, rental_id: int, equipment_id: int, error: Exception
) -> None:
    saga = db.get(Saga, saga_id)
    saga.status = "COMPENSATING"
    saga.current_step = "RELEASE_EQUIPMENT"
    saga.error = str(error)
    db.commit()
    try:
        grpc_client.liberar_unidad(equipment_id)
    except HTTPException as release_error:
        saga = db.get(Saga, saga_id)
        saga.status = "COMPENSATION_FAILED"
        saga.error = f"{error}; liberación fallida: {_http_error_message(release_error)}"
        db.commit()
        return

    saga = db.get(Saga, saga_id)
    rental = db.get(Rental, rental_id)
    rental.status = "FAILED"
    saga.status = "COMPENSATED"
    saga.current_step = "COMPENSATION_COMPLETED"
    db.commit()


def reconcile_payment(db: Session, rental_id: int) -> Saga:
    rental = db.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RENTAL_NOT_FOUND", "message": f"Arriendo {rental_id} no encontrado."},
        )
    saga = (
        db.query(Saga)
        .filter(Saga.rental_id == rental_id, Saga.operation == "CREATE")
        .first()
    )
    if saga is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SAGA_NOT_FOUND", "message": "No hay una Saga de creación para este arriendo."},
        )
    if saga.status in {"COMPENSATED", "COMPLETED"} or saga.current_step == "PAYMENT_CONFIRMED":
        return saga
    if saga.status in {"COMPENSATING", "COMPENSATION_FAILED", "FAILED"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SAGA_REQUIRES_REVIEW", "message": "La Saga está compensando o requiere revisión."},
        )
    if saga.current_step not in {"START_PAYMENT", "WAITING_PAYMENT"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "PAYMENT_NOT_STARTED", "message": "La Saga aún no llegó al paso de pago."},
        )

    if saga.payment_id is None:
        if saga.status != "IN_DOUBT":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "PAYMENT_START_IN_PROGRESS", "message": "El inicio del pago aún está en curso."},
            )
        try:
            checkout = iniciar_cobro(
                rental_id=rental.id,
                customer_id=rental.client_id,
                amount_rent=rental.amount_rent,
                amount_deposit=rental.amount_deposit,
                idempotency_key=saga.idempotency_key,
                require_checkout=False,
            )
        except PaymentStartRejected as error:
            _compensate_failed_payment(db, saga.id, rental.id, rental.equipment_id, error)
            return db.get(Saga, saga.id)
        except PaymentStartUncertain as error:
            _mark_payment_in_doubt(db, saga.id, error)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "PAYMENT_UNCERTAIN", "message": str(error)},
            ) from error
        recovered = db.execute(
            update(Saga)
            .where(
                Saga.id == saga.id,
                Saga.status == "IN_DOUBT",
                Saga.current_step == "START_PAYMENT",
                Saga.payment_id.is_(None),
            )
            .values(
                payment_id=checkout.paymentId,
                checkout_status=checkout.status,
                checkout_redirect_url=checkout.redirectUrl,
                checkout_token=checkout.token,
                current_step="WAITING_PAYMENT",
            )
        ).rowcount
        db.commit()
        db.expire_all()
        saga = db.get(Saga, saga.id)
        if not recovered and saga.payment_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "PAYMENT_STATE_CHANGED", "message": "La Saga cambió durante la recuperación del pago."},
            )
        if saga.current_step == "PAYMENT_CONFIRMED" or saga.status in {"COMPENSATED", "COMPENSATING", "COMPENSATION_FAILED"}:
            return saga

    try:
        payment = consultar_pago(saga.payment_id)
    except PaymentLookupUncertain as error:
        _mark_payment_in_doubt(db, saga.id, error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "PAYMENT_UNCERTAIN", "message": str(error)},
        ) from error

    if payment.rental_id != rental.id or payment.amount_total != rental.amount_rent + rental.amount_deposit:
        _mark_payment_in_doubt(db, saga.id, ValueError("El cobro no coincide con el arriendo."))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "PAYMENT_MISMATCH", "message": "El cobro no coincide con el arriendo."},
        )

    if payment.status in {"FAILED", "CANCELLED"}:
        _compensate_failed_payment(
            db, saga.id, rental.id, rental.equipment_id,
            ValueError(f"Pago {payment.status}"),
        )
        return db.get(Saga, saga.id)

    if payment.status == "CHARGED":
        db.execute(
            update(Saga)
            .where(
                Saga.id == saga.id,
                Saga.status.in_(["IN_PROGRESS", "IN_DOUBT"]),
                Saga.current_step.in_(["START_PAYMENT", "WAITING_PAYMENT"]),
            )
            .values(
                status="IN_PROGRESS", current_step="PAYMENT_CONFIRMED",
                checkout_status="CHARGED", error=None,
            )
        )
    else:
        has_checkout = bool(payment.redirect_url and payment.token)
        db.execute(
            update(Saga)
            .where(
                Saga.id == saga.id,
                Saga.status.in_(["IN_PROGRESS", "IN_DOUBT"]),
                Saga.current_step.in_(["START_PAYMENT", "WAITING_PAYMENT"]),
            )
            .values(
                status="IN_PROGRESS" if payment.status == "PENDING" and has_checkout else "IN_DOUBT",
                current_step="WAITING_PAYMENT",
                checkout_status=payment.status,
                checkout_redirect_url=payment.redirect_url,
                checkout_token=payment.token,
                error=None if payment.status == "PENDING" and has_checkout else "El pago aún no tiene un resultado o checkout utilizable.",
            )
        )
    db.commit()
    return db.get(Saga, saga.id)


def _mark_payment_in_doubt(db: Session, saga_id: str, error: Exception) -> None:
    db.rollback()
    db.execute(
        update(Saga)
        .where(
            Saga.id == saga_id,
            Saga.status.in_(["IN_PROGRESS", "IN_DOUBT"]),
            Saga.current_step.in_(["START_PAYMENT", "WAITING_PAYMENT"]),
        )
        .values(status="IN_DOUBT", error=str(error))
    )
    db.commit()


def _compensate_failed_payment(
    db: Session, saga_id: str, rental_id: int, equipment_id: int, error: Exception
) -> None:
    claimed = db.execute(
        update(Saga)
        .where(
            Saga.id == saga_id,
            Saga.status.in_(["IN_PROGRESS", "IN_DOUBT"]),
            Saga.current_step.in_(["START_PAYMENT", "WAITING_PAYMENT"]),
        )
        .values(status="COMPENSATING", current_step="RELEASE_EQUIPMENT", error=str(error))
    ).rowcount
    db.commit()
    if not claimed:
        return
    try:
        grpc_client.liberar_unidad(equipment_id)
    except HTTPException as release_error:
        saga = db.get(Saga, saga_id)
        saga.status = "COMPENSATION_FAILED"
        saga.error = f"{error}; liberación fallida: {_http_error_message(release_error)}"
        db.commit()
        return
    saga = db.get(Saga, saga_id)
    rental = db.get(Rental, rental_id)
    rental.status = "FAILED"
    saga.status = "COMPENSATED"
    saga.current_step = "COMPENSATION_COMPLETED"
    saga.checkout_status = "FAILED"
    db.commit()


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
