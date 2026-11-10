from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from pagos.models import (
    ChargeRequest,
    RefundRequest,
    serialize_payment,
    serialize_refund,
    utc_now,
)
from pagos.providers import get_provider
from pagos.providers.base import ProviderUnavailable
from pagos.resilience import bulkhead, circuit_breaker, invoke_provider_call
from pagos.storage import (
    create_payment,
    create_refund,
    get_payment,
    get_payment_by_idempotency_key,
    get_refund_by_idempotency_key,
    update_payment,
    update_refund_result,
)

router = APIRouter()


def _validate_reused_charge(payment, payload: ChargeRequest) -> None:
    if (
        payment.rental_id != payload.rental_id
        or payment.customer_id != payload.customer_id
        or payment.amount_total != payload.amount_total
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "IDEMPOTENCY_KEY_CONFLICT",
                "message": "La clave de idempotencia ya se usó con otros datos.",
            },
        )


def _validate_reused_refund(refund, payment_id: int, payload: RefundRequest) -> None:
    if refund.payment_id != payment_id or refund.reason != payload.reason:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "IDEMPOTENCY_KEY_CONFLICT",
                "message": "La clave de idempotencia ya se usó para otro reembolso.",
            },
        )


@router.post("/payments/charge")
async def charge_payment(
    payload: ChargeRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=255
    ),
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

    if payload.rental_id <= 0 or payload.customer_id <= 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_REQUEST",
                "message": "rental_id y customer_id deben ser positivos.",
            },
        )

    existing = get_payment_by_idempotency_key(idempotency_key)
    if existing is not None:
        _validate_reused_charge(existing, payload)
        return JSONResponse(status_code=200, content=serialize_payment(existing))

    with bulkhead.acquire():
        circuit_breaker.before_call()
        try:
            payment, created = create_payment(payload, idempotency_key)
            if not created:
                circuit_breaker.release_probe()
                _validate_reused_charge(payment, payload)
                return JSONResponse(status_code=200, content=serialize_payment(payment))

            provider_response = await invoke_provider_call(
                get_provider().create_transaction,
                payment.payment_id,
                payload.rental_id,
                payload.amount_total,
                payload.customer_id,
            )
            circuit_breaker.on_success()
            payment = update_payment(
                payment.payment_id,
                token=provider_response["token"],
                session_id=provider_response["session_id"],
                buy_order=provider_response["buy_order"],
                redirect_url=provider_response["redirect_url"],
            )
            return JSONResponse(status_code=201, content=serialize_payment(payment))
        except ProviderUnavailable as exc:
            circuit_breaker.on_failure()
            update_payment(payment.payment_id, status="UNKNOWN")
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                    "message": "No fue posible contactar a la pasarela de pagos.",
                },
            ) from exc
        except TimeoutError as exc:
            circuit_breaker.on_failure()
            update_payment(payment.payment_id, status="UNKNOWN")
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "PAYMENT_PROVIDER_TIMEOUT",
                    "message": "Tiempo de espera agotado al comunicarse con Transbank.",
                },
            ) from exc


@router.get("/payments/{payment_id}")
async def get_payment_endpoint(payment_id: int) -> Dict[str, Any]:
    payment = get_payment(payment_id)
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
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=255
    ),
    request_id: str | None = Header(None, alias="X-Request-ID"),
):
    payment = get_payment(payment_id)
    if payment is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "PAYMENT_NOT_FOUND", "message": "Pago inexistente."},
        )

    if payload.rental_id != payment.rental_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_REFUND",
                "message": "El rental_id no coincide con el pago.",
            },
        )

    existing_refund = get_refund_by_idempotency_key(idempotency_key)
    if existing_refund is not None:
        _validate_reused_refund(existing_refund, payment_id, payload)
        return JSONResponse(
            status_code=200,
            content=serialize_refund(existing_refund),
        )

    if payment.status in {"REFUNDED", "REFUND_PENDING", "CANCELLED", "FAILED"}:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "REFUND_CONFLICT",
                "message": "El pago no se puede reembolsar en su estado actual.",
            },
        )
    if not payment.token:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "REFUND_CONFLICT",
                "message": "El pago todavía no tiene un token de proveedor para reembolsar.",
            },
        )

    with bulkhead.acquire():
        circuit_breaker.before_call()
        try:
            refund, created = create_refund(
                payment_id,
                idempotency_key,
                payload.reason,
            )
            if refund is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": "PAYMENT_NOT_FOUND",
                        "message": "Pago inexistente.",
                    },
                )
            if not created:
                circuit_breaker.release_probe()
                _validate_reused_refund(refund, payment_id, payload)
                return JSONResponse(
                    status_code=200,
                    content=serialize_refund(refund),
                )

            refund_response = await invoke_provider_call(
                get_provider().refund_transaction,
                payment.token,
                payload.reason,
                payment.amount_total,
            )
            circuit_breaker.on_success()
            refund = update_refund_result(
                refund.refund_id,
                status=refund_response["status"],
                message=refund_response["message"],
                provider_operation=refund_response["provider_operation"],
                amount_refunded=refund_response["amount_refunded"],
            )
            return JSONResponse(
                status_code=200,
                content=serialize_refund(refund),
            )
        except ProviderUnavailable as exc:
            circuit_breaker.on_failure()
            refund = get_refund_by_idempotency_key(idempotency_key)
            if refund is not None:
                update_refund_result(
                    refund.refund_id,
                    status="UNKNOWN",
                    message="No se pudo confirmar el resultado del reembolso.",
                )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                    "message": "No fue posible realizar el reembolso.",
                },
            ) from exc
        except TimeoutError as exc:
            circuit_breaker.on_failure()
            refund = get_refund_by_idempotency_key(idempotency_key)
            if refund is not None:
                update_refund_result(
                    refund.refund_id,
                    status="UNKNOWN",
                    message="Tiempo agotado; el resultado del reembolso es incierto.",
                )
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "PAYMENT_PROVIDER_TIMEOUT",
                    "message": "Tiempo de espera agotado al solicitar el reembolso.",
                },
            ) from exc
