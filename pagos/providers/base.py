from typing import Any, Dict, Protocol


class ProviderUnavailable(RuntimeError):
    pass


class PaymentProvider(Protocol):
    def create_transaction(
        self,
        payment_id: int,
        rental_id: int,
        amount_total: int,
        customer_id: int,
    ) -> Dict[str, Any]: ...

    def confirm_transaction(self, token: str) -> Dict[str, Any]: ...

    def refund_transaction(
        self,
        token: str,
        reason: str,
        amount: int,
    ) -> Dict[str, Any]: ...
