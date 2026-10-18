import textwrap

from tools.readme_check import check_readme

GOOD_README = """\
# demo

Exports demo metrics.

## Why

No upstream exporter reports this.

## Retire when

Upstream ships its own /metrics endpoint.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DEMO_URL` | `http://demo:8080` | Where the demo API lives. |
| `DEMO_API_KEY` | required | API key for the demo API. |
"""

GOOD_SOURCE = """\
import os

URL = os.environ.get("DEMO_URL", "http://demo:8080")


def key(environ):
    return environ["DEMO_API_KEY"]
"""


def component(tmp_path, readme=GOOD_README, sources=None):
    root = tmp_path / "demo"
    (root / "src" / "demo").mkdir(parents=True)
    (root / "README.md").write_text(textwrap.dedent(readme))
    for name, text in (sources or {"demo/__main__.py": GOOD_SOURCE}).items():
        (root / "src" / name).write_text(text)
    return root


def test_a_readme_with_why_retire_when_and_every_env_var_passes(tmp_path):
    assert check_readme(component(tmp_path)) == []


def test_a_readme_without_why_or_retire_when_fails(tmp_path):
    readme = GOOD_README.replace("## Why", "## Background").replace("Upstream ships its own /metrics endpoint.", "")
    problems = check_readme(component(tmp_path, readme=readme))
    assert any("## Why" in p for p in problems)
    assert any("## Retire when" in p for p in problems)


def test_an_env_var_the_code_reads_but_the_readme_omits_fails(tmp_path):
    source = GOOD_SOURCE + 'TIMEOUT = float(os.getenv("DEMO_TIMEOUT", "10"))\n'
    problems = check_readme(component(tmp_path, sources={"demo/__main__.py": source}))
    assert len(problems) == 1
    assert "DEMO_TIMEOUT" in problems[0] and "not documented" in problems[0]


def test_a_documented_env_var_the_code_never_reads_fails(tmp_path):
    readme = GOOD_README + "| `DEMO_STALE` | `1` | Left over from an old version. |\n"
    problems = check_readme(component(tmp_path, readme=readme))
    assert len(problems) == 1
    assert "DEMO_STALE" in problems[0] and "not read" in problems[0]


def test_a_documented_env_var_without_a_default_fails(tmp_path):
    readme = GOOD_README.replace("| `DEMO_API_KEY` | required |", "| `DEMO_API_KEY` |  |")
    problems = check_readme(component(tmp_path, readme=readme))
    assert len(problems) == 1
    assert "DEMO_API_KEY" in problems[0] and "default" in problems[0]


def test_an_env_var_read_through_a_computed_name_fails(tmp_path):
    source = GOOD_SOURCE + 'PREFIX = "DEMO"\nPORT = os.environ.get(PREFIX + "_PORT", "9100")\n'
    problems = check_readme(component(tmp_path, sources={"demo/__main__.py": source}))
    assert len(problems) == 1
    assert "__main__.py:" in problems[0] and "string literal" in problems[0]


def test_a_shell_component_documents_its_parameter_expansion_defaults(tmp_path):
    readme = """\
    # demo

    ## Why

    Nothing orders container starts on boot.

    ## Retire when

    The container runtime orders starts itself.

    ## Configuration

    | Variable | Default | Description |
    |---|---|---|
    | `DEMO_DIR` | `/srv/demo` | Where the start order lives. |
    """
    script = '#!/bin/sh\nBASE=${DEMO_DIR:-/srv/demo}\nWAIT=${DEMO_WAIT:-60}\nfor f in "$BASE"/*; do echo "${f}"; done\n'
    problems = check_readme(component(tmp_path, readme=readme, sources={"demo.sh": script}))
    assert len(problems) == 1
    assert "DEMO_WAIT" in problems[0] and "demo.sh:3" in problems[0]
