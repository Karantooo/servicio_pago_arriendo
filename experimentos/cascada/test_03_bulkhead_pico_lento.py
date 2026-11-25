import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from pagos.observability import bulkhead_rejections_total
from pagos.resilience import Bulkhead

from experimentos.helpers import ExperimentTrace, show_evidence
from experimentos.cascada.helpers import (
    charge,
    metrics_snapshot,
    relevant_log_events,
    set_mode,
)


def test_bulkhead_rejects_overload_without_exhausting_health_endpoint(
    payments_client, monkeypatch, caplog, experiment_iteration
):
    import pagos.providers.mock_provider as mock_provider_module
    import pagos.resilience as resilience

    capacity = 2
    monkeypatch.setattr(mock_provider_module, "TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(resilience, "TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(resilience, "bulkhead", Bulkhead(capacity=capacity))
    bulkhead_rejections_total.labels(service="payments-api")._value.set(0)
    trace = ExperimentTrace(experiment_iteration)
    baseline = metrics_snapshot(payments_client)
    trace.add("estado_inicial", bulkhead_capacity=capacity, metrics=baseline)
    set_mode(payments_client, "SLOW", trace=trace)
    ready = Barrier(4)

    def call_charge(index):
        ready.wait(timeout=3)
        started = time.perf_counter()
        response = charge(
            payments_client,
            f"bulkhead-burst-{index}",
            rental_id=1200 + index,
            trace=trace,
        )
        return response.status_code, round((time.perf_counter() - started) * 1000, 2)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(call_charge, range(4)))

    statuses = [status for status, _ in results]
    assert 429 in statuses, results
    assert 504 in statuses, results
    started = time.perf_counter()
    health = payments_client.get("/health")
    health_ms = (time.perf_counter() - started) * 1000
    trace.http("consultar_health", "GET", "/health", None, health, health_ms)
    metrics_after = metrics_snapshot(payments_client)
    trace.add(
        "metricas_despues_del_pico",
        bulkhead_capacity=capacity,
        metrics=metrics_after,
    )
    rejected = metrics_after["bulkhead_rejections"]
    assert rejected >= 1
    assert health.status_code == 200
    assert health_ms < 150
    events = relevant_log_events(caplog, {"bulkhead_rejected"})
    assert events

    show_evidence(
        "cascada-03-bulkhead-pico",
        before={"bulkhead_capacity": capacity, "metrics": baseline},
        after={
            "responses": results,
            "rejected_by_bulkhead": statuses.count(429),
            "provider_timeouts": statuses.count(504),
            "metrics": metrics_after,
            "health_status": health.status_code,
            "health_ms": round(health_ms, 2),
            "log_events": events,
        },
        trace=trace,
    )
    time.sleep(0.45)
