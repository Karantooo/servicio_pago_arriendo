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

Los estados `IN_DOUBT` y `COMPENSATION_FAILED` requieren revisión: la llamada remota pudo haber tenido éxito aunque la respuesta o la compensación no se hayan confirmado. La interfaz gRPC actual no recibe una clave idempotente para cada operación; por eso, esta implementación registra y expone esas situaciones, pero no reintenta automáticamente operaciones ambiguas.

## Servicio de Pagos y resiliencia

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
- El proveedor se selecciona con `PAYMENTS_PROVIDER=mock` (predeterminado) o `PAYMENTS_PROVIDER=transbank`. El mock ofrece los modos `NORMAL`, `SLOW` y `DOWN`, modificables en caliente con `POST /payments/mock-provider/mode`.
- El adaptador Transbank usa Webpay Plus en ambiente de integración. Para iniciar el flujo se configura `PAYMENTS_TRANSBANK_RETURN_URL` con una URL de retorno accesible para el navegador.
- Circuit Breaker: umbral 3 fallos, ventana abierta 15 s.
- Bulkhead: 5 operaciones simultáneas.
- Timeout de 5 s entre Pagos y el proveedor (`PAYMENTS_PROVIDER_TIMEOUT_SECONDS`); Arriendos usa `PAYMENTS_TIMEOUT_SECONDS` para sus llamadas al servicio de Pagos.

Pagos persiste transacciones, reembolsos y claves de idempotencia en su propia base PostgreSQL (`payments-db`) y volumen (`payments_pg_data`). Los estados `UNKNOWN` se conservan para evitar que un reintento incierto inicie un segundo cobro o reembolso.

Para levantarlo junto con los otros servicios:

```bash
docker compose -f docker-compose.yaml up --build
```

La API de pagos queda expuesta en `http://localhost:8003`.

# Declaración de uso de inteligencia artificial

El proyecto se desarrolló siguiendo la metodología *Spec-Driven Development* (SDD), en la cual la especificación es el artefacto central del desarrollo y la implementación se deriva de ella. En este flujo se utilizó **Claude Code** como asistente de generación de código, bajo el siguiente esquema de trabajo:

- **Especificación (autoría del equipo):** el equipo definió los requisitos, el contrato de la API (endpoints, modelos y códigos de error), la frontera entre los servicios de Arriendos y Equipos, y las decisiones de arquitectura documentadas en los ADR.
- **Generación (asistida por IA):** a partir de dichas especificaciones, la herramienta generó el código de implementación de los parte de los servicios (routers FastAPI, cliente gRPC, modelos de base de datos y el esquema de autenticación por doble API key), además de borradores de apoyo para la documentación (ADR e interpretación de resultados).
- **Verificación y ajuste (autoría del equipo):** el equipo revisó el código generado contra la especificación, probó el comportamiento de los endpoints y adaptó la implementación y los textos cuando no cumplían lo especificado o los comentarios recibidos en la revisión.

Las decisiones de diseño y arquitectura son del equipo; la IA se utilizó como herramienta de implementación y no como fuente de dichas decisiones. El equipo se hace responsable del contenido y del funcionamiento del sistema entregado.
