"""E2E-001 Run 9 mock upstream — echoes GET/POST byte-identically.

Pattern reused from e2e-output/run7/mock_upstream.py (same behavior, port
parameterized via argv[1]) so the pass-through check stays deterministic.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def _respond(self):
        body = json.dumps({"ok": True, "upstream": "mock"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream-Marker", "mock")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n) if n else b""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream-Marker", "mock")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9081
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()