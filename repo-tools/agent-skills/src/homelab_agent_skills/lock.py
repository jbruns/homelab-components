"""The consuming repo's `.agent-skills.lock`: where the skills come from, which ones, and where they install."""

import re
from dataclasses import dataclass
from pathlib import Path

from . import AgentSkillsError
from .files import replace_file

LOCK_FILE = ".agent-skills.lock"
DEFAULT_TARGET = ".agents/skills"

# A skill path names a directory under the source's skills/ tree ("tdd", "engineering/tdd").
_SKILL_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_TARGET_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_REVISION = re.compile(r"[0-9a-f]{40}")


def skill_name(skill):
    """The directory a skill installs as: its final path segment (`engineering/tdd` -> `tdd`)."""
    return skill.rsplit("/", 1)[-1]


def exclusion_matches(entry, skill):
    """A path exclusion matches exactly; a bare name matches that skill in any category."""
    return entry == (skill if "/" in entry else skill_name(skill))


def validate_skill_path(skill):
    if not _SKILL_PATH.fullmatch(skill):
        raise AgentSkillsError(f"invalid skill path: {skill}")
    return skill


def _validate_target(target, where):
    segments = target.split("/")
    if not all(_TARGET_SEGMENT.fullmatch(s) and s not in (".", "..", ".git") for s in segments):
        raise AgentSkillsError(f"target must be a relative path inside the repo without '.', '..' or '.git': {where}")
    return target


@dataclass(frozen=True)
class Lock:
    path: Path
    source: str
    ref: str
    revision: str
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    targets: tuple[str, ...] = (DEFAULT_TARGET,)

    def is_excluded(self, skill):
        return any(exclusion_matches(entry, skill) for entry in self.excludes)

    def state(self):
        """The settings an installation was resolved from, as recorded in each target's state file.

        Targets are not recorded: each target holds its own state, so its location is implied.
        """
        lines = [f"source {self.source}", f"ref {self.ref}", f"revision {self.revision}"]
        lines += sorted(f"include {entry}" for entry in self.includes)
        lines += sorted(f"exclude {entry}" for entry in self.excludes)
        return lines


def load_lock(path):
    if not path.is_file():
        raise AgentSkillsError(f"missing lock file: {path}")

    singles = {"source": None, "ref": None, "revision": None}
    lists = {"include": [], "exclude": [], "target": []}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) != 2:
            raise AgentSkillsError(f"invalid entry at {path}:{number}")
        key, value = fields
        if key in singles:
            if singles[key] is not None:
                raise AgentSkillsError(f"duplicate {key} in {path}")
            if key == "revision" and not _REVISION.fullmatch(value):
                raise AgentSkillsError("revision must be a full lowercase commit SHA")
            singles[key] = value
        elif key in lists:
            if key == "target":
                _validate_target(value, f"{path}:{number}")
            else:
                validate_skill_path(value)
            if value in lists[key]:
                raise AgentSkillsError(f"duplicate {key} '{value}' in {path}")
            lists[key].append(value)
        elif key == "skill":
            raise AgentSkillsError(
                f"'skill' entries were replaced by the source manifest; use include/exclude ({path}:{number})"
            )
        else:
            raise AgentSkillsError(f"unknown key '{key}' at {path}:{number}")

    for key, value in singles.items():
        if value is None:
            raise AgentSkillsError(f"lock file has no {key}")

    targets = lists["target"] or [DEFAULT_TARGET]
    for target in targets:
        for other in targets:
            if other != target and other.startswith(target + "/"):
                raise AgentSkillsError(f"target '{other}' is inside target '{target}' in {path}")

    lock = Lock(
        path, **singles, includes=tuple(lists["include"]), excludes=tuple(lists["exclude"]), targets=tuple(targets)
    )
    for entry in lock.includes:
        if lock.is_excluded(entry):
            raise AgentSkillsError(f"'{entry}' is both included and excluded in {path}")
    return lock


def write_pin(lock, ref, revision):
    """Rewrites only the lock's `ref` and `revision` lines, keeping comments and every other entry."""
    replaced = {"ref": f"ref {ref}", "revision": f"revision {revision}"}
    lines = []
    for line in lock.path.read_text().splitlines(keepends=True):
        fields = line.split()
        if fields and fields[0] in replaced:
            line = replaced[fields[0]] + "\n"
        lines.append(line)
    replace_file(lock.path, "".join(lines), mode=lock.path.stat().st_mode & 0o777)
