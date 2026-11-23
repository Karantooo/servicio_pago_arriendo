import time
from concurrent import futures

import grpc
from google.protobuf import empty_pb2

from app import grpc_client, saga_orchestrator
from app.bd_models import Saga
from app.generated import equipos_pb2_grpc
from app.models import PaymentCheckout


def test_slow_equipment_response_within_deadline_advances_saga(
    client, db_session, monkeypatch
):
    timeout_seconds = 0.5
    rpc_durations = []

    class SlowEquipment(equipos_pb2_grpc.ServicioEquiposServicer):
        def ReservarEquipoIdentificado(self, request, context):
            started = time.perf_counter()
            time.sleep(0.03)
            rpc_durations.append(time.perf_counter() - started)
            return empty_pb2.Empty()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    equipos_pb2_grpc.add_ServicioEquiposServicer_to_server(
        SlowEquipment(), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=3)
        monkeypatch.setattr(grpc_client, "GRPC_TIMEOUT_SECONDS", timeout_seconds)
        monkeypatch.setattr(
            grpc_client,
            "get_grpc_stub",
            lambda: equipos_pb2_grpc.ServicioEquiposStub(channel),
        )
        monkeypatch.setattr(
            saga_orchestrator,
            "iniciar_cobro",
            lambda **_: PaymentCheckout(
                paymentId=501,
                status="PENDING",
                redirectUrl="https://example.com/pay",
                token="experiment-token",
            ),
        )

        created_client = client.post(
            "/v1/clients",
            json={"name": "Experimento latencia", "email": "latencia@example.com"},
        )
        assert created_client.status_code == 201
        tariff = client.put(
            "/v2/tariffs/50",
            json={"dailyRate": 10000, "deposit": 20000},
        )
        assert tariff.status_code == 200

        response = client.post(
            "/v2/rentals",
            headers={"Idempotency-Key": "experiment-latency"},
            json={
                "clientId": created_client.json()["id"],
                "equipmentId": 50,
                "startDate": "2099-09-20T10:00:00Z",
                "endDate": "2099-09-25T18:00:00Z",
            },
        )

        assert response.status_code == 201, response.text
        assert rpc_durations and 0.03 <= rpc_durations[0] < timeout_seconds
        saga = db_session.query(Saga).filter_by(
            idempotency_key="experiment-latency"
        ).one()
        assert saga.status == "IN_PROGRESS"
        assert saga.current_step == "WAITING_PAYMENT"

        saga_response = client.get(f"/v2/rentals/{saga.rental_id}/saga")
        assert saga_response.status_code == 200
        assert saga_response.json()["currentStep"] == "WAITING_PAYMENT"
    finally:
        channel.close()
        server.stop(grace=1).wait()
