# API Gateway de prueba

La configuración usa KrakenD Community como única entrada HTTP. Los backends
mock son servicios internos de Docker y no publican puertos al host; se pueden
reemplazar por los servicios reales cuando sus contratos estén definidos.

## Capacidades demostradas

- `/api/profile` y `/api/dashboard` validan un JWT HS256 antes de enrutar.
- `/api/dashboard` agrega en paralelo `profile` y `rentals` en una respuesta
  adaptada al cliente.
- Los endpoints protegidos limitan a 10 solicitudes por segundo por IP y a 100
  solicitudes por segundo globales.
- Cada backend tiene un límite de salida y un circuit breaker.
- `/api/rentals-unstable` responde con un backend que falla para observar la
  apertura del circuit breaker en los logs de KrakenD.
- `/health` permite comprobar el gateway sin autenticación.

La clave de prueba está únicamente en `jwk-symmetric.json`. Debe cambiarse
antes de cualquier despliegue real y no debe reutilizarse en producción.

## Ejecución

Desde `api_gateway/`:

```bash
docker compose -f docker-compose.gateway.yml up --build
```

Crear un token de prueba (requiere Python, sin dependencias externas):

```bash
python3 - <<'PY'
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
```

Usar el token impreso:

```bash
curl http://localhost:8080/health
curl -H "Authorization: Bearer <TOKEN>" http://localhost:8080/api/profile
curl -H "Authorization: Bearer <TOKEN>" http://localhost:8080/api/dashboard
```

Una solicitud protegida sin token responde `401`. El límite por cliente se
puede observar enviando más de 10 solicitudes por segundo desde la misma IP;
KrakenD responderá `429`.

Para observar el circuit breaker:

```bash
for i in 1 2 3 4 5; do
  curl -i -H "Authorization: Bearer <TOKEN>" \
    http://localhost:8080/api/rentals-unstable
done
```
