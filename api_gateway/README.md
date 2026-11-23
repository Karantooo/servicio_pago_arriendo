# API Gateway y BFF

KrakenD forma parte del Compose raíz y es el único servicio HTTP publicado al
host. Los backends usan DNS interno de Docker (`arriendos-api`,
`entregas-api` y `payments-api`).

## Ejecución

Desde la raíz del repositorio:

```bash
docker compose up --build
```

### Gateway aislado con backends mock

Para probar solo KrakenD y sus ejemplos de JWT, agregación y circuit breaker,
se conserva un Compose opcional:

```bash
docker compose -f api_gateway/docker-compose.gateway.yml up --build
```

Este entorno usa [krakend.mock.json](./krakend.mock.json) y los backends mock
locales; no enruta a los servicios del dominio. Para apagarlo:

```bash
docker compose -f api_gateway/docker-compose.gateway.yml down
```

Ambos entornos publican por defecto el puerto `8080`, por lo que no deben
ejecutarse simultáneamente con ese mismo puerto. Se puede cambiar el puerto
publicado del Compose opcional con `KRAKEND_PORT=8082`.

Las rutas de dominio requieren JWT HS256 y la `API-Key` de cliente/admin que
usan los servicios existentes. KrakenD valida el JWT y reenvía `API-Key` al
backend; no devuelve ese header ni las credenciales en la respuesta. El endpoint
`/api/rental-summary/{rentalId}` consulta en paralelo Arriendos y Entregas y
agrupa las respuestas como `rental` y `delivery`.
El estado de recuperación del pago está disponible en
`GET /api/rentals/{rentalId}/saga`, también protegido por JWT y API-Key.

La clave de firma incluida en `jwk-symmetric.json` es solo para desarrollo. Debe
reemplazarse antes de cualquier despliegue compartido o de producción.

## Prueba local

Generar un JWT de prueba con Python:

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
    "sub": "test-client",
    "roles": ["cliente"],
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

Con JWT y la API key readonly local (`CLIENT_API_KEY` del `.env`):

```bash
curl -i \
  --oauth2-bearer "$TOKEN" \
  -H "API-Key: apikey_cliente" \
  http://localhost:8080/api/rental-summary/1
```

Sin JWT, KrakenD debe responder `401` antes de llamar a los servicios:

```bash
curl -i -H "API-Key: apikey_cliente" \
  http://localhost:8080/api/rentals/1
```

El límite de las rutas protegidas es de 10 solicitudes por segundo por IP
(además del límite global definido en `krakend.json`).

## Pruebas de fallas del gateway

Las pruebas usan únicamente el Compose mock, un JWT de desarrollo y el puerto
8082; no cargan `.env` ni llaman a los servicios reales. Desde la raíz del
repositorio, inicia un entorno limpio:

```powershell
$env:KRAKEND_PORT = "8082"
docker compose -p gateway-failure-tests -f api_gateway/docker-compose.gateway.yml down --remove-orphans
docker compose -p gateway-failure-tests -f api_gateway/docker-compose.gateway.yml up -d --build
python -m unittest discover -s api_gateway/tests -p "test_gateway_*.py" -v
docker compose -p gateway-failure-tests -f api_gateway/docker-compose.gateway.yml down --remove-orphans
```

Los casos verifican un backend que responde 503 y la apertura del circuit breaker,
el timeout frente a un backend lento y el rate limit ante un pico de
solicitudes. Con `max_errors: 3`, el circuito abre tras cuatro respuestas de
fallo; por ello, ejecuta la suite
contra un Compose recién iniciado.
