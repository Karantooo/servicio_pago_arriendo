import grpc
from app.database import SessionLocal
from app.models.equipo import (
    Equipo,
    ReservaEquipoEnConflicto,
    liberar_equipo,
    liberar_equipo_identificado,
    reservar_equipo,
    reservar_equipo_identificado,
)
from uuid import UUID
from generated import equipos_pb2, equipos_pb2_grpc
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from google.protobuf import empty_pb2

class ServicioEquipos(equipos_pb2_grpc.ServicioEquiposServicer):
    """Implementacion del contrato definido en equipos.proto."""

    def ConsultarEquipo(self, request, context):
        db = SessionLocal()
        try:
            equipo = (
                db.query(Equipo)
                .filter(
                    Equipo.equipo_id == int(request.id),
                    Equipo.activo == True,
                )
                .one()
            )

            print("equipo:", equipo)
            print("equipo_id:", equipo.equipo_id, type(equipo.equipo_id))
            print("nombre:", equipo.nombre, type(equipo.nombre))
            print("descripcion:", equipo.descripcion, type(equipo.descripcion))
            print(
                "unidades_disponibles:",
                equipo.unidades_disponibles,
                type(equipo.unidades_disponibles),
            )

            response = equipos_pb2.Equipo(
                id=int(equipo.equipo_id),
                nombre=str(equipo.nombre),
                descripcion=str(equipo.descripcion),
                unidades_disponibles=int(equipo.unidades_disponibles),
            )

            return response

        except NoResultFound as e:
            context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"Equipo no encontrado: {str(e)}",
            )
        except MultipleResultsFound as e:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"Error (múltiples resultados): {str(e)}",
            )
        except Exception as e:

            context.abort(
                grpc.StatusCode.INTERNAL,
                f"Error al consultar equipo: {str(e)}",
            )
        finally:
            db.close()

    def ConsultarDisponibilidadEquipo(self, request, context):
        db = SessionLocal()
        try:
            equipo = (
                db.query(Equipo)
                .filter(Equipo.equipo_id == int(request.id), Equipo.activo == True)
                .one()
            )
            return equipos_pb2.EquipoDisponibilidad(
                id=equipo.equipo_id, unidades_disponibles=equipo.unidades_disponibles
            )
        except NoResultFound as e:
            context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"Equipo no encontrado: {str(e)}",
            )
        except MultipleResultsFound as e:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"Error (múltiples resultados): {str(e)}",
            )
        except Exception as e:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"Error al consultar disponibilidad: {str(e)}",
            )

        finally:
            db.close()

    def ListarEquipos(self, request, context):
        db = SessionLocal()
        # Deberiamos de colocar un limite de resultados y paginacion
        try:
            equipos = db.query(Equipo).filter(Equipo.activo == True).yield_per(100)

            for equipo in equipos:
                yield equipos_pb2.Equipo(
                    id=equipo.equipo_id,
                    nombre=equipo.nombre,
                    descripcion=equipo.descripcion,
                    unidades_disponibles=equipo.unidades_disponibles,
                )

        except Exception as e:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"Error al listar equipos: {str(e)}",
            )
        finally:
            db.close()

    def ReservarEquipo(self, request, context):
        db = SessionLocal()

        try:
            reservado = reservar_equipo(db, int(request.id))

        except Exception as e:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"Error al reservar equipo: {str(e)}",
            )
        finally:
            db.close()

        if not reservado:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "No se puede reservar el equipo",
            )

        return empty_pb2.Empty()

    def LiberarEquipo(self, request, context):
        db = SessionLocal()

        try:
            reservado = liberar_equipo(db, int(request.id))

        except Exception as e:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"Error al liberar equipo: {str(e)}",
            )
        finally:
            db.close()

        if not reservado:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "No se puede liberar el equipo",
            )

        return empty_pb2.Empty()

    def ReservarEquipoIdentificado(self, request, context):
        return self._ejecutar_reserva_identificada(
            request, context, reservar_equipo_identificado, "reservar"
        )

    def LiberarEquipoIdentificado(self, request, context):
        return self._ejecutar_reserva_identificada(
            request, context, liberar_equipo_identificado, "liberar"
        )

    def _ejecutar_reserva_identificada(self, request, context, operacion, accion):
        try:
            reserva_id = str(UUID(request.reserva_id))
        except ValueError:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "reserva_id debe ser un UUID.")
        if request.id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "El ID de equipo debe ser positivo.")

        try:
            with SessionLocal() as db:
                completada = operacion(db, request.id, reserva_id)
        except ReservaEquipoEnConflicto:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, "La reserva pertenece a otro equipo.")
        except LookupError:
            context.abort(grpc.StatusCode.NOT_FOUND, "La reserva no existe.")
        except Exception:
            context.abort(grpc.StatusCode.INTERNAL, f"Error al {accion} el equipo.")

        if not completada:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, f"No se puede {accion} el equipo.")
        return empty_pb2.Empty()
