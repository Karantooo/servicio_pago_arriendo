from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import grpc

from generated import equipos_pb2, equipos_pb2_grpc


def test_two_reservations_competing_for_last_unit_only_one_succeeds(
    grpc_channel, crear_equipo
):
    crear_equipo(
        equipo_id=90,
        nombre="Equipo de concurrencia",
        cantidad_total=1,
        unidades_disponibles=1,
        activo=True,
    )
    stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)
    start = Barrier(2)
    reservations = [
        equipos_pb2.ReservaEquipoRequest(id=90, reserva_id=str(uuid4()))
        for _ in range(2)
    ]

    def reserve(request):
        start.wait(timeout=5)
        try:
            stub.ReservarEquipoIdentificado(request, timeout=5)
            return grpc.StatusCode.OK
        except grpc.RpcError as error:
            return error.code()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, reservations))

    assert results.count(grpc.StatusCode.OK) == 1
    assert results.count(grpc.StatusCode.FAILED_PRECONDITION) == 1
    availability = stub.ConsultarDisponibilidadEquipo(
        equipos_pb2.EquipoRequest(id=90),
        timeout=5,
    )
    assert availability.unidades_disponibles == 0
