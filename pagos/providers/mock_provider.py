import threading
import time
from typing import Any, Dict

from pagos.config import PROVIDER_MODE, TIMEOUT_SECONDS
from pagos.providers.base import ProviderUnavailable


class MockProvider:
    _modes = {"NORMAL", "SLOW", "DOWN"}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = PROVIDER_MODE

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> str:
        normalized = mode.strip().upper()
        if normalized not in self._modes:
            raise ValueError(f"Modo inválido: {mode}")
        with self._lock:
            self._mode = normalized
        return self._mode

    def _apply_mode(self) -> None:
        mode = self.mode
        if mode == "DOWN":
            raise ProviderUnavailable("Mock provider está caído")
        if mode == "SLOW":
            time.sleep(max(0, TIMEOUT_SECONDS + 1))

    def create_transaction(
        self,
        payment_id: int,
        rental_id: int,
        amount_total: int,
        customer_id: int,
    ) -> Dict[str, Any]:
        self._apply_mode()
        return {
            "token": f"token-{payment_id}",
            "session_id": f"session-{payment_id}",
            "buy_order": f"RENT-{rental_id}-{payment_id}",
            "redirect_url": "https://webpay3gint.transbank.cl/webpayserver/initTransaction",
            "amount": amount_total,
            "customer_id": customer_id,
        }

    def confirm_transaction(self, token: str) -> Dict[str, Any]:
        self._apply_mode()
        return {
            "status": "AUTHORIZED",
            "response_code": 0,
            "message": "Pago autorizado correctamente.",
            "token": token,
        }

    def refund_transaction(
        self,
        token: str,
        reason: str,
        amount: int,
    ) -> Dict[str, Any]:
        self._apply_mode()
        payment_id = int(token.removeprefix("token-"))
        return {
            "refund_id": 7000 + payment_id,
            "payment_id": payment_id,
            "amount_refunded": amount,
            "provider_operation": "REVERSED",
            "message": f"Reembolso procesado ({reason}).",
            "status": "REFUNDED",
        }
