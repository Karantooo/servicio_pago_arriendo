from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from uuid import uuid4

import grpc
from fastapi.testclient import TestClient
from google.protobuf import empty_pb2
from app import grpc_client, saga_orchestrator
from app.bd_models import Saga
from app.database import get_db
from app.generated import equipos_pb2_grpc
from app.main import app
from app.models import PaymentCheckout


def test_five_concurrent_rentals_leave_sagas_waiting_for_payment(
    client, db_session, monkeypatch
):
    worker_count = 5
    start = Barrier(worker_count)
    lock = Lock()
    reservation_calls = 0
    payment_calls = 0

    class AvailableEquipment(equipos_pb2_grpc.ServicioEquiposServicer):
        def ReservarEquipoIdentificado(self, request, context):
            nonlocal reservation_calls
            with lock:
                reservation_calls += 1
            return empty_pb2.Empty()

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=worker_count)
    )
    equipos_pb2_grpc.add_ServicioEquiposServicer_to_server(
        AvailableEquipment(), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=3)
        monkeypatch.setattr(
            grpc_client,
            "GRPC_TIMEOUT_SECONDS",
            1,
        )
        monkeypatch.setattr(
            grpc_client,
            "get_grpc_stub",
            lambda: equipos_pb2_grpc.ServicioEquiposStub(channel),
        )

        def start_payment(**_):
            nonlocal payment_calls
            with lock:
                payment_calls += 1
                payment_id = 1000 + payment_calls
            return PaymentCheckout(
                paymentId=payment_id,
                status="PENDING",
                redirectUrl="https://example.com/pay",
                token=f"experiment-token-{payment_id}",
            )

        monkeypatch.setattr(
            saga_orchestrator,
            "iniciar_cobro",
            start_payment,
        )

        customers = []
        for index in range(worker_count):
            created_client = client.post(
                "/v1/clients",
                json={
                    "name": f"Experimento burst {index}",
                    "email": f"burst-{uuid4()}@example.com",
                },
            )
            assert created_client.status_code == 201
            customers.append(created_client.json()["id"])

        tariff = client.put(
            "/v2/tariffs/50",
            json={"dailyRate": 10000, "deposit": 20000},
        )
        assert tariff.status_code == 200

        idempotency_keys = [f"experiment-burst-{i}" for i in range(worker_count)]
        db_session.rollback()
        db_override = app.dependency_overrides.pop(get_db)

        def create_rental(index):
            start.wait(timeout=10)
            with TestClient(app) as worker_client:
                return worker_client.post(
                    "/v2/rentals",
                    headers={
                        "API-Key": "apikey_admin",
                        "Idempotency-Key": idempotency_keys[index],
                    },
                    json={
                        "clientId": customers[index],
                        "equipmentId": 50,
                        "startDate": "2099-09-20T10:00:00Z",
                        "endDate": "2099-09-25T18:00:00Z",
                    },
                )

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                responses = list(pool.map(create_rental, range(worker_count)))
        finally:
            app.dependency_overrides[get_db] = db_override

        assert [response.status_code for response in responses] == [
            201
        ] * worker_count, [response.text for response in responses]
        db_session.expire_all()
        sagas = (
            db_session.query(Saga)
            .filter(Saga.idempotency_key.in_(idempotency_keys))
            .all()
        )
        assert len(sagas) == worker_count
        assert all(
            saga.status == "IN_PROGRESS"
            and saga.current_step == "WAITING_PAYMENT"
            for saga in sagas
        )
        assert reservation_calls == worker_count
        assert payment_calls == worker_count

        for saga in sagas:
            saga_response = client.get(f"/v2/rentals/{saga.rental_id}/saga")
            assert saga_response.status_code == 200
            assert saga_response.json()["currentStep"] == "WAITING_PAYMENT"
    finally:
        channel.close()
        server.stop(grace=1).wait()
