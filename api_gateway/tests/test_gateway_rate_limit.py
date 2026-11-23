import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from gateway_test_utils import request, test_token, wait_for_gateway


class GatewayRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wait_for_gateway()

    def test_burst_is_limited_per_client(self):
        token = test_token()
        time.sleep(1.1)

        with ThreadPoolExecutor(max_workers=30) as pool:
            results = list(
                pool.map(
                    lambda _: request("/api/profile", token),
                    range(30),
                )
            )

        status_codes = [status_code for status_code, _ in results]
        self.assertIn(200, status_codes)
        self.assertIn(429, status_codes)
