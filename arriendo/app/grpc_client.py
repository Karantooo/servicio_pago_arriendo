import os
from datetime import datetime, timezone
from typing import NoReturn

import grpc
from fastapi import HTTPException, status

# Archivos generados durante la construcción de la imagen.
from app.generated import equipos_pb2, equipos_pb2_grpc


GRPC_URL = os.getenv("EQUIPOS_GRPC_URL", "equipos-api:50051")

GRPC_TIMEOUT_SECONDS = float(
    os.getenv("GRPC_TIMEOUT_SECONDS", "3")
)

if GRPC_TIMEOUT_SECONDS <= 0:
    raise ValueError(
        "GRPC_TIMEOUT_SECONDS debe ser mayor que cero"
    )

_channel = grpc.insecure_channel(GRPC_URL)
_stub = equipos_pb2_grpc.ServicioEquiposStub(_channel)


def get_grpc_stub():
    return _stub


def _raise_grpc_error(
    error: grpc.RpcError,
    equipment_id: int,
    operation: str,
) -> NoReturn:
    code = error.code()
    if code == grpc.StatusCode.FAILED_PRECONDITION:
        if operation == "reserve":
            detail = {
                "code": "INSUFFICIENT_STOCK",
                "message": (
                    f"El equipo {equipment_id} ya no tiene unidades disponibles."
                ),
            }
        else:
            detail = {
                "code": "INVALID_STATE_TRANSITION",
                "message": f"El equipo {equipment_id} no se puede liberar.",
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from error

    grpc_to_http = {
        grpc.StatusCode.CANCELLED: (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EQUIPMENTS_SERVICE_CANCELLED",
            "El servicio de Equipos canceló la operación.",
        ),
        grpc.StatusCode.UNKNOWN: (
            status.HTTP_502_BAD_GATEWAY,
            "EQUIPMENTS_SERVICE_ERROR",
            "El servicio de Equipos devolvió un error desconocido.",
        ),
        grpc.StatusCode.INVALID_ARGUMENT: (
            status.HTTP_400_BAD_REQUEST,
            "BAD_REQUEST",
            "El servicio de Equipos rechazó los datos de la operación.",
        ),
        grpc.StatusCode.DEADLINE_EXCEEDED: (
            status.HTTP_504_GATEWAY_TIMEOUT,
            "EQUIPMENTS_SERVICE_TIMEOUT",
            "Equipos no respondió dentro del tiempo permitido.",
        ),
        grpc.StatusCode.NOT_FOUND: (
            status.HTTP_404_NOT_FOUND,
            "EQUIPMENT_NOT_FOUND",
            f"El equipo {equipment_id} no existe en el catálogo.",
        ),
        grpc.StatusCode.ALREADY_EXISTS: (
            status.HTTP_409_CONFLICT,
            "EQUIPMENTS_RESOURCE_ALREADY_EXISTS",
            "El recurso ya existe en el servicio de Equipos.",
        ),
        grpc.StatusCode.PERMISSION_DENIED: (
            status.HTTP_502_BAD_GATEWAY,
            "EQUIPMENTS_SERVICE_AUTHORIZATION_ERROR",
            "El servicio de Equipos rechazó la autorización interna.",
        ),
        grpc.StatusCode.RESOURCE_EXHAUSTED: (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EQUIPMENTS_SERVICE_RESOURCE_EXHAUSTED",
            "El servicio de Equipos no dispone de recursos para atender la solicitud.",
        ),
        grpc.StatusCode.ABORTED: (
            status.HTTP_409_CONFLICT,
            "EQUIPMENTS_OPERATION_ABORTED",
            "La operación fue abortada por un conflicto en Equipos.",
        ),
        grpc.StatusCode.OUT_OF_RANGE: (
            status.HTTP_400_BAD_REQUEST,
            "BAD_REQUEST",
            "El valor solicitado está fuera del rango permitido.",
        ),
        grpc.StatusCode.UNIMPLEMENTED: (
            status.HTTP_502_BAD_GATEWAY,
            "EQUIPMENTS_OPERATION_UNSUPPORTED",
            "El servicio de Equipos no implementa esta operación.",
        ),
        grpc.StatusCode.INTERNAL: (
            status.HTTP_502_BAD_GATEWAY,
            "EQUIPMENTS_SERVICE_ERROR",
            "Ocurrió un error interno en el servicio de Equipos.",
        ),
        grpc.StatusCode.UNAVAILABLE: (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EQUIPMENTS_SERVICE_UNAVAILABLE",
            "El servicio de Equipos no está disponible.",
        ),
        grpc.StatusCode.DATA_LOSS: (
            status.HTTP_502_BAD_GATEWAY,
            "EQUIPMENTS_SERVICE_DATA_LOSS",
            "El servicio de Equipos informó una pérdida de datos.",
        ),
        grpc.StatusCode.UNAUTHENTICATED: (
            status.HTTP_502_BAD_GATEWAY,
            "EQUIPMENTS_SERVICE_AUTHENTICATION_ERROR",
            "Falló la autenticación interna con el servicio de Equipos.",
        ),
    }
    http_status, error_code, message = grpc_to_http.get(
        code,
        (
            status.HTTP_502_BAD_GATEWAY,
            "EQUIPMENTS_SERVICE_ERROR",
            "Ocurrió un error al comunicarse con el servicio de Equipos.",
        ),
    )
    raise HTTPException(
        status_code=http_status,
        detail={"code": error_code, "message": message},
    ) from error


def reservar_unidad(
    equipment_id: int,
    reservation_id: str | None = None,
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> None:
    stub = get_grpc_stub()
    request = equipos_pb2.EquipoRequest(id=equipment_id)

    try:
        if reservation_id is not None:
            reserva = equipos_pb2.ReservaEquipoRequest(id=equipment_id, reserva_id=reservation_id)
            if start_date is not None and end_date is not None:
                if start_date.tzinfo is None:
                    start_date = start_date.replace(tzinfo=timezone.utc)
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                reserva.inicio.FromDatetime(start_date)
                reserva.fin.FromDatetime(end_date)
            stub.ReservarEquipoIdentificado(
                reserva,
                timeout=GRPC_TIMEOUT_SECONDS,
            )
            return

        disponibilidad = stub.ConsultarDisponibilidadEquipo(
            request,
            timeout=GRPC_TIMEOUT_SECONDS,
        )
        if disponibilidad.unidades_disponibles <= 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "INSUFFICIENT_STOCK",
                    "message": (
                        f"El equipo {equipment_id} "
                        "no tiene unidades disponibles."
                    ),
                },
            )

        stub.ReservarEquipo(
            request,
            timeout=GRPC_TIMEOUT_SECONDS,
        )

    except grpc.RpcError as error:
        _raise_grpc_error(error, equipment_id, "reserve")


def liberar_unidad(equipment_id: int, reservation_id: str | None = None) -> None:
    stub = get_grpc_stub()
    request = equipos_pb2.EquipoRequest(id=equipment_id)

    try:
        if reservation_id is None:
            stub.LiberarEquipo(request, timeout=GRPC_TIMEOUT_SECONDS)
        else:
            stub.LiberarEquipoIdentificado(
                equipos_pb2.ReservaEquipoRequest(id=equipment_id, reserva_id=reservation_id),
                timeout=GRPC_TIMEOUT_SECONDS,
            )
    except grpc.RpcError as error:
        _raise_grpc_error(error, equipment_id, "release")
