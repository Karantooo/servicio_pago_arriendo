# Experimentos de Saga y resiliencia

La suite organiza **nueve demostraciones: tres por tipo de escenario**. Cada
prueba emite líneas `TRACE_JSON` por interacción, `SAGA_EVENT_JSON` por evento
persistido y `APP_LOG_JSON` por log de resiliencia, además de una línea
`EVIDENCE_JSON` con estado antes/después, cronología completa, métricas y
resultados. La ejecución completa guarda la salida en
`experimentos/evidencia/`. `run.ps1` también ejecuta
`formatear_log.py` automáticamente y genera, junto al log, un reporte
`*.legible.txt`.

## Ejecución en Windows

Desde la raíz del repositorio, en PowerShell:

```powershell
.\experimentos\run.ps1
```

Para convertir un log existente manualmente:

```powershell
python .\experimentos\formatear_log.py .\experimentos\evidencia\experimentos-<fecha>.log
python .\experimentos\formatear_log.py .\experimentos\evidencia\experimentos-<fecha>.log --md
```

El formato predeterminado resume las trazas; agrega `--full` para incluir el
JSON completo de cada paso.

Para repetir solo una categoría:

```powershell
python -m pytest -s -v experimentos\exito
python -m pytest -s -v experimentos\compensacion
python -m pytest -s -v experimentos\cascada
```

Requiere Python con las dependencias de desarrollo de Arriendos, Pagos y
`grpcio-tools`. No requiere Docker, `.env`, credenciales reales, Transbank,
PostgreSQL ni servicios desplegados. La suite crea SQLite temporal, genera los
stubs gRPC en una carpeta temporal y los elimina al finalizar. Los límites de
proveedor se reducen dentro de las pruebas para que sean demostrables en pocos
segundos; no son valores recomendados de producción.

## Escenarios y evidencia

### `exito/` — camino feliz

1. `test_01_flujo_completo.py`: reserva, pago confirmado, entrega agendada y
   notificación de entrega; verifica Saga `COMPLETED/DONE`.
2. `test_02_pago_confirmado_por_reconciliacion.py`: comienza esperando el pago,
   concilia el cobro confirmado y cierra la Saga al entregar.
3. `test_03_reintento_idempotente.py`: reproduce solicitudes y conciliaciones;
   demuestra que no duplica reserva, pago, arriendo ni entrega.

**Qué muestra:** los eventos de reserva, pago y agendamiento quedan registrados
y el callback final deja el arriendo `ACTIVE` y la Saga `COMPLETED`. Sin la
idempotencia, los reintentos podrían repetir efectos externos o crear registros
duplicados.

**Cómo leer la evidencia:** cada `TRACE_JSON` muestra una interacción;
`SAGA_EVENT_JSON` muestra un evento de Saga, ordenado por secuencia. `timeline`
numera cada interacción e incluye la
solicitud y respuesta HTTP (método, ruta, cuerpo y código), o la solicitud y el
resultado de cada paso simulado. `saga_events` muestra, en orden, los eventos
persistidos por la Saga y su detalle.

### `compensacion/` — falla del último paso

1. `test_01_falla_entrega_reembolso_liberacion.py`:
   falla el agendamiento final y verifica compensación inversa, primero
   reembolso y luego liberación de Equipos.
2. `test_02_reintento_liberacion.py`: simula que falla la liberación después
   del reembolso, demuestra el estado explícito `COMPENSATION_FAILED` y que la
   conciliación reintenta solo la liberación, sin duplicar el reembolso.
3. `test_03_reembolso_incierto_conciliado.py`: la respuesta del reembolso es
   incierta; mantiene la reserva hasta confirmar el reembolso y luego completa
   la liberación.

**Qué muestra:** no queda un cobro confirmado sin operación ni se libera una
reserva antes de saber que el reembolso ocurrió. Si la compensación no se puede
confirmar, la Saga queda marcada para conciliación en vez de aparentar éxito.

### `cascada/` — lentitud, caída y pico de carga en Pagos

1. `test_01_pasarela_lenta_circuit_breaker.py`: tres timeouts simulados abren el
   Circuit Breaker; la siguiente solicitud recibe fallback
   `PAYMENT_PENDING_RETRY` rápido y `/health` sigue respondiendo.
2. `test_02_pasarela_caida_fallback.py`: la pasarela simulada cae, abre el
   circuito y las solicitudes siguientes hacen fail-fast.
3. `test_03_bulkhead_pico_lento.py`: un pico de cuatro cobros supera un
   Bulkhead de dos cupos; registra rechazos `429`, timeouts `504` y comprueba
   que `/health` sigue disponible.

**Qué muestra:** cada prueba presenta el circuito y el contador de rechazos del
Bulkhead antes/después, los tiempos, los estados HTTP y eventos de log como
`circuit_breaker_state_changed`, `payment_provider_timeout` y
`bulkhead_rejected` (también emitido como `APP_LOG_JSON`). Sin Circuit Breaker,
cada petición nueva esperaría a la
pasarela lenta; sin Bulkhead, esas esperas ocuparían recursos de trabajo hasta
agotarlos y perjudicarían las rutas sanas.

En el pico, cada entrada `solicitud_cobro` de `timeline` identifica el
`Idempotency-Key`, el arriendo, la respuesta completa y la latencia. El campo
`outcome` distingue `rechazada_por_bulkhead` (HTTP 429) de
`admitida_por_bulkhead` (entró al proveedor y terminó en timeout HTTP 504).
`log_events` conserva los detalles registrados por el Bulkhead.

## Alcance de la demostración

Arriendos ejecuta sus endpoints y persistencia de Saga con SQLite efímero;
Equipos, Entregas y los resultados de pago de la Saga se sustituyen por dobles
deterministas para poder provocar y verificar cada transición. Los escenarios
de cascada sí recorren las rutas, persistencia, métricas y resiliencia reales
del servicio Pagos en proceso, con su proveedor mock. Por tanto, es evidencia
reproducible del comportamiento de la aplicación, no una prueba de carga ni una
certificación de una pasarela externa en producción.
