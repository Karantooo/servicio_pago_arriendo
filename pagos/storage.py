from typing import Dict

from pagos.models import PaymentRecord, RefundRecord


payments: Dict[int, PaymentRecord] = {}
refunds: Dict[int, RefundRecord] = {}
operation_by_idempotency: Dict[str, int] = {}
refund_by_idempotency: Dict[str, int] = {}


def next_payment_id() -> int:
    return max((payment.payment_id for payment in payments.values()), default=0) + 1


def next_refund_id() -> int:
    return max((refund.refund_id for refund in refunds.values()), default=700) + 1
