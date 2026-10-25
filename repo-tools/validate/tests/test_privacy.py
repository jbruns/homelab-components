"""The privacy check: denylisted names in prose, from a denylist held outside the repo."""

import os
import subprocess
import sys

import pytest

from conftest import commit, write

NAME = "Quuxwell"
DENY = r"[Qq]uuxwell"

FILES = {
    "README.md": f"# Docs\n\nWritten by {NAME} for the team.\n",
    "tools/run.py": f'''
        """Run the thing for {NAME}."""

        quuxwell_path = "data/x"  # set up by {NAME}
        print(f"done, {NAME}")
    ''',
    "tools/hook": f"#!/bin/sh\n# ask {NAME}\nquuxwell --go\n",
    ".github/workflows/ci.yaml": f"# owned by {NAME}\njobs:\n  a:\n    uses: quuxwell/ci/x.yaml@abc\n",
    "LICENSE": f"Copyright {NAME}\n",
    "uv.lock": f'source = "{NAME}"\n',
    "renovate.json": f'{{"extends": ["github>{NAME}/presets"]}}\n',
    "notes.json": f'{{"by": "{NAME}"}}\n',
    "docs/use.md": "```yaml\nuses: quuxwell/ci/.github/workflows/x.yaml@abc\n```\n",
}


@pytest.fixture
def private(repo):
    write(repo, FILES)
    commit(repo)
    return repo


def test_reports_prose_by_file_and_line_with_the_match_redacted(validate, private, monkeypatch, capsys):
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", DENY)
    validate.check_privacy()
    out = capsys.readouterr().out
    assert NAME not in out
    assert "WARN: privacy: denylisted text at README.md:3: Written by [redacted] for the team." in out
    locations = [line.split(" at ", 1)[1].split(": ", 1)[0] for line in out.splitlines() if " at " in line]
    assert locations == [
        ".github/workflows/ci.yaml:1",
        "README.md:3",
        "tools/hook:2",
        "tools/run.py:1",
        "tools/run.py:3",
        "tools/run.py:4",
    ]
    assert validate.failures == []


def test_load_bearing_identifiers_and_files_are_exempt(validate, private, monkeypatch, capsys):
    """Code (identifiers, YAML values, commands), uses: refs, LICENSE, lockfiles, Renovate config and file types
    that hold no prose are never scanned."""
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", DENY)
    validate.check_privacy()
    out = capsys.readouterr().out
    for exempt in ("LICENSE", "uv.lock", "renovate.json", "notes.json", "docs/use.md", "ci.yaml:4", "hook:3"):
        assert f" {exempt}" not in out and f"/{exempt}" not in out


def test_an_allowlist_pattern_exempts_the_text_it_covers(validate, repo, monkeypatch, capsys):
    write(repo, {"README.md": f"Install from git+https://example.invalid/{NAME}/tools.\n"})
    commit(repo)
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", DENY)
    monkeypatch.setattr(validate, "PRIVACY_ALLOW", [validate.re.compile(r"git\+https://\S+")])
    validate.check_privacy()
    out = capsys.readouterr().out
    assert "README.md" not in out and "privacy ok" in out


def test_fail_mode_fails_with_a_line_annotation(validate, private, monkeypatch, capsys):
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", DENY)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(validate, "PRIVACY_MODE", "fail")
    validate.check_privacy()
    out = capsys.readouterr().out
    assert len(validate.failures) == 6
    assert "::error file=README.md,line=3::privacy: denylisted text at README.md:3: " in out


def test_a_blank_denylist_skips(validate, private, monkeypatch, capsys):
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", "\n  \n")
    validate.check_privacy()
    assert validate.failures == []
    assert capsys.readouterr().out == "privacy: HOMELAB_PRIVACY_DENYLIST is not set; skipped\n"


def test_an_invalid_pattern_fails_without_echoing_it(validate, monkeypatch, capsys):
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", f"{DENY}\n(?P=Secretname)")
    validate.check_privacy()
    assert validate.failures == ["privacy: HOMELAB_PRIVACY_DENYLIST: pattern on line 2 is not a valid regex"]
    assert "Secretname" not in capsys.readouterr().out


def test_an_allowlisted_match_is_still_redacted_when_its_line_is_reported(validate, repo, monkeypatch, capsys):
    write(repo, {"README.md": f"See example.invalid/{NAME}/tools, or ask {NAME}.\n"})
    commit(repo)
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", DENY)
    monkeypatch.setattr(validate, "PRIVACY_ALLOW", [validate.re.compile(r"example\.invalid/\S+")])
    validate.check_privacy()
    out = capsys.readouterr().out
    assert NAME not in out
    assert "README.md:1: See example.invalid/[redacted]/tools, or ask [redacted]." in out


def test_a_python_tokenize_error_keeps_earlier_prose_and_scans_the_rest_as_text(validate, repo, monkeypatch, capsys):
    write(repo, {"tools/broken.py": f'"""For {NAME}."""\nx = 1\ny = """{NAME} never closed\nz = {NAME}\n'})
    commit(repo)
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", DENY)
    monkeypatch.setattr(validate, "PRIVACY_MODE", "fail")
    validate.check_privacy()
    out = capsys.readouterr().out
    assert NAME not in out
    assert [f.split(": ")[1].removeprefix("denylisted text at ") for f in validate.failures] == [
        "tools/broken.py:1",
        "tools/broken.py:3",
        "tools/broken.py:4",
    ]


def test_more_than_one_pattern_and_overlapping_matches(validate, repo, monkeypatch, capsys):
    write(repo, {"README.md": f"{NAME}s and Corvina\n"})
    commit(repo)
    monkeypatch.setenv("HOMELAB_PRIVACY_DENYLIST", f"{DENY}\n{NAME}s\nCorvina\n")
    validate.check_privacy()
    assert "README.md:1: [redacted] and [redacted]" in capsys.readouterr().out


def privacy_cli(repo, *args: str, denylist: str | None = DENY) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_ACTIONS", "HOMELAB_PRIVACY_DENYLIST")}
    if denylist is not None:
        env["HOMELAB_PRIVACY_DENYLIST"] = denylist
    return subprocess.run(
        [sys.executable, "-m", "homelab_validate", "--root", str(repo), *args, "privacy"],
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_warns_by_default_and_fails_in_fail_mode(private):
    warned = privacy_cli(private)
    assert warned.returncode == 0, warned.stdout + warned.stderr
    assert "WARN: privacy:" in warned.stdout and "warn-only" in warned.stdout
    failed = privacy_cli(private, "--privacy-mode", "fail")
    assert failed.returncode == 1
    assert "ERROR: privacy:" in failed.stdout and NAME not in failed.stdout


def test_cli_skips_without_a_denylist_even_in_fail_mode(private):
    r = privacy_cli(private, "--privacy-mode", "fail", denylist=None)
    assert r.returncode == 0 and "HOMELAB_PRIVACY_DENYLIST is not set; skipped" in r.stdout


def test_cli_privacy_allow(private):
    r = privacy_cli(private, "--privacy-mode", "fail", "--privacy-allow", ".*")
    assert r.returncode == 0, r.stdout
    assert "privacy ok" in r.stdout


def test_cli_rejects_a_bad_allowlist_pattern(private):
    r = privacy_cli(private, "--privacy-allow", "(")
    assert r.returncode == 2 and "--privacy-allow takes a regex" in r.stderr
