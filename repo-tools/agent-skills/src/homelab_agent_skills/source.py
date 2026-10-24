"""The upstream skills repository at the pinned ref, fetched shallow and sparse into a scratch directory."""

import json
import subprocess

from . import AgentSkillsError
from .lock import exclusion_matches, skill_name, validate_skill_path

MANIFEST_PATH = ".claude-plugin/plugin.json"


def git(*args, cwd=None, env=None):
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    except FileNotFoundError:
        raise AgentSkillsError("git is required") from None
    if result.returncode != 0:
        raise AgentSkillsError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class Checkout:
    """A sparse checkout of `ref` from `url` in `path`, which must not exist yet. Nothing is checked out until
    `checkout()` is called with the skill paths to materialise."""

    def __init__(self, path, url, ref):
        self.path = path
        self.ref = ref
        git("init", "--quiet", str(path))
        git("remote", "add", "origin", url, cwd=path)
        git("sparse-checkout", "init", "--cone", cwd=path)
        git("fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", ref, cwd=path)
        self.revision = git("rev-parse", "FETCH_HEAD^{commit}", cwd=path)

    def manifest_skills(self):
        """The skill paths the source publishes in its manifest's `skills` array (`./skills/engineering/tdd` ->
        `engineering/tdd`). A source without a manifest publishes nothing."""
        try:
            git("cat-file", "-e", f"FETCH_HEAD:{MANIFEST_PATH}", cwd=self.path)
        except AgentSkillsError:
            return []
        return parse_manifest(git("show", f"FETCH_HEAD:{MANIFEST_PATH}", cwd=self.path))

    def checkout(self, skills):
        git("sparse-checkout", "set", "--skip-checks", *(f"skills/{skill}" for skill in skills), cwd=self.path)
        git("checkout", "--quiet", "--detach", self.revision, cwd=self.path)

    def skill_dir(self, skill):
        return self.path / "skills" / skill


def parse_manifest(text):
    try:
        manifest = json.loads(text)
    except ValueError as error:
        raise AgentSkillsError(f"invalid source manifest: {error}") from None
    if not isinstance(manifest, dict):
        raise AgentSkillsError("source manifest must be a JSON object")
    skills = manifest.get("skills", [])
    if not isinstance(skills, list):
        raise AgentSkillsError("source manifest skills must be an array")
    paths = []
    for path in skills:
        if not isinstance(path, str):
            raise AgentSkillsError("source manifest skill paths must be strings")
        path = path.removeprefix("./")
        if not path.startswith("skills/"):
            raise AgentSkillsError(f"source manifest skill path must be under skills/: {path}")
        path = path.removeprefix("skills/").rstrip("/")
        try:
            paths.append(validate_skill_path(path))
        except AgentSkillsError:
            raise AgentSkillsError(f"invalid source manifest skill path: {path!r}") from None
    return paths


def resolve_skills(lock, published):
    """The installed set: the published skills minus excludes, plus includes, in that order."""
    skills = []
    for skill in [*published, *lock.includes]:
        if lock.is_excluded(skill) or skill in skills:
            continue
        for other in skills:
            if skill_name(other) == skill_name(skill):
                raise AgentSkillsError(f"'{other}' and '{skill}' would both install as '{skill_name(skill)}'")
        skills.append(skill)
    return skills


def unmatched_excludes(lock, published):
    """Excludes that match no published skill, e.g. because upstream removed it."""
    return [entry for entry in lock.excludes if not any(exclusion_matches(entry, skill) for skill in published)]
