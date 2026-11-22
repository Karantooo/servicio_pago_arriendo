from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, status


ADMIN_HEADERS = {"API-Key": "test-admin-key"}
READONLY_HEADERS = {"API-Key": "test-readonly-key"}


def _payload(**overrides):
    payload = {
        "rentalId": 10,
        "clientId": 20,
        "equipmentId": 30,
        "scheduledAt": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "paymentStatus": "PAID",
        "paymentId": 42,
    }
    return {**payload, **overrides}


def _mock_rental(**overrides):
    return {
        "id": 10,
        "clientId": 20,
        "equipmentId": 30,
        "status": "PENDING",
        "startDate": datetime.now(timezone.utc).isoformat(),
        "endDate": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        **overrides,
    }


def _mock_get_rental(monkeypatch, rental=None):
    async def get_rental(self, rental_id):
        return rental or _mock_rental()

    async def get_saga(self, rental_id):
        return {
            "operation": "CREATE",
            "status": "IN_PROGRESS",
            "currentStep": "PAYMENT_CONFIRMED",
            "paymentStatus": "CHARGED",
            "paymentId": 42,
        }

    monkeypatch.setattr("entregas_api.rentals_client.RentalsClient.get_rental", get_rental)
    monkeypatch.setattr("entregas_api.rentals_client.RentalsClient.get_saga", get_saga)


def _create(client, key="delivery-key", payload=None):
    return client.post(
        "/v1/deliveries",
        headers={**ADMIN_HEADERS, "Idempotency-Key": key},
        json=payload or _payload(),
    )


def test_health_and_role_based_auth(client, monkeypatch):
    _mock_get_rental(monkeypatch)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/v1/deliveries").status_code == 401
    assert client.get("/v1/deliveries", headers=READONLY_HEADERS).status_code == 200
    assert _create(client, payload=_payload()).status_code == 201
    denied = client.post(
        "/v1/deliveries",
        headers={**READONLY_HEADERS, "Idempotency-Key": "readonly"},
        json=_payload(),
    )
    assert denied.status_code == 403


def test_metrics_endpoint_exports_request_latency_and_propagates_request_id(client):
    response = client.get(
        "/health",
        headers={"X-Request-ID": "entregas-load-29"},
    )
    metrics = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "entregas-load-29"
    assert metrics.status_code == 200
    assert 'http_request_duration_seconds_count{endpoint="GET /health",service="entregas-api",status_code="200"}' in metrics.text


def test_failure_injection_is_disabled_by_default(client, monkeypatch):
    response = client.post(
        "/admin/failure-injection",
        headers=ADMIN_HEADERS,
        json={"mode": "ALWAYS_FAIL"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "FAILURE_INJECTION_DISABLED"


def test_failure_injection_requires_admin_key(client, monkeypatch):
    monkeypatch.setenv("DELIVERIES_ENABLE_FAILURE_INJECTION", "true")
    missing_key = client.post(
        "/admin/failure-injection",
        json={"mode": "FAIL_NEXT"},
    )
    denied = client.post(
        "/admin/failure-injection",
        headers=READONLY_HEADERS,
        json={"mode": "FAIL_NEXT"},
    )
    assert missing_key.status_code == 401
    assert denied.status_code == 403


def test_failure_injection_rejects_unknown_mode(client, monkeypatch):
    monkeypatch.setenv("DELIVERIES_ENABLE_FAILURE_INJECTION", "true")
    response = client.post(
        "/admin/failure-injection",
        headers=ADMIN_HEADERS,
        json={"mode": "SOMETIMES_FAIL"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FAILURE_MODE"


def test_fail_next_returns_503_then_resets_to_normal(client, monkeypatch, caplog):
    monkeypatch.setenv("DELIVERIES_ENABLE_FAILURE_INJECTION", "true")
    _mock_get_rental(monkeypatch)
    configured = client.post(
        "/admin/failure-injection",
        headers=ADMIN_HEADERS,
        json={"mode": "FAIL_NEXT"},
    )
    assert configured.status_code == 200
    assert configured.json()["mode"] == "FAIL_NEXT"

    failed = _create(client, key="fail-once")
    assert failed.status_code == 503
    assert failed.json() == {"detail": "Simulated delivery failure for testing"}
    assert "Simulated delivery failure injected" in caplog.text

    created = _create(client, key="succeeds-after-failure")
    assert created.status_code == 201
    assert created.json()["status"] == "SCHEDULED"


def test_always_fail_can_be_disabled_with_normal(client, monkeypatch):
    monkeypatch.setenv("DELIVERIES_ENABLE_FAILURE_INJECTION", "true")
    _mock_get_rental(monkeypatch)
    configured = client.post(
        "/admin/failure-injection",
        headers=ADMIN_HEADERS,
        json={"mode": "ALWAYS_FAIL"},
    )
    assert configured.status_code == 200
    assert _create(client, key="always-fails-1").status_code == 503
    assert _create(client, key="always-fails-2").status_code == 503

    reset = client.post(
        "/admin/failure-injection",
        headers=ADMIN_HEADERS,
        json={"mode": "NORMAL"},
    )
    assert reset.status_code == 200
    assert _create(client, key="normal-again").status_code == 201


def test_create_checks_rental_and_is_idempotent(client, monkeypatch):
    _mock_get_rental(monkeypatch)
    original_request = _payload()
    first = _create(client, payload=original_request)
    assert first.status_code == 201
    assert first.json()["status"] == "SCHEDULED"

    replay = _create(client, payload=original_request)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    changed = _create(
        client,
        payload={**original_request, "notes": "otra solicitud"},
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_create_rejects_mismatched_rental_and_duplicate_rental(client, monkeypatch):
    _mock_get_rental(monkeypatch)
    mismatch = _create(
        client,
        key="mismatch",
        payload=_payload(clientId=999),
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "RENTAL_DATA_MISMATCH"

    assert _create(client, key="first").status_code == 201
    duplicate = _create(client, key="second")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "DELIVERY_ALREADY_EXISTS"


def test_paid_delivery_requires_ready_creation_saga(client, monkeypatch):
    _mock_get_rental(monkeypatch)

    async def get_saga(self, rental_id):
        return {
            "operation": "CREATE",
            "status": "IN_PROGRESS",
            "currentStep": "WAITING_PAYMENT",
        }

    monkeypatch.setattr("entregas_api.rentals_client.RentalsClient.get_saga", get_saga)
    response = _create(
        client,
        payload=_payload(paymentStatus="PAID", paymentId=42),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "RENTAL_NOT_READY_FOR_DELIVERY"


@pytest.mark.parametrize(("status", "step"), [
    ("IN_PROGRESS", "WAITING_FOR_DELIVERY"),
    ("IN_DOUBT", "DELIVERY_SCHEDULING_IN_DOUBT"),
])
def test_paid_delivery_accepts_pending_scheduling(client, monkeypatch, status, step):
    _mock_get_rental(monkeypatch)

    async def get_saga(self, rental_id):
        return {
            "operation": "CREATE",
            "status": status,
            "currentStep": step,
            "paymentStatus": "CHARGED",
            "paymentId": 42,
        }

    monkeypatch.setattr("entregas_api.rentals_client.RentalsClient.get_saga", get_saga)
    response = _create(
        client,
        payload=_payload(paymentStatus="PAID", paymentId=42),
    )
    assert response.status_code == 201
    assert response.json()["paymentId"] == 42


@pytest.mark.parametrize("override", [
    {"paymentStatus": "UNPAID"},
    {"paymentId": None},
    {"paymentId": 999},
])
def test_create_rejects_missing_or_mismatched_payment(client, monkeypatch, override):
    _mock_get_rental(monkeypatch)
    response = _create(client, payload=_payload(**override))
    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_REQUIRED"
    assert client.get("/v1/rentals/10/delivery", headers=READONLY_HEADERS).status_code == 404


def test_deliver_notifies_rentals(client, monkeypatch):
    _mock_get_rental(monkeypatch)
    created = _create(client)
    assert created.status_code == 201
    delivery_id = created.json()["id"]
    calls = []

    async def notify(self, rental_id, payload):
        calls.append((rental_id, payload))

    monkeypatch.setattr(
        "entregas_api.rentals_client.RentalsClient.notify_delivery_status", notify
    )
    delivered = client.post(
        f"/v1/deliveries/{delivery_id}/deliver",
        headers=ADMIN_HEADERS,
        json={"receivedBy": "Ana"},
    )
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "DELIVERED"
    assert delivered.json()["rentalSyncStatus"] == "SYNCED"
    assert delivered.json()["receivedBy"] == "Ana"
    assert calls[0][0] == 10
    assert calls[0][1]["status"] == "DELIVERED"

    events = client.get(
        f"/v1/deliveries/{delivery_id}/events",
        headers=READONLY_HEADERS,
    )
    assert [event["type"] for event in events.json()] == [
        "SCHEDULED",
        "DELIVERED",
    ]


def test_failed_rental_sync_can_be_reconciled(client, monkeypatch):
    _mock_get_rental(monkeypatch)
    delivery_id = _create(client).json()["id"]
    async def fail_notification(self, rental_id, payload):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "RENTALS_SERVICE_UNAVAILABLE",
                "message": "offline",
            },
        )

    monkeypatch.setattr(
        "entregas_api.rentals_client.RentalsClient.notify_delivery_status",
        fail_notification,
    )
    failed = client.post(
        f"/v1/deliveries/{delivery_id}/deliver",
        headers=ADMIN_HEADERS,
    )
    assert failed.status_code == 503
    assert client.get(
        f"/v1/deliveries/{delivery_id}", headers=READONLY_HEADERS
    ).json()["rentalSyncStatus"] == "SYNC_FAILED"

    calls = []

    async def recover(self, rental_id, payload):
        calls.append(payload)

    monkeypatch.setattr(
        "entregas_api.rentals_client.RentalsClient.notify_delivery_status", recover
    )
    reconciled = client.post(
        f"/v1/deliveries/{delivery_id}/reconcile-rental",
        headers=ADMIN_HEADERS,
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["rentalSyncStatus"] == "SYNCED"
    assert calls[0]["status"] == "DELIVERED"


def test_pending_rental_sync_retries_when_delivery_request_is_replayed(
    client, monkeypatch
):
    from entregas_api.database import SessionLocal
    from entregas_api.db_models import Delivery, DeliveryEventRecord

    _mock_get_rental(monkeypatch)
    delivery_id = _create(client).json()["id"]
    with SessionLocal() as db:
        delivery = db.get(Delivery, delivery_id)
        delivery.status = "DELIVERED"
        delivery.payment_status = "PAID"
        delivery.rental_sync_status = "PENDING_SYNC"
        db.add(
            DeliveryEventRecord(
                delivery_id=delivery_id,
                type="DELIVERED",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    calls = []

    async def notify(self, rental_id, payload):
        calls.append(payload)

    monkeypatch.setattr(
        "entregas_api.rentals_client.RentalsClient.notify_delivery_status", notify
    )
    response = client.post(
        f"/v1/deliveries/{delivery_id}/deliver",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["rentalSyncStatus"] == "SYNCED"
    assert calls[0]["status"] == "DELIVERED"


def test_reschedule_and_cancel_notify_arriendos(client, monkeypatch):
    _mock_get_rental(monkeypatch)
    delivery_id = _create(client).json()["id"]
    new_date = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    rescheduled = client.put(
        f"/v1/deliveries/{delivery_id}/schedule",
        headers=ADMIN_HEADERS,
        json={"scheduledAt": new_date, "reason": "Cambio de agenda"},
    )
    assert rescheduled.status_code == 200
    actual_date = datetime.fromisoformat(
        rescheduled.json()["scheduledAt"].replace("Z", "+00:00")
    )
    assert actual_date == datetime.fromisoformat(new_date)

    calls = []

    async def notify(self, rental_id, payload):
        calls.append(payload)

    monkeypatch.setattr(
        "entregas_api.rentals_client.RentalsClient.notify_delivery_status", notify
    )
    cancelled = client.post(
        f"/v1/deliveries/{delivery_id}/cancel",
        headers=ADMIN_HEADERS,
        json={"reason": "Cliente cancela"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["rentalSyncStatus"] == "SYNCED"
    assert calls[0]["status"] == "CANCELLED"


def test_reschedule_rejects_unpaid_legacy_delivery(client, monkeypatch):
    from entregas_api.database import SessionLocal
    from entregas_api.db_models import Delivery

    _mock_get_rental(monkeypatch)
    delivery_id = _create(client).json()["id"]
    with SessionLocal() as db:
        delivery = db.get(Delivery, delivery_id)
        delivery.payment_status = "UNPAID"
        delivery.payment_id = None
        original_date = delivery.scheduled_at
        db.commit()

    response = client.put(
        f"/v1/deliveries/{delivery_id}/schedule",
        headers=ADMIN_HEADERS,
        json={"scheduledAt": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_REQUIRED"
    with SessionLocal() as db:
        assert db.get(Delivery, delivery_id).scheduled_at == original_date


def test_validation_errors_use_contract_status_code(client):
    response = client.post(
        "/v1/deliveries",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "invalid"},
        json={"rentalId": 1},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_REQUEST"


def test_empty_rental_lookup_returns_not_found(client):
    response = client.get(
        "/v1/rentals/999/delivery",
        headers=READONLY_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
