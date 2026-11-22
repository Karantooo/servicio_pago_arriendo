import os
import secrets
from typing import Dict

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from pagos.providers import get_provider
from pagos.providers.mock_provider import MockProvider
from pagos.resilience import circuit_breaker

router = APIRouter()


def require_admin_key(
    admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    expected_key = os.getenv("ADMIN_API_KEY")
    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "ADMIN_AUTH_NOT_CONFIGURED",
                "message": "La autenticación administrativa no está configurada.",
            },
        )
    if not admin_key:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "ADMIN_KEY_REQUIRED",
                "message": "Se requiere el header X-Admin-Key.",
            },
        )
    if not secrets.compare_digest(admin_key, expected_key):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "INVALID_ADMIN_KEY",
                "message": "La API Key de administrador es inválida.",
            },
        )


def _mock_provider() -> MockProvider:
    provider = get_provider()
    if not isinstance(provider, MockProvider):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "MOCK_PROVIDER_DISABLED",
                "message": "El servicio está configurado con un proveedor distinto de mock.",
            },
        )
    return provider


class ProviderModeRequest(BaseModel):
    mode: str = "NORMAL"


@router.post("/payments/mock-provider/mode")
async def set_provider_mode(
    mode: ProviderModeRequest,
    _: None = Depends(require_admin_key),
) -> Dict[str, str]:
    try:
        current = _mock_provider().set_mode(mode.mode)
        return {"status": "ok", "mode": current}
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_PROVIDER_MODE", "message": str(exc)},
        ) from exc


@router.get("/payments/mock-provider/mode")
async def get_provider_mode() -> Dict[str, str]:
    return {"mode": _mock_provider().mode, "state": circuit_breaker.state}
