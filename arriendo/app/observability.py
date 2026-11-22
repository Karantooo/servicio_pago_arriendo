import contextvars
import json
import logging
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SERVICE_NAME = "arriendos-api"
REQUEST_ID_HEADER = "X-Request-ID"
request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arriendos_request_id",
    default="",
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("service", "endpoint", "status_code"),
)
bulkhead_rejections_total = Counter(
    "bulkhead_rejections_total",
    "Requests rejected by a service bulkhead; Arriendos currently has none configured.",
    labelnames=("service",),
)
circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Current payments-api circuit breaker state.",
    labelnames=("service", "state"),
)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        details = getattr(record, "details", None)
        if details is None:
            details = record.getMessage()
        if record.exc_info:
            details = {
                "message": details,
                "exception": self.formatException(record.exc_info),
            }
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "service": SERVICE_NAME,
                "request_id": request_id_context.get(),
                "event": getattr(record, "event", record.name),
                "duration_ms": getattr(record, "duration_ms", 0.0),
                "details": details,
            },
            ensure_ascii=False,
            default=str,
        )


def configure_json_logging() -> None:
    root = logging.getLogger()
    root.setLevel(min(root.level or logging.INFO, logging.INFO))
    for handler in root.handlers:
        if getattr(handler, "_service_json_formatter", False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler._service_json_formatter = True
    root.addHandler(handler)


def set_circuit_breaker_state(state: str) -> None:
    for candidate in ("CLOSED", "OPEN", "HALF_OPEN"):
        circuit_breaker_state.labels(
            service=SERVICE_NAME,
            state=candidate,
        ).set(1 if candidate == state else 0)


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied_request_id = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name.lower() == REQUEST_ID_HEADER.lower().encode("ascii")
            ),
            "",
        )
        request_id = (
            supplied_request_id
            if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied_request_id)
            else str(uuid4())
        )
        token = request_id_context.set(request_id)
        started = time.perf_counter()
        status_code = 500
        recorded = False

        def record_request() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            duration = time.perf_counter() - started
            route = scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            endpoint = f"{scope['method']} {route_path}"
            if route_path != "/metrics":
                http_request_duration_seconds.labels(
                    service=SERVICE_NAME,
                    endpoint=endpoint,
                    status_code=str(status_code),
                ).observe(duration)
            logging.getLogger(__name__).info(
                "HTTP request completed",
                extra={
                    "event": "http_request_completed",
                    "duration_ms": round(duration * 1000, 3),
                    "details": {
                        "method": scope["method"],
                        "endpoint": endpoint,
                        "status_code": status_code,
                    },
                },
            )

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != REQUEST_ID_HEADER.lower().encode("ascii")
                ]
                headers.append(
                    (
                        REQUEST_ID_HEADER.lower().encode("ascii"),
                        request_id.encode("ascii"),
                    )
                )
                message["headers"] = headers
            await send(message)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                record_request()

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            record_request()
            request_id_context.reset(token)


def install_observability(app: FastAPI) -> None:
    configure_json_logging()
    set_circuit_breaker_state("CLOSED")
    bulkhead_rejections_total.labels(service=SERVICE_NAME).inc(0)

    def metrics() -> Response:
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    app.add_api_route("/metrics", metrics, methods=["GET"], include_in_schema=False)
    app.add_middleware(MetricsMiddleware)
