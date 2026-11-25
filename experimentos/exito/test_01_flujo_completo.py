from datetime import datetime, timezone

from app import saga_orchestrator
from app.bd_models import Rental, Saga
from app.delivery_client import ScheduledDelivery
from app.models import PaymentCheckout

from experimentos.helpers import (
    ExperimentTrace,
    create_rental_request,
    notify_delivery,
    saga_events,
    show_evidence,
)


def test_complete_rental_commits_reservation_payment_and_delivery(
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
        calls.append(("PAYMENT_CONFIRMED", 501))
        trace.add(
            "iniciar_cobro",
            request=kwargs,
            response={"payment_id": 501, "status": "CHARGED"},
        )
        return PaymentCheckout(paymentId=501, status="CHARGED")

    def schedule(**kwargs):
        calls.append(("DELIVERY_SCHEDULED", 701))
        trace.add(
            "agendar_entrega",
            request=kwargs,
            response={"delivery_id": 701, "status": "SCHEDULED"},
        )
        return ScheduledDelivery(delivery_id=701, status="SCHEDULED")

    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        reserve,
    )
    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", charge)
    monkeypatch.setattr(saga_orchestrator, "agendar_entrega", schedule)

    before = {"rentals": db_session.query(Rental).count(), "sagas": db_session.query(Saga).count()}
    response = create_rental_request(
        arriendo_client, "success-complete", trace=trace
    )
    assert response.status_code == 201, response.text
    rental_id = response.json()["rental"]["id"]
    delivered = notify_delivery(arriendo_client, rental_id, 701, trace=trace)
    assert delivered.status_code == 200, delivered.text

    db_session.expire_all()
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    events = saga_events(db_session, saga.id)
    assert rental.status == "ACTIVE"
    assert saga.status == "COMPLETED"
    assert saga.current_step == "DONE"
    assert saga.payment_id == 501
    assert saga.delivery_id == 701
    assert [call[0] for call in calls] == [
        "RESERVE_EQUIPMENT",
        "PAYMENT_CONFIRMED",
        "DELIVERY_SCHEDULED",
    ]
    assert {event["step"] for event in events} >= {
        "RESERVE_EQUIPMENT",
        "START_PAYMENT",
        "DELIVERY_SCHEDULING",
        "DELIVERY_COMPLETED",
    }
    assert {
        (event["step"], event["status"])
        for event in events
    } >= {
        ("RESERVE_EQUIPMENT", "COMPLETED"),
        ("START_PAYMENT", "COMPLETED"),
        ("DELIVERY_SCHEDULING", "COMPLETED"),
        ("DELIVERY_COMPLETED", "COMPLETED"),
    }
    show_evidence(
        "exito-01-flujo-completo",
        before=before,
        after={"rental_status": rental.status, "saga": saga.status, "step": saga.current_step},
        committed_steps=calls,
        saga_events=events,
        trace=trace,
    )
