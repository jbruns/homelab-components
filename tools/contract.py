"""Contract Test harness: a pytest plugin that starts a Component's upstreams in Docker.

A test marked `contract` asks for an upstream by its name in the Component's `upstreams.yaml`:

    @pytest.mark.contract
    def test_reads_the_queue(upstream):
        sonarr = upstream("sonarr")
        sonarr.url   # http://127.0.0.1:<published port>
        sonarr.env   # env from upstreams.yaml, e.g. the API key the container was started with

The container runs from the pinned image, or from the image a Canary names (tools/upstreams.contract_image).
It starts once per test session and is removed at the end.
"""

import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.upstreams import contract_image, load_upstreams

READY_TIMEOUT = float(os.environ.get("CONTRACT_READY_TIMEOUT", "180"))


@dataclass(frozen=True)
class RunningUpstream:
    name: str
    image: str
    url: str
    env: dict


def _docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def _wait_ready(url, container):
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5):
                return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return
        except OSError:
            pass
        time.sleep(1)
    logs = subprocess.run(["docker", "logs", "--tail", "50", container], capture_output=True, text=True)
    pytest.fail(f"upstream at {url} not ready after {READY_TIMEOUT:.0f}s; last logs:\n{logs.stdout}{logs.stderr}")


def _start(spec):
    image = contract_image(spec, os.environ)
    options = [arg for key, value in spec.env.items() for arg in ("--env", f"{key}={value}")]
    if spec.privileged:
        options.append("--privileged")
    container = _docker("run", "--detach", "--publish", f"127.0.0.1::{spec.port}", *options, image)
    try:
        host_port = _docker("port", container, f"{spec.port}/tcp").splitlines()[0].rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{host_port}"
        _wait_ready(url + spec.ready_path, container)
    except BaseException:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)
        raise
    return container, RunningUpstream(name=spec.name, image=image, url=url, env=dict(spec.env))


@pytest.fixture(scope="session")
def _running_upstreams():
    running = {}
    yield running
    for container, _ in running.values():
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)


@pytest.fixture
def upstream(request, _running_upstreams):
    """Start (once per session) and return the named upstream of the Component this test belongs to."""
    if request.node.get_closest_marker("contract") is None:
        pytest.fail("tests that start an upstream must be marked @pytest.mark.contract")
    component_dir = next(p for p in Path(request.path).parents if (p / "upstreams.yaml").is_file())
    declared = {spec.name: spec for spec in load_upstreams(component_dir)}

    def start(name):
        key = (component_dir.name, name)
        if key not in _running_upstreams:
            if name not in declared:
                pytest.fail(f"{component_dir}/upstreams.yaml declares no upstream {name!r}")
            _running_upstreams[key] = _start(declared[name])
        return _running_upstreams[key][1]

    return start
