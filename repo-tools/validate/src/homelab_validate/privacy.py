"""Denylisted names in prose (check `privacy`).

Prose is what a reader sees: Markdown in full, Python comments, docstrings and string literals (help text and
output), and the comments of shell, YAML, TOML and other `#`-comment files. Load-bearing identifiers are left
alone: code outside comments and strings (so file paths, config keys and `uses:` refs in YAML and shell), files
that are not prose (LICENSE, lockfiles, Renovate config, SOPS-encrypted files, binaries, unknown file types) and
any text an allowlist pattern covers. A match is reported by file and line with the matched text redacted.
"""

import io
import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

DENYLIST_ENV = "HOMELAB_PRIVACY_DENYLIST"
MODES = ("warn", "fail")
REDACTED = "[redacted]"
EXCERPT_LENGTH = 160

MARKDOWN = {".md", ".markdown", ".rst"}
PYTHON = {".py", ".pyi"}
HASH_COMMENTS = {".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".conf", ".alloy", ".tmpl"}
HASH_COMMENT_NAMES = {"Dockerfile", "Containerfile", "Makefile", ".gitignore", ".dockerignore", ".gitattributes"}
EXEMPT_NAMES = re.compile(
    r"(LICEN[CS]E|COPYING|NOTICE)(\..*)?|.*\.lock|.*-lock\.(json|ya?ml)|go\.sum|"
    r"renovate\.json5?|\.renovaterc(\.json5?)?|.*\.sops\..*|\.sops\.env",
    re.IGNORECASE,
)
# Renovate presets: renovate/<preset>.json.
EXEMPT_DIRS = {"renovate"}
# A `uses:` ref is load-bearing wherever it is written, a Markdown example included.
DEFAULT_ALLOW = (r"uses:\s*\S+",)
HASH_COMMENT_RE = re.compile(r"(?:^|\s)#+(.*)")
SHEBANG_RE = re.compile(r"#!.*\b(?:(?P<python>python[0-9.]*)|(?:ba|z|da)?sh)\b")


@dataclass(frozen=True)
class Match:
    path: str
    line: int
    excerpt: str


def kind(path: str, first_line: str) -> str | None:
    """How to read a file's prose: "markdown", "python", "hash", or None when the file holds none."""
    p = PurePosixPath(path)
    if EXEMPT_NAMES.fullmatch(p.name) or (len(p.parts) > 1 and p.parts[-2] in EXEMPT_DIRS and p.suffix == ".json"):
        return None
    if p.suffix.lower() in MARKDOWN:
        return "markdown"
    if p.suffix in PYTHON:
        return "python"
    if p.suffix in HASH_COMMENTS or p.name in HASH_COMMENT_NAMES or p.name.endswith(".env.example"):
        return "hash"
    if not p.suffix and (shebang := SHEBANG_RE.match(first_line)):
        return "python" if shebang["python"] else "hash"
    return None


def hash_comments(text: str) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(text.splitlines(), 1):
        if comment := HASH_COMMENT_RE.search(line):
            yield number, comment.group(1)


def python_prose(text: str) -> Iterator[tuple[int, str]]:
    """Comments and string literals (docstrings, help and output text), line by line. Tokens are read one at a
    time, so a tokenize error keeps what came before it; from the line it stopped on, every line is prose."""
    prose = {tokenize.COMMENT, tokenize.STRING}
    prose |= {
        getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING),
        getattr(tokenize, "TSTRING_MIDDLE", tokenize.STRING),
    }
    reached = 1
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            reached = token.end[0]
            if token.type in prose:
                for offset, line in enumerate(token.string.splitlines()):
                    yield token.start[0] + offset, line
    except (tokenize.TokenError, SyntaxError):
        yield from list(enumerate(text.splitlines(), 1))[reached - 1 :]


def prose(path: str, text: str) -> Iterator[tuple[int, str]]:
    """(line number, prose on that line) for every line of `text` that holds prose."""
    match kind(path, text.partition("\n")[0]):
        case "markdown":
            yield from enumerate(text.splitlines(), 1)
        case "python":
            yield from python_prose(text)
        case "hash":
            yield from hash_comments(text)


def compile_patterns(text: str) -> list[re.Pattern]:
    """One regex per non-blank line. Raises ValueError naming the line, never the pattern."""
    patterns = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                patterns.append(re.compile(line.strip()))
            except re.error:
                raise ValueError(f"pattern on line {number} is not a valid regex") from None
    return patterns


def scan(path: str, text: str, deny: list[re.Pattern], allow: list[re.Pattern]) -> list[Match]:
    """A line is reported when it holds a match no allowlist match covers; its excerpt redacts every match."""
    lines: dict[int, list[str]] = {}
    for number, segment in prose(path, text):
        lines.setdefault(number, []).append(segment)
    out = []
    for number, segments in sorted(lines.items()):
        line = " ".join(segments)
        allowed = [m.span() for pattern in allow for m in pattern.finditer(line)]
        hits = [m.span() for pattern in deny for m in pattern.finditer(line) if m.end() > m.start()]
        if any(not any(a <= start and stop <= b for a, b in allowed) for start, stop in hits):
            out.append(Match(path, number, redact(line, hits)))
    return out


def redact(line: str, spans: list[tuple[int, int]]) -> str:
    out, end = [], 0
    for start, stop in sorted(spans):
        if start >= end:
            out.append(line[end:start] + REDACTED)
        end = max(end, stop)
    out.append(line[end:])
    excerpt = "".join(out).strip()
    return excerpt if len(excerpt) <= EXCERPT_LENGTH else excerpt[: EXCERPT_LENGTH - 1] + "…"
