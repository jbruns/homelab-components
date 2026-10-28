"""Shared by the unit tests, the Contract Test and the Contract Test's in-image driver (tests/contract_run.py)."""

import zipfile

from cwa_ingest_gate.hardcover import norm

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>{title}</dc:title><dc:creator opf:role="aut">{author}</dc:creator><dc:language>{lang}</dc:language>
<dc:identifier id="id">urn:uuid:{title}</dc:identifier>{meta}</metadata>
<manifest><item id="t" href="text.xhtml" media-type="application/xhtml+xml"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{extra}</manifest>
<spine toc="ncx"><itemref idref="t"/></spine></package>"""
PRODUCER = '<dc:contributor opf:role="bkp">calibre (4.99.5) [https://calibre-ebook.com]</dc:contributor>'
NCX = """<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/>
<docTitle><text>x</text></docTitle><navMap>{points}</navMap></ncx>"""
POINT = (
    '<navPoint id="p{n}" playOrder="{n}"><navLabel><text>{n}</text></navLabel><content src="text.xhtml"/></navPoint>'
)


def epub(path, title, author="Andy Weir", lang="en", converted=False, producer=False):
    """A valid EPUB; a converted one carries calibre's page_styles.css, and producer names calibre as `bkp`."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        extra = '<item id="css" href="page_styles.css" media-type="text/css"/>' if converted else ""
        meta = PRODUCER if producer else ""
        z.writestr("content.opf", OPF.format(title=title, author=author, lang=lang, extra=extra, meta=meta))
        z.writestr("toc.ncx", NCX.format(points="".join(POINT.format(n=n) for n in (1, 2, 3))))
        z.writestr("text.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{title}</p></body></html>')
        if converted:
            z.writestr("page_styles.css", "")


def hc_book(id, title="Project Hail Mary", author="Andy Weir", description=None):
    return {
        "id": id, "slug": f"b-{id}", "title": title, "description": description, "rating": None, "ratings_count": 0,
        "release_date": None, "cached_contributors": [{"author": {"name": author}, "contribution": None}],
        "cached_tags": {"Genre": [{"tag": "Sci-Fi", "count": 5}]}, "book_series": [], "image": None,
        "default_ebook_edition": None, "default_physical_edition": None,
    }  # fmt: skip


class Hardcover:
    """Hardcover with only the given books; a search finds each book whose title's words the query holds."""

    def __init__(self, *books, fail=False):
        self.books, self.fail = {b["id"]: b for b in books}, fail

    def search(self, q):
        if self.fail:
            raise RuntimeError("Hardcover: unreachable")
        return [
            {"id": b["id"], "title": b["title"], "authors": [c["author"]["name"] for c in b["cached_contributors"]],
             "users_count": 10, "isbns": [], "compilation": False}
            for b in self.books.values() if set(norm(b["title"]).split()) <= set(norm(q).split())
        ]  # fmt: skip

    def editions_by_isbn(self, i13s):
        return {}

    def books_by_goodreads(self, ids):
        return {}

    def details(self, ids):
        return {i: b for i, b in self.books.items() if i in ids}
