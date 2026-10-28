"""The gate against a stub Library (metadata.db and real EPUB files), a stub Hardcover and a calibredb recorder."""

import re
import sqlite3
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cwa_ingest_gate import gate
from cwa_ingest_gate.hardcover import Vocabulary

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
VOCAB = Vocabulary({"tags": ["Fantasy", "Science Fiction"], "synonyms": {"Sci-Fi": "Science Fiction"}}, {})

SCHEMA = """
create table books (id integer primary key, title text, path text, timestamp text, pubdate text,
  has_cover int default 0, series_index real default 1);
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
RETAIL_OPF = "<package><metadata><dc:language>{lang}</dc:language></metadata></package>"
CONVERTED_OPF = (
    "<package><metadata><dc:language>{lang}</dc:language>"
    '<dc:contributor opf:role="bkp">calibre (5.0) [https://calibre-ebook.com]</dc:contributor></metadata></package>'
)


def epub(path, retail=True, lang="en"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("OEBPS/content.opf", (RETAIL_OPF if retail else CONVERTED_OPF).format(lang=lang))
        z.writestr("OEBPS/toc.ncx", "<ncx><navMap>" + "<navPoint><text>x</text></navPoint>" * 3 + "</navMap></ncx>")
        z.writestr("OEBPS/text.xhtml", "<html><body><p>text</p></body></html>")
        if not retail:
            z.writestr("OEBPS/page_styles.css", "")


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
    ):
        i = self.db.execute(
            "insert into books (title, path, timestamp) values (?, '', ?)",
            (title, added.isoformat(sep=" ")),
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
            epub(p, retail, opf_lang) if f == "EPUB" else p.write_bytes(b"x" * 100)
            self.db.execute(
                "insert into data (book, format, uncompressed_size, name) values (?, ?, ?, ?)",
                (i, f, p.stat().st_size, name),
            )
        self.db.commit()
        return i

    def _id(self, table, name):
        row = self.db.execute(f"select id from {table} where name=?", (name,)).fetchone()
        return row[0] if row else self.db.execute(f"insert into {table} (name) values (?)", (name,)).lastrowid


def hc_book(id, title="Project Hail Mary", author="Andy Weir"):
    return {
        "id": id, "slug": f"b-{id}", "title": title, "description": None, "rating": None, "ratings_count": 0,
        "release_date": None, "cached_contributors": [{"author": {"name": author}, "contribution": None}],
        "cached_tags": {"Genre": [{"tag": "Sci-Fi", "count": 5}]}, "book_series": [], "image": None,
        "default_ebook_edition": None, "default_physical_edition": None,
    }  # fmt: skip


class StubHardcover:
    """Finds each Hardcover book whose title the search names."""

    def __init__(self, *books, fail=False):
        self.books, self.fail = {b["id"]: b for b in books}, fail

    def search(self, q):
        if self.fail:
            raise RuntimeError("Hardcover: unreachable")
        return [
            {"id": b["id"], "title": b["title"], "authors": [c["author"]["name"] for c in b["cached_contributors"]],
             "users_count": 10, "isbns": [], "compilation": False}
            for b in self.books.values() if b["title"] in q
        ]  # fmt: skip

    def editions_by_isbn(self, i13s):
        return {}

    def books_by_goodreads(self, ids):
        return {}

    def details(self, ids):
        return {i: b for i, b in self.books.items() if i in ids}


class Calibredb:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        assert args[:3] == ["calibredb", "--with-library", str(self.library)]
        self.calls.append(args[3:])
        return 0, ""

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
            state, hc or StubHardcover(), VOCAB, run=calibredb, now=now,
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
    assert gate.run_once(gate.Config(library=str(lib.root)), state, StubHardcover(), VOCAB, calibredb, NOW) == []
    assert state.load() == 1 and calibredb.calls == []


def test_a_confident_match_is_written_then_embedded(lib, gatekeeper):
    i = lib.add("Project Hail Mary, The", tags=["Junk"])
    [out] = gatekeeper(StubHardcover(hc_book(10)))
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
    outs = {o["book"]: o for o in gatekeeper(StubHardcover(hc_book(10)))}
    assert outs[fr] == {"book": fr, "title": "Le Livre", "result": "rejected", "reason": "language: fra"}
    assert ["remove", "--permanent", str(fr)] in gatekeeper.calibredb.calls
    assert outs[unknown]["result"] == "flagged" and outs[unknown]["flags"] == ["language unknown"]


def test_a_pdf_book_is_flagged_to_confirm_its_layout(lib, gatekeeper):
    i = lib.add("Project Hail Mary", formats=("PDF",))
    [out] = gatekeeper(StubHardcover(hc_book(10)))
    assert out["result"] == "flagged" and out["flags"] == ["PDF only: confirm layout"]
    assert "review" in field(gatekeeper.calibredb.calls[0], "tags").split(",")
    assert gatekeeper.calibredb.calls[0][1] == str(i)


def test_a_retail_copy_replaces_the_existing_books_file(lib, gatekeeper):
    old = lib.add("Project Hail Mary", hardcover=10, retail=False, added=NOW - timedelta(days=9))
    gatekeeper.state.save(old)
    new = lib.add("Project Hail Mary", retail=True)
    [out] = gatekeeper(StubHardcover(hc_book(10)))
    assert out == {"book": new, "title": "Project Hail Mary", "result": "merged", "into": old, "kept": "new file",
                   "format": "EPUB", "step": "2 retail EPUB"}  # fmt: skip
    add_format, embed, remove = gatekeeper.calibredb.calls
    assert add_format[:2] == ["add_format", str(old)] and re.search(rf"\({new}\)/.*\.epub$", add_format[2])
    assert embed == ["embed_metadata", str(old)] and remove == ["remove", "--permanent", str(new)]


def test_a_worse_copy_is_dropped_and_the_existing_file_kept(lib, gatekeeper):
    old = lib.add("Project Hail Mary", hardcover=10, retail=True)
    gatekeeper.state.save(old)
    new = lib.add("Project Hail Mary", formats=("MOBI",))
    [out] = gatekeeper(StubHardcover(hc_book(10)))
    assert (out["result"], out["into"], out["kept"], out["step"]) == ("merged", old, "existing file", "1 format")
    assert gatekeeper.calibredb.calls == [["remove", "--permanent", str(new)]]


def test_an_epub_replaces_an_existing_pdf(lib, gatekeeper):
    old = lib.add("Project Hail Mary", hardcover=10, formats=("PDF",))
    gatekeeper.state.save(old)
    lib.add("Project Hail Mary")
    [out] = gatekeeper(StubHardcover(hc_book(10)))
    assert (out["kept"], out["step"]) == ("new file", "1 format")
    assert gatekeeper.calibredb.verbs() == ["add_format", "remove_format", "embed_metadata", "remove"]
    assert gatekeeper.calibredb.calls[1] == ["remove_format", str(old), "PDF"]


def test_a_hardcover_id_held_by_two_books_is_flagged_not_merged(lib, gatekeeper):
    a = lib.add("Project Hail Mary", hardcover=10)
    b = lib.add("Project Hail Mary", hardcover=10)
    gatekeeper.state.save(b)
    lib.add("Project Hail Mary")
    [out] = gatekeeper(StubHardcover(hc_book(10)))
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
        gatekeeper(StubHardcover(fail=True))
    assert gatekeeper.calibredb.calls == [] and gatekeeper.state.load() == 0


def test_serves_healthz_and_counts_outcomes():
    status = gate.Status()
    status.record([{"result": "written"}, {"result": "merged"}, {"result": "written"}], NOW.timestamp())
    server = gate.serve(0, status)
    port = server.server_address[1]
    import threading

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
