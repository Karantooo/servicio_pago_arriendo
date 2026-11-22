import asyncio
import json
import logging
import threading

import pytest
from fastapi import HTTPException

from pagos import resilience
from pagos.observability import (
    JsonLogFormatter,
    bulkhead_rejections_total,
    request_id_context,
)
from pagos.resilience import Bulkhead


def test_bulkhead_retains_slot_until_timed_out_provider_finishes(monkeypatch):
    bulkhead = Bulkhead(capacity=1)
    monkeypatch.setattr(resilience, "bulkhead", bulkhead)
    monkeypatch.setattr(resilience, "TIMEOUT_SECONDS", 0.02)
    started = threading.Event()
    finish = threading.Event()

    def slow_provider():
        started.set()
        assert finish.wait(timeout=2)
        return "finished"

    async def verify_capacity():
        with pytest.raises(TimeoutError):
            await resilience.invoke_provider_call(slow_provider)
        assert started.is_set()
        with pytest.raises(HTTPException) as rejected:
            await resilience.invoke_provider_call(lambda: "should not run")
        assert rejected.value.status_code == 429
        assert rejected.value.detail == "Bulkhead capacity exceeded"
        assert (
            bulkhead_rejections_total.labels(service="payments-api")._value.get()
            >= 1
        )

    try:
        asyncio.run(verify_capacity())
    finally:
        finish.set()

    async def verify_released_after_completion():
        await asyncio.sleep(0.02)
        assert await resilience.invoke_provider_call(lambda: "next") == "next"

    asyncio.run(verify_released_after_completion())


def test_json_log_formatter_includes_request_correlation_fields():
    token = request_id_context.set("payments-test-request")
    try:
        record = logging.LogRecord(
            name="payments.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Bulkhead rejected request",
            args=(),
            exc_info=None,
        )
        record.event = "bulkhead_rejected"
        record.duration_ms = 0.0
        record.details = {"capacity": 1}

        payload = json.loads(JsonLogFormatter().format(record))
    finally:
        request_id_context.reset(token)

    assert payload["service"] == "payments-api"
    assert payload["request_id"] == "payments-test-request"
    assert payload["event"] == "bulkhead_rejected"
    assert payload["duration_ms"] == 0.0
    assert payload["details"] == {"capacity": 1}
