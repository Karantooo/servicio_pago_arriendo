import time
from uuid import uuid4

from helpers import (
    confirm_mock_payment,
    create_rental,
    print_evidence,
    timed_request,
    wait_for_payment,
    wait_for_saga,
)


def _complete_payment(api, trace, rental):
    confirm_mock_payment(api, trace, rental["payment_token"])
    wait_for_payment(api, trace, rental["payment_id"], "CHARGED")


def _schedule_delivery(api, trace, rental_id):
    reconciliation = timed_request(
        api,
        trace,
        "conciliar pago confirmado",
        "POST",
        f"/api/rentals/{rental_id}/reconcile-payment",
    )
    assert reconciliation.status_code == 200 or (
        reconciliation.status_code == 409
        and reconciliation.json().get("code") == "RECONCILIATION_IN_PROGRESS"
    ), (
        f"No se pudo conciliar el pago: HTTP {reconciliation.status_code}; "
        f"respuesta: {reconciliation.text[:500]}"
    )
    return wait_for_saga(
        api,
        trace,
        rental_id,
        lambda value: value.get("deliveryId") is not None,
    )


def test_create_payment_persists_real_rental_and_payment(
    api, experiment_iteration
):
    trace = []
    rental = create_rental(api, trace, experiment_iteration, 1)

    persisted_rental = timed_request(
        api,
        trace,
        "leer arriendo persistido",
        "GET",
        f"/api/rentals/{rental['rental_id']}",
    )
    assert persisted_rental.status_code == 200
    assert persisted_rental.json()["id"] == rental["rental_id"]

    payment = timed_request(
        api,
        trace,
        "leer pago persistido",
        "GET",
        f"/api/payments/{rental['payment_id']}",
    )
    assert payment.status_code == 200
    assert payment.json()["status"] == "PENDING"
    print_evidence(
        "crear-pago-real",
        experiment_iteration,
        trace,
        rental_id=rental["rental_id"],
        payment_id=rental["payment_id"],
        payment_status="PENDING",
    )


def test_idempotent_retry_reuses_persisted_rental_and_payment(
    api, experiment_iteration
):
    trace = []
    key = f"idempotencia-{experiment_iteration}-{uuid4().hex}"
    rental = create_rental(
        api,
        trace,
        experiment_iteration,
        2,
        idempotency_key=key,
    )
    replay = timed_request(
        api,
        trace,
        "reintentar creación idempotente",
        "POST",
        "/api/rentals",
        headers={"Idempotency-Key": key},
        json=rental["rental_body"],
    )
    assert replay.status_code == 201
    replay_body = replay.json()
    assert replay_body["rental"]["id"] == rental["rental_id"]
    assert replay_body["payment"]["paymentId"] == rental["payment_id"]

    payment = timed_request(
        api,
        trace,
        "verificar pago único",
        "GET",
        f"/api/payments/{rental['payment_id']}",
    )
    assert payment.status_code == 200
    assert payment.json()["status"] == "PENDING"
    print_evidence(
        "reintento-idempotente-real",
        experiment_iteration,
        trace,
        rental_id=rental["rental_id"],
        payment_id=rental["payment_id"],
        replayed=True,
    )


def test_payment_and_delivery_complete_saga(api, experiment_iteration):
    trace = []
    rental = create_rental(api, trace, experiment_iteration, 3)
    _complete_payment(api, trace, rental)
    saga = _schedule_delivery(api, trace, rental["rental_id"])
    delivery = timed_request(
        api,
        trace,
        "registrar retiro del equipo",
        "POST",
        f"/api/deliveries/{saga['deliveryId']}/deliver",
        json={"receivedBy": "Prueba de integración"},
    )
    assert delivery.status_code == 200, (
        f"No se pudo completar la entrega: HTTP {delivery.status_code}"
    )
    final_saga = wait_for_saga(
        api,
        trace,
        rental["rental_id"],
        lambda value: value.get("status") == "COMPLETED",
    )
    rental_response = timed_request(
        api,
        trace,
        "verificar arriendo activo",
        "GET",
        f"/api/rentals/{rental['rental_id']}",
    )
    assert rental_response.status_code == 200
    assert rental_response.json()["status"] == "ACTIVE"
    print_evidence(
        "pago-entrega-saga-completa",
        experiment_iteration,
        trace,
        rental_id=rental["rental_id"],
        payment_id=rental["payment_id"],
        delivery_id=final_saga["deliveryId"],
        saga_status=final_saga["status"],
        rental_status=rental_response.json()["status"],
    )


def test_cancellation_refunds_mock_payment(api, experiment_iteration):
    trace = []
    rental = create_rental(api, trace, experiment_iteration, 4)
    _complete_payment(api, trace, rental)
    creation_saga = _schedule_delivery(api, trace, rental["rental_id"])

    cancelled = timed_request(
        api,
        trace,
        "cancelar arriendo pagado",
        "POST",
        f"/api/rentals/{rental['rental_id']}/cancel",
    )
    assert cancelled.status_code == 200, (
        f"No se pudo cancelar el arriendo: HTTP {cancelled.status_code}"
    )
    assert cancelled.json()["status"] == "CANCELLED"

    payment = wait_for_payment(
        api, trace, rental["payment_id"], "REFUNDED", timeout_seconds=60
    )
    cancellation_saga = wait_for_saga(
        api,
        trace,
        rental["rental_id"],
        lambda value: value.get("operation") == "CANCEL"
        and value.get("status") == "COMPLETED",
    )
    print_evidence(
        "cancelacion-reembolso-mock",
        experiment_iteration,
        trace,
        rental_id=rental["rental_id"],
        payment_id=rental["payment_id"],
        payment_status=payment["status"],
        delivery_id=creation_saga["deliveryId"],
        refund_status=cancellation_saga["refundStatus"],
        saga_status=cancellation_saga["status"],
    )


def test_gateway_rejects_missing_authentication(api, experiment_iteration):
    started = time.perf_counter()
    response = api.get("/api/rentals", headers={"Authorization": "", "API-Key": ""})
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    assert response.status_code in {401, 403}
    print_evidence(
        "rechazo-autenticacion",
        experiment_iteration,
        [
            {
                "action": "solicitud sin credenciales",
                "method": "GET",
                "path": "/api/rentals",
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            }
        ],
        rejected=True,
    )
