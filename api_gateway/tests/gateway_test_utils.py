import base64
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
_TEST_SECRET = b"gateway-test-secret-change-me-32-bytes"


def _encode(value):
    return (
        base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )


def test_token():
    header = _encode({"alg": "HS256", "typ": "JWT", "kid": "gateway-test"})
    payload = _encode(
        {
            "iss": "servicio-pago-arriendo-test",
            "aud": "servicio-pago-arriendo",
            "sub": "gateway-failure-test",
            "roles": ["cliente"],
        }
    )
    unsigned = f"{header}.{payload}".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(_TEST_SECRET, unsigned, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def request(path, token=None, timeout=6):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{BASE_URL}{path}", headers=headers)
    try:
        response = urlopen(req, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def wait_for_gateway(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health_status, _ = request("/health", timeout=2)
            rentals_status, _ = request(
                "/__test/rentals-failure-count", timeout=2
            )
            if health_status == 200 and rentals_status == 200:
                return
        except URLError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Gateway y backends mock no disponibles en {BASE_URL}")
