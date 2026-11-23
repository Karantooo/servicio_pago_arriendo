from fastapi import HTTPException

from app import saga_orchestrator
from app.bd_models import Rental, Saga
from app.delivery_client import DeliverySchedulingRejected
from app.models import PaymentCheckout
from app.payments_client import RefundState

from experimentos.helpers import (
    ExperimentTrace,
    create_rental_request,
    saga_events,
    show_evidence,
)


def test_release_error_is_explicit_and_retry_finishes_compensation(
    arriendo_client, db_session, monkeypatch
):
    calls = []

    trace = ExperimentTrace()

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
                response={"payment_id": 602, "status": "CHARGED"},
            )
            or PaymentCheckout(paymentId=602, status="CHARGED")
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
    monkeypatch.setattr(
        saga_orchestrator,
        "reembolsar_pago",
        lambda **_: (
            calls.append("REFUND_CONFIRMED")
            or trace.add(
                "reembolsar_pago",
                request={},
                response={"refund_id": 802, "status": "REFUNDED"},
            )
            or RefundState(refund_id=802, status="REFUNDED")
        ),
    )

    def release(equipment_id, reservation_id):
        calls.append("RELEASE_EQUIPMENT")
        if calls.count("RELEASE_EQUIPMENT") == 1:
            trace.add(
                "liberar_equipo",
                request={"equipment_id": equipment_id, "reservation_id": reservation_id},
                response={"status_code": 503, "detail": "Equipos temporalmente caído"},
            )
            raise HTTPException(status_code=503, detail="Equipos temporalmente caído")
        trace.add(
            "reintentar_liberacion",
            request={"equipment_id": equipment_id, "reservation_id": reservation_id},
            response={"result": "released"},
        )

    monkeypatch.setattr(saga_orchestrator.grpc_client, "liberar_unidad", release)
    response = create_rental_request(
        arriendo_client, "compensate-release-retry", trace=trace
    )
    assert response.status_code == 409
    rental = db_session.query(Rental).one()
    saga = db_session.query(Saga).one()
    assert saga.status == "COMPENSATION_FAILED"
    assert saga.current_step == "RELEASE_EQUIPMENT"
    assert saga.refund_status == "REFUNDED"
    assert rental.status == "PENDING"
    trace.add(
        "estado_antes_del_reintento",
        rental_status=rental.status,
        saga_status=saga.status,
        current_step=saga.current_step,
        refund_status=saga.refund_status,
    )

    retried = arriendo_client.post(
        f"/v2/rentals/{rental.id}/reconcile-payment"
    )
    trace.http(
        "conciliar_pago_y_reintentar_liberacion",
        "POST",
        f"/v2/rentals/{rental.id}/reconcile-payment",
        None,
        retried,
    )
    assert retried.status_code == 200, retried.text
    db_session.refresh(rental)
    db_session.refresh(saga)
    events = saga_events(db_session, saga.id)
    assert saga.status == "COMPENSATED"
    assert saga.equipment_release_status == "COMPLETED"
    assert rental.status == "FAILED"
    assert calls.count("REFUND_CONFIRMED") == 1
    assert calls.count("RELEASE_EQUIPMENT") == 2
    show_evidence(
        "compensacion-02-reintento-liberacion",
        before={"saga": "COMPENSATION_FAILED", "refund": saga.refund_status},
        after={"rental_status": rental.status, "saga": saga.status},
        compensation_calls=calls,
        saga_events=events,
        trace=trace,
    )
