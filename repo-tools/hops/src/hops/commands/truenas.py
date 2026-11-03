"""hops truenas call/batch: a read-only passthrough to the TrueNAS middleware, for gaps in hops. The front end is
hops.apicall; this is its TrueNAS backend."""

import re

from hops import apicall, config, truenas
from hops.errors import UsageError

HELP = "read-only TrueNAS middleware calls (JSON-RPC), for what other hops commands lack"
# Read methods by name: TrueNAS's conventions for reads, plus reads that don't follow them.
READ_SUFFIXES = (".query", ".get_instance", ".config")
READ_METHODS = {
    "system.info",
    "system.version",
    "pool.dataset.details",
    "alert.list",
    "docker.status",
    "filesystem.stat",
    "filesystem.getacl",
}
# Fail closed: only these namespaces' reads, plus READ_METHODS, suited to Host-hygiene gaps and to
# auditing the Host's configuration, each checked against the 25.10 schema for secret-bearing fields
# that redact() must mask. Anything else may hold secrets that TrueNAS doesn't mask: app configs,
# SSH keypairs (keychaincredential), webhooks (alertservice), SNMP communities, directory-service
# and Kerberos credentials, VM and instance configs, mail, reporting exporters.
NAMESPACES = {
    "pool",
    "pool.dataset",
    "pool.snapshot",
    "pool.snapshottask",
    "pool.scrub",
    "pool.resilver",
    "disk",
    "boot",
    "boot.environment",
    "replication",
    "cloudsync",
    "service",
    "interface",
    "network.configuration",
    "staticroute",
    "sharing.smb",
    "sharing.nfs",
    "smb",
    "nfs",
    "ssh",
    "user",
    "group",
    "certificate",
    "api_key",
    "auth.twofactor",
    "system.general",
    "system.advanced",
    "system.security",
    "system.ntpserver",
    "audit",
    "update",
    "docker",
    "catalog",
    "ups",
    "tunable",
    "initshutdownscript",
    "cronjob",
}
# Query options that only filter, order or count. `select` is refused: a projection can drop the
# fields TrueNAS's own masking keys on, or rename a secret. `extra` is refused: it switches on
# per-method data (e.g. disk.query's SED passwords).
QUERY_OPTIONS = {"order_by", "count", "get", "offset", "limit"}
README = "hops' README.md (truenas call)"
READS = ", ".join(f"*{m}" for m in READ_SUFFIXES) + ", " + ", ".join(sorted(READ_METHODS))
METHOD = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)+")


def register(subparsers) -> None:
    p = subparsers.add_parser("truenas", help=HELP, description=HELP + ".")
    sub = p.add_subparsers(dest="truenas_command", metavar="<command>", required=True)
    apicall.register(
        sub,
        Backend,
        call_description=f"Call a TrueNAS read method ({READS}) in an allowed namespace (see {README}). "
        f"Every other method is refused before anything is sent. Credential-looking fields are printed as "
        f"{truenas.REDACTED}.",
        method_help="e.g. system.info or pool.query",
        param_help='one JSON value per positional parameter, e.g. \'[["name","=","tank"]]\'',
    )


def is_read(method: str) -> bool:
    return bool(METHOD.fullmatch(method)) and (method in READ_METHODS or method.endswith(READ_SUFFIXES))


def is_allowed(method: str) -> bool:
    return method in READ_METHODS or method.rsplit(".", 1)[0] in NAMESPACES


def check_query_options(options: dict) -> None:
    """Refuse query options other than filtering, ordering and counting, before connecting. The message names
    the allowed options, never the refused ones: a caller-supplied name may be anything, e.g. a secret."""
    if set(options) - QUERY_OPTIONS:
        raise UsageError(
            f"a query option is refused; allowed: {', '.join(sorted(QUERY_OPTIONS))}; "
            "select the fields you need from the output instead"
        )


class Backend:
    """TrueNAS's JSON-RPC middleware behind hops.apicall: ctx.truenas (one connection, one login with the
    EBUSY backoff), the read allowlist above, and truenas.redact."""

    api = "truenas"
    capability = "truenas"

    def __init__(self, ctx):
        self.ctx = ctx

    def check(self, method: str, params: list) -> None:
        if not is_read(method):
            raise UsageError(f"the method is not a read method; hops truenas call only reads ({READS})")
        if not is_allowed(method):
            raise UsageError(
                "the method is not on hops truenas call's allowlist, as its results may hold "
                f"secrets; the allowed namespaces are listed in {README}"
            )
        for param in params:
            if isinstance(param, dict):
                check_query_options(param)

    def open(self) -> None:
        self.ctx.truenas.open()

    def call(self, method: str, params: list):
        return self.ctx.truenas.call(method, *params)

    def redact(self, result, method: str):
        return truenas.redact(result, method)

    def _keys(self) -> list[str]:
        """The API keys to scrub: the configured one, read straight from the settings so that an invalid URL or
        another config error cannot switch scrubbing off, and the client's when one is built."""
        keys = {config.host_setting(self.ctx.repo_root, self.ctx.host, "TRUENAS_API_KEY", self.ctx.env)}
        if "truenas" in vars(self.ctx):  # a client already built (cached_property stores it on the instance)
            keys.add(self.ctx.truenas.cfg.api_key)
        return sorted((k for k in keys if k), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for key in self._keys():
            text = text.replace(key, truenas.REDACTED)
        return text

    def redact_params(self, params: list) -> list:
        return truenas.scrub_strings(truenas.redact_params(params), self._keys())
