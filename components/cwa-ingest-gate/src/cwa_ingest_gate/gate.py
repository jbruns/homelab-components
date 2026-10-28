"""The ingest gate: each run checks the Books added since the last run.

A Book that is not in English, a comic, or has no files is removed. Every other Book is matched on
Hardcover. A Book matching a Hardcover book the Library already holds merges into that Book: the better
file (by ranking.py) stays on the existing Book, which keeps its id, metadata and read status, and the new
record goes. Any other Book gets Hardcover's metadata written and embedded in its file; one that is
unmatched, uncertain, PDF only or of unknown language is tagged for review instead of guessed at.
"""

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import hardcover, ranking
from .hardcover import REVIEW_TAG


@dataclass
class Config:
    library: str = hardcover.LIBRARY
    settle: timedelta = field(default_factory=lambda: timedelta(minutes=2))
    threshold: float = hardcover.THRESHOLD


class State:
    """The highest Book id the gate has checked, in a file that survives restarts."""

    def __init__(self, directory):
        self.path = os.path.join(directory, "mark")

    def load(self):
        try:
            with open(self.path) as f:
                return int(f.read().strip())
        except FileNotFoundError:
            return None

    def save(self, mark):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            f.write(f"{mark}\n")
        os.replace(tmp, self.path)


def log(out):
    print(json.dumps(out, ensure_ascii=False), file=sys.stderr, flush=True)


def added(ts):
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def settled(db, mark, cutoff):
    """Ids above the mark, oldest first, up to the first Book added after the cutoff: calibre may still be
    writing that one, and the mark can only move past Books in order."""
    out = []
    for i, ts in db.execute("select id, timestamp from books where id > ? order by id", (mark,)):
        if added(ts) > cutoff:
            break
        out.append(i)
    return out


def holders(db, hid, batch):
    rows = db.execute("select book from identifiers where type='hardcover-id' and val=? order by book", (str(hid),))
    return [b for (b,) in rows if b not in batch]


def flag(rec, old, reason):
    rec["flags"].append(reason)
    tags = rec["changes"].get("tags", {}).get("new", old["tags"])
    if REVIEW_TAG not in tags:
        rec["changes"]["tags"] = {"old": old["tags"], "new": sorted([*tags, REVIEW_TAG])}


def run_once(config, state, hc, vocab, run=hardcover.run_calibredb, now=None):
    """Check every settled Book added since the last run; return one outcome per Book."""
    now = now or datetime.now(UTC)
    db = hardcover.connect(config.library)
    try:
        mark = state.load()
        if mark is None:
            mark = db.execute("select coalesce(max(id), 0) from books").fetchone()[0]
            state.save(mark)
            log({"start": "checking Books added after this one", "book": mark})
            return []
        ids = settled(db, mark, now - config.settle)
        books = ranking.load(db, config.library, ids)
        steps, checked, extra = [], [], {}
        for i in ids:
            reason, flags = ranking.deletion(books[i])
            if reason:
                steps.append((i, "reject", reason))
                continue
            if {f["format"] for f in books[i]["files"]} == {"PDF"}:
                flags.append("PDF only: confirm layout")
            checked.append(hardcover.current(db, config.library, i))
            extra[i] = flags
            steps.append((i, "plan", None))
        recs = {r["book"]: r for r in hardcover.plan(checked, hc, vocab, config.threshold)} if checked else {}
        olds = {b["id"]: b["fields"] for b in checked}
        for i, rec in recs.items():
            planned, rec["flags"] = rec["flags"], []
            for reason in extra[i]:
                flag(rec, olds[i], reason)
            rec["flags"] += planned
            if rec.pop("delete", None):
                flag(rec, olds[i], "omnibus: holds Books also arriving now")
            hid = (rec.get("hardcover") or {}).get("id")
            held = holders(db, hid, set(ids)) if rec["status"] == "match" else []
            if len(held) > 1:
                flag(rec, olds[i], f"Hardcover book {hid} is also held by {', '.join(map(str, held))}")
            elif held:
                rec["into"] = held[0]
                rec["into_files"] = ranking.load(db, config.library, held)[held[0]]["files"]
    finally:
        db.close()

    cli = ["calibredb", "--with-library", config.library]
    outs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, action, reason in steps:
            book = books[i]
            out = {"book": i, "title": book["title"]}
            if action == "reject":
                rc, msg = run([*cli, "remove", "--permanent", str(i)])
                out.update(result="rejected", reason=reason)
            elif "into" in recs[i]:
                rc, msg = merge(run, cli, config.library, recs[i], book["files"], out)
            else:
                rc, msg = write(run, cli, recs[i], tmp)
                out.update(result="flagged" if recs[i]["flags"] else "written", flags=recs[i]["flags"])
            if rc:
                out.update(result="failed", reason=msg.splitlines()[-1:])
            log(out)
            outs.append(out)
            state.save(i)
    return outs


def write(run, cli, rec, tmp):
    cover = None
    if "cover" in rec["changes"]:
        cover = os.path.join(tmp, f"{rec['book']}.img")
        try:
            if not hardcover.download(rec["changes"]["cover"]["new"]["url"], cover):
                cover = None
        except Exception:
            cover = None
    rc, msg = run([*cli, "set_metadata", str(rec["book"]), *hardcover.calibredb_fields(rec["changes"], cover)])
    return (rc, msg) if rc else run([*cli, "embed_metadata", str(rec["book"])])


def merge(run, cli, library, rec, files, out):
    """Keep the better file on the existing Book, then remove the new record."""
    into = rec["into"]
    new, old = ranking.rank(files)[0], ranking.rank(rec["into_files"])[0]
    winner, step = ranking.compare(new, old)
    out.update(result="merged", into=into, kept="new file" if winner is new else "existing file")
    out.update(format=winner["format"], step=step)
    if winner is new:
        steps = [["add_format", str(into), os.path.join(library, new["path"])]]
        steps += [["remove_format", str(into), f["format"]] for f in rec["into_files"] if f["format"] != new["format"]]
        steps.append(["embed_metadata", str(into)])
        for args in steps:
            rc, msg = run([*cli, *args])
            if rc:
                return rc, msg
    return run([*cli, "remove", "--permanent", str(rec["book"])])


class Status:
    """What /metrics reports: Books by outcome, and when a run last finished."""

    def __init__(self):
        self.counts, self.last_success, self.lock = Counter(), 0.0, threading.Lock()

    def record(self, outs, ts):
        with self.lock:
            self.counts.update(o["result"] for o in outs)
            self.last_success = ts

    def metrics(self):
        with self.lock:
            lines = [
                "# HELP cwa_ingest_gate_books_total Books the gate has checked, by outcome.",
                "# TYPE cwa_ingest_gate_books_total counter",
                *(f'cwa_ingest_gate_books_total{{result="{k}"}} {v}' for k, v in sorted(self.counts.items())),
                "# HELP cwa_ingest_gate_last_success_timestamp_seconds When a run last finished without error.",
                "# TYPE cwa_ingest_gate_last_success_timestamp_seconds gauge",
                f"cwa_ingest_gate_last_success_timestamp_seconds {self.last_success:.0f}",
            ]
        return "\n".join(lines) + "\n"


def serve(port, status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                body, ctype = b"ok\n", "text/plain"
            elif self.path == "/metrics":
                body, ctype = status.metrics().encode(), "text/plain; version=0.0.4"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("", port), Handler)


def healthcheck(port):
    """Exit status for the image's healthcheck: 0 if this process answers on /healthz, without touching upstreams."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main(argv, environ):
    port = int(environ.get("PORT", "9715"))
    if argv == ["healthcheck"]:
        return healthcheck(port)
    if argv not in ([], ["once"]):
        print("usage: python -m cwa_ingest_gate [once|healthcheck]", file=sys.stderr)
        return 2
    token = environ.get("HARDCOVER_TOKEN", "")
    if not token:
        print("HARDCOVER_TOKEN is required", file=sys.stderr)
        return 2
    config = Config(
        library=environ.get("LIBRARY", hardcover.LIBRARY),
        settle=timedelta(seconds=float(environ.get("SETTLE", "120"))),
        threshold=float(environ.get("THRESHOLD", str(hardcover.THRESHOLD))),
    )
    state = State(environ.get("STATE_DIR", "/state"))
    tags = environ.get("TAGS_FILE", "/config/tags.json")
    authors = environ.get("AUTHOR_OVERRIDES_FILE", "/config/author-overrides.json")
    interval = float(environ.get("INTERVAL", "300"))
    hc = hardcover.Hardcover(token)

    def once():
        return run_once(config, state, hc, hardcover.load_vocabulary(tags, authors))

    if argv == ["once"]:
        once()
        return 0
    status = Status()
    server = serve(port, status)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log({"start": f"serving /metrics on :{port}, checking every {interval:g}s"})
    while True:
        try:
            status.record(once(), time.time())
        except Exception:
            traceback.print_exc()
        time.sleep(interval)
