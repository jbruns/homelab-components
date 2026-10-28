"""Runs inside the pinned CWA image for the Contract Tests: one gate run on /library with real calibredb and the
Hardcover books given on stdin, printing its outcomes as JSON."""

import json
import sys
from datetime import timedelta

from cwa_ingest_gate import gate
from cwa_ingest_gate.hardcover import Vocabulary, norm


class Hardcover:
    def __init__(self, books):
        self.books = {b["id"]: b for b in books}

    def search(self, q):
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


spec = json.load(sys.stdin)
state = gate.State("/state")
state.save(spec["mark"])
vocab = Vocabulary({"tags": ["Science Fiction"], "synonyms": {"Sci-Fi": "Science Fiction"}}, {})
config = gate.Config(library="/library", settle=timedelta(0))
print(json.dumps(gate.run_once(config, state, Hardcover(spec["hardcover"]), vocab)))
