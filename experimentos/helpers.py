import json
import threading
from datetime import datetime, timezone
from uuid import uuid4

from app.bd_models import SagaStepEvent


class ExperimentTrace:
    def __init__(self):
        self._steps = []
        self._lock = threading.Lock()

    def add(self, action, **details):
        with self._lock:
            self._steps.append(
                {"sequence": len(self._steps) + 1, "action": action, **details}
            )

    def http(self, action, method, path, request, response, elapsed_ms=None):
        try:
            response_body = response.json()
        except ValueError:
            response_body = response.text
        self.add(
            action,
            request={
                "method": method,
                "path": path,
                "body": request,
            },
            response={
                "status_code": response.status_code,
                "body": response_body,
                "elapsed_ms": elapsed_ms,
            },
        )

    def snapshot(self):
        with self._lock:
            return list(self._steps)


def create_rental_request(client, idempotency_key, *, client_id=None, trace=None):
    if client_id is None:
        customer_request = {
            "name": f"Experimento {idempotency_key}",
            "email": f"{uuid4()}@experimentos.example",
        }
        created_client = client.post(
            "/v1/clients",
            json=customer_request,
        )
        assert created_client.status_code == 201, created_client.text
        if trace is not None:
            trace.http(
                "crear cliente",
                "POST",
                "/v1/clients",
                customer_request,
                created_client,
            )
        client_id = created_client.json()["id"]

    tariff_request = {"dailyRate": 10000, "deposit": 20000}
    tariff = client.put(
        "/v2/tariffs/50",
        json=tariff_request,
    )
    assert tariff.status_code == 200, tariff.text
    if trace is not None:
        trace.http(
            "configurar tarifa",
            "PUT",
            "/v2/tariffs/50",
            tariff_request,
            tariff,
        )

    rental_request = {
        "clientId": client_id,
        "equipmentId": 50,
        "startDate": "2099-09-20T10:00:00Z",
        "endDate": "2099-09-25T18:00:00Z",
    }
    created = client.post(
        "/v2/rentals",
        headers={"Idempotency-Key": idempotency_key},
        json=rental_request,
    )
    if trace is not None:
        trace.http(
            "crear arriendo",
            "POST",
            "/v2/rentals",
            {
                "headers": {"Idempotency-Key": idempotency_key},
                "body": rental_request,
            },
            created,
        )
    return created


def saga_events(db_session, saga_id):
    events = (
        db_session.query(SagaStepEvent)
        .filter_by(saga_id=saga_id)
        .order_by(SagaStepEvent.id)
        .all()
    )
    return [
        {"step": event.step, "status": event.status, "detail": event.detail}
        for event in events
    ]


def notify_delivery(client, rental_id, delivery_id, *, trace=None):
    notification = {
        "deliveryId": delivery_id,
        "status": "DELIVERED",
        "reason": "Experimento de defensa",
        "occurredAt": datetime.now(timezone.utc).isoformat(),
    }
    response = client.put(
        f"/v2/rentals/{rental_id}/delivery-status",
        json=notification,
    )
    if trace is not None:
        trace.http(
            "notificar entrega",
            "PUT",
            f"/v2/rentals/{rental_id}/delivery-status",
            notification,
            response,
        )
    return response


def show_evidence(name, **evidence):
    trace = evidence.pop("trace", None)
    timeline = []
    if trace is not None:
        timeline = trace.snapshot()
        evidence["timeline"] = timeline
    for step in timeline:
        print(
            "TRACE_JSON "
            + json.dumps(
                {"experiment": name, **step},
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )
        )
    for sequence, event in enumerate(evidence.get("saga_events", []), start=1):
        print(
            "SAGA_EVENT_JSON "
            + json.dumps(
                {"experiment": name, "sequence": sequence, **event},
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )
        )
    after = evidence.get("after", {})
    if isinstance(after, dict):
        for event in after.get("log_events", []):
            print(
                "APP_LOG_JSON "
                + json.dumps(
                    {"experiment": name, **event},
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                )
            )
    print(
        "EVIDENCE_JSON "
        + json.dumps(
            {"experiment": name, **evidence},
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
    )
