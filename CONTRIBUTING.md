# Contributing

For humans and agents. Vocabulary (Custom Component, Retirement Condition, Contract Test, Canary) follows
[jbruns/home-ops GLOSSARY.md](https://github.com/jbruns/home-ops/blob/main/GLOSSARY.md).

## Set up

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -m "not contract"     # unit tests, everything
.venv/bin/pytest -m contract           # Contract Tests; needs a running Docker
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/uv pip install --python .venv/bin/python -e repo-tools/<tool> && .venv/bin/pytest repo-tools/<tool>   # a shared tool's tests
```

Python 3.13 matches the distroless base image. Test dependencies live in `requirements-dev.txt`, pinned.

## Add a Component

1. **Justify it.** Confirm no upstream or community option covers the need. Write that as `## Why`, and the
   capability whose arrival would let you delete the Component as `## Retire when`.
2. **Copy the template.** `cp -r components/_template components/<name>`, where `<name>` is lowercase
   letters, digits and `-` (it becomes the image name and the release tag prefix). Rename
   `src/template_exporter` to a package name unique in this repo, and update the `Dockerfile` and tests.
3. **Declare the upstreams** the Component calls in `upstreams.yaml`, each pinned `repository:tag@sha256:digest`
   (`docker buildx imagetools inspect <repository>:<tag>` prints the digest). Pin a tag whose shape the Canary
   can follow: a plain release tag such as `v1.2.3` or `4.0.15.2941`, with any variant suffix (`-alpine`) you
   need, never `latest` or `rolling`. Fields are documented at the top of the template's file.
4. **Test first.** For each behaviour, write a failing test, then the code that passes it:
   - Unit tests in `tests/` exercise the Component through its public interface (its `/metrics` output, its
     exit status), using stand-ins for upstreams only where a real one is impractical.
   - Contract Tests are marked `@pytest.mark.contract` and take the `upstream` fixture:
     `sonarr = upstream("sonarr")` starts that upstream from its pin and returns `sonarr.url` and `sonarr.env`.
     Assert on the fields and endpoints the Component relies on, so an upstream change that breaks it fails here.
     The harness starts HTTP services; a Docker Engine API upstream is a pinned `docker:<version>-dind` with
     `privileged: true`. A CLI upstream (such as `restic`) declares no `port`; `upstream("restic").command(...)`
     is the argv that runs its pinned image once. Anything else the harness cannot start yet is added to
     `tools/contract.py`, test-first, in the PR that needs it.
5. **Configure through the environment.** Every site-specific value (URLs, hostnames, credentials, paths, ports)
   comes from an env var with a generic default, or is `required`. Read env vars with literal names
   (`environ.get("SONARR_URL", "http://sonarr:8989")`; in shell `${NAME:-default}`) and list each one in the
   README's `## Configuration` table (`Variable | Default | Description`). CI fails when the table and the code
   disagree.
6. **Ship a healthcheck.** The image runs `python3 -m <package> healthcheck`, which exits 0 only when the
   process answers on its own `/healthz` (which never calls an upstream, so a slow upstream cannot mark it
   unhealthy), and declares it as the Dockerfile `HEALTHCHECK`, so a Compose Service meets the home-ops
   Baseline without `wget` or a shell. If the image cannot start without some env (an API key, say),
   put dummy values in `tests/smoke.env`; CI starts the image with them and waits for healthy.
7. **Keep the image minimal.** Stdlib-only Python on `gcr.io/distroless/python3-debian13:nonroot`, pinned by
   digest, running as nonroot. A Component that needs a binary (e.g. `restic`) copies it in from a pinned image
   in a multi-stage build. A Component that runs directly on the Host (a POSIX shell script) has no
   `Dockerfile`; its release attaches the files directly under `src/` to the GitHub Release instead.
8. **Open a PR.** CI runs the README check, unit tests, Contract Tests and the image build for each Component
   the PR touches.

## Conventions

- **Docker Engine API**: put the API version in the request path (`/v1.43/containers/json`), so Contract Tests
  and Canaries test the API the Component actually uses.
- **Errors**: an exporter reports an upstream it cannot reach as a metric (`*_up 0`) and keeps serving.
- **Logs**: one line per event on stdout/stderr; never log credentials.
- **Repo Tooling** (`tools/`, `repo-tools/`) is documented and tested like a Component, without a Retirement
  Condition.

## Shared Repo Tooling

Repo Tooling that more than one homelab repo runs (jbruns/home-ops, asotbz/ops) lives here under `repo-tools/`,
so a fix lands once and every repo picks it up through a pin ([ADR 0012 in
jbruns/home-ops](https://github.com/jbruns/home-ops/blob/main/docs/adr/0012-shared-repo-tooling.md)).

- **Here or in the consuming repo?** A tool belongs here when a second repo needs it, or when it takes everything
  repo-specific from the repo it runs in (its `hosts.yaml`, its `.env`, its stack roots). Tooling that only makes
  sense for one repo stays in that repo's `tools/`. This repo's own `tools/` is internal tooling for Components,
  not shared.
- **Layout.** One tool per `repo-tools/<tool>/`, where `<tool>` is lowercase letters, digits and `-`, and never
  the name of a Component (it becomes the release tag prefix). A Python tool is its own package: `pyproject.toml`
  (Python >=3.11, few dependencies, its own ruff and pytest config), `src/<package>/`, `tests/`, and a `README.md`
  documenting install, usage and every contract a consuming repo relies on. It must install with
  `uv tool install "git+https://github.com/jbruns/homelab-components@<ref>#subdirectory=repo-tools/<tool>"`.
  A tool that uses another one here (hops uses `homelab-hosts`) lists it by its package name and points
  `[tool.uv.sources]` at its folder (`homelab-hosts = { path = "../hosts" }`), so it installs with uv, not pip,
  and gets the sibling from the same ref. Reusable workflows for consuming repos are `.github/workflows/repo-<name>.yaml` (`on: workflow_call`), and
  Renovate presets are `renovate/<preset>.json`.
- **hosts.yaml.** A consuming repo declares its Hosts in a committed `hosts.yaml` at its root
  ([schema](repo-tools/hosts/README.md)). `repo-tools/hosts` (`homelab_hosts`) is the only code that parses it:
  every tool that needs Hosts depends on that package, takes the Host from `--host`, the stack path, or the only
  Host (`Hosts.pick`) (a repo-wide check or generator may instead default to every Host, with `--host` to pick
  one), reads connection details from `.env` through `ssh_settings`/`truenas_settings` (only what the command
  uses), and gates Host-specific features on `Host.has(...)`. Never assume a Host, a stack root or a capability.
- **Test first**, as for a Component: CI installs every `repo-tools/<tool>` and runs ruff and its pytest suite on
  every PR (`.github/workflows/test-repo-tool.yaml`).
- **Release and pin.** Merge to `main`, then tag `<tool>-vX.Y.Z` on `main` and push it; the release workflow
  reruns the tool's checks and creates a GitHub Release (no image, no assets). Consuming repos pin that tag (or a
  commit) in their install command, dependency or `uses:` line, and Renovate bumps the pin. Semver from the
  consumer's view: a renamed flag, key or exported name, or a stricter `hosts.yaml`, is a major bump.
- **Reusable workflows and presets** take everything repo-specific from the calling repo (its `hosts.yaml`, its
  config files) or from inputs with generic defaults; never hardcode a Host, stack root or repo path. Pin every
  action by digest and every tool version as an input default annotated `# renovate:`. A workflow that runs a
  `repo-tools/<tool>` installs it from its own commit (`job.workflow_sha`), and `ci.yaml` runs it against
  `.github/repo-ci-fixture`. They release together as `repo-ci-vX.Y.Z` (see the
  [README](README.md#consuming-the-ci-workflows-and-renovate-preset)).

## Release

The version is the tag; nothing in the code changes. Tag `<component>-vX.Y.Z` (or `<tool>-vX.Y.Z` for a shared
tool) on `main` and push it (see [README](README.md#releasing)). Follow semver from the consumer's view: a renamed metric or env var is a major
bump.
