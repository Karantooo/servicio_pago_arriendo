import time

from pagos.resilience import circuit_breaker

from experimentos.helpers import ExperimentTrace, show_evidence
from experimentos.cascada.helpers import (
    charge,
    metrics_snapshot,
    relevant_log_events,
    set_mode,
)


def test_slow_provider_opens_breaker_and_fast_fails_with_retry_fallback(
    payments_client, monkeypatch, caplog, experiment_iteration
):
    import pagos.providers.mock_provider as mock_provider_module
    import pagos.resilience as resilience

    monkeypatch.setattr(mock_provider_module, "TIMEOUT_SECONDS", 0.35)
    monkeypatch.setattr(resilience, "TIMEOUT_SECONDS", 0.04)
    threshold = circuit_breaker.threshold
    trace = ExperimentTrace(experiment_iteration)
    before_metrics = metrics_snapshot(payments_client)
    trace.add("estado_inicial", circuit="CLOSED", metrics=before_metrics)
    set_mode(payments_client, "SLOW", trace=trace)

    timeout_responses = []
    for index in range(threshold):
        started = time.perf_counter()
        response = charge(
            payments_client, f"slow-circuit-{index}", trace=trace
        )
        timeout_responses.append(
            {
                "status": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
        assert response.status_code == 504

    after_timeouts_metrics = metrics_snapshot(payments_client)
    trace.add(
        "circuito_tras_timeouts",
        circuit=circuit_breaker.state,
        metrics=after_timeouts_metrics,
    )
    assert circuit_breaker.state == "OPEN"
    assert after_timeouts_metrics["circuit_breaker"]["OPEN"] == 1

    started = time.perf_counter()
    fallback = charge(
        payments_client, "slow-circuit-fallback", trace=trace
    )
    fail_fast_ms = (time.perf_counter() - started) * 1000
    assert fallback.status_code == 503
    assert fallback.json()["detail"]["status"] == "PAYMENT_PENDING_RETRY"
    assert fail_fast_ms < 150
    health_started = time.perf_counter()
    health = payments_client.get("/health")
    health_ms = (time.perf_counter() - health_started) * 1000
    trace.http("consultar_health", "GET", "/health", None, health, health_ms)
    assert health.status_code == 200
    assert health_ms < 150

    events = relevant_log_events(
        caplog, {"payment_provider_timeout", "circuit_breaker_state_changed"}
    )
    assert {event["event"] for event in events} == {
        "payment_provider_timeout",
        "circuit_breaker_state_changed",
    }
    show_evidence(
        "cascada-01-pasarela-lenta",
        before={"circuit": "CLOSED", "metrics": before_metrics},
        after={
            "circuit": circuit_breaker.state,
            "timeout_responses": timeout_responses,
            "fallback_status": fallback.status_code,
            "fallback_payment_status": fallback.json()["detail"]["status"],
            "fail_fast_ms": round(fail_fast_ms, 2),
            "health_status": health.status_code,
            "health_ms": round(health_ms, 2),
            "metrics_after_timeouts": after_timeouts_metrics,
            "metrics_after_fallback": metrics_snapshot(payments_client),
            "log_events": events,
        },
        trace=trace,
    )
    time.sleep(mock_provider_module.TIMEOUT_SECONDS + 1)
