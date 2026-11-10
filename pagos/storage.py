from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from pagos.database import SessionLocal
from pagos.db_models import Payment, Refund
from pagos.models import ChargeRequest, utc_now


def get_payment(payment_id: int) -> Payment | None:
    with SessionLocal() as db:
        return db.get(Payment, payment_id)


def get_payment_by_idempotency_key(idempotency_key: str) -> Payment | None:
    with SessionLocal() as db:
        return (
            db.query(Payment)
            .filter(Payment.idempotency_key == idempotency_key)
            .one_or_none()
        )


def get_payment_by_token(token: str) -> Payment | None:
    with SessionLocal() as db:
        return db.query(Payment).filter(Payment.token == token).one_or_none()


def create_payment(
    payload: ChargeRequest,
    idempotency_key: str,
) -> tuple[Payment, bool]:
    with SessionLocal() as db:
        existing = (
            db.query(Payment)
            .filter(Payment.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return existing, False

        payment = Payment(
            rental_id=payload.rental_id,
            customer_id=payload.customer_id,
            amount_total=payload.amount_total,
            status="PENDING",
            buy_order=f"pending-{uuid4().hex}",
            session_id=f"SESSION-{payload.rental_id}",
            token=None,
            redirect_url=None,
            idempotency_key=idempotency_key,
            created_at=utc_now(),
        )
        db.add(payment)
        try:
            db.flush()
            payment.buy_order = f"RENT-{payload.rental_id}-{payment.payment_id}"
            db.commit()
            return payment, True
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(Payment)
                .filter(Payment.idempotency_key == idempotency_key)
                .one_or_none()
            )
            if existing is None:
                raise
            return existing, False


def update_payment(payment_id: int, **changes) -> Payment:
    with SessionLocal() as db:
        payment = db.get(Payment, payment_id)
        if payment is None:
            raise LookupError(f"Pago {payment_id} no existe")
        for field, value in changes.items():
            setattr(payment, field, value)
        db.commit()
        return payment


def get_refund_by_idempotency_key(idempotency_key: str) -> Refund | None:
    with SessionLocal() as db:
        return (
            db.query(Refund)
            .filter(Refund.idempotency_key == idempotency_key)
            .one_or_none()
        )


def create_refund(
    payment_id: int,
    idempotency_key: str,
    reason: str,
) -> tuple[Refund | None, bool]:
    with SessionLocal() as db:
        existing_by_key = (
            db.query(Refund)
            .filter(Refund.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing_by_key is not None:
            return existing_by_key, False

        existing_for_payment = (
            db.query(Refund)
            .filter(Refund.payment_id == payment_id)
            .one_or_none()
        )
        if existing_for_payment is not None:
            return existing_for_payment, False

        payment = (
            db.query(Payment)
            .filter(Payment.payment_id == payment_id)
            .with_for_update()
            .one_or_none()
        )
        if payment is None:
            return None, False

        refund = Refund(
            payment_id=payment.payment_id,
            rental_id=payment.rental_id,
            amount_refunded=None,
            status="REFUND_PENDING",
            provider_operation=None,
            message="Reembolso en proceso.",
            reason=reason,
            idempotency_key=idempotency_key,
            created_at=utc_now(),
        )
        payment.status = "REFUND_PENDING"
        db.add(refund)
        try:
            db.commit()
            return refund, True
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(Refund)
                .filter(Refund.idempotency_key == idempotency_key)
                .one_or_none()
            )
            if existing is None:
                existing = (
                    db.query(Refund)
                    .filter(Refund.payment_id == payment_id)
                    .one_or_none()
                )
            if existing is None:
                raise
            return existing, False


def update_refund_result(
    refund_id: int,
    *,
    status: str,
    message: str,
    provider_operation: str | None = None,
    amount_refunded: int | None = None,
) -> Refund:
    with SessionLocal() as db:
        refund = db.get(Refund, refund_id)
        if refund is None:
            raise LookupError(f"Reembolso {refund_id} no existe")
        refund.status = status
        refund.message = message
        refund.provider_operation = provider_operation
        if amount_refunded is not None:
            refund.amount_refunded = amount_refunded

        payment = db.get(Payment, refund.payment_id)
        if payment is None:
            raise LookupError(f"Pago {refund.payment_id} no existe")
        payment.status = "REFUNDED" if status == "REFUNDED" else "REFUND_PENDING"
        db.commit()
        return refund
