import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pagos.observability import request_id_context

logger = logging.getLogger(__name__)


def notify_payment_result(rental_id: int) -> None:
    base_url = os.getenv("RENTALS_API_URL")
    api_key = os.getenv("RENTALS_API_KEY")
    if not base_url or not api_key:
        return

    headers = {"API-Key": api_key}
    request_id = request_id_context.get()
    if request_id:
        headers["X-Request-ID"] = request_id
    request = Request(
        f"{base_url.rstrip('/')}/v2/rentals/{rental_id}/reconcile-payment",
        headers=headers,
        method="POST",
        data=b"",
    )
    try:
        with urlopen(request, timeout=5):
            pass
    except (HTTPError, URLError, TimeoutError) as error:
        logger.warning(
            "Rental payment reconciliation remains pending",
            extra={
                "event": "rental_payment_reconciliation_pending",
                "details": {"rental_id": rental_id, "error": str(error)},
            },
        )
