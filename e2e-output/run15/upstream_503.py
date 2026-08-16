"""E2E-001 Run 15 failing upstream — always answers 503 (Content-Length 0).

Used to prove the relay forwards a live (non-2xx) upstream response
byte-identically (relay 9097 -> this 9092).
"""

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # silence
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9092
    HTTPServer(("127.0.0.1", port), H).serve_forever()