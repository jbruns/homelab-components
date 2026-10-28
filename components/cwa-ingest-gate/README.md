# cwa-ingest-gate

Checks each Book that [Calibre-Web Automated](https://github.com/crocodilestick/Calibre-Web-Automated) (CWA) adds to
its calibre Library. It matches the Book on [Hardcover](https://hardcover.app), then either writes Hardcover's
metadata, merges the Book into a copy already in the Library, removes it, or tags it `review` for a person to
check. It runs beside CWA as its own container and writes only through `calibredb`.

## What a run does

Every `INTERVAL` seconds it checks the Books whose id is above the highest id it has checked so far. It keeps that
**mark** in `STATE_DIR/mark`. The first run checks nothing: it sets the mark to the newest Book, so it never
rewrites a Library that was there before it. It skips a Book that was added less than `SETTLE` seconds ago, while
calibre may still be writing it, and every Book after it.

Each new Book goes through these checks in order:

1. **Rejected**: removed with `calibredb remove --permanent` if it has no files, is a comic (CBR, CBZ, CB7 or
   CBT), or is not in English. The language comes from the Book's languages, or from its EPUB when the Book has
   none. Codes that name no language (`und`, `mul`, `zxx`, `mis`) don't count. A Book whose language is unknown
   is flagged instead.
2. **Matched** on Hardcover, strongest evidence first: an ISBN naming an edition, a Goodreads ID, an existing
   `hardcover-id`, or a title search. A search result must score at least `THRESHOLD` on title and author. Every
   match must share an author with the Book. A match that came from anything other than a search must also have a
   close title.
3. **Merged** if the Book matches a Hardcover book that exactly one checked Book (at or below the mark) already
   holds, by its `hardcover-id`. The existing Book keeps its id, metadata, shelves and read status. Its best file and the
   new Book's best file are compared by the ranking below:
   - If the new file is better, it replaces the existing Book's file (`add_format`). The existing Book's other
     Formats are removed and its metadata is embedded in the new file.
   - Either way, the new record is removed.
   - The log names the step that decided.
4. **Written**: otherwise, the Book gets Hardcover's fields (`set_metadata`), which are then embedded in its file
   (`embed_metadata`). The fields are:
   - title, authors, identifiers, series, description, publisher, publication date, rating and cover;
   - tags: Hardcover genres from the controlled list in `TAGS_FILE`.
5. **Flagged**: some Books are also tagged `review`, and the reason is logged:
   - An unmatched or uncertain Book gets only its tags mapped.
   - A matched Book that is PDF only, is of unknown language, or matches a Hardcover book that several Books
     hold, is written as above.
   - A Book that is not merged, but shares its main title and an author with a checked Book, is flagged
     `may duplicate Book N`. That Book has no `hardcover-id`, or a different one, so a person decides.

Only a person removes `review`. A Book whose `calibredb` call fails is logged as `failed` and tagged `review`, and
the mark moves past it. A Hardcover error comes before any write, so it leaves the whole batch for the next run.

**Ranking.** The first step on which two files differ decides which one stays:

1. Format: EPUB, then AZW3, MOBI, AZW, FB2, LIT, TXT, RTF, DOCX, and PDF last.
2. A retail EPUB beats one that calibre converted. Conversion leaves markers in the content: `page_styles.css`,
   `index_split_*`, and `calibre` classes. Calibre named as `bkp` contributor alone doesn't count, because editing
   a file's metadata in calibre adds it too.
3. A table of contents with more than one entry.
4. The larger file.
5. The file already in the Library.

Every outcome is logged to stderr as one JSON line.

Turn off CWA's own automatic Hardcover ID lookup while the gate runs. Otherwise CWA writes a `hardcover-id`
before the gate has checked the match.

## Why

CWA's Hardcover lookup applies the first search result without checking it, so a summary, a study guide or
another author's book can overwrite a Book's metadata. CWA's duplicate handling merges by title and author, not by
provider ID, and it does not rank the files it keeps. No upstream option or community tool checks a provider
match before applying it, or merges new arrivals into the copy a Library already holds.

## Retire when

CWA checks a provider match (shared author, title similarity) before applying it, and merges a new Book into the
Book holding the same provider ID, using a configurable ranking of Formats.

## Configuration

Every environment variable the code reads, with its default (`required` when there is none). CI fails if this
table and the code disagree.

| Variable | Default | Description |
|---|---|---|
| `HARDCOVER_TOKEN` | required | Hardcover API token, with or without the `Bearer ` prefix. |
| `LIBRARY` | `/calibre-library` | The calibre Library: the folder holding `metadata.db`. |
| `STATE_DIR` | `/state` | Writable folder for the mark. Keep it on a volume. The image's `/state` is open to any user, so a new volume mounted there is too. |
| `TAGS_FILE` | `/config/tags.json` | The controlled tag list: `{"tags": [...], "synonyms": {"name": "tag" or ["tag", ...]}}`. Read on every run. |
| `AUTHOR_OVERRIDES_FILE` | `/config/author-overrides.json` | Author names to write instead of Hardcover's: `{"Hardcover name": "Library name"}`. Read on every run. |
| `INTERVAL` | `300` | Seconds between runs. |
| `SETTLE` | `120` | Seconds a Book must be in the Library before it is checked. |
| `THRESHOLD` | `0.85` | Lowest search score accepted as a match. |
| `PORT` | `9715` | Port serving `/metrics` and `/healthz`. |

The image runs as `65532` by default. Run the container as the user and group that own the Library, and mount the Library read-write. `calibredb`
writes there, and it writes its own settings under a temporary `HOME`. `python -m cwa_ingest_gate once` runs a
single check and exits.

## Metrics

| Metric | Description |
|---|---|
| `cwa_ingest_gate_books_total{result}` | Books checked since start, by `result`: `written`, `flagged`, `merged`, `rejected` or `failed`. |
| `cwa_ingest_gate_last_success_timestamp_seconds` | When a run last finished without error; 0 before the first. |

## Healthcheck

`healthcheck` exits 0 when the gate answers on its own `/healthz`, which never calls Hardcover or `calibredb`. The
image declares it as its `HEALTHCHECK`. Compose should declare the same one (the Baseline):

```yaml
healthcheck:
  test: ["CMD", "python3", "-m", "cwa_ingest_gate", "healthcheck"]
```

## Upstreams

| Upstream | Pinned in | Contract |
|---|---|---|
| Calibre-Web Automated (calibre's `calibredb`) | [`upstreams.yaml`](upstreams.yaml) and the [`Dockerfile`](Dockerfile), which builds on the same image; move the two pins together | On a real Library: the gate reads `metadata.db`, and `set_metadata`, `embed_metadata`, `add_format`, `remove_format` and `remove --permanent` change it as the gate expects |
| Hardcover GraphQL API | not pinned (a hosted API) | Unit tests stub it with the shapes the gate reads |
