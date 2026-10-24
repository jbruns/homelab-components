"""Installs the pinned skills into each target and checks, offline, that they are unchanged.

Each target holds its own state file listing the lock settings it was installed from and a git tree hash of every
managed skill, so `check` needs no network and setup never touches a directory it did not install.
"""

import os
import re
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import AgentSkillsError
from .lock import skill_name, validate_skill_path, write_pin
from .source import Checkout, git, resolve_skills, unmatched_excludes

STATE_FILE = ".agent-skills-managed"
FIX = "run setup-agent-skills"
_TREE_HASH = re.compile(r"[0-9a-f]{40}")


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


class Target:
    """One directory the skills install into, relative to the repo root."""

    def __init__(self, root, relative):
        self.root = root
        self.relative = relative
        self.dir = root / relative
        self.state_file = self.dir / STATE_FILE

    def managed(self):
        """(skill path, tree hash) for each skill the state file records; empty when there is no state."""
        if not self.state_file.is_file():
            return []
        entries = []
        for line in self.state_file.read_text().splitlines():
            fields = line.split()
            if not fields or fields[0] != "skill":
                continue
            if len(fields) not in (2, 3):
                raise AgentSkillsError(f"invalid managed state: {self.state_file}")
            entries.append((validate_skill_path(fields[1]), fields[2] if len(fields) == 3 else None))
        return entries

    def refuse_unmanaged(self, skills):
        managed = {skill_name(skill) for skill, _ in self.managed()}
        for skill in skills:
            path = self.dir / skill_name(skill)
            if (path.exists() or path.is_symlink()) and skill_name(skill) not in managed:
                raise AgentSkillsError(f"{path} exists but is not managed by this helper")

    def install(self, checkout, skills, lock):
        for skill, _ in self.managed():
            _remove(self.dir / skill_name(skill))
        self.dir.mkdir(parents=True, exist_ok=True)
        for skill in skills:
            path = self.dir / skill_name(skill)
            _remove(path)
            shutil.copytree(checkout.skill_dir(skill), path, symlinks=True)
        lines = lock.state() + [f"skill {skill} {self.tree_hash(skill_name(skill))}" for skill in skills]
        partial = self.dir / f"{STATE_FILE}.new"
        partial.write_text("\n".join(lines) + "\n")
        partial.replace(self.state_file)

    def tree_hash(self, name):
        """The git tree hash of an installed skill and every supporting file, via a throwaway index."""
        with tempfile.TemporaryDirectory(prefix="agent-skills-index.") as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            git("read-tree", "--empty", cwd=self.root, env=env)
            git("add", "-f", "--", f"{self.relative}/{name}", cwd=self.root, env=env)
            return git("write-tree", cwd=self.root, env=env)

    def check(self, lock):
        if not self.state_file.is_file():
            raise AgentSkillsError(f"agent skills are not installed in {self.relative}; {FIX}")
        recorded = [line for line in self.state_file.read_text().splitlines() if line.split()[:1] != ["skill"]]
        if recorded != lock.state():
            raise AgentSkillsError(f"agent skills in {self.relative} are stale; {FIX}")
        managed = self.managed()
        for skill, expected in managed:
            name = skill_name(skill)
            if expected is None or not _TREE_HASH.fullmatch(expected):
                raise AgentSkillsError(f"invalid installed state for '{skill}' in {self.relative}; {FIX}")
            if not (self.dir / name / "SKILL.md").is_file():
                raise AgentSkillsError(f"missing installed skill '{name}' in {self.relative}; {FIX}")
            if self.tree_hash(name) != expected:
                raise AgentSkillsError(f"installed skill '{name}' in {self.relative} has local changes; {FIX}")
        if not managed:
            raise AgentSkillsError(f"no skills are installed in {self.relative}; {FIX}")

    def clean(self):
        """Removes the managed skills and the state; returns whether there was anything to remove."""
        if not self.state_file.is_file():
            return False
        for skill, _ in self.managed():
            _remove(self.dir / skill_name(skill))
        self.state_file.unlink()
        return True


def _targets(root, lock):
    return [Target(root, relative) for relative in lock.targets]


def _install(root, lock, checkout):
    if checkout.revision != lock.revision:
        raise AgentSkillsError(
            f"{lock.ref} resolved to {checkout.revision}, expected {lock.revision}; update the lock deliberately"
        )
    published = checkout.manifest_skills()
    skills = resolve_skills(lock, published)
    for entry in unmatched_excludes(lock, published):
        print(f"warning: exclude '{entry}' matches no skill in the source manifest at {lock.ref}", file=sys.stderr)
    if not skills:
        raise AgentSkillsError(
            f"no skills to install: {lock.ref} has no source manifest entries and the lock has no includes"
        )

    checkout.checkout(skills)
    for skill in skills:
        if not (checkout.skill_dir(skill) / "SKILL.md").is_file():
            raise AgentSkillsError(f"skill '{skill}' does not exist at {lock.ref}")
    targets = _targets(root, lock)
    for target in targets:
        target.refuse_unmanaged(skills)
    for target in targets:
        target.install(checkout, skills, lock)
    print(f"Installed {len(skills)} agent skills from {lock.ref} ({lock.revision}) into {', '.join(lock.targets)}.")


def setup(root, lock):
    with tempfile.TemporaryDirectory(prefix="agent-skills.") as scratch:
        _install(root, lock, Checkout(Path(scratch) / "source", lock.source, lock.ref))


def check(root, lock):
    for target in _targets(root, lock):
        target.check(lock)
    print(f"Agent skills match {lock.ref} ({lock.revision}).")


def pin(root, lock, ref):
    """Resolves `ref`, installs it, and only then moves the lock's pin to it."""
    if not ref or any(c.isspace() for c in ref):
        raise AgentSkillsError("ref must be non-empty and contain no whitespace")
    with tempfile.TemporaryDirectory(prefix="agent-skills-pin.") as scratch:
        checkout = Checkout(Path(scratch) / "source", lock.source, ref)
        _install(root, replace(lock, ref=ref, revision=checkout.revision), checkout)
    write_pin(lock, ref, checkout.revision)
    shown = lock.path.relative_to(root) if lock.path.is_relative_to(root) else lock.path
    print(f"Pinned {ref} at {checkout.revision} in {shown}.")


def clean(root, targets):
    removed = [Target(root, relative).clean() for relative in targets]
    print("Removed repository-managed agent skills." if any(removed) else "No managed agent skills are installed.")
