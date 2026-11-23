import time

from pagos.resilience import circuit_breaker

from experimentos.helpers import ExperimentTrace, show_evidence
from experimentos.cascada.helpers import (
    charge,
    metrics_snapshot,
    relevant_log_events,
    set_mode,
)


def test_down_provider_opens_breaker_and_rejects_later_calls_quickly(
    payments_client, caplog
):
    trace = ExperimentTrace()
    before_metrics = metrics_snapshot(payments_client)
    trace.add("estado_inicial", circuit="CLOSED", metrics=before_metrics)
    set_mode(payments_client, "DOWN", trace=trace)
    failed = []
    for index in range(circuit_breaker.threshold):
        response = charge(
            payments_client, f"down-circuit-{index}", trace=trace
        )
        failed.append(response.status_code)
        assert response.status_code == 503
        assert response.json()["detail"]["status"] == "UNKNOWN"

    opened_metrics = metrics_snapshot(payments_client)
    trace.add(
        "circuito_tras_fallas",
        circuit=circuit_breaker.state,
        metrics=opened_metrics,
    )
    assert circuit_breaker.state == "OPEN"
    assert opened_metrics["circuit_breaker"]["OPEN"] == 1

    started = time.perf_counter()
    fallback = charge(
        payments_client, "down-circuit-fallback", trace=trace
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert fallback.status_code == 503
    assert fallback.json()["detail"]["status"] == "PAYMENT_PENDING_RETRY"
    assert elapsed_ms < 150
    metrics_after = payments_client.get("/metrics").text
    assert metric_value(
        metrics_after,
        "http_request_duration_seconds_count",
        endpoint="POST /payments/charge",
        service="payments-api",
        status_code="503",
    ) >= circuit_breaker.threshold + 1

    events = relevant_log_events(
        caplog, {"payment_provider_unavailable", "circuit_breaker_state_changed"}
    )
    assert {event["event"] for event in events} == {
        "payment_provider_unavailable",
        "circuit_breaker_state_changed",
    }
    show_evidence(
        "cascada-02-pasarela-caida",
        before={"circuit": "CLOSED", "metrics": before_metrics},
        after={
            "circuit": circuit_breaker.state,
            "provider_failure_statuses": failed,
            "fallback_status": fallback.status_code,
            "fallback_payment_status": fallback.json()["detail"]["status"],
            "fail_fast_ms": round(elapsed_ms, 2),
            "metrics_before_fallback": opened_metrics,
            "metrics_after_fallback": metrics_snapshot(payments_client),
            "log_events": events,
        },
        trace=trace,
    )


def metric_value(metrics, name, **labels):
    label_text = ",".join(
        f'{key}="{value}"' for key, value in sorted(labels.items())
    )
    target = f"{name}{{{label_text}}}"
    for line in metrics.splitlines():
        if line.startswith(target + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0
