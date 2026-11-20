from datetime import datetime, timezone
from uuid import uuid4

from app.bd_models import RentalDeliveryNotification, Saga


def _notification(delivery_id=7001, status="DELIVERED"):
    return {
        "deliveryId": delivery_id,
        "status": status,
        "reason": "Prueba de integración",
        "occurredAt": datetime.now(timezone.utc).isoformat(),
    }


def _create_payment_confirmed_saga(db_session, rental_id):
    saga = Saga(
        id=str(uuid4()),
        rental_id=rental_id,
        operation="CREATE",
        status="IN_PROGRESS",
        current_step="WAITING_FOR_DELIVERY",
    )
    db_session.add(saga)
    db_session.commit()
    return saga


def test_delivered_notification_activates_and_is_idempotent(
    admin_client, db_session, crear_arriendo
):
    rental = crear_arriendo(status="PENDING")
    saga = _create_payment_confirmed_saga(db_session, rental.id)

    first = admin_client.put(
        f"/v2/rentals/{rental.id}/delivery-status",
        json=_notification(),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "ACTIVE"

    db_session.refresh(saga)
    assert saga.status == "COMPLETED"
    assert saga.current_step == "DONE"
    assert (
        db_session.query(RentalDeliveryNotification)
        .filter_by(delivery_id=7001, status="DELIVERED")
        .count()
        == 1
    )

    replay = admin_client.put(
        f"/v2/rentals/{rental.id}/delivery-status",
        json=_notification(),
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "ACTIVE"
    assert (
        db_session.query(RentalDeliveryNotification)
        .filter_by(delivery_id=7001, status="DELIVERED")
        .count()
        == 1
    )


def test_cancelled_notification_cancels_pending_rental_with_saga(
    admin_client, db_session, crear_arriendo, monkeypatch
):
    rental = crear_arriendo(status="PENDING")
    creation_saga = _create_payment_confirmed_saga(db_session, rental.id)
    released = []
    monkeypatch.setattr(
        "app.saga_orchestrator.grpc_client.liberar_unidad",
        lambda equipment_id: released.append(equipment_id),
    )

    response = admin_client.put(
        f"/v2/rentals/{rental.id}/delivery-status",
        json=_notification(delivery_id=7002, status="CANCELLED"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert released == [rental.equipment_id]
    db_session.refresh(creation_saga)
    assert creation_saga.status == "COMPLETED"
    assert creation_saga.current_step == "DONE"

    replay = admin_client.put(
        f"/v2/rentals/{rental.id}/delivery-status",
        json=_notification(delivery_id=7002, status="CANCELLED"),
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "CANCELLED"
    assert released == [rental.equipment_id]
    assert (
        db_session.query(RentalDeliveryNotification)
        .filter_by(delivery_id=7002, status="CANCELLED")
        .count()
        == 1
    )


def test_delivery_notification_rejects_unconfirmed_payment(
    admin_client, crear_arriendo
):
    rental = crear_arriendo(status="PENDING")
    response = admin_client.put(
        f"/v2/rentals/{rental.id}/delivery-status",
        json=_notification(delivery_id=7003),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "RENTAL_NOT_READY_FOR_DELIVERY"
