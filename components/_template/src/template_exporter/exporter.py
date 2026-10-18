"""Prometheus exporter for a traefik/whoami upstream: the copy-from template for new Components.

Reads the upstream's /api once per scrape and reports whether it answered and which hostname it gave.
`python -m template_exporter` serves /metrics and /healthz; `python -m template_exporter healthcheck` probes /healthz.
"""

import json
import sys
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HELP = {
    "template_upstream_up": "1 if the upstream API answered.",
    "template_upstream_info": "The hostname the upstream reported, as a label.",
}


@dataclass(frozen=True)
class Config:
    upstream_url: str
    port: int
    timeout: float

    @classmethod
    def from_env(cls, environ):
        return cls(
            upstream_url=environ.get("TEMPLATE_UPSTREAM_URL", "http://whoami").rstrip("/"),
            port=int(environ.get("PORT", "9100")),
            timeout=float(environ.get("TIMEOUT", "10")),
        )


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def collect(config):
    """The /metrics body for one scrape."""
    try:
        with urllib.request.urlopen(f"{config.upstream_url}/api", timeout=config.timeout) as response:
            hostname = json.load(response)["hostname"]
        up = 1
    except Exception as e:  # report down, keep serving
        print(f"upstream API failed: {type(e).__name__}", file=sys.stderr, flush=True)
        up = 0
    lines = [f"# HELP template_upstream_up {HELP['template_upstream_up']}", "# TYPE template_upstream_up gauge"]
    lines.append(f"template_upstream_up {up}")
    if up:
        lines += [
            f"# HELP template_upstream_info {HELP['template_upstream_info']}",
            "# TYPE template_upstream_info gauge",
        ]
        lines.append(f'template_upstream_info{{hostname="{_escape(hostname)}"}} 1')
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
    """Exit status for the image's healthcheck: 0 if this process answers on /healthz, without touching upstreams."""
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
        print("usage: python -m template_exporter [healthcheck]", file=sys.stderr)
        return 2
    print(f"serving /metrics on :{config.port} for {config.upstream_url}", flush=True)
    serve(config).serve_forever()
    return 0
