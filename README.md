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
tools/                    this repo's own Repo Tooling: README check, upstream pins, Canary planning, Contract Test harness
.github/workflows/        ci, test-component and test-repo-tool (reusable), release, canary
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
| ruff, actionlint, Renovate config, Repo Tooling tests | every PR | yes |
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

## Renovate

`renovate.json` pins and updates base image digests in each `Dockerfile`, action digests in workflows, the
upstream pins in every `upstreams.yaml` (custom regex manager), the Python dev tools in `requirements-dev.txt`,
and tool versions annotated `# renovate:` in workflows.

## License

[MIT](LICENSE).
