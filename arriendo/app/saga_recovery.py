import asyncio
import logging
import os
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.bd_models import Rental, Saga
from app.database import SessionLocal
from app.redis import get_rental_cache
from app.saga_orchestrator import DELIVERY_RETRY_STEPS, reconcile_payment

logger = logging.getLogger(__name__)
BATCH_SIZE = 20


@contextmanager
def lock_rental_workflow(db: Session, rental_id: int):
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        yield
        return

    # La transacción separada conserva el bloqueo entre commits de la Saga.
    with bind.begin() as connection:
        claimed = connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:namespace, :rental_id)"),
            {"namespace": 87025, "rental_id": rental_id},
        ).scalar_one()
        if not claimed:
            raise HTTPException(
                status_code=409,
                detail={"code": "RECONCILIATION_IN_PROGRESS", "message": "El arriendo ya se está conciliando."},
            )
        yield


def reconcile_rental(db: Session, rental_id: int) -> Saga:
    with lock_rental_workflow(db, rental_id):
        return reconcile_payment(db, rental_id)


def recover_batch(after_rental_id: int) -> int:
    with SessionLocal() as db:
        rental_ids = db.scalars(
            select(Saga.rental_id)
            .join(Rental, Rental.id == Saga.rental_id)
            .where(
                Saga.rental_id > after_rental_id,
                Saga.operation == "CREATE",
                Saga.status.in_(["IN_PROGRESS", "IN_DOUBT"]),
                Saga.current_step.in_(DELIVERY_RETRY_STEPS | {"WAITING_PAYMENT"}),
                Saga.payment_id.is_not(None),
                Saga.delivery_id.is_(None),
                Rental.status == "PENDING",
            )
            .distinct()
            .order_by(Saga.rental_id)
            .limit(BATCH_SIZE)
        ).all()

    for rental_id in rental_ids:
        try:
            with SessionLocal() as db:
                reconcile_rental(db, rental_id)
        except HTTPException as error:
            if error.status_code != 409:
                logger.warning("Conciliación pendiente para arriendo %s: %s", rental_id, error.detail)
        except Exception:
            logger.exception("Falló la recuperación del arriendo %s", rental_id)
        finally:
            try:
                get_rental_cache().invalidate(rental_id)
            except Exception:
                logger.exception("No se pudo invalidar la caché del arriendo %s", rental_id)
    return rental_ids[-1] if len(rental_ids) == BATCH_SIZE else 0


async def _recover_periodically(stop: asyncio.Event, interval: float) -> None:
    cursor = 0
    while not stop.is_set():
        try:
            cursor = await asyncio.to_thread(recover_batch, cursor)
        except Exception:
            logger.exception("Falló la búsqueda de Sagas pendientes")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


@asynccontextmanager
async def saga_recovery_lifespan(_app: FastAPI):
    interval = float(os.getenv("SAGA_RECOVERY_INTERVAL_SECONDS", "0"))
    if interval <= 0:
        yield
        return

    stop = asyncio.Event()
    worker = asyncio.create_task(_recover_periodically(stop, interval))
    try:
        yield
    finally:
        stop.set()
        await worker
