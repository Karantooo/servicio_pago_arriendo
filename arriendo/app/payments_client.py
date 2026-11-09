import os

import httpx
from app.models import PaymentCheckout

PAYMENTS_API_URL = os.getenv(
    "PAYMENTS_API_URL", "http://payments:8003"
).rstrip("/")
PAYMENTS_TIMEOUT_SECONDS = float(
    os.getenv("PAYMENTS_TIMEOUT_SECONDS", "3")
)


def iniciar_cobro(
    rental_id: int,
    customer_id: int,
    amount_rent: int,
    amount_deposit: int,
    idempotency_key: str,
) -> PaymentCheckout:
    response = httpx.post(
        f"{PAYMENTS_API_URL}/payments/charge",
        headers={"Idempotency-Key": idempotency_key},
        json={
            "rental_id": rental_id,
            "customer_id": customer_id,
            "amount_rent": amount_rent,
            "amount_deposit": amount_deposit,
        },
        timeout=PAYMENTS_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()

    return PaymentCheckout(
        paymentId=data["payment_id"],
        status=data["status"],
        redirectUrl=data.get("redirect_url"),
        token=data.get("token"),
    )

