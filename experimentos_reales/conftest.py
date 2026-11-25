import base64
import hashlib
import hmac
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _admin_token() -> str:
    jwk_path = ROOT / "api_gateway" / "jwk-symmetric.json"
    jwk = json.loads(jwk_path.read_text(encoding="utf-8"))["keys"][0]
    key = base64.urlsafe_b64decode(jwk["k"] + "=" * (-len(jwk["k"]) % 4))
    header = _b64url(
        json.dumps(
            {"alg": "HS256", "typ": "JWT", "kid": jwk["kid"]},
            separators=(",", ":"),
        ).encode()
    )
    payload = _b64url(
        json.dumps(
            {
                "iss": "servicio-pago-arriendo-test",
                "aud": "servicio-pago-arriendo",
                "roles": ["admin"],
                "exp": int(time.time()) + 3600,
            },
            separators=(",", ":"),
        ).encode()
    )
    signature = hmac.new(
        key, f"{header}.{payload}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def pytest_addoption(parser):
    parser.addoption(
        "--iterations",
        type=int,
        default=50,
        help="cantidad de ejecuciones de cada experimento (predeterminado: 50)",
    )
    parser.addoption(
        "--base-url",
        default="http://127.0.0.1:18080",
        help="URL pública del gateway levantado con Docker Compose",
    )


def pytest_configure(config):
    if config.getoption("--iterations") < 1:
        raise pytest.UsageError("--iterations debe ser mayor que cero")
    config._experiment_run_metrics = []


def pytest_generate_tests(metafunc):
    if "experiment_iteration" in metafunc.fixturenames:
        iterations = metafunc.config.getoption("--iterations")
        metafunc.parametrize(
            "experiment_iteration",
            range(1, iterations + 1),
            ids=lambda iteration: f"ejecucion-{iteration:02d}",
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    state = getattr(item, "_experiment_timing", {"elapsed_seconds": 0, "result": "passed"})
    state["elapsed_seconds"] += report.duration
    if report.outcome != "passed":
        state["result"] = report.outcome
    item._experiment_timing = state
    if report.when == "teardown" and hasattr(item, "callspec"):
        metric = {
            "test": item.originalname,
            "iteration": item.callspec.params["experiment_iteration"],
            "elapsed_ms": round(state["elapsed_seconds"] * 1000, 2),
            "result": state["result"],
        }
        item.config._experiment_run_metrics.append(metric)
        print("RUN_METRIC_JSON " + json.dumps(metric, sort_keys=True))


def pytest_sessionfinish(session, exitstatus):
    grouped = defaultdict(list)
    for metric in session.config._experiment_run_metrics:
        grouped[metric["test"]].append(metric)
    for test, metrics in sorted(grouped.items()):
        durations = sorted(metric["elapsed_ms"] for metric in metrics)
        p95_index = max(0, math.ceil(len(durations) * 0.95) - 1)
        summary = {
            "test": test,
            "executions": len(metrics),
            "passed": sum(metric["result"] == "passed" for metric in metrics),
            "failed": sum(metric["result"] != "passed" for metric in metrics),
            "min_ms": durations[0],
            "average_ms": round(sum(durations) / len(durations), 2),
            "p95_ms": durations[p95_index],
            "max_ms": durations[-1],
        }
        print("EXPERIMENT_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))


@pytest.fixture(scope="session")
def api(pytestconfig):
    admin_key = os.getenv("EXPERIMENTS_ADMIN_API_KEY")
    if not admin_key:
        pytest.exit("Falta EXPERIMENTS_ADMIN_API_KEY; ejecuta experimentos_reales\\run.ps1.")
    client = httpx.Client(
        base_url=pytestconfig.getoption("--base-url").rstrip("/"),
        timeout=httpx.Timeout(30.0, connect=5.0),
        headers={
            "Authorization": f"Bearer {_admin_token()}",
            "API-Key": admin_key,
            "Content-Type": "application/json",
        },
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session", autouse=True)
def configure_equipment_tariff(api):
    response = api.put(
        "/api/tariffs/1",
        json={"dailyRate": 1000, "deposit": 0},
    )
    assert response.status_code == 200, (
        f"No se pudo preparar la tarifa del equipo 1: HTTP {response.status_code}"
    )
