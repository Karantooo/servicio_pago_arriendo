from app import saga_orchestrator
from app.bd_models import Rental, Saga
from app.delivery_client import ScheduledDelivery
from app.models import PaymentCheckout
from app.payments_client import PaymentState

from experimentos.helpers import (
    ExperimentTrace,
    create_rental_request,
    notify_delivery,
    saga_events,
    show_evidence,
)


def test_reconciled_charge_advances_pending_saga_to_completed(
    arriendo_client, db_session, monkeypatch, experiment_iteration
):
    calls = []

    trace = ExperimentTrace(experiment_iteration)

    def reserve(*args, **kwargs):
        calls.append(("RESERVE_EQUIPMENT", args[0]))
        trace.add(
            "reservar_equipo",
            request={"equipment_id": args[0]},
            response={"result": "accepted"},
        )

    def start_payment(**kwargs):
        trace.add(
            "iniciar_cobro",
            request=kwargs,
            response={"payment_id": 502, "status": "PENDING"},
        )
        return PaymentCheckout(
            paymentId=502,
            status="PENDING",
            redirectUrl="https://example.invalid/checkout",
            token="experiment-token-502",
        )

    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        reserve,
    )
    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", start_payment)

    created = create_rental_request(
        arriendo_client, "success-reconcile", trace=trace
    )
    assert created.status_code == 201, created.text
    rental_id = created.json()["rental"]["id"]
    saga = db_session.query(Saga).one()
    assert saga.current_step == "WAITING_PAYMENT"

    monkeypatch.setattr(
        saga_orchestrator,
        "consultar_pago",
        lambda payment_id: (
            trace.add(
                "consultar_pago",
                request={"payment_id": payment_id},
                response={"payment_id": 502, "status": "CHARGED"},
            )
            or calls.append(("PAYMENT_CONFIRMED", payment_id))
            or PaymentState(
                payment_id=502,
                rental_id=rental_id,
                amount_total=80000,
                status="CHARGED",
                redirect_url=None,
                token="experiment-token-502",
            )
        ),
    )
    monkeypatch.setattr(
        saga_orchestrator,
        "agendar_entrega",
        lambda **kwargs: (
            trace.add(
                "agendar_entrega",
                request=kwargs,
                response={"delivery_id": 702, "status": "SCHEDULED"},
            )
            or calls.append(("DELIVERY_SCHEDULED", 702))
            or ScheduledDelivery(delivery_id=702, status="SCHEDULED")
        ),
    )
    reconciled = arriendo_client.post(
        f"/v2/rentals/{rental_id}/reconcile-payment"
    )
    trace.http(
        "conciliar_pago",
        "POST",
        f"/v2/rentals/{rental_id}/reconcile-payment",
        None,
        reconciled,
    )
    assert reconciled.status_code == 200, reconciled.text
    delivered = notify_delivery(arriendo_client, rental_id, 702, trace=trace)
    assert delivered.status_code == 200, delivered.text

    db_session.expire_all()
    rental = db_session.get(Rental, rental_id)
    saga = db_session.query(Saga).one()
    events = saga_events(db_session, saga.id)
    assert rental.status == "ACTIVE"
    assert saga.status == "COMPLETED"
    assert saga.current_step == "DONE"
    assert saga.delivery_id == 702
    assert [call[0] for call in calls] == [
        "RESERVE_EQUIPMENT",
        "PAYMENT_CONFIRMED",
        "DELIVERY_SCHEDULED",
    ]
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
        "exito-02-reconciliacion",
        before={"status": "IN_PROGRESS", "step": "WAITING_PAYMENT"},
        after={"rental_status": rental.status, "saga": saga.status, "step": saga.current_step},
        committed_steps=calls,
        saga_events=events,
        trace=trace,
    )
