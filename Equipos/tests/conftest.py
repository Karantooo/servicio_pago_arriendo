from concurrent import futures

import grpc
import pytest
from app.database import SessionLocal
from app.models.equipo import Equipo, ReservaEquipo
from app.servicio_equipos import ServicioEquipos
from generated import equipos_pb2_grpc


@pytest.fixture()
def db_session():
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


def limpiar_equipos():
    session = SessionLocal()

    try:
        session.query(ReservaEquipo).delete()
        session.query(Equipo).delete()
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def limpiar_db():
    limpiar_equipos()
    yield
    limpiar_equipos()


@pytest.fixture()
def crear_equipo(db_session):

    def _crear_equipo(
        equipo_id=1,
        nombre="Equipo de prueba",
        descripcion="Descripción de prueba",
        cantidad_total=10,
        unidades_disponibles=10,
        activo=True,
    ):
        equipo = Equipo(
            equipo_id=equipo_id,
            nombre=nombre,
            descripcion=descripcion,
            cantidad_total=cantidad_total,
            unidades_disponibles=unidades_disponibles,
            activo=activo,
        )

        db_session.add(equipo)
        db_session.commit()
        db_session.refresh(equipo)

        return equipo

    return _crear_equipo


@pytest.fixture
def grpc_channel():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))

    equipos_pb2_grpc.add_ServicioEquiposServicer_to_server(ServicioEquipos(), server)

    port = server.add_insecure_port("[::]:0")
    server.start()

    channel = grpc.insecure_channel(f"localhost:{port}")

    yield channel

    channel.close()
    server.stop(grace=0)
