"""Contract Tests: startup-order.sh driving a real, pinned Docker Engine (dind) through the Docker CLI."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "startup-order.sh"
# Workload image pulled into the dind engine; any image with sh and sleep will do.
WORKLOAD = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"


class Engine:
    def __init__(self, url):
        self.host = "tcp://" + url.removeprefix("http://")

    def docker(self, *args):
        return subprocess.run(
            ["docker", "--host", self.host, *args], check=True, capture_output=True, text=True, timeout=300
        ).stdout.strip()

    def create(self, name, level=None, *options, healthy_after=None):
        labels = [] if level is None else ["--label", f"startup-order={level}"]
        if healthy_after is None:
            command = ["sleep", "3600"]
        else:
            command = ["sh", "-c", f"sleep {healthy_after}; touch /tmp/ok; sleep 3600"]
            options = (*options, "--health-cmd", "test -f /tmp/ok", "--health-interval", "1s")
        self.docker("run", "--detach", "--init", "--name", name, *labels, *options, WORKLOAD, *command)

    def started_at(self, name):
        return self.docker("inspect", "--format", "{{.State.StartedAt}}", name)

    def running(self):
        return sorted(self.docker("ps", "--format", "{{.Names}}").split())

    def startup_order(self, tmp_path, *args):
        result = subprocess.run(
            ["sh", str(SCRIPT), *args],
            env={
                "PATH": os.environ["PATH"],
                "DOCKER_HOST": self.host,
                "STARTUP_ORDER_DIR": str(tmp_path / "startup"),
                "STARTUP_ORDER_SETTLE": "0",
                "STARTUP_ORDER_LEVEL_TIMEOUT": "60",
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout


@pytest.fixture
def engine(upstream):
    engine = Engine(upstream("docker").url)
    engine.docker("pull", "--quiet", WORKLOAD)
    yield engine
    names = engine.docker("ps", "--all", "--quiet").split()
    if names:
        engine.docker("rm", "--force", *names)


def started_in_order(engine, *names):
    times = [engine.started_at(n) for n in names]
    return times == sorted(times)


@pytest.mark.contract
def test_shutdown_then_boot_brings_containers_back_level_by_level(engine, tmp_path):
    engine.create("edge", 0)
    engine.create("db", 1, healthy_after=3)
    engine.create("app", 2)
    engine.create("unlabelled")
    engine.create("sidecar", 3)
    engine.create("pinned", "skip")
    pinned_started = engine.started_at("pinned")

    engine.startup_order(tmp_path, "stop")
    assert engine.running() == ["pinned"]

    log = engine.startup_order(tmp_path, "run")
    assert engine.running() == ["app", "db", "edge", "pinned", "sidecar", "unlabelled"]
    assert started_in_order(engine, "edge", "db", "app", "sidecar")
    assert started_in_order(engine, "db", "unlabelled", "sidecar")
    assert engine.docker("inspect", "--format", "{{.State.Health.Status}}", "db") == "healthy"
    assert "run: level 1 ready after" in log
    assert engine.started_at("pinned") == pinned_started


@pytest.mark.contract
def test_boot_restarts_what_dockerd_already_started_above_level_1(engine, tmp_path):
    engine.create("db", 1)
    engine.create("app", 2)
    engine.create("unlabelled")
    engine.create("sidecar", 3)
    db_started, app_started = engine.started_at("db"), engine.started_at("app")

    log = engine.startup_order(tmp_path, "run")
    assert "run: stopping level 2 for reorder:" in log
    assert engine.running() == ["app", "db", "sidecar", "unlabelled"]
    assert engine.started_at("db") == db_started
    assert engine.started_at("app") != app_started
    assert started_in_order(engine, "app", "sidecar")
    assert started_in_order(engine, "unlabelled", "sidecar")


@pytest.mark.contract
def test_plan_reads_the_engine_and_changes_nothing(engine, tmp_path):
    engine.create("db", 1)
    engine.create("app", 2)
    app_started = engine.started_at("app")

    log = engine.startup_order(tmp_path, "plan")
    assert "run: starting level 1: db" in log
    assert "run: starting level 2: app" in log
    assert engine.started_at("app") == app_started
