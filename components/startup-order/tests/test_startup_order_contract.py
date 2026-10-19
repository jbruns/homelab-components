"""Contract Tests: startup-order.sh driving a real, pinned Docker CLI and Engine.

The script runs inside the pinned docker:dind container, so both its `docker` CLI and its engine are the pinned (or
Canary) version, and its busybox userland checks the script stays POSIX sh.
"""

import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "startup-order.sh"
INSTALLED = "/usr/local/bin/startup-order.sh"
# Workload image pulled into the dind engine; any image with sh and sleep will do.
WORKLOAD = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"


def host_docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=300).stdout.strip()


class Engine:
    def __init__(self, url):
        published = f"127.0.0.1:{url.rsplit(':', 1)[1]}->2375/tcp"
        listing = host_docker("ps", "--format", "{{.ID}} {{.Ports}}").splitlines()
        self.container = next(line.split()[0] for line in listing if published in line)
        host_docker("cp", str(SCRIPT), f"{self.container}:{INSTALLED}")

    def docker(self, *args):
        return host_docker("exec", self.container, "docker", *args)

    def create(self, name, level=None, *options, healthy_after=None, command=None):
        labels = [] if level is None else ["--label", f"startup-order={level}"]
        if healthy_after is None:
            command = command or ["sleep", "3600"]
        else:
            command = ["sh", "-c", f"sleep {healthy_after}; touch /tmp/ok; sleep 3600"]
            options = (*options, "--health-cmd", "test -f /tmp/ok", "--health-interval", "1s")
        self.docker("run", "--detach", "--init", "--name", name, *labels, *options, WORKLOAD, *command)

    def started_at(self, name):
        return self.docker("inspect", "--format", "{{.State.StartedAt}}", name)

    def running(self):
        return sorted(self.docker("ps", "--format", "{{.Names}}").split())

    def status(self, name):
        return self.docker("inspect", "--format", "{{.State.Status}}", name)

    def startup_order(self, *args, expect=0, level_timeout=60):
        settings = ["--env", "STARTUP_ORDER_SETTLE=0", "--env", f"STARTUP_ORDER_LEVEL_TIMEOUT={level_timeout}"]
        result = subprocess.run(
            ["docker", "exec", *settings, self.container, "sh", INSTALLED, *args],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == expect, result.stdout + result.stderr
        return result.stdout


@pytest.fixture
def engine(upstream):
    engine = Engine(upstream("docker").url)
    engine.docker("pull", "--quiet", WORKLOAD)
    yield engine
    names = engine.docker("ps", "--all", "--quiet").split()
    if names:
        engine.docker("rm", "--force", *names)
    host_docker("exec", engine.container, "rm", "-rf", "/var/lib/startup-order")


def started_in_order(engine, *names):
    times = [engine.started_at(n) for n in names]
    return times == sorted(times)


@pytest.mark.contract
def test_shutdown_then_boot_brings_containers_back_level_by_level(engine):
    engine.create("edge", 0)
    engine.create("db", 1, healthy_after=3)
    engine.create("app", 2)
    engine.create("unlabelled")
    engine.create("sidecar", 3)
    engine.create("pinned", "skip")
    pinned_started = engine.started_at("pinned")

    engine.startup_order("stop")
    assert engine.running() == ["pinned"]

    log = engine.startup_order("run")
    assert engine.running() == ["app", "db", "edge", "pinned", "sidecar", "unlabelled"]
    assert started_in_order(engine, "edge", "db", "app", "sidecar")
    assert started_in_order(engine, "db", "unlabelled", "sidecar")
    assert engine.docker("inspect", "--format", "{{.State.Health.Status}}", "db") == "healthy"
    assert "run: level 1 ready after" in log
    assert engine.started_at("pinned") == pinned_started


@pytest.mark.contract
def test_boot_restarts_what_dockerd_already_started_above_level_1(engine):
    engine.create("db", 1)
    engine.create("app", 2)
    engine.create("unlabelled")
    engine.create("sidecar", 3)
    db_started, app_started = engine.started_at("db"), engine.started_at("app")

    log = engine.startup_order("run")
    assert "run: stopping level 2 for reorder:" in log
    assert engine.running() == ["app", "db", "sidecar", "unlabelled"]
    assert engine.started_at("db") == db_started
    assert engine.started_at("app") != app_started
    assert started_in_order(engine, "app", "sidecar")
    assert started_in_order(engine, "unlabelled", "sidecar")


@pytest.mark.contract
def test_plan_reads_the_engine_and_changes_nothing(engine):
    engine.create("db", 1)
    engine.create("app", 2)
    app_started = engine.started_at("app")

    log = engine.startup_order("plan")
    assert "run: starting level 1: db" in log
    assert "run: starting level 2: app" in log
    assert engine.started_at("app") == app_started


@pytest.mark.contract
def test_a_crash_looping_target_is_not_counted_as_running(engine):
    engine.create("db", 1)
    engine.create("crashy", 2, "--restart=always", command=["sh", "-c", "exit 1"])
    deadline = time.monotonic() + 30
    while engine.status("crashy") != "restarting":
        assert time.monotonic() < deadline, "crashy never reached restarting"
        time.sleep(0.2)
    assert "crashy" in engine.running()

    log = engine.startup_order("run", expect=1, level_timeout=10)
    assert "run: not started: crashy" in log
    assert "run: done" not in log
    assert host_docker("exec", engine.container, "cat", "/var/lib/startup-order/state").split() == ["crashy", "db"]
