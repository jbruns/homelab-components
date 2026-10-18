# homelab-components

Custom Components: code that runs on a homelab Host because no upstream or community option covers the need.
Each Component states **Why** it exists and when to retire it (**Retire when**, its Retirement Condition), takes
every site-specific value from its environment so any host can adopt it, and ships as an image
`ghcr.io/jbruns/<component>` consumed by digest. Background: [ADR 0008 in
jbruns/home-ops](https://github.com/jbruns/home-ops/blob/main/docs/adr/0008-custom-components-in-shared-repo.md).

To add or change a Component, read [CONTRIBUTING.md](CONTRIBUTING.md).

## Layout

```text
components/
└── <component>/          one folder per Component; folders starting with _ are never released
    ├── src/<package>/    runtime code (stdlib-only Python where reasonable)
    ├── tests/            unit tests, and Contract Tests marked @pytest.mark.contract
    ├── upstreams.yaml    each upstream the Component calls, pinned tag@digest
    ├── Dockerfile        minimal base (distroless Python), nonroot, HEALTHCHECK
    └── README.md         Why, Retire when, Configuration (every env var)
tools/                    Repo Tooling: README check, upstream pins, Canary planning, Contract Test harness
.github/workflows/        ci, test-component (reusable), release, canary
```

`components/_template` is the copy-from template. CI exercises it end to end (README check, unit tests, a
Contract Test against a real `traefik/whoami`, image build reaching healthy), and it is never released.

## Checks

| Check | When | Blocks merge |
|---|---|---|
| ruff, actionlint, Renovate config, Repo Tooling tests | every PR | yes |
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

Tag a commit on `main` `<component>-vX.Y.Z` (semver per Component) and push the tag:

```sh
git tag arr-queue-exporter-v1.0.0 && git push origin arr-queue-exporter-v1.0.0
```

The `release` workflow runs the Component's checks, then pushes `ghcr.io/jbruns/<component>:X.Y.Z` for
linux/amd64 and linux/arm64 with provenance and an SBOM. Consumers pin it by digest; Renovate in the consuming
repo picks up new versions.

## Renovate

`renovate.json` pins and updates base image digests in each `Dockerfile`, action digests in workflows, the
upstream pins in every `upstreams.yaml` (custom regex manager), the Python dev tools in `requirements-dev.txt`,
and tool versions annotated `# renovate:` in workflows.
