# coding: utf-8

from typing import Dict, List  # noqa: F401
import importlib
import pkgutil

from entregas_api.apis.health_api_base import BaseHealthApi
import entregas_api.impl

from fastapi import (  # noqa: F401
    APIRouter,
    Body,
    Cookie,
    Depends,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Response,
    Security,
    status,
)

from entregas_api.models.health_response import HealthResponse

router = APIRouter()

ns_pkg = entregas_api.impl
for _, name, _ in pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + "."):
    importlib.import_module(name)


@router.get(
    "/health",
    responses={
        200: {"model": HealthResponse, "description": "El servicio está en ejecución"},
    },
    tags=["Health"],
    summary="Estado del servicio",
    response_model_by_alias=True,
)
async def health_check(
) -> HealthResponse:
    """Endpoint público para el healthcheck de Docker. No depende de Arriendos ni de la base de datos."""
    if not BaseHealthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseHealthApi.subclasses[0]().health_check()
