from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.database import Base

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
    status = Column(String(20), nullable=False, default="ACTIVE")  # ACTIVE, COMPLETED, CANCELLED, PENDING, FAILED, CANCELLING, RETURNING

    client = relationship("Client", back_populates="rentals")


class Saga(Base):
    __tablename__ = "sagas"

    id = Column(String(36), primary_key=True)
    rental_id = Column(Integer, ForeignKey("rentals.id"), nullable=False, index=True)
    operation = Column(String(20), nullable=False)
    status = Column(String(30), nullable=False, index=True)
    current_step = Column(String(50), nullable=False)
    payment_id = Column(BigInteger, nullable=True)
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
