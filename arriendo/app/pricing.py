from datetime import datetime, timedelta
from math import ceil

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.bd_models import RentalTariff


def calculate_rental_amounts(
    db: Session, equipment_id: int, start_date: datetime, end_date: datetime
) -> tuple[int, int]:
    tariff = db.get(RentalTariff, equipment_id)
    if tariff is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "TARIFF_NOT_FOUND",
                "message": f"No hay una tarifa para el equipo {equipment_id}.",
            },
        )

    duration = end_date - start_date
    if duration <= timedelta(0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "endDate debe ser posterior a startDate."},
        )

    days = ceil(duration / timedelta(days=1))
    return tariff.daily_rate * days, tariff.deposit
