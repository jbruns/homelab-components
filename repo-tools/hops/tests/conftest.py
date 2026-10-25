"""Shared fixtures for hops tests: a fixture repository and a fake Host that records SSH argv.

hops is installed (uv pip install -e .); this puts the tests directory on sys.path for hops_fakes.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hops_fakes import STACKS, FakeHost, hosts_yaml, write  # noqa: E402


def pytest_collection_modifyitems(config, items):
    if "live" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="talks to the real Host; run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A consuming repo with the STACKS fixture: hosts.yaml with Host `host-a` (stack root tn) (not a git
    repository)."""
    root = tmp_path / "repo"
    write(root, STACKS)
    return root


@pytest.fixture
def plain_host(repo) -> Path:
    """The fixture repo with Host host-a declared without capabilities: a plain doco-cd Host."""
    write(repo, {"hosts.yaml": hosts_yaml(capabilities="[]")})
    return repo


@pytest.fixture
def host_config(tmp_path):
    from hops.config import HostConfig

    return HostConfig(host="tn.example", user="agent", key=Path("/keys/id"), control_dir=tmp_path / "cm")


@pytest.fixture
def fake_host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def runner(host_config, fake_host):
    """A real Runner whose ssh process is fake_host."""
    from hops.runner import Runner

    return Runner(host_config, process=fake_host)


@dataclass
class Outcome:
    code: int
    out: str
    err: str


@pytest.fixture
def hops(repo, runner, capsys):
    """Run the hops CLI against the fixture repository and fake Host: hops("status", "--json").

    TrueNAS is not configured unless `truenas=` passes a client (e.g. one on hops_fakes.FakeTrueNAS).
    """
    from hops import cli

    env = {k: v for k, v in os.environ.items() if not k.startswith("HOPS_")}

    def run(*argv: str, truenas=None) -> Outcome:
        ctx = cli.Context(repo_root=repo, runner=runner, truenas=truenas, env=env, cwd=repo)
        code = cli.main(list(argv), ctx=ctx)
        captured = capsys.readouterr()
        return Outcome(code, captured.out, captured.err)

    return run
