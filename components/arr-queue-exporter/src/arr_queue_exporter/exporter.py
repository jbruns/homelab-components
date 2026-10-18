"""Prometheus exporter for the Sonarr or Radarr queue items that are stuck.

exportarr's `*_queue_total` cannot be used: it emits one sample, labelled with only the last item's state. This
reads one app's `/api/v3/queue` (including items the app could not match) on each scrape and reports each item
that needs attention, so an alert's `for` measures how long that item has been stuck, whichever stuck state it
is in. One instance watches one app; run one per Sonarr or Radarr.
`python -m arr_queue_exporter` serves /metrics and /healthz; `python -m arr_queue_exporter healthcheck` probes /healthz.
"""

import json
import sys
import urllib.request
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Sonarr ignores Radarr's parameter and vice versa, so one request shape serves both apps.
QUEUE_PATH = "/api/v3/queue?pageSize=1000&includeUnknownSeriesItems=true&includeUnknownMovieItems=true"
# Downloaded but not imported, or failed and waiting for the app to act (trackedDownloadState),
# or any item the app flags (trackedDownloadStatus). importPending is normal briefly.
STUCK_STATES = {"importPending", "importBlocked", "failedPending"}
STUCK_STATUSES = {"warning", "error"}
LABEL_LIMIT = 200

HELP = {
    "arr_queue_up": "1 if the app's queue API answered.",
    "arr_queue_items": "Queue items by tracked download state and status.",
    "arr_queue_item_stuck": "1 for each queue item that needs attention.",
    "arr_queue_item_info": "A stuck item's state, status and first message, as labels.",
}


@dataclass(frozen=True)
class Config:
    url: str
    api_key: str
    app: str
    port: int
    timeout: float

    @classmethod
    def from_env(cls, environ):
        """Config from the environment; raises ValueError naming any required variable that is unset."""
        url, api_key, app = environ.get("ARR_URL"), environ.get("ARR_API_KEY"), environ.get("ARR_APP")
        missing = [name for name, value in (("ARR_URL", url), ("ARR_API_KEY", api_key), ("ARR_APP", app)) if not value]
        if missing:
            raise ValueError(f"required environment variables not set: {', '.join(missing)}")
        return cls(
            url=url.rstrip("/"),
            api_key=api_key,
            app=app,
            port=int(environ.get("PORT", "9713")),
            timeout=float(environ.get("TIMEOUT", "10")),
        )


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def _fetch(config):
    request = urllib.request.Request(config.url + QUEUE_PATH, headers={"X-Api-Key": config.api_key})
    with urllib.request.urlopen(request, timeout=config.timeout) as response:
        return json.load(response).get("records") or []


def _message(record):
    for status_message in record.get("statusMessages") or []:
        for text in status_message.get("messages") or []:
            return text
    return record.get("errorMessage") or ""


def _state(record):
    """(trackedDownloadState, trackedDownloadStatus); unmatched items may lack both."""
    return record.get("trackedDownloadState", ""), record.get("trackedDownloadStatus", "")


def _samples(config):
    try:
        records, up = _fetch(config), 1
    except Exception as e:  # report down, keep serving
        print(f"{config.app} queue API failed: {type(e).__name__}", file=sys.stderr, flush=True)
        records, up = [], 0
    app = config.app
    yield "arr_queue_up", {"app": app}, up
    counts = Counter(_state(r) for r in records)
    for (state, status), n in sorted(counts.items()):
        yield "arr_queue_items", {"app": app, "state": state, "status": status}, n
    for r in records:
        state, status = _state(r)
        if state not in STUCK_STATES and status not in STUCK_STATUSES:
            continue
        item = {"app": app, "id": r.get("id", ""), "title": (r.get("title") or "")[:LABEL_LIMIT]}
        yield "arr_queue_item_stuck", item, 1
        yield "arr_queue_item_info", {**item, "state": state, "status": status, "message": _message(r)[:LABEL_LIMIT]}, 1


def collect(config):
    """The /metrics body for one scrape."""
    lines, seen = [], set()
    for metric, labels, value in _samples(config):
        if metric not in seen:
            seen.add(metric)
            lines += [f"# HELP {metric} {HELP[metric]}", f"# TYPE {metric} gauge"]
        rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        lines.append(f"{metric}{{{rendered}}} {value}")
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
    """Exit status for the image's healthcheck: 0 if this process answers on /healthz, without touching the app."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{config.port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main(argv, environ):
    if argv not in ([], ["healthcheck"]):
        print("usage: python -m arr_queue_exporter [healthcheck]", file=sys.stderr)
        return 2
    try:
        config = Config.from_env(environ)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if argv == ["healthcheck"]:
        return healthcheck(config)
    print(f"serving /metrics on :{config.port} for {config.app} at {config.url}", flush=True)
    serve(config).serve_forever()
    return 0
