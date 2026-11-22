import asyncio
import threading

import pytest
from fastapi import HTTPException

from pagos import resilience
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

    try:
        asyncio.run(verify_capacity())
    finally:
        finish.set()

    async def verify_released_after_completion():
        await asyncio.sleep(0.02)
        assert await resilience.invoke_provider_call(lambda: "next") == "next"

    asyncio.run(verify_released_after_completion())
