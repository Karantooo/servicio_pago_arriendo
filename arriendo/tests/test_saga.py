import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from uuid import uuid4

from app.bd_models import Rental, Saga
from app.models import PaymentCheckout
from app.payments_client import PaymentLookupUncertain, PaymentStartRejected, PaymentStartUncertain, PaymentState


PAYLOAD = {
    "clientId": 1,
    "equipmentId": 50,
    "startDate": "2026-09-20T10:00:00Z",
    "endDate": "2026-09-25T18:00:00Z",
}
IDEMPOTENCY_HEADERS = {"Idempotency-Key": "rental-create-1"}


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
        headers=IDEMPOTENCY_HEADERS,
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
    assert sent["idempotency_key"] == IDEMPOTENCY_HEADERS["Idempotency-Key"]
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


def test_v2_retry_returns_existing_checkout_without_second_charge(
    client, db_session, monkeypatch
):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    calls = {"reserve": 0, "charge": 0}

    def reserve(_):
        calls["reserve"] += 1

    def charge(**kwargs):
        calls["charge"] += 1
        return PaymentCheckout(
            paymentId=501, status="PENDING",
            redirectUrl="https://webpay.example/pagar", token="token-501",
        )

    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", reserve)
    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", charge)
    payload = {**PAYLOAD, "clientId": client_id}

    first = client.post("/v2/rentals", json=payload, headers=IDEMPOTENCY_HEADERS)
    retry = client.post("/v2/rentals", json=payload, headers=IDEMPOTENCY_HEADERS)

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert calls == {"reserve": 1, "charge": 1}
    assert db_session.query(Rental).count() == 1
    assert db_session.query(Saga).count() == 1


def test_v2_reused_key_with_different_body_is_conflict(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    monkeypatch.setattr(
        saga_orchestrator, "iniciar_cobro",
        lambda **kwargs: PaymentCheckout(
            paymentId=501, status="PENDING",
            redirectUrl="https://webpay.example/pagar", token="token-501",
        ),
    )
    payload = {**PAYLOAD, "clientId": client_id}
    first = client.post("/v2/rentals", json=payload, headers=IDEMPOTENCY_HEADERS)
    changed = client.post(
        "/v2/rentals",
        json={**payload, "endDate": "2026-09-26T18:00:00Z"},
        headers=IDEMPOTENCY_HEADERS,
    )

    assert first.status_code == 201
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert db_session.query(Rental).count() == 1


def test_v2_create_requires_idempotency_key(client, db_session):
    client_id = _create_client(client)
    response = client.post(
        "/v2/rentals", json={**PAYLOAD, "clientId": client_id}
    )
    assert response.status_code == 400
    assert db_session.query(Rental).count() == 0


def test_create_idempotency_key_has_database_unique_constraint(
    db_session, crear_arriendo
):
    rental = crear_arriendo()
    db_session.add_all([
        Saga(
            id=str(uuid4()), rental_id=rental.id, operation="CREATE",
            status="STARTED", current_step="RESERVE_EQUIPMENT",
            idempotency_key="same-key",
        ),
        Saga(
            id=str(uuid4()), rental_id=rental.id, operation="CREATE",
            status="STARTED", current_step="RESERVE_EQUIPMENT",
            idempotency_key="same-key",
        ),
    ])
    with pytest.raises(IntegrityError):
        db_session.commit()


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
        headers=IDEMPOTENCY_HEADERS,
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
        headers=IDEMPOTENCY_HEADERS,
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
    response = client.post(
        "/v2/rentals", json={**PAYLOAD, "clientId": client_id},
        headers=IDEMPOTENCY_HEADERS,
    )

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
    response = client.post(
        "/v2/rentals", json={**PAYLOAD, "clientId": client_id},
        headers=IDEMPOTENCY_HEADERS,
    )

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
    response = client.post(
        "/v2/rentals", json={**PAYLOAD, "clientId": client_id},
        headers=IDEMPOTENCY_HEADERS,
    )

    assert response.status_code == 504
    assert released == []
    assert db_session.query(Rental).one().status == "PENDING"
    assert db_session.query(Saga).one().status == "IN_DOUBT"


def test_v2_unknown_charge_is_not_confirmed(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    monkeypatch.setattr(
        saga_orchestrator,
        "iniciar_cobro",
        lambda **_: PaymentCheckout(paymentId=501, status="UNKNOWN"),
    )

    response = client.post(
        "/v2/rentals", json={**PAYLOAD, "clientId": client_id},
        headers=IDEMPOTENCY_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["payment"]["status"] == "UNKNOWN"
    saga = db_session.query(Saga).one()
    assert saga.status == "IN_DOUBT"
    assert saga.current_step == "WAITING_PAYMENT"
    assert db_session.query(Rental).one().status == "PENDING"


def test_reconcile_does_not_repeat_charge_while_creation_is_running(
    client, db_session, crear_arriendo, monkeypatch
):
    from app import saga_orchestrator

    rental = crear_arriendo(status="PENDING")
    saga = Saga(
        id=str(uuid4()), rental_id=rental.id, operation="CREATE",
        status="IN_PROGRESS", current_step="START_PAYMENT",
        idempotency_key="pending-create",
    )
    db_session.add(saga)
    db_session.commit()

    def unexpected_charge(**_):
        raise AssertionError("La conciliación no debe iniciar otro cobro aún")

    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", unexpected_charge)
    response = client.post(f"/v2/rentals/{rental.id}/reconcile-payment")

    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_START_IN_PROGRESS"


def _pending_payment(rental_id, status="PENDING", payment_id=501):
    return PaymentState(
        payment_id=payment_id,
        rental_id=rental_id,
        amount_total=80000,
        status=status,
        redirect_url="https://webpay.example/pagar",
        token="token-501",
    )


def test_reconcile_confirmed_payment_waits_for_delivery(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    monkeypatch.setattr(
        saga_orchestrator, "iniciar_cobro",
        lambda **_: PaymentCheckout(paymentId=501, status="PENDING", redirectUrl="https://webpay.example/pagar", token="token-501"),
    )
    created = client.post("/v2/rentals", json={**PAYLOAD, "clientId": client_id}, headers=IDEMPOTENCY_HEADERS)
    rental_id = created.json()["rental"]["id"]
    calls = []
    monkeypatch.setattr(saga_orchestrator, "consultar_pago", lambda payment_id: calls.append(payment_id) or _pending_payment(rental_id, "CHARGED"))

    first = client.post(f"/v2/rentals/{rental_id}/reconcile-payment")
    second = client.post(f"/v2/rentals/{rental_id}/reconcile-payment")

    assert first.status_code == second.status_code == 200
    assert first.json()["currentStep"] == "PAYMENT_CONFIRMED"
    assert calls == [501]
    assert db_session.get(Rental, rental_id).status == "PENDING"


def test_reconcile_failed_payment_releases_once(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    released = []
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    monkeypatch.setattr(saga_orchestrator.grpc_client, "liberar_unidad", released.append)
    monkeypatch.setattr(
        saga_orchestrator, "iniciar_cobro",
        lambda **_: PaymentCheckout(paymentId=501, status="PENDING", redirectUrl="https://webpay.example/pagar", token="token-501"),
    )
    created = client.post("/v2/rentals", json={**PAYLOAD, "clientId": client_id}, headers=IDEMPOTENCY_HEADERS)
    rental_id = created.json()["rental"]["id"]
    monkeypatch.setattr(saga_orchestrator, "consultar_pago", lambda _: _pending_payment(rental_id, "FAILED"))

    first = client.post(f"/v2/rentals/{rental_id}/reconcile-payment")
    second = client.post(f"/v2/rentals/{rental_id}/reconcile-payment")

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "COMPENSATED"
    assert released == [50]
    assert db_session.get(Rental, rental_id).status == "FAILED"


def test_reconcile_recovers_missing_payment_id_with_original_key(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    sent = []

    def charge(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            raise PaymentStartUncertain("Timeout")
        return PaymentCheckout(paymentId=501, status="PENDING", redirectUrl="https://webpay.example/pagar", token="token-501")

    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", charge)
    created = client.post("/v2/rentals", json={**PAYLOAD, "clientId": client_id}, headers=IDEMPOTENCY_HEADERS)
    assert created.status_code == 504
    rental_id = db_session.query(Rental).one().id
    monkeypatch.setattr(saga_orchestrator, "consultar_pago", lambda _: _pending_payment(rental_id))

    response = client.post(f"/v2/rentals/{rental_id}/reconcile-payment")

    assert response.status_code == 200
    assert response.json()["currentStep"] == "WAITING_PAYMENT"
    assert sent[0]["idempotency_key"] == sent[1]["idempotency_key"]
    assert sent[1]["require_checkout"] is False
    assert db_session.query(Saga).one().payment_id == 501


def test_reconcile_unknown_and_lookup_failure_keep_reservation(client, db_session, monkeypatch):
    from app import saga_orchestrator

    client_id = _create_client(client)
    _create_tariff(client)
    released = []
    monkeypatch.setattr(saga_orchestrator.grpc_client, "reservar_unidad", lambda _: None)
    monkeypatch.setattr(saga_orchestrator.grpc_client, "liberar_unidad", released.append)
    monkeypatch.setattr(
        saga_orchestrator, "iniciar_cobro",
        lambda **_: PaymentCheckout(paymentId=501, status="PENDING", redirectUrl="https://webpay.example/pagar", token="token-501"),
    )
    created = client.post("/v2/rentals", json={**PAYLOAD, "clientId": client_id}, headers=IDEMPOTENCY_HEADERS)
    rental_id = created.json()["rental"]["id"]
    monkeypatch.setattr(saga_orchestrator, "consultar_pago", lambda _: _pending_payment(rental_id, "UNKNOWN"))
    unknown = client.post(f"/v2/rentals/{rental_id}/reconcile-payment")
    assert unknown.status_code == 200
    assert unknown.json()["status"] == "IN_DOUBT"

    def lookup_failure(_):
        raise PaymentLookupUncertain("Timeout")

    monkeypatch.setattr(saga_orchestrator, "consultar_pago", lookup_failure)
    failure = client.post(f"/v2/rentals/{rental_id}/reconcile-payment")
    assert failure.status_code == 503
    assert released == []
    assert db_session.get(Rental, rental_id).status == "PENDING"


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
