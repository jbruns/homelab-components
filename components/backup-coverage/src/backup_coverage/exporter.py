"""Backup coverage: is every path stack-back should back up in the newest restic snapshot?

The expected paths come from Docker, not from stack-back's own run: every container, stopped ones too, mapped to
stack-back's paths by `backup_coverage.stackback`. A path is "due" once it should be in a backup: a volume whose
container and volume both predate the newest /volumes snapshot, or a database dump whose container is older than
DATABASE_DUE_AFTER. A recreate defers the check to the next backup. A volume's live size (Docker's disk usage)
tells an empty snapshot directory that is expected (the volume is empty) from one that is not.

`python -m backup_coverage` checks every REFRESH_INTERVAL and serves the last good result on /metrics, plus /healthz;
`python -m backup_coverage healthcheck` probes /healthz.
"""

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backup_coverage.stackback import Settings, backup_paths

# Docker Engine API 1.44 is Docker 25.0 and later (the Host runs 28.3.1); also the oldest API Docker 29 serves.
DOCKER_API = "/v1.44"
DOCKER_TIMEOUT = 30
# Docker walks every volume to size it, which can take tens of seconds.
DOCKER_DISK_USAGE_TIMEOUT = 300
RESTIC_TIMEOUT = 1800

HELP = {
    "backup_coverage_expected": "Path stack-back is configured to back up.",
    "backup_coverage_due": "1 once the path should be in a backup (see the backup-coverage README).",
    "backup_coverage_present": "1 if the path is in its newest snapshot.",
    "backup_coverage_entries": "Direct children of a volume path in the newest /volumes snapshot.",
    "backup_coverage_volume_bytes": (
        "Live size of the volume now (-1 unknown); 0 means an empty snapshot dir is expected."
    ),
    "backup_coverage_snapshot_timestamp": "Newest snapshot holding the path (volumes: newest /volumes snapshot).",
}
LAST_SUCCESS_HELP = "When the last check completed."
UP_HELP = "1 if the last check reached Docker and restic."


@dataclass(frozen=True)
class Config:
    restic_repository: str
    docker_host: str = "unix:///var/run/docker.sock"
    port: int = 8002
    refresh_interval: float = 3600
    database_due_after: float = 93600
    stack_back: Settings = field(default_factory=Settings)
    restic_command: tuple = ("restic",)

    @classmethod
    def from_env(cls, environ):
        repository = environ.get("RESTIC_REPOSITORY", "")
        if not repository:
            raise ValueError("RESTIC_REPOSITORY is required")
        return cls(
            restic_repository=repository,
            docker_host=environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
            port=int(environ.get("PORT", "8002")),
            refresh_interval=float(environ.get("REFRESH_INTERVAL", "3600")),
            database_due_after=float(environ.get("DATABASE_DUE_AFTER", "93600")),
            stack_back=Settings.from_env(environ),
        )


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker_get(host, path, timeout=DOCKER_TIMEOUT):
    """JSON from GET `path` on the Docker Engine API at `host` (unix:///socket or tcp://host:port)."""
    if host.startswith("unix://"):
        conn = _UnixConnection(host.removeprefix("unix://"), timeout)
    elif host.startswith("tcp://"):
        url = urllib.parse.urlsplit(host)
        conn = http.client.HTTPConnection(url.hostname, url.port or 2375, timeout=timeout)
    else:
        raise ValueError(f"DOCKER_HOST must be unix:// or tcp://, not {host!r}")
    try:
        conn.request("GET", DOCKER_API + path)
        response = conn.getresponse()
        body = response.read()
    except OSError as e:
        raise RuntimeError(f"docker GET {path} on {host}: {e}") from None
    finally:
        conn.close()
    if response.status != 200:
        raise RuntimeError(f"docker GET {path}: HTTP {response.status}")
    return json.loads(body)


def restic(config, *args):
    """The JSON lines restic prints for `args`, read-only (--no-lock) against the configured repository.

    The repository goes to restic in RESTIC_REPOSITORY, never on its command line, and errors never repeat it: its
    URL can hold credentials.
    """
    command = [*config.restic_command, "--no-lock", "--json", *args]
    env = {**os.environ, "RESTIC_REPOSITORY": config.restic_repository}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=RESTIC_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"restic {args[0]} timed out after {RESTIC_TIMEOUT}s") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"restic {args[0]} did not run: {type(e).__name__}") from None
    if result.returncode != 0:
        error = _restic_error(result.stderr).replace(config.restic_repository, "<RESTIC_REPOSITORY>")
        error = re.sub(r"://[^/@\s]+@", "://<credentials>@", error)
        raise RuntimeError(f"restic {args[0]} exited {result.returncode}: {error}")
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith(("{", "["))]


def _restic_error(stderr):
    """The first line of restic's last error; with --json, restic 0.17+ prints it as a JSON exit_error."""
    lines = stderr.strip().splitlines()
    if not lines:
        return ""
    try:
        message = json.loads(lines[-1])["message"]
    except (ValueError, KeyError, TypeError):
        message = lines[-1]
    return message.splitlines()[0] if message else ""


def _timestamp(rfc3339):
    return datetime.fromisoformat(rfc3339).timestamp()


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def collect(config):
    """The /metrics body for one check."""
    now = time.time()
    containers = docker_get(config.docker_host, "/containers/json?all=1")

    def inspect(container_id):
        return docker_get(config.docker_host, f"/containers/{container_id}/json")["Config"]

    expected = backup_paths(containers, config.stack_back, inspect)
    volume_created = {
        v["Name"]: _timestamp(v["CreatedAt"]) for v in docker_get(config.docker_host, "/volumes")["Volumes"] or []
    }
    disk_usage = docker_get(config.docker_host, "/system/df?type=volume", timeout=DOCKER_DISK_USAGE_TIMEOUT)
    sizes = {v["Name"]: (v.get("UsageData") or {}).get("Size", -1) for v in disk_usage.get("Volumes") or []}
    snapshots = restic(config, "snapshots")[0] or []

    volume_snapshots = [s for s in snapshots if s["paths"] == ["/volumes"]]
    newest = max(volume_snapshots, key=lambda s: _timestamp(s["time"]), default=None)
    newest_ts = _timestamp(newest["time"]) if newest else 0

    database_newest = {}
    for s in snapshots:
        if len(s["paths"]) == 1 and s["paths"][0].startswith("/databases/"):
            p = s["paths"][0]
            database_newest[p] = max(database_newest.get(p, 0), _timestamp(s["time"]))

    # One non-recursive listing of every expected volume dir: the dir itself plus its children.
    volume_paths = sorted({p.path for p in expected if p.kind == "volume"})
    present, entries = set(), {}
    if newest and volume_paths:
        for node in restic(config, "ls", newest["id"], *volume_paths):
            path = node.get("path")
            if node.get("struct_type") != "node" or not path:
                continue
            if path in volume_paths:
                present.add(path)
            parent = path.rsplit("/", 1)[0]
            if parent in volume_paths:
                entries[parent] = entries.get(parent, 0) + 1

    lines = []
    for name, text in HELP.items():
        lines += [f"# HELP {name} {text}", f"# TYPE {name} gauge"]
    for p in expected:
        labels = ",".join(
            f'{key}="{_escape(value)}"'
            for key, value in (("kind", p.kind), ("project", p.project), ("service", p.service), ("path", p.path))
        )
        if p.kind == "volume":
            since = max(p.container_created, volume_created.get(p.volume, p.container_created))
            due = since < newest_ts
            is_present = p.path in present
            snapshot = newest_ts
            lines.append(f"backup_coverage_entries{{{labels}}} {entries.get(p.path, 0)}")
            lines.append(f"backup_coverage_volume_bytes{{{labels}}} {sizes.get(p.volume, -1)}")
        else:
            due = p.container_created < now - config.database_due_after
            snapshot = database_newest.get(p.path, 0)
            is_present = snapshot > 0
        lines += [
            f"backup_coverage_expected{{{labels}}} 1",
            f"backup_coverage_due{{{labels}}} {int(due)}",
            f"backup_coverage_present{{{labels}}} {int(is_present)}",
            f"backup_coverage_snapshot_timestamp{{{labels}}} {snapshot}",
        ]
    lines += [
        f"# HELP backup_coverage_last_success_timestamp {LAST_SUCCESS_HELP}",
        "# TYPE backup_coverage_last_success_timestamp gauge",
        f"backup_coverage_last_success_timestamp {time.time()}",
    ]
    return "\n".join(lines) + "\n"


class Exporter:
    """Holds the last good check and whether the last check succeeded; /metrics is empty until the first check."""

    def __init__(self, config):
        self.config = config
        self.checked = ""
        self.up = None

    def refresh(self):
        try:
            self.checked = collect(self.config)
            self.up = 1
            print(f"checked {self.checked.count('backup_coverage_expected{')} paths", flush=True)
        except Exception as e:  # keep serving the last good result, with up 0
            self.up = 0
            print(f"check failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def metrics(self):
        if self.up is None:
            return ""
        up = f"# HELP backup_coverage_up {UP_HELP}\n# TYPE backup_coverage_up gauge\nbackup_coverage_up {self.up}\n"
        return self.checked + up

    def run(self):
        while True:
            self.refresh()
            time.sleep(self.config.refresh_interval)


def serve(exporter):
    """An HTTP server for /metrics and /healthz on the configured port; the caller runs serve_forever()."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                self._reply(exporter.metrics(), "text/plain; version=0.0.4")
            elif self.path == "/healthz":
                self._reply("ok\n", "text/plain")
            else:
                self.send_error(404)

        def _reply(self, text, content_type):
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("", exporter.config.port), Handler)


def healthcheck(port):
    """Exit status for the image's healthcheck: 0 if this process answers on /healthz, without touching upstreams."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main(argv, environ):
    if argv == ["healthcheck"]:
        return healthcheck(int(environ.get("PORT", "8002")))
    if argv:
        print("usage: python -m backup_coverage [healthcheck]", file=sys.stderr)
        return 2
    try:
        config = Config.from_env(environ)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    exporter = Exporter(config)
    threading.Thread(target=exporter.run, daemon=True).start()
    print(f"serving /metrics on :{config.port}, checking every {config.refresh_interval:g}s", flush=True)
    serve(exporter).serve_forever()
    return 0
