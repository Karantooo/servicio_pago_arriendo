from fastapi import FastAPI

from pagos.routes.health import router as health_router
from pagos.routes.mock_provider import router as mock_provider_router
from pagos.routes.payments import router as payments_router
from pagos.routes.transbank import router as transbank_router

app = FastAPI(
    title="Payments API",
    description="Servicio de pagos con proveedor configurable y resiliencia",
    version="1.1.0",
    servers=[
        {
            "url": "http://localhost:8003",
            "description": "Servicio de pagos local",
        }
    ],
)

app.include_router(health_router)
app.include_router(mock_provider_router)
app.include_router(payments_router)
app.include_router(transbank_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("pagos.main:app", host="0.0.0.0", port=8003, reload=False)
