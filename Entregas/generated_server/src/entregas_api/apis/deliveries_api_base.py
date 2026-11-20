# coding: utf-8

from typing import ClassVar, Dict, List, Tuple  # noqa: F401

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
from entregas_api.security_api import get_token_APIKeyHeader

class BaseDeliveriesApi:
    subclasses: ClassVar[Tuple] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseDeliveriesApi.subclasses = BaseDeliveriesApi.subclasses + (cls,)
    async def get_all_deliveries(
        self,
        limit: Annotated[Optional[Annotated[int, Field(le=100, ge=1)]], Field(description="Cantidad máxima de registros a retornar (1 a 100)")],
        offset: Annotated[Optional[Annotated[int, Field(ge=0)]], Field(description="Cantidad de registros a saltar (paginación)")],
        status: Optional[DeliveryStatus],
        payment_status: Optional[PaymentStatus],
        scheduled_from: Annotated[Optional[datetime], Field(description="Fecha agendada desde (inclusive)")],
        scheduled_to: Annotated[Optional[datetime], Field(description="Fecha agendada hasta (inclusive)")],
    ) -> List[DeliveryResponse]:
        """Retorna entregas ordenadas por fecha agendada. Si no hay registros, retorna una lista vacía."""
        ...


    async def create_delivery(
        self,
        idempotency_key: Annotated[str, Field(min_length=1, max_length=128, description="Clave única de la solicitud. Debe reutilizarse al reintentar la misma creación; una clave usada con otros datos produce 409.")],
        delivery_request: DeliveryRequest,
    ) -> DeliveryResponse:
        """Registra la entrega de un arriendo y la deja agendada (&#x60;SCHEDULED&#x60;) para la fecha indicada. Se llama al momento de realizar el pago. Valida contra Arriendos que: - el arriendo existe y &#x60;clientId&#x60; y &#x60;equipmentId&#x60; coinciden, - el arriendo no está cancelado, finalizado ni fallido, - &#x60;scheduledAt&#x60; es futura y no posterior al &#x60;endDate&#x60; del arriendo, - si &#x60;paymentStatus&#x60; es &#x60;PAID&#x60;, la Saga del arriendo está esperando la Entrega, - no existe ya una entrega para ese arriendo."""
        ...


    async def get_delivery_by_id(
        self,
        deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")],
    ) -> DeliveryResponse:
        ...


    async def get_delivery_by_rental(
        self,
        rentalId: Annotated[int, Field(ge=1, description="ID del arriendo en el servicio de Arriendos")],
    ) -> DeliveryResponse:
        """Retorna la entrega asociada al ID de arriendo (no consulta a Arriendos)."""
        ...


    async def get_delivery_events(
        self,
        deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")],
    ) -> List[DeliveryEvent]:
        """Retorna la trazabilidad (agendamiento, reagendamientos, pago y entrega), del más antiguo al más reciente."""
        ...


    async def reschedule_delivery(
        self,
        deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")],
        reschedule_request: RescheduleRequest,
    ) -> DeliveryResponse:
        """Cambia la fecha de retiro. Solo permitido mientras la entrega está &#x60;SCHEDULED&#x60;. Aplican las mismas reglas de fecha que al crearla (futura y no posterior al fin del arriendo); para validarlas consulta Arriendos."""
        ...


    async def mark_delivery_paid(
        self,
        deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")],
        mark_paid_request: Optional[MarkPaidRequest],
    ) -> DeliveryResponse:
        """Pasa &#x60;paymentStatus&#x60; de &#x60;UNPAID&#x60; a &#x60;PAID&#x60;. Es idempotente: si ya estaba &#x60;PAID&#x60;, responde 200 sin cambios. No se permite sobre entregas &#x60;CANCELLED&#x60;."""
        ...


    async def deliver_delivery(
        self,
        deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")],
        deliver_request: Optional[DeliverRequest],
    ) -> DeliveryResponse:
        """Solo desde &#x60;SCHEDULED&#x60; y con &#x60;paymentStatus &#x3D; PAID&#x60;; pasa a &#x60;DELIVERED&#x60; y guarda &#x60;deliveredAt&#x60;. Luego notifica a Arriendos por REST (&#x60;PUT /v2/rentals/{rentalId}/delivery-status&#x60; con &#x60;DELIVERED&#x60;), lo que activa el arriendo. Si la notificación falla, la entrega queda &#x60;DELIVERED&#x60; con &#x60;rentalSyncStatus &#x3D; SYNC_FAILED&#x60; y se responde 502, 503 o 504; se reintenta con &#x60;reconcile-rental&#x60;."""
        ...


    async def cancel_delivery(
        self,
        deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")],
        cancel_request: Optional[CancelRequest],
    ) -> DeliveryResponse:
        """Solo desde &#x60;SCHEDULED&#x60;. Notifica a Arriendos con &#x60;CANCELLED&#x60;."""
        ...


    async def reconcile_delivery_rental(
        self,
        deliveryId: Annotated[int, Field(ge=1, description="ID de la entrega")],
    ) -> DeliveryResponse:
        """Para entregas con &#x60;rentalSyncStatus &#x3D; SYNC_FAILED&#x60;. Reenvía a Arriendos el último resultado (idempotente) y, si Arriendos responde, deja &#x60;rentalSyncStatus &#x3D; SYNCED&#x60;."""
        ...
