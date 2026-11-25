import httpx

from app import delivery_client, saga_orchestrator
from app.bd_models import Rental, Saga
from app.models import PaymentCheckout
from app.payments_client import RefundState

from experimentos.helpers import (
    ExperimentTrace,
    create_rental_request,
    saga_events,
    show_evidence,
)


def test_delivery_failure_refunds_then_releases_equipment(
    arriendo_client, db_session, monkeypatch, experiment_iteration
):
    calls = []
    trace = ExperimentTrace(experiment_iteration)

    def reserve(equipment_id, reservation_id, **_):
        calls.append(("RESERVE_EQUIPMENT", equipment_id, reservation_id))
        trace.add(
            "reservar_equipo",
            request={"equipment_id": equipment_id, "reservation_id": reservation_id},
            response={"result": "accepted"},
        )

    def charge(**kwargs):
        calls.append(("PAYMENT_CHARGED", 601))
        trace.add(
            "iniciar_cobro",
            request=kwargs,
            response={"payment_id": 601, "status": "CHARGED"},
        )
        return PaymentCheckout(paymentId=601, status="CHARGED")

    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        reserve,
    )
    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", charge)

    def fail_delivery(url, *, json, **_):
        calls.append(("DELIVERY_FAILED", None))
        trace.add(
            "agendar_entrega",
            request={"url": url, "body": json},
            response={"status_code": 409, "code": "DELIVERY_FAILURE_INJECTED"},
        )
        return httpx.Response(
            409,
            json={
                "code": "DELIVERY_FAILURE_INJECTED",
                "message": "Fallo de entrega inyectado para pruebas.",
            },
        )

    monkeypatch.setattr(delivery_client.httpx, "post", fail_delivery)

    def refund(**kwargs):
        calls.append(("REFUND_CONFIRMED", kwargs["payment_id"]))
        trace.add(
            "reembolsar_pago",
            request={"payment_id": kwargs["payment_id"]},
            response={"refund_id": 801, "status": "REFUNDED"},
        )
        return RefundState(refund_id=801, status="REFUNDED")

    monkeypatch.setattr(saga_orchestrator, "reembolsar_pago", refund)

    def release(equipment_id, reservation_id):
        calls.append(("RELEASE_EQUIPMENT", equipment_id, reservation_id))
        trace.add(
            "liberar_equipo",
            request={"equipment_id": equipment_id, "reservation_id": reservation_id},
            response={"result": "released"},
        )

    monkeypatch.setattr(saga_orchestrator.grpc_client, "liberar_unidad", release)

    response = create_rental_request(
        arriendo_client, "compensate-delivery", trace=trace
    )
    assert response.status_code == 409
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    events = saga_events(db_session, saga.id)
    assert rental.status == "FAILED"
    assert saga.status == "COMPENSATED"
    assert saga.current_step == "COMPENSATION_COMPLETED"
    assert saga.refund_status == "REFUNDED"
    assert saga.equipment_release_status == "COMPLETED"
    assert {
        (event["step"], event["status"])
        for event in events
    } >= {
        ("RESERVE_EQUIPMENT", "COMPLETED"),
        ("START_PAYMENT", "COMPLETED"),
        ("REFUND_PAYMENT", "COMPLETED"),
        ("RELEASE_EQUIPMENT", "COMPLETED"),
        ("DELIVERY_SCHEDULING", "FAILED"),
    }
    assert [call[0] for call in calls] == [
        "RESERVE_EQUIPMENT",
        "PAYMENT_CHARGED",
        "DELIVERY_FAILED",
        "REFUND_CONFIRMED",
        "RELEASE_EQUIPMENT",
    ]
    show_evidence(
        "compensacion-01-falla-entrega",
        before={"payment": "CHARGED", "equipment": "RESERVED"},
        after={
            "rental_status": rental.status,
            "saga": saga.status,
            "refund": saga.refund_status,
            "equipment_release": saga.equipment_release_status,
        },
        reverse_order=calls[3:],
        saga_events=events,
        trace=trace,
    )