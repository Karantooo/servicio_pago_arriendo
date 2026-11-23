import time
import unittest

from gateway_test_utils import request, test_token, wait_for_gateway


class GatewayTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wait_for_gateway()

    def test_slow_backend_exceeds_response_header_timeout(self):
        started = time.monotonic()
        status_code, _ = request("/api/rentals-slow", test_token(), timeout=6)
        elapsed = time.monotonic() - started

        self.assertEqual(status_code, 500)
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 4.5)
