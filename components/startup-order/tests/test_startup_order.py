import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "startup-order.sh"
FAKE_DOCKER = Path(__file__).resolve().parent / "fake_docker.py"


class FakeHost:
    """The script run against fake_docker.py: containers in, docker start/stop calls and logs out."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "startup"
        self.state_file = tmp_path / "docker.json"
        self.calls_file = tmp_path / "calls"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_DOCKER}" "$@"\n')
        docker.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DOCKER_STATE": str(self.state_file),
            "FAKE_DOCKER_CALLS": str(self.calls_file),
            "STARTUP_ORDER_DIR": str(self.dir),
            "STARTUP_ORDER_SETTLE": "0",
        }
        self.containers()

    def containers(self, *containers, up=True):
        self.state_file.write_text(json.dumps({"up": up, "containers": list(containers)}))

    def run(self, *args, **env):
        return subprocess.run(
            ["sh", str(SCRIPT), *args], env={**self.env, **env}, capture_output=True, text=True, timeout=60
        )

    def calls(self):
        return self.calls_file.read_text().splitlines() if self.calls_file.exists() else []

    def running(self):
        return sorted(c["name"] for c in json.loads(self.state_file.read_text())["containers"] if c.get("running"))


def container(name, level=None, running=True, **extra):
    labels = {} if level is None else {"startup-order": str(level)}
    return {"name": name, "labels": labels, "running": running, **extra}


@pytest.fixture
def host(tmp_path):
    if shutil.which("flock") is None:
        pytest.skip("needs flock (util-linux), as on the Host")
    return FakeHost(tmp_path)


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_rejects_an_unknown_command_with_usage(host):
    result = host.run("bogus")
    assert result.returncode == 2
    assert "usage:" in result.stderr and "stop|start|run|plan" in result.stderr


def test_stop_records_running_containers_then_stops_them_from_the_highest_level_down(host):
    host.containers(
        container("edge", 0),
        container("db", 1),
        container("app", 2),
        container("exporter", 3),
        container("idle", 2, running=False),
    )
    assert host.run("stop").returncode == 0
    assert host.calls() == ["stop exporter", "stop app", "stop db", "stop edge"]
    assert (host.dir / "state").read_text().split() == ["app", "db", "edge", "exporter"]
    log = (host.dir / "shutdown.log").read_text()
    assert re.search(r"stop: recorded +4 containers", log)
    assert "stop: level 3: exporter" in log
    assert "stop: done" in log


def test_stop_with_nothing_running_records_nothing(host):
    assert host.run("stop").returncode == 0
    assert not (host.dir / "state").exists()
    assert re.search(r"stop: recorded +0 containers", (host.dir / "shutdown.log").read_text())


def test_stop_keeps_names_recorded_by_an_earlier_shutdown(host):
    host.dir.mkdir()
    (host.dir / "state").write_text("earlier\n")
    host.containers(container("app", 2))
    host.run("stop")
    assert (host.dir / "state").read_text().split() == ["app", "earlier"]


def test_stop_never_touches_skipped_containers(host):
    host.containers(container("app", 2), container("pinned", "skip"))
    host.run("stop")
    assert host.calls() == ["stop app"]
    assert host.running() == ["pinned"]


def test_run_restarts_recorded_containers_in_level_order(host):
    host.dir.mkdir()
    (host.dir / "state").write_text("edge\ndb\napp\nexporter\n")
    host.containers(
        container("exporter", 3, running=False),
        container("app", 2, running=False),
        container("db", 1, running=False),
        container("edge", 0, running=False),
    )
    result = host.run("run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert host.calls() == ["start edge", "start db", "start app", "start exporter"]
    assert host.running() == ["app", "db", "edge", "exporter"]
    assert "run: 4 targets (0 already running, state file: yes)" in result.stdout
    assert "run: level 0 ready after 0s" in result.stdout
    assert "run: done" in result.stdout
    assert not (host.dir / "state").exists()


def test_run_restarts_what_docker_already_started_above_level_1_so_it_comes_up_in_order(host):
    host.containers(
        container("edge", 0),
        container("db", 1),
        container("app", 2),
        container("exporter", 3),
    )
    result = host.run("run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert host.calls() == [
        "stop exporter",
        "stop app",
        "start edge",
        "start db",
        "start app",
        "start exporter",
    ]
    assert "run: stopping level 3 for reorder: exporter" in result.stdout


def test_unlabelled_and_out_of_range_containers_start_at_level_2(host):
    host.containers(
        container("sidecar", 3, running=False),
        container("unlabelled", running=False),
        container("odd", "9", running=False),
        container("padded", "03", running=False),
        container("db", 1, running=False),
    )
    host.dir.mkdir()
    (host.dir / "state").write_text("sidecar\nunlabelled\nodd\npadded\ndb\n")
    host.run("run")
    assert host.calls() == ["start db", "start unlabelled odd padded", "start sidecar"]


def test_run_never_touches_skipped_containers_even_when_recorded(host):
    host.dir.mkdir()
    (host.dir / "state").write_text("pinned\napp\n")
    host.containers(container("pinned", "skip", running=False), container("app", 2, running=False))
    host.run("run")
    assert host.calls() == ["start app"]
    assert host.running() == ["app"]


def test_run_leaves_stopped_containers_that_were_not_recorded_alone(host):
    host.containers(container("app", 2, running=False))
    result = host.run("run")
    assert host.calls() == []
    assert "run: 0 targets (0 already running, state file: no)" in result.stdout


def test_run_drops_recorded_names_that_no_longer_exist(host):
    host.dir.mkdir()
    (host.dir / "state").write_text("gone\napp\n")
    host.containers(container("app", 2, running=False))
    result = host.run("run")
    assert result.returncode == 0
    assert host.calls() == ["start app"]


def test_run_warns_and_moves_on_when_a_level_is_not_healthy_in_time(host):
    host.dir.mkdir()
    (host.dir / "state").write_text("db\napp\n")
    host.containers(
        container("db", 1, running=False, healthcheck=True, healthy_on_start=False),
        container("app", 2, running=False),
    )
    result = host.run("run", STARTUP_ORDER_LEVEL_TIMEOUT="0")
    assert result.returncode == 0
    assert "run: WARNING level 1 not ready after 0s: db; continuing" in result.stdout
    assert host.calls() == ["start db", "start app"]


def test_plan_reports_what_run_would_do_and_changes_nothing(host):
    host.dir.mkdir()
    (host.dir / "state").write_text("db\n")
    host.containers(container("db", 1, running=False), container("app", 2))
    result = host.run("plan")
    assert result.returncode == 0
    assert "run: stopping level 2 for reorder: app" in result.stdout
    assert "run: starting level 1: db" in result.stdout
    assert "run: starting level 2: app" in result.stdout
    assert host.calls() == []
    assert (host.dir / "state").read_text() == "db\n"


def test_run_gives_up_when_docker_never_answers(host):
    host.containers(up=False)
    result = host.run("run", STARTUP_ORDER_DOCKER_WAIT="0")
    assert result.returncode == 1
    assert "run: Docker not ready after 0s, giving up" in result.stdout


def test_run_refuses_to_start_while_another_process_holds_the_lock(host):
    host.dir.mkdir()
    with open(host.dir / "run.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = host.run("run")
    assert result.returncode == 1
    assert "another run holds" in result.stdout


def test_run_proceeds_over_a_lock_file_left_by_a_run_that_is_gone(host):
    recorded(host, "app")
    (host.dir / "run.lock").write_text("")
    host.containers(container("app", 2, running=False))
    result = host.run("run")
    assert result.returncode == 0, result.stdout
    assert host.calls() == ["start app"]


def test_concurrent_runs_never_overlap_and_never_remove_each_others_lock(host):
    recorded(host, "db")
    (host.dir / "run.lock").write_text("")
    host.containers(container("db", 1, running=False))
    env = {**host.env, "FAKE_DOCKER_SLOW": "start=2"}
    contenders = [
        subprocess.Popen(["sh", str(SCRIPT), "run"], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)
    ]
    results = [(p.wait(timeout=60), p.stdout.read()) for p in contenders]
    assert sorted(rc for rc, _ in results) == [0, 1, 1, 1], results
    assert host.calls() == ["start db"]
    assert (host.dir / "run.lock").exists()


def test_run_stops_when_terminated_and_frees_the_lock(host):
    host.containers(up=False)
    process = subprocess.Popen(["sh", str(SCRIPT), "run"], env=host.env, stdout=subprocess.PIPE, text=True)
    wait_for(lambda: (host.dir / "run.lock").exists())
    process.terminate()
    assert process.wait(timeout=30) == 143
    host.containers()
    assert host.run("run").returncode == 0


def test_a_run_terminated_mid_reorder_leaves_what_it_stopped_for_the_next_run(host):
    host.containers(container("db", 1), container("app", 2))
    env = {**host.env, "FAKE_DOCKER_SLOW": "stop=3"}
    process = subprocess.Popen(["sh", str(SCRIPT), "run"], env=env, stdout=subprocess.PIPE, text=True)
    wait_for(lambda: "stop app" in host.calls())
    process.terminate()
    assert process.wait(timeout=30) == 143
    assert host.running() == ["db"]

    result = host.run("run")
    assert result.returncode == 0, result.stdout
    assert host.running() == ["app", "db"]
    assert not (host.dir / "state").exists()


def test_run_requires_flock(tmp_path):
    host = FakeHost(tmp_path)
    bin_dir = tmp_path / "bin"
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT), "run"], env={**host.env, "PATH": str(bin_dir)}, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "startup-order: requires flock" in result.stderr


def recorded(host, *names):
    host.dir.mkdir(exist_ok=True)
    (host.dir / "state").write_text("".join(n + "\n" for n in names))


def test_reads_levels_from_the_label_named_in_the_environment(host):
    recorded(host, "db", "app")
    host.containers(
        {"name": "db", "labels": {"boot.tier": "0"}, "running": False},
        {"name": "app", "labels": {"boot.tier": "1", "startup-order": "skip"}, "running": False},
    )
    host.run("run", STARTUP_ORDER_LABEL="boot.tier")
    assert host.calls() == ["start db", "start app"]


def test_unlabelled_containers_take_the_configured_default_level(host):
    recorded(host, "unlabelled", "db")
    host.containers(container("unlabelled", running=False), container("db", 1, running=False))
    host.run("run", STARTUP_ORDER_DEFAULT_LEVEL="0")
    assert host.calls() == ["start unlabelled", "start db"]


def test_levels_run_up_to_the_configured_maximum(host):
    recorded(host, "top", "mid", "base")
    host.containers(
        container("top", 5, running=False), container("mid", 4, running=False), container("base", 0, running=False)
    )
    host.run("run", STARTUP_ORDER_MAX_LEVEL="5")
    assert host.calls() == ["start base", "start mid", "start top"]
    host.run("stop", STARTUP_ORDER_MAX_LEVEL="5")
    assert host.calls()[3:] == ["stop top", "stop mid", "stop base"]


def test_boot_restarts_only_levels_from_the_configured_reorder_level(host):
    host.containers(container("db", 1), container("app", 2), container("exporter", 3))
    host.run("run", STARTUP_ORDER_REORDER_FROM="3")
    assert host.calls() == ["stop exporter", "start db", "start app", "start exporter"]


@pytest.mark.parametrize(
    "env",
    [
        {"STARTUP_ORDER_MAX_LEVEL": "x"},
        {"STARTUP_ORDER_DEFAULT_LEVEL": "4"},
        {"STARTUP_ORDER_REORDER_FROM": "-1"},
        {"STARTUP_ORDER_LEVEL_TIMEOUT": "soon"},
    ],
)
def test_rejects_invalid_configuration_before_touching_anything(host, env):
    host.containers(container("app", 2))
    result = host.run("run", **env)
    assert result.returncode == 2
    assert f"startup-order: invalid {next(iter(env))}" in result.stderr
    assert host.calls() == []


@pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid (util-linux), as on the Host")
def test_start_runs_in_the_background_and_logs_to_last_log(host):
    host.dir.mkdir()
    (host.dir / "state").write_text("app\n")
    host.containers(container("app", 2, running=False))
    assert host.run("start").returncode == 0
    for _ in range(100):
        log = host.dir / "last.log"
        if log.exists() and "run: done" in log.read_text():
            break
        subprocess.run(["sleep", "0.1"])
    assert "run: done" in (host.dir / "last.log").read_text()
    assert host.calls() == ["start app"]


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
def test_script_is_posix_sh_and_shellcheck_clean():
    result = subprocess.run(["shellcheck", "--shell=sh", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
