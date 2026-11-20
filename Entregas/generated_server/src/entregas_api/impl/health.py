from entregas_api.apis.health_api_base import BaseHealthApi
from entregas_api.models.health_response import HealthResponse


class HealthApi(BaseHealthApi):
    async def health_check(self) -> HealthResponse:
        return HealthResponse(status="ok")
