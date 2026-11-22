import logging
import os
from threading import Lock
from typing import Literal


FailureMode = Literal["NORMAL", "FAIL_NEXT", "ALWAYS_FAIL"]
_MODES: dict[str, FailureMode] = {
    "NORMAL": "NORMAL",
    "FAIL_NEXT": "FAIL_NEXT",
    "ALWAYS_FAIL": "ALWAYS_FAIL",
}
logger = logging.getLogger(__name__)
_lock = Lock()
_mode: FailureMode = "NORMAL"


def is_failure_injection_enabled() -> bool:
    enabled = os.getenv("DELIVERIES_ENABLE_FAILURE_INJECTION", "false")
    return enabled.strip().lower() == "true"


def set_failure_mode(mode: str) -> FailureMode:
    selected = _MODES.get(mode.strip().upper())
    if selected is None:
        raise ValueError(f"Modo de fallo no válido: {mode}")

    global _mode
    with _lock:
        _mode = selected
    return selected


def should_fail_delivery_creation() -> bool:
    if not is_failure_injection_enabled():
        return False

    global _mode
    with _lock:
        mode = _mode
        if mode == "FAIL_NEXT":
            _mode = "NORMAL"

    if mode == "NORMAL":
        return False

    logger.warning(
        "Simulated delivery failure injected",
        extra={"failure_mode": mode, "operation": "create_delivery"},
    )
    return True
