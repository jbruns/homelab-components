"""Repos with more than one Host, and Hosts without TrueNAS or monitoring."""

import shutil
import subprocess
import textwrap

import pytest

from conftest import HOSTS_YAML, SOPS_ENV, commit, configure, git, write
from test_validate import FIXTURES, docker_compose_available, messages, run_cli

# A second Host: nested stack root, a renamed doco-cd Stack, no capabilities, no monitoring or Traefik.
SITE = "sites/host-b"
MULTI_HOSTS_YAML = (
    HOSTS_YAML
    + f"""
      host-b:
        stack_root: {SITE}
        deployer: doco-cd
        capabilities: []
        doco_cd: {{stack: deployer}}
"""
)
SITE_FILES = {
    "hosts.yaml": MULTI_HOSTS_YAML,
    f"{SITE}/.doco-cd.yml": "auto_discovery:\n  depth: 1\n",
    f"{SITE}/web/compose.yaml": "include:\n  - nginx/service.yaml\n",
    f"{SITE}/web/nginx/service.yaml": "services:\n  nginx:\n    image: nginx:1\n    environment: ['NAME=${NAME}']\n",
    f"{SITE}/web/.sops.env": "NAME=ENC[x]\n" + SOPS_ENV,
    f"{SITE}/web/env.template": "NAME=\n",
}


@pytest.fixture
def multi(validate, repo):
    """The fixture repo with a second Host, validate pointed at both."""
    write(repo, SITE_FILES)
    commit(repo, "second host")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    configure(validate, repo)
    return validate


def test_every_host_is_checked(multi, capsys):
    multi.check_secrets()
    multi.check_env()
    multi.check_cloudflare()
    out = capsys.readouterr().out
    assert messages(multi) == ""
    assert "env checked: web" in out and "env checked: app" in out
    assert f"cloudflare: host-b has no {SITE}/edge/traefik/service.yaml; skipped" in out


def test_env_reports_the_second_hosts_stack(multi, repo):
    write(repo, {f"{SITE}/web/env.template": ""})
    multi.check_env()
    assert messages(multi) == "web: variables used but missing from env.template: NAME"


def test_layout_holds_for_every_host(multi, repo):
    write(repo, {f"{SITE}/web/compose.yaml": "include:\n  - nginx.yaml\n", f"{SITE}/web/nginx.yaml": "services: {}\n"})
    commit(repo)
    multi.check_secrets()
    assert f"{SITE}/web/nginx.yaml" in messages(multi)


def test_host_option_selects_one_host(multi, repo, capsys):
    configure(multi, repo, host="host-b")
    multi.check_env()
    out = capsys.readouterr().out
    assert "env checked: web" in out and "env checked: app" not in out


def test_monitoring_skips_a_host_without_prometheus(multi, repo, runtime, capsys):
    configure(multi, repo, host="host-b")
    multi.check_monitoring()
    assert runtime.runs == [] and messages(multi) == ""
    assert f"monitoring: host-b has no {SITE}/monitoring/prometheus; skipped" in capsys.readouterr().out


def test_monitoring_checks_declared_containers_per_host(multi, repo, runtime):
    write(repo, {"tools/declared-containers/declared_containers.py": "import sys; print(*sys.argv[1:]); exit(1)\n"})
    write(repo, {f"{SITE}/monitoring/prometheus/service.yaml": "services:\n  prometheus:\n    image: prom/p:2\n"})
    multi.check_monitoring()
    assert multi.failures == [f"--check --root {repo} --host host-a", f"--check --root {repo} --host host-b"]
    assert "prom/p:2" in [r.image for r in runtime.runs]


def test_declared_containers_command_is_the_installed_tool(validate, repo, monkeypatch):
    monkeypatch.setattr(validate, "OVERRIDES", {})
    monkeypatch.setattr(validate.shutil, "which", lambda name: f"/bin/{name}")
    host = validate.HOSTS[0]
    assert validate.generator_command("declared-containers", host) == ["/bin/homelab-declared-containers"]
    assert validate.generator_command("dashboards", host) is None


def isolation(multi, repo, monkeypatch, files):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, files)
    commit(repo)
    monkeypatch.setenv("VALIDATE_BASE", base)
    multi.check_isolation()
    return messages(multi)


@pytest.mark.parametrize(
    "files,expected",
    [
        ({f"{SITE}/deployer/x.yml": "x", "tn/app/x.yml": "x"}, ""),
        ({"tn/doco-cd/x.yml": "x", f"{SITE}/web/x.yml": "x"}, ""),
        ({f"{SITE}/_baseline.yaml": "x", "tn/app/x.yml": "x"}, ""),
        (
            {f"{SITE}/deployer/x.yml": "x", f"{SITE}/web/x.yml": "x"},
            f"isolation: {SITE}/deployer changes must ship in their own PR (also changed: web)",
        ),
        (
            {f"{SITE}/_baseline.yaml": "x", f"{SITE}/web/x.yml": "x"},
            f"isolation: {SITE}/_baseline.yaml changes must ship in their own PR (also changed: web)",
        ),
    ],
)
def test_isolation_is_per_host(multi, repo, monkeypatch, files, expected):
    assert isolation(multi, repo, monkeypatch, files) == expected


def test_isolation_compares_against_the_default_branch(multi, repo, monkeypatch):
    write(repo, {"hosts.yaml": "default_branch: trunk\n" + textwrap.dedent(MULTI_HOSTS_YAML).lstrip("\n")})
    commit(repo)
    configure(multi, repo)
    monkeypatch.delenv("VALIDATE_BASE", raising=False)
    multi.check_isolation()
    assert messages(multi).startswith("isolation: cannot diff against origin/trunk")


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_compose_holds_each_host_to_its_own_baseline(multi, repo, capsys):
    shutil.copy(FIXTURES / "_baseline.yaml", repo / "tn/_baseline.yaml")
    shutil.copy(FIXTURES / "_baseline.yaml", repo / SITE / "_baseline.yaml")
    write(repo, {f"{SITE}/deployer/compose.yaml": "services:\n  deployer:\n    image: d:1\n"})
    commit(repo)
    multi.check_compose()
    out = messages(multi)
    assert f"web: Baseline: nginx: must extend {SITE}/_baseline.yaml" in out
    # The doco-cd Stack carries the Baseline inline instead of extending it.
    assert "deployer: must extend" not in out
    printed = capsys.readouterr().out
    assert "Services on host-a meet it" in printed and "Services on host-b meet it" in printed


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_a_legacy_volume_of_an_unselected_host_is_not_reported_gone(multi, repo, monkeypatch):
    configure(multi, repo, host="host-b", legacy_volumes={"media_config"})
    multi.check_compose()
    assert "legacy name" not in messages(multi)


def test_a_legacy_stack_of_an_unselected_host_is_not_reported_gone(multi, repo):
    write(repo, {"tn/old/docker-compose.yaml": "services: {}\n"})
    commit(repo)
    configure(multi, repo, host="host-b", legacy_stacks={"old"})
    multi.check_secrets()
    assert multi.failures == []
    configure(multi, repo, legacy_stacks={"old"})
    multi.check_secrets()
    assert multi.failures == []


def test_a_missing_declared_containers_command_fails_cleanly(validate, repo, runtime, monkeypatch):
    monkeypatch.setattr(validate, "OVERRIDES", {})
    monkeypatch.setattr(validate, "generator_command", lambda name, host: ["no-such-generator-cmd"])
    validate.check_monitoring()
    assert "declared containers: cannot run no-such-generator-cmd" in messages(validate)


# --- a repo whose only Host has no TrueNAS, monitoring or Traefik ------------------------------


def test_a_plain_host_repo_passes(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(
        root,
        {
            "hosts.yaml": "version: 1\nhosts:\n  pi: {stack_root: stacks, deployer: doco-cd, capabilities: []}\n",
            ".doco-cd.yml": "auto_discovery:\n  depth: 1\n",
            "stacks/app/compose.yaml": "include:\n  - web/service.yaml\n",
            "stacks/app/web/service.yaml": "services:\n  web:\n    image: nginx:1\n",
            "stacks/app/.sops.env": SOPS_ENV,
            "stacks/app/env.template": "",
        },
    )
    commit(root, "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    r = run_cli("--root", str(root), "secrets", "env", "cloudflare", "isolation", "monitoring")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pi has no stacks/monitoring/prometheus; skipped" in r.stdout
    assert "pi has no stacks/edge/traefik/service.yaml; skipped" in r.stdout


def test_cli_unknown_host(repo):
    r = run_cli("--root", str(repo), "--host", "nope", "secrets")
    assert r.returncode == 1 and "no Host 'nope'" in r.stderr


def test_secrets_fails_without_a_doco_cd_config(repo):
    subprocess.run(["git", "rm", "-q", ".doco-cd.yml"], cwd=repo, check=True)
    commit(repo)
    r = run_cli("--root", str(repo), "secrets")
    assert r.returncode == 1 and ".doco-cd" in r.stdout
