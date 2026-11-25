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


def test_idempotent_replay_does_not_duplicate_committed_saga_steps(
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
        calls.append(("START_PAYMENT", 503))
        trace.add(
            "iniciar_cobro",
            request=kwargs,
            response={"payment_id": 503, "status": "PENDING"},
        )
        return PaymentCheckout(
            paymentId=503,
            status="PENDING",
            redirectUrl="https://example.invalid/checkout",
            token="experiment-token-503",
        )

    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        reserve,
    )
    monkeypatch.setattr(saga_orchestrator, "iniciar_cobro", start_payment)
    first = create_rental_request(
        arriendo_client, "success-idempotent", trace=trace
    )
    assert first.status_code == 201, first.text
    replay = create_rental_request(
        arriendo_client,
        "success-idempotent",
        client_id=first.json()["rental"]["clientId"],
        trace=trace,
    )
    assert replay.status_code == 201, replay.text
    assert first.json() == replay.json()
    rental_id = first.json()["rental"]["id"]

    monkeypatch.setattr(
        saga_orchestrator,
        "consultar_pago",
        lambda payment_id: (
            trace.add(
                "consultar_pago",
                request={"payment_id": payment_id},
                response={"payment_id": 503, "status": "CHARGED"},
            )
            or PaymentState(
                payment_id=503,
                rental_id=rental_id,
                amount_total=80000,
                status="CHARGED",
                redirect_url=None,
                token="experiment-token-503",
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
                response={"delivery_id": 703, "status": "SCHEDULED"},
            )
            or calls.append(("DELIVERY_SCHEDULED", 703))
            or ScheduledDelivery(delivery_id=703, status="SCHEDULED")
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
    repeated_reconcile = arriendo_client.post(
        f"/v2/rentals/{rental_id}/reconcile-payment"
    )
    trace.http(
        "repetir_conciliacion",
        "POST",
        f"/v2/rentals/{rental_id}/reconcile-payment",
        None,
        repeated_reconcile,
    )
    assert repeated_reconcile.status_code == 200, repeated_reconcile.text
    delivered = notify_delivery(arriendo_client, rental_id, 703, trace=trace)
    assert delivered.status_code == 200, delivered.text

    db_session.expire_all()
    rental = db_session.get(Rental, rental_id)
    saga = db_session.query(Saga).one()
    events = saga_events(db_session, saga.id)
    assert db_session.query(Rental).count() == 1
    assert saga.status == "COMPLETED"
    assert saga.current_step == "DONE"
    assert rental.status == "ACTIVE"
    assert [call[0] for call in calls].count("RESERVE_EQUIPMENT") == 1
    assert [call[0] for call in calls].count("START_PAYMENT") == 1
    assert [call[0] for call in calls].count("DELIVERY_SCHEDULED") == 1
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
        "exito-03-reintento-idempotente",
        before={"saga_count": 1, "rental_count": 1},
        after={"rental_status": rental.status, "saga": saga.status, "step": saga.current_step},
        duplicate_side_effects=0,
        committed_steps=calls,
        saga_events=events,
        trace=trace,
    )
