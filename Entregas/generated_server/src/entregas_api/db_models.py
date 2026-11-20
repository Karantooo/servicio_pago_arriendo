from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from entregas_api.database import Base


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("rental_id", name="uq_deliveries_rental_id"),
        Index("ix_deliveries_status_scheduled_at", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rental_id: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[int] = mapped_column(Integer, nullable=False)
    equipment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SCHEDULED")
    payment_status: Mapped[str] = mapped_column(String(20), nullable=False)
    payment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_by: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(String(300))
    cancel_reason: Mapped[str | None] = mapped_column(String(300))
    rental_sync_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NOT_REQUIRED"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    events: Mapped[list["DeliveryEventRecord"]] = relationship(
        back_populates="delivery", cascade="all, delete-orphan"
    )


class DeliveryEventRecord(Base):
    __tablename__ = "delivery_events"
    __table_args__ = (Index("ix_delivery_events_delivery_created", "delivery_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    delivery: Mapped[Delivery] = relationship(back_populates="events")


class IdempotencyKey(Base):
    __tablename__ = "delivery_idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
