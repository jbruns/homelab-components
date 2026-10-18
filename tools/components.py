"""Repo Tooling CLI used by the workflows: which Components to test, README checks, Canary plans, releases.

python -m tools.components affected [--base REF]   JSON list of Components a change touches
python -m tools.components check-readme NAME...     README rule (tools/readme_check.py)
python -m tools.components canary-plan              JSON list of Canaries to run (needs skopeo)
python -m tools.components release TAG             component=/version= lines for a release tag
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
_RELEASE_TAG = re.compile(r"^(?P<component>[a-z0-9][a-z0-9-]*)-v(?P<version>\d+\.\d+\.\d+)$")


def all_components(components_dir=COMPONENTS_DIR):
    """Every Component folder, whether or not it is complete; the checks report what is missing."""
    return sorted(p.name for p in components_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))


def affected_components(changed_paths, components):
    """Components whose tests a change to `changed_paths` can affect, in `components` order."""
    if any(path.startswith(SHARED_PATHS) for path in changed_paths):
        return list(components)
    touched = {path.split("/")[1] for path in changed_paths if path.startswith("components/") and path.count("/") >= 2}
    return [c for c in components if c in touched]


def parse_release_tag(tag):
    """(component, version) for a `<component>-vX.Y.Z` tag of a releasable Component."""
    m = _RELEASE_TAG.match(tag)
    if not m:
        raise ValueError(f"{tag!r} is not a release tag (<component>-vX.Y.Z; components starting with _ never release)")
    return m["component"], m["version"]


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
    elif args.command == "check-readme":
        problems = [p for c in args.components for p in check_readme(COMPONENTS_DIR / c)]
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    elif args.command == "canary-plan":
        print(json.dumps(canary_plan(all_components())))
    elif args.command == "release":
        component, version = parse_release_tag(args.tag)
        if component not in all_components():
            raise ValueError(f"{args.tag!r} names no Component under components/")
        image = (COMPONENTS_DIR / component / "Dockerfile").is_file()
        print(f"component={component}\nversion={version}\nimage={str(image).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
