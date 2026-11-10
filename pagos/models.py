from datetime import datetime, timezone
from typing import Any, Dict, Literal

from pydantic import BaseModel, Field


class ChargeRequest(BaseModel):
    rental_id: int
    customer_id: int
    amount_rent: int = Field(ge=0)
    amount_deposit: int = Field(ge=0)

    @property
    def amount_total(self) -> int:
        return self.amount_rent + self.amount_deposit


class RefundRequest(BaseModel):
    reason: Literal["SAGA_COMPENSATION"]
    rental_id: int


class PaymentRecord(BaseModel):
    payment_id: int
    rental_id: int
    customer_id: int
    amount_total: int
    status: str
    buy_order: str
    session_id: str
    token: str
    redirect_url: str
    createdAt: datetime


class RefundRecord(BaseModel):
    refund_id: int
    payment_id: int
    rental_id: int
    amount_refunded: int
    status: str
    provider_operation: str
    message: str
    createdAt: datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def serialize_payment(payment: PaymentRecord) -> Dict[str, Any]:
    return {
        "payment_id": payment.payment_id,
        "rental_id": payment.rental_id,
        "amount_total": payment.amount_total,
        "status": payment.status,
        "buy_order": payment.buy_order,
        "session_id": payment.session_id,
        "token": payment.token,
        "redirect_url": payment.redirect_url,
        "createdAt": payment.createdAt.isoformat().replace("+00:00", "Z"),
    }


def serialize_refund(refund: RefundRecord) -> Dict[str, Any]:
    return {
        "refund_id": refund.refund_id,
        "payment_id": refund.payment_id,
        "rental_id": refund.rental_id,
        "amount_refunded": refund.amount_refunded,
        "status": refund.status,
        "provider_operation": refund.provider_operation,
        "message": refund.message,
        "createdAt": refund.createdAt.isoformat().replace("+00:00", "Z"),
    }
