from app import saga_orchestrator
from app.bd_models import Rental, Saga
from app.delivery_client import DeliverySchedulingRejected
from app.models import PaymentCheckout
from app.payments_client import PaymentState, RefundUncertain

from experimentos.helpers import (
    ExperimentTrace,
    create_rental_request,
    saga_events,
    show_evidence,
)


def test_uncertain_refund_holds_reservation_until_reconciliation(
    arriendo_client, db_session, monkeypatch, experiment_iteration
):
    calls = []

    trace = ExperimentTrace(experiment_iteration)

    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "reservar_unidad",
        lambda equipment_id, reservation_id, **_: (
            calls.append("RESERVE_EQUIPMENT")
            or trace.add(
                "reservar_equipo",
                request={"equipment_id": equipment_id, "reservation_id": reservation_id},
                response={"result": "accepted"},
            )
        ),
    )
    monkeypatch.setattr(
        saga_orchestrator,
        "iniciar_cobro",
        lambda **kwargs: (
            trace.add(
                "iniciar_cobro",
                request=kwargs,
                response={"payment_id": 603, "status": "CHARGED"},
            )
            or PaymentCheckout(paymentId=603, status="CHARGED")
        ),
    )
    monkeypatch.setattr(
        saga_orchestrator,
        "agendar_entrega",
        lambda **kwargs: (
            trace.add(
                "agendar_entrega",
                request=kwargs,
                response={"result": "rejected", "reason": "Fallo final de agendamiento."},
            )
            or (_ for _ in ()).throw(
                DeliverySchedulingRejected("Fallo final de agendamiento.")
            )
        ),
    )

    def uncertain_refund(**_):
        calls.append("REFUND_UNCERTAIN")
        trace.add(
            "reembolsar_pago",
            request={},
            response={"result": "unknown", "reason": "Sin confirmación del proveedor"},
        )
        raise RefundUncertain("No se recibió confirmación del proveedor.")

    monkeypatch.setattr(saga_orchestrator, "reembolsar_pago", uncertain_refund)
    released = []
    monkeypatch.setattr(
        saga_orchestrator.grpc_client,
        "liberar_unidad",
        lambda *args, **kwargs: (
            released.append(args[0])
            or trace.add(
                "liberar_equipo",
                request={"equipment_id": args[0]},
                response={"result": "released"},
            )
        ),
    )
    response = create_rental_request(
        arriendo_client, "compensate-refund-uncertain", trace=trace
    )
    assert response.status_code == 409
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    assert saga.status == "COMPENSATION_FAILED"
    assert saga.current_step == "REFUND_PAYMENT"
    assert rental.status == "PENDING"
    assert released == []

    monkeypatch.setattr(
        saga_orchestrator,
        "consultar_pago",
        lambda payment_id: (
            calls.append("REFUND_CONFIRMED_BY_LOOKUP")
            or trace.add(
                "consultar_reembolso",
                request={"payment_id": payment_id},
                response={"payment_id": 603, "status": "REFUNDED"},
            )
            or PaymentState(
                payment_id=603,
                rental_id=rental.id,
                amount_total=80000,
                status="REFUNDED",
                redirect_url=None,
                token="experiment-token-603",
            )
        ),
    )
    reconciled = arriendo_client.post(
        f"/v2/rentals/{rental.id}/reconcile-payment"
    )
    trace.http(
        "conciliar_pago_y_compensacion",
        "POST",
        f"/v2/rentals/{rental.id}/reconcile-payment",
        None,
        reconciled,
    )
    assert reconciled.status_code == 200, reconciled.text
    db_session.refresh(rental)
    db_session.refresh(saga)
    events = saga_events(db_session, saga.id)
    assert saga.status == "COMPENSATED"
    assert saga.refund_status == "REFUNDED"
    assert saga.equipment_release_status == "COMPLETED"
    assert rental.status == "FAILED"
    assert released == [rental.equipment_id]
    assert calls.index("REFUND_CONFIRMED_BY_LOOKUP") < len(calls)
    show_evidence(
        "compensacion-03-reembolso-incierto",
        before={"saga": "COMPENSATION_FAILED", "equipment_releases": 0},
        after={"rental_status": rental.status, "saga": saga.status},
        compensation_calls=calls + ["RELEASE_EQUIPMENT"],
        saga_events=events,
        trace=trace,
    )
