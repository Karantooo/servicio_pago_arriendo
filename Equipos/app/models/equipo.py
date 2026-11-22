from datetime import datetime, timezone

from app.models.base import Base
from sqlalchemy import Boolean, CheckConstraint, DateTime, String, Text, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column


class Equipo(Base):
    __tablename__ = "equipos"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    equipo_id: Mapped[int] = mapped_column(
        nullable=False,
        index=True,
    )

    nombre: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    descripcion: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    cantidad_total: Mapped[int] = mapped_column(
        nullable=False,
    )

    unidades_disponibles: Mapped[int] = mapped_column(
        nullable=False,
    )

    activo: Mapped[bool] = mapped_column(
        default=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "cantidad_total >= 0",
            name="ck_equipos_total_no_negativo",
        ),
        CheckConstraint(
            "unidades_disponibles >= 0",
            name="ck_equipos_disponible_no_negativo",
        ),
        CheckConstraint(
            "unidades_disponibles <= cantidad_total",
            name="ck_equipos_disponible_menor_total",
        ),
    )


class ReservaEquipo(Base):
    __tablename__ = "reservas_equipos"

    reserva_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    equipo_id: Mapped[int] = mapped_column(nullable=False)
    inicio: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    liberada: Mapped[bool] = mapped_column(nullable=False, default=False)


class ReservaEquipoEnConflicto(Exception):
    pass


def _utc(fecha: datetime | None) -> datetime | None:
    if fecha is not None and fecha.tzinfo is None:
        return fecha.replace(tzinfo=timezone.utc)
    return fecha


def _hay_capacidad(
    db: Session, equipo_id: int, inicio: datetime | None = None, fin: datetime | None = None,
    excluir_reserva: str | None = None,
) -> bool:
    # Serializa las admisiones para este equipo hasta el commit o rollback.
    equipo = db.execute(
        select(Equipo).where(Equipo.equipo_id == equipo_id, Equipo.activo.is_(True))
        .with_for_update()
    ).scalar_one_or_none()
    if equipo is None or equipo.unidades_disponibles == 0:
        return False

    reservas = select(ReservaEquipo.inicio, ReservaEquipo.fin).where(
        ReservaEquipo.equipo_id == equipo_id,
        ReservaEquipo.liberada.is_(False),
        ReservaEquipo.inicio.is_not(None),
        ReservaEquipo.fin.is_not(None),
    )
    if excluir_reserva is not None:
        reservas = reservas.where(ReservaEquipo.reserva_id != excluir_reserva)
    if inicio is not None and fin is not None:
        reservas = reservas.where(ReservaEquipo.inicio < fin, ReservaEquipo.fin > inicio)

    # Los fines (-1) se procesan antes que los inicios (+1) en el mismo instante.
    eventos = []
    for desde, hasta in db.execute(reservas):
        desde, hasta = _utc(desde), _utc(hasta)
        eventos.extend(((desde, 1), (hasta, -1)))

    ocupadas = 0
    for _, cambio in sorted(eventos):
        ocupadas += cambio
        if ocupadas >= equipo.unidades_disponibles:
            return False
    return True


def _descontar_unidad(db: Session, equipo_id: int) -> bool:
    if not _hay_capacidad(db, equipo_id):
        return False
    stmt = (
        update(Equipo)
        .where(
            Equipo.equipo_id == equipo_id,
            Equipo.activo == True,
            Equipo.unidades_disponibles > 0,
        )
        .values(unidades_disponibles=Equipo.unidades_disponibles - 1)
    )
    return db.execute(stmt).rowcount == 1


def _devolver_unidad(db: Session, equipo_id: int, solo_activo: bool = True) -> bool:
    stmt = (
        update(Equipo)
        .where(
            Equipo.equipo_id == equipo_id,
            Equipo.unidades_disponibles < Equipo.cantidad_total,
        )
        .values(unidades_disponibles=Equipo.unidades_disponibles + 1)
    )
    if solo_activo:
        stmt = stmt.where(Equipo.activo == True)
    return db.execute(stmt).rowcount == 1


def reservar_equipo(db: Session, equipo_id: int) -> bool:
    reservado = _descontar_unidad(db, equipo_id)
    if reservado:
        db.commit()
    else:
        db.rollback()
    return reservado


def liberar_equipo(db: Session, equipo_id: int) -> bool:
    liberado = _devolver_unidad(db, equipo_id)
    if liberado:
        db.commit()
    else:
        db.rollback()
    return liberado


def reservar_equipo_identificado(
    db: Session,
    equipo_id: int,
    reserva_id: str,
    inicio: datetime | None = None,
    fin: datetime | None = None,
) -> bool:
    creada = db.execute(
        insert(ReservaEquipo)
        .values(reserva_id=reserva_id, equipo_id=equipo_id, inicio=inicio, fin=fin, liberada=False)
        .on_conflict_do_nothing(index_elements=[ReservaEquipo.reserva_id])
        .returning(ReservaEquipo.reserva_id)
    ).scalar_one_or_none()

    if creada is None:
        existente = db.get(ReservaEquipo, reserva_id)
        if (existente.equipo_id, _utc(existente.inicio), _utc(existente.fin)) != (
            equipo_id, inicio, fin
        ):
            raise ReservaEquipoEnConflicto
        # Conservar el historial evita que un reintento tardío reactive la reserva.
        return True

    if inicio is None:
        reservada = _descontar_unidad(db, equipo_id)
    else:
        reservada = _hay_capacidad(db, equipo_id, inicio, fin, excluir_reserva=reserva_id)
    if not reservada:
        db.rollback()
        return False

    db.commit()
    return True


def liberar_equipo_identificado(db: Session, equipo_id: int, reserva_id: str) -> bool:
    reserva = db.get(ReservaEquipo, reserva_id, with_for_update=True)
    if reserva is None:
        raise LookupError(reserva_id)
    if reserva.equipo_id != equipo_id:
        raise ReservaEquipoEnConflicto
    if reserva.liberada:
        return True

    if reserva.inicio is None and not _devolver_unidad(db, equipo_id, solo_activo=False):
        db.rollback()
        return False

    reserva.liberada = True
    db.commit()
    return True
