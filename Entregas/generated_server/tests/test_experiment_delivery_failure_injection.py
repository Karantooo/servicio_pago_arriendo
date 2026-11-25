from datetime import datetime, timedelta, timezone


ADMIN_HEADERS = {"API-Key": "test-admin-key"}
READONLY_HEADERS = {"API-Key": "test-readonly-key"}


def _payload():
    return {
        "rentalId": 10,
        "clientId": 20,
        "equipmentId": 30,
        "scheduledAt": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "paymentStatus": "PAID",
        "paymentId": 42,
    }


def _mock_rental_client(monkeypatch):
    async def get_rental(self, rental_id):
        return {
            "id": rental_id,
            "clientId": 20,
            "equipmentId": 30,
            "status": "PENDING",
            "startDate": datetime.now(timezone.utc).isoformat(),
            "endDate": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        }

    async def get_saga(self, rental_id):
        return {
            "operation": "CREATE",
            "status": "IN_PROGRESS",
            "currentStep": "PAYMENT_CONFIRMED",
            "paymentStatus": "CHARGED",
            "paymentId": 42,
        }

    monkeypatch.setattr(
        "entregas_api.rentals_client.RentalsClient.get_rental",
        get_rental,
    )
    monkeypatch.setattr(
        "entregas_api.rentals_client.RentalsClient.get_saga",
        get_saga,
    )


def test_fail_next_returns_controlled_409_without_persisting_delivery(
    client, monkeypatch, caplog
):
    monkeypatch.setenv("DELIVERIES_ENABLE_FAILURE_INJECTION", "true")
    _mock_rental_client(monkeypatch)

    configured = client.post(
        "/admin/failure-injection",
        headers=ADMIN_HEADERS,
        json={"mode": "FAIL_NEXT"},
    )
    assert configured.status_code == 200
    assert configured.json()["mode"] == "FAIL_NEXT"

    failed = client.post(
        "/v1/deliveries",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "experiment-fail-once"},
        json=_payload(),
    )
    assert failed.status_code == 409
    assert failed.json() == {
        "code": "DELIVERY_FAILURE_INJECTED",
        "message": "Fallo de entrega inyectado para pruebas.",
    }
    assert "Simulated delivery failure injected" in caplog.text
    assert client.get("/v1/deliveries", headers=READONLY_HEADERS).json() == []

    payload = _payload()
    recovered = client.post(
        "/v1/deliveries",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "experiment-after-failure"},
        json=payload,
    )
    assert recovered.status_code == 201
    assert recovered.json()["status"] == "SCHEDULED"

    client.post("/admin/failure-injection", headers=ADMIN_HEADERS, json={"mode": "FAIL_NEXT"})
    replay = client.post(
        "/v1/deliveries",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "experiment-after-failure"},
        json=payload,
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == recovered.json()["id"]

    # La repetición idempotente no consume la inyección del siguiente agendamiento.
    next_delivery = client.post(
        "/v1/deliveries",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "experiment-next-rental"},
        json={**payload, "rentalId": 11},
    )
    assert next_delivery.status_code == 409
    assert next_delivery.json()["code"] == "DELIVERY_FAILURE_INJECTED"
