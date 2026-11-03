"""hops proxmox call/batch: a read-only passthrough to the Proxmox VE API, for gaps in hops. The front end is
hops.apicall; this is its Proxmox VE backend."""

import re

from hops import apicall, config, proxmox
from hops.errors import UsageError

HELP = "read-only Proxmox VE API GETs, for what other hops commands lack"
README = "hops' README.md (proxmox call)"
# Each path segment that names something: a node, a Guest ID, a storage, a firewall group, alias or IP set, a
# notification target or matcher, or a rule's position.
SEGMENTS = {
    "{node}": re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*"),
    "{vmid}": re.compile(r"[1-9][0-9]{2,8}"),
    "{storage}": re.compile(r"[A-Za-z][A-Za-z0-9._-]*"),
    "{name}": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*"),
    "{group}": re.compile(r"[A-Za-z][A-Za-z0-9_-]*"),
    "{pos}": re.compile(r"[0-9]+"),
}
NOTIFICATION_ENDPOINTS = ("gotify", "sendmail", "smtp", "webhook")
# Fail closed: only these GET paths, each with the query parameters its Proxmox VE 9 schema takes (path parameters
# excluded). Each was checked against that schema for its permission (the token holds only PVEAuditor) and for
# secret-bearing fields, which proxmox.redact masks. Anything else is refused before it is sent.
PATHS: dict[str, tuple[str, ...]] = {
    "nodes": (),
    "nodes/{node}/qemu": ("full",),
    "nodes/{node}/lxc": (),
    "nodes/{node}/qemu/{vmid}/config": ("current", "snapshot"),
    "nodes/{node}/lxc/{vmid}/config": ("current", "snapshot"),
    "nodes/{node}/qemu/{vmid}/status/current": (),
    "nodes/{node}/lxc/{vmid}/status/current": (),
    "nodes/{node}/storage": ("content", "enabled", "format", "storage", "target"),
    "nodes/{node}/storage/{storage}/content": ("content", "vmid"),
    "nodes/{node}/network": ("type",),
    "nodes/{node}/apt/update": (),
    "nodes/{node}/version": (),
    "nodes/{node}/firewall": (),
    "nodes/{node}/firewall/rules": (),
    "nodes/{node}/firewall/rules/{pos}": (),
    "nodes/{node}/firewall/options": (),
    "cluster/backup": (),
    "cluster/resources": ("type",),
    "cluster/firewall": (),
    "cluster/firewall/aliases": (),
    "cluster/firewall/aliases/{name}": (),
    "cluster/firewall/groups": (),
    "cluster/firewall/groups/{group}": (),
    "cluster/firewall/groups/{group}/{pos}": (),
    "cluster/firewall/ipset": (),
    "cluster/firewall/ipset/{name}": (),
    "cluster/firewall/macros": (),
    "cluster/firewall/options": (),
    "cluster/firewall/refs": ("type",),
    "cluster/firewall/rules": (),
    "cluster/firewall/rules/{pos}": (),
    "cluster/notifications": (),
    "cluster/notifications/endpoints": (),
    **{f"cluster/notifications/endpoints/{e}": () for e in NOTIFICATION_ENDPOINTS},
    **{f"cluster/notifications/endpoints/{e}/{{name}}": () for e in NOTIFICATION_ENDPOINTS},
    "cluster/notifications/targets": (),
    "cluster/notifications/matchers": (),
    "cluster/notifications/matchers/{name}": (),
    "cluster/notifications/matcher-fields": (),
    "cluster/notifications/matcher-field-values": (),
    "access/users": ("enabled", "full"),
    "access/acl": (),
    "storage": ("type",),
}
TEMPLATES = [(t, t.split("/")) for t in PATHS]


def register(subparsers) -> None:
    p = subparsers.add_parser("proxmox", help=HELP, description=HELP + ".")
    sub = p.add_subparsers(dest="proxmox_command", metavar="<command>", required=True)
    apicall.register(
        sub,
        Backend,
        call_description="GET an allowlisted Proxmox VE API path (see "
        f"{README}), relative to /api2/json. Every other path is refused before anything is sent. "
        f"Credential-looking fields are printed as {proxmox.REDACTED}.",
        method_help="an API path, e.g. nodes or nodes/pve/qemu/100/config",
        param_help='at most one: a JSON object of query parameters, e.g. \'{"content": "backup"}\'',
    )


def template(path: str) -> str | None:
    """The allowlisted path template `path` matches, or None."""
    segments = path.split("/")
    for name, parts in TEMPLATES:
        if len(parts) == len(segments) and all(
            SEGMENTS[p].fullmatch(s) if p in SEGMENTS else p == s for p, s in zip(parts, segments, strict=True)
        ):
            return name
    return None


def normalize(method: str) -> str:
    return method.strip("/")


def check_params(name: str, params: list) -> None:
    """At most one params object, of the template's own query parameters, each a scalar. Messages name what is
    allowed, never what was refused: a caller-supplied name or value may be anything, e.g. a secret."""
    if not params:
        return
    if len(params) > 1 or not isinstance(params[0], dict):
        raise UsageError("params must be at most one JSON object of query parameters")
    allowed = PATHS[name]
    if set(params[0]) - set(allowed):
        raise UsageError(
            f"a query parameter is refused; {name} takes "
            + (", ".join(allowed) if allowed else "none")
            + f" (see {README})"
        )
    if not all(isinstance(v, (str, int, bool)) and v is not None for v in params[0].values()):
        raise UsageError("query parameter values must be strings, numbers or booleans")


class Backend:
    """The Proxmox VE API behind hops.apicall: ctx.proxmox (one HTTPS connection; a token, so no login), the GET
    path allowlist above, and proxmox.redact."""

    api = "proxmox"
    capability = "proxmox"

    def __init__(self, ctx):
        self.ctx = ctx

    def check(self, method: str, params: list) -> None:
        name = template(normalize(method))
        if name is None:
            raise UsageError(
                "the path is not on hops proxmox call's allowlist of read paths, as other results may hold "
                f"secrets; the allowed paths are listed in {README}"
            )
        check_params(name, params)

    def open(self) -> None:
        self.ctx.proxmox.open()

    def call(self, method: str, params: list):
        return self.ctx.proxmox.get(normalize(method), params[0] if params else None)

    def redact(self, result, method: str):
        return proxmox.redact(result, normalize(method))

    def _secrets(self) -> list[str]:
        """The token secrets to scrub: the configured one, read straight from the settings so that an invalid URL
        or another config error cannot switch scrubbing off, and the client's when one is built."""
        secrets = {config.host_setting(self.ctx.repo_root, self.ctx.host, "PROXMOX_TOKEN_SECRET", self.ctx.env)}
        if "proxmox" in vars(self.ctx):  # a client already built (cached_property stores it on the instance)
            secrets.add(self.ctx.proxmox.cfg.token_secret)
        return sorted((s for s in secrets if s), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets():
            text = text.replace(secret, proxmox.REDACTED)
        return text

    def redact_params(self, params: list) -> list:
        return proxmox.scrub_strings(proxmox.redact_params(params), self._secrets())
