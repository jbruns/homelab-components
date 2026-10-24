"""Where hops finds the repo, which Host it targets, and how it reaches that Host.

Every site-specific value comes from the consuming repo, read through homelab_hosts:

- repo root: the nearest directory at or above the working directory holding hosts.yaml.
- Host: `--host <name>`; else the Host whose stack root holds the working directory; else the only Host.
- `.env`: the repo root's, else the main worktree's (agent worktrees under `.worktrees/` have none).
  Keys are per Host: `HOPS_<NAME>_SSH_HOST` and so on (homelab_hosts.env_prefix).
- environment variables override `.env`.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import homelab_hosts
from homelab_hosts import Host, Hosts, HostsError, TrueNasSettings

from hops.errors import ConfigError


@dataclass(frozen=True)
class HostConfig:
    host: str
    user: str
    key: Path | None  # None: let ssh pick (its config or default keys)
    control_dir: Path  # ssh ControlPath sockets


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.strip() or None


def find_repo_root(cwd: Path | None = None) -> Path:
    """The consuming repo to read hosts.yaml and declared state from: hosts.yaml at or above `cwd`."""
    try:
        return homelab_hosts.find_repo_root(cwd)
    except HostsError as exc:
        raise ConfigError(f"{exc}: run hops inside a repo that declares its Hosts") from None


def load_hosts(repo_root: Path) -> Hosts:
    try:
        return homelab_hosts.load_hosts(repo_root)
    except HostsError as exc:
        raise ConfigError(str(exc)) from None


def pick_host(hosts: Hosts, name: str | None, cwd: Path | None = None) -> Host:
    """`name` (--host) if given, else the Host whose stack root holds `cwd`, else the only Host."""
    try:
        return hosts.pick(name, path=Path.cwd() if cwd is None else cwd)
    except HostsError as exc:
        raise ConfigError(str(exc)) from None


def env_files(repo_root: Path) -> list[Path]:
    """The .env files hops reads, first match wins: the checkout's, then the main worktree's."""
    candidates = [repo_root / ".env"]
    common = _git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        candidates.append(Path(common).parent / ".env")
    return list(dict.fromkeys(p.resolve() for p in candidates))


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines, comments, `export ` and surrounding quotes are allowed."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_values(repo_root: Path, env=None) -> dict[str, str]:
    """Settings by name: the first .env found, overridden by the environment."""
    env = dict(os.environ if env is None else env)
    found = next((f for f in env_files(repo_root) if f.is_file()), None)
    return {**(read_env_file(found) if found else {}), **env}


def where(repo_root: Path) -> str:
    """Where a missing setting can go, for error messages."""
    return "the environment or " + " or ".join(str(f) for f in env_files(repo_root))


def host_setting(repo_root: Path, host: Host, key: str, env=None) -> str | None:
    """One per-Host `.env` value, `HOPS_<NAME>_<key>`; None when unset or empty."""
    return load_values(repo_root, env).get(homelab_hosts.env_prefix(host) + key) or None


def _settings(read, repo_root: Path, host: Host, env):
    """`read(values, host)` from homelab_hosts; a ConfigError names each missing key and where to set it."""
    try:
        return read(load_values(repo_root, env), host)
    except HostsError as exc:
        raise ConfigError(f"{'; '.join(exc.problems)}; set it in {where(repo_root)}") from None


def truenas_settings(repo_root: Path, host: Host, env=None) -> TrueNasSettings:
    """The Host's TrueNAS URL and API key. Only a TrueNAS client needs them: SSH commands never ask."""
    return _settings(homelab_hosts.truenas_settings, repo_root, host, env)


def load_host(repo_root: Path, host: Host, env=None) -> HostConfig:
    """How to reach the Host over SSH; needs only the SSH keys, never the TrueNAS ones. The user is required: hops
    never guesses which account it acts as."""
    values = load_values(repo_root, env)
    ssh = _settings(homelab_hosts.ssh_settings, repo_root, host, env)
    if not ssh.user:
        user_key = homelab_hosts.env_prefix(host) + "SSH_USER"
        raise ConfigError(f"{user_key} is not set (needed for Host {host.name!r}); set it in {where(repo_root)}")
    home = values.get("HOME") or str(Path.home())
    cache = values.get("XDG_CACHE_HOME") or f"{home}/.cache"
    return HostConfig(
        host=ssh.host,
        user=ssh.user,
        key=Path(_expand(ssh.key, home)) if ssh.key else None,
        control_dir=Path(cache) / "hops",
    )


def _expand(path: str, home: str) -> str:
    return home + path[1:] if path == "~" or path.startswith("~/") else path
