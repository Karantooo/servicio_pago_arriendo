import os
from time import perf_counter
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException, status

from entregas_api.observability import request_id_context


def _raise_rentals_error(
    status_code: int, code: str, message: str
) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


class RentalsClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("RENTALS_API_URL", "http://arriendos-api:8080").rstrip("/")
        self.api_key = os.getenv("RENTALS_API_KEY") or os.getenv("ADMIN_API_KEY", "")
        self.timeout = float(os.getenv("RENTALS_TIMEOUT_SECONDS", "5"))

    async def get_rental(self, rental_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/v2/rentals/{rental_id}")
        required_fields = {"id", "clientId", "equipmentId", "status", "endDate"}
        if not required_fields.issubset(data):
            _raise_rentals_error(
                status.HTTP_502_BAD_GATEWAY,
                "RENTALS_SERVICE_ERROR",
                "Arriendos respondió con datos incompletos.",
            )
        return data

    async def get_saga(self, rental_id: int) -> dict[str, Any]:
        try:
            return await self._request("GET", f"/v2/rentals/{rental_id}/saga")
        except HTTPException as error:
            if error.status_code == status.HTTP_404_NOT_FOUND:
                _raise_rentals_error(
                    status.HTTP_409_CONFLICT,
                    "RENTAL_NOT_READY_FOR_DELIVERY",
                    "El arriendo no tiene una Saga lista para la entrega.",
                )
            raise

    async def notify_delivery_status(
        self, rental_id: int, payload: dict[str, Any]
    ) -> None:
        await self._request(
            "PUT",
            f"/v2/rentals/{rental_id}/delivery-status",
            json=payload,
            notification=True,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        notification: bool = False,
    ) -> dict[str, Any]:
        try:
            started = perf_counter()
            headers = {"API-Key": self.api_key}
            request_id = request_id_context.get()
            if request_id:
                headers["X-Request-ID"] = request_id
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json,
                )
        except httpx.TimeoutException as error:
            logger.warning(
                "Rentals API request timed out",
                extra={
                    "event": "rentals_api_request_timeout",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "details": {"method": method, "path": path, "error": str(error)},
                },
            )
            _raise_rentals_error(
                status.HTTP_504_GATEWAY_TIMEOUT,
                "RENTALS_SERVICE_TIMEOUT",
                "Tiempo de espera agotado al comunicar con Arriendos.",
            )
        except httpx.RequestError as error:
            logger.warning(
                "Rentals API request failed",
                extra={
                    "event": "rentals_api_request_failed",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "details": {"method": method, "path": path, "error": str(error)},
                },
            )
            _raise_rentals_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "RENTALS_SERVICE_UNAVAILABLE",
                "El servicio de Arriendos no se encuentra disponible.",
            )

        if response.status_code == status.HTTP_404_NOT_FOUND and not notification:
            _raise_rentals_error(
                status.HTTP_404_NOT_FOUND,
                "RENTAL_NOT_FOUND",
                "El arriendo solicitado no existe.",
            )
        if response.status_code != status.HTTP_200_OK:
            _raise_rentals_error(
                status.HTTP_502_BAD_GATEWAY,
                "RENTALS_SERVICE_ERROR",
                "Arriendos respondió con un error o con un estado inesperado.",
            )

        if notification:
            return {}
        try:
            data = response.json()
        except ValueError:
            _raise_rentals_error(
                status.HTTP_502_BAD_GATEWAY,
                "RENTALS_SERVICE_ERROR",
                "Arriendos respondió con JSON inválido.",
            )
        if not isinstance(data, dict):
            _raise_rentals_error(
                status.HTTP_502_BAD_GATEWAY,
                "RENTALS_SERVICE_ERROR",
                "Arriendos respondió con un formato inesperado.",
            )
        return data


def parse_rental_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        _raise_rentals_error(
            status.HTTP_502_BAD_GATEWAY,
            "RENTALS_SERVICE_ERROR",
            "Arriendos respondió con una fecha inválida.",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _raise_rentals_error(
            status.HTTP_502_BAD_GATEWAY,
            "RENTALS_SERVICE_ERROR",
            "Arriendos respondió con una fecha inválida.",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _raise_rentals_error(
            status.HTTP_502_BAD_GATEWAY,
            "RENTALS_SERVICE_ERROR",
            "Arriendos respondió con una fecha sin zona horaria.",
        )
    return parsed
