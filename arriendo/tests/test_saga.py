import pytest
from fastapi import HTTPException

from app.bd_models import Rental, Saga
from app.models import PaymentCheckout
from app.payments_client import PaymentStartRejected, PaymentStartUncertain


PAYLOAD = {
    "clientId": 1,
    "equipmentId": 50,
    "startDate": "2026-09-20T10:00:00Z",
    "endDate": "2026-09-25T18:00:00Z",
}


def _create_client(client):
    response = client.post(
        "/v1/clients",
        json={"name": "Cliente Saga", "email": "saga@example.com"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _create_tariff(client):
    response = client.put(
        "/v2/tariffs/50", json={"dailyRate": 10000, "deposit": 20000}
    )
    assert response.status_code == 200


def test_tariff_is_admin_only(admin_client, cliente_client, db_session):
    response = cliente_client.put(
        "/v2/tariffs/50", json={"dailyRate": 10000, "deposit": 20000}
    )
    assert response.status_code == 403

    response = admin_client.put(
        "/v2/tariffs/50", json={"dailyRate": 10000, "deposit": 20000}
    )
    assert response.status_code == 200
    assert response.json() == {
        "equipmentId": 50,
        "dailyRate": 10000,
        "deposit": 20000,
    }


def test_v2_create_rental_waits_for_payment(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    sent = {}
    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        lambda equipment_id: None,
    )
    def fake_charge(**kwargs):
        sent.update(kwargs)
        return PaymentCheckout(
            paymentId=501,
            status="PENDING",
            redirectUrl="https://webpay.example/pagar",
            token="token-501",
        )

    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", fake_charge)

    response = client.post(
        "/v2/rentals",
        json={**PAYLOAD, "clientId": client_id},
    )

    assert response.status_code == 201
    assert response.json()["rental"]["status"] == "PENDING"
    assert response.json()["payment"]["paymentId"] == 501
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    assert rental.status == "PENDING"
    assert rental.amount_rent == 60000  # Cinco días y ocho horas: seis periodos.
    assert rental.amount_deposit == 20000
    assert sent["amount_rent"] == 60000
    assert sent["amount_deposit"] == 20000
    assert sent["idempotency_key"] == saga.id
    assert saga.operation == "CREATE"
    assert saga.status == "IN_PROGRESS"
    assert saga.current_step == "WAITING_PAYMENT"
    assert saga.payment_id == 501

    updated = client.put(
        "/v2/tariffs/50", json={"dailyRate": 15000, "deposit": 30000}
    )
    assert updated.status_code == 200
    db_session.refresh(rental)
    assert (rental.amount_rent, rental.amount_deposit) == (60000, 20000)

    saga_response = client.get(f"/v2/rentals/{rental.id}/saga")
    assert saga_response.status_code == 200
    assert saga_response.json()["id"] == saga.id


def test_v2_create_reservation_failure_is_persisted(
    client,
    db_session,
    monkeypatch,
):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)

    def fail_reservation(equipment_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "INSUFFICIENT_STOCK", "message": "Sin stock."},
        )

    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        fail_reservation,
    )

    response = client.post(
        "/v2/rentals",
        json={**PAYLOAD, "clientId": client_id},
    )

    assert response.status_code == 409
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    assert rental.status == "FAILED"
    assert saga.status == "FAILED"
    assert saga.error == "Sin stock."


def test_v2_uncertain_reservation_keeps_saga_in_doubt(
    client,
    db_session,
    monkeypatch,
):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)

    def timeout_reservation(equipment_id):
        raise HTTPException(
            status_code=504,
            detail={"code": "EQUIPMENTS_SERVICE_TIMEOUT", "message": "Timeout."},
        )

    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        timeout_reservation,
    )

    response = client.post(
        "/v2/rentals",
        json={**PAYLOAD, "clientId": client_id},
    )

    assert response.status_code == 504
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    assert rental.status == "PENDING"
    assert saga.status == "IN_DOUBT"


def test_v2_missing_tariff_does_not_reserve(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)

    def unexpected_reservation(equipment_id):
        pytest.fail("No se debe reservar sin una tarifa")

    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", unexpected_reservation)
    response = client.post("/v2/rentals", json={**PAYLOAD, "clientId": client_id})

    assert response.status_code == 404
    assert response.json()["code"] == "TARIFF_NOT_FOUND"
    assert db_session.query(Rental).count() == 0


def test_v2_rejected_payment_releases_equipment(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    released = []
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    monkeypatch.setattr(
        saga_orchestrator.grpc_client, "liberar_unidad", released.append
    )

    def reject_payment(**kwargs):
        raise PaymentStartRejected("Solicitud rechazada")

    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", reject_payment)
    response = client.post("/v2/rentals", json={**PAYLOAD, "clientId": client_id})

    assert response.status_code == 502
    assert released == [50]
    assert db_session.query(Rental).one().status == "FAILED"
    assert db_session.query(Saga).one().status == "COMPENSATED"


def test_v2_uncertain_payment_keeps_reservation(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    released = []
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    monkeypatch.setattr(
        saga_orchestrator.grpc_client, "liberar_unidad", released.append
    )

    def timeout_payment(**kwargs):
        raise PaymentStartUncertain("Timeout")

    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", timeout_payment)
    response = client.post("/v2/rentals", json={**PAYLOAD, "clientId": client_id})

    assert response.status_code == 504
    assert released == []
    assert db_session.query(Rental).one().status == "PENDING"
    assert db_session.query(Saga).one().status == "IN_DOUBT"


@pytest.mark.parametrize(
    ("operation", "expected_status", "expected_saga_operation"),
    [
        ("cancel", "CANCELLED", "CANCEL"),
        ("return", "COMPLETED", "RETURN"),
    ],
)
def test_v2_finish_records_saga_and_releases_unit(
    client,
    db_session,
    monkeypatch,
    crear_arriendo,
    operation,
    expected_status,
    expected_saga_operation,
):
    from app import saga_orchestrator

    rental = crear_arriendo()
    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "liberar_unidad",
        lambda equipment_id: None,
    )

    response = client.post(f"/v2/rentals/{rental.id}/{operation}")

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    db_session.refresh(rental)
    saga = db_session.query(Saga).one()
    assert rental.status == expected_status
    assert saga.operation == expected_saga_operation
    assert saga.status == "COMPLETED"
