from fastapi import APIRouter, HTTPException, Query, Request

from pagos.models import utc_now
from pagos.providers import get_provider
from pagos.providers.base import ProviderUnavailable
from pagos.resilience import bulkhead, circuit_breaker, invoke_provider_call
from pagos.storage import payments

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

    payment = next((item for item in payments.values() if item.token == token), None)
    if payment is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PAYMENT_NOT_FOUND",
                "message": "No se encontró la transacción asociada.",
            },
        )

    with bulkhead.acquire():
        circuit_breaker.before_call()
        try:
            confirmation = await invoke_provider_call(
                get_provider().confirm_transaction,
                token,
            )
            if confirmation.get("status") == "AUTHORIZED" and confirmation.get(
                "response_code"
            ) == 0:
                payment.status = "CHARGED"
                status = "CHARGED"
                message = "Pago autorizado correctamente."
                response_code = 0
            else:
                payment.status = "FAILED"
                status = "FAILED"
                message = confirmation.get("message", "Pago rechazado.")
                response_code = confirmation.get("response_code", 1)
            circuit_breaker.on_success()
            return {
                "payment_id": payment.payment_id,
                "rental_id": payment.rental_id,
                "status": status,
                "response_code": response_code,
                "message": message,
                "createdAt": utc_now().isoformat().replace("+00:00", "Z"),
            }
        except ProviderUnavailable as exc:
            circuit_breaker.on_failure()
            payment.status = "UNKNOWN"
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "PAYMENT_PROVIDER_UNAVAILABLE",
                    "message": "Transbank no está disponible temporalmente.",
                },
            ) from exc
        except TimeoutError as exc:
            circuit_breaker.on_failure()
            payment.status = "UNKNOWN"
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "PAYMENT_PROVIDER_TIMEOUT",
                    "message": "Tiempo de espera agotado al confirmar la transacción.",
                },
            ) from exc
