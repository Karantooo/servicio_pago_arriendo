# coding: utf-8

from typing import ClassVar, Dict, List, Tuple  # noqa: F401

from entregas_api.models.health_response import HealthResponse


class BaseHealthApi:
    subclasses: ClassVar[Tuple] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseHealthApi.subclasses = BaseHealthApi.subclasses + (cls,)
    async def health_check(
        self,
    ) -> HealthResponse:
        """Endpoint público para el healthcheck de Docker. No depende de Arriendos ni de la base de datos."""
        ...
