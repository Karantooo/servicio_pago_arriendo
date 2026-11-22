import grpc
import pytest
from app.models.equipo import Equipo
from generated import equipos_pb2, equipos_pb2_grpc
from google.protobuf import empty_pb2


class TestConsultarEquipo:
    def test_consultar_equipo(self, grpc_channel, crear_equipo):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=1,
            nombre="Notebook",
            descripcion="Notebook Lenovo",
            cantidad_total=10,
            unidades_disponibles=5,
            activo=True,
        )

        response = stub.ConsultarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        assert response.id == equipo.equipo_id
        assert response.nombre == equipo.nombre
        assert response.descripcion == equipo.descripcion
        assert response.unidades_disponibles == equipo.unidades_disponibles

    def test_consultar_equipo_inexistente(self, grpc_channel):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ConsultarEquipo(equipos_pb2.EquipoRequest(id=99999))

        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


class TestConsultarDisponibilidadEquipo:
    def test_consultar_disponibilidad_equipo(self, grpc_channel, crear_equipo):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=2,
            nombre="Proyector",
            descripcion="Proyector Epson",
            cantidad_total=5,
            unidades_disponibles=3,
            activo=True,
        )

        response = stub.ConsultarDisponibilidadEquipo(
            equipos_pb2.EquipoRequest(id=equipo.equipo_id)
        )

        assert response.id == equipo.equipo_id
        assert response.unidades_disponibles == equipo.unidades_disponibles

    def test_consultar_disponibilidad_equipo_sin_unidades(
        self, grpc_channel, crear_equipo
    ):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=11,
            nombre="Servidor",
            descripcion="Servidor Dell",
            cantidad_total=3,
            unidades_disponibles=0,
            activo=True,
        )

        response = stub.ConsultarDisponibilidadEquipo(
            equipos_pb2.EquipoRequest(id=equipo.equipo_id)
        )

        assert response.id == equipo.equipo_id
        assert response.unidades_disponibles == 0

    def test_consultar_disponibilidad_equipo_inexistente(self, grpc_channel):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ConsultarDisponibilidadEquipo(equipos_pb2.EquipoRequest(id=99999))

        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


class TestListarEquipos:
    def test_listar_equipos(self, grpc_channel, crear_equipo):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo1 = crear_equipo(
            equipo_id=1,
            nombre="Tablet",
            descripcion="Tablet Samsung",
            cantidad_total=8,
            unidades_disponibles=4,
            activo=True,
        )

        equipo2 = crear_equipo(
            equipo_id=2,
            nombre="Impresora",
            descripcion="Impresora HP",
            cantidad_total=6,
            unidades_disponibles=2,
            activo=True,
        )

        response = stub.ListarEquipos(empty_pb2.Empty())
        equipos = list(response)

        assert len(equipos) == 2

        assert any(equipo.id == equipo1.equipo_id for equipo in equipos)

        assert any(equipo.id == equipo2.equipo_id for equipo in equipos)

    def test_listar_equipos_catalogo_completo(self, grpc_channel, crear_equipo):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo1 = crear_equipo(
            equipo_id=20,
            nombre="Notebook",
            descripcion="Notebook Lenovo",
            cantidad_total=10,
            unidades_disponibles=5,
            activo=True,
        )

        equipo2 = crear_equipo(
            equipo_id=21,
            nombre="Proyector",
            descripcion="Proyector Epson",
            cantidad_total=5,
            unidades_disponibles=2,
            activo=True,
        )

        equipo3 = crear_equipo(
            equipo_id=22,
            nombre="Cámara",
            descripcion="Cámara Canon",
            cantidad_total=4,
            unidades_disponibles=1,
            activo=True,
        )

        response = stub.ListarEquipos(empty_pb2.Empty())
        equipos = list(response)

        equipos_por_id = {equipo.id: equipo for equipo in equipos}

        assert len(equipos) == 3

        assert equipos_por_id[equipo1.equipo_id].nombre == equipo1.nombre
        assert equipos_por_id[equipo2.equipo_id].nombre == equipo2.nombre
        assert equipos_por_id[equipo3.equipo_id].nombre == equipo3.nombre

        assert (
            equipos_por_id[equipo1.equipo_id].unidades_disponibles
            == equipo1.unidades_disponibles
        )

        assert (
            equipos_por_id[equipo2.equipo_id].unidades_disponibles
            == equipo2.unidades_disponibles
        )

        assert (
            equipos_por_id[equipo3.equipo_id].unidades_disponibles
            == equipo3.unidades_disponibles
        )

    def test_listar_equipos_sin_equipos(self, grpc_channel):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        response = stub.ListarEquipos(empty_pb2.Empty())
        equipos = list(response)

        assert equipos == []

    def test_listar_equipos_excluye_inactivos(self, grpc_channel, crear_equipo):
        # Supuesto: el catálogo listado solo debe incluir equipos activos.
        # Si el negocio decide mostrar también los inactivos, borra este
        # test o ajusta la aserción.
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo_activo = crear_equipo(
            equipo_id=80,
            nombre="Monitor",
            descripcion="Monitor LG",
            cantidad_total=5,
            unidades_disponibles=5,
            activo=True,
        )

        equipo_inactivo = crear_equipo(
            equipo_id=81,
            nombre="Router",
            descripcion="Router viejo",
            cantidad_total=2,
            unidades_disponibles=2,
            activo=False,
        )

        response = stub.ListarEquipos(empty_pb2.Empty())
        ids = {equipo.id for equipo in response}

        assert equipo_activo.equipo_id in ids
        assert equipo_inactivo.equipo_id not in ids


class TestReservarEquipo:
    def test_reservar_equipo(self, grpc_channel, crear_equipo, db_session):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=3,
            nombre="Cámara",
            descripcion="Cámara Canon",
            cantidad_total=4,
            unidades_disponibles=2,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        response = stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert isinstance(response, empty_pb2.Empty)
        assert new_equipo.unidades_disponibles == unidades_antes - 1

    def test_reservar_equipo_dos_veces(self, grpc_channel, crear_equipo, db_session):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=30,
            nombre="Notebook",
            descripcion="Notebook Dell",
            cantidad_total=5,
            unidades_disponibles=3,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert new_equipo.unidades_disponibles == (unidades_antes - 2)

    def test_reservar_ultima_unidad(self, grpc_channel, crear_equipo, db_session):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=31,
            nombre="Micrófono",
            descripcion="Micrófono Rode",
            cantidad_total=1,
            unidades_disponibles=1,
            activo=True,
        )

        stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert new_equipo.unidades_disponibles == 0

    def test_reservar_equipo_sin_unidades_disponibles(
        self, grpc_channel, crear_equipo, db_session
    ):
        # Caso de borde clave: no debe permitirse reservar cuando el
        # stock disponible ya es 0 (evita que unidades_disponibles
        # quede negativo).
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=32,
            nombre="Servidor",
            descripcion="Servidor Dell",
            cantidad_total=2,
            unidades_disponibles=0,
            activo=True,
        )

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert new_equipo.unidades_disponibles == 0

    def test_reservar_equipo_inexistente(self, grpc_channel):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=99999))

        # ReservarEquipo usa un chequeo genérico: id inexistente también
        # cae en FAILED_PRECONDITION, no en NOT_FOUND.
        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    def test_reservar_equipo_inactivo(self, grpc_channel, crear_equipo, db_session):
        # Supuesto: un equipo dado de baja (activo=False) no puede
        # reservarse aunque tenga unidades disponibles.
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=33,
            nombre="Proyector",
            descripcion="Proyector dado de baja",
            cantidad_total=3,
            unidades_disponibles=3,
            activo=False,
        )

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert new_equipo.unidades_disponibles == 3


class TestLiberarEquipo:
    def test_liberar_equipo(self, grpc_channel, crear_equipo, db_session):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=4,
            nombre="Micrófono",
            descripcion="Micrófono Rode",
            cantidad_total=3,
            unidades_disponibles=1,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        response = stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert isinstance(response, empty_pb2.Empty)
        assert new_equipo.unidades_disponibles == unidades_antes + 1

    def test_liberar_equipo_dos_veces(self, grpc_channel, crear_equipo, db_session):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=40,
            nombre="Tablet",
            descripcion="Tablet Samsung",
            cantidad_total=5,
            unidades_disponibles=1,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert new_equipo.unidades_disponibles == (unidades_antes + 2)

    def test_liberar_equipo_no_supera_cantidad_total(
        self, grpc_channel, crear_equipo, db_session
    ):
        # Confirmado contra la implementación real: si
        # unidades_disponibles ya es igual a cantidad_total, la llamada
        # se rechaza (FAILED_PRECONDITION) en vez de capear el valor.
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=41,
            nombre="Monitor",
            descripcion="Monitor Dell",
            cantidad_total=3,
            unidades_disponibles=3,
            activo=True,
        )

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        db_session.expire_all()

        new_equipo = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert new_equipo.unidades_disponibles == equipo.cantidad_total

    def test_liberar_equipo_inexistente(self, grpc_channel):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=99999))

        # LiberarEquipo usa un chequeo genérico: id inexistente también
        # cae en FAILED_PRECONDITION, no en NOT_FOUND.
        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION


class TestFlujoCombinado:
    def test_reservar_y_liberar_equipo(self, grpc_channel, crear_equipo, db_session):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=50,
            nombre="Proyector",
            descripcion="Proyector Epson",
            cantidad_total=4,
            unidades_disponibles=2,
            activo=True,
        )

        unidades_iniciales = equipo.unidades_disponibles

        stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        db_session.expire_all()

        equipo_reservado = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert equipo_reservado.unidades_disponibles == unidades_iniciales - 1

        stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        db_session.expire_all()

        equipo_liberado = (
            db_session.query(Equipo).filter(Equipo.equipo_id == equipo.equipo_id).one()
        )

        assert equipo_liberado.unidades_disponibles == unidades_iniciales

    def test_disponibilidad_se_actualiza_despues_de_reservar(
        self, grpc_channel, crear_equipo
    ):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=60,
            nombre="Monitor",
            descripcion="Monitor Samsung",
            cantidad_total=5,
            unidades_disponibles=3,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        disponibilidad_antes = stub.ConsultarDisponibilidadEquipo(
            equipos_pb2.EquipoRequest(id=equipo.equipo_id)
        )

        assert disponibilidad_antes.unidades_disponibles == equipo.unidades_disponibles

        stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        disponibilidad_despues = stub.ConsultarDisponibilidadEquipo(
            equipos_pb2.EquipoRequest(id=equipo.equipo_id)
        )

        assert disponibilidad_despues.unidades_disponibles == (unidades_antes - 1)

    def test_disponibilidad_se_actualiza_despues_de_liberar(
        self, grpc_channel, crear_equipo
    ):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=61,
            nombre="Monitor",
            descripcion="Monitor Samsung",
            cantidad_total=5,
            unidades_disponibles=2,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        disponibilidad_antes = stub.ConsultarDisponibilidadEquipo(
            equipos_pb2.EquipoRequest(id=equipo.equipo_id)
        )

        assert disponibilidad_antes.unidades_disponibles == equipo.unidades_disponibles

        stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        disponibilidad_despues = stub.ConsultarDisponibilidadEquipo(
            equipos_pb2.EquipoRequest(id=equipo.equipo_id)
        )

        assert disponibilidad_despues.unidades_disponibles == (unidades_antes + 1)

    def test_listar_equipos_refleja_reserva(self, grpc_channel, crear_equipo):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=70,
            nombre="Notebook",
            descripcion="Notebook HP",
            cantidad_total=5,
            unidades_disponibles=5,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        stub.ReservarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        response = stub.ListarEquipos(empty_pb2.Empty())
        equipos = list(response)

        equipo_response = next(e for e in equipos if e.id == equipo.equipo_id)

        assert equipo_response.unidades_disponibles == (unidades_antes - 1)

    def test_listar_equipos_refleja_liberacion(self, grpc_channel, crear_equipo):
        stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)

        equipo = crear_equipo(
            equipo_id=71,
            nombre="Cámara",
            descripcion="Cámara Sony",
            cantidad_total=5,
            unidades_disponibles=2,
            activo=True,
        )

        unidades_antes = equipo.unidades_disponibles

        stub.LiberarEquipo(equipos_pb2.EquipoRequest(id=equipo.equipo_id))

        response = stub.ListarEquipos(empty_pb2.Empty())
        equipos = list(response)

        equipo_response = next(e for e in equipos if e.id == equipo.equipo_id)

        assert equipo_response.unidades_disponibles == (unidades_antes + 1)


def test_reserva_identificada_con_reintentos_concurrentes(grpc_channel, crear_equipo):
    from uuid import uuid4

    crear_equipo(equipo_id=1, cantidad_total=1, unidades_disponibles=1)
    crear_equipo(equipo_id=2, cantidad_total=1, unidades_disponibles=1)
    stub = equipos_pb2_grpc.ServicioEquiposStub(grpc_channel)
    reserva_id = str(uuid4())
    reserva = equipos_pb2.ReservaEquipoRequest(id=1, reserva_id=reserva_id)
    consulta = equipos_pb2.EquipoRequest(id=1)

    intentos = [stub.ReservarEquipoIdentificado.future(reserva, timeout=5) for _ in range(2)]
    for intento in intentos:
        intento.result()
    assert stub.ConsultarDisponibilidadEquipo(consulta).unidades_disponibles == 0

    otro_equipo = equipos_pb2.ReservaEquipoRequest(id=2, reserva_id=reserva_id)
    with pytest.raises(grpc.RpcError) as error:
        stub.ReservarEquipoIdentificado(otro_equipo)
    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
    with pytest.raises(grpc.RpcError) as error:
        stub.LiberarEquipoIdentificado(otro_equipo)
    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS

    intentos = [stub.LiberarEquipoIdentificado.future(reserva, timeout=5) for _ in range(2)]
    for intento in intentos:
        intento.result()
    assert stub.ConsultarDisponibilidadEquipo(consulta).unidades_disponibles == 1

    otra_reserva = equipos_pb2.ReservaEquipoRequest(id=1, reserva_id=str(uuid4()))
    stub.ReservarEquipoIdentificado(otra_reserva)
    stub.LiberarEquipoIdentificado(reserva)
    assert stub.ConsultarDisponibilidadEquipo(consulta).unidades_disponibles == 0
    stub.LiberarEquipoIdentificado(otra_reserva)
    stub.ReservarEquipoIdentificado(reserva)
    assert stub.ConsultarDisponibilidadEquipo(consulta).unidades_disponibles == 1
