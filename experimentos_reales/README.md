# Experimentos integrales con servicios reales

La suite levanta una instancia aislada de `docker-compose.yaml` y ejecuta los
casos por HTTP contra el gateway, usando los servicios, las bases de datos y
las llamadas internas reales del sistema. Pagos se inicia con la configuración
de prueba `PAYMENTS_PROVIDER=mock`, igual que en los tests existentes de ese
servicio: el proveedor externo de cobros, confirmaciones y reembolsos es el
único simulado. No se usa `TestClient` ni una base temporal. Cada caso se
ejecuta 50 veces por defecto y mide latencias HTTP y duración total; los
resultados incluyen promedio, mínimo, p95 y máximo.

## Requisitos

- Docker Desktop iniciado y funcional.
- Python 3.12 o posterior.
- Archivo `.env` del proyecto con `ADMIN_API_KEY` y `CLIENT_API_KEY`.
- Acceso a Internet para construir las imágenes de Docker.

Desde la raíz del proyecto, ejecutar en PowerShell:

```powershell
.\experimentos\run.ps1
```

El runner permite indicar repeticiones; para una corrida corta:

```powershell
.\experimentos\run.ps1 -Iterations 3
```

El script instala las dependencias, levanta los contenedores con un nombre de
proyecto y puerto aislados, espera el health check, ejecuta las pruebas y
elimina únicamente los contenedores y volúmenes de esa instancia. Configura
Pagos con el modo `NORMAL` y la misma cascada de timeouts del proyecto:
Transbank 1 s, proveedor 2 s, Pagos 3 s y gateway 5 s. Los contenedores de
otros proyectos no se detienen ni se reutilizan.
Las creaciones de arriendo se espacian al menos 250 ms para respetar el límite
de 5 solicitudes por segundo del gateway durante las repeticiones idempotentes.

## Casos ejecutados

| Caso | Flujo verificado |
|---|---|
| Checkout y persistencia | Cliente, arriendo, pago pendiente y lectura posterior desde los servicios |
| Idempotencia | Repetición de la solicitud y verificación de que conserva los mismos IDs |
| Pago y entrega | Callback HTTP, conciliación, entrega y Saga completada |
| Cancelación y reembolso | Cancelación de un arriendo pagado, baja de Entregas y reembolso |
| Autenticación | Rechazo de una solicitud al gateway sin credenciales |

Cada uno se repite 50 veces. La ejecución completa crea aproximadamente 200
registros de pago, confirma 100 y solicita 50 reembolsos al proveedor mock. No
se envían cobros ni reembolsos a Transbank o a otro proveedor externo.

## Evidencia

El log se guarda en `experimentos/evidencia/`. Incluye líneas
`RUN_METRIC_JSON`, `TRACE_JSON`/`EVIDENCE_JSON` y
`EXPERIMENT_SUMMARY_JSON`. Las trazas conservan rutas, códigos HTTP y
latencias; no guardan claves, JWT ni tokens del proveedor.

El número de ejecuciones se puede reducir para depurar:

```powershell
python -m pytest -s -v .\experimentos_reales --iterations 1 --base-url http://127.0.0.1:8080
```

Esta ejecución manual requiere que el stack ya esté levantado con
`PAYMENTS_PROVIDER=mock` y que `EXPERIMENTS_ADMIN_API_KEY` esté definida.
