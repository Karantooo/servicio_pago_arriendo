from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import admin_validation
from app.bd_models import RentalTariff
from app.database import get_db

router = APIRouter(prefix="/v2/tariffs", tags=["Tariffs v2"])


class TariffRequest(BaseModel):
    dailyRate: int = Field(..., gt=0)
    deposit: int = Field(..., ge=0)


class TariffResponse(TariffRequest):
    equipmentId: int


@router.put("/{equipmentId}", response_model=TariffResponse)
def put_tariff(
    tariff_in: TariffRequest,
    equipmentId: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    admin_auth: dict = Depends(admin_validation),
) -> TariffResponse:
    tariff = db.get(RentalTariff, equipmentId)
    if tariff is None:
        tariff = RentalTariff(equipment_id=equipmentId)
        db.add(tariff)
    tariff.daily_rate = tariff_in.dailyRate
    tariff.deposit = tariff_in.deposit
    db.commit()
    return TariffResponse(
        equipmentId=equipmentId,
        dailyRate=tariff.daily_rate,
        deposit=tariff.deposit,
    )
