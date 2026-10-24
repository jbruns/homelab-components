"""doco-cd's REST API, called from a container on doco-cd's network with a key decrypted here.

Where doco-cd lives comes from the Host's `doco_cd` settings in hosts.yaml (homelab_hosts.DocoCd): its
Stack (the Service is `<stack>/doco-cd`), the API key file under the stack root, its address, and the
network (default `metrics`) a transport container shares with it.

The API is reachable only on doco-cd's networks. The request is sent from the transport: a running
container on that network whose busybox has `nc -e` and `timeout`, and which is not being recreated
(prometheus would stop mid-request, and doco-cd cancels a recreate whose client has gone away).

The whole HTTP request, x-api-key included, goes over ssh's stdin, so the key is never in a process
argument on either machine and never written to disk on the Host. `nc -e` hands the socket to sh,
which copies the request from stdin and then the response to stdout. A plain `… | nc` would
half-close the connection at the end of stdin, which also cancels the recreate.
"""

import json
import subprocess
from pathlib import Path
from urllib.parse import quote

from homelab_hosts import DocoCd

from hops import docker
from hops.declared import Declared, Service
from hops.errors import ConfigError, HopsError, HostTimeout
from hops.runner import Runner

SERVICE = "doco-cd"  # doco-cd's Service name inside its Stack
PREFERRED = "monitoring/prometheus"
# Whether a container can be the transport. busybox prints its usage to stderr.
PROBE = "command -v timeout >/dev/null && nc --help 2>&1 | grep -q -- '-e PROG'"
# fd 5 keeps ssh's stdin and fd 6 its stdout across nc's exec, which makes the socket stdin and
# stdout (nc keeps fd 3 for the socket). $1 is the deadline in seconds, $2 and $3 doco-cd's host and port.
SCRIPT = 'exec 5<&0 6>&1; exec timeout "$1" nc -w 10 "$2" "$3" -e sh -c \'cat <&5 && exec cat >&6 5<&-\''
# What docker exec returns when `timeout` ends the request: busybox's TERM, or coreutils' 124.
TIMED_OUT = {124, 143}
# Added to the deadline on this machine, so the Host's `timeout` normally fires first.
SSH_GRACE = 30


def decrypt(path: Path) -> str:
    """`sops -d path`, run on this machine."""
    try:
        done = subprocess.run(["sops", "-d", str(path)], capture_output=True, text=True)
    except FileNotFoundError:
        raise ConfigError("sops is not installed; hops decrypts doco-cd's API key with it") from None
    if done.returncode != 0:
        raise HopsError(f"sops -d {path} failed: {done.stderr.strip()}")
    return done.stdout


def service_key(cfg: DocoCd) -> str:
    """doco-cd's own Service, `<stack>/doco-cd`."""
    return f"{cfg.stack}/{SERVICE}"


def address(cfg: DocoCd) -> tuple[str, str]:
    """doco-cd's host and port from `address` (`host[:port]`, port 80 when omitted)."""
    host, sep, port = cfg.address.rpartition(":")
    return (host, port) if sep and host and port.isdigit() else (cfg.address, "80")


def api_key(repo_root: Path, stack_root: str, cfg: DocoCd) -> str:
    key = decrypt(repo_root / stack_root / cfg.api_secret).strip()
    if not key or "\n" in key or "\r" in key:
        raise HopsError(f"{stack_root}/{cfg.api_secret} does not hold a single-line API key")
    return key


def request(method: str, path: str, key: str, cfg: DocoCd) -> str:
    # HTTP/1.0: the response is never chunked, and doco-cd closes the connection after it.
    host = address(cfg)[0]
    return f"{method} {path} HTTP/1.0\r\nHost: {host}\r\nx-api-key: {key}\r\nContent-Length: 0\r\n\r\n"


def transport(runner: Runner, declared: Declared, excluded: list[Service], cfg: DocoCd) -> str:
    """The container to send requests from: running on doco-cd's network, able to, and not in `excluded`."""
    names = runner.docker(
        "network", "inspect", cfg.network, "--format", "{{range .Containers}}{{.Name}} {{end}}"
    ).stdout.split()
    keys = {s.key for s in excluded}
    excluded_names = {s.container_name for s in excluded} - {None}
    candidates = []
    for c in docker.inspect(runner, *names):
        service = declared.by_compose_labels(c.labels)
        if c.running and c.name not in excluded_names and not (service and service.key in keys):
            candidates.append((not (service and service.key == PREFERRED), c.name))
    for _, name in sorted(candidates):
        if runner.docker("exec", name, "sh", "-c", PROBE, check=False).returncode == 0:
            return name
    raise HopsError(
        f"no running container on the {cfg.network} network outside the Services being recreated "
        "has busybox nc -e and timeout to reach doco-cd; recreate fewer Services at once"
    )


def send(
    runner: Runner, via: str, method: str, path: str, key: str, timeout: int, cfg: DocoCd, interrupted: str = ""
) -> tuple[int, str, str]:
    """Send one request to doco-cd from container `via` within `timeout` seconds; returns the status
    code, the status line after the version, and the body. `interrupted` is appended to deadline errors."""
    suffix = f"; {interrupted}" if interrupted else ""
    try:
        result = runner.docker(
            "exec",
            "-i",
            via,
            "sh",
            "-c",
            SCRIPT,
            "sh",
            str(timeout),
            *address(cfg),
            input=request(method, path, key, cfg),
            check=False,
            timeout=timeout + SSH_GRACE,
        )
    except HostTimeout as exc:
        raise HopsError(f"{exc}{suffix}") from None
    # The Runner reads text with universal newlines, so CRLF arrives as LF.
    head, _, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    status = head.split("\n", 1)[0].split(" ", 2)
    if len(status) < 2 or not status[0].startswith("HTTP/") or not status[1].isdigit():
        if result.returncode in TIMED_OUT:
            raise HopsError(f"no answer from doco-cd within {timeout}s{suffix}")
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise HopsError(f"no HTTP response from doco-cd: {detail}")
    return int(status[1]), " ".join(status[1:]), body


def error_message(body: str) -> str:
    try:
        data = json.loads(body)
        message = " ".join(str(data[k]) for k in ("error", "details") if data.get(k))
    except (json.JSONDecodeError, AttributeError):
        message = body.strip()
    return message or "no detail"


def recreate(runner: Runner, via: str, service: Service, key: str, timeout: int, cfg: DocoCd) -> None:
    """Ask doco-cd, from container `via`, to force-recreate one Service within `timeout` seconds;
    raises HopsError unless it answers 2xx."""
    interrupted = (
        f"doco-cd's recreate of {service.key} may have been interrupted; check it with hops status {service.key}"
    )
    path = f"/v1/api/project/{quote(service.stack, safe='')}/recreate?service={quote(service.name, safe='')}"
    code, status, body = send(runner, via, "POST", path, key, timeout, cfg, interrupted)
    if not 200 <= code < 300:
        raise HopsError(f"doco-cd answered {status} for {service.key}: {error_message(body)}")


def get(runner: Runner, via: str, path: str, key: str, cfg: DocoCd, timeout: int = 30):
    """GET `path` from doco-cd, from container `via`; returns the parsed JSON body of a 2xx answer.
    A 404 raises NotFound."""
    code, status, body = send(runner, via, "GET", path, key, timeout, cfg)
    endpoint = path.split("?")[0]
    if not 200 <= code < 300:
        raise (NotFound if code == 404 else HopsError)(
            f"doco-cd answered {status} for {endpoint}: {error_message(body)}"
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise HopsError(f"doco-cd answered {endpoint} with something other than JSON") from None


class NotFound(HopsError):
    pass
