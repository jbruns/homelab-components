"""Declared state: the Stacks and Services the repo's Stack root declares, and target name resolution.

Names resolve from the repo, never from running containers, so a typo fails before anything
touches the Host. Compose files are read as doco-cd deploys them: each Stack's entry file and
everything it includes, recursively.
"""

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hops.errors import ConfigError, ResolveError

VARIABLE = re.compile(r"\$\{?[A-Za-z_]")
# Datastore (GLOSSARY) engines, by the last path segment of the image repository.
DATASTORE_IMAGES = {
    "mariadb": "mariadb",
    "mysql": "mariadb",
    "postgres": "postgres",
    "postgis": "postgres",
    "valkey": "valkey",
    "redis": "valkey",
}


# The labels Compose sets on each container; doco-cd's project is the Stack.
PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"


def compose_files(entry: Path) -> list[Path]:
    """A Stack's entry file plus everything it includes, recursively."""
    seen: list[Path] = []
    todo = [entry]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        if not f.exists():
            raise ConfigError(f"{entry}: includes missing {f}")
        seen.append(f)
        for inc in (yaml.safe_load(f.read_text()) or {}).get("include") or []:
            paths = inc.get("path") if isinstance(inc, dict) else inc
            todo += [(f.parent / p).resolve() for p in ([paths] if isinstance(paths, str) else paths or [])]
    return seen


def labels_of(service: dict) -> dict[str, str]:
    """A Compose service's labels as a mapping, from either the list or the mapping form."""
    labels = service.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(item.split("=", 1) if "=" in item else (item, "") for item in labels)
    return {k: str(v) for k, v in labels.items()}


@dataclass(frozen=True)
class Image:
    ref: str  # as written in Compose: repository[:tag][@digest]
    repository: str
    tag: str | None
    digest: str | None

    @classmethod
    def parse(cls, ref: str) -> "Image":
        name, _, digest = ref.partition("@")
        repository, tag = name, None
        if ":" in name.rsplit("/", 1)[-1]:
            repository, tag = name.rsplit(":", 1)
        return cls(ref, repository, tag, digest or None)


@dataclass(frozen=True)
class Service:
    stack: str
    name: str
    file: Path  # the Compose file that declares it
    spec: dict = field(repr=False, compare=False)  # its Compose mapping, as written
    labels: dict[str, str] = field(repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.stack}/{self.name}"

    @property
    def tier(self) -> str | None:
        return self.labels.get("tier") or None

    @property
    def container_name(self) -> str | None:
        return self.spec.get("container_name")

    @property
    def container(self) -> str:
        """container_name, else Compose's default (a guess: the trailing number can grow)."""
        return self.container_name or f"{self.stack}-{self.name}-1"

    @property
    def image(self) -> Image | None:
        ref = self.spec.get("image")
        return Image.parse(str(ref)) if ref and not VARIABLE.search(str(ref)) else None

    @property
    def datastore(self) -> str | None:
        """The Datastore engine (mariadb, postgres, valkey) its image runs, or None."""
        if not self.image:
            return None
        return DATASTORE_IMAGES.get(self.image.repository.rsplit("/", 1)[-1])


class Declared:
    def __init__(self, root: Path, services: list[Service], files: dict[str, list[Path]]):
        self.root = root
        self.services = services
        self.files = files  # each Stack's Compose files: its entry file and includes
        self.stacks: dict[str, list[Service]] = {}
        for s in services:
            self.stacks.setdefault(s.stack, []).append(s)

    @classmethod
    def load(cls, root: Path, stack_root: str) -> "Declared":
        """Every Service doco-cd deploys from <stack_root>/<stack>/compose.yaml and its includes."""
        services, stack_files = [], {}
        for entry in sorted((root / stack_root).glob("*/compose.yaml")):
            stack = entry.parent.name  # doco-cd's Compose project name
            files = compose_files(entry)
            stack_files[stack] = files
            found = []
            for f in files:
                for name, spec in ((yaml.safe_load(f.read_text()) or {}).get("services") or {}).items():
                    spec = spec or {}
                    found.append(Service(stack, name, f, spec, labels_of(spec)))
            services += sorted(found, key=lambda s: s.name)
        return cls(root, services, stack_files)

    def resource_names(self, kind: str) -> dict[str, str]:
        """Host names of the top-level `volumes` or `networks` the Stacks declare, each to its Stack.

        Compose's naming: an explicit `name:`, else the key for an external one, else <stack>_<key>.
        A name that interpolates a variable is returned as written.
        """
        names = {}
        for stack, files in self.files.items():
            for f in files:
                for key, spec in ((yaml.safe_load(f.read_text()) or {}).get(kind) or {}).items():
                    spec = spec or {}
                    external = spec.get("external")
                    name = (
                        spec.get("name")
                        or (isinstance(external, dict) and external.get("name"))
                        or (key if external else f"{stack}_{key}")
                    )
                    names.setdefault(str(name), stack)
        return names

    def get(self, stack: str, service: str) -> Service | None:
        return next((s for s in self.stacks.get(stack, []) if s.name == service), None)

    def by_compose_labels(self, labels: dict[str, str]) -> Service | None:
        """The Service a container belongs to, by the labels Compose puts on it."""
        return self.get(labels.get(PROJECT, ""), labels.get(SERVICE, ""))

    def resolve(self, names: list[str]) -> list[Service]:
        """Targets: stack/service, a bare Stack name (all its Services), or a unique bare Service name."""
        found: dict[str, Service] = {}
        for name in names:
            if "/" not in name and name in self.stacks:
                matches = self.stacks[name]
            else:
                matches = [self._one(name, allow_stack=False)]
            found.update((s.key, s) for s in matches)
        return list(found.values())

    def resolve_one(self, name: str) -> Service:
        """One Service: stack/service, a unique bare Service name, or a Stack that has only one."""
        return self._one(name, allow_stack=True)

    def _one(self, name: str, allow_stack: bool) -> Service:
        if "/" in name:
            stack, _, service = name.partition("/")
            if stack not in self.stacks:
                raise ResolveError(f"unknown Stack {stack!r}{self._suggest(stack, self.stacks)}")
            found = self.get(stack, service)
            if not found:
                have = ", ".join(s.name for s in self.stacks[stack])
                raise ResolveError(f"Stack {stack} has no Service {service!r} (it has {have})")
            return found
        matches = [s for s in self.services if s.name == name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ResolveError(f"{name!r} is ambiguous: {', '.join(s.key for s in matches)}")
        if allow_stack and name in self.stacks:
            if len(self.stacks[name]) == 1:
                return self.stacks[name][0]
            raise ResolveError(
                f"Stack {name} has several Services; name one: " + ", ".join(s.key for s in self.stacks[name])
            )
        known = {*self.stacks, *(s.name for s in self.services)}
        raise ResolveError(f"unknown Stack or Service {name!r}{self._suggest(name, known)}")

    @staticmethod
    def _suggest(name: str, known) -> str:
        close = difflib.get_close_matches(name, sorted(known), n=3)
        return f" (did you mean {' or '.join(close)}?)" if close else ""
