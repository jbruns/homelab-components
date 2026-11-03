"""Proxmox VE API: HTTPS GETs to `<HOPS_<NAME>_PROXMOX_URL>/api2/json/<path>` with an API token.

The connection, authentication, errors and redaction live here; feature modules (e.g. hops/guests.py) read
through `Client`.

- `load_config(repo_root, host, env)` reads the Host's `HOPS_<NAME>_PROXMOX_URL`, `_PROXMOX_TOKEN_ID`,
  `_PROXMOX_TOKEN_SECRET` and `_PROXMOX_INSECURE`. A Host without the `proxmox` capability has no Proxmox VE for
  hops, and commands that need one fail with a ConfigError (Unavailable).
- `Client(cfg).get(path, params)` returns the `data` of one GET. A token needs no login, so the client only
  connects on first use (or `open()`) and reuses that one HTTPS connection; `close()` ends it.
- The token travels only in the `Authorization: PVEAPIToken=<id>=<secret>` header: never in an argv or a log, and
  its secret is replaced by [REDACTED] in every error hops raises.
- The Host is often switched off. A connection that cannot be made (refused, no route, no answer within
  CONNECT_TIMEOUT) raises HostOff, which a report reads as "off" rather than as a failure.
- `redact(data, path)` masks credential-looking fields in a result before it is printed.
"""

import hashlib
import http.client
import json
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

from homelab_hosts import Host, env_prefix

from hops import config
from hops.errors import CallError, ConfigError, HopsError, HostTimeout

REDACTED = "[REDACTED]"
API = "/api2/json/"
CONNECT_TIMEOUT = 5.0  # a Host that is off answers nothing; say so quickly
TIMEOUT = 30.0  # a read, once connected (e.g. a backup storage's content over NFS)
# A token ID as Proxmox VE writes it: user@realm!name.
TOKEN_ID = re.compile(r"[^\s@!=:]+@[A-Za-z][A-Za-z0-9.\-_]*![A-Za-z][A-Za-z0-9.\-_]*")
TOKEN_SECRET = re.compile(r"[\x21-\x7e]+")  # printable, no spaces: it goes into a header


@dataclass(frozen=True)
class ProxmoxConfig:
    url: str  # https://<host>[:port], for messages
    netloc: str
    token_id: str
    token_secret: str = field(repr=False)
    verify: bool = True  # TLS certificate and host name
    key_name: str = "the API token"  # the .env keys it came from, for error messages
    insecure_key: str = "the INSECURE setting"  # the .env key that skips TLS verification


class Unavailable(ConfigError):
    """The Host has no Proxmox VE: hosts.yaml does not give it the proxmox capability."""


class HostOff(HopsError):
    """The Host cannot be reached: it is switched off, or off the network."""


class Untrusted(HopsError):
    """The Host's TLS certificate failed verification."""


class ProxmoxError(CallError):
    """The API answered one GET with an error status; the connection is still usable."""

    def __init__(self, message: str, status: int, path: str):
        super().__init__(message)
        self.status = status  # e.g. 403 (permission), 500 (most of Proxmox VE's own errors)
        self.path = path


def load_config(repo_root: Path, host: Host, env=None) -> ProxmoxConfig:
    """The Host's Proxmox VE endpoint and token; Unavailable when the Host lacks the `proxmox` capability."""
    if not host.has("proxmox"):
        raise Unavailable(f"Host {host.name!r} has no Proxmox VE: hosts.yaml does not give it the proxmox capability")
    settings = config.proxmox_settings(repo_root, host, env)
    prefix = env_prefix(host)
    url = urlsplit(settings.url)
    if url.scheme != "https" or not url.netloc or url.path not in ("", "/") or url.query or url.fragment:
        raise ConfigError(f"{prefix}PROXMOX_URL must be an https:// URL with no path, e.g. https://pve.example:8006")
    if "@" in url.netloc:
        raise ConfigError(
            f"{prefix}PROXMOX_URL must not contain a user name or password; hops authenticates with "
            f"{prefix}PROXMOX_TOKEN_ID and {prefix}PROXMOX_TOKEN_SECRET"
        )
    if not TOKEN_ID.fullmatch(settings.token_id):
        raise ConfigError(f"{prefix}PROXMOX_TOKEN_ID must be a Proxmox VE token ID: user@realm!name")
    if not TOKEN_SECRET.fullmatch(settings.token_secret):
        raise ConfigError(f"{prefix}PROXMOX_TOKEN_SECRET must be printable, with no spaces")
    return ProxmoxConfig(
        url=f"https://{url.netloc}",
        netloc=url.netloc,
        token_id=settings.token_id,
        token_secret=settings.token_secret,
        verify=not settings.insecure,
        key_name=f"{prefix}PROXMOX_TOKEN_ID and {prefix}PROXMOX_TOKEN_SECRET",
        insecure_key=f"{prefix}PROXMOX_INSECURE",
    )


class Transport(Protocol):
    def get(self, target: str, headers: dict[str, str]) -> tuple[int, str, bytes]: ...
    def close(self) -> None: ...


Connect = Callable[..., Transport]  # connect(netloc, *, verify, connect_timeout, timeout) -> Transport


class Client:
    def __init__(self, cfg: ProxmoxConfig, *, connect: Connect | None = None):
        self.cfg = cfg
        self._connect = connect or https_connect
        self._conn: Transport | None = None

    def open(self) -> None:
        """Connect, unless connected already. HostOff when the Host does not answer."""
        if self._conn is None:
            self._conn = self._new()

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            conn.close()

    def get(self, path: str, params: dict | None = None):
        """The `data` of `GET /api2/json/<path>?<params>`. ProxmoxError for an error status; HopsError when the
        token is rejected or the connection fails (HostOff when the Host cannot be reached at all)."""
        target = API + quote(path.strip("/"), safe="/") + (f"?{urlencode(query(params))}" if params else "")
        headers = {
            "Authorization": f"PVEAPIToken={self.cfg.token_id}={self.cfg.token_secret}",
            "Accept": "application/json",
        }
        reused = self._conn is not None
        self.open()
        try:
            status, reason, body = self._conn.get(target, headers)
        except HostOff:
            self.close()
            if not reused:
                raise
            # A kept-alive connection the server has since closed: GET is idempotent, so try once more.
            self.open()
            try:
                status, reason, body = self._conn.get(target, headers)
            except HopsError as exc:
                self.close()
                raise type(exc)(self._scrub(str(exc))) from None
        except HopsError as exc:
            self.close()
            raise type(exc)(self._scrub(str(exc))) from None
        return self._answer(path, status, reason, body)

    def _new(self) -> Transport:
        try:
            return self._connect(
                self.cfg.netloc, verify=self.cfg.verify, connect_timeout=CONNECT_TIMEOUT, timeout=TIMEOUT
            )
        except Untrusted as exc:
            raise Untrusted(
                self._scrub(f"{exc}; for the Host's self-signed certificate, set {self.cfg.insecure_key}=1")
            ) from None
        except HopsError as exc:
            raise type(exc)(self._scrub(str(exc))) from None

    def _answer(self, path: str, status: int, reason: str, body: bytes):
        if status == 401:
            raise HopsError(f"Proxmox VE at {self.cfg.url} rejected the token in {self.cfg.key_name}")
        try:
            data = json.loads(body or b"null")
        except ValueError:
            data = None
            if status == 200:
                raise HopsError(f"Proxmox VE GET {path}: the answer is not JSON") from None
        if status != 200:
            raise ProxmoxError(self._scrub(error_text(path, status, reason, data)), status, path)
        return data.get("data") if isinstance(data, dict) else None

    def _scrub(self, text: str) -> str:
        return text.replace(self.cfg.token_secret, REDACTED) if self.cfg.token_secret else text


def query(params: dict) -> dict:
    """Query parameters as Proxmox VE reads them: booleans as 1/0."""
    return {k: int(v) if isinstance(v, bool) else v for k, v in params.items()}


def error_text(path: str, status: int, reason: str, data) -> str:
    """An error answer as one line: the status, Proxmox VE's reason (its status line carries the message), and the
    names of any parameters it refused, never their values."""
    text = f"Proxmox VE GET {path}: {status} {reason}".rstrip()
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, dict) and errors:
        text += f" (parameters: {', '.join(sorted(str(k) for k in errors))})"
    return text


def https_connect(netloc: str, *, verify: bool, connect_timeout: float, timeout: float) -> Transport:
    """The real transport: one keep-alive HTTPS connection (stdlib http.client). A connection that cannot be made
    raises HostOff; a certificate that fails verification a HopsError naming the setting that skips it."""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    url = urlsplit(f"https://{netloc}")
    conn = http.client.HTTPSConnection(url.hostname, url.port or 8006, timeout=connect_timeout, context=context)
    try:
        conn.connect()
    except ssl.SSLCertVerificationError as exc:
        conn.close()
        raise Untrusted(f"Proxmox VE at https://{netloc}: TLS certificate not trusted ({exc.verify_message})") from None
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        raise HostOff(f"Proxmox VE at https://{netloc} is off or unreachable ({describe(exc)})") from None
    conn.sock.settimeout(timeout)

    class _Https:
        def get(self, target: str, headers: dict[str, str]) -> tuple[int, str, bytes]:
            try:
                conn.request("GET", target, headers=headers)
                response = conn.getresponse()
                return response.status, response.reason, response.read()
            except TimeoutError:
                raise HostTimeout(f"no answer from Proxmox VE at https://{netloc} within {timeout:g}s") from None
            except (OSError, http.client.HTTPException) as exc:
                raise HostOff(f"lost the connection to Proxmox VE at https://{netloc} ({describe(exc)})") from None

        def close(self) -> None:
            conn.close()

    return _Https()


def describe(exc: Exception) -> str:
    return type(exc).__name__ if isinstance(exc, (TimeoutError, socket.timeout)) else (str(exc) or type(exc).__name__)


# A field is a credential when its name contains one of these (case-insensitive): passwords (`cipassword`),
# secrets, tokens, and keys (`sshkeys`, a Ceph `keyring`, a PBS `encryption-key`, two-factor `keys`).
SECRET_NAMES = ("password", "passwd", "secret", "token", "key")
# Names that only look secret, from Proxmox VE 9's API schema: a VM's keyboard layout, and a user's list of token
# IDs (`tokens`, walked into, and each `tokenid`; a token's secret is never readable).
NOT_SECRET = {"keyboard", "tokens", "tokenid"}
# A Guest's config fields holding free text or raw settings, which can hold a secret in any form: QEMU `args`, raw
# `lxc` keys, an OCI container's `env` and `entrypoint`, and the Guest's notes (`description`).
GUEST_FREE_TEXT = {"args", "lxc", "env", "entrypoint", "description"}
GUEST_CONFIG = re.compile(r"nodes/[^/]+/(qemu|lxc)/[^/]+/config")
# Notification targets (cluster/notifications/...): a webhook's `header` and `secret` entries keep their names and
# lose their values; its `body` is redacted; its `url` (which can carry a token in its path or query) is
# fingerprinted, so an audit can compare it with the URL it expects without seeing it.
NAMED_VALUES = {"header", "secret"}
NOTIFICATION_FREE_TEXT = {"body"}
FINGERPRINTED = {"url"}
# A URL's user name and password, e.g. https://user:pass@host/, in any string.
URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#]*@")
PROPERTY_NAME = re.compile(r"(?:^|,)name=([^,]*)")


def secret_name(name: str) -> bool:
    name = name.lower()
    return name not in NOT_SECRET and any(s in name for s in SECRET_NAMES)


def fingerprint(text: str) -> str:
    return f"[REDACTED sha256:{hashlib.sha256(text.encode()).hexdigest()[:12]}]"


def named_value(entry) -> str:
    """A `name=<name>,value=<base64>` property string with its value redacted."""
    found = PROPERTY_NAME.search(entry) if isinstance(entry, str) else None
    return f"name={found.group(1)},value={REDACTED}" if found else REDACTED


def redact(data, path: str = ""):
    """A copy of `path`'s result with credential-looking values, a Guest's free-text config, a notification
    target's credentials, and URL userinfo in any string replaced by [REDACTED]. A boolean or null is never masked:
    it holds no secret."""
    path = path.strip("/")
    guest = bool(GUEST_CONFIG.fullmatch(path))
    notification = path.startswith("cluster/notifications/")

    def walk(x):
        if isinstance(x, str):
            return URL_USERINFO.sub(rf"\1{REDACTED}@", x)
        if isinstance(x, list):
            return [walk(v) for v in x]
        if not isinstance(x, dict):
            return x
        out = {}
        for k, v in x.items():
            name = str(k).lower()
            if v is None or isinstance(v, bool):
                out[k] = v
            elif notification and name in NAMED_VALUES:
                out[k] = [named_value(e) for e in v] if isinstance(v, list) else named_value(v)
            elif notification and name in NOTIFICATION_FREE_TEXT:
                out[k] = REDACTED
            elif notification and name in FINGERPRINTED and isinstance(v, str):
                out[k] = fingerprint(v)
            elif secret_name(name):
                out[k] = REDACTED
            elif guest and name in GUEST_FREE_TEXT:
                out[k] = REDACTED
            else:
                out[k] = walk(v)
        return out

    return walk(data)


def redact_params(params: list) -> list:
    """A copy of a call's params for echoing: credential-named query parameters masked, at any depth."""
    return redact(params)


def scrub_strings(data, secrets: list[str]):
    """A copy of `data` with each of `secrets` replaced by [REDACTED] in every string, object keys included."""

    def walk(x):
        if isinstance(x, str):
            for secret in secrets:
                x = x.replace(secret, REDACTED)
            return x
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, dict):
            return {walk(k): walk(v) for k, v in x.items()}
        return x

    return walk(data)
