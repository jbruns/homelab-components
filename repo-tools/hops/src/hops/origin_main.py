"""Declared state as origin/<default branch> has it: what doco-cd deploys, whatever this checkout holds.

The default branch is hosts.yaml's `default_branch` (main unless the repo says otherwise)."""

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

from hops.declared import Declared
from hops.errors import ConfigError, HopsError


def ref(branch: str) -> str:
    return f"origin/{branch}"


def refspec(branch: str) -> str:
    # Explicit, so a narrowed remote.origin.fetch cannot leave origin/<branch> stale.
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise HopsError(f"git {' '.join(args)} failed: {detail.decode(errors='replace').strip() or exc}") from None


def load(repo_root: Path, stack_root: str, branch: str) -> tuple[Declared, str]:
    """Fetch `branch` from origin and load the Stack root of that commit; returns it and the commit's SHA.

    The tree is unpacked under the repository's git directory, so it never mixes with the checkout.
    Refuses an inventory without Services: removal must never run against a partial declared state.
    """
    _git(repo_root, "fetch", "--quiet", "origin", refspec(branch))
    # Pinned now: everything below reads this SHA, whatever origin/<branch> points to later.
    commit = _git(repo_root, "rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}").decode().strip()
    common = Path(_git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    dest = common / "hops" / "origin-main" / commit
    for stale in dest.parent.glob("*"):  # earlier commits' trees, and a half-written one
        shutil.rmtree(stale, ignore_errors=True)
    dest.mkdir(parents=True)
    archive = _git(repo_root, "archive", "--format=tar", commit, "--", stack_root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # Python before 3.11.4; git archive of our own commit is trusted
            tar.extractall(dest)
    declared = Declared.load(dest, stack_root)
    if not declared.services:
        raise ConfigError(
            f"{ref(branch)} ({commit[:12]}) declares no Services under {stack_root}/; refusing to remove "
            "anything against it"
        )
    return declared, commit
