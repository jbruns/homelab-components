"""Upstreams a Component calls: the pins its Contract Tests use, and the version its Canary tries."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_VERSION_TAG = re.compile(r"^(?P<prefix>v?)(?P<numbers>\d+(?:\.\d+)*)(?P<suffix>-[A-Za-z][\w.]*)?$")
_PINNED_IMAGE = re.compile(
    r"^(?P<repository>[^\s:@]+(?::\d+/[^\s:@]+)?):(?P<tag>[^\s:@]+)@(?P<digest>sha256:[0-9a-f]{64})$"
)


@dataclass(frozen=True)
class Upstream:
    name: str
    repository: str
    tag: str
    digest: str
    port: int | None  # None: a CLI upstream, run once per command instead of as a service
    env: dict = field(default_factory=dict)
    ready_path: str = "/"
    privileged: bool = False

    @property
    def image(self):
        return f"{self.repository}:{self.tag}@{self.digest}"


def load_upstreams(component_dir):
    """The upstreams declared in `<component_dir>/upstreams.yaml`, in file order."""
    path = Path(component_dir) / "upstreams.yaml"
    declared = (yaml.safe_load(path.read_text()) or {}).get("upstreams") or {}
    upstreams = []
    for name, spec in declared.items():
        pinned = _PINNED_IMAGE.match(str(spec.get("image", "")))
        if not pinned:
            raise ValueError(f"{path}: upstream {name!r} image must be pinned as repository:tag@sha256:digest")
        upstreams.append(
            Upstream(
                name=name,
                repository=pinned["repository"],
                tag=pinned["tag"],
                digest=pinned["digest"],
                port=int(spec["port"]) if "port" in spec else None,
                env={k: str(v) for k, v in (spec.get("env") or {}).items()},
                ready_path=spec.get("ready_path", "/"),
                privileged=bool(spec.get("privileged", False)),
            )
        )
    return upstreams


def contract_image(upstream, environ):
    """The image a test run starts for `upstream`: its pin, unless a Canary overrides it.

    A Canary sets CONTRACT_UPSTREAM to the upstream's name and CONTRACT_IMAGE to the image to try.
    """
    if environ.get("CONTRACT_UPSTREAM") == upstream.name:
        return environ["CONTRACT_IMAGE"]
    return upstream.image


def _shape(tag):
    m = _VERSION_TAG.match(tag)
    if not m:
        return None
    numbers = tuple(int(n) for n in m["numbers"].split("."))
    return (m["prefix"], len(numbers), m["suffix"] or ""), numbers


def newest_stable(pinned_tag, tags):
    """Newest tag shaped like `pinned_tag` (same `v` prefix, number of version parts and variant suffix)."""
    pinned = _shape(pinned_tag)
    if pinned is None:
        return None
    shape, _ = pinned
    candidates = []
    for tag in tags:
        parsed = _shape(tag)
        if parsed and parsed[0] == shape:
            candidates.append((parsed[1], tag))
    return max(candidates)[1] if candidates else None
