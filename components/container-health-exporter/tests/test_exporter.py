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
EXPORTER = [sys.executable, "-m", "container_health_exporter"]


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
        self.delay = 0.0  # seconds every request takes
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append(self.path)
                time.sleep(fake.delay)
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

    def listed(self):
        return [p for p in self.requests if p.endswith("/containers/json?all=1")]

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
        'container_healthy{container="cache",stack="media"} 0',
        'container_healthy{container="db",stack="media"} 0',
        'container_healthy{container="web",stack="media"} 1',
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
        'container_healthy{container="checked",stack=""} 0',
        'container_healthy{container="created",stack=""} 0',
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
    assert 'container_healthy{container="0123456789ab_sonarr",stack="media"} 1' in metrics


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
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack=""} 1']


def test_escapes_label_values(docker, start_exporter):
    docker.containers = [container("a", "web", "Up (healthy)", **{"com.docker.compose.project": 'a"b\\c\nd'})]
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack="a\\"b\\\\c d"} 1']


def test_keeps_the_home_ops_exposition_when_configured_like_home_ops(docker, start_exporter):
    labels = {"com.docker.compose.project": "media", "com.docker.compose.service": "sonarr", "tier": "info"}
    docker.containers = [container("a", "0123456789ab_sonarr", "Up 1 minute (healthy)", **labels)]
    env = {"DOCKER_HOST": docker.address, "CONTAINER_LABELS": "tier", "IGNORE_LABEL": "startup-order=skip"}
    assert start_exporter(env).metrics() == (
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


def test_copies_configured_container_labels_onto_container_healthy(docker, start_exporter):
    docker.containers = [
        container("a", "web", "Up (healthy)", **{"tier": "critical", "com.example.team": "media"}),
        container("b", "db", "Up (unhealthy)"),
    ]
    env = {"DOCKER_HOST": docker.address, "CONTAINER_LABELS": "tier, com.example.team"}
    assert healthy_lines(start_exporter(env).metrics()) == [
        'container_healthy{container="db",stack="",tier="",com_example_team=""} 0',
        'container_healthy{container="web",stack="",tier="critical",com_example_team="media"} 1',
    ]


@pytest.mark.parametrize("name", ["container", "stack", "com.docker.compose.project,stack", "team,team", "a.b,a_b"])
def test_refuses_container_labels_that_would_clash_with_its_own_or_each_other(name):
    result = subprocess.run(
        EXPORTER, env={"PYTHONPATH": str(SRC), "CONTAINER_LABELS": name}, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert "CONTAINER_LABELS" in result.stderr


def test_calls_the_docker_api_at_its_pinned_version(docker, start_exporter):
    docker.containers = [container("s1", "checked", "Exited (0) 1 hour ago", state="exited")]
    docker.healthchecks = {"s1": ["CMD", "true"]}
    start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert docker.requests and all(p.startswith("/v1.44/") for p in docker.requests)


def test_accepts_docker_host_as_a_tcp_url(docker, start_exporter):
    docker.containers = [container("a", "web", "Up (healthy)")]
    metrics = start_exporter({"DOCKER_HOST": f"tcp://{docker.address}/"}).metrics()
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack=""} 1']


def test_answers_404_off_metrics(docker, start_exporter):
    exporter = start_exporter({"DOCKER_HOST": docker.address})
    with pytest.raises(urllib.error.HTTPError) as error:
        exporter.get("/nope")
    assert error.value.code == 404


def test_ignores_containers_carrying_the_configured_ignore_label(docker, start_exporter):
    docker.containers = [
        container("a", "0123456789ab_job", "Up (healthy)", **{"startup-order": "skip"}),
        container("b", "web", "Up (healthy)", **{"startup-order": "2"}),
    ]
    metrics = start_exporter({"DOCKER_HOST": docker.address, "IGNORE_LABEL": "startup-order=skip"}).metrics()
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack=""} 1']
    assert "container_leftover_name{" not in metrics


def test_ignores_containers_carrying_the_configured_ignore_label_with_any_value(docker, start_exporter):
    docker.containers = [
        container("a", "job", "Up (healthy)", **{"example.ignore": ""}),
        container("b", "web", "Up (healthy)"),
    ]
    metrics = start_exporter({"DOCKER_HOST": docker.address, "IGNORE_LABEL": "example.ignore"}).metrics()
    assert healthy_lines(metrics) == ['container_healthy{container="web",stack=""} 1']


def test_ignores_no_container_by_default(docker, start_exporter):
    docker.containers = [container("a", "job", "Up (healthy)", **{"startup-order": "skip"})]
    metrics = start_exporter({"DOCKER_HOST": docker.address}).metrics()
    assert healthy_lines(metrics) == ['container_healthy{container="job",stack=""} 1']


def healthcheck(port):
    env = {"PYTHONPATH": str(SRC), "PORT": str(port)}
    return subprocess.run([*EXPORTER, "healthcheck"], env=env, timeout=10).returncode


def test_healthcheck_passes_only_while_the_exporter_serves(docker, start_exporter):
    assert healthcheck(free_port()) == 1
    exporter = start_exporter({"DOCKER_HOST": docker.address})
    assert healthcheck(exporter.port) == 0


def test_healthcheck_leaves_the_docker_api_alone(docker, start_exporter):
    exporter = start_exporter({"DOCKER_HOST": docker.address})
    docker.requests.clear()
    assert exporter.get("/healthz") == "ok\n"
    assert healthcheck(exporter.port) == 0
    assert docker.requests == []


def test_rejects_unknown_arguments():
    result = subprocess.run([*EXPORTER, "nope"], env={"PYTHONPATH": str(SRC)}, capture_output=True, timeout=10)
    assert result.returncode == 2


def scrape_concurrently(exporter, count, stagger=0.1):
    """Start `count` scrapes `stagger` seconds apart; return (body, seconds since the first started) for each."""
    results = [None] * count
    started = time.monotonic()

    def scrape(i):
        body = exporter.metrics()
        results[i] = (body, time.monotonic() - started)

    threads = [Thread(target=scrape, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
        time.sleep(stagger)
    for thread in threads:
        thread.join(30)
    return results


def test_overlapping_scrapes_share_one_docker_collection(docker, start_exporter):
    docker.containers = [container("a", "web", "Up (healthy)")]
    exporter = start_exporter({"DOCKER_HOST": docker.address})
    docker.requests.clear()
    docker.delay = 1.0
    results = scrape_concurrently(exporter, 4)
    assert len(docker.listed()) == 1
    bodies = {body for body, _ in results}
    assert len(bodies) == 1 and "container_health_up 1\n" in bodies.pop()
    finished = [seconds for _, seconds in results]
    assert max(finished) - min(finished) < 0.5


def test_a_scrape_waiting_on_a_slow_collection_gives_up_after_the_timeout(docker, start_exporter):
    docker.containers = [container(f"s{i}", f"stopped{i}", "Exited (0)", state="exited") for i in range(4)]
    docker.healthchecks = {f"s{i}": ["CMD", "true"] for i in range(4)}
    exporter = start_exporter({"DOCKER_HOST": docker.address, "TIMEOUT": "1"})
    docker.healthchecks = {f"s{i}": ["CMD", "true"] for i in range(4, 8)}
    docker.containers = [container(f"s{i}", f"stopped{i}", "Exited (0)", state="exited") for i in range(4, 8)]
    docker.delay = 0.8  # each request within TIMEOUT, the whole collection (list + 4 inspects) well beyond it
    (first, first_done), (waiter, waiter_done) = scrape_concurrently(exporter, 2, stagger=0.2)
    assert "container_health_up 0\n" in waiter
    assert "container_healthy{" not in waiter
    assert waiter_done < 3.0 < first_done
    assert len(docker.listed()) == 2  # startup probe + one collection
