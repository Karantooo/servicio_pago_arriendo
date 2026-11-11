from fastapi import APIRouter, HTTPException, Query, Request

from pagos.models import utc_now
from pagos.providers import get_provider
from pagos.providers.base import ProviderUnavailable
from pagos.resilience import bulkhead, circuit_breaker, invoke_provider_call
from pagos.storage import (
    claim_payment_confirmation,
    complete_payment_confirmation,
    get_payment_by_token,
    mark_payment_confirmation_unknown,
)

router = APIRouter()


@router.get("/payments/transbank/return")
async def transbank_return_get(
    token_ws: str | None = Query(default=None),
    TBK_TOKEN: str | None = Query(default=None),
    TBK_ORDEN_COMPRA: str | None = Query(default=None),
    TBK_ID_SESION: str | None = Query(default=None),
):
    return await handle_transbank_return(
        token_ws,
        TBK_TOKEN,
        TBK_ORDEN_COMPRA,
        TBK_ID_SESION,
    )


@router.post("/payments/transbank/return")
async def transbank_return_post(request: Request):
    form = await request.form()
    return await handle_transbank_return(
        form.get("token_ws"),
        form.get("TBK_TOKEN"),
        form.get("TBK_ORDEN_COMPRA"),
        form.get("TBK_ID_SESION"),
    )


async def handle_transbank_return(
    token_ws: str | None,
    tbk_token: str | None,
    tbk_order: str | None,
    tbk_session: str | None,
):
    token = token_ws or tbk_token
    if not token:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_RETURN",
                "message": "No se recibió token_ws o TBK_TOKEN.",
            },
        )

    payment = get_payment_by_token(token)
    if payment is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PAYMENT_NOT_FOUND",
                "message": "No se encontró la transacción asociada.",
            },
        )

    if payment.status != "PENDING":
        return _confirmation_response(payment)

    with bulkhead.acquire():
        circuit_breaker.before_call()
        payment, claimed = claim_payment_confirmation(token)
        if payment is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "PAYMENT_NOT_FOUND",
                    "message": "No se encontró la transacción asociada.",
                },
            )
        if not claimed:
            circuit_breaker.release_probe()
            return _confirmation_response(payment)

        try:
            confirmation = await invoke_provider_call(
                get_provider().confirm_transaction,
                token,
            )
            circuit_breaker.on_success()
            if confirmation.get("status") == "AUTHORIZED" and confirmation.get(
                "response_code"
            ) == 0:
                result_status = "CHARGED"
            else:
                result_status = "FAILED"
            payment = complete_payment_confirmation(
                payment.payment_id,
                status=result_status,
            )
            if payment is None:
                raise RuntimeError("No se pudo recuperar el pago confirmado.")
            return _confirmation_response(
                payment,
                message=confirmation.get("message"),
                response_code=confirmation.get("response_code"),
            )
        except ProviderUnavailable as exc:
            circuit_breaker.on_failure()
            mark_payment_confirmation_unknown(payment.payment_id)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                    "message": "Transbank no está disponible temporalmente.",
                },
            ) from exc
        except TimeoutError as exc:
            circuit_breaker.on_failure()
            mark_payment_confirmation_unknown(payment.payment_id)
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "PAYMENT_PROVIDER_TIMEOUT",
                    "message": "Tiempo de espera agotado al confirmar la transacción.",
                },
            ) from exc


def _confirmation_response(
    payment,
    *,
    message: str | None = None,
    response_code: int | None = None,
):
    status_messages = {
        "CHARGED": "Pago autorizado correctamente.",
        "FAILED": "Pago rechazado.",
        "CONFIRMING": "La confirmación del pago sigue en curso.",
        "UNKNOWN": "El resultado de la confirmación no pudo determinarse.",
    }
    if payment.status == "CHARGED":
        response_code = 0 if response_code is None else response_code
    elif payment.status == "FAILED":
        response_code = 1 if response_code is None else response_code
    else:
        response_code = None

    return {
        "payment_id": payment.payment_id,
        "rental_id": payment.rental_id,
        "status": payment.status,
        "response_code": response_code,
        "message": message or status_messages.get(
            payment.status,
            "La confirmación no requiere otra operación.",
        ),
        "createdAt": utc_now().isoformat().replace("+00:00", "Z"),
    }
