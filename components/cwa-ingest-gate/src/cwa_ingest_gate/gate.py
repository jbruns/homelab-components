"""The ingest gate: each run checks the Books added since the last run.

A Book that is not in English, a comic, or has no files is removed. Every other Book is matched on
Hardcover. A Book matching a Hardcover book the Library already holds merges into that Book: the better
file (by ranking.py) stays on the existing Book, which keeps its id, metadata and read status, and the new
record goes. Any other Book gets Hardcover's metadata written and embedded in its file; one that is
unmatched, uncertain, PDF only or of unknown language is tagged for review instead of guessed at.

Each run also trims every checked Book holding more than one Format down to its best file. CWA's automerge adds a
Format to a Book whose title and authors match instead of making a new record, so the gate never sees it arrive.
"""

import functools
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


def crowded(db, library, mark, cutoff):
    """Checked Books (at or below the mark) holding more than one Format, once calibre last changed them before the
    cutoff. One whose best file is unusable and already tagged review is left for the person deciding."""
    ids = [
        i
        for i, ts in db.execute(
            "select b.id, b.last_modified from books b join data d on d.book = b.id where b.id <= ? "
            "group by b.id having count(*) > 1 order by b.id",
            (mark,),
        )
        if unchanged(ts, cutoff)
    ]
    books = ranking.load(db, library, ids)
    books = [books[i] for i in ids]
    return [b for b in books if ranking.usable(ranking.rank(b["files"])[0]) or REVIEW_TAG not in b["tags"]]


def unchanged(last_modified, cutoff):
    return not last_modified or added(last_modified) <= cutoff


def settled_since(db, book, cutoff):
    """Whether calibre last changed this Book before the cutoff."""
    return unchanged(db.execute("select last_modified from books where id=?", (book,)).fetchone()[0], cutoff)


def holders(db, hid, mark):
    """The checked Books (at or below the mark) holding this Hardcover ID."""
    rows = db.execute(
        "select book from identifiers where type='hardcover-id' and val=? and book<=? order by book", (str(hid), mark)
    )
    return [b for (b,) in rows]


def library_titles(db, mark):
    """Each checked Book's id, title and authors."""
    rows = db.execute(
        "select b.id, b.title, a.name from books b left join books_authors_link l on l.book=b.id "
        "left join authors a on a.id=l.author where b.id<=? order by b.id, l.id",
        (mark,),
    )
    out = {}
    for i, title, author in rows:
        out.setdefault(i, (title, []))[1].extend([author] if author else [])
    return out


def likely_duplicates(rec, titles):
    """Checked Books with the same main title and a shared author as the Book or its Hardcover match: a copy the
    Hardcover ID can't find, because that Book has none or another one."""
    hc = rec.get("hardcover") or {}
    names = {hardcover.main_title(t) for t in (rec["title"], hc.get("title")) if t} - {""}
    authors = [*rec["authors"], *hc.get("authors", [])]
    return [
        i
        for i, (title, theirs) in titles.items()
        if hardcover.main_title(title) in names
        and hardcover.authors_match(authors, theirs)
        and not hardcover.numbers_conflict(title, rec["title"])
    ]


def flag(rec, old, reason):
    rec["flags"].append(reason)
    tags = rec["changes"].get("tags", {}).get("new", old["tags"])
    if REVIEW_TAG not in tags:
        rec["changes"]["tags"] = {"old": old["tags"], "new": sorted([*tags, REVIEW_TAG])}


@dataclass
class Step:
    """What the gate will do with one new Book: reject it, merge it into a checked Book, or write its record."""

    book: int
    title: str
    tags: list
    files: list
    reject: str | None = None
    rec: dict | None = None
    into: int | None = None
    into_files: list = field(default_factory=list)


def run_once(config, state, hc, vocab, run=hardcover.run_calibredb, now=None, count=lambda outs: None):
    """Trim every checked Book to one Format, then check every settled Book added since the last run; return one
    outcome per Book. Trimming needs no Hardcover, so a Hardcover error doesn't hold it back; `count` gets each
    batch of outcomes as soon as it is done, so trims count even when Hardcover then fails."""
    now = now or datetime.now(UTC)
    calibredb = _calibredb(config.library, run)
    db = hardcover.connect(config.library)
    try:
        mark = state.load()
        if mark is None:
            mark = db.execute("select coalesce(max(id), 0) from books").fetchone()[0]
            state.save(mark)
            log({"start": "checking Books added after this one", "book": mark})
            return []
        books = crowded(db, config.library, mark, now - config.settle)
    finally:
        db.close()
    outs = []
    for book in books:
        out = {"book": book["id"], "title": book["title"]}
        _attempt(calibredb, book["id"], book["tags"], out, functools.partial(trim, calibredb, book, out))
        outs.append(out)
    count(outs)
    db = hardcover.connect(config.library)
    try:
        steps = plan(db, config, mark, hc, vocab, now, trimmed={o["book"] for o in outs})
    finally:
        db.close()
    checked = apply(steps, calibredb, config.library, state)
    count(checked)
    return outs + checked


def plan(db, config, mark, hc, vocab, now, trimmed=frozenset()):
    """Every decision, before anything is written: a Hardcover error leaves the whole batch for the next run.
    A merge waits while its Book settles, unless the gate itself just changed that Book (`trimmed`)."""
    ids = settled(db, mark, now - config.settle)
    books = ranking.load(db, config.library, ids)
    steps, checked, extra = [], [], {}
    for i in ids:
        b = books[i]
        steps.append(Step(i, b["title"], sorted(b["tags"]), b["files"]))
        reason, flags = ranking.deletion(b)
        if reason:
            steps[-1].reject = reason
            continue
        if {f["format"] for f in b["files"]} == {"PDF"}:
            flags.append("PDF only: confirm layout")
        checked.append(hardcover.current(db, config.library, i))
        extra[i] = flags
    if not checked:
        return steps
    recs = {r["book"]: r for r in hardcover.plan(checked, hc, vocab, config.threshold)}
    olds = {b["id"]: b["fields"] for b in checked}
    titles = library_titles(db, mark)
    for step in steps:
        rec = step.rec = recs.get(step.book)
        if not rec:
            continue
        old, planned, rec["flags"] = olds[step.book], rec["flags"], []
        for reason in extra[step.book]:
            flag(rec, old, reason)
        rec["flags"] += planned
        if rec.pop("delete", None):
            flag(rec, old, "omnibus: holds Books also arriving now")
        hid = (rec.get("hardcover") or {}).get("id")
        held = holders(db, hid, mark) if rec["status"] == "match" else []
        if len(held) > 1:
            flag(rec, old, f"Hardcover book {hid} is also held by {', '.join(map(str, held))}")
        elif held and held[0] not in trimmed and not settled_since(db, held[0], now - config.settle):
            # CWA may still be adding a Format to that Book: this Book and every one after it wait
            del steps[steps.index(step) :]
            break
        elif held:
            step.into, step.into_files = held[0], ranking.load(db, config.library, held)[held[0]]["files"]
        else:
            for i in likely_duplicates(rec, titles):
                flag(rec, old, f"may duplicate Book {i}")
    return steps


class CalibredbError(Exception):
    pass


def _calibredb(library, run):
    def calibredb(*args):
        rc, msg = run(["calibredb", "--with-library", library, *args])
        if rc:
            raise CalibredbError(f"{args[0]}: {(msg.splitlines() or ['exit status ' + str(rc)])[-1]}")

    return calibredb


def _attempt(calibredb, book, tags, out, action):
    """Run one Book's action and log its outcome; a Book whose action fails is tagged review."""
    try:
        action()
    except Exception as e:
        out.update(result="failed", reason=str(e))
        try:
            calibredb("set_metadata", str(book), "--field", "tags:" + ",".join(sorted({*tags, REVIEW_TAG})))
        except Exception:
            pass
    log(out)


def apply(steps, calibredb, library, state):
    """Carry out each Step, moving the mark past it whatever happens. A Step that fails is tagged review."""
    outs = []
    with tempfile.TemporaryDirectory() as tmp:
        for step in steps:
            out = {"book": step.book, "title": step.title}

            def action(step=step, out=out):
                if step.reject:
                    out.update(result="rejected", reason=step.reject)
                    calibredb("remove", "--permanent", str(step.book))
                elif step.into:
                    merge(calibredb, library, step, out)
                else:
                    out.update(result="flagged" if step.rec["flags"] else "written", flags=step.rec["flags"])
                    write(calibredb, step.rec, tmp)

            _attempt(calibredb, step.book, step.tags, out, action)
            outs.append(out)
            state.save(step.book)
    return outs


def write(calibredb, rec, tmp):
    cover = None
    if "cover" in rec["changes"]:
        cover = os.path.join(tmp, f"{rec['book']}.img")
        try:
            if not hardcover.download(rec["changes"]["cover"]["new"]["url"], cover):
                cover = None
        except Exception:
            cover = None
    calibredb("set_metadata", str(rec["book"]), *hardcover.calibredb_fields(rec["changes"], cover))
    calibredb("embed_metadata", str(rec["book"]))


def merge(calibredb, library, step, out):
    """Keep the better file on the checked Book, then remove the new record."""
    new = ranking.rank(step.files)[0]
    if step.into_files:
        winner, decided = ranking.compare(new, ranking.rank(step.into_files)[0])
    else:
        winner, decided = new, ranking.STEPS[0]
    out.update(result="merged", into=step.into, kept="new file" if winner is new else "existing file")
    out.update(format=winner["format"], step=decided)
    if winner is new:
        calibredb("add_format", str(step.into), os.path.join(library, new["path"]))
        for f in step.into_files:
            if f["format"] != new["format"]:
                calibredb("remove_format", str(step.into), f["format"])
        calibredb("embed_metadata", str(step.into))
    calibredb("remove", "--permanent", str(step.book))


def trim(calibredb, book, out):
    """Keep the Book's best file and remove its other Formats; a Book with no usable file is flagged instead."""
    files = ranking.rank(book["files"])
    best, rest = files[0], files[1:]
    if not ranking.usable(best):
        out.update(result="flagged", flags=["no usable Format"])
        calibredb("set_metadata", str(book["id"]), "--field", "tags:" + ",".join(sorted({*book["tags"], REVIEW_TAG})))
        return
    out.update(result="trimmed", kept=best["format"], removed=[f["format"] for f in rest])
    out.update(step=ranking.compare(best, rest[0])[1])
    # Embed first: once the other Formats are gone the Book no longer needs trimming, so a failed embed would stay
    calibredb("embed_metadata", "--only-formats", best["format"], str(book["id"]))
    for f in rest:
        calibredb("remove_format", str(book["id"]), f["format"])


class Status:
    """What /metrics reports: Books by outcome, and when a run last finished."""

    def __init__(self):
        self.counts, self.last_success, self.lock = Counter(), 0.0, threading.Lock()

    def count(self, outs):
        with self.lock:
            self.counts.update(o["result"] for o in outs)

    def succeeded(self, ts):
        with self.lock:
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

    def once(count=lambda outs: None):
        return run_once(config, state, hc, hardcover.load_vocabulary(tags, authors), count=count)

    if argv == ["once"]:
        once()
        return 0
    status = Status()
    server = serve(port, status)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log({"start": f"serving /metrics on :{port}, checking every {interval:g}s"})
    while True:
        try:
            once(status.count)
            status.succeeded(time.time())
        except Exception:
            traceback.print_exc()
        time.sleep(interval)
