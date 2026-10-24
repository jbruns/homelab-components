"""Repository validation for every Host a consuming repo's hosts.yaml declares. Used by CI and the
pre-commit hook. See README.md.

Checks:
  secrets     every sensitive file referenced by Compose is tracked only in SOPS-encrypted form
              (doco-cd Stacks, entry file compose.yaml, reference the *.sops.* files directly),
              and no plaintext twin (x.yml beside x.sops.yml) is tracked; every file a Stack includes is
              <stack root>/<stack>/<service>/service.yaml and some Stack includes it, and every doco-cd
              deploy config keeps auto_discovery.depth 1
  env         every ${VAR} a Stack uses exists in its env.template and its encrypted .sops.env
  compose     `docker compose config` succeeds for every Stack and templates/* with placeholder values;
              every Service has a startup-order label, json-file logging and restart: unless-stopped,
              no env_file sets format (doco-cd supports only the default),
              and every Exposed Service (https router) a probe.url. A Service that extends its Host's
              <stack root>/_baseline.yaml meets the Baseline or carries a baseline.exempt.<item> reason, and
              its routers follow ADR 0006 (names, health and Client Paths, Protected Probes). Every image
              VOLUME path (read from the registry by digest, cached) has a mount at that exact path
              (Baseline item anonymous-volumes; no digest or a platform other than linux/amd64 needs
              its Exemption, and volumes_from fails); Services behind any profile are checked. Every
              top-level volume has a name of its Stack (or a --legacy-volume) and is external or
              labelled homeops.regenerable (ADR 0010)
  isolation   each Host's doco-cd Stack and _baseline.yaml changes ship in their own PR
  monitoring  full config checks and behavioral rule tests with the images the Stacks deploy;
              Grafana provisioning boots, dashboard generator parity and PromQL/LogQL checks
  cloudflare  the Cloudflare ranges Traefik trusts are valid and identical everywhere they appear
"""

import argparse
import base64
import fnmatch
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from string import Template

import yaml
from homelab_hosts import Host, HostsError, find_repo_root, load_hosts

from homelab_validate import cloudflare
from homelab_validate.registry import Registry, RegistryError, Response, urllib_http

# The repository being checked and the Hosts selected in it (configure()).
ROOT = Path.cwd()
HOSTS: list[Host] = []
EVERY_HOST = True
DEFAULT_BRANCH = "main"
# Inputs individual checks read, relative to each Host's stack root. None: not checked unless configured.
PATHS: dict[str, str | None] = {
    "prometheus": "monitoring/prometheus",
    "alertmanager": "monitoring/alertmanager",
    "loki": "monitoring/loki",
    "alloy": "monitoring/alloy",
    "blackbox": "monitoring/blackbox-exporter/blackbox.yml",
    "dashboards": None,
    "dashboards-json": "monitoring/grafana/dashboards",
    "declared-containers": None,
    "grafana": "monitoring/grafana",
    "rule-tests": "monitoring/prometheus/tests",
    "traefik": "edge/traefik/service.yaml",
}
# --path NAME=PATH: relative to ROOT or absolute, for every selected Host.
OVERRIDES: dict[str, str] = {}
# ${VAR} but not $${VAR} (Compose's escape for a literal "$")
VAR_RE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)")
SOPS_MARKERS = ("sops_version=", '"sops":', "sops:")

failures: list[str] = []


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def fail(msg: str, file: Path | None = None) -> None:
    failures.append(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        loc = f" file={_rel(file)}" if file else ""
        print(f"::error{loc}::{msg}".replace("\n", "%0A"))
    else:
        print(f"ERROR: {msg}")


def warn(msg: str) -> None:
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"WARN: {msg}")


def tracked_files() -> set[Path]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT, text=True)
    return {ROOT / p for p in out.split("\0") if p and (ROOT / p).is_file()}


# doco-cd auto-discovers a Stack only by <stack root>/<stack>/compose.yaml (its deploy config's working_dir).
ENTRY_NAME = "compose.yaml"
# Each file a Stack's entry file includes: <stack root>/<stack>/<service>/service.yaml, one Service plus its sidecars.
INCLUDED_NAME = "service.yaml"
# doco-cd's deploy config: .doco-cd.yml, .doco-cd.yaml or .doco-cd.<target>.y(a)ml, anywhere in the repo.
DOCO_CD_CONFIG_RE = re.compile(r"\.doco-cd(\.[^.]+)?\.ya?ml")


def configure(
    root: Path, paths: dict[str, str] | None = None, host: str | None = None, legacy_volumes: set[str] | None = None
) -> None:
    """Point every check at a repository root and the Hosts its hosts.yaml declares (only `host`, if given),
    override entries of PATHS (relative to `root` or absolute) and set the LEGACY_VOLUMES."""
    global ROOT, HOSTS, EVERY_HOST, DEFAULT_BRANCH, LEGACY_VOLUMES
    if unknown := set(paths or {}) - set(PATHS):
        raise KeyError(f"unknown path {sorted(unknown)[0]!r}; choose from {', '.join(PATHS)}")
    ROOT = root.resolve()
    hosts = load_hosts(ROOT)
    HOSTS = [hosts.pick(host)] if host else list(hosts.values())
    EVERY_HOST = len(HOSTS) == len(hosts)
    DEFAULT_BRANCH = hosts.default_branch
    OVERRIDES.clear()
    OVERRIDES.update(paths or {})
    if legacy_volumes is not None:
        LEGACY_VOLUMES = set(legacy_volumes)


def path(name: str, host: Host) -> Path | None:
    """An input of PATHS for one Host: its override, else its default under the Host's stack root."""
    if name in OVERRIDES:
        return ROOT / OVERRIDES[name]
    return None if PATHS[name] is None else host_dir(host) / PATHS[name]


def available(name: str, host: Host, kind: str = "dir", skip: str | None = None) -> bool:
    """Whether an input of PATHS exists as a `kind` ("dir" or "file"). A missing default means the Host
    lacks that component, so its check is skipped (printing `skip`); a missing --path is a failure."""
    p = path(name, host)
    if p is not None and (p.is_dir() if kind == "dir" else p.is_file()):
        return True
    if name in OVERRIDES:
        msg = f"--path {name}={OVERRIDES[name]}: not a {'directory' if kind == 'dir' else 'file'}"
        if msg not in failures:
            fail(msg)
    elif skip:
        print(skip)
    return False


def host_dir(host: Host, base: Path | None = None) -> Path:
    """The Host's stack root under `base` (default ROOT), as written in hosts.yaml, so it matches git's paths."""
    return (base or ROOT) / host.stack_root


def stacks(host: Host, base: Path | None = None) -> list[Path]:
    return sorted(p.parent for p in host_dir(host, base).glob(f"*/{ENTRY_NAME}"))


def entry(stack: Path) -> Path:
    return stack / ENTRY_NAME


def compose_files(entry: Path) -> list[Path]:
    """The entry Compose file plus everything it includes, recursively."""
    seen: list[Path] = []
    todo = [entry]
    while todo:
        f = todo.pop()
        if f in seen or not f.exists():
            continue
        seen.append(f)
        doc = yaml.safe_load(f.read_text()) or {}
        for inc in doc.get("include") or []:
            path = inc.get("path") if isinstance(inc, dict) else inc
            for p in [path] if isinstance(path, str) else (path or []):
                todo.append((f.parent / p).resolve())
    return seen


def sensitive_refs(compose: Path) -> list[Path]:
    """Files a Compose file expects to exist, decrypted, on the Host."""
    doc = yaml.safe_load(compose.read_text()) or {}
    refs: list[str] = []
    for svc in (doc.get("services") or {}).values():
        env_file = svc.get("env_file")
        for e in [env_file] if isinstance(env_file, (str, dict)) else (env_file or []):
            refs.append(e["path"] if isinstance(e, dict) else e)
        for v in svc.get("volumes") or []:
            if isinstance(v, dict) and str(v.get("x-sops")).lower() == "true":
                refs.append(v["source"])
    for sec in (doc.get("secrets") or {}).values():
        if isinstance(sec, dict) and sec.get("file"):
            refs.append(sec["file"])
    return [(compose.parent / r).resolve() for r in refs]


def env_file_formats(compose: Path) -> list[str]:
    """Services whose env_file sets a format. doco-cd loads Stacks with compose-go, which knows only
    the default; the docker compose CLI registers "raw" itself, so `docker compose config` accepts it."""
    doc = yaml.safe_load(compose.read_text()) or {}
    found = []
    for name, svc in (doc.get("services") or {}).items():
        env_file = svc.get("env_file")
        entries = [env_file] if isinstance(env_file, (str, dict)) else (env_file or [])
        if any(isinstance(e, dict) and e.get("format") for e in entries):
            found.append(name)
    return found


def env_keys(path: Path) -> set[str]:
    keys = set()
    if not path.exists():
        return keys
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if not k.startswith("sops_"):
                keys.add(k)
    return keys


def check_secrets() -> None:
    tracked = tracked_files()
    for f in sorted(tracked):
        if ".sops." in f.name or f.name == ".sops.env":
            if f.name == ".sops.yaml":
                continue
            if not any(m in f.read_text(errors="ignore") for m in SOPS_MARKERS):
                fail("file is named *.sops.* but is not SOPS-encrypted", f)
            twin = f.with_name(f.name.replace(".sops.", ".", 1))
            if twin != f and twin in tracked:
                fail(f"plaintext twin of a SOPS file is tracked: {_rel(twin)}", f)
        elif f.name == ".env" or f.suffix == ".env":
            fail("plaintext .env file is tracked; encrypt it as *.sops.env", f)
    check_layout(tracked)
    for host in HOSTS:
        for stack in stacks(host):
            check_stack_secrets(stack, tracked)
    print("secrets ok" if not failures else "secrets: problems found")


def check_layout(tracked: set[Path]) -> None:
    """A Stack is <stack root>/<stack>/compose.yaml; each file it includes is <service>/service.yaml."""
    for host in HOSTS:
        check_host_layout(host, tracked)
    # doco-cd scans its working_dir and each Stack folder, never deeper while depth is 1 (kimdre/doco-cd
    # internal/config/deploy/auto_discovery.go). It only looks for compose.yaml, so service.yaml is
    # never discovered; this is defence in depth: at depth 0 (unlimited) a compose.yaml in a Service
    # folder would become a Stack, and delete: true would remove it when it disappears.
    configs = sorted(f for f in tracked if DOCO_CD_CONFIG_RE.fullmatch(f.name))
    if not configs:
        fail("no doco-cd deploy config (.doco-cd.yml) is tracked")
    for config in configs:
        depth = ((yaml.safe_load(config.read_text()) or {}).get("auto_discovery") or {}).get("depth")
        if depth != 1:
            fail(
                f"auto_discovery.depth must stay 1 (found {depth!r}): deeper discovery would deploy a compose.yaml "
                "below a Stack folder as a Stack of its own",
                config,
            )


def check_host_layout(host: Host, tracked: set[Path]) -> None:
    included: set[Path] = set()
    for stack in stacks(host):
        for f in compose_files(entry(stack))[1:]:
            included.add(f)
            if f.name != INCLUDED_NAME or f.parent.parent != stack:
                fail(
                    f"{stack.name}: included file {_rel(f)} must be {_rel(stack)}/<service>/{INCLUDED_NAME}",
                    entry(stack),
                )
    root = host_dir(host)
    for f in sorted(tracked):
        if not f.is_relative_to(root):
            continue
        if re.fullmatch(r"docker-compose\.ya?ml", f.name):
            fail(f"rename to {INCLUDED_NAME} (one Service per file) or {ENTRY_NAME} (a Stack's entry file)", f)
        elif f.name == INCLUDED_NAME and f.parent.parent == root:
            fail(f"doco-cd never discovers this; a Stack's entry file must be {ENTRY_NAME}", f)
        elif f.name == INCLUDED_NAME and f not in included:
            fail(f"no Stack's include: lists this {INCLUDED_NAME}, so it is never deployed", f)


def check_stack_secrets(stack: Path, tracked: set[Path]) -> None:
    # doco-cd reads interpolation values from .sops.env (env_files in its deploy config) and fails without it.
    if stack / ".sops.env" not in tracked:
        fail(f"{stack.name}: Stack has no tracked .sops.env", entry(stack))
    for compose in compose_files(entry(stack)):
        for ref in sensitive_refs(compose):
            if not ref.is_relative_to(ROOT):
                continue  # Host path outside the repo, e.g. the age key
            if ref not in tracked:
                fail(f"referenced file {_rel(ref)} is not tracked", compose)
            elif ".sops." not in ref.name:
                fail(f"sensitive file {_rel(ref)} must be SOPS-encrypted and named *.sops.*", compose)


def stack_vars(stack: Path) -> set[str]:
    used: set[str] = set()
    for compose in compose_files(entry(stack)):
        used |= set(VAR_RE.findall(compose.read_text()))
    return used


def check_env() -> None:
    for host in HOSTS:
        check_host_env(host)


def check_host_env(host: Host) -> None:
    for stack in stacks(host):
        used = stack_vars(stack)
        template, encrypted = stack / "env.template", stack / ".sops.env"
        tpl, enc = env_keys(template), env_keys(encrypted)
        if missing := sorted(used - tpl):
            fail(f"{stack.name}: variables used but missing from env.template: {', '.join(missing)}", template)
        if missing := sorted(used - enc):
            fail(
                f"{stack.name}: variables used but missing from .sops.env (deploy would get blanks): "
                f"{', '.join(missing)}",
                encrypted,
            )
        if unused := sorted(tpl - used):
            warn(f"{stack.name}: env.template keys no Compose file uses: {', '.join(unused)}")
        print(f"env checked: {stack.name}")
    check_dependencies_env(host)


def dependencies_template(host: Host) -> Path | None:
    """The Host's dependencies.yml when it uses dependency rendering (it has the template, a
    dependencies-render Service, or a prometheus.yml that reads the rendered file); else None. Fails when
    the renderer or Prometheus still uses a template that is missing."""
    prom = path("prometheus", host)
    template = prom / "dependencies.yml"
    if template.is_file():
        return template
    service = prom / INCLUDED_NAME
    renderer = service.is_file() and "dependencies-render" in (
        yaml.safe_load(service.read_text()).get("services") or {}
    )
    config = prom / "prometheus.yml"
    if renderer or (config.is_file() and "dependencies.yml" in config.read_text()):
        msg = f"{_rel(template)} is missing, but dependencies-render or prometheus.yml still uses it"
        if msg not in failures:
            fail(msg, template)
    return None


def check_dependencies_env(host: Host) -> None:
    """dependencies-render fails on any placeholder in dependencies.yml its environment lacks."""
    prom = path("prometheus", host)
    if not available("prometheus", host) or not (template := dependencies_template(host)):
        return
    svc = (yaml.safe_load((prom / INCLUDED_NAME).read_text()).get("services") or {}).get("dependencies-render")
    if svc is None:
        fail(f"{_rel(template)}: no dependencies-render Service in {_rel(prom / INCLUDED_NAME)} renders it", template)
        return
    passed = {e.split("=", 1)[0] for e in svc.get("environment") or []}
    if missing := sorted(set(VAR_RE.findall(template.read_text())) - passed):
        fail(f"dependencies.yml placeholders not passed to dependencies-render: {', '.join(missing)}", template)


def placeholder(key: str) -> str:
    k = key.upper()
    if "SUBNET" in k:
        return "10.99.0.0/24"
    if "IPV4" in k:
        return "10.99.0.1"
    if k.endswith(("UID", "GID", "PORT")):
        return "1000"
    if k.startswith(("ZFS_POOL_ROOT", "DOCKER_DATA_ROOT")):
        return "/mnt/placeholder"
    return "placeholder"


STARTUP_LABEL = "startup-order"
STARTUP_LEVELS = {"0", "1", "2", "3"}
# json-file rotation for every service; the engine default keeps logs forever.
LOGGING = {"driver": "json-file", "max-size": "10m", "max-file": "3"}
HTTPS_RULE_RE = re.compile(r"^traefik\.http\.routers\..+-https\.rule$")

# The Baseline (ADR 0007). The tool owns its items; each Host's stack root holds the values in
# _baseline.yaml. Every Service `extends` its Host's file, except doco-cd's, which carry the same items
# inline (extending would race doco-cd's own self-update); both are held to it.
BASELINE_NAME = "_baseline.yaml"
TEMPLATES_DIR = "templates"
EXEMPT_PREFIX = "baseline.exempt."
NNP_RE = re.compile(r"^no-new-privileges([=:]true)?$")
BASELINE_ITEMS = {
    "healthcheck": lambda s: (
        bool((s.get("healthcheck") or {}).get("test")) and not (s.get("healthcheck") or {}).get("disable")
    ),
    "no-new-privileges": lambda s: any(NNP_RE.match(str(o)) for o in s.get("security_opt") or []),
    "digest": lambda s: "@sha256:" in str(s.get("image", "")),
    "memory": lambda s: bool(
        ((s.get("deploy") or {}).get("resources") or {}).get("limits", {}).get("memory") or s.get("mem_limit")
    ),
    "cap_drop": lambda s: "ALL" in (s.get("cap_drop") or []),
}
# Every VOLUME path in the Service's image has a mount; checked against the image config, so it is
# not in BASELINE_ITEMS (see uncovered_volumes).
ANONYMOUS_VOLUMES = "anonymous-volumes"
EXEMPT_ITEMS = [*BASELINE_ITEMS, ANONYMOUS_VOLUMES]
# Router names carry their kind, so recording rules can tell UI, health and Client Path apart (ADR 0006).
ROUTER_LABEL_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.")
SERVICE_LABEL_RE = re.compile(r"^traefik\.http\.services\.([^.]+)\.")
PROTECTED_CHAIN = "protected"
FORWARD_AUTH = "idp-forward-auth"
STRIP_IDP = "strip-idp-headers"


def baseline_file(host: Host) -> str:
    """The Host's Baseline file, relative to the repo root."""
    return f"{PurePosixPath(host.stack_root)}/{BASELINE_NAME}"


def baseline_services(stack: Path, host: Host, base: Path) -> set[str]:
    """Services whose own definition extends the Host's Baseline file."""
    target = (base / baseline_file(host)).resolve()
    names = set()
    for compose in compose_files(entry(stack)):
        for name, svc in ((yaml.safe_load(compose.read_text()) or {}).get("services") or {}).items():
            ext = (svc or {}).get("extends")
            if isinstance(ext, dict) and ext.get("file") and (compose.parent / ext["file"]).resolve() == target:
                names.add(name)
    return names


def middlewares(labels: dict, router: str) -> list[str]:
    raw = str(labels.get(f"traefik.http.routers.{router}.middlewares", ""))
    return [m.strip().split("@")[0] for m in raw.split(",") if m.strip()]


@dataclass(frozen=True)
class Uninspectable:
    """Why validate cannot read a Service's image VOLUME paths; the item then needs an Exemption."""

    why: str


def baseline_problems(
    name: str, svc: dict, uncovered: list[str] | Uninspectable | None = None
) -> tuple[list[str], list[str]]:
    """Problems with one Baseline Service, and its Exemptions for the audit report. `uncovered` holds
    the image's VOLUME paths with no mount, or why they cannot be inspected; None when the registry
    could not be read (reported on its own)."""
    labels = svc.get("labels") or {}
    problems, exemptions = [], []
    for key in labels:
        if key.startswith(EXEMPT_PREFIX) and key[len(EXEMPT_PREFIX) :] not in EXEMPT_ITEMS:
            problems.append(f"{name}: {key} names no Baseline item ({', '.join(EXEMPT_ITEMS)})")
    checks = {item: met(svc) for item, met in BASELINE_ITEMS.items()}
    if uncovered is not None:
        checks[ANONYMOUS_VOLUMES] = not isinstance(uncovered, Uninspectable) and not uncovered
    for item, met in checks.items():
        reason = str(labels.get(EXEMPT_PREFIX + item, "")).strip()
        if met and EXEMPT_PREFIX + item in labels:
            problems.append(f"{name}: stale Exemption {EXEMPT_PREFIX}{item}; the Service meets it")
        elif not met and not reason and isinstance(uncovered, Uninspectable) and item == ANONYMOUS_VOLUMES:
            problems.append(
                f"{name}: cannot inspect image VOLUMEs {uncovered.why}; mount each VOLUME path "
                f"and add a {EXEMPT_PREFIX}{item} reason"
            )
        elif not met and not reason:
            detail = f" (image VOLUME paths with no mount: {', '.join(uncovered)})" if item == ANONYMOUS_VOLUMES else ""
            problems.append(f"{name}: misses Baseline item {item!r} without a {EXEMPT_PREFIX}{item} reason{detail}")
        elif not met:
            exemptions.append(f"{name}: {item}: {reason}")
    for target in declared_anonymous(svc):
        problems.append(
            f"{name}: volume at {target} has no source (an anonymous volume); name it, bind it or make it a tmpfs"
        )
    if svc.get("volumes_from"):
        problems.append(f"{name}: volumes_from unsupported by validate; mount the volumes by name instead")
    return problems + router_problems(svc.get("container_name") or name, labels), exemptions


def declared_anonymous(svc: dict) -> list[str]:
    """Targets of service volumes written with only a target (`- /path`)."""
    return [v["target"] for v in svc.get("volumes") or [] if v.get("type") == "volume" and not v.get("source")]


def uncovered_volumes(svc: dict, image_paths: list[str]) -> list[str]:
    """The image's VOLUME paths that no named volume, bind or tmpfs is mounted at. Only the exact path
    counts: Docker creates an anonymous volume at a VOLUME path even under a mount on its parent."""
    targets = {
        posixpath.normpath(v["target"])
        for v in svc.get("volumes") or []
        if v.get("target") and (v.get("type") != "volume" or v.get("source"))
    }
    targets |= {posixpath.normpath(t.split(":", 1)[0]) for t in svc.get("tmpfs") or []}
    return sorted(p for p in image_paths if posixpath.normpath(p) not in targets)


# Volume names (ADR 0010): <stack>_[<service>_]<purpose>, explicit, and external unless regenerable.
REGENERABLE_LABEL = "homeops.regenerable"
# Names from before ADR 0010, until each Stack's rename moves them; set with --legacy-volume. The list only
# shrinks: an entry no Stack declares fails.
LEGACY_VOLUMES: set[str] = set()
# Templates are copied to <stack root>/<stack>/ with `app` renamed, so their volumes are named for Stack `app`.
TEMPLATE_STACK = "app"


def volume_labels(volume: dict) -> dict:
    labels = volume.get("labels") or {}
    if isinstance(labels, list):
        return dict(str(entry).partition("=")[::2] for entry in labels)
    return {key: "" if value is None else value for key, value in labels.items()}


def volume_problems(stack: str, volumes: dict, stack_names: set[str]) -> list[str]:
    """Top-level volumes as written in one Stack's files (the rendered model fills in a name)."""
    problems = []
    for key, volume in volumes.items():
        volume = volume or {}
        name = volume.get("name")
        if not name:
            problems.append(f"{stack}: volume {key} has no explicit name:")
        if not volume.get("external") and not str(volume_labels(volume).get(REGENERABLE_LABEL, "")).strip():
            problems.append(
                f"{stack}: volume {name or key} must be external: true (it holds data) or carry a "
                f"{REGENERABLE_LABEL} label saying what regenerates it"
            )
        # An external volume may be another Stack's, borrowed under its owner's name.
        owners = (stack_names | {stack}) if volume.get("external") else {stack}
        if name and name not in LEGACY_VOLUMES and not any(name.startswith(f"{s}_") for s in owners):
            problems.append(f"{stack}: volume name {name} must start with {stack}_ (ADR 0010)")
    return problems


def stack_volumes(stack: Path) -> dict:
    """Top-level volumes declared across a Stack's entry file and the files it includes."""
    volumes = {}
    for compose in compose_files(entry(stack)):
        volumes |= (yaml.safe_load(compose.read_text()) or {}).get("volumes") or {}
    return volumes


# Image configs come from the registry by pinned digest, anonymously; tests swap in recordings.
REGISTRY_HTTP = urllib_http
PLATFORM = ("linux", "amd64")
RETRY_DELAYS = (2, 8)  # seconds; registries answer 429 or 5xx in bursts


def patient(http, sleep=time.sleep):
    """A transport that retries a 429 or 5xx, honouring a short Retry-After."""

    def send(method: str, url: str, headers: dict[str, str]) -> Response:
        for delay in RETRY_DELAYS:
            resp = http(method, url, headers)
            if resp.status != 429 and resp.status < 500:
                return resp
            after = resp.headers.get("retry-after", "")
            sleep(min(int(after), 30) if after.isdigit() else delay)
        return http(method, url, headers)

    return send


def image_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "homelab-validate" / "image-volumes"


def fetch_image_volumes(image: str, registry: Registry) -> list[str]:
    """The VOLUME paths of a digest-pinned image's linux/amd64 config, from its registry."""
    ref, _, digest = image.partition("@")
    repository = ref if ":" not in ref.rsplit("/", 1)[-1] else ref.rsplit(":", 1)[0]
    manifest = registry.manifest(repository, digest)
    if "manifests" in manifest:
        picked = [
            m["digest"]
            for m in manifest["manifests"]
            if ((m.get("platform") or {}).get("os"), (m.get("platform") or {}).get("architecture")) == PLATFORM
        ]
        if not picked:
            raise RegistryError(f"{image}: its index has no {'/'.join(PLATFORM)} image")
        manifest = registry.manifest(repository, picked[0])
    config = json.loads(registry.blob(repository, manifest["config"]["digest"]))
    return sorted((config.get("config") or {}).get("Volumes") or {})


def image_volumes(image: str, registry: Registry) -> list[str]:
    """fetch_image_volumes, cached by the pinned digest: a digest's content never changes."""
    cache = image_cache_dir() / (image.partition("@sha256:")[2] + ".json")
    if cache.is_file():
        return json.loads(cache.read_text())["volumes"]
    volumes = fetch_image_volumes(image, registry)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"image": image, "volumes": volumes}) + "\n")
    return volumes


def router_problems(name: str, labels: dict) -> list[str]:
    """`name` is the container name: Traefik metrics are mapped back to containers by it."""
    problems = []
    routers = {m.group(1) for k in labels if (m := ROUTER_LABEL_RE.match(k))}
    kind_re = re.compile(rf"^{re.escape(name)}-(http|https|health|client-[a-z0-9]+)$")
    for r in sorted(routers):
        if not kind_re.match(r):
            problems.append(f"{name}: router {r!r} must be {name}-http|-https|-health|-client-<protocol>")
            continue
        rule, mws = str(labels.get(f"traefik.http.routers.{r}.rule", "")), middlewares(labels, r)
        client, health = "-client-" in r, r.endswith("-health")
        if "Header" in rule and not client:
            problems.append(f"{name}: router {r} matches on a header; only Client Paths may")
        if health and ("PathPrefix(" in rule or "Path(" not in rule):
            problems.append(f"{name}: router {r} must match one exact Path()")
        if (client or health) and {PROTECTED_CHAIN, FORWARD_AUTH} & set(mws):
            problems.append(f"{name}: router {r} must not use forward auth")
        if (client or health) and STRIP_IDP not in mws:
            problems.append(f"{name}: router {r} skips forward auth, so it needs {STRIP_IDP}")
        if client and ("crowdsec-bouncer" not in mws or not any("ratelimit" in m for m in mws)):
            problems.append(f"{name}: Client Path {r} needs crowdsec-bouncer and a rate limit")
    if any({PROTECTED_CHAIN, FORWARD_AUTH} & set(middlewares(labels, r)) for r in routers):
        if not str(labels.get("probe.idp_url", "")).startswith("https://"):
            problems.append(f"{name}: a Protected Service needs a probe.idp_url label")
        if f"{name}-health" not in routers:
            problems.append(f"{name}: a Protected Service needs a {name}-health router for its Probe")
    handed = str(labels.get(f"traefik.http.middlewares.{FORWARD_AUTH}.forwardAuth.authResponseHeaders", ""))
    strip = f"traefik.http.middlewares.{STRIP_IDP}.headers.customRequestHeaders."
    if handed:
        stripped = {k.removeprefix(strip) for k, v in labels.items() if k.startswith(strip) and v == ""}
        if missing := sorted({h.strip() for h in handed.split(",") if h.strip()} - stripped):
            problems.append(f"{name}: {STRIP_IDP} must empty every {FORWARD_AUTH} header; missing {', '.join(missing)}")
    for s in sorted({m.group(1) for k in labels if (m := SERVICE_LABEL_RE.match(k))} - {name}):
        problems.append(f"{name}: Traefik service {s!r} must be named {name!r} (Http5xxRatio maps it to the container)")
    return problems


def probe_modules(host: Host) -> set[str]:
    """The Host's blackbox-exporter modules; none when it has no blackbox.yml."""
    if not available("blackbox", host, "file"):
        return set()
    return set(yaml.safe_load(path("blackbox", host).read_text())["modules"])


def probe_problems(name: str, labels: dict, modules: set[str]) -> list[str]:
    """An Exposed Service (an https router) needs a Probe (GLOSSARY.md, prometheus.yml)."""
    problems = []
    if any(HTTPS_RULE_RE.match(k) for k in labels) and not str(labels.get("probe.url", "")).startswith("https://"):
        problems.append(f"{name}: has an https router but no probe.url label")
    if "probe.module" in labels and labels["probe.module"] not in modules:
        problems.append(f"{name}: probe.module {labels['probe.module']!r} is not a blackbox.yml module")
    return problems


def check_compose() -> None:
    registry, read_images = Registry(http=patient(REGISTRY_HTTP)), {}
    declared_volumes: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        for f in tracked_files():
            dst = work / f.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        templates = sorted((work / TEMPLATES_DIR).glob(f"*/{ENTRY_NAME}"))
        for host in HOSTS:
            # Each template is rendered as if it were a Stack of every Host, so it stays valid against each Baseline.
            for tpl in templates:
                shutil.copytree(tpl.parent, host_dir(host, work) / f"_template-{tpl.parent.name}")
            declared_volumes |= check_host_compose(host, work, registry, read_images)
    # Another Host may declare a legacy name, so only a run over every Host can tell one is gone.
    for name in sorted(LEGACY_VOLUMES - declared_volumes) if EVERY_HOST else []:
        fail(f"volumes (ADR 0010): legacy name {name} is no longer declared; drop its --legacy-volume")


def check_host_compose(host: Host, work: Path, registry: Registry, read_images: dict) -> set[str]:
    """Render and check each Stack of one Host in the working copy `work`; returns the volume names it declares."""
    modules = probe_modules(host)
    exemptions: list[str] = []
    counts = {"baseline": 0, "total": 0}
    declared_volumes: set[str] = set()
    baseline = baseline_file(host)
    stack_names = {s.name for s in stacks(host, work)}
    for stack in stacks(host, work):
        keys = env_keys(stack / "env.template") | stack_vars(stack)
        (stack / ".env").write_text("".join(f"{k}={placeholder(k)}\n" for k in sorted(keys)))
        for compose in compose_files(entry(stack)):
            for ref in sensitive_refs(compose):
                if ref.is_relative_to(work) and not ref.exists():
                    ref.write_text("")
        r = subprocess.run(
            ["docker", "compose", "--profile", "*", "config", "--format", "json"],
            cwd=stack,
            capture_output=True,
            text=True,
        )
        noise = [line for line in r.stderr.splitlines() if line.strip()]
        template = stack.name.startswith("_template-")
        # Read before rendering, so a Stack that fails to render still declares its volumes.
        volumes = stack_volumes(stack)
        if not template:
            declared_volumes |= {v["name"] for v in volumes.values() if v and v.get("name")}
        src = (
            ROOT / TEMPLATES_DIR / stack.name.removeprefix("_template-") if template else host_dir(host) / stack.name
        ) / entry(stack).name
        if r.returncode != 0 or any("is not set" in line for line in noise):
            fail(f"{stack.name}: docker compose config failed:\n" + "\n".join(noise), src)
            continue
        services = json.loads(r.stdout).get("services", {})
        bad = [
            name for name, svc in services.items() if (svc.get("labels") or {}).get(STARTUP_LABEL) not in STARTUP_LEVELS
        ]
        probes = [p for name, svc in services.items() for p in probe_problems(name, svc.get("labels") or {}, modules)]
        logs = [
            name
            for name, svc in services.items()
            if {
                "driver": (svc.get("logging") or {}).get("driver"),
                **{k: (svc.get("logging") or {}).get("options", {}).get(k) for k in ("max-size", "max-file")},
            }
            != LOGGING
        ]
        restart = [name for name, svc in services.items() if svc.get("restart") != "unless-stopped"]
        # `config` resolves env_file away, so read the sources.
        env_formats = [n for f in compose_files(entry(stack)) for n in env_file_formats(f)]
        on_baseline = baseline_services(stack, host, work)
        vol_problems = volume_problems(TEMPLATE_STACK if template else stack.name, volumes, stack_names)
        if template and set(services) - on_baseline:
            fail(f"{stack.name}: every template Service must extend {baseline}", src)
        doco_cd = stack.name == host.doco_cd.stack
        if doco_cd and on_baseline:
            fail(f"{stack.name} must not extend {baseline} (ADR 0007); keep its settings inline", src)
        inline = set(services) if doco_cd else set()
        base_problems = [f"{name}: must extend {baseline}" for name in sorted(set(services) - on_baseline - inline)]
        for name in sorted((on_baseline | inline) & set(services)):
            uncovered = anonymous_volume_check(stack.name, name, services[name], registry, read_images, src)
            p, e = baseline_problems(name, services[name], uncovered)
            base_problems += p
            exemptions += [f"{stack.name}/{x}" for x in e]
        if not template:
            counts["baseline"] += len((on_baseline | inline) & set(services))
            counts["total"] += len(services)
        if bad:
            fail(
                f"{stack.name}: services without a valid {STARTUP_LABEL} label (0-3, ADR-0003): "
                f"{', '.join(sorted(bad))}",
                src,
            )
        if probes:
            fail(f"{stack.name}: " + "; ".join(probes), src)
        if logs:
            fail(
                f"{stack.name}: services without json-file logging at max-size 10m, max-file 3: "
                f"{', '.join(sorted(logs))}",
                src,
            )
        if restart:
            fail(f"{stack.name}: services without restart: unless-stopped: {', '.join(sorted(restart))}", src)
        if env_formats:
            fail(
                f"{stack.name}: env_file format is unsupported by doco-cd (use the default and single-quote "
                f"values containing $): {', '.join(sorted(env_formats))}",
                src,
            )
        if base_problems:
            fail(f"{stack.name}: Baseline (ADR 0007): " + "; ".join(base_problems), src)
        if vol_problems:
            fail(f"{stack.name}: volumes (ADR 0010): " + "; ".join(vol_problems), src)
        if (
            not bad
            and not probes
            and not logs
            and not restart
            and not env_formats
            and not base_problems
            and not vol_problems
        ):
            print(f"compose ok: {stack.name}")
    print(f"Baseline: {counts['baseline']} of {counts['total']} Services on {host.name} meet it (ADR 0007)")
    for x in exemptions:
        print(f"  Exemption {x}")
    return declared_volumes


def anonymous_volume_check(
    stack: str, name: str, svc: dict, registry: Registry, read: dict, src: Path
) -> list[str] | Uninspectable | None:
    """The Service's image VOLUME paths with no mount; Uninspectable when validate cannot read them
    (no digest, or another platform); None after a registry failure, which fails by image name."""
    image = str(svc.get("image", ""))
    if "@sha256:" not in image:
        return Uninspectable("without a digest")
    platform = svc.get("platform")
    if platform and platform != "/".join(PLATFORM):
        return Uninspectable(f"for platform {platform} (validate reads only {'/'.join(PLATFORM)})")
    if image not in read:
        try:
            read[image] = image_volumes(image, registry)
        except (RegistryError, ValueError, KeyError) as exc:
            read[image] = None
            fail(
                f"{stack}: {name}: cannot read the image config of {image} for its VOLUME paths "
                f"(anonymous volumes, ADR 0007): {exc}",
                src,
            )
    return None if read[image] is None else uncovered_volumes(svc, read[image])


def deployed_image(compose: Path, service: str) -> str:
    return yaml.safe_load(compose.read_text())["services"][service]["image"]


def docker_run(image: str, mounts: dict[Path, str], entrypoint: str, *args: str) -> None:
    cmd = ["docker", "run", "--rm", "--entrypoint", entrypoint]
    for src, dst in mounts.items():
        cmd += ["-v", f"{src}:{dst}:ro"]
    cmd += [image, *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    label = f"{entrypoint} {' '.join(args)}"
    if r.returncode != 0:
        fail(f"{label} failed:\n{out}")
    else:
        print(f"ok: {label}")


class MonitoringError(Exception):
    """A failed external check, reported as a validation failure."""


def base_ref() -> str:
    """The ref changes are compared against: CI sets VALIDATE_BASE to the PR base."""
    return os.environ.get("VALIDATE_BASE") or f"origin/{DEFAULT_BRANCH}"


def base_files(*paths: Path) -> dict[str, str]:
    """Tracked files under paths at base_ref(), by repo-relative name."""
    base = base_ref()
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base, "--", *map(_rel, paths)], cwd=ROOT, capture_output=True, text=True
    )
    if listed.returncode:
        raise MonitoringError(f"cannot read {base}: {listed.stderr.strip()}")
    return {
        name: subprocess.check_output(["git", "show", f"{base}:{name}"], cwd=ROOT, text=True)
        for name in listed.stdout.splitlines()
    }


def render_dummy(text: str) -> tuple[str, set[str]]:
    """Substitute every $VAR with a dummy value; return the text and the names it used."""
    template = Template(text)
    names = set(template.get_identifiers())
    return template.substitute(
        {name: "http://example.invalid" if name.endswith("_URL") else "ci-dummy" for name in names}
    ), names


def docker_output(*args: str, stderr: bool = False) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode:
        raise MonitoringError(f"docker {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return (result.stdout + (result.stderr if stderr else "")).strip()


def container_logs(container: str) -> str:
    return docker_output("logs", container, stderr=True)


def http_get(url: str, *, auth: tuple[str, str] | None = None) -> bytes:
    headers = {"Authorization": "Basic " + base64.b64encode(":".join(auth).encode()).decode()} if auth else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise MonitoringError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc


@contextmanager
def monitoring_service(
    image: str,
    mounts: dict[Path, str],
    port: int,
    ready: str,
    *args: str,
    healthy: Callable[[bytes], bool] = lambda body: True,
    env: dict[str, str] | None = None,
    user: str = "0:0",
):
    """An isolated, disposable instance; publish only a random loopback port."""
    cmd = ["create", "--label", "startup-order=skip", "--user", user, "--publish", f"127.0.0.1::{port}"]
    for src, dst in mounts.items():
        cmd += ["--volume", f"{src}:{dst}"]
    for key, value in (env or {}).items():
        cmd += ["--env", f"{key}={value}"]
    container = docker_output(*cmd, image, *args)
    try:
        docker_output("start", container)
        address = docker_output("port", container, f"{port}/tcp")
        base = f"http://127.0.0.1:{address.rsplit(':', 1)[1]}"
        deadline = time.monotonic() + 120
        while True:
            try:
                if healthy(http_get(base + ready)):
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError, MonitoringError):
                pass  # Connection refused / 503 is expected during startup.
            if docker_output("inspect", "--format", "{{.State.Running}}", container) == "false":
                raise MonitoringError(f"{image} exited before becoming healthy")
            if time.monotonic() >= deadline:
                raise MonitoringError(f"{image} did not become healthy:\n{container_logs(container)}")
            time.sleep(0.25)
        yield base, container
    except MonitoringError as exc:
        raise MonitoringError(f"{exc}\n{image} logs:\n{container_logs(container)}") from exc
    finally:
        docker_output("rm", "--force", "--volumes", container)


GRAFANA_MACROS = {
    "__rate_interval": "5m",
    "__interval": "1m",
    "__auto": "5m",
    "__range": "1h",
    "__interval_ms": "60000",
    "__range_s": "3600",
    "__range_ms": "3600000",
}
GRAFANA_VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)(?::(\w+))?\}|([A-Za-z_]\w*))")


def interpolate_query(expr: str, variables: dict[str, dict]) -> str:
    def replace(match: re.Match) -> str:
        name = match[1] or match[3]
        if name in GRAFANA_MACROS:
            return GRAFANA_MACROS[name]
        if name not in variables:
            raise MonitoringError(f"undeclared Grafana variable {match[0]}")
        if match[2] not in (None, "regex", "raw", "csv", "pipe"):
            raise MonitoringError(f"unsupported Grafana variable format {match[0]}")
        return "5m" if variables[name]["type"] == "interval" else "ci_value"

    return GRAFANA_VAR.sub(replace, expr)


def variable_expression(expr: str) -> str:
    """Translate Grafana's classic variable wrappers to an equivalent parseable expression."""
    if expr.startswith("query_result(") and expr.endswith(")"):
        return expr[len("query_result(") : -1]
    if expr.startswith("label_values(") and expr.endswith(")"):
        args = expr[len("label_values(") : -1].rsplit(",", 1)
        if not re.fullmatch(r"\s*[a-zA-Z_]\w*\s*", args[-1]):
            raise MonitoringError(f"invalid label_values label: {args[-1]}")
        return args[0] if len(args) == 2 else '{__name__=~".+"}'
    if expr.startswith("label_names(") and expr.endswith(")"):
        return expr[len("label_names(") : -1] or '{__name__=~".+"}'
    return expr


@dataclass(frozen=True)
class DashboardQuery:
    file: Path
    dashboard: str
    location: str
    kind: str  # the datasource type: prometheus or loki
    expr: str  # Grafana variables and macros replaced, ready to parse
    original: str


def dashboard_queries(dashboard: dict, sources: list[dict], file: Path) -> list[DashboardQuery]:
    """Check all datasource references and collect panel, variable and annotation queries."""
    variables = {v["name"]: v for v in dashboard.get("templating", {}).get("list", [])}
    by_uid = {s["uid"]: s for s in sources}
    default = next((s["uid"] for s in sources if s.get("isDefault")), None)
    queries = []

    def source_type(ref):
        uid = ref.get("uid") if isinstance(ref, dict) else ref
        uid = uid or default
        if uid in ("-- Grafana --", "-- Mixed --", "__expr__"):
            return None
        if uid and uid.startswith("$"):
            name = uid.removeprefix("${").removesuffix("}") if uid.startswith("${") else uid[1:]
            variable = variables.get(name, {})
            if variable.get("type") != "datasource":
                raise MonitoringError(f"unresolved datasource uid {uid}")
            kind = variable["query"]
            regex = variable.get("regex", "").strip("/")
            matches = [s for s in sources if s["type"] == kind and (not regex or re.search(regex, s["name"]))]
            if not matches:
                raise MonitoringError(f"datasource variable {uid} matches no provisioned datasource")
            return kind
        if uid not in by_uid:
            raise MonitoringError(f"unknown datasource uid {uid!r}")
        kind = by_uid[uid]["type"]
        if isinstance(ref, dict) and ref.get("type") not in (None, kind):
            raise MonitoringError(f"datasource {uid} has type {kind}, not {ref['type']}")
        return kind

    def collect(item, location, inherited=None, variable=False):
        ref = item.get("datasource", inherited)
        expr = item.get("expr")
        if variable:
            expr = item.get("query")
            if isinstance(expr, dict):
                expr = expr.get("query")
        try:
            kind = source_type(ref) if ref is not None or expr or variable else None
            if expr:
                if kind not in ("prometheus", "loki"):
                    raise MonitoringError(f"query has unsupported datasource {kind!r}")
                parsed = interpolate_query(expr, variables)
                if variable and kind == "prometheus":
                    parsed = variable_expression(parsed)
                queries.append(DashboardQuery(file, dashboard["title"], location, kind, parsed, expr))
        except (MonitoringError, re.error) as exc:
            detail = f"; query {expr!r}" if expr else ""
            fail(f"{dashboard['title']} / {location}: {exc}{detail}", file)
        for target in item.get("targets", []):
            collect(target, f"{location} / {target.get('refId', '?')}", ref)
        for panel in item.get("panels", []):
            collect(panel, f"{location} / {panel.get('title', '?')}", ref)

    for variable in variables.values():
        if variable["type"] == "query":
            collect(variable, f"variable {variable['name']}", variable=True)
    for annotation in dashboard.get("annotations", {}).get("list", []):
        collect(annotation, f"annotation {annotation.get('name', '?')}")
    for panel in dashboard.get("panels", []):
        collect(panel, f"panel {panel.get('title', '?')}")
    return queries


def check_dashboard_queries(host: Host, prom_image: str, loki_image: str, sources: list[dict]) -> None:
    queries = [
        query
        for file in sorted(path("dashboards-json", host).glob("*.json"))
        for query in dashboard_queries(json.loads(file.read_text()), sources, file)
    ]
    if not queries:
        return
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "empty-prometheus.yml"
        config.write_text("global: {}\nscrape_configs: []\n")
        with monitoring_service(
            prom_image,
            {config: "/etc/prometheus/prometheus.yml:ro"},
            9090,
            "/-/ready",
            "--config.file=/etc/prometheus/prometheus.yml",
            "--storage.tsdb.path=/tmp/prometheus",
        ) as (prom_url, _):
            with monitoring_service(
                loki_image,
                {path("loki", host) / "loki.yaml": "/etc/loki/loki.yaml:ro"},
                3100,
                "/ready",
                "-config.file=/etc/loki/loki.yaml",
            ) as (loki_url, _):
                for query in queries:
                    endpoint = (
                        prom_url + "/api/v1/query"
                        if query.kind == "prometheus"
                        else loki_url + "/loki/api/v1/format_query"
                    )
                    try:
                        result = json.loads(http_get(endpoint + "?" + urllib.parse.urlencode({"query": query.expr})))
                        if result.get("status") != "success":
                            raise MonitoringError(f"unexpected query response: {result}")
                    except (MonitoringError, urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                        fail(
                            f"{query.dashboard} / {query.location}: {query.kind} query {query.original!r} "
                            f"failed: {exc}",
                            query.file,
                        )
    print(f"checked {len(queries)} Grafana queries with deployed Prometheus and Loki")


def provisioning_sources(document: object) -> list[dict]:
    if not isinstance(document, dict) or not isinstance(document.get("datasources"), list):
        raise MonitoringError("Grafana provisioning: expected a datasources list")
    sources = []
    for source in document["datasources"]:
        if not isinstance(source, dict) or any(
            not isinstance(source.get(key), str) or not source[key] for key in ("uid", "name", "type")
        ):
            raise MonitoringError("Grafana provisioning: every datasource needs a uid, name and type")
        sources.append(source)
    return sources


def datasource_document(text: str) -> tuple[dict, set[str]]:
    """Render like Grafana: substitute $VAR in YAML values only, never in comments."""
    rendered, names = render_dummy(yaml.safe_dump(yaml.safe_load(text)))
    document = yaml.safe_load(rendered)
    provisioning_sources(document)
    return document, names


def grafana_provisions(host: Host) -> list[dict]:
    """Datasource provisioning to boot in turn: the base ref's (or the current one again), then the
    current one. The current template's placeholders must all be set in datasources.sops.env."""
    grafana = path("grafana", host)
    provisioning = grafana / "provisioning/datasources.yml"
    current, names = datasource_document(provisioning.read_text())
    if missing := names - env_keys(grafana / "datasources.sops.env"):
        raise MonitoringError(f"Grafana datasource environment is missing {', '.join(sorted(missing))}")
    if not provisioning.is_relative_to(ROOT):
        warn("Grafana upgrade check cannot read external --path provisioning from the Git base")
        return [current, current]
    previous = base_files(provisioning).get(_rel(provisioning))
    return [datasource_document(previous)[0] if previous else current, current]


GRAFANA_ADMIN = ("ci-admin", "ci-password")
GRAFANA_ERROR = re.compile(r"level=(error|fatal|critical)|\"level\":\"(error|fatal|critical)\"")


def boot_grafana(host: Host, provisions: list[dict]) -> None:
    """Boot the deployed Grafana once per provisioning on one database, so a uid/name migration
    collision fails as it would on the Host, not just clean-install errors."""
    grafana = path("grafana", host)
    image = deployed_image(grafana / INCLUDED_NAME, "grafana")
    expected = {json.loads(f.read_text())["uid"] for f in path("dashboards-json", host).glob("*.json")}
    env = {
        "GF_SECURITY_ADMIN_USER": GRAFANA_ADMIN[0],
        "GF_SECURITY_ADMIN_PASSWORD": GRAFANA_ADMIN[1],
        "GF_LOG_LEVEL": "info",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": "/etc/grafana/dashboards/overview.json",
    }
    with tempfile.TemporaryDirectory() as tmp:
        datasources = Path(tmp) / "datasources.yml"
        data = Path(tmp) / "data"
        data.mkdir()
        mounts = {
            datasources: "/etc/grafana/provisioning/datasources/validate.yaml:ro",
            grafana / "provisioning/dashboards.yml": "/etc/grafana/provisioning/dashboards/validate.yaml:ro",
            path("dashboards-json", host): "/etc/grafana/dashboards:ro",
            data: "/var/lib/grafana",
        }
        for provision in provisions:
            datasources.write_text(yaml.safe_dump(provision))
            with monitoring_service(
                image,
                mounts,
                3000,
                "/api/health",
                env=env,
                healthy=lambda body: json.loads(body).get("database") == "ok",
                user=f"{os.getuid()}:{os.getgid()}",
            ) as (url, container):
                actual = json.loads(http_get(url + "/api/datasources", auth=GRAFANA_ADMIN))
                if {s["uid"] for s in actual} != {s["uid"] for s in provisioning_sources(provision)}:
                    raise MonitoringError("Grafana did not provision the expected datasource uids")
                dashboards = json.loads(http_get(url + "/api/search?type=dash-db&limit=5000", auth=GRAFANA_ADMIN))
                if {d["uid"] for d in dashboards} != expected:
                    raise MonitoringError("Grafana did not provision every dashboard")
                for line in container_logs(container).splitlines():
                    if "provision" in line.lower() and GRAFANA_ERROR.search(line):
                        raise MonitoringError(f"Grafana provisioning error: {line}")
    print("ok: Grafana provisioning (clean database and upgrade/repeat boot)")


def check_grafana(host: Host, prom_image: str, loki_image: str) -> None:
    provisions = grafana_provisions(host)
    boot_grafana(host, provisions)
    check_dashboard_queries(host, prom_image, loki_image, provisioning_sources(provisions[-1]))


def rule_definitions(documents: list[dict]) -> dict[str, dict]:
    return {
        rule.get("alert") or rule["record"]: {"rule": rule, "interval": group.get("interval")}
        for document in documents
        for group in document.get("groups", [])
        for rule in group["rules"]
    }


def rule_cases(documents: list[dict], name: str, alert: bool) -> list[dict]:
    """The test groups exercising one rule, cut down to its assertions and without the group name."""
    pattern = re.compile(r'alertname\s*=\s*"' + re.escape(name) + '"')
    cases = []
    for document in documents:
        for case in document.get("tests", []):
            expressions = [
                test
                for test in case.get("promql_expr_test", [])
                if (pattern.search(test["expr"]) if alert else test["expr"] == name)
            ]
            alerts = [test for test in case.get("alert_rule_test", []) if test["alertname"] == name]
            if expressions or alerts:
                cases.append(
                    {
                        **{
                            key: value
                            for key, value in case.items()
                            if key not in ("name", "promql_expr_test", "alert_rule_test")
                        },
                        "promql_expr_test": expressions,
                        "alert_rule_test": alerts,
                        "evaluation_interval": document.get("evaluation_interval"),
                        "group_eval_order": document.get("group_eval_order"),
                    }
                )
    return cases


DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
# Every alert carries these; Alertmanager routes and inhibits on them (alerting-rules.yml).
ROUTING_LABELS = {"tier", "stack"}


def duration_seconds(text: object) -> float:
    return sum(int(n) * DURATION_UNITS[unit] for n, unit in re.findall(r"(\d+)(ms|s|m|h|d|w|y)", str(text)))


def label_names(labels: str) -> set[str]:
    return set(re.findall(r"([A-Za-z_]\w*)\s*=", re.sub(r'"(?:[^"\\]|\\.)*"', '""', labels)))


def alert_evaluations(case: dict) -> list[tuple[float, list[set[str]]]]:
    """Each evaluation of a rule_cases() case: its time and the label names of each firing alert."""
    return [
        (duration_seconds(test["eval_time"]), [label_names(s["labels"]) for s in test.get("exp_samples") or []])
        for test in case["promql_expr_test"]
    ] + [
        (duration_seconds(test["eval_time"]), [set(a.get("exp_labels") or {}) for a in test.get("exp_alerts") or []])
        for test in case["alert_rule_test"]
    ]


def label_template_problems(rule: dict) -> list[str]:
    """A routing label templated from a series label renders empty (or, in promtool, literally)
    when that label is missing, so the expr must guarantee it: a matcher that cannot match the
    empty string, or a label_replace that fills it in when empty."""
    problems = []
    expr = rule["expr"]
    for key in sorted(ROUTING_LABELS):
        value = str((rule.get("labels") or {}).get(key, ""))
        if "{{" not in value:
            continue
        single = re.fullmatch(r"\{\{\s*\$labels\.(\w+)\s*\}\}", value.strip())
        if not single:
            problems.append(f"{key} can render empty or literally; template it as a single {{{{ $labels.<label> }}}}")
            continue
        label = single.group(1)
        matchers = re.findall(rf'\b{label}\s*(=~|!=|=)\s*"((?:[^"\\]|\\.)*)"', expr)
        pinned = any(
            (op == "!=" and v == "") or (op == "=" and v != "") or (op == "=~" and not re.fullmatch(v, ""))
            for op, v in matchers
        )
        filled = re.search(rf'label_replace\(.*?,\s*"{label}"\s*,\s*"[^"]+"\s*,\s*"{label}"\s*,\s*""\s*\)', expr, re.S)
        if not pinned and not filled:
            problems.append(
                f'{key} can render empty or literally; pin {label} non-empty in the expr (e.g. {label}!="")'
            )
    return problems


def alert_test_problems(name: str, rule: dict, cases: list[dict]) -> list[str]:
    groups = [alert_evaluations(case) for case in cases]
    evaluations = [e for group in groups for e in group]
    problems = []
    if not any(alerts for _, alerts in evaluations) or (
        name != "Watchdog" and all(alerts for _, alerts in evaluations)
    ):
        problems.append("add firing and nonfiring promtool test cases")
    if any(not ROUTING_LABELS <= labels for _, alerts in evaluations for labels in alerts):
        problems.append(f"assert {' and '.join(sorted(ROUTING_LABELS))} in every firing expectation")
    # Firing at t2 means active since t2 - for; nonfiring at t1 >= t2 - for proves the pending period.
    hold = duration_seconds(rule.get("for", ""))
    if hold and not any(
        not quiet and alerts and 0 < t2 - t1 <= hold for group in groups for t1, quiet in group for t2, alerts in group
    ):
        problems.append(
            f"pin the for: {rule['for']} boundary with a nonfiring evaluation at most that long before a firing one"
        )
    return problems


def check_rule_tests(host: Host, image: str, rule_files: list[Path], mounts: dict[Path, str]) -> None:
    if "rule-tests" in OVERRIDES and not available("rule-tests", host):
        return
    rules = rule_definitions([yaml.safe_load(f.read_text()) or {} for f in rule_files])
    if not rules:
        return
    prom, tests = path("prometheus", host), path("rule-tests", host)
    suites = sorted(tests.glob("*.test.yml"))
    documents = [yaml.safe_load(f.read_text()) for f in suites]
    for name, definition in rules.items():
        alert = "alert" in definition["rule"]
        cases = rule_cases(documents, name, alert)
        if alert:
            for problem in alert_test_problems(name, definition["rule"], cases) + label_template_problems(
                definition["rule"]
            ):
                fail(f"{name}: {problem}", tests)
        elif not cases:
            fail(f"{name}: add a recording-rule assertion to a promtool suite", tests)

    # Require the relevant behavioral case to change whenever a rule changes, not just any test file.
    if all(f.is_relative_to(ROOT) for f in [*rule_files, tests]):
        try:
            previous = base_files(prom, tests)
        except MonitoringError as exc:
            fail(f"rule test freshness: {exc}")
        else:

            def in_dir(name: str, folder: Path, pattern: str) -> bool:
                file = PurePosixPath(name)
                return file.parent == PurePosixPath(_rel(folder)) and fnmatch.fnmatch(file.name, pattern)

            old_suites = [yaml.safe_load(t) or {} for n, t in previous.items() if in_dir(n, tests, "*.test.yml")]
            old_rules = rule_definitions(
                [yaml.safe_load(t) or {} for n, t in previous.items() if in_dir(n, prom, "*rules*.yml")]
            )
            for name, definition in rules.items():
                alert = "alert" in definition["rule"]
                if definition != old_rules.get(name) and rule_cases(documents, name, alert) == rule_cases(
                    old_suites, name, alert
                ):
                    fail(f"{name}: rule changed since {base_ref()}; update its promtool behavioral case", tests)
    else:
        warn("rule test freshness cannot compare external --path inputs to the Git base")
    if suites:
        docker_run(
            image, {**mounts, tests: "/tests"}, "promtool", "test", "rules", *[f"/tests/{f.name}" for f in suites]
        )


def check_generator(command: list[str], what: str, *args: str) -> None:
    """Run a generator's --check: its committed output must match what it would write."""
    try:
        result = subprocess.run([*command, "--check", *args], capture_output=True, text=True)
    except FileNotFoundError:
        fail(f"{what}: cannot run {command[0]}; install it or set --path (see README.md)")
        return
    if result.returncode:
        fail(result.stdout.strip() or result.stderr.strip(), Path(command[-1]))
    else:
        print(f"ok: {what} match {Path(command[-1]).name}")


DECLARED_CONTAINERS = "homelab-declared-containers"


def generator_command(name: str, host: Host) -> list[str] | None:
    """How to run a generator: a configured script with this Python, else (declared-containers only) the
    installed command; None when the dashboards generator is not configured or a --path script is missing."""
    if path(name, host):
        return [sys.executable, str(path(name, host))] if available(name, host, "file") else None
    if name == "declared-containers":
        return [shutil.which(DECLARED_CONTAINERS) or DECLARED_CONTAINERS]
    return None


def skipped(missing: Path) -> str:
    return f"monitoring: no {_rel(missing)}; its check skipped"


def check_monitoring() -> None:
    for host in HOSTS:
        skip = f"monitoring: {host.name} has no {_rel(path('prometheus', host))}; skipped"
        if not available("prometheus", host, skip=skip):
            continue
        check_host_monitoring(host)


def check_host_monitoring(host: Host) -> None:
    prom = path("prometheus", host)
    prom_image = deployed_image(prom / INCLUDED_NAME, "prometheus")
    rule_files = sorted(prom.glob("*rules*.yml"))
    prom_mounts = {f: f"/etc/prometheus/{f.name}" for f in [prom / "prometheus.yml", *rule_files]}
    with tempfile.TemporaryDirectory() as tmp:
        dummy = Path(tmp)
        config_mounts = dict(prom_mounts)
        if template := dependencies_template(host):
            rendered = dummy / "dependencies.yml"
            rendered.write_text(render_dummy(template.read_text())[0])
            config_mounts[rendered] = "/etc/prometheus/sd/dependencies.yml"
        compose = yaml.safe_load((prom / INCLUDED_NAME).read_text())
        for name in compose.get("secrets", {}):
            secret = dummy / name
            secret.write_text("ci-dummy-secret\n")
            config_mounts[secret] = f"/run/secrets/{name}"
        docker_run(prom_image, config_mounts, "promtool", "check", "config", "/etc/prometheus/prometheus.yml")
    docker_run(
        prom_image, prom_mounts, "promtool", "check", "rules", *[f"/etc/prometheus/{f.name}" for f in rule_files]
    )
    check_rule_tests(host, prom_image, rule_files, {f: f"/etc/prometheus/{f.name}" for f in rule_files})

    am = path("alertmanager", host)
    if available("alertmanager", host, skip=skipped(am)):
        if (am / "config.yml") in tracked_files():
            am_image = deployed_image(am / INCLUDED_NAME, "alertmanager")
            docker_run(
                am_image,
                {am / "config.yml": "/etc/alertmanager/config.yml"},
                "amtool",
                "check-config",
                "/etc/alertmanager/config.yml",
            )
        else:
            warn("alertmanager config is encrypted-only; amtool check skipped until it is split from its secrets")

    loki = path("loki", host)
    loki_image = None
    if available("loki", host, skip=skipped(loki)):
        loki_image = deployed_image(loki / INCLUDED_NAME, "loki")
        docker_run(
            loki_image,
            {loki / "loki.yaml": "/etc/loki/loki.yaml"},
            "/usr/bin/loki",
            "-config.file=/etc/loki/loki.yaml",
            "-verify-config",
        )

    alloy = path("alloy", host)
    if available("alloy", host, skip=skipped(alloy)):
        alloy_image = deployed_image(alloy / INCLUDED_NAME, "alloy")
        docker_run(
            alloy_image,
            {alloy / "config.alloy": "/etc/alloy/config.alloy"},
            "/bin/alloy",
            "validate",
            "/etc/alloy/config.alloy",
        )

    if dashboards := generator_command("dashboards", host):
        if "dashboards-json" not in OVERRIDES or available("dashboards-json", host):
            check_generator(dashboards, "grafana dashboards", "--out", str(path("dashboards-json", host)))
    elif "dashboards" not in OVERRIDES:
        print("monitoring: no dashboards generator (--path dashboards=SCRIPT); its parity check skipped")
    if declared := generator_command("declared-containers", host):
        check_generator(declared, "declared containers", "--root", str(ROOT), "--host", host.name)
    if not available("grafana", host, skip=skipped(path("grafana", host))):
        return
    if not loki_image:
        fail(f"monitoring: the Grafana check needs Loki at {_rel(loki)}")
        return
    try:
        check_grafana(host, prom_image, loki_image)
    except (MonitoringError, urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        fail(f"monitoring: {exc}")


def check_cloudflare() -> None:
    for host in HOSTS:
        traefik = path("traefik", host)
        if not available("traefik", host, "file", f"cloudflare: {host.name} has no {_rel(traefik)}; skipped"):
            continue
        errs = cloudflare.problems(traefik.read_text())
        for err in errs:
            fail(err, traefik)
        if not errs:
            print("cloudflare ok")


def check_isolation() -> None:
    """A change to a Host's doco-cd Stack ships alone: doco-cd self-updates mid-Deploy, so other
    Stacks of that Host in the same commit deploy across the handover. A change to a Host's Baseline
    ships without Stack changes: it recreates every Service that extends it, so its Deploy should be
    about it alone (ADR 0007). Compares against VALIDATE_BASE (CI sets the PR base) or the default branch."""
    base = base_ref()
    r = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"isolation: cannot diff against {base}: {r.stderr.strip()}")
        return
    changed = [PurePosixPath(f) for f in r.stdout.split()]
    isolated = True
    for host in HOSTS:
        root = PurePosixPath(host.stack_root)
        depth = len(root.parts)
        stacks_changed = {f.parts[depth] for f in changed if f.parts[:depth] == root.parts and len(f.parts) > depth + 1}
        doco_cd = host.doco_cd.stack
        if doco_cd in stacks_changed and len(stacks_changed) > 1:
            others = ", ".join(sorted(stacks_changed - {doco_cd}))
            fail(f"isolation: {root}/{doco_cd} changes must ship in their own PR (also changed: {others})")
            isolated = False
        elif PurePosixPath(baseline_file(host)) in changed and stacks_changed:
            fail(
                f"isolation: {baseline_file(host)} changes must ship in their own PR "
                f"(also changed: {', '.join(sorted(stacks_changed))})"
            )
            isolated = False
    if isolated:
        print("isolation ok")


CHECKS = {
    "secrets": check_secrets,
    "env": check_env,
    "compose": check_compose,
    "monitoring": check_monitoring,
    "cloudflare": check_cloudflare,
    "isolation": check_isolation,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="homelab-validate", description="Repository validation for every Host in hosts.yaml."
    )
    parser.add_argument("--root", type=Path, help="repository to check (default: the one holding the cwd)")
    parser.add_argument("--host", help="check only this Host (default: every Host)")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=f"override a path, relative to --root ({', '.join(PATHS)})",
    )
    parser.add_argument(
        "--legacy-volume",
        action="append",
        default=[],
        metavar="NAME",
        help="a volume name from before ADR 0010 that is allowed until renamed",
    )
    parser.add_argument("checks", nargs="*", metavar="check", help=f"{', '.join(CHECKS)} (default: all)")
    args = parser.parse_args(argv)
    overrides = dict(p.split("=", 1) for p in args.path if "=" in p)
    if len(overrides) != len(args.path) or set(overrides) - set(PATHS):
        parser.error(f"--path takes NAME=PATH with NAME one of {', '.join(PATHS)}")
    if unknown := [name for name in args.checks if name not in CHECKS]:
        sys.exit(f"unknown check {unknown[0]!r}; choose from {', '.join(CHECKS)}")
    try:
        configure(args.root or find_repo_root(), overrides, args.host, set(args.legacy_volume))
    except HostsError as exc:
        print(exc, file=sys.stderr)
        return 1
    for name in args.checks or list(CHECKS):
        CHECKS[name]()
    return 1 if failures else 0
