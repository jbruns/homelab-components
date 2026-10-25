"""Denylisted names in prose (check `privacy`).

Prose is what a reader sees: Markdown in full, Python comments, docstrings and string literals (help text and
output), the comments of shell, YAML, TOML and other `#`-comment files, and the comments and prose fields of
Renovate config (the text Renovate publishes in PRs and commits). Load-bearing identifiers are left alone: code
outside comments and strings (so file paths, config keys and `uses:` refs in YAML and shell), every other Renovate
field (package names, `matchStrings`, `extends`, datasources, registry URLs), files that are not prose (LICENSE,
lockfiles, SOPS-encrypted files, binaries, unknown file types) and any text an allowlist pattern covers. A match is
reported by file and line with the matched text redacted.
"""

import bisect
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
    r"(LICEN[CS]E|COPYING|NOTICE)(\..*)?|.*\.lock|.*-lock\.(json|ya?ml)|go\.sum|.*\.sops\..*|\.sops\.env",
    re.IGNORECASE,
)
# Renovate config, wherever Renovate looks for it (.github/ and .gitlab/ included), and presets renovate/<preset>.json.
RENOVATE_NAMES = re.compile(r"renovate\.json5?|\.renovaterc(\.json5?)?")
RENOVATE_DIRS = {"renovate"}
# Renovate fields whose strings it publishes as prose; every other field is load-bearing.
RENOVATE_PROSE = re.compile(
    r"description|prBodyTemplate|prTitle|groupName|prHeader|prFooter|commitBody|commitMessage.*|.*Notes"
)
# Renovate mappings whose values are prose and whose keys (column names, which prBodyColumns refers to) are not.
RENOVATE_PROSE_MAPS = {"prBodyDefinitions", "prBodyHeadingDefinitions"}
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
    if EXEMPT_NAMES.fullmatch(p.name):
        return None
    if RENOVATE_NAMES.fullmatch(p.name) or (
        len(p.parts) > 1 and p.parts[-2] in RENOVATE_DIRS and p.suffix in {".json", ".json5"}
    ):
        return "renovate"
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


# JSON5 tokens, which cover JSON: comments, strings in either quote, punctuation, and bare words (unquoted keys,
# numbers, true/false/null).
JSON5_TOKEN = re.compile(
    r"""(?P<space>\s+)|(?P<comment>//[^\n]*|/\*.*?\*/)|(?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')"""
    r"""|(?P<punct>[{}\[\]:,])|(?P<bare>[^\s{}\[\]:,"'/]+)""",
    re.DOTALL,
)
JSON5_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(\r\n|.))", re.DOTALL)
JSON5_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0", "\n": "", "\r\n": ""}


def json5_unescape(text: str) -> str:
    """Escapes decoded, a `\\uXXXX` surrogate pair combined into its character and a lone surrogate replaced by
    U+FFFD, so the result always encodes."""

    def unescape(m: re.Match) -> str:
        if m[1] or m[2]:
            return chr(int(m[1] or m[2], 16))
        return JSON5_ESCAPES.get(m[3], m[3])

    decoded = JSON5_ESCAPE.sub(unescape, text)
    return decoded.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def renovate_prose(text: str) -> Iterator[tuple[int, str]]:
    """Comments, and the strings of prose fields (RENOVATE_PROSE) at any depth, packageRules included, each at the
    line its string starts on. A string belongs to the field that holds it, or the array that holds it; every value
    of a RENOVATE_PROSE_MAPS mapping is prose. Read as JSON5, which covers JSON; from where the text stops being
    JSON5, every line is prose."""
    starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
    # One entry per open object [its current key, expecting a key next, its values are prose] or array [the key
    # that holds it, None, its values are prose].
    stack: list[list] = []

    def holds_prose() -> bool:
        return bool(stack) and (stack[-1][2] or bool(stack[-1][0] and RENOVATE_PROSE.fullmatch(stack[-1][0])))

    pos = 0
    while pos < len(text):
        token = JSON5_TOKEN.match(text, pos)
        line = bisect.bisect_right(starts, pos)
        if not token:
            yield from list(enumerate(text.splitlines(), 1))[line - 1 :]
            return
        pos, value = token.end(), token[0]
        if token["comment"]:
            for offset, part in enumerate(value.splitlines()):
                yield line + offset, part.lstrip("/*").rstrip("*/")
        elif value == "{":
            stack.append([None, True, bool(stack) and stack[-1][0] in RENOVATE_PROSE_MAPS])
        elif value == "[":
            stack.append([stack[-1][0] if stack else None, None, holds_prose()])
        elif value in ("}", "]"):
            if stack:
                stack.pop()
        elif value == ",":
            if stack and stack[-1][1] is not None:
                stack[-1][1] = True
        elif token["string"] or token["bare"]:
            word = json5_unescape(value[1:-1] if token["string"] else value)
            if stack and stack[-1][1]:
                stack[-1][0], stack[-1][1] = word, False
            elif token["string"] and holds_prose():
                yield line, " ".join(word.splitlines())


def prose(path: str, text: str) -> Iterator[tuple[int, str]]:
    """(line number, prose on that line) for every line of `text` that holds prose."""
    match kind(path, text.partition("\n")[0]):
        case "markdown":
            yield from enumerate(text.splitlines(), 1)
        case "python":
            yield from python_prose(text)
        case "hash":
            yield from hash_comments(text)
        case "renovate":
            yield from renovate_prose(text)


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
