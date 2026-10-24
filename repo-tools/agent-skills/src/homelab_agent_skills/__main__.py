"""setup-agent-skills: install, check, pin or clean the agent skills pinned in the repo's `.agent-skills.lock`."""

import os
import sys
from pathlib import Path

from . import AgentSkillsError, install
from .lock import DEFAULT_TARGET, LOCK_FILE, load_lock
from .source import git

USAGE = """\
Usage: setup-agent-skills [setup|check|clean]
       setup-agent-skills pin <ref>

  setup  Install the repository's pinned skills (default).
  check  Verify, offline, that the installed skills match .agent-skills.lock.
  clean  Remove only the skills managed by this helper.
  pin    Resolve a new source ref, install it, then update the lock.

Run inside the consuming repository; it finds the repo root with git.
Set AGENT_SKILLS_LOCK to an absolute path to use an alternate lock file.
"""

# command -> number of arguments after it
_ARITY = {"setup": 0, "check": 0, "clean": 0, "pin": 1}


def _repo_root():
    try:
        return Path(git("rev-parse", "--show-toplevel"))
    except AgentSkillsError as error:
        raise AgentSkillsError(f"run setup-agent-skills inside a git repository ({error})") from None


def _lock_path(root):
    return Path(os.environ.get("AGENT_SKILLS_LOCK") or root / LOCK_FILE)


def _clean_targets(lock_path):
    """The lock's targets; clean still works, from the default target, when the lock is missing or invalid."""
    try:
        return load_lock(lock_path).targets
    except AgentSkillsError:
        return (DEFAULT_TARGET,)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "setup"
    if command in ("-h", "--help", "help") and len(args) == 1:
        print(USAGE, end="")
        return 0
    if command not in _ARITY or len(args[1:]) != _ARITY[command]:
        print(USAGE, end="", file=sys.stderr)
        return 2

    try:
        root = _repo_root()
        lock_path = _lock_path(root)
        if command == "clean":
            install.clean(root, _clean_targets(lock_path))
            return 0
        lock = load_lock(lock_path)
        if command == "setup":
            install.setup(root, lock)
        elif command == "check":
            install.check(root, lock)
        else:
            install.pin(root, lock, args[1])
    except AgentSkillsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
