import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def notify_payment_result(rental_id: int) -> None:
    base_url = os.getenv("RENTALS_API_URL")
    api_key = os.getenv("RENTALS_API_KEY")
    if not base_url or not api_key:
        return

    request = Request(
        f"{base_url.rstrip('/')}/v2/rentals/{rental_id}/reconcile-payment",
        headers={"API-Key": api_key},
        method="POST",
        data=b"",
    )
    try:
        with urlopen(request, timeout=5):
            pass
    except (HTTPError, URLError, TimeoutError) as error:
        logger.warning("La conciliación del arriendo %s quedó pendiente: %s", rental_id, error)
