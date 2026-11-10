from datetime import datetime, timezone
from sqlalchemy import BigInteger, CheckConstraint, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.database import Base

class RentalTariff(Base):
    __tablename__ = "rental_tariffs"

    equipment_id = Column(Integer, primary_key=True)
    daily_rate = Column(BigInteger, nullable=False)
    deposit = Column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint("daily_rate > 0", name="ck_tariff_daily_rate_positive"),
        CheckConstraint("deposit >= 0", name="ck_tariff_deposit_nonnegative"),
    )

class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    rentals = relationship("Rental", back_populates="client")

class Rental(Base):
    __tablename__ = "rentals"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    equipment_id = Column(Integer, nullable=False, index=True)
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)
    amount_rent = Column(BigInteger, nullable=True)
    amount_deposit = Column(BigInteger, nullable=True)
    status = Column(String(20), nullable=False, default="ACTIVE")  # ACTIVE, COMPLETED, CANCELLED, PENDING, FAILED, CANCELLING, RETURNING

    client = relationship("Client", back_populates="rentals")


class Saga(Base):
    __tablename__ = "sagas"

    id = Column(String(36), primary_key=True)
    rental_id = Column(Integer, ForeignKey("rentals.id"), nullable=False, index=True)
    operation = Column(String(20), nullable=False)
    status = Column(String(30), nullable=False, index=True)
    current_step = Column(String(50), nullable=False)
    idempotency_key = Column(String(128), unique=True, nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    payment_id = Column(BigInteger, nullable=True)
    checkout_status = Column(String(20), nullable=True)
    checkout_redirect_url = Column(Text, nullable=True)
    checkout_token = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
