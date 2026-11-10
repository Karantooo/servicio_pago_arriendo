from typing import Any, Dict

from fastapi import APIRouter

from pagos.models import utc_now

router = APIRouter()


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    return {
        "status": "UP",
        "service": "payments",
        "createdAt": utc_now().isoformat().replace("+00:00", "Z"),
    }
