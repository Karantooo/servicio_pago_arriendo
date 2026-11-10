from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from pagos.models import (
    ChargeRequest,
    PaymentRecord,
    RefundRecord,
    RefundRequest,
    serialize_payment,
    serialize_refund,
    utc_now,
)
from pagos.providers import get_provider
from pagos.providers.base import ProviderUnavailable
from pagos.resilience import bulkhead, circuit_breaker, invoke_provider_call
from pagos.storage import (
    next_payment_id,
    next_refund_id,
    operation_by_idempotency,
    payments,
    refund_by_idempotency,
    refunds,
)

router = APIRouter()


@router.post("/payments/charge")
async def charge_payment(
    payload: ChargeRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    request_id: str | None = Header(None, alias="X-Request-ID"),
):
    if not payload.amount_total:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_AMOUNT",
                "message": "El monto total no puede ser cero.",
            },
        )

    if idempotency_key in operation_by_idempotency:
        payment = payments[operation_by_idempotency[idempotency_key]]
        return JSONResponse(status_code=200, content=serialize_payment(payment))

    if payload.rental_id <= 0 or payload.customer_id <= 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_REQUEST",
                "message": "rental_id y customer_id deben ser positivos.",
            },
        )

    with bulkhead.acquire():
        circuit_breaker.before_call()
        try:
            payment_id = next_payment_id()
            payment = PaymentRecord(
                payment_id=payment_id,
                rental_id=payload.rental_id,
                customer_id=payload.customer_id,
                amount_total=payload.amount_total,
                status="PENDING",
                buy_order=f"RENT-{payload.rental_id}-{payment_id}",
                session_id=f"SESSION-{payload.rental_id}",
                token=f"token-{payment_id}",
                redirect_url="https://webpay3gint.transbank.cl/webpayserver/initTransaction",
                createdAt=utc_now(),
            )
            provider_response = await invoke_provider_call(
                get_provider().create_transaction,
                payment_id,
                payload.rental_id,
                payload.amount_total,
                payload.customer_id,
            )
            payment.token = provider_response["token"]
            payment.session_id = provider_response["session_id"]
            payment.buy_order = provider_response["buy_order"]
            payment.redirect_url = provider_response["redirect_url"]
            payments[payment_id] = payment
            operation_by_idempotency[idempotency_key] = payment_id
            circuit_breaker.on_success()
            return JSONResponse(status_code=201, content=serialize_payment(payment))
        except ProviderUnavailable as exc:
            circuit_breaker.on_failure()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                    "message": "No fue posible contactar a la pasarela de pagos.",
                },
            ) from exc
        except TimeoutError as exc:
            circuit_breaker.on_failure()
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "PAYMENT_PROVIDER_TIMEOUT",
                    "message": "Tiempo de espera agotado al comunicarse con Transbank.",
                },
            ) from exc
        except HTTPException:
            circuit_breaker.on_failure()
            raise
        except Exception as exc:
            circuit_breaker.on_failure()
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "PAYMENT_INTERNAL_ERROR",
                    "message": "Error interno en el servicio de pagos.",
                },
            ) from exc


@router.get("/payments/{payment_id}")
async def get_payment(payment_id: int) -> Dict[str, Any]:
    payment = payments.get(payment_id)
    if payment is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "PAYMENT_NOT_FOUND", "message": "Pago inexistente."},
        )
    return serialize_payment(payment)


@router.post("/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: int,
    payload: RefundRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    request_id: str | None = Header(None, alias="X-Request-ID"),
):
    payment = payments.get(payment_id)
    if payment is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "PAYMENT_NOT_FOUND", "message": "Pago inexistente."},
        )

    if idempotency_key in refund_by_idempotency:
        refund = refunds[refund_by_idempotency[idempotency_key]]
        return JSONResponse(status_code=200, content=serialize_refund(refund))

    if payload.rental_id != payment.rental_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_REFUND",
                "message": "El rental_id no coincide con el pago.",
            },
        )

    if payment.status in {"REFUNDED", "CANCELLED", "FAILED"}:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "REFUND_CONFLICT",
                "message": "El pago no se puede reembolsar en su estado actual.",
            },
        )

    with bulkhead.acquire():
        circuit_breaker.before_call()
        try:
            refund_response = await invoke_provider_call(
                get_provider().refund_transaction,
                payment.token,
                payload.reason,
                payment.amount_total,
            )
            refund = RefundRecord(
                refund_id=next_refund_id(),
                payment_id=payment_id,
                rental_id=payment.rental_id,
                amount_refunded=refund_response["amount_refunded"],
                status=refund_response["status"],
                provider_operation=refund_response["provider_operation"],
                message=refund_response["message"],
                createdAt=utc_now(),
            )
            refunds[refund.refund_id] = refund
            refund_by_idempotency[idempotency_key] = refund.refund_id
            payment.status = "REFUNDED"
            circuit_breaker.on_success()
            return JSONResponse(status_code=200, content=serialize_refund(refund))
        except ProviderUnavailable as exc:
            circuit_breaker.on_failure()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                    "message": "No fue posible realizar el reembolso.",
                },
            ) from exc
        except TimeoutError as exc:
            circuit_breaker.on_failure()
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "PAYMENT_PROVIDER_TIMEOUT",
                    "message": "Tiempo de espera agotado al solicitar el reembolso.",
                },
            ) from exc
