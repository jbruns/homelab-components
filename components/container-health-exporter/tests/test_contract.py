import subprocess
import time

import pytest

from container_health_exporter.exporter import Collector, Config

BUSYBOX = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
HEALTH = ["--health-interval", "1s", "--health-retries", "1", "--health-start-period", "0s"]


def in_dind(docker, *args):
    host = docker.url.replace("http://", "tcp://")
    return subprocess.run(["docker", "--host", host, *args], check=True, capture_output=True, text=True).stdout


_created = set()


@pytest.fixture
def dind_containers(upstream):
    """Labelled containers inside the pinned dind (once per session), with and without health checks."""
    docker = upstream("docker")
    if docker.url in _created:
        return docker
    in_dind(docker, "pull", "--quiet", BUSYBOX)
    stack = ["--label", "com.docker.compose.project=contract", "--label", "com.docker.compose.service=web"]

    def run(name, *options, start=True):
        command = "run" if start else "create"
        detach = ["--detach"] if start else []
        in_dind(docker, command, *detach, "--name", name, *stack, *options, BUSYBOX, "sleep", "3600")

    run("healthy", "--label", "tier=critical", "--health-cmd", "true", *HEALTH)
    run("unhealthy", "--health-cmd", "false", *HEALTH)
    run("starting", "--health-cmd", "true", "--health-interval", "1h", "--health-start-period", "1h")
    run("plain")
    run("disabled", "--no-healthcheck")
    run("stopped-checked", "--health-cmd", "true", start=False)
    run("stopped-plain", start=False)
    run("ignored", "--label", "example.ignore=yes", "--health-cmd", "true", *HEALTH)
    run("0123456789ab_web", "--health-cmd", "true", *HEALTH)
    _created.add(docker.url)
    settled = "healthy\nunhealthy\nhealthy\n"
    deadline = time.monotonic() + 60
    while health_of(docker, "healthy", "unhealthy", "0123456789ab_web") != settled:
        assert time.monotonic() < deadline, "dind containers did not reach their health states"
        time.sleep(1)
    return docker


def health_of(docker, *names):
    return in_dind(docker, "inspect", "--format", "{{.State.Health.Status}}", *names)


def scrape(docker, **config):
    return Collector(Config(docker_host=docker.url, port=0, timeout=10, **config)).collect()


def healthy_lines(metrics):
    return sorted(line for line in metrics.splitlines() if line.startswith("container_healthy{"))


@pytest.mark.contract
def test_reports_health_checks_of_containers_a_real_docker_runs(dind_containers):
    config = {"container_labels": ("tier",), "ignore_label": "example.ignore"}
    expected = [
        'container_healthy{container="0123456789ab_web",stack="contract",tier=""} 1',
        'container_healthy{container="healthy",stack="contract",tier="critical"} 1',
        'container_healthy{container="starting",stack="contract",tier=""} 0',
        'container_healthy{container="stopped-checked",stack="contract",tier=""} 0',
        'container_healthy{container="unhealthy",stack="contract",tier=""} 0',
    ]
    metrics = scrape(dind_containers, **config)
    assert "container_health_up 1\n" in metrics
    assert healthy_lines(metrics) == expected


@pytest.mark.contract
def test_reports_a_container_left_under_a_temporary_compose_name_in_a_real_docker(dind_containers):
    metrics = scrape(dind_containers)
    leftovers = [line for line in metrics.splitlines() if line.startswith("container_leftover_name{")]
    assert leftovers == ['container_leftover_name{container="0123456789ab_web",stack="contract",service="web"} 1']
