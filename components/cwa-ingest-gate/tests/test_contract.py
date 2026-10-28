"""Contract Tests: one gate run with the calibredb of the pinned CWA image, on a Library calibredb built.

The gate runs inside the image (tests/contract_run.py) with Hardcover stubbed; the test reads the Library after.
"""

import json
import os
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
HERE = Path(__file__).resolve().parent
# As the Dockerfile sets them
ENV = ["PATH=/app/calibre:/lsiopy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
       "CALIBRE_CONFIG_DIR=/tmp/calibre-config", "PYTHONPATH=/gate"]  # fmt: skip

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>{title}</dc:title><dc:creator opf:role="aut">{author}</dc:creator><dc:language>{lang}</dc:language>
<dc:identifier id="id">urn:uuid:{title}</dc:identifier></metadata>
<manifest><item id="t" href="text.xhtml" media-type="application/xhtml+xml"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{extra}</manifest>
<spine toc="ncx"><itemref idref="t"/></spine></package>"""
NCX = """<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/>
<docTitle><text>x</text></docTitle><navMap>{points}</navMap></ncx>"""
POINT = (
    '<navPoint id="p{n}" playOrder="{n}"><navLabel><text>{n}</text></navLabel><content src="text.xhtml"/></navPoint>'
)


def epub(path, title, author="Andy Weir", lang="en", converted=False):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        extra = '<item id="css" href="page_styles.css" media-type="text/css"/>' if converted else ""
        z.writestr("content.opf", OPF.format(title=title, author=author, lang=lang, extra=extra))
        z.writestr("toc.ncx", NCX.format(points="".join(POINT.format(n=n) for n in (1, 2, 3))))
        z.writestr("text.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{title}</p></body></html>')
        if converted:
            z.writestr("page_styles.css", "")


def hc_book(id, title, description):
    return {
        "id": id, "slug": f"b-{id}", "title": title, "description": description, "rating": None, "ratings_count": 0,
        "release_date": None, "cached_contributors": [{"author": {"name": "Andy Weir"}, "contribution": None}],
        "cached_tags": {"Genre": [{"tag": "Sci-Fi", "count": 5}]}, "book_series": [], "image": None,
        "default_ebook_edition": None, "default_physical_edition": None,
    }  # fmt: skip


@pytest.mark.contract
def test_one_run_on_a_real_library(upstream, tmp_path):
    cwa = upstream("calibre-web-automated")
    lib, files, state = tmp_path / "lib", tmp_path / "files", tmp_path / "state"
    for d in (lib, files, state):
        d.mkdir()

    def run(*args, stdin=None, entrypoint="calibredb"):
        argv = cwa.command(
            "--entrypoint", entrypoint, "--user", f"{os.getuid()}:{os.getgid()}",
            *(a for e in ENV for a in ("--env", e)), "--volume", f"{SRC}:/gate:ro", "--volume", f"{HERE}:/tests:ro",
            "--volume", f"{lib}:/library", "--volume", f"{files}:/files:ro", "--volume", f"{state}:/state",
        )  # fmt: skip
        r = subprocess.run([*argv, *args], input=stdin, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout

    for name, title, kw in [
        ("old.epub", "Project Hail Mary", {"converted": True}),
        ("new.epub", "Project Hail Mary", {}),
        ("martian.epub", "Martian, The", {}),
        ("livre.epub", "Le Livre", {"lang": "fr"}),
    ]:
        epub(files / name, title, **kw)
    (files / "old.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    calibredb = ("--with-library", "/library")
    run("add", *calibredb, "--duplicates", "/files/old.epub")
    run("add_format", *calibredb, "1", "/files/old.pdf")
    run("set_metadata", *calibredb, "1", "--field", "identifiers:hardcover-id:10")
    run("add", *calibredb, "--duplicates", "/files/new.epub", "/files/martian.epub", "/files/livre.epub")

    spec = {
        "mark": 1,
        "hardcover": [hc_book(10, "Project Hail Mary", None), hc_book(20, "The Martian", "Stranded on Mars.")],
    }
    outs = {
        o["book"]: o for o in json.loads(run("/tests/contract_run.py", stdin=json.dumps(spec), entrypoint="python3"))
    }

    assert (outs[2]["result"], outs[2]["into"], outs[2]["kept"], outs[2]["step"]) == (
        "merged",
        1,
        "new file",
        "2 retail EPUB",
    )
    assert outs[3]["result"] == "written" and outs[4]["result"] == "rejected"
    db = sqlite3.connect(lib / "metadata.db")
    assert [i for (i,) in db.execute("select id from books order by id")] == [1, 3]
    assert db.execute("select format from data where book=1").fetchall() == [("EPUB",)]
    assert db.execute("select title from books where id=3").fetchone() == ("The Martian",)
    assert db.execute("select val from identifiers where book=3 and type='hardcover-id'").fetchone() == ("20",)
    tags = db.execute("select t.name from books_tags_link l join tags t on t.id=l.tag where l.book=3").fetchall()
    assert tags == [("Science Fiction",)]
    path, name = db.execute("select b.path, d.name from books b join data d on d.book=b.id where b.id=1").fetchone()
    with zipfile.ZipFile(lib / path / f"{name}.epub") as z:
        assert "page_styles.css" not in z.namelist()  # the new retail file replaced the converted one
    path, name = db.execute("select b.path, d.name from books b join data d on d.book=b.id where b.id=3").fetchone()
    with zipfile.ZipFile(lib / path / f"{name}.epub") as z:
        opf = z.read(next(n for n in z.namelist() if n.endswith(".opf"))).decode()
    assert "Stranded on Mars." in opf and "The Martian" in opf  # embedded
