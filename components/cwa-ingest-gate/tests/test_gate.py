"""The gate against a stub Library (metadata.db and real EPUB files), a stub Hardcover and a calibredb recorder."""

import re
import sqlite3
import sys
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # gate_stubs, shared with the in-image driver
from gate_stubs import Hardcover, epub, hc_book

from cwa_ingest_gate import gate, ranking
from cwa_ingest_gate.hardcover import Vocabulary

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
VOCAB = Vocabulary({"tags": ["Fantasy", "Science Fiction"], "synonyms": {"Sci-Fi": "Science Fiction"}}, {})

SCHEMA = """
create table books (id integer primary key, title text, path text, timestamp text, pubdate text,
  has_cover int default 0, series_index real default 1, last_modified text);
create table authors (id integer primary key, name text);
create table books_authors_link (id integer primary key, book int, author int);
create table data (id integer primary key, book int, format text, uncompressed_size int, name text);
create table languages (id integer primary key, lang_code text);
create table books_languages_link (id integer primary key, book int, lang_code int, item_order int default 0);
create table tags (id integer primary key, name text);
create table books_tags_link (id integer primary key, book int, tag int);
create table identifiers (id integer primary key, book int, type text, val text);
create table ratings (id integer primary key, rating int);
create table books_ratings_link (id integer primary key, book int, rating int);
create table series (id integer primary key, name text);
create table books_series_link (id integer primary key, book int, series int);
create table comments (id integer primary key, book int, text text);
create table publishers (id integer primary key, name text);
create table books_publishers_link (id integer primary key, book int, publisher int);
"""


class Lib:
    """A stub Library; add() makes a Book with its files and returns its id."""

    def __init__(self, root):
        self.root = root
        self.db = sqlite3.connect(root / "metadata.db")
        self.db.executescript(SCHEMA)

    def add(
        self,
        title,
        author="Andy Weir",
        formats=("EPUB",),
        lang="eng",
        hardcover=None,
        retail=True,
        opf_lang="en",
        added=NOW - timedelta(hours=1),
        tags=(),
        modified=None,
    ):
        i = self.db.execute(
            "insert into books (title, path, timestamp, last_modified) values (?, '', ?, ?)",
            (title, added.isoformat(sep=" "), (modified or added).isoformat(sep=" ")),
        ).lastrowid
        path = f"{author}/{title} ({i})"
        self.db.execute("update books set path=? where id=?", (path, i))
        (self.root / path).mkdir(parents=True)
        self.db.execute("insert into books_authors_link (book, author) values (?, ?)", (i, self._id("authors", author)))
        if lang:
            self.db.execute("insert into languages (lang_code) values (?)", (lang,))
            self.db.execute("insert into books_languages_link (book, lang_code) values (?, last_insert_rowid())", (i,))
        for t in tags:
            self.db.execute("insert into books_tags_link (book, tag) values (?, ?)", (i, self._id("tags", t)))
        if hardcover:
            self.db.execute(
                "insert into identifiers (book, type, val) values (?, 'hardcover-id', ?)", (i, str(hardcover))
            )
        name = f"{title} - {author}"
        for f in formats:
            p = self.root / path / f"{name}.{f.lower()}"
            epub(p, title, author, opf_lang, converted=not retail) if f == "EPUB" else p.write_bytes(b"x" * 100)
            self.db.execute(
                "insert into data (book, format, uncompressed_size, name) values (?, ?, ?, ?)",
                (i, f, p.stat().st_size, name),
            )
        self.db.commit()
        return i

    def _id(self, table, name):
        row = self.db.execute(f"select id from {table} where name=?", (name,)).fetchone()
        return row[0] if row else self.db.execute(f"insert into {table} (name) values (?)", (name,)).lastrowid


class Calibredb:
    """Records each call; a call whose verb is in `fail` exits 1."""

    def __init__(self):
        self.calls, self.fail = [], set()

    def __call__(self, args):
        assert args[:3] == ["calibredb", "--with-library", str(self.library)]
        self.calls.append(args[3:])
        return (1, "calibredb: database is locked") if args[3] in self.fail else (0, "")

    def verbs(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def lib(tmp_path):
    (tmp_path / "library").mkdir()
    return Lib(tmp_path / "library")


@pytest.fixture
def gatekeeper(lib, tmp_path):
    """Runs the gate once, from a state that has seen every Book added before the test's first add()."""
    (tmp_path / "state").mkdir()
    state = gate.State(tmp_path / "state")
    state.save(0)
    calibredb = Calibredb()
    calibredb.library = lib.root

    def once(hc=None, now=NOW, settle=timedelta(minutes=2)):
        return gate.run_once(
            gate.Config(library=str(lib.root), settle=settle, threshold=0.85),
            state, hc or Hardcover(), VOCAB, run=calibredb, now=now,
        )  # fmt: skip

    once.state, once.calibredb = state, calibredb
    return once


def field(call, name):
    return next(a.split(":", 1)[1] for a in call if a.startswith(f"{name}:"))


def test_first_run_starts_after_the_newest_book(lib, tmp_path):
    lib.add("Old Book")
    state = gate.State(tmp_path)
    calibredb = Calibredb()
    calibredb.library = lib.root
    assert gate.run_once(gate.Config(library=str(lib.root)), state, Hardcover(), VOCAB, calibredb, NOW) == []
    assert state.load() == 1 and calibredb.calls == []


def test_a_confident_match_is_written_then_embedded(lib, gatekeeper):
    i = lib.add("Project Hail Mary, The", tags=["Junk"])
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert out == {"book": i, "title": "Project Hail Mary, The", "result": "written", "flags": []}
    set_metadata, embed = gatekeeper.calibredb.calls
    assert set_metadata[:2] == ["set_metadata", str(i)]
    assert field(set_metadata, "title") == "Project Hail Mary" and field(set_metadata, "tags") == "Science Fiction"
    assert "hardcover-id:10" in field(set_metadata, "identifiers")
    assert embed == ["embed_metadata", str(i)]
    assert gatekeeper.state.load() == i


def test_an_unmatched_book_is_flagged(lib, gatekeeper):
    i = lib.add("Unfindable", tags=["Sci-Fi"])
    [out] = gatekeeper()
    assert out["result"] == "flagged" and out["flags"] == ["unmatched: no search result passed the threshold"]
    [set_metadata, _] = gatekeeper.calibredb.calls
    assert set_metadata[:2] == ["set_metadata", str(i)] and field(set_metadata, "tags") == "Science Fiction,review"


def test_a_non_english_book_is_rejected_and_an_unknown_language_flagged(lib, gatekeeper):
    fr = lib.add("Le Livre", lang="fra")
    unknown = lib.add("Project Hail Mary", lang=None, opf_lang="und")
    outs = {o["book"]: o for o in gatekeeper(Hardcover(hc_book(10)))}
    assert outs[fr] == {"book": fr, "title": "Le Livre", "result": "rejected", "reason": "language: fra"}
    assert ["remove", "--permanent", str(fr)] in gatekeeper.calibredb.calls
    assert outs[unknown]["result"] == "flagged" and outs[unknown]["flags"] == ["language unknown"]


def test_a_pdf_book_is_flagged_to_confirm_its_layout(lib, gatekeeper):
    i = lib.add("Project Hail Mary", formats=("PDF",))
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert out["result"] == "flagged" and out["flags"] == ["PDF only: confirm layout"]
    assert "review" in field(gatekeeper.calibredb.calls[0], "tags").split(",")
    assert gatekeeper.calibredb.calls[0][1] == str(i)


def test_a_retail_copy_replaces_the_existing_books_file(lib, gatekeeper):
    old = lib.add("Project Hail Mary", hardcover=10, retail=False, added=NOW - timedelta(days=9))
    gatekeeper.state.save(old)
    new = lib.add("Project Hail Mary", retail=True)
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert out == {"book": new, "title": "Project Hail Mary", "result": "merged", "into": old, "kept": "new file",
                   "format": "EPUB", "step": "2 retail EPUB"}  # fmt: skip
    add_format, embed, remove = gatekeeper.calibredb.calls
    assert add_format[:2] == ["add_format", str(old)] and re.search(rf"\({new}\)/.*\.epub$", add_format[2])
    assert embed == ["embed_metadata", str(old)] and remove == ["remove", "--permanent", str(new)]


def test_calibre_named_as_producer_alone_does_not_make_an_epub_converted(tmp_path):
    epub(tmp_path / "edited.epub", "Piranesi", producer=True)
    epub(tmp_path / "converted.epub", "Piranesi", producer=True, converted=True)
    assert ranking.inspect_epub(tmp_path / "edited.epub")["retail"] is True
    assert ranking.inspect_epub(tmp_path / "converted.epub")["markers"] == ["page_styles.css"]


def test_a_worse_copy_is_dropped_and_the_existing_file_kept(lib, gatekeeper):
    old = lib.add("Project Hail Mary", hardcover=10, retail=True)
    gatekeeper.state.save(old)
    new = lib.add("Project Hail Mary", formats=("MOBI",))
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert (out["result"], out["into"], out["kept"], out["step"]) == ("merged", old, "existing file", "1 format")
    assert gatekeeper.calibredb.calls == [["remove", "--permanent", str(new)]]


def test_an_epub_replaces_an_existing_pdf(lib, gatekeeper):
    old = lib.add("Project Hail Mary", hardcover=10, formats=("PDF",))
    gatekeeper.state.save(old)
    lib.add("Project Hail Mary")
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert (out["kept"], out["step"]) == ("new file", "1 format")
    assert gatekeeper.calibredb.verbs() == ["add_format", "remove_format", "embed_metadata", "remove"]
    assert gatekeeper.calibredb.calls[1] == ["remove_format", str(old), "PDF"]


def test_a_hardcover_id_held_by_two_books_is_flagged_not_merged(lib, gatekeeper):
    a = lib.add("Project Hail Mary", hardcover=10)
    b = lib.add("Project Hail Mary", hardcover=10)
    gatekeeper.state.save(b)
    lib.add("Project Hail Mary")
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert out["result"] == "flagged" and out["flags"] == [f"Hardcover book 10 is also held by {a}, {b}"]
    assert gatekeeper.calibredb.verbs() == ["set_metadata", "embed_metadata"]


def test_a_book_still_settling_waits_with_every_book_after_it(lib, gatekeeper):
    settled = lib.add("Unfindable")
    lib.add("Still Arriving", added=NOW - timedelta(seconds=30))
    lib.add("Unfindable Too")
    assert [o["book"] for o in gatekeeper()] == [settled]
    assert gatekeeper.state.load() == settled


def test_a_hardcover_failure_writes_nothing_and_retries_next_time(lib, gatekeeper):
    lib.add("Project Hail Mary")
    with pytest.raises(RuntimeError):
        gatekeeper(Hardcover(fail=True))
    assert gatekeeper.calibredb.calls == [] and gatekeeper.state.load() == 0


def test_an_undetermined_language_is_flagged_not_rejected(lib, gatekeeper):
    lib.add("Project Hail Mary", lang="und", opf_lang="und")
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert out["result"] == "flagged" and out["flags"] == ["language unknown"]


def test_a_book_without_files_takes_the_new_file(lib, gatekeeper):
    old = lib.add("Project Hail Mary", hardcover=10, formats=())
    gatekeeper.state.save(old)
    new = lib.add("Project Hail Mary")
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert (out["result"], out["into"], out["kept"], out["step"]) == ("merged", old, "new file", "1 format")
    assert gatekeeper.calibredb.verbs() == ["add_format", "embed_metadata", "remove"]
    assert gatekeeper.calibredb.calls[-1] == ["remove", "--permanent", str(new)]


def test_a_failed_write_is_flagged_and_the_gate_moves_on(lib, gatekeeper):
    i = lib.add("Project Hail Mary", tags=["Sci-Fi"])
    j = lib.add("Unfindable")
    gatekeeper.calibredb.fail = {"embed_metadata"}
    outs = gatekeeper(Hardcover(hc_book(10)))
    assert [(o["book"], o["result"]) for o in outs] == [(i, "failed"), (j, "failed")]
    assert outs[0]["reason"] == "embed_metadata: calibredb: database is locked"
    assert gatekeeper.calibredb.calls[2] == ["set_metadata", str(i), "--field", "tags:Sci-Fi,review"]
    assert gatekeeper.state.load() == j


def test_an_error_on_one_book_does_not_stop_the_next(lib, gatekeeper):
    i = lib.add("Project Hail Mary")
    j = lib.add("Unfindable")

    def broken(args):
        if args[3:5] == ["set_metadata", str(i)] and len(args) > 7:
            raise OSError("calibredb: not found")
        return gatekeeper.calibredb(args)

    outs = gate.run_once(gate.Config(library=str(lib.root)), gatekeeper.state, Hardcover(hc_book(10)), VOCAB,
                         run=broken, now=NOW)  # fmt: skip
    assert [(o["book"], o["result"]) for o in outs] == [(i, "failed"), (j, "flagged")]
    assert outs[0]["reason"] == "calibredb: not found"


def test_a_copy_of_a_book_without_a_hardcover_id_is_flagged_as_a_likely_duplicate(lib, gatekeeper):
    old = lib.add("Project Hail Mary: A Novel", added=NOW - timedelta(days=9))
    lib.add("The Martian")
    gatekeeper.state.save(old + 1)
    lib.add("Project Hail Mary")
    [out] = gatekeeper(Hardcover(hc_book(10)))
    assert out["result"] == "flagged" and out["flags"] == [f"may duplicate Book {old}"]


def test_a_format_cwa_added_to_a_checked_book_is_trimmed_to_the_best(lib, gatekeeper):
    i = lib.add("Project Hail Mary", formats=("PDF", "EPUB"), hardcover=10)
    single = lib.add("The Martian", formats=("PDF",))
    gatekeeper.state.save(single)
    [out] = gatekeeper()
    assert out == {"book": i, "title": "Project Hail Mary", "result": "trimmed", "kept": "EPUB",
                   "removed": ["PDF"], "step": "1 format"}  # fmt: skip
    assert gatekeeper.calibredb.calls == [["remove_format", str(i), "PDF"], ["embed_metadata", str(i)]]
    assert gatekeeper.state.load() == single


def test_a_book_whose_formats_changed_within_settle_is_trimmed_later(lib, gatekeeper):
    i = lib.add("Project Hail Mary", formats=("MOBI", "EPUB"), modified=NOW - timedelta(seconds=30))
    gatekeeper.state.save(i)
    assert gatekeeper() == [] and gatekeeper.calibredb.calls == []
    [out] = gatekeeper(now=NOW + timedelta(minutes=5))
    assert (out["result"], out["kept"], out["removed"]) == ("trimmed", "EPUB", ["MOBI"])


def test_trimming_does_not_wait_for_hardcover(lib, gatekeeper):
    i = lib.add("Project Hail Mary", formats=("PDF", "EPUB"))
    gatekeeper.state.save(i)
    lib.add("The Martian")
    with pytest.raises(RuntimeError):
        gatekeeper(Hardcover(fail=True))
    assert gatekeeper.calibredb.verbs() == ["remove_format", "embed_metadata"]
    assert gatekeeper.state.load() == i


def test_a_book_with_no_usable_format_is_flagged_once_not_trimmed(lib, gatekeeper):
    i = lib.add("Project Hail Mary", formats=("KFX", "ZIP"), tags=["Sci-Fi"])
    gatekeeper.state.save(i)
    [out] = gatekeeper()
    assert out == {"book": i, "title": "Project Hail Mary", "result": "flagged", "flags": ["no usable Format"]}
    assert gatekeeper.calibredb.calls == [["set_metadata", str(i), "--field", "tags:Sci-Fi,review"]]
    lib.db.execute("insert into tags (name) values ('review')")
    lib.db.execute("insert into books_tags_link (book, tag) values (?, last_insert_rowid())", (i,))
    lib.db.commit()
    assert gatekeeper() == []


def test_a_failed_trim_is_flagged_and_retried_next_run(lib, gatekeeper):
    i = lib.add("Project Hail Mary", formats=("PDF", "EPUB"))
    gatekeeper.state.save(i)
    gatekeeper.calibredb.fail = {"remove_format"}
    [out] = gatekeeper()
    assert (out["result"], out["reason"]) == ("failed", "remove_format: calibredb: database is locked")
    assert gatekeeper.calibredb.calls[-1] == ["set_metadata", str(i), "--field", "tags:review"]
    gatekeeper.calibredb.fail = set()
    assert [o["result"] for o in gatekeeper()] == ["trimmed"]


def test_serves_healthz_and_counts_outcomes():
    status = gate.Status()
    status.record([{"result": "written"}, {"result": "merged"}, {"result": "written"}], NOW.timestamp())
    server = gate.serve(0, status)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as r:
            assert r.status == 200
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as r:
            body = r.read().decode()
    finally:
        server.shutdown()
    assert 'cwa_ingest_gate_books_total{result="written"} 2' in body
    assert 'cwa_ingest_gate_books_total{result="merged"} 1' in body
    assert f"cwa_ingest_gate_last_success_timestamp_seconds {NOW.timestamp():.0f}" in body


def test_the_image_builds_on_the_pinned_cwa_image():
    here = Path(__file__).resolve().parents[1]
    pin = re.search(r"image: (\S+)", (here / "upstreams.yaml").read_text()).group(1)
    assert re.search(r"^FROM (\S+)", (here / "Dockerfile").read_text(), re.M).group(1) == pin
