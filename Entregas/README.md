# Servicio de Entregas

El contrato canónico está en `openapi.yaml`. El servidor FastAPI está en `generated_server/` y se generó inicialmente con OpenAPI Generator 7.27.0 (`python-fastapi`); los controladores y modelos generados se complementan con la persistencia, validaciones, autenticación e integración implementadas en `generated_server/src/entregas_api/impl/`.

Para regenerar el esqueleto desde la raíz del proyecto:

```powershell
npx --yes @openapitools/openapi-generator-cli generate `
  -i "servicio_pago_arriendo\Entregas\openapi.yaml" `
  -g python-fastapi `
  -o "servicio_pago_arriendo\Entregas\generated_server" `
  --additional-properties=packageName=entregas_api
```

La regeneración reemplaza archivos del servidor generado. Revisar y reaplicar los ajustes de `main.py`, `security_api.py`, `apis/deliveries_api.py`, `Dockerfile` y `impl/` antes de desplegar.

## Ejecución local

```powershell
cd servicio_pago_arriendo\Entregas\generated_server
$env:ADMIN_API_KEY = "..."
$env:CLIENT_API_KEY = "..."
$env:RENTALS_API_URL = "http://localhost:8080"
$env:RENTALS_API_KEY = $env:ADMIN_API_KEY
pip install -r requirements.txt
$env:PYTHONPATH = "src"
uvicorn entregas_api.main:app --host 0.0.0.0 --port 8081
```

`DATABASE_URL` permite configurar PostgreSQL; si no se define, se usa SQLite local. Docker Compose persiste SQLite en el volumen `entregas_data`. `RENTALS_TIMEOUT_SECONDS` configura el timeout de las llamadas a Arriendos (5 segundos por defecto).

La autenticación usa `API-Key`: `CLIENT_API_KEY` permite consultas y `ADMIN_API_KEY` permite además escrituras. `/health` es público. La sincronización final con Arriendos es recuperable con `POST /v1/deliveries/{deliveryId}/reconcile-rental`.

## Inyección de fallas para demostraciones

La inyección de fallas está deshabilitada por defecto. Para habilitar temporalmente el control administrativo, configura `DELIVERIES_ENABLE_FAILURE_INJECTION=true` en el servicio de Entregas y reinícialo. El endpoint requiere la `ADMIN_API_KEY`:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8081/admin/failure-injection" `
  -Headers @{"API-Key" = $env:ADMIN_API_KEY} `
  -ContentType "application/json" `
  -Body '{"mode":"FAIL_NEXT"}'
```

`FAIL_NEXT` hace que la próxima creación/agendamiento responda con HTTP 409 y código `DELIVERY_FAILURE_INJECTED`, sin guardar la entrega; luego vuelve automáticamente a `NORMAL`. `ALWAYS_FAIL` mantiene el fallo hasta configurar `NORMAL`. El control solo debe habilitarse en entornos de prueba o demostración.

## Pruebas

```powershell
cd servicio_pago_arriendo\Entregas\generated_server
pip install -r requirements-dev.txt
pytest -q
```

También se ejecutan como parte de `docker compose -f docker-compose.test.yaml up --build` desde la raíz del proyecto.
