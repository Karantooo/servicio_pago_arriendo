import time
from concurrent import futures

import grpc
import pytest
from fastapi import status
from google.protobuf import empty_pb2

from app import grpc_client
from app.bd_models import Rental, Saga
from app.generated import equipos_pb2_grpc


def test_equipment_timeout_keeps_saga_in_doubt(
    client, db_session, monkeypatch
):
    class SlowEquipment(equipos_pb2_grpc.ServicioEquiposServicer):
        def ReservarEquipoIdentificado(self, request, context):
            time.sleep(0.35)
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
        monkeypatch.setattr(grpc_client, "GRPC_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(
            grpc_client,
            "get_grpc_stub",
            lambda: equipos_pb2_grpc.ServicioEquiposStub(channel),
        )

        created_client = client.post(
            "/v1/clients",
            json={"name": "Experimento timeout", "email": "timeout@example.com"},
        )
        assert created_client.status_code == 201
        tariff = client.put(
            "/v2/tariffs/50",
            json={"dailyRate": 10000, "deposit": 20000},
        )
        assert tariff.status_code == 200

        response = client.post(
            "/v2/rentals",
            headers={"Idempotency-Key": "experiment-timeout"},
            json={
                "clientId": created_client.json()["id"],
                "equipmentId": 50,
                "startDate": "2099-09-20T10:00:00Z",
                "endDate": "2099-09-25T18:00:00Z",
            },
        )

        assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
        assert response.json()["code"] == "EQUIPMENTS_SERVICE_TIMEOUT"
        saga = db_session.query(Saga).filter_by(
            idempotency_key="experiment-timeout"
        ).one()
        rental = db_session.get(Rental, saga.rental_id)
        assert saga.status == "IN_DOUBT"
        assert saga.current_step == "RESERVE_EQUIPMENT"
        assert rental.status == "PENDING"

        saga_response = client.get(f"/v2/rentals/{rental.id}/saga")
        assert saga_response.status_code == 200
        assert saga_response.json()["status"] == "IN_DOUBT"
    finally:
        channel.close()
        server.stop(grace=1).wait()
