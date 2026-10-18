"""Prometheus exporter for the health of each Usenet provider (server) configured in SABnzbd.

exportarr covers SABnzbd's queue, quota and disk, but not each server's state. `api?mode=status` reports, per
server, whether it is enabled and the error or warning SABnzbd last hit on it (e.g. failed authentication, too
many connections). The API is read once per scrape.
`python -m sabnzbd_status_exporter` serves /metrics and /healthz; `... healthcheck` probes /healthz.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Metric families in output order.
HELP = {
    "sabnzbd_status_up": "1 if the status API answered.",
    "sabnzbd_status_scrape_duration_seconds": "Time taken to read the status API.",
    "sabnzbd_usenet_server_active": "1 if the server is enabled in SABnzbd.",
    "sabnzbd_usenet_server_optional": "1 if the server is optional (a failure does not fail the download).",
    "sabnzbd_usenet_server_priority": "Server priority; 0 is tried first.",
    "sabnzbd_usenet_server_connections": "Configured connections.",
    "sabnzbd_usenet_server_error": "1 while SABnzbd reports an error for the server.",
    "sabnzbd_usenet_server_error_info": "The server's current error, as a label.",
    "sabnzbd_usenet_server_warning": "1 while SABnzbd reports a warning for the server.",
    "sabnzbd_usenet_server_warning_info": "The server's current warning, as a label.",
}
MAX_MESSAGE = 200


@dataclass(frozen=True)
class Config:
    sabnzbd_url: str
    api_key: str = field(repr=False)
    port: int
    timeout: float

    @classmethod
    def from_env(cls, environ):
        return cls(
            sabnzbd_url=environ.get("SABNZBD_URL", "http://sabnzbd:8080").rstrip("/"),
            api_key=environ.get("SABNZBD_API_KEY", ""),
            port=int(environ.get("PORT", "9712")),
            timeout=float(environ.get("TIMEOUT", "10")),
        )

    @property
    def status_url(self):
        query = urllib.parse.urlencode({"mode": "status", "output": "json", "apikey": self.api_key})
        return f"{self.sabnzbd_url}/api?{query}"


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def _servers(config):
    with urllib.request.urlopen(config.status_url, timeout=config.timeout) as response:
        return json.load(response)["status"].get("servers") or []


def _samples(servers):
    for s in servers:
        name = {"server": s.get("servername", "")}
        yield "sabnzbd_usenet_server_active", name, int(bool(s.get("serveractive")))
        yield "sabnzbd_usenet_server_optional", name, int(bool(s.get("serveroptional")))
        yield "sabnzbd_usenet_server_priority", name, s.get("serverpriority") or 0
        yield "sabnzbd_usenet_server_connections", name, s.get("servertotalconn") or 0
        for kind in ("error", "warning"):
            text = (s.get(f"server{kind}") or "").strip()
            yield f"sabnzbd_usenet_server_{kind}", name, int(bool(text))
            if text:
                yield f"sabnzbd_usenet_server_{kind}_info", {**name, kind: text[:MAX_MESSAGE]}, 1


def collect(config):
    """The /metrics body for one scrape."""
    start = time.monotonic()
    try:
        servers, up = _servers(config), 1
    except Exception as e:  # report down, keep serving
        print(f"status API failed: {type(e).__name__}", flush=True)
        servers, up = [], 0
    families = {metric: [] for metric in HELP}
    families["sabnzbd_status_up"].append(({}, up))
    families["sabnzbd_status_scrape_duration_seconds"].append(({}, round(time.monotonic() - start, 3)))
    for metric, labels, value in _samples(servers):
        families[metric].append((labels, value))

    lines = []
    for metric, samples in families.items():
        if samples:
            lines += [f"# HELP {metric} {HELP[metric]}", f"# TYPE {metric} gauge"]
        for labels, value in samples:
            lbl = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
            lines.append(f"{metric}{{{lbl}}} {value}" if lbl else f"{metric} {value}")
    return "\n".join(lines) + "\n"


def serve(config):
    """An HTTP server for /metrics and /healthz on the configured port; the caller runs serve_forever()."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                self._reply(collect(config), "text/plain; version=0.0.4")
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
    """Exit status for the image's healthcheck: 0 if this process answers on /healthz, without calling SABnzbd."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{config.port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main(argv, environ):
    config = Config.from_env(environ)
    if argv == ["healthcheck"]:
        return healthcheck(config)
    if argv:
        print("usage: python -m sabnzbd_status_exporter [healthcheck]", file=sys.stderr)
        return 2
    if not config.api_key:
        print("SABNZBD_API_KEY is required", file=sys.stderr)
        return 2
    print(f"serving /metrics on :{config.port} for {config.sabnzbd_url}", flush=True)
    serve(config).serve_forever()
    return 0
