"""Config: the repo root (hosts.yaml), the Host, the repo-root .env and environment overrides."""
from pathlib import Path

import pytest
from hops_fakes import NAS_2, git, hosts_yaml, write

from hops import config
from hops.errors import ConfigError

def truenas_host(root: Path):
    return config.load_hosts(root).hosts["truenas"]


def test_host_config_reads_the_hosts_keys_from_dotenv(repo):
    write(repo, {".env": """
        # Host access
        HOPS_TRUENAS_TRUENAS_URL=https://tn.example
        HOPS_TRUENAS_TRUENAS_API_KEY=fake-key
        export HOPS_TRUENAS_SSH_HOST="tn.example"
        HOPS_TRUENAS_SSH_USER=agent
        HOPS_TRUENAS_SSH_KEY=~/.ssh/agent_ed25519
    """})
    host = config.load_host(repo, truenas_host(repo), env={"HOME": "/home/me"})
    assert (host.host, host.user, host.key) == ("tn.example", "agent", Path("/home/me/.ssh/agent_ed25519"))


def test_environment_overrides_dotenv(plain_host):
    write(plain_host, {".env": "HOPS_TRUENAS_SSH_HOST=tn.example\nHOPS_TRUENAS_SSH_USER=someone\n"})
    host = config.load_host(plain_host, truenas_host(plain_host), env={
        "HOME": "/home/me", "HOPS_TRUENAS_SSH_HOST": "other.example", "HOPS_TRUENAS_SSH_KEY": "~/.ssh/k",
    })
    assert (host.host, host.user, host.key) == ("other.example", "someone", Path("/home/me/.ssh/k"))


def test_without_a_key_ssh_picks_one(plain_host):
    env = {"HOME": "/h", "HOPS_TRUENAS_SSH_HOST": "h", "HOPS_TRUENAS_SSH_USER": "u"}
    assert config.load_host(plain_host, truenas_host(plain_host), env=env).key is None


def test_missing_host_names_the_key_and_where_it_looked(plain_host):
    with pytest.raises(ConfigError, match=r"HOPS_TRUENAS_SSH_HOST is not set \(needed for Host 'truenas'\).*\.env"):
        config.load_host(plain_host, truenas_host(plain_host), env={"HOME": "/home/me"})


def test_the_ssh_user_is_required(plain_host):
    with pytest.raises(ConfigError, match=r"HOPS_TRUENAS_SSH_USER is not set \(needed for Host 'truenas'\).*\.env"):
        config.load_host(plain_host, truenas_host(plain_host), env={"HOME": "/h", "HOPS_TRUENAS_SSH_HOST": "h"})


def test_control_socket_lives_in_the_user_cache_dir(plain_host):
    env = {"HOME": "/home/me", "HOPS_TRUENAS_SSH_HOST": "h", "HOPS_TRUENAS_SSH_USER": "u"}
    host = truenas_host(plain_host)
    assert config.load_host(plain_host, host, env={**env, "XDG_CACHE_HOME": "/c"}).control_dir == Path("/c/hops")
    assert config.load_host(plain_host, host, env=env).control_dir == Path("/home/me/.cache/hops")


def test_dotenv_falls_back_to_the_main_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    write(main, {"hosts.yaml": hosts_yaml(capabilities="[]"), "tn/edge/compose.yaml": "services: {}\n",
                 ".env": "HOPS_TRUENAS_SSH_HOST=tn.example\nHOPS_TRUENAS_SSH_USER=u\n"})
    git(main, "add", "hosts.yaml", "tn")
    git(main, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    git(main, "worktree", "add", "-q", str(main / ".worktrees" / "1"))
    worktree = main / ".worktrees" / "1"

    assert config.find_repo_root(worktree / "tn") == worktree.resolve()
    assert config.load_host(worktree, truenas_host(worktree), env={"HOME": "/h"}).host == "tn.example"


def test_repo_root_is_the_nearest_directory_with_hosts_yaml(repo):
    assert config.find_repo_root(repo / "tn" / "edge") == repo.resolve()


def test_outside_a_repo_that_declares_hosts_it_says_so(tmp_path):
    with pytest.raises(ConfigError, match="no hosts.yaml in .*: run hops inside a repo that declares its Hosts"):
        config.find_repo_root(tmp_path)


def test_host_is_the_named_one_else_the_one_holding_cwd_else_the_only_one(repo):
    write(repo, NAS_2)
    hosts = config.load_hosts(repo)
    assert config.pick_host(hosts, "nas-2", repo / "tn").name == "nas-2"
    assert config.pick_host(hosts, None, repo / "hosts" / "nas-2" / "web").name == "nas-2"
    assert config.pick_host(hosts, None, repo / "tn" / "edge").name == "truenas"
    with pytest.raises(ConfigError, match="pick one of: truenas, nas-2 \\(e.g. --host\\)"):
        config.pick_host(hosts, None, repo)
    with pytest.raises(ConfigError, match="no Host 'nas-3' in hosts.yaml; Hosts: truenas, nas-2"):
        config.pick_host(hosts, "nas-3", repo)
