# Instrucciones de ejecución

Las siguientes instrucciones asumen que **Docker** y **Docker Compose** se encuentran instalados y que los comandos se ejecutan desde la **raíz del proyecto**.

## Configuración del archivo `.env`

Modifique las variables de entorno según sea necesario para adaptarlas a su contexto de ejecución.

## Ejecución de los servicios

Para construir las imágenes y levantar los servicios definidos en `docker-compose.yaml`, ejecutar:

```bash
docker compose -f docker-compose.yaml up --build
```

La API REST queda disponible en `http://localhost:8080`. El servicio gRPC y las bases de datos no publican puertos al host: los servicios se comunican por la red interna de Docker Compose.
La API de Entregas queda disponible en `http://localhost:8081`; su implementación y configuración están descritas en [Entregas/README.md](./Entregas/README.md).

Para detener los contenedores asociados al proyecto:

```bash
docker compose down
```

Si además se desea eliminar los volúmenes asociados a los servicios, lo que implica eliminar los datos persistidos en ellos, ejecutar:

```bash
docker compose down -v
```

> **Advertencia:** el uso de `-v` elimina los volúmenes asociados al proyecto y, por lo tanto, los datos persistidos en ellos.

## Ejecución de las pruebas

Las pruebas pueden ejecutarse mediante la configuración definida en `docker-compose.test.yaml`:

```bash
docker compose -f docker-compose.test.yaml up --build
```

Una vez finalizadas las pruebas, los servicios pueden permanecer activos. Para detenerlos y eliminar los volúmenes generados durante la ejecución de las pruebas:

```bash
docker compose down -v
```

## Verificación de contenedores activos

Para comprobar qué contenedores se encuentran actualmente en ejecución:

```bash
docker ps
```

Si se identifica algún contenedor que permanezca activo y deba ser detenido, se puede utilizar:

```bash
docker stop <id_contenedor>
```

Reemplazando `<id_contenedor>` por el identificador correspondiente.

Una vez detenido el contenedor, si se desea eliminarlo junto con los volúmenes anónimos asociados:

```bash
docker rm -v <id_contenedor>
```

Esta última operación elimina el contenedor y los volúmenes anónimos asociados a él.


## API v2 con Saga

La API `v2` mantiene las rutas de arriendos bajo `/v2/rentals`. Su contrato está en `arriendo/openapiv2.yaml`; el `openapi.yaml` original no incluye cambios de v2. Las operaciones de escritura (`POST /v2/rentals`, `POST /{rentalId}/cancel` y `POST /{rentalId}/return`) son coordinadas por `app/saga_orchestrator.py` y registran el estado en la tabla `sagas`. Se puede consultar la Saga más reciente de un arriendo mediante `GET /v2/rentals/{rentalId}/saga`.

Antes de crear un arriendo v2, un administrador debe registrar su tarifa con `PUT /v2/tariffs/{equipmentId}` y un JSON como `{"dailyRate": 10000, "deposit": 20000}`. Arriendos cobra cada periodo iniciado de hasta 24 horas a `dailyRate` y suma una garantía fija `deposit`. El cliente no envía montos en `POST /v2/rentals`; Arriendos los calcula y guarda una copia en el arriendo para mantener el importe ante cambios posteriores de tarifa. Si falta la tarifa, la creación responde `TARIFF_NOT_FOUND` sin reservar la unidad.

`POST /v2/rentals` requiere `Idempotency-Key`; los reintentos de la misma solicitud deben reutilizarla. Tras reservar la unidad, Arriendos inicia Pagos con esa clave. Una respuesta `PENDING` deja el arriendo pendiente y devuelve `rental` junto con `payment` (ID, URL y token de Webpay). Un rechazo confirmado de Pagos libera la unidad; un resultado incierto deja la Saga `IN_DOUBT` para conciliación. Un pago confirmado avanza automáticamente al agendamiento de Entregas; cuando Entregas notifica `DELIVERED`, Arriendos activa el arriendo.
Cuando el cobro queda confirmado, Arriendos llama automáticamente a `POST /v1/deliveries` de Entregas para ejecutar el paso 3 de la Saga. La llamada usa la misma clave idempotente del arriendo, `PAID` como estado inicial de pago y `deliveryId` queda persistido en la Saga. Un agendamiento exitoso avanza la Saga a `DELIVERY_SCHEDULED`; un resultado incierto deja `DELIVERY_SCHEDULING_IN_DOUBT` para ser reintentado por `POST /v2/rentals/{rentalId}/reconcile-payment`.

Cada transición relevante queda registrada en `saga_step_events`. Se puede consultar el historial completo mediante `GET /v2/rentals/{rentalId}/saga/events`.

Si Entregas rechaza el agendamiento después de cobrar, Arriendos ejecuta las compensaciones en orden inverso: solicita a Pagos el reembolso con una clave idempotente derivada de la Saga y luego libera la unidad en Equipos. El `refundId`, su estado y el resultado de la compensación quedan registrados en la Saga. Si el reembolso o la liberación no pueden confirmarse, la SAGA queda en `IN_DOUBT` o `COMPENSATION_FAILED` y puede conciliarse mediante la misma operación de pago.

Entregas consulta arriendos y Sagas con la API Key de administrador. Al registrar el retiro, llama `PUT /v2/rentals/{rentalId}/delivery-status` para activar el arriendo; una cancelación notifica el mismo endpoint y cancela el arriendo liberando la unidad mediante una Saga. Las notificaciones se deduplican por `deliveryId` y estado. Los estados `IN_DOUBT` y `COMPENSATION_FAILED` requieren revisión: la llamada remota pudo haber tenido éxito aunque la respuesta o la compensación no se hayan confirmado. Los arriendos v2 nuevos usan una identidad de reserva persistida para los comandos gRPC de Equipos. Repetir la reserva o la liberación con esa identidad no vuelve a modificar el stock. Los arriendos creados antes del cambio conservan la liberación por ID de equipo. La conciliación sigue siendo manual.

Los arriendos v2 nuevos reservan el equipo para el intervalo `[startDate, endDate)`. Equipos serializa las nuevas reservas por equipo y compara su ocupación simultánea con la capacidad disponible. Dos arriendos pueden compartir la misma unidad si sus períodos no se solapan. Las reservas antiguas sin fechas y las de v1 siguen consumiendo una unidad global, y se descuentan de la capacidad para todos los intervalos. Las consultas de catálogo de Equipos siguen mostrando el contador global.

Para cancelar un arriendo v2 con entrega agendada, `POST /v2/rentals/{rentalId}/cancel` cancela la entrega, reembolsa el cobro y libera el equipo, en ese orden. Si un paso no se confirma, el arriendo queda `CANCELLING`; repetir el mismo endpoint retoma el paso pendiente. Una vez entregado el equipo, se usa la devolución.

Tras persistir el resultado del callback de Webpay, Pagos solicita la conciliación a Arriendos mediante la API interna. Arriendos consulta el resultado directamente en Pagos. Si el aviso se pierde, el worker de Arriendos revisa por lotes las Sagas de creación pendientes con un ID de pago y un paso recuperable de pago o agendamiento. Docker Compose lo habilita cada 15 segundos por defecto con `SAGA_RECOVERY_INTERVAL_SECONDS` (`0` lo deshabilita). Los estados de reserva, reembolso o liberación inciertos siguen usando la conciliación manual.

## Servicio de Pagos y resiliencia

Los nueve experimentos reproducibles de éxito, compensación y falla en cascada
están agrupados en [experimentos/README.md](./experimentos/README.md). En
PowerShell se ejecutan desde la raíz con `.\experimentos\run.ps1`; generan una
salida con evidencia estructurada y usan bases SQLite temporales y un proveedor
mock.

El repositorio incluye el microservicio `pagos` con un contrato `pagos/open-api-pagos_transbank.yaml`

- `pagos/main.py` crea la aplicación; `pagos/routes/` separa las rutas de pagos, retorno Transbank, salud y control del mock.
- `pagos/models.py` contiene los modelos de solicitud y serializadores; `pagos/db_models.py` define las entidades persistentes y `pagos/storage.py` concentra las consultas y transacciones de base de datos.
- `pagos/database.py` configura SQLAlchemy; el servicio usa PostgreSQL en Docker y SQLite como opción local predeterminada.
- `pagos/resilience.py` concentra timeout, Circuit Breaker y Bulkhead.
- `pagos/providers/` contiene la interfaz/fábrica de proveedores, el mock y el adaptador de Transbank Webpay Plus.
- `POST /payments/charge` con `Idempotency-Key` y reuso seguro.
- `GET/POST /payments/transbank/return` para confirmar la transacción.
- `POST /payments/{payment_id}/refund` con compensación idempotente.
- `GET /payments/{payment_id}` para consulta.
- `GET /health` para salud del servicio.
- El proveedor se selecciona con `PAYMENTS_PROVIDER=mock` (predeterminado) o `PAYMENTS_PROVIDER=transbank`. El mock ofrece los modos `NORMAL`, `SLOW` y `DOWN`, modificables en caliente con `POST /payments/mock-provider/mode`, protegido por `X-Admin-Key`.
- Entregas permite inyectar fallas de agendamiento en pruebas. La función está deshabilitada por defecto (`DELIVERIES_ENABLE_FAILURE_INJECTION=false`); actívala en `.env` y recrea el servicio para habilitar el endpoint admin.
- El adaptador Transbank usa Webpay Plus en ambiente de integración. Para iniciar el flujo se configura `PAYMENTS_TRANSBANK_RETURN_URL` con una URL de retorno accesible para el navegador.
- Circuit Breaker: umbral 3 fallos, ventana abierta 15 s.
- Bulkhead: 5 operaciones simultáneas.
- Timeouts en cascada: Transbank usa 1 s (`PAYMENTS_EXTERNAL_TIMEOUT_SECONDS`), Pagos espera hasta 2 s (`PAYMENTS_PROVIDER_TIMEOUT_SECONDS`), Arriendos hasta 3 s (`PAYMENTS_TIMEOUT_SECONDS`) y KrakenD hasta 5 s (`api_gateway/krakend.json`).
- El Circuit Breaker de Arriendos protege las llamadas a Pagos; el estado `PAYMENT_PENDING_RETRY` queda guardado en la Saga y se consulta en `GET /v2/rentals/{rentalId}/saga`.
- El Bulkhead ejecuta como máximo cinco llamadas en un `ThreadPoolExecutor` acotado y conserva el cupo hasta que termina la tarea real, incluso si vence el timeout HTTP.
- Los tres servicios exponen `/metrics` en formato Prometheus con latencia HTTP (`service`, `endpoint`, `status_code`), estado del Circuit Breaker donde aplica y rechazos del Bulkhead en Pagos. Arriendos publica el contador de rechazos en cero porque no implementa Bulkhead.
- Los logs de aplicación son JSON y correlacionan `X-Request-ID` (generado si no llega del Gateway); el mismo identificador se propaga entre servicios.

Consultar la instrumentación desde el host usando el puerto de cada API dentro de su contenedor:

```bash
docker compose exec payments-api sh -c "python -c \"from urllib.request import urlopen; print(urlopen('http://localhost:8003/metrics').read().decode())\" | grep -E 'circuit_breaker_state|bulkhead_rejections_total|http_request_duration_seconds'"
docker compose exec arriendos-api sh -c "python -c \"from urllib.request import urlopen; print(urlopen('http://localhost:8080/metrics').read().decode())\" | grep -E 'circuit_breaker_state|bulkhead_rejections_total|http_request_duration_seconds'"
docker compose exec entregas-api sh -c "python -c \"from urllib.request import urlopen; print(urlopen('http://localhost:8081/metrics').read().decode())\" | grep 'http_request_duration_seconds'"
```

La respuesta se filtra para mostrar el estado del circuito, rechazos del Bulkhead y latencias (`http_request_duration_seconds_bucket`, `_count` y `_sum`).

Pagos persiste transacciones, reembolsos y claves de idempotencia en su propia base PostgreSQL (`payments-db`) y volumen (`payments_pg_data`). Los estados `UNKNOWN` se conservan para evitar que un reintento incierto inicie un segundo cobro o reembolso.

Para levantarlo junto con los otros servicios:

```bash
docker compose up --build
```

Solo KrakenD se expone al host en `http://localhost:8080`. Los endpoints
administrativos siguientes requieren un JWT de prueba con rol `admin`; `TOKEN`
debe estar definido en el shell.

Para generar un JWT de desarrollo compatible con el JWK local:

```bash
TOKEN=$(python3 - <<'PY'
import base64, hashlib, hmac, json

encode = lambda value: base64.urlsafe_b64encode(
    json.dumps(value, separators=(",", ":")).encode()
).rstrip(b"=").decode()
header = encode({"alg": "HS256", "typ": "JWT", "kid": "gateway-test"})
payload = encode({
    "iss": "servicio-pago-arriendo-test",
    "aud": "servicio-pago-arriendo",
    "sub": "admin-test",
    "roles": ["admin"],
})
unsigned = f"{header}.{payload}".encode()
secret = b"gateway-test-secret-change-me-32-bytes"
signature = base64.urlsafe_b64encode(
    hmac.new(secret, unsigned, hashlib.sha256).digest()
).rstrip(b"=").decode()
print(f"{header}.{payload}.{signature}")
PY
)
```

Habilitar la inyección de fallas de Entregas, en `.env`:

```env
DELIVERIES_ENABLE_FAILURE_INJECTION=true
```

Recrear Entregas para aplicar la variable:

```bash
docker compose up -d --force-recreate entregas-api
```

Activar `FAIL_NEXT` (falla una creación y se restablece automáticamente):

```bash
curl -i --oauth2-bearer "$TOKEN" \
  -H "API-Key: apikey_admin" \
  -H "Content-Type: application/json" \
  -d '{"mode":"FAIL_NEXT"}' \
  http://localhost:8080/api/admin/failure-injection
```

Activar `ALWAYS_FAIL` o volver al modo `NORMAL`:

```bash
curl -i --oauth2-bearer "$TOKEN" \
  -H "API-Key: apikey_admin" \
  -H "Content-Type: application/json" \
  -d '{"mode":"ALWAYS_FAIL"}' \
  http://localhost:8080/api/admin/failure-injection

curl -i --oauth2-bearer "$TOKEN" \
  -H "API-Key: apikey_admin" \
  -H "Content-Type: application/json" \
  -d '{"mode":"NORMAL"}' \
  http://localhost:8080/api/admin/failure-injection
```

Cambiar el proveedor mock de Pagos requiere `X-Admin-Key`:

```bash
curl -i --oauth2-bearer "$TOKEN" \
  -H "X-Admin-Key: apikey_admin" \
  -H "Content-Type: application/json" \
  -d '{"mode":"SLOW"}' \
  http://localhost:8080/api/admin/payments/mock-provider/mode

curl -i --oauth2-bearer "$TOKEN" \
  -H "X-Admin-Key: apikey_admin" \
  -H "Content-Type: application/json" \
  -d '{"mode":"DOWN"}' \
  http://localhost:8080/api/admin/payments/mock-provider/mode

curl -i --oauth2-bearer "$TOKEN" \
  -H "X-Admin-Key: apikey_admin" \
  -H "Content-Type: application/json" \
  -d '{"mode":"NORMAL"}' \
  http://localhost:8080/api/admin/payments/mock-provider/mode
```

# Declaración de uso de inteligencia artificial

El proyecto se desarrolló siguiendo la metodología *Spec-Driven Development* (SDD), en la cual la especificación es el artefacto central del desarrollo y la implementación se deriva de ella. En este flujo se utilizó **Claude Code** como asistente de generación de código, bajo el siguiente esquema de trabajo:

- **Especificación (autoría del equipo):** el equipo definió los requisitos, el contrato de la API (endpoints, modelos y códigos de error), la frontera entre los servicios de Arriendos y Equipos, y las decisiones de arquitectura documentadas en los ADR.
- **Generación (asistida por IA):** a partir de dichas especificaciones, la herramienta generó el código de implementación de los parte de los servicios (routers FastAPI, cliente gRPC, modelos de base de datos y el esquema de autenticación por doble API key), además de borradores de apoyo para la documentación (ADR e interpretación de resultados).
- **Verificación y ajuste (autoría del equipo):** el equipo revisó el código generado contra la especificación, probó el comportamiento de los endpoints y adaptó la implementación y los textos cuando no cumplían lo especificado o los comentarios recibidos en la revisión.

Las decisiones de diseño y arquitectura son del equipo; la IA se utilizó como herramienta de implementación y no como fuente de dichas decisiones. El equipo se hace responsable del contenido y del funcionamiento del sistema entregado.
