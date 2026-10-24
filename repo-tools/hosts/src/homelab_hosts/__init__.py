"""The one loader and validator for a consuming repo's hosts.yaml (v1). See README.md for the schema."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = [
    "CAPABILITIES",
    "DEPLOYERS",
    "HOSTS_FILE",
    "DocoCd",
    "Host",
    "HostSettings",
    "Hosts",
    "HostsError",
    "SshSettings",
    "TrueNasSettings",
    "env_prefix",
    "find_repo_root",
    "host_settings",
    "load_hosts",
]

HOSTS_FILE = "hosts.yaml"
CAPABILITIES = frozenset({"truenas", "zfs"})
DEPLOYERS = frozenset({"doco-cd"})

_HOST_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_TOP_KEYS = {"version", "default_branch", "hosts"}
_HOST_KEYS = {"stack_root", "deployer", "capabilities", "doco_cd"}
_DOCO_CD_DEFAULTS = {
    "stack": "doco-cd",
    "api_secret": "doco-cd/api_secret.sops.txt",
    "address": "doco-cd:80",
    "network": "metrics",
}
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}


class HostsError(Exception):
    """hosts.yaml is missing or invalid, or a Host cannot be picked or configured. `problems` lists each one."""

    def __init__(self, problems: str | list[str]):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class DocoCd:
    """Where a Host's doco-cd lives. `api_secret` is relative to the Host's stack root."""

    stack: str
    api_secret: str
    address: str
    network: str


@dataclass(frozen=True)
class Host:
    name: str
    stack_root: str
    """As written in hosts.yaml: a directory relative to the repo root, holding `<stack>/compose.yaml`."""
    path: Path
    """Absolute path of the stack root."""
    deployer: str
    capabilities: frozenset[str]
    doco_cd: DocoCd

    def has(self, capability: str) -> bool:
        """Whether the Host declares `capability`. An unknown capability name raises HostsError (catches typos)."""
        if capability not in CAPABILITIES:
            raise HostsError(f"unknown capability {capability!r}; known: {', '.join(sorted(CAPABILITIES))}")
        return capability in self.capabilities


@dataclass(frozen=True)
class Hosts(Mapping[str, Host]):
    """A validated hosts.yaml: a read-only mapping of Host name to Host, in file order."""

    root: Path
    """The repo root (the directory holding hosts.yaml)."""
    default_branch: str
    hosts: Mapping[str, Host]

    def __getitem__(self, name: str) -> Host:
        return self.hosts[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.hosts)

    def __len__(self) -> int:
        return len(self.hosts)

    def pick(self, name: str | None = None, path: str | Path | None = None) -> Host:
        """The Host a command targets: `name` if given (e.g. --host), else the Host whose stack root contains
        `path`, else the only Host. Otherwise HostsError, listing the Hosts."""
        if name is not None:
            if name not in self.hosts:
                raise HostsError(f"no Host {name!r} in {HOSTS_FILE}; Hosts: {', '.join(self.hosts)}")
            return self.hosts[name]
        if path is not None:
            target = Path(path).resolve()
            containing = [h for h in self.hosts.values() if target.is_relative_to(h.path.resolve())]
            if containing:
                return max(containing, key=lambda h: len(h.path.resolve().parts))
        if len(self.hosts) == 1:
            return next(iter(self.hosts.values()))
        where = f" contains {str(path)!r}" if path is not None else ""
        raise HostsError(f"no Host's stack root{where}; pick one of: {', '.join(self.hosts)} (e.g. --host)")


@dataclass(frozen=True)
class SshSettings:
    host: str
    user: str | None
    """None: let ssh pick (its config or the local user)."""
    key: str | None
    """Private key path as written (callers expand `~`). None: let ssh pick."""


@dataclass(frozen=True)
class TrueNasSettings:
    url: str
    api_key: str
    insecure: bool


@dataclass(frozen=True)
class HostSettings:
    ssh: SshSettings
    truenas: TrueNasSettings | None
    """Set exactly when the Host has the `truenas` capability."""


def find_repo_root(start: str | Path | None = None) -> Path:
    """The nearest directory at or above `start` (default: the working directory) holding hosts.yaml."""
    here = Path.cwd() if start is None else Path(start)
    here = here.resolve()
    for directory in (here, *here.parents):
        if (directory / HOSTS_FILE).is_file():
            return directory
    raise HostsError(f"no {HOSTS_FILE} in {here} or any parent directory")


def load_hosts(root: str | Path | None = None) -> Hosts:
    """Load and validate `<root>/hosts.yaml`; `root` defaults to find_repo_root(). Raises HostsError listing every
    problem found."""
    root = find_repo_root() if root is None else Path(root).resolve()
    file = root / HOSTS_FILE
    try:
        data = yaml.safe_load(file.read_text())
    except FileNotFoundError:
        raise HostsError(f"no {HOSTS_FILE} in {root}") from None
    except yaml.YAMLError as e:
        raise HostsError(f"{file}: not valid YAML: {e}") from None

    problems: list[str] = []
    if not isinstance(data, dict):
        raise HostsError(f"{file}: must be a mapping with version, hosts and optionally default_branch")
    _unknown_keys(data, _TOP_KEYS, "", problems)
    if "version" not in data:
        problems.append("version: missing; this library reads version 1")
    elif data["version"] != 1 or isinstance(data["version"], bool):
        problems.append(f"version: {data['version']!r} is not supported; this library reads version 1")
    default_branch = data.get("default_branch", "main")
    if not isinstance(default_branch, str) or not default_branch:
        problems.append(f"default_branch: must be a non-empty string, not {default_branch!r}")

    hosts: dict[str, Host] = {}
    raw_hosts = data.get("hosts")
    if not isinstance(raw_hosts, dict) or not raw_hosts:
        problems.append("hosts: must map at least one Host name to its settings")
        raw_hosts = {}
    stack_roots: dict[Path, str] = {}
    for name, raw in raw_hosts.items():
        host = _host(root, name, raw, problems)
        if host is None:
            continue
        other = stack_roots.setdefault(host.path.resolve(), host.name)
        if other != host.name:
            problems.append(f"hosts.{name}.stack_root: {host.stack_root!r} is already the stack root of Host {other!r}")
        hosts[host.name] = host

    if problems:
        raise HostsError([f"{file}: {p}" for p in problems])
    return Hosts(root=root, default_branch=default_branch, hosts=hosts)


def _unknown_keys(data: dict, known: set[str], where: str, problems: list[str]) -> None:
    for key in data:
        if key not in known:
            problems.append(f"{where}{key}: unknown key; expected one of: {', '.join(sorted(known))}")


def _host(root: Path, name: object, raw: object, problems: list[str]) -> Host | None:
    if not isinstance(name, str) or not _HOST_NAME.fullmatch(name):
        problems.append(f"hosts.{name}: bad Host name; use lowercase letters, digits and '-', starting alphanumeric")
        return None
    where = f"hosts.{name}."
    if not isinstance(raw, dict):
        problems.append(f"hosts.{name}: must be a mapping of {', '.join(sorted(_HOST_KEYS))}")
        return None
    before = len(problems)
    _unknown_keys(raw, _HOST_KEYS, where, problems)

    stack_root = raw.get("stack_root")
    path = None
    if stack_root is None:
        problems.append(f"{where}stack_root: missing")
    elif not isinstance(stack_root, str) or not stack_root.strip("/"):
        problems.append(f"{where}stack_root: must be a directory relative to the repo root, not {stack_root!r}")
    elif Path(stack_root).is_absolute() or ".." in Path(stack_root).parts:
        problems.append(f"{where}stack_root: {stack_root!r} must be relative to the repo root and stay inside it")
    else:
        path = root / Path(stack_root)
        if not path.is_dir():
            problems.append(f"{where}stack_root: {stack_root!r} is not a directory under {root}")

    deployer = raw.get("deployer")
    if deployer is None:
        problems.append(f"{where}deployer: missing; supported: {', '.join(sorted(DEPLOYERS))}")
    elif deployer not in DEPLOYERS:
        problems.append(f"{where}deployer: unknown deployer {deployer!r}; supported: {', '.join(sorted(DEPLOYERS))}")

    capabilities = raw.get("capabilities")
    if capabilities is None:
        problems.append(f"{where}capabilities: missing; a list, possibly empty, of {', '.join(sorted(CAPABILITIES))}")
        capabilities = []
    elif not isinstance(capabilities, list):
        problems.append(f"{where}capabilities: must be a list, not {capabilities!r}")
        capabilities = []
    for capability in capabilities:
        if capability not in CAPABILITIES:
            problems.append(
                f"{where}capabilities: unknown capability {capability!r}; known: {', '.join(sorted(CAPABILITIES))}"
            )

    raw_doco_cd = raw.get("doco_cd", {})
    doco_cd = dict(_DOCO_CD_DEFAULTS)
    if not isinstance(raw_doco_cd, dict):
        problems.append(f"{where}doco_cd: must be a mapping of {', '.join(sorted(_DOCO_CD_DEFAULTS))}")
    else:
        _unknown_keys(raw_doco_cd, set(_DOCO_CD_DEFAULTS), f"{where}doco_cd.", problems)
        for key, value in raw_doco_cd.items():
            if key in _DOCO_CD_DEFAULTS:
                if isinstance(value, str) and value:
                    doco_cd[key] = value
                else:
                    problems.append(f"{where}doco_cd.{key}: must be a non-empty string, not {value!r}")

    if len(problems) > before:
        return None
    return Host(
        name=name,
        stack_root=stack_root,
        path=path,
        deployer=deployer,
        capabilities=frozenset(capabilities),
        doco_cd=DocoCd(**doco_cd),
    )


def env_prefix(host: Host | str) -> str:
    """`HOPS_<NAME>_`: the .env key prefix for a Host, NAME uppercased with '-' turned into '_'."""
    name = host.name if isinstance(host, Host) else host
    return f"HOPS_{name.upper().replace('-', '_')}_"


def host_settings(env: Mapping[str, str], host: Host) -> HostSettings:
    """A Host's connection settings from `env` (the caller's parsed .env and/or os.environ; this never reads a
    file). Raises HostsError naming every required key that is missing or empty; never includes values."""
    prefix = env_prefix(host)

    def get(key: str) -> str | None:
        value = env.get(prefix + key)
        return value if value else None

    missing = [prefix + key for key in ("SSH_HOST",) if get(key) is None]
    if host.has("truenas"):
        missing += [prefix + key for key in ("TRUENAS_URL", "TRUENAS_API_KEY") if get(key) is None]
    problems = [f"{key} is not set (needed for Host {host.name!r})" for key in missing]

    insecure = False
    if host.has("truenas"):
        raw = (env.get(prefix + "TRUENAS_INSECURE") or "").strip().lower()
        if raw in _TRUTHY:
            insecure = True
        elif raw not in _FALSY:
            problems.append(f"{prefix}TRUENAS_INSECURE must be 1/0, true/false, yes/no or on/off")
    if problems:
        raise HostsError(problems)

    ssh = SshSettings(host=get("SSH_HOST"), user=get("SSH_USER"), key=get("SSH_KEY"))
    truenas = None
    if host.has("truenas"):
        truenas = TrueNasSettings(url=get("TRUENAS_URL"), api_key=get("TRUENAS_API_KEY"), insecure=insecure)
    return HostSettings(ssh=ssh, truenas=truenas)
