import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def fake_arr():
    """Start stand-ins for a Sonarr/Radarr API: fake_arr(records) -> server; server.url, server.requests."""
    servers = []

    def start(records=None, api_key="key"):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append((self.path, self.headers.get("X-Api-Key")))
                if self.headers.get("X-Api-Key") != api_key:
                    code, body = 401, b""
                elif not self.path.startswith("/api/v3/queue?"):
                    code, body = 404, b""
                else:
                    code, body = 200, json.dumps({"page": 1, "records": records}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        server.url = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
