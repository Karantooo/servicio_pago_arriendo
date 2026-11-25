# coding: utf-8

from typing import Dict, List  # noqa: F401
import importlib
import pkgutil

from entregas_api.apis.deliveries_api_base import BaseDeliveriesApi
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
from pydantic import BaseModel

from datetime import datetime
from pydantic import Field, field_validator
from typing import List, Optional
from typing_extensions import Annotated
from entregas_api.models.bad_gateway_error import BadGatewayError
from entregas_api.models.bad_request_error import BadRequestError
from entregas_api.models.cancel_request import CancelRequest
from entregas_api.models.conflict_error import ConflictError
from entregas_api.models.deliver_request import DeliverRequest
from entregas_api.models.delivery_event import DeliveryEvent
from entregas_api.models.delivery_request import DeliveryRequest
from entregas_api.models.delivery_response import DeliveryResponse
from entregas_api.models.delivery_status import DeliveryStatus
from entregas_api.models.forbidden_error import ForbiddenError
from entregas_api.models.gateway_timeout_error import GatewayTimeoutError
from entregas_api.models.internal_server_error import InternalServerError
from entregas_api.models.mark_paid_request import MarkPaidRequest
from entregas_api.models.not_authorized_error import NotAuthorizedError
from entregas_api.models.not_found_error import NotFoundError
from entregas_api.models.payment_status import PaymentStatus
from entregas_api.models.reschedule_request import RescheduleRequest
from entregas_api.models.service_unavailable_error import ServiceUnavailableError
from entregas_api.models.extra_models import TokenModel  # noqa: F401
from entregas_api.security_api import get_token_APIKeyHeader
from entregas_api.impl.failure_injection import (
    is_failure_injection_enabled,
    set_failure_mode,
)

router = APIRouter()


class FailureInjectionRequest(BaseModel):
    mode: str


@router.post(
    "/admin/failure-injection",
    responses={
        200: {"description": "Modo de inyección de fallas actualizado"},
        400: {"description": "Modo de falla no válido"},
        401: {"model": NotAuthorizedError},
        403: {"model": ForbiddenError},
        404: {"description": "La inyección de fallas está deshabilitada"},
    },
    tags=["Admin"],
    summary="Configurar inyección controlada de fallas",
)
async def configure_failure_injection(
    request: FailureInjectionRequest,
    token_APIKeyHeader: TokenModel = Security(get_token_APIKeyHeader),
) -> Dict[str, str]:
    if not is_failure_injection_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "FAILURE_INJECTION_DISABLED",
                "message": "La inyección de fallas está deshabilitada.",
            },
        )
    try:
        mode = set_failure_mode(request.mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_FAILURE_MODE", "message": str(exc)},
        ) from exc
    return {"status": "ok", "mode": mode}

ns_pkg = entregas_api.impl
for _, name, _ in pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + "."):
    importlib.import_module(name)


@router.get(
    "/v1/deliveries",
    responses={
        200: {"model": List[DeliveryResponse], "description": "Lista de entregas (puede ser vacía)"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;readonly&#x60; o &#x60;admin&#x60;)"},
    },
    tags=["Deliveries"],
    summary="Listar entregas",
    response_model_by_alias=True,
)
async def get_all_deliveries(
    limit: Annotated[Optional[Annotated[int, Field(le=100, ge=1)]], Field(description="Cantidad máxima de registros a retornar (1 a 100)")] = Query(20, description="Cantidad máxima de registros a retornar (1 a 100)", alias="limit", ge=1, le=100),
    offset: Annotated[Optional[Annotated[int, Field(ge=0)]], Field(description="Cantidad de registros a saltar (paginación)")] = Query(0, description="Cantidad de registros a saltar (paginación)", alias="offset", ge=0),
    status: Optional[DeliveryStatus] = Query(None, description="", alias="status"),
    payment_status: Optional[PaymentStatus] = Query(None, description="", alias="paymentStatus"),
    scheduled_from: Annotated[Optional[datetime], Field(description="Fecha agendada desde (inclusive)")] = Query(None, description="Fecha agendada desde (inclusive)", alias="scheduledFrom"),
    scheduled_to: Annotated[Optional[datetime], Field(description="Fecha agendada hasta (inclusive)")] = Query(None, description="Fecha agendada hasta (inclusive)", alias="scheduledTo"),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> List[DeliveryResponse]:
    """Retorna entregas ordenadas por fecha agendada. Si no hay registros, retorna una lista vacía."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().get_all_deliveries(limit, offset, status, payment_status, scheduled_from, scheduled_to)


@router.post(
    "/v1/deliveries",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": DeliveryResponse, "description": "Entrega creada y agendada"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Arriendo inexistente en Arriendos"},
        409: {"model": ConflictError, "description": "Conflicto. Posibles códigos: DELIVERY_ALREADY_EXISTS, RENTAL_NOT_READY_FOR_DELIVERY, RENTAL_DATA_MISMATCH, IDEMPOTENCY_KEY_REUSED, IDEMPOTENCY_IN_PROGRESS o DELIVERY_FAILURE_INJECTED."},
        500: {"model": InternalServerError, "description": "Error de persistencia local"},
        502: {"model": BadGatewayError, "description": "Respuesta inválida o inesperada del servicio de Arriendos"},
        503: {"model": ServiceUnavailableError, "description": "Servicio no disponible"},
        504: {"model": GatewayTimeoutError, "description": "Timeout al comunicar con el servicio de Arriendos"},
    },
    tags=["Deliveries"],
    summary="Crear y agendar una entrega",
    response_model_by_alias=True,
)
async def create_delivery(
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128, description="Clave única de la solicitud. Debe reutilizarse al reintentar la misma creación; una clave usada con otros datos produce 409.")] = Header(..., description="Clave única de la solicitud. Debe reutilizarse al reintentar la misma creación; una clave usada con otros datos produce 409.", pattern=r"^\S+$", min_length=1, max_length=128),
    delivery_request: DeliveryRequest = Body(..., description=""),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    """Registra la entrega de un arriendo y la deja agendada (&#x60;SCHEDULED&#x60;) para la fecha indicada. Se llama al momento de realizar el pago. Valida contra Arriendos que: - el arriendo existe y &#x60;clientId&#x60; y &#x60;equipmentId&#x60; coinciden, - el arriendo no está cancelado, finalizado ni fallido, - &#x60;scheduledAt&#x60; es futura y no posterior al &#x60;endDate&#x60; del arriendo, - si &#x60;paymentStatus&#x60; es &#x60;PAID&#x60;, la Saga del arriendo está esperando la Entrega, - no existe ya una entrega para ese arriendo."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().create_delivery(idempotency_key, delivery_request)


@router.get(
    "/v1/deliveries/{deliveryId}",
    responses={
        200: {"model": DeliveryResponse, "description": "Entrega encontrada"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;readonly&#x60; o &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Recurso no encontrado"},
    },
    tags=["Deliveries"],
    summary="Obtener una entrega",
    response_model_by_alias=True,
)
async def get_delivery_by_id(
    deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")] = Path(..., description="ID de la entrega", ge=1),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().get_delivery_by_id(deliveryId)


@router.get(
    "/v1/rentals/{rentalId}/delivery",
    responses={
        200: {"model": DeliveryResponse, "description": "Entrega encontrada"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;readonly&#x60; o &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "El arriendo no tiene una entrega registrada"},
    },
    tags=["Deliveries"],
    summary="Obtener la entrega de un arriendo",
    response_model_by_alias=True,
)
async def get_delivery_by_rental(
    rentalId: Annotated[int, Field(ge=1, description="ID del arriendo en el servicio de Arriendos")] = Path(..., description="ID del arriendo en el servicio de Arriendos", ge=1),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    """Retorna la entrega asociada al ID de arriendo (no consulta a Arriendos)."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().get_delivery_by_rental(rentalId)


@router.get(
    "/v1/deliveries/{deliveryId}/events",
    responses={
        200: {"model": List[DeliveryEvent], "description": "Eventos de la entrega"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;readonly&#x60; o &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Recurso no encontrado"},
    },
    tags=["Deliveries"],
    summary="Historial de eventos de una entrega",
    response_model_by_alias=True,
)
async def get_delivery_events(
    deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")] = Path(..., description="ID de la entrega", ge=1),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> List[DeliveryEvent]:
    """Retorna la trazabilidad (agendamiento, reagendamientos, pago y entrega), del más antiguo al más reciente."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().get_delivery_events(deliveryId)


@router.put(
    "/v1/deliveries/{deliveryId}/schedule",
    responses={
        200: {"model": DeliveryResponse, "description": "Entrega reagendada"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Recurso no encontrado"},
        409: {"model": ConflictError, "description": "Transición inválida (INVALID_STATE_TRANSITION): el estado actual de la entrega no permite la operación"},
        502: {"model": BadGatewayError, "description": "Respuesta inválida o inesperada del servicio de Arriendos"},
        503: {"model": ServiceUnavailableError, "description": "Servicio de Arriendos no disponible"},
        504: {"model": GatewayTimeoutError, "description": "Timeout al comunicar con el servicio de Arriendos"},
    },
    tags=["Deliveries"],
    summary="Reagendar una entrega",
    response_model_by_alias=True,
)
async def reschedule_delivery(
    deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")] = Path(..., description="ID de la entrega", ge=1),
    reschedule_request: RescheduleRequest = Body(..., description=""),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    """Cambia la fecha de retiro. Solo permitido mientras la entrega está &#x60;SCHEDULED&#x60;. Aplican las mismas reglas de fecha que al crearla (futura y no posterior al fin del arriendo); para validarlas consulta Arriendos."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().reschedule_delivery(deliveryId, reschedule_request)


@router.post(
    "/v1/deliveries/{deliveryId}/mark-paid",
    responses={
        200: {"model": DeliveryResponse, "description": "Entrega pagada"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Recurso no encontrado"},
        409: {"model": ConflictError, "description": "Transición inválida (INVALID_STATE_TRANSITION): el estado actual de la entrega no permite la operación"},
    },
    tags=["Deliveries"],
    summary="Marcar la entrega como pagada",
    response_model_by_alias=True,
)
async def mark_delivery_paid(
    deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")] = Path(..., description="ID de la entrega", ge=1),
    mark_paid_request: Optional[MarkPaidRequest] = Body(None, description=""),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    """Pasa &#x60;paymentStatus&#x60; de &#x60;UNPAID&#x60; a &#x60;PAID&#x60;. Es idempotente: si ya estaba &#x60;PAID&#x60;, responde 200 sin cambios. No se permite sobre entregas &#x60;CANCELLED&#x60;."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().mark_delivery_paid(deliveryId, mark_paid_request)


@router.post(
    "/v1/deliveries/{deliveryId}/deliver",
    responses={
        200: {"model": DeliveryResponse, "description": "Entrega registrada y Arriendos notificado"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Recurso no encontrado"},
        409: {"model": ConflictError, "description": "Transición inválida (INVALID_STATE_TRANSITION) o pago pendiente (PAYMENT_REQUIRED)."},
        500: {"model": InternalServerError, "description": "Error de persistencia local"},
        502: {"model": BadGatewayError, "description": "Respuesta inválida o inesperada del servicio de Arriendos"},
        503: {"model": ServiceUnavailableError, "description": "Servicio de Arriendos no disponible"},
        504: {"model": GatewayTimeoutError, "description": "Timeout al comunicar con el servicio de Arriendos"},
    },
    tags=["Deliveries"],
    summary="Registrar la entrega (el cliente retiró el equipo)",
    response_model_by_alias=True,
)
async def deliver_delivery(
    deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")] = Path(..., description="ID de la entrega", ge=1),
    deliver_request: Optional[DeliverRequest] = Body(None, description=""),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    """Solo desde &#x60;SCHEDULED&#x60; y con &#x60;paymentStatus &#x3D; PAID&#x60;; pasa a &#x60;DELIVERED&#x60; y guarda &#x60;deliveredAt&#x60;. Luego notifica a Arriendos por REST (&#x60;PUT /v2/rentals/{rentalId}/delivery-status&#x60; con &#x60;DELIVERED&#x60;), lo que activa el arriendo. Si la notificación falla, la entrega queda &#x60;DELIVERED&#x60; con &#x60;rentalSyncStatus &#x3D; SYNC_FAILED&#x60; y se responde 502, 503 o 504; se reintenta con &#x60;reconcile-rental&#x60;."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().deliver_delivery(deliveryId, deliver_request)


@router.post(
    "/v1/deliveries/{deliveryId}/cancel",
    responses={
        200: {"model": DeliveryResponse, "description": "Entrega cancelada"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Recurso no encontrado"},
        409: {"model": ConflictError, "description": "Transición inválida (INVALID_STATE_TRANSITION): el estado actual de la entrega no permite la operación"},
        500: {"model": InternalServerError, "description": "Error de persistencia local"},
        502: {"model": BadGatewayError, "description": "Respuesta inválida o inesperada del servicio de Arriendos"},
        503: {"model": ServiceUnavailableError, "description": "Servicio de Arriendos no disponible"},
        504: {"model": GatewayTimeoutError, "description": "Timeout al comunicar con el servicio de Arriendos"},
    },
    tags=["Deliveries"],
    summary="Cancelar una entrega",
    response_model_by_alias=True,
)
async def cancel_delivery(
    deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")] = Path(..., description="ID de la entrega", ge=1),
    cancel_request: Optional[CancelRequest] = Body(None, description=""),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    """Solo desde &#x60;SCHEDULED&#x60;. Notifica a Arriendos con &#x60;CANCELLED&#x60;."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().cancel_delivery(deliveryId, cancel_request)


@router.post(
    "/v1/deliveries/{deliveryId}/reconcile-rental",
    responses={
        200: {"model": DeliveryResponse, "description": "Sincronización exitosa"},
        400: {"model": BadRequestError, "description": "Datos de entrada inválidos"},
        401: {"model": NotAuthorizedError, "description": "No autenticado"},
        403: {"model": ForbiddenError, "description": "Permisos insuficientes (requiere rol &#x60;admin&#x60;)"},
        404: {"model": NotFoundError, "description": "Recurso no encontrado"},
        409: {"model": ConflictError, "description": "No hay resultado pendiente de sincronizar (NOTHING_TO_RECONCILE)"},
        502: {"model": BadGatewayError, "description": "Respuesta inválida o inesperada del servicio de Arriendos"},
        503: {"model": ServiceUnavailableError, "description": "Servicio de Arriendos no disponible"},
        504: {"model": GatewayTimeoutError, "description": "Timeout al comunicar con el servicio de Arriendos"},
    },
    tags=["Deliveries"],
    summary="Reintentar sincronización con Arriendos",
    response_model_by_alias=True,
)
async def reconcile_delivery_rental(
    deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")] = Path(..., description="ID de la entrega", ge=1),
    token_APIKeyHeader: TokenModel = Security(
        get_token_APIKeyHeader
    ),
) -> DeliveryResponse:
    """Para entregas con &#x60;rentalSyncStatus &#x3D; SYNC_FAILED&#x60;. Reenvía a Arriendos el último resultado (idempotente) y, si Arriendos responde, deja &#x60;rentalSyncStatus &#x3D; SYNCED&#x60;."""
    if not BaseDeliveriesApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseDeliveriesApi.subclasses[0]().reconcile_delivery_rental(deliveryId)
