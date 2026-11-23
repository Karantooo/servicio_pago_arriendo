import time


ADMIN_HEADERS = {"X-Admin-Key": "experiment-admin-key"}
STATES = ("CLOSED", "OPEN", "HALF_OPEN")


def metric_value(metrics, name, **labels):
    label_text = ",".join(
        f'{key}="{value}"' for key, value in sorted(labels.items())
    )
    target = f"{name}{{{label_text}}}"
    for line in metrics.splitlines():
        if line.startswith(target + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def metrics_snapshot(client):
    metrics = client.get("/metrics").text
    return {
        "circuit_breaker": {
            state: metric_value(
                metrics,
                "circuit_breaker_state",
                service="payments-api",
                state=state,
            )
            for state in STATES
        },
        "bulkhead_rejections": metric_value(
            metrics,
            "bulkhead_rejections_total",
            service="payments-api",
        ),
    }


def relevant_log_events(caplog, names):
    return [
        {
            "event": getattr(record, "event", ""),
            "level": record.levelname,
            "details": getattr(record, "details", None),
        }
        for record in caplog.records
        if getattr(record, "event", "") in names
    ]


def set_mode(client, mode, *, trace=None):
    request = {"mode": mode}
    response = client.post(
        "/payments/mock-provider/mode",
        headers=ADMIN_HEADERS,
        json=request,
    )
    assert response.status_code == 200, response.text
    if trace is not None:
        trace.http(
            "configurar_modo_proveedor",
            "POST",
            "/payments/mock-provider/mode",
            request,
            response,
        )


def charge(client, key, *, rental_id=1101, trace=None):
    request = {
        "rental_id": rental_id,
        "customer_id": 31,
        "amount_rent": 50000,
        "amount_deposit": 10000,
    }
    started = time.perf_counter()
    response = client.post(
        "/payments/charge",
        headers={"Idempotency-Key": key},
        json=request,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if trace is not None:
        outcome = (
            "rechazada_por_bulkhead"
            if response.status_code == 429
            else "admitida_por_bulkhead"
            if response.status_code == 504
            else "respondida"
        )
        trace.add(
            "solicitud_cobro",
            outcome=outcome,
            request={
                "method": "POST",
                "path": "/payments/charge",
                "headers": {"Idempotency-Key": key},
                "body": request,
            },
            response={
                "status_code": response.status_code,
                "body": response.json(),
                "elapsed_ms": elapsed_ms,
            },
        )
    return response
