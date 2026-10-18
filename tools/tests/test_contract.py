import subprocess

from tools.contract import _start, remove_container
from tools.upstreams import Upstream

# Any small local image works; this is the template's pinned upstream, already pulled by its Contract Test.
IMAGE = "traefik/whoami:v1.11.0@sha256:200689790a0a0ea48ca45992e0450bc26ccab5307375b41c84dfc4f2475937ab"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def test_removing_an_upstream_container_also_removes_its_anonymous_volumes():
    container = docker("create", "--volume", "/data", IMAGE)
    volume = docker("inspect", "--format", "{{range .Mounts}}{{.Name}}{{end}}", container)
    assert volume
    remove_container(container)
    assert docker("volume", "ls", "--quiet", "--filter", f"name=^{volume}$") == ""


def test_a_started_upstream_names_its_container_so_tests_can_seed_it():
    repository, rest = IMAGE.split(":", 1)
    tag, digest = rest.split("@")
    spec = Upstream(name="whoami", repository=repository, tag=tag, digest=digest, port=80, ready_path="/health")
    container, running = _start(spec)
    try:
        assert running.container == container
        assert docker("inspect", "--format", "{{.State.Running}}", running.container) == "true"
    finally:
        remove_container(container)
