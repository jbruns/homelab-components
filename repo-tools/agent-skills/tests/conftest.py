import json
import subprocess

import pytest

from homelab_agent_skills.__main__ import main


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Source:
    """A throwaway upstream skills repository: skills/<path>/SKILL.md plus an optional published manifest."""

    def __init__(self, path):
        self.path = path
        path.mkdir()
        git("init", "--quiet", cwd=path)

    def add_skill(self, skill, files=None):
        directory = self.path / "skills" / skill
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(f"---\nname: {skill.rsplit('/', 1)[-1]}\ndescription: Test skill\n---\n")
        for name, text in (files or {}).items():
            (directory / name).write_text(text)

    def git(self, *args):
        return git(*args, cwd=self.path)

    def publish(self, *skills):
        """Writes the manifest listing `skills`, commits, and returns the commit SHA."""
        return self.write_manifest(json.dumps({"name": "test", "skills": [f"./skills/{s}" for s in skills]}))

    def write_manifest(self, text):
        manifest = self.path / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text(text + "\n")
        return self.commit("publish")

    def commit(self, message):
        git("add", "-A", cwd=self.path)
        git("commit", "--quiet", "--allow-empty", "-m", message, cwd=self.path)
        return git("rev-parse", "HEAD", cwd=self.path)


class Project:
    """A consuming repo; the working directory during a test."""

    def __init__(self, path, source):
        self.path = path
        self.source = source
        path.mkdir()
        git("init", "--quiet", cwd=path)
        self.lock = path / ".agent-skills.lock"

    def write_lock(self, revision, *entries, ref=None):
        lines = [f"source {self.source.path}", f"ref {ref or revision}", f"revision {revision}", *entries]
        self.lock.write_text("\n".join(lines) + "\n")

    def installed(self, target=".agents/skills"):
        return sorted(p.name for p in (self.path / target).iterdir() if not p.name.startswith("."))


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """Keeps the developer's git config (signing, hooks, templates) out of the throwaway repositories."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Agent Skills Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "agent-skills-test@example.invalid")
    monkeypatch.delenv("AGENT_SKILLS_LOCK", raising=False)


@pytest.fixture
def make_source(tmp_path):
    return lambda name: Source(tmp_path / name)


@pytest.fixture
def make_project(tmp_path):
    return lambda name, source: Project(tmp_path / name, source)


@pytest.fixture
def source(make_source):
    return make_source("source")


@pytest.fixture
def project(tmp_path, source, monkeypatch):
    project = Project(tmp_path / "project", source)
    monkeypatch.chdir(project.path)
    return project


@pytest.fixture
def run(capsys):
    """Runs setup-agent-skills in the working directory; returns (exit code, stdout, stderr)."""

    def run(*args):
        code = main(list(args))
        out, err = capsys.readouterr()
        return code, out, err

    return run
