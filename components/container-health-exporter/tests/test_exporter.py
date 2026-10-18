import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

SRC = Path(__file__).parents[1] / "src"
EXPORTER = [sys.executable, str(SRC / "container_health_exporter" / "exporter.py")]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def container(cid, name, status="", state="running", **labels):
    return {"Id": cid, "Names": [f"/{name}"], "State": state, "Status": status, "Labels": labels}


class FakeDocker:
    """A Docker Engine API stand-in: GET /containers/json and /containers/<id>/json, with or without /v1.xx."""

    def __init__(self):
        self.containers = []
        self.healthchecks = {}  # container ID -> Config.Healthcheck.Test
        self.failing = set()  # paths (without version) answering 500
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append(self.path)
                path = re.sub(r"^/v[\d.]+(?=/)", "", self.path)
                if path in fake.failing:
                    return self._reply(500, {"message": "boom"})
                if path == "/containers/json?all=1":
                    return self._reply(200, fake.containers)
                inspect = re.fullmatch(r"/containers/([^/]+)/json", path)
                if inspect and inspect[1] in fake.healthchecks:
                    test = fake.healthchecks[inspect[1]]
                    config = {"Healthcheck": {"Test": test}} if test is not None else {}
                    return self._reply(200, {"Id": inspect[1], "Config": config})
                self._reply(404, {"message": "no such container"})

            def _reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def address(self):
        return f"127.0.0.1:{self.server.server_port}"

    def inspected(self):
        return [p for p in self.requests if re.search(r"/containers/[^/]+/json$", p)]


@pytest.fixture
def docker():
    fake = FakeDocker()
    yield fake
    fake.server.shutdown()


class Exporter:
    def __init__(self, port, process):
        self.port, self.process = port, process

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return response.read().decode()

    def metrics(self):
        return self.get("/metrics")


@pytest.fixture
def start_exporter():
    """Start the exporter process with `env`; it is stopped after the test."""
    started = []

    def start(env, args=()):
        port = free_port()
        full_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC), "PORT": str(port), **env}
        process = subprocess.Popen([*EXPORTER, *args], env=full_env)
        started.append(process)
        exporter = Exporter(port, process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                exporter.get("/metrics")
                return exporter
            except OSError:
                time.sleep(0.05)
        pytest.fail("exporter did not start serving")

    yield start
    for process in started:
        process.terminate()
        process.wait(5)


def healthy_lines(metrics):
    return sorted(line for line in metrics.splitlines() if line.startswith("container_healthy{"))


def test_reports_running_containers_by_the_state_their_health_check_reports(docker, start_exporter):
    docker.containers = [
        container("a", "web", "Up 2 hours (healthy)", **{"com.docker.compose.project": "media", "tier": "critical"}),
        container("b", "db", "Up 1 minute (unhealthy)", **{"com.docker.compose.project": "media", "tier": "warning"}),
        container("c", "cache", "Up 5 seconds (health: starting)", **{"com.docker.compose.project": "media"}),
        container("d", "plain", "Up 3 days", **{"com.docker.compose.project": "media"}),
    ]
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert healthy_lines(metrics) == [
        'container_healthy{container="cache",stack="media",tier=""} 0',
        'container_healthy{container="db",stack="media",tier="warning"} 0',
        'container_healthy{container="web",stack="media",tier="critical"} 1',
    ]
    assert docker.inspected() == []


def test_reports_stopped_containers_with_a_health_check_as_0_and_inspects_each_once(docker, start_exporter):
    docker.containers = [
        container("s1", "checked", "Exited (0) 1 hour ago", state="exited"),
        container("s2", "unchecked", "Exited (0) 1 hour ago", state="exited"),
        container("s3", "disabled", "Exited (1) 1 hour ago", state="exited"),
        container("s4", "created", "Created", state="created"),
    ]
    docker.healthchecks = {"s1": ["CMD", "true"], "s2": None, "s3": ["NONE"], "s4": ["CMD-SHELL", "true"]}
    exporter = start_exporter({"DOCKER_HOST": docker.address})
    exporter.metrics()
    metrics = exporter.metrics()
    assert healthy_lines(metrics) == [
        'container_healthy{container="checked",stack="",tier=""} 0',
        'container_healthy{container="created",stack="",tier=""} 0',
    ]
    assert sorted(re.sub(r"^/v[\d.]+", "", p) for p in docker.inspected()) == [
        f"/containers/{cid}/json" for cid in ("s1", "s2", "s3", "s4")
    ]


def test_reinspects_a_container_id_that_came_back_after_disappearing(docker, start_exporter):
    stopped = container("s1", "checked", "Exited (0) 1 hour ago", state="exited")
    docker.healthchecks = {"s1": ["CMD", "true"]}
    exporter = start_exporter({"DOCKER_HOST": docker.address})
    docker.containers = [stopped]
    exporter.metrics()
    docker.containers = []
    exporter.metrics()
    docker.containers = [stopped]
    exporter.metrics()
    assert len(docker.inspected()) == 2


def test_reports_containers_left_under_a_temporary_compose_name(docker, start_exporter):
    labels = {"com.docker.compose.project": "media", "com.docker.compose.service": "sonarr"}
    docker.containers = [
        container("a", "0123456789ab_sonarr", "Up 1 minute (healthy)", **labels),
        container("b", "sonarr", "Up 1 minute (healthy)", **labels),
        container("c", "0123456789ab", "Up 1 minute", **labels),
    ]
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    leftovers = [line for line in metrics.splitlines() if line.startswith("container_leftover_name{")]
    assert leftovers == ['container_leftover_name{container="0123456789ab_sonarr",stack="media",service="sonarr"} 1']
    assert 'container_healthy{container="0123456789ab_sonarr",stack="media",tier=""} 1' in metrics


def test_reports_the_docker_api_down_and_keeps_serving(start_exporter):
    exporter = start_exporter({"DOCKER_HOST": f"127.0.0.1:{free_port()}", "TIMEOUT": "2"})
    assert "container_health_up 0\n" in exporter.metrics()
    assert "container_health_up 0\n" in exporter.metrics()


def test_reports_the_docker_api_down_when_it_answers_with_an_error(docker, start_exporter):
    docker.failing.add("/containers/json?all=1")
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert "container_health_up 0\n" in metrics
    assert healthy_lines(metrics) == []


def test_skips_a_container_it_cannot_inspect_and_reports_the_rest(docker, start_exporter):
    docker.containers = [
        container("gone", "gone", "Exited (0) 1 hour ago", state="exited"),
        container("a", "web", "Up 2 hours (healthy)"),
    ]
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert "container_health_up 1\n" in metrics
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack="",tier=""} 1']


def test_escapes_label_values(docker, start_exporter):
    docker.containers = [container("a", "web", "Up (healthy)", **{"com.docker.compose.project": 'a"b\\c\nd'})]
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack="a\\"b\\\\c d",tier=""} 1']


def test_serves_the_whole_exposition(docker, start_exporter):
    labels = {"com.docker.compose.project": "media", "com.docker.compose.service": "sonarr", "tier": "info"}
    docker.containers = [container("a", "0123456789ab_sonarr", "Up 1 minute (healthy)", **labels)]
    assert start_exporter({"DOCKER_HOST": docker.address}).metrics() == (
        "# HELP container_health_up 1 if the Docker API answered.\n"
        "# TYPE container_health_up gauge\n"
        "container_health_up 1\n"
        "# HELP container_healthy 1 while the container's health check reports healthy.\n"
        "# TYPE container_healthy gauge\n"
        'container_healthy{container="0123456789ab_sonarr",stack="media",tier="info"} 1\n'
        "# HELP container_leftover_name 1 for a container still named <id>_<name> by an interrupted Compose recreate.\n"
        "# TYPE container_leftover_name gauge\n"
        'container_leftover_name{container="0123456789ab_sonarr",stack="media",service="sonarr"} 1\n'
    )


def test_answers_404_off_metrics(docker, start_exporter):
    exporter = start_exporter({"DOCKER_HOST": docker.address})
    with pytest.raises(urllib.error.HTTPError) as error:
        exporter.get("/nope")
    assert error.value.code == 404


def test_ignores_containers_labelled_startup_order_skip(docker, start_exporter):
    docker.containers = [
        container("a", "0123456789ab_job", "Up (healthy)", **{"startup-order": "skip"}),
        container("b", "web", "Up (healthy)", **{"startup-order": "2"}),
    ]
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack="",tier=""} 1']
    assert "container_leftover_name{" not in metrics
