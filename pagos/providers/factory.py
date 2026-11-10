from functools import lru_cache

from pagos.config import PROVIDER_NAME
from pagos.providers.base import PaymentProvider
from pagos.providers.mock_provider import MockProvider
from pagos.providers.transbank_provider import TransbankProvider


@lru_cache(maxsize=1)
def get_provider() -> PaymentProvider:
    if PROVIDER_NAME == "mock":
        return MockProvider()
    if PROVIDER_NAME == "transbank":
        return TransbankProvider()
    raise ValueError(
        f"Proveedor de pagos no soportado: {PROVIDER_NAME}. Use 'mock' o 'transbank'."
    )
