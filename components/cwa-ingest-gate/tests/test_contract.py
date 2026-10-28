"""Contract Tests: one gate run with the calibredb of the pinned CWA image, on a Library calibredb built.

The gate runs inside the image (tests/contract_run.py) with Hardcover stubbed; the test reads the Library after.
"""

import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # gate_stubs, shared with the in-image driver
from gate_stubs import epub, hc_book

SRC = Path(__file__).resolve().parents[1] / "src"
HERE = Path(__file__).resolve().parent
# As the Dockerfile sets them
ENV = ["PATH=/app/calibre:/lsiopy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
       "CALIBRE_CONFIG_DIRECTORY=/tmp/calibre-config", "PYTHONPATH=/gate:/tests"]  # fmt: skip


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
        "hardcover": [hc_book(10), hc_book(20, "The Martian", description="Stranded on Mars.")],
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
