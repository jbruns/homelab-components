"""Stand-ins and launchers shared by this Component's tests."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def unused_port():
    return _free_port()


def _env(env):
    return {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC), **env}


@pytest.fixture
def run_exporter():
    """Run `python -m sabnzbd_status_exporter <args>` to completion with the given env."""

    def run(*args, **env):
        return subprocess.run(
            [sys.executable, "-m", "sabnzbd_status_exporter", *args],
            env=_env(env),
            capture_output=True,
            text=True,
            timeout=10,
        )

    return run


@pytest.fixture
def sabnzbd():
    """A stand-in SABnzbd: answers every request with `.status` and `.body`, and records the request paths."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            server.paths.append(self.path)
            body = server.body if isinstance(server.body, bytes) else json.dumps(server.body).encode()
            self.send_response(server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.status, server.body, server.paths = 200, {"status": {"servers": []}}, []
    server.url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


class Exporter:
    def __init__(self, process, port):
        self.process, self.port = process, port

    def get(self, path="/metrics"):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, response.headers["Content-Type"], response.read().decode()

    def metrics(self):
        return self.get()[2]

    def output(self):
        self.process.terminate()
        return self.process.communicate(timeout=10)


@pytest.fixture
def start_exporter():
    """Start `python -m sabnzbd_status_exporter` with the given env and wait until it listens."""
    started = []

    def start(**env):
        port = int(env.setdefault("PORT", str(_free_port())))
        process = subprocess.Popen(
            [sys.executable, "-m", "sabnzbd_status_exporter"],
            env=_env(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return Exporter(process, port)
            except OSError:
                if process.poll() is not None:
                    return Exporter(process, port)
                time.sleep(0.05)
        pytest.fail("exporter did not listen within 10s")

    yield start
    for process in started:
        process.kill()
        process.communicate()
