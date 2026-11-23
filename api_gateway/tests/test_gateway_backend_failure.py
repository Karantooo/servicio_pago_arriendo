import json
import unittest

from gateway_test_utils import request, test_token, wait_for_gateway


class GatewayBackendFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wait_for_gateway()

    def test_backend_503_opens_circuit_breaker_after_threshold(self):
        token = test_token()

        unauthenticated, _ = request("/api/rentals-unstable")
        self.assertEqual(unauthenticated, 401)

        stats_status, stats_body = request("/__test/rentals-failure-count")
        self.assertEqual(stats_status, 200)
        failures_before = json.loads(stats_body)["failure_requests"]

        for _ in range(4):
            status_code, _ = request("/api/rentals-unstable", token)
            self.assertEqual(status_code, 500)

        stats_status, stats_body = request("/__test/rentals-failure-count")
        self.assertEqual(stats_status, 200)
        failures_after_open = json.loads(stats_body)["failure_requests"]
        self.assertEqual(failures_after_open, failures_before + 4)

        status_code, _ = request("/api/rentals-unstable", token)
        self.assertEqual(status_code, 500)
        stats_status, stats_body = request("/__test/rentals-failure-count")
        self.assertEqual(stats_status, 200)
        self.assertEqual(
            json.loads(stats_body)["failure_requests"],
            failures_after_open,
            "El circuit breaker debería rechazar la quinta llamada sin invocar el backend.",
        )

        dashboard_status, dashboard_body = request("/api/dashboard", token)
        self.assertEqual(dashboard_status, 200)
        self.assertEqual(
            set(json.loads(dashboard_body)),
            {"profile", "rentals"},
        )
