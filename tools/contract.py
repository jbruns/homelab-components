"""Contract Test harness: a pytest plugin that starts a Component's upstreams in Docker.

A test marked `contract` asks for an upstream by its name in the Component's `upstreams.yaml`:

    @pytest.mark.contract
    def test_reads_the_queue(upstream):
        sonarr = upstream("sonarr")
        sonarr.url   # http://127.0.0.1:<published port>
        sonarr.env   # env from upstreams.yaml, e.g. the API key the container was started with
        sonarr.container   # container ID, e.g. to `docker exec` test data into the upstream

An upstream declared without a `port` is a CLI (e.g. restic): nothing starts, and `.command(*docker_options)` is
the argv that runs its image once with its env, e.g. `restic.command("--volume", f"{repo}:/repo") + ["init"]`.

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
    url: str | None
    env: dict
    container: str | None

    def command(self, *docker_options):
        """argv running this upstream's image once, with its env and any extra `docker run` options."""
        return ["docker", "run", "--rm", "--interactive", *_env_options(self.env), *docker_options, self.image]


def _docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def remove_container(container):
    """Remove a container with its anonymous volumes (e.g. dind's /var/lib/docker), so test runs leave none behind."""
    subprocess.run(["docker", "rm", "--force", "--volumes", container], capture_output=True)


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


def _env_options(env):
    return [arg for key, value in env.items() for arg in ("--env", f"{key}={value}")]


def _start(spec):
    image = contract_image(spec, os.environ)
    if spec.port is None:
        _docker("pull", "--quiet", image)
        return None, RunningUpstream(name=spec.name, image=image, url=None, env=dict(spec.env), container=None)
    options = _env_options(spec.env)
    if spec.privileged:
        options.append("--privileged")
    container = _docker("run", "--detach", "--publish", f"127.0.0.1::{spec.port}", *options, image)
    try:
        host_port = _docker("port", container, f"{spec.port}/tcp").splitlines()[0].rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{host_port}"
        _wait_ready(url + spec.ready_path, container)
    except BaseException:
        remove_container(container)
        raise
    return container, RunningUpstream(name=spec.name, image=image, url=url, env=dict(spec.env), container=container)


@pytest.fixture(scope="session")
def _running_upstreams():
    running = {}
    yield running
    for container, _ in running.values():
        if container:
            remove_container(container)


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
