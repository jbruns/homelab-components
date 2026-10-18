"""The README rule every Component must meet (ADR 0008).

A Component README needs a non-empty `## Why` (the gap no upstream or community option covers), a non-empty
`## Retire when` (its Retirement Condition), and a `## Configuration` table listing exactly the environment
variables the code under `src/` reads, each with a default (or `required`). Python reads are found through
`os.environ` / `os.getenv` with literal names; shell reads are `${NAME:-default}`-style expansions.
"""

import ast
import re
from pathlib import Path

REQUIRED_SECTIONS = ("Why", "Retire when")
_HEADING = re.compile(r"^(#{1,2})\s+(.+?)\s*$")
# In shell, an env var is a parameter expansion with a default or a required check: ${NAME:-x}, ${NAME:?msg}.
_SHELL_ENV_READ = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?[-=?]")
_TABLE_SEPARATOR = re.compile(r"^\|?[\s:|-]+\|?$")


def _is_environ(node):
    return (isinstance(node, ast.Name) and node.id.endswith("environ")) or (
        isinstance(node, ast.Attribute) and node.attr == "environ"
    )


def _env_name_node(node):
    """The node naming the env var when `node` reads the environment, else None."""
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("get", "pop", "setdefault") and _is_environ(func.value):
            return node.args[0]
        if (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
            isinstance(func, ast.Name) and func.id == "getenv"
        ):
            return node.args[0]
    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        return node.slice
    if isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
        return node.left
    return None


def _python_env_reads(path, reads, problems):
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        name = _env_name_node(node)
        if name is None:
            continue
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            reads.setdefault(name.value, f"{path}:{node.lineno}")
        else:
            problems.append(f"{path}:{node.lineno}: env var name must be a string literal so the README can be checked")


def env_reads(src_dir):
    """Env vars the code under `src_dir` reads, mapped to where; plus reads whose name cannot be checked."""
    reads, problems = {}, []
    for path in sorted(Path(src_dir).rglob("*.py")):
        _python_env_reads(path, reads, problems)
    for path in sorted(Path(src_dir).rglob("*.sh")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for name in _SHELL_ENV_READ.findall(line):
                reads.setdefault(name, f"{path}:{lineno}")
    return reads, problems


def documented_env(configuration):
    """Env vars in the `## Configuration` table, mapped to their Default cell."""
    rows = [line.strip() for line in configuration.splitlines() if line.strip().startswith("|")]
    documented = {}
    for row in rows[1:]:
        if _TABLE_SEPARATOR.match(row):
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        documented[cells[0].strip("`")] = cells[1] if len(cells) > 1 else ""
    return documented


def sections(markdown):
    """Body text of each `##` section, keyed by heading."""
    found, current = {}, None
    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = heading[2] if heading[1] == "##" else None
            if current is not None:
                found[current] = ""
        elif current is not None:
            found[current] += line + "\n"
    return found


def check_readme(component_dir):
    """Problems with the Component's README; empty when it meets the rule."""
    component_dir = Path(component_dir)
    readme = component_dir / "README.md"
    if not readme.is_file():
        return [f"{readme}: missing"]
    body = sections(readme.read_text())
    problems = []
    for name in REQUIRED_SECTIONS:
        if not body.get(name, "").strip():
            problems.append(f"{readme}: needs a non-empty '## {name}' section")

    reads, unreadable = env_reads(component_dir / "src")
    problems += unreadable
    documented = documented_env(body.get("Configuration", ""))
    for name, where in sorted(reads.items()):
        if name not in documented:
            problems.append(f"{readme}: {name} (read at {where}) is not documented in the '## Configuration' table")
    for name, default in sorted(documented.items()):
        if not default:
            problems.append(f"{readme}: {name} needs a default in the Default column (a value, or `required`)")
    for name in sorted(documented.keys() - reads.keys()):
        problems.append(f"{readme}: {name} is documented but not read by the code under src/")
    return problems
