"""Repo Tooling CLI used by the workflows: which Components to test, README checks, Canary plans, releases.

python -m tools.components affected [--base REF]   JSON list of Components a change touches
python -m tools.components repo-tools               JSON list of shared Repo Tooling folders under repo-tools/
python -m tools.components check-readme NAME...     README rule (tools/readme_check.py)
python -m tools.components canary-plan              JSON list of Canaries to run (needs skopeo)
python -m tools.components release TAG             kind=/name=/version=/image= lines for a release tag
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from tools.readme_check import check_readme
from tools.upstreams import load_upstreams, newest_stable

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_DIR = ROOT / "components"
REPO_TOOLS_DIR = ROOT / "repo-tools"
# Changes here can change every Component's test result.
SHARED_PATHS = (
    "tools/",
    "conftest.py",
    "pyproject.toml",
    "requirements-dev.txt",
    ".github/workflows/ci.yaml",
    ".github/workflows/test-component.yaml",
    ".github/actions/",
)
# The reusable workflows (.github/workflows/repo-*.yaml) and Renovate presets (renovate/) release together.
REPO_CI = "repo-ci"
_RELEASE_TAG = re.compile(r"^(?P<name>[a-z0-9][a-z0-9-]*)-v(?P<version>\d+\.\d+\.\d+)$")


def all_components(components_dir=COMPONENTS_DIR):
    """Every Component folder, whether or not it is complete; the checks report what is missing."""
    return sorted(p.name for p in components_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))


def all_repo_tools(repo_tools_dir=REPO_TOOLS_DIR):
    """Every shared Repo Tooling folder under repo-tools/; CI tests each one on every change."""
    if not repo_tools_dir.is_dir():
        return []
    return sorted(p.name for p in repo_tools_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def affected_components(changed_paths, components):
    """Components whose tests a change to `changed_paths` can affect, in `components` order."""
    if any(path.startswith(SHARED_PATHS) for path in changed_paths):
        return list(components)
    touched = {path.split("/")[1] for path in changed_paths if path.startswith("components/") and path.count("/") >= 2}
    return [c for c in components if c in touched]


def parse_release_tag(tag):
    """(name, version) for a `<name>-vX.Y.Z` tag of a releasable Component or repo-tools/ folder."""
    m = _RELEASE_TAG.match(tag)
    if not m:
        raise ValueError(
            f"{tag!r} is not a release tag (<component, repo-tool or repo-ci>-vX.Y.Z; names starting with _ never release)"
        )
    return m["name"], m["version"]


def _check_unique_names(components, repo_tools):
    """A release tag names exactly one thing, so a repo-tools/ folder never shares a Component's name."""
    if clashes := sorted(set(components) & set(repo_tools)):
        raise ValueError(f"both a Component and a folder under repo-tools/: {', '.join(clashes)}; rename one")
    if REPO_CI in components or REPO_CI in repo_tools:
        raise ValueError(f"{REPO_CI!r} is reserved for the reusable workflows and Renovate presets; rename the folder")


def release_target(tag, components_dir=COMPONENTS_DIR, repo_tools_dir=REPO_TOOLS_DIR):
    """What a `<name>-vX.Y.Z` tag releases: a Component (an image when it has a Dockerfile, else its src/ files),
    a shared Repo Tooling folder, or (`repo-ci`) the reusable workflows and Renovate presets (a GitHub Release only)."""
    name, version = parse_release_tag(tag)
    components, repo_tools = all_components(components_dir), all_repo_tools(repo_tools_dir)
    _check_unique_names(components, repo_tools)
    if name in components:
        image = (components_dir / name / "Dockerfile").is_file()
        return {"kind": "component", "name": name, "version": version, "image": str(image).lower()}
    if name in repo_tools:
        return {"kind": "repo-tool", "name": name, "version": version, "image": "false"}
    if name == REPO_CI:
        return {"kind": "repo-ci", "name": name, "version": version, "image": "false"}
    raise ValueError(f"{tag!r} names no Component under components/, no folder under repo-tools/, and not {REPO_CI}")


def _registry_reference(repository):
    first = repository.split("/")[0]
    if "." in first or ":" in first or first == "localhost":
        return repository
    return f"docker.io/{repository if '/' in repository else 'library/' + repository}"


def list_tags(repository):
    out = subprocess.run(
        ["skopeo", "list-tags", f"docker://{_registry_reference(repository)}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)["Tags"]


def canary_plan(components):
    """One Canary per (Component, upstream) whose newest stable tag differs from the pinned one."""
    plan = []
    for component in components:
        component_dir = COMPONENTS_DIR / component
        if not (component_dir / "upstreams.yaml").is_file():
            continue
        for upstream in load_upstreams(component_dir):
            newest = newest_stable(upstream.tag, list_tags(upstream.repository))
            if newest and newest != upstream.tag:
                plan.append(
                    {
                        "component": component,
                        "upstream": upstream.name,
                        "version": newest,
                        "image": f"{upstream.repository}:{newest}",
                        "pinned": upstream.tag,
                    }
                )
            else:
                print(f"{component}/{upstream.name}: pinned {upstream.tag} is the newest stable tag", file=sys.stderr)
    return plan


def _changed_paths(base):
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"], check=True, capture_output=True, text=True, cwd=ROOT
    ).stdout
    return out.split()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.components")
    sub = parser.add_subparsers(dest="command", required=True)
    affected = sub.add_parser("affected")
    affected.add_argument("--base", help="git ref to diff against; every Component when omitted")
    sub.add_parser("repo-tools")
    readme = sub.add_parser("check-readme")
    readme.add_argument("components", nargs="+")
    sub.add_parser("canary-plan")
    release = sub.add_parser("release")
    release.add_argument("tag")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1


def _run(args):
    if args.command == "affected":
        components = all_components()
        print(json.dumps(affected_components(_changed_paths(args.base), components) if args.base else components))
    elif args.command == "repo-tools":
        repo_tools = all_repo_tools(REPO_TOOLS_DIR)
        _check_unique_names(all_components(COMPONENTS_DIR), repo_tools)
        print(json.dumps(repo_tools))
    elif args.command == "check-readme":
        problems = [p for c in args.components for p in check_readme(COMPONENTS_DIR / c)]
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    elif args.command == "canary-plan":
        print(json.dumps(canary_plan(all_components())))
    elif args.command == "release":
        print("\n".join(f"{key}={value}" for key, value in release_target(args.tag).items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
