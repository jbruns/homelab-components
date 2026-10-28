"""What a Book's files are, whether the Book belongs in the Library, and which of two files to keep.

The ranking compares two files step by step, and the first step on which they differ decides:
1. Format: EPUB, then reflowable Formats calibre converts well, then PDF.
2. A retail EPUB beats one calibre converted.
3. A validator's verdict. The gate runs no validator, so it skips this step.
4. An EPUB with a table of contents.
5. The larger file.
6. The file already in the Library.
"""

import functools
import os
import re
import zipfile

FORMATS = ("EPUB", "AZW3", "MOBI", "AZW", "FB2", "LIT", "TXT", "RTF", "DOCX", "PDF")
COMIC_FORMATS = ("CBR", "CBZ", "CB7", "CBT")
COMIC_TAG = re.compile(r"\b(comics?|graphic novels?|manga)\b")
NOT_A_LANGUAGE = {"und", "mul", "zxx", "mis"}
# Markers calibre's conversion leaves in an EPUB; a retail EPUB has none of them
CONVERTED = (("producer", re.compile(r'role="bkp"[^>]*>\s*calibre', re.I)),)
STEPS = ("1 format", "2 retail EPUB", "3 epubcheck", "4 table of contents", "5 larger file", "6 existing")


def inspect_epub(path):
    """Retail or converted, table-of-contents entries and declared language of an EPUB; broken if unreadable."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            opf = next((n for n in names if n.endswith(".opf")), None)
            opf_text = z.read(opf).decode("utf8", "replace") if opf else ""
            markers = [m for m, rx in CONVERTED if rx.search(opf_text)]
            if any(n.endswith("page_styles.css") for n in names):
                markers.append("page_styles.css")
            if any(re.search(r"index_split_\d+", n) for n in names):
                markers.append("index_split")
            pages = [n for n in names if n.endswith((".html", ".xhtml", ".htm"))]
            if pages and re.search(r'class="calibre\d*"', z.read(pages[len(pages) // 2]).decode("utf8", "replace")):
                markers.append("calibre classes")
            toc = 0
            for n in names:
                if n.endswith(".ncx"):
                    toc = max(toc, z.read(n).decode("utf8", "replace").count("<navPoint"))
                elif n.endswith((".xhtml", ".html")) and "nav" in n.rsplit("/", 1)[-1].lower():
                    nav = z.read(n).decode("utf8", "replace")
                    m = re.search(r'<nav[^>]*epub:type="toc".*?</nav>', nav, re.S)
                    if m:
                        toc = max(toc, m.group(0).count("<a "))
            lang = re.search(r"<dc:language[^>]*>\s*([^<\s]+)", opf_text)
            return {
                "retail": not markers,
                "markers": markers,
                "toc": toc,
                "lang": lang.group(1).casefold() if lang else None,
                "broken": False,
            }
    except (zipfile.BadZipFile, OSError, KeyError):
        return {"retail": False, "markers": ["unreadable"], "toc": 0, "lang": None, "broken": True}


def load(db, library, ids):
    """Each Book's timestamp, languages, tags, Hardcover ID and files, by id."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    books = {}
    for i, title, path, ts in db.execute(f"select id, title, path, timestamp from books where id in ({marks})", ids):
        books[i] = {"id": i, "title": title, "path": path, "timestamp": ts or "", "langs": [], "tags": [], "files": []}
    for b, code in db.execute(
        "select l.book, g.lang_code from books_languages_link l join languages g on g.id=l.lang_code "
        f"where l.book in ({marks}) order by l.item_order",
        ids,
    ):
        books[b]["langs"].append(code)
    for b, name in db.execute(
        f"select l.book, t.name from books_tags_link l join tags t on t.id=l.tag where l.book in ({marks})", ids
    ):
        books[b]["tags"].append(name)
    for b, fmt, size, name in db.execute(
        f"select book, format, uncompressed_size, name from data where book in ({marks}) order by format", ids
    ):
        rel = f"{books[b]['path']}/{name}.{fmt.lower()}"
        f = {"book": b, "format": fmt, "path": rel, "size": size or 0, "timestamp": books[b]["timestamp"]}
        if fmt == "EPUB":
            f.update(inspect_epub(os.path.join(library, rel)))
        books[b]["files"].append(f)
    return books


def usable(f):
    return f["format"] in FORMATS and not f.get("broken")


def _step_keys(f):
    """One comparable value per step (higher is better), or None where the step can't judge this file."""
    epub = f["format"] == "EPUB"
    return (
        -FORMATS.index(f["format"]) if usable(f) else -len(FORMATS),
        f["retail"] if epub else None,
        None,  # the gate runs no validator
        f["toc"] > 1 if epub else None,
        f["size"],
        (_neg(f["timestamp"]), -f["book"]),
    )


def _neg(ts):
    """Older first: invert a timestamp string so that greater means earlier."""
    return tuple(-ord(c) for c in ts)


def compare(a, b):
    """(winner, step): the first step on which a and b differ."""
    ka, kb = _step_keys(a), _step_keys(b)
    for i, step in enumerate(STEPS):
        if ka[i] is None or kb[i] is None or ka[i] == kb[i]:
            continue
        return (a, step) if ka[i] > kb[i] else (b, step)
    return a, STEPS[-1]


def rank(files):
    """Best first."""
    return sorted(files, key=functools.cmp_to_key(lambda x, y: -1 if compare(x, y)[0] is x else 1))


def deletion(book):
    """Why a Book leaves the Library (or None), and its flags. A comic tag alone is only a flag."""
    if not book["files"]:
        return "no files", []
    comic = sorted({f["format"] for f in book["files"]} & set(COMIC_FORMATS))
    if comic:
        return f"comic: {', '.join(comic)}", []
    tag = next((t for t in book["tags"] if COMIC_TAG.search(t.casefold())), None)
    flags = [f"comic? tagged {tag}"] if tag else []
    if book["langs"]:
        if not any(code.startswith("en") for code in book["langs"]):
            return f"language: {', '.join(book['langs'])}", []
        return None, flags
    lang = next((f["lang"] for f in book["files"] if f.get("lang")), None)
    if lang and lang.startswith("en"):
        return None, flags
    if lang and re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]+)*", lang) and lang.split("-")[0] not in NOT_A_LANGUAGE:
        return f"language: {lang} (from EPUB)", []
    return None, flags + ["language unknown"]
