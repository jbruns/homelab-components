# homelab-components

Custom Components: code that runs on a homelab Host because no upstream or community option covers the need.
Each Component states **Why** it exists and when to retire it (**Retire when**, its Retirement Condition), takes
every site-specific value from its environment so any host can adopt it, and ships as an image
`ghcr.io/jbruns/<component>` consumed by digest. Background: [ADR 0008 in
jbruns/home-ops](https://github.com/jbruns/home-ops/blob/main/docs/adr/0008-custom-components-in-shared-repo.md).

It also holds **shared Repo Tooling** (`repo-tools/`): tools that several homelab repos (jbruns/home-ops,
asotbz/ops) run against their own Hosts, consumed by pinned tag like a Component. Background: [ADR 0012 in
jbruns/home-ops](https://github.com/jbruns/home-ops/blob/main/docs/adr/0012-shared-repo-tooling.md).

To add or change a Component or a shared tool, read [CONTRIBUTING.md](CONTRIBUTING.md).

## Layout

```text
components/
└── <component>/          one folder per Component; folders starting with _ are never released
    ├── src/<package>/    runtime code (stdlib-only Python where reasonable)
    ├── tests/            unit tests, and Contract Tests marked @pytest.mark.contract
    ├── upstreams.yaml    each upstream the Component calls, pinned tag@digest
    ├── Dockerfile        minimal base (distroless Python), nonroot, HEALTHCHECK
    └── README.md         Why, Retire when, Configuration (every env var)
repo-tools/
└── <tool>/               shared Repo Tooling for consuming repos; one Python package per tool
    ├── pyproject.toml    its own package, dependencies and ruff/pytest config
    ├── src/<package>/
    ├── tests/
    └── README.md         install, usage, and every contract consumers rely on
renovate/                 Renovate presets for consuming repos (repo-default.json)
tools/                    this repo's own Repo Tooling: README check, upstream pins, Canary planning, Contract Test harness
.github/workflows/        ci, test-component and test-repo-tool (reusable), release, canary,
                          and repo-<name>.yaml: reusable CI for consuming repos
```

Shared tools, each documented in its README:

- [`repo-tools/hosts`](repo-tools/hosts/README.md): the `homelab_hosts` package, the one loader and validator for
  a consuming repo's `hosts.yaml`.
- [`repo-tools/agent-skills`](repo-tools/agent-skills/README.md): `setup-agent-skills`, which installs and checks
  the agent skills a consuming repo pins in its `.agent-skills.lock`.
- [`repo-tools/validate`](repo-tools/validate/README.md): `homelab-validate`, the repository checks (secrets, env,
  compose and the Baseline, monitoring, cloudflare, isolation) for every Host.
- [`repo-tools/declared-containers`](repo-tools/declared-containers/README.md): `homelab-declared-containers`, each
  Host's declared Services as a node-exporter textfile for ContainerDown.

`components/_template` is the copy-from template. CI exercises it end to end (README check, unit tests, a
Contract Test against a real `traefik/whoami`, image build reaching healthy), and it is never released.

## Checks

| Check | When | Blocks merge |
|---|---|---|
| ruff, Repo Tooling tests | every PR | yes |
| actionlint, Renovate config and presets, gitleaks (this repo runs its own `repo-lint` and `repo-gitleaks`) | every PR | yes |
| `repo-validate` and `repo-declared-containers` against `.github/repo-ci-fixture` | every PR, once `repo-tools/<tool>` exists | yes |
| each `repo-tools/<tool>`: install, ruff, pytest | every PR | yes |
| README check, unit tests, Contract Tests, image build reaching healthy | each Component a PR touches (every Component when `tools/` or shared config changes) | yes |
| Canary | weekly, and on demand | no; opens an issue |

The required status check is `ci`.

### Contract Tests

A Contract Test runs the Component against each upstream at the version this repo pins in its
`upstreams.yaml` (`repository:tag@sha256:digest`). The harness (`tools/contract.py`) starts the upstream with
the Docker CLI, publishes its port on `127.0.0.1`, waits until it answers, and hands the test its URL and env.
Renovate bumps the pins here, so its PR runs the Contract Tests against the new version before it can merge.

### Canaries

Every Monday the `canary` workflow lists each upstream's tags (`skopeo list-tags`) and picks the newest stable
one with the same shape as the pinned tag: same `v` prefix, same number of version parts, same variant suffix
(`-alpine`), so pre-releases and other variants drop out. When that tag is newer than the pin, it runs the
Contract Tests with that one upstream swapped for it (`CONTRACT_UPSTREAM`, `CONTRACT_IMAGE`). A failure opens an
issue titled `Canary: <component> fails against <upstream> <version>`, or comments on the open one.

## Releasing

Tag a commit on `main` `<component>-vX.Y.Z` or `<tool>-vX.Y.Z` (semver per Component or shared tool) and push
the tag:

```sh
git tag arr-queue-exporter-v1.0.0 && git push origin arr-queue-exporter-v1.0.0
```

The `release` workflow refuses tags off `main`, runs the Component's checks, then pushes
`ghcr.io/jbruns/<component>:X.Y.Z` for linux/amd64 and linux/arm64 with provenance and an SBOM, and creates a
GitHub Release naming the image digest. A Component without a `Dockerfile` (a Host script) gets a GitHub
Release with its `src/` files attached instead. Consumers pin by digest; Renovate in the consuming repo picks
up new versions.

A tag naming a folder under `repo-tools/` (`hosts-v1.0.0`) runs that tool's checks, then creates a GitHub Release
with no image and no assets. Consumers install the tool from the tag:

```sh
uv tool install "git+https://github.com/jbruns/homelab-components@hosts-v1.0.0#subdirectory=repo-tools/hosts"
```

A Component and a `repo-tools/` folder never share a name, so a tag always names exactly one of them.

A tag `repo-ci-vX.Y.Z` releases the reusable workflows and Renovate presets together (a GitHub Release, no
assets); `repo-ci` is reserved and names no Component or tool. Bump the major when a consuming repo must change
its call: a renamed or removed input, a stricter check, or a preset rule that changes what merges by itself.

## Consuming the CI workflows and Renovate preset

A homelab repo with a `hosts.yaml` ([schema](repo-tools/hosts/README.md)) gets the same CI as the others by
calling the reusable workflows, pinned to a `repo-ci-vX.Y.Z` release by its commit:

| Workflow | Runs | Inputs (defaults) |
|---|---|---|
| `repo-gitleaks.yaml` | gitleaks on the PR's commits, a push's commits, or (another event) every commit; the repo's `.gitleaks.toml` and `.gitleaksignore` apply | `full-history` (`false`), `gitleaks-version` |
| `repo-lint.yaml` | actionlint on the repo's workflows; `renovate-config-validator --strict` | `renovate-configs` (`renovate.json`; empty skips), `actionlint-version`, `renovate-version` |
| `repo-validate.yaml` | [`homelab-validate [checks…]`](repo-tools/validate/README.md) (Docker for `compose` and `monitoring`, including promtool rule tests); pull requests compare against their base; the image-config cache is kept between runs | `checks` (every check), `extra-args`, `working-directory` (`.`), `tool-ref`, `timeout-minutes` (`30`), `uv-version` |
| `repo-declared-containers.yaml` | [`homelab-declared-containers --check`](repo-tools/declared-containers/README.md) | `extra-args`, `working-directory` (`.`), `tool-ref`, `uv-version` |

`extra-args` passes the tool's options, split like a shell command line but never run by a shell: for example
`--path dashboards=tools/dashboards/dashboards.py --legacy-volume media_config` for a repo that keeps an input
outside its stack roots, or `--host truenas`. `repo-validate` and `repo-declared-containers` install their tools
(`repo-validate` both, for the `monitoring` check) from the workflow's own commit, so the one pin on `uses:` pins
the tools too. Set `tool-ref` (a `<tool>-vX.Y.Z` tag or a commit) only to run a different tool
version than the workflow's; Renovate does not bump it. Each workflow is one job; keep the repo's own
aggregate job as its required check:

```yaml
# .github/workflows/ci.yaml in the consuming repo
on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  gitleaks:
    uses: jbruns/homelab-components/.github/workflows/repo-gitleaks.yaml@<sha> # repo-ci-v1.0.0
  lint:
    uses: jbruns/homelab-components/.github/workflows/repo-lint.yaml@<sha> # repo-ci-v1.0.0
  validate:
    uses: jbruns/homelab-components/.github/workflows/repo-validate.yaml@<sha> # repo-ci-v1.0.0
    with:
      checks: secrets env compose isolation
  monitoring:
    uses: jbruns/homelab-components/.github/workflows/repo-validate.yaml@<sha> # repo-ci-v1.0.0
    with:
      checks: monitoring
      extra-args: --path dashboards=tools/dashboards/dashboards.py
      timeout-minutes: 10
  declared-containers:
    uses: jbruns/homelab-components/.github/workflows/repo-declared-containers.yaml@<sha> # repo-ci-v1.0.0
  ci:
    if: always()
    needs: [gitleaks, lint, validate, monitoring, declared-containers]
    runs-on: ubuntu-24.04
    steps:
      - if: contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled')
        run: exit 1
```

When a repo first adopts `repo-gitleaks`, run it once with `full-history: true` (or by `workflow_dispatch`, which
always scans every commit) to clear the existing history.

The Renovate preset [`renovate/repo-default.json`](renovate/repo-default.json) carries the shared rules: digest
pinning for images and actions, a 5-day minimum release age, a morning schedule (America/Los_Angeles) with no
hourly PR limit, automerge for patch updates and digest pins, never automerge for Host-installed Component release
assets, the Grafana and Prometheus groups, and one-major-at-a-time Nextcloud. It extends the `docker-compose`
manager to every `service.yaml` (Renovate cannot read `hosts.yaml`, so this covers each Host's
`<stack>/<service>/service.yaml` by name) and reads `# renovate:` version comments in workflows. It adds no
Contract-Test gate: those run here, where a Component releases only after its Contract Tests pass against the
pinned upstreams, so a consumer only ever sees tested releases. Pin it to the
same release; anything repo-specific (more automerge, custom datasources, a different schedule) goes after it:

```json
{
  "extends": ["github>jbruns/homelab-components//renovate/repo-default#repo-ci-v1.0.0"],
  "packageRules": []
}
```

With both pins on the same tag, the preset makes Renovate propose each new `repo-ci-vX.Y.Z` as one PR that moves
every `uses:` commit and the `extends` tag together. Patch releases automerge like any patch; review minor and
major ones against their release notes.

## Renovate

`renovate.json` pins and updates base image digests in each `Dockerfile`, action digests in workflows, the
upstream pins in every `upstreams.yaml` (custom regex manager), the Python dev tools in `requirements-dev.txt`,
and tool versions annotated `# renovate:` in workflows.

## License

[MIT](LICENSE).
