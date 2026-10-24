# homelab-agent-skills

Shared Repo Tooling: `setup-agent-skills` installs the agent skills a consuming repo pins in its committed
`.agent-skills.lock`, and checks offline that the installed skills still match it. Every repo-specific value (the
skills source, its pin, the skill set and where the skills install) comes from that file, so each repo pins its own
set. It is local development tooling: it never runs on a Host, needs no Host access, Docker or secrets, and deploys
nothing. Background: [ADR 0012 in
jbruns/home-ops](https://github.com/jbruns/home-ops/blob/main/docs/adr/0012-shared-repo-tooling.md).

Needs Git 2.25+ (sparse checkout) and Python 3.11+ (uv provides one). It has no Python dependencies.

## Install in a consuming repo

Commit this thin wrapper as `tools/agent-skills/setup-agent-skills` (mode 755). It pins the tool by commit, which
uv caches, so `check` keeps working offline after the first run; the tag beside it is for people and Renovate.

```bash
#!/usr/bin/env bash
# setup-agent-skills from jbruns/homelab-components (shared Repo Tooling), pinned by commit.
# Usage and the .agent-skills.lock format: https://github.com/jbruns/homelab-components/tree/main/repo-tools/agent-skills
set -euo pipefail
tag=agent-skills-v1.0.0
commit=0000000000000000000000000000000000000000
export AGENT_SKILLS_COMMAND="$0"
cd "$(dirname "${BASH_SOURCE[0]}")"
exec uvx --quiet --from "git+https://github.com/jbruns/homelab-components@${commit}#subdirectory=repo-tools/agent-skills" \
  setup-agent-skills "$@"
```

Set `commit` to the commit the tag names (`git ls-remote https://github.com/jbruns/homelab-components
refs/tags/agent-skills-v1.0.0`). Then:

- commit a `.agent-skills.lock` (below) at the repo root;
- gitignore the install targets (`.agents/skills/` by default);
- tell agents in `AGENTS.md` to run `tools/agent-skills/setup-agent-skills check` before repository work, and
  `tools/agent-skills/setup-agent-skills` when it fails, then start a fresh session to discover the skills.

The wrapper needs [uv](https://docs.astral.sh/uv/). It changes into its own directory first, so it always manages
the repo it lives in, wherever it is run from, and names itself in recovery hints ("run
tools/agent-skills/setup-agent-skills").

Renovate keeps `tag` and `commit` current with a custom manager (the shared Renovate preset should carry it):

```json
{
  "customManagers": [
    {
      "customType": "regex",
      "managerFilePatterns": ["/(^|/)tools/agent-skills/setup-agent-skills$/"],
      "matchStrings": ["tag=(?<currentValue>agent-skills-v\\S+)\\s+commit=(?<currentDigest>[0-9a-f]{40})"],
      "datasourceTemplate": "github-tags",
      "depNameTemplate": "jbruns/homelab-components",
      "versioningTemplate": "regex:^agent-skills-v(?<major>\\d+)\\.(?<minor>\\d+)\\.(?<patch>\\d+)$"
    }
  ]
}
```

Without the wrapper, install the command itself, pinned the same way, and run `setup-agent-skills` anywhere in
the repo:

```sh
uv tool install "git+https://github.com/jbruns/homelab-components@agent-skills-v1.0.0#subdirectory=repo-tools/agent-skills"
```

## Run

```sh
setup-agent-skills          # install or repair (the default command, also `setup`)
setup-agent-skills check    # offline: exits 1 unless the installed skills match the lock
setup-agent-skills pin REF  # move the pin to an upstream tag or commit, then install it
setup-agent-skills clean    # remove only the skills this tool installed
```

It finds the repo root with `git rev-parse --show-toplevel` from the working directory, and reads
`<root>/.agent-skills.lock`, or the absolute path in `AGENT_SKILLS_LOCK`, which still installs into this repo.
Errors print `error: ...` and exit 1; bad usage exits 2. Recovery hints say "run setup-agent-skills", or the
command in `AGENT_SKILLS_COMMAND`. Start a fresh agent session after installing or changing
skills so the agent discovers them.

## .agent-skills.lock

```text
# Where the skills come from; keep revision on the commit ref resolves to.
source https://github.com/mattpocock/skills.git
ref v1.3.1
revision 24fe0ef7737efae15c87225755e9f6f5965e4888

# Optional: adjust the published set.
exclude wizard
exclude engineering/tdd
include misc/setup-pre-commit

# Optional: where the skills install, relative to the repo root; repeat for several. Default .agents/skills.
target .agents/skills
target .claude/skills
```

One `key value` pair per line; `#` starts a comment line. Unknown keys, duplicates and extra fields fail.

| Key | Required | Meaning |
|---|---|---|
| `source` | yes | Git URL (or path) of the skills repository |
| `ref` | yes | Its tag, branch or commit to install |
| `revision` | yes | The full lowercase commit SHA `ref` must resolve to; setup refuses a ref that has moved |
| `exclude` | no | Skip a published skill: a bare name matches it in any category, a path matches exactly |
| `include` | no | Add an unpublished skill, as a path under the source's `skills/` |
| `target` | no | An install directory inside the repo (no `..`, `.` or `.git` segments in any case, no symlink on its path, not nested in another target); default `.agents/skills` |

The installed set is the source's published `.claude-plugin/plugin.json` `skills` array, minus `exclude`, plus
`include`. Manifest paths must be under `skills/`; each skill installs by its final segment (`engineering/tdd`
becomes `<target>/tdd`) with every supporting file in its directory. A source without that manifest needs
`include` entries. An unmatched exclusion warns, a missing inclusion fails, an entry both included and excluded
fails, and two skills that would install under one name fail.

## Change the pin or the set

```sh
setup-agent-skills pin <upstream-tag-or-commit>
setup-agent-skills check
git diff -- .agent-skills.lock
```

Review the upstream changes before adopting a release, then commit the lock change. `pin` resolves the ref,
installs it, and rewrites only the lock's `ref` and `revision` lines, after the install succeeds; a failed pin
leaves the lock and the installed skills alone. Skills added to or removed from the upstream manifest follow the
new pin. To customise the set or the targets, edit the lock and rerun setup. To change providers, edit `source`,
then `pin` a ref from that provider. To drop a target, run `clean` before removing it from the lock, because
`clean` and setup only touch the targets the lock names.

## Check and recover

Each target holds a state file, `.agent-skills-managed`, recording the lock settings it was installed from and
the git tree hash of every managed skill, supporting files included. `check` compares the lock with that state and
rehashes every skill in every target, without network access; any difference exits 1 naming the target. Rerun
setup to repair missing or changed files.

Setup replaces local edits to managed skills and removes previously managed skills no longer selected. It never
touches a directory it did not install: an unmanaged directory, file or symlink with a selected skill's name in any
target fails setup before anything changes. Keep custom skills under other names. Every command refuses a
target whose path passes through a symlink before it reads or changes anything, so a target cannot reach
outside the repo or into `.git`; state and lock files are written to a fresh temporary file and renamed into
place. Setup records the skills it is
about to copy before copying them, so an interrupted install stays managed: `check` fails, and setup or `clean`
finishes the job. `clean` removes only the skills listed in each target's state; with a missing or invalid lock it
cleans `.agents/skills`. If a state file is lost, move the conflicting skill directories aside before reinstalling.

The state format is unchanged from jbruns/home-ops' original `tools/agent-skills`, so an install made by either
passes the other's `check`.

## Develop

```sh
cd repo-tools/agent-skills
python -m pip install -e . pytest ruff  # or install -e . into the repo's .venv
python -m pytest && ruff check . && ruff format --check .
```

The tests build throwaway local Git repositories for the skills source and the consuming repos; they need no
network access. A renamed command, lock key or state format is a major version bump.
