"""Prometheus exporter for Docker health-check status.

Reports whether each container's own health check reports healthy, read from the Docker Engine API (in production
through a read-only docker-socket-proxy). Only containers that define a health check are reported; a stopped one
reports 0, where cAdvisor drops it.

It also reports containers left under Compose's temporary `<12-hex id>_<name>` name, which an interrupted recreate
leaves behind. cAdvisor keeps a container's first name, so it can't.

`python -m container_health_exporter` serves /metrics and /healthz; `... healthcheck` probes /healthz.
"""

import http.client
import json
import re
import sys
import threading
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Served by Docker 25 and later, including Docker 29.0, whose oldest accepted version was 1.44.
API_VERSION = "v1.44"
LEFTOVER = re.compile(r"^[0-9a-f]{12}_(.+)$")
OWN_LABELS = ("container", "stack")


def _metric_label(container_label):
    name = re.sub(r"[^A-Za-z0-9_]", "_", container_label)
    return "_" + name if name[:1].isdigit() else name


@dataclass(frozen=True)
class Config:
    docker_host: str
    port: int
    timeout: float
    container_labels: tuple = ()
    # (key, value) of IGNORE_LABEL; value None matches any value.
    ignore_label: tuple | None = None

    @classmethod
    def from_env(cls, environ):
        container_labels = tuple(
            label.strip() for label in environ.get("CONTAINER_LABELS", "").split(",") if label.strip()
        )
        metric_labels = [_metric_label(label) for label in container_labels]
        clashing = sorted({name for name in metric_labels if name in OWN_LABELS or metric_labels.count(name) > 1})
        if clashing:
            raise ValueError(f"CONTAINER_LABELS: more than one metric label would be named {', '.join(clashing)}")
        key, has_value, value = environ.get("IGNORE_LABEL", "").strip().partition("=")
        return cls(
            docker_host=environ.get("DOCKER_HOST", "docker-socket-proxy:2375"),
            port=int(environ.get("PORT", "9714")),
            timeout=float(environ.get("TIMEOUT", "10")),
            container_labels=container_labels,
            ignore_label=(key, value if has_value else None) if key else None,
        )

    @property
    def docker_address(self):
        """(host, port) of DOCKER_HOST, given as `host:port` or `tcp://host:port`."""
        address = re.sub(r"^(tcp|http)://", "", self.docker_host).rstrip("/")
        host, _, port = address.rpartition(":")
        return (host, int(port)) if host else (address, 2375)


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def _labels(pairs):
    return ",".join(f'{k}="{_escape(v)}"' for k, v in pairs)


class Collector:
    """Builds /metrics from the Docker Engine API, remembering which stopped containers define a health check."""

    def __init__(self, config):
        self.config = config
        # Whether a container defines a health check, by container ID. Running containers show it in their
        # status ("Up 2 hours (healthy)"); stopped ones need an inspect, done once per ID.
        self._has_check = {}
        self._lock = threading.Lock()

    def _get(self, path):
        host, port = self.config.docker_address
        conn = http.client.HTTPConnection(host, port, timeout=self.config.timeout)
        try:
            conn.request("GET", f"/{API_VERSION}{path}")
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError(f"{path}: HTTP {response.status}")
            return json.load(response)
        finally:
            conn.close()

    def _ignored(self, labels):
        if self.config.ignore_label is None:
            return False
        key, value = self.config.ignore_label
        return key in labels and (value is None or labels[key] == value)

    def _health(self, container):
        """'healthy' | 'unhealthy' | 'starting' | 'stopped', or None without a health check."""
        status = container.get("Status") or ""
        if container.get("State") == "running":
            for state in ("unhealthy", "healthy", "starting"):
                if f"({state}" in status or f"health: {state}" in status:
                    return state
            return None
        cid = container["Id"]
        if cid not in self._has_check:
            test = (self._get(f"/containers/{cid}/json").get("Config") or {}).get("Healthcheck", {}).get("Test") or []
            self._has_check[cid] = bool(test) and test[0] != "NONE"
        return "stopped" if self._has_check[cid] else None

    def collect(self):
        """The /metrics body for one scrape."""
        with self._lock:
            return self._collect()

    def _collect(self):
        lines = [
            "# HELP container_health_up 1 if the Docker API answered.",
            "# TYPE container_health_up gauge",
        ]
        try:
            containers, up = self._get("/containers/json?all=1"), 1
        except Exception as e:  # report down, keep serving
            print(f"Docker API failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            containers, up = [], 0
        lines.append(f"container_health_up {up}")
        lines += [
            "# HELP container_healthy 1 while the container's health check reports healthy.",
            "# TYPE container_healthy gauge",
        ]
        leftovers = []
        live = set()
        for container in containers:
            live.add(container["Id"])
            labels = container.get("Labels") or {}
            if self._ignored(labels):
                continue
            name = (container.get("Names") or ["/"])[0].lstrip("/")
            own = [("container", name), ("stack", labels.get("com.docker.compose.project", ""))]
            if LEFTOVER.match(name):
                leftovers.append([*own, ("service", labels.get("com.docker.compose.service", ""))])
            try:
                state = self._health(container)
            except Exception as e:
                print(f"inspect failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                continue
            if state is None:
                continue
            pairs = own + [(_metric_label(label), labels.get(label, "")) for label in self.config.container_labels]
            lines.append(f"container_healthy{{{_labels(pairs)}}} {int(state == 'healthy')}")
        lines += [
            "# HELP container_leftover_name 1 for a container still named <id>_<name> by an interrupted Compose"
            " recreate.",
            "# TYPE container_leftover_name gauge",
        ]
        lines += [f"container_leftover_name{{{_labels(pairs)}}} 1" for pairs in leftovers]
        for cid in set(self._has_check) - live:
            del self._has_check[cid]
        return "\n".join(lines) + "\n"


def serve(config):
    """An HTTP server for /metrics and /healthz on the configured port; the caller runs serve_forever()."""
    collector = Collector(config)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                self._reply(collector.collect(), "text/plain; version=0.0.4")
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

    return ThreadingHTTPServer(("", config.port), Handler)


def healthcheck(config):
    """Exit status for the image's healthcheck: 0 if this process answers on /healthz, without touching Docker."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{config.port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main(argv, environ):
    try:
        config = Config.from_env(environ)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if argv == ["healthcheck"]:
        return healthcheck(config)
    if argv:
        print("usage: python -m container_health_exporter [healthcheck]", file=sys.stderr)
        return 2
    print(f"serving /metrics on :{config.port} for Docker at {config.docker_host}", flush=True)
    serve(config).serve_forever()
    return 0
