import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from template_exporter.exporter import Config, collect, healthcheck, serve


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_whoami():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            server.requests += 1
            body = json.dumps({"hostname": "whoami-1"}).encode()
            self.send_response(200 if self.path == "/api" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def url(server):
    return f"http://127.0.0.1:{server.server_port}"


def test_reports_the_upstream_up_with_its_hostname(fake_whoami):
    metrics = collect(Config(upstream_url=url(fake_whoami), port=0, timeout=2))
    assert "template_upstream_up 1\n" in metrics
    assert 'template_upstream_info{hostname="whoami-1"} 1\n' in metrics


def test_reports_the_upstream_down_when_it_does_not_answer():
    metrics = collect(Config(upstream_url=f"http://127.0.0.1:{free_port()}", port=0, timeout=2))
    assert "template_upstream_up 0\n" in metrics
    assert "template_upstream_info" not in metrics


def test_healthcheck_passes_only_while_metrics_are_served(fake_whoami):
    config = Config(upstream_url=url(fake_whoami), port=free_port(), timeout=2)
    assert healthcheck(config) == 1
    server = serve(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert healthcheck(config) == 0
    finally:
        server.shutdown()


def test_healthcheck_leaves_the_upstream_alone(fake_whoami):
    config = Config(upstream_url=url(fake_whoami), port=free_port(), timeout=2)
    server = serve(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert healthcheck(config) == 0
    finally:
        server.shutdown()
    assert fake_whoami.requests == 0


def test_configuration_comes_from_the_environment_with_documented_defaults():
    assert Config.from_env({}) == Config(upstream_url="http://whoami", port=9100, timeout=10.0)
    assert Config.from_env({"TEMPLATE_UPSTREAM_URL": "http://h:81/", "PORT": "9200", "TIMEOUT": "3"}) == Config(
        upstream_url="http://h:81", port=9200, timeout=3.0
    )
