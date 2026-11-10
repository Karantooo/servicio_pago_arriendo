from typing import Any, Dict

from pagos.config import TRANSBANK_RETURN_URL
from pagos.providers.base import ProviderUnavailable


class TransbankProvider:
    """Transbank Webpay Plus adapter configured for the integration environment."""

    def __init__(self) -> None:
        try:
            from transbank.common.integration_api_keys import IntegrationApiKeys
            from transbank.common.integration_commerce_codes import IntegrationCommerceCodes
            from transbank.common.integration_type import IntegrationType
            from transbank.webpay.webpay_plus.transaction import Transaction, WebpayOptions
        except ImportError as exc:
            raise RuntimeError(
                "El proveedor Transbank requiere el paquete transbank-sdk."
            ) from exc

        options = WebpayOptions(
            IntegrationCommerceCodes.WEBPAY_PLUS,
            IntegrationApiKeys.WEBPAY,
            IntegrationType.TEST,
        )
        self._transaction = Transaction(options=options)

    def create_transaction(
        self,
        payment_id: int,
        rental_id: int,
        amount_total: int,
        customer_id: int,
    ) -> Dict[str, Any]:
        try:
            buy_order = f"RENT-{rental_id}-{payment_id}"
            session_id = f"SESSION-{rental_id}"
            response = self._transaction.create(
                buy_order,
                session_id,
                amount_total,
                TRANSBANK_RETURN_URL,
            )
            return {
                "token": response["token"],
                "session_id": session_id,
                "buy_order": buy_order,
                "redirect_url": response["url"],
                "amount": amount_total,
                "customer_id": customer_id,
            }
        except Exception as exc:
            raise ProviderUnavailable("Transbank no pudo iniciar la transacción") from exc

    def confirm_transaction(self, token: str) -> Dict[str, Any]:
        try:
            response = self._transaction.commit(token)
            return {
                "status": response.get("status"),
                "response_code": response.get("response_code"),
                "message": "Pago autorizado correctamente."
                if response.get("response_code") == 0
                else "Transbank rechazó la transacción.",
                "token": token,
            }
        except Exception as exc:
            raise ProviderUnavailable("Transbank no pudo confirmar la transacción") from exc

    def refund_transaction(
        self,
        token: str,
        reason: str,
        amount: int,
    ) -> Dict[str, Any]:
        try:
            response = self._transaction.refund(token, amount)
            response_code = response.get("response_code")
            operation = "NULLIFIED" if response.get("type") == "NULLIFIED" else "REVERSED"
            if response_code not in (None, 0):
                raise ProviderUnavailable("Transbank rechazó el reembolso")
            return {
                "amount_refunded": response.get("amount", amount),
                "provider_operation": operation,
                "message": f"Reembolso procesado ({reason}).",
                "status": "REFUNDED",
            }
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable("Transbank no pudo procesar el reembolso") from exc
