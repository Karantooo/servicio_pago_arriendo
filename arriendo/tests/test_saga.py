import pytest
from fastapi import HTTPException

from app.bd_models import Rental, Saga


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


def test_v2_create_rental_completes_saga(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        lambda equipment_id: None,
    )

    response = client.post(
        "/v2/rentals",
        json={**PAYLOAD, "clientId": client_id},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "ACTIVE"
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    assert rental.status == "ACTIVE"
    assert saga.operation == "CREATE"
    assert saga.status == "COMPLETED"
    assert saga.current_step == "DONE"

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
