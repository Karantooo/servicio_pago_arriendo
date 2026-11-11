from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from pagos.database import SessionLocal, initialize_database
from pagos.db_models import Payment, Refund
from pagos.main import app
from pagos.providers import get_provider
from pagos.resilience import circuit_breaker


@pytest.fixture(autouse=True)
def reset_database():
    initialize_database()
    with SessionLocal() as db:
        db.query(Refund).delete()
        db.query(Payment).delete()
        db.commit()
    circuit_breaker.on_success()
    get_provider().set_mode("NORMAL")
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def create_charge(client, key="charge-1"):
    return client.post(
        "/payments/charge",
        json={
            "rental_id": 1001,
            "customer_id": 25,
            "amount_rent": 50000,
            "amount_deposit": 100000,
        },
        headers={"Idempotency-Key": key},
    )


def test_charge_and_idempotency_survive_database_engine_reconnect(client):
    response = create_charge(client)
    assert response.status_code == 201
    payment = response.json()

    from pagos.database import engine

    engine.dispose()
    duplicate = create_charge(client)

    assert duplicate.status_code == 200
    assert duplicate.json()["payment_id"] == payment["payment_id"]
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1


def test_idempotency_key_cannot_be_reused_with_different_charge(client):
    assert create_charge(client).status_code == 201
    response = client.post(
        "/payments/charge",
        json={
            "rental_id": 1002,
            "customer_id": 25,
            "amount_rent": 50000,
            "amount_deposit": 100000,
        },
        headers={"Idempotency-Key": "charge-1"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_provider_failure_is_persisted_as_unknown_without_recharging(client):
    get_provider().set_mode("DOWN")

    failed = create_charge(client)
    assert failed.status_code == 503

    retry = create_charge(client)
    assert retry.status_code == 200
    assert retry.json()["status"] == "UNKNOWN"
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1


def test_payment_return_and_refund_are_persisted_and_idempotent(client):
    charge = create_charge(client)
    payment = charge.json()
    payment_id = payment["payment_id"]

    returned = client.get(
        "/payments/transbank/return",
        params={"token_ws": payment["token"]},
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == "CHARGED"

    refund_body = {"reason": "SAGA_COMPENSATION", "rental_id": 1001}
    headers = {"Idempotency-Key": "refund-1"}
    refund = client.post(
        f"/payments/{payment_id}/refund",
        json=refund_body,
        headers=headers,
    )
    replay = client.post(
        f"/payments/{payment_id}/refund",
        json=refund_body,
        headers=headers,
    )

    assert refund.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["refund_id"] == refund.json()["refund_id"]
    with SessionLocal() as db:
        assert db.query(Refund).count() == 1
        assert db.get(Payment, payment_id).status == "REFUNDED"


def test_repeated_confirmation_callback_does_not_call_provider_twice(
    client,
    monkeypatch,
):
    charge = create_charge(client)
    payment = charge.json()
    provider = get_provider()
    call_count = 0
    count_lock = Lock()

    def confirm_once(token):
        nonlocal call_count
        with count_lock:
            call_count += 1
        return {
            "status": "AUTHORIZED",
            "response_code": 0,
            "message": "Pago autorizado correctamente.",
            "token": token,
        }

    monkeypatch.setattr(provider, "confirm_transaction", confirm_once)
    params = {"token_ws": payment["token"]}

    first = client.get("/payments/transbank/return", params=params)
    repeated = client.get("/payments/transbank/return", params=params)

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["status"] == "CHARGED"
    assert repeated.json()["status"] == "CHARGED"
    assert call_count == 1


def test_concurrent_confirmation_callbacks_claim_payment_once(
    client,
    monkeypatch,
):
    charge = create_charge(client)
    payment = charge.json()
    provider = get_provider()
    provider_entered = Event()
    finish_provider = Event()
    call_count = 0
    count_lock = Lock()

    def slow_confirm(token):
        nonlocal call_count
        with count_lock:
            call_count += 1
        provider_entered.set()
        assert finish_provider.wait(timeout=5)
        return {
            "status": "AUTHORIZED",
            "response_code": 0,
            "message": "Pago autorizado correctamente.",
            "token": token,
        }

    monkeypatch.setattr(provider, "confirm_transaction", slow_confirm)
    params = {"token_ws": payment["token"]}

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_request = executor.submit(
            client.get,
            "/payments/transbank/return",
            params=params,
        )
        assert provider_entered.wait(timeout=5)

        concurrent_replay = client.get(
            "/payments/transbank/return",
            params=params,
        )
        finish_provider.set()
        first_response = first_request.result(timeout=5)

    assert concurrent_replay.status_code == 200
    assert concurrent_replay.json()["status"] == "CONFIRMING"
    assert first_response.status_code == 200
    assert first_response.json()["status"] == "CHARGED"
    assert call_count == 1

    final_replay = client.get("/payments/transbank/return", params=params)
    assert final_replay.status_code == 200
    assert final_replay.json()["status"] == "CHARGED"
    assert call_count == 1


def test_unknown_confirmation_callback_does_not_repeat_provider_call(client):
    charge = create_charge(client)
    payment = charge.json()
    provider = get_provider()
    provider.set_mode("DOWN")
    params = {"token_ws": payment["token"]}

    failed_confirmation = client.get(
        "/payments/transbank/return",
        params=params,
    )
    assert failed_confirmation.status_code == 503

    provider.set_mode("NORMAL")
    replay = client.get("/payments/transbank/return", params=params)

    assert replay.status_code == 200
    assert replay.json()["status"] == "UNKNOWN"
    with SessionLocal() as db:
        assert db.get(Payment, payment["payment_id"]).status == "UNKNOWN"


def test_uncertain_refund_is_persisted_and_not_submitted_again(client):
    charge = create_charge(client)
    payment = charge.json()
    payment_id = payment["payment_id"]
    returned = client.get(
        "/payments/transbank/return",
        params={"token_ws": payment["token"]},
    )
    assert returned.status_code == 200
    get_provider().set_mode("DOWN")

    refund_body = {"reason": "SAGA_COMPENSATION", "rental_id": 1001}
    headers = {"Idempotency-Key": "refund-uncertain"}
    failed = client.post(
        f"/payments/{payment_id}/refund",
        json=refund_body,
        headers=headers,
    )
    retry = client.post(
        f"/payments/{payment_id}/refund",
        json=refund_body,
        headers=headers,
    )

    assert failed.status_code == 503
    assert retry.status_code == 200
    assert retry.json()["status"] == "UNKNOWN"
    assert retry.json()["amount_refunded"] is None
    with SessionLocal() as db:
        assert db.query(Refund).count() == 1
        assert db.get(Payment, payment_id).status == "REFUND_PENDING"
