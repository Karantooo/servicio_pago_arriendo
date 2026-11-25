import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4


RENTAL_POST_INTERVAL_SECONDS = 0.25
_last_rental_post_at = 0.0


def timed_request(api, trace, action, method, path, **kwargs):
    global _last_rental_post_at
    normalized_path = path.split("?", 1)[0]
    if method.upper() == "POST" and normalized_path == "/api/rentals":
        wait_seconds = (
            RENTAL_POST_INTERVAL_SECONDS
            - (time.perf_counter() - _last_rental_post_at)
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    started = time.perf_counter()
    if method.upper() == "POST" and normalized_path == "/api/rentals":
        _last_rental_post_at = started
    response = api.request(method, path, **kwargs)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    trace.append(
        {
            "action": action,
            "method": method,
            "path": normalized_path,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
        }
    )
    return response


def create_rental(api, trace, iteration, case_offset, *, idempotency_key=None):
    unique = uuid4().hex
    client_body = {
        "name": f"Experimento integral {unique[:12]}",
        "email": f"{unique}@experimentos.example",
    }
    created_client = timed_request(
        api, trace, "crear cliente", "POST", "/api/clients", json=client_body
    )
    assert created_client.status_code == 201, (
        f"No se pudo crear el cliente: HTTP {created_client.status_code}"
    )
    client_id = created_client.json()["id"]

    run_offset_days = int(unique[:8], 16) % 100000
    start = datetime.now(timezone.utc) + timedelta(
        days=60 + case_offset * 120 + iteration * 2 + run_offset_days
    )
    end = start + timedelta(days=1)
    rental_body = {
        "clientId": client_id,
        "equipmentId": 1,
        "startDate": start.isoformat().replace("+00:00", "Z"),
        "endDate": end.isoformat().replace("+00:00", "Z"),
    }
    key = idempotency_key or f"e2e-{case_offset}-{iteration}-{unique}"
    response = timed_request(
        api,
        trace,
        "crear arriendo e iniciar pago",
        "POST",
        "/api/rentals",
        headers={"Idempotency-Key": key},
        json=rental_body,
    )
    assert response.status_code in {201, 202}, (
        f"No se pudo iniciar el arriendo/pago: HTTP {response.status_code}; "
        f"detalle: {response.text[:500]}"
    )
    body = response.json()
    payment = body.get("payment", {})
    assert payment.get("paymentId") is not None, "Pagos no devolvió paymentId."
    assert payment.get("status") == "PENDING", (
        f"Se esperaba pago PENDING, recibido: {payment.get('status')}"
    )
    token = payment.get("token")
    assert token, "El proveedor de pagos no devolvió el token del pago."

    return {
        "rental_id": body["rental"]["id"],
        "client_id": client_id,
        "payment_id": payment["paymentId"],
        "payment_token": token,
        "idempotency_key": key,
        "rental_body": rental_body,
    }


def confirm_mock_payment(api, trace, payment_token):
    response = timed_request(
        api,
        trace,
        "confirmar pago con proveedor mock",
        "GET",
        "/payments/transbank/return",
        params={"token_ws": payment_token},
    )
    assert response.status_code == 200, (
        f"No se pudo confirmar el pago de prueba: HTTP {response.status_code}"
    )
    assert response.json().get("status") == "CHARGED", (
        f"El callback no confirmó el pago: {response.json().get('status')}"
    )
    return response.json()


def wait_for_payment(api, trace, payment_id, expected_status, timeout_seconds=90):
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        response = timed_request(
            api,
            trace,
            "consultar pago",
            "GET",
            f"/api/payments/{payment_id}",
        )
        assert response.status_code == 200, (
            f"No se pudo consultar el pago: HTTP {response.status_code}"
        )
        last_status = response.json().get("status")
        if last_status == expected_status:
            return response.json()
        time.sleep(1)
    raise AssertionError(
        f"Pago {payment_id} no alcanzó {expected_status}; último estado: {last_status}."
    )


def wait_for_saga(api, trace, rental_id, predicate, timeout_seconds=90):
    deadline = time.monotonic() + timeout_seconds
    last_saga = None
    while time.monotonic() < deadline:
        response = timed_request(
            api,
            trace,
            "consultar saga",
            "GET",
            f"/api/rentals/{rental_id}/saga",
        )
        assert response.status_code == 200, (
            f"No se pudo consultar la Saga: HTTP {response.status_code}"
        )
        last_saga = response.json()
        if predicate(last_saga):
            return last_saga
        time.sleep(1)
    raise AssertionError(
        "La Saga no alcanzó el estado esperado; "
        + json.dumps(
            {
                "id": last_saga.get("id") if last_saga else None,
                "operation": last_saga.get("operation") if last_saga else None,
                "status": last_saga.get("status") if last_saga else None,
                "currentStep": last_saga.get("currentStep") if last_saga else None,
                "paymentStatus": last_saga.get("paymentStatus") if last_saga else None,
                "refundStatus": last_saga.get("refundStatus") if last_saga else None,
            }
        )
    )


def print_evidence(name, iteration, trace, **result):
    for step in trace:
        print(
            "TRACE_JSON "
            + json.dumps(
                {"experiment": name, "iteration": iteration, **step},
                sort_keys=True,
            )
        )
    print(
        "EVIDENCE_JSON "
        + json.dumps(
            {
                "experiment": name,
                "iteration": iteration,
                "timeline": trace,
                "result": result,
            },
            sort_keys=True,
        )
    )
