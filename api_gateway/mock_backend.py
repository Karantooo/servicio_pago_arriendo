import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


failure_requests = 0


class MockBackend(BaseHTTPRequestHandler):
    def do_GET(self):
        global failure_requests
        service = os.environ.get("SERVICE_NAME", "mock")
        if self.path == "/health":
            self._send(200, {"service": service, "status": "ok"})
            return
        if service == "rentals" and self.path == "/test/failure-count":
            self._send(200, {"failure_requests": failure_requests})
            return
        if service == "profile" and self.path == "/profile":
            self._send(200, {"client_id": "test-client", "name": "Cliente de prueba"})
            return
        if service == "rentals" and self.path == "/rentals":
            self._send(200, {"active": 2, "items": [{"id": "rental-1", "status": "active"}]})
            return
        if service == "rentals" and self.path == "/slow":
            time.sleep(3)
            self._send(200, {"status": "delayed"})
            return
        if service == "rentals" and self.path == "/failure":
            failure_requests += 1
            self._send(503, {"error": "backend_unavailable"})
            return
        self._send(404, {"error": "not_found"})

    def log_message(self, format, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 8080), MockBackend).serve_forever()
