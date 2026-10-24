# homelab-validate

Shared Repo Tooling: the repository checks a consuming repo runs in CI on every PR and in its pre-commit hook,
for **every Host** its `hosts.yaml` declares. It reads the Hosts and their stack roots through
[`homelab_hosts`](../hosts/README.md), never runs on a Host, and needs no decryption key, production credential
or Host access. Background: [ADR 0012 in
jbruns/home-ops](https://github.com/jbruns/home-ops/blob/main/docs/adr/0012-shared-repo-tooling.md); the ADRs
named in messages (0003, 0006, 0007, 0010) are jbruns/home-ops ADRs.

| Check | What it enforces, for each Host |
|---|---|
| `secrets` | Sensitive files are tracked only SOPS-encrypted as `*.sops.*`, with no plaintext twin; Stack layout (`<stack root>/<stack>/compose.yaml`, each included file `<service>/service.yaml` and included by some Stack); at least one doco-cd deploy config (`.doco-cd*.y(a)ml`, anywhere) is tracked and every one keeps `auto_discovery.depth: 1` |
| `env` | Every `${VAR}` a Stack uses is in its `env.template` and `.sops.env`; when the Host uses [dependency rendering](#dependency-rendering), its `dependencies.yml` exists and its placeholders reach `dependencies-render` |
| `compose` | `docker compose config` renders every Stack, and every `templates/<name>/` as a Stack of each Host; startup-order, logging, restart and Probe rules; no `env_file` `format` (doco-cd supports only the default); the [Baseline](#the-baseline); [volume names](#volumes) |
| `monitoring` | Full Prometheus config and rule checks, behavioral rule suites, amtool/Loki/Alloy config checks, dashboard generation and query checks, declared-containers freshness, and Grafana provisioning boots, using the images the Host deploys; see [Monitoring checks](#monitoring-checks) |
| `cloudflare` | Traefik's trusted Cloudflare ranges are valid and identical (see [Cloudflare ranges](#cloudflare-ranges)) |
| `isolation` | A change to the Host's doco-cd Stack, or to its `_baseline.yaml`, ships in its own PR |

No check depends on a Host capability: the only Host-specific inputs are its stack root, its doco-cd Stack name
(`doco_cd.stack` in `hosts.yaml`) and the files found under its stack root. An input a Host does not have by
convention is skipped (see [Inputs](#inputs)), so a Host without TrueNAS, monitoring or Traefik passes on the checks that apply.

## Install

Python 3.11 or newer. Pin a release tag (or a commit):

```sh
uv tool install "git+https://github.com/jbruns/homelab-components@validate-v1.0.0#subdirectory=repo-tools/validate"
uv tool install "git+https://github.com/jbruns/homelab-components@declared-containers-v1.0.0#subdirectory=repo-tools/declared-containers"
```

Prerequisites, by check:

- every check: `git` (it reads tracked files and diffs against the base ref);
- `compose`: Docker with the Compose plugin (`docker compose config`), and anonymous HTTPS access to the image
  registries (see [Volumes](#volumes));
- `monitoring`: Docker (it runs the deployed Prometheus, Alertmanager, Loki, Alloy and Grafana images), and
  [`homelab-declared-containers`](../declared-containers/README.md) on `PATH` unless `--path declared-containers`
  names a script.

## Run

```sh
homelab-validate                                  # every check, every Host
homelab-validate secrets env                      # just these
homelab-validate --host truenas monitoring        # one Host
```

With no check named, every check runs, including `isolation`. Exit status is 1 if any check failed, or if
`hosts.yaml` is invalid or `--host` names no Host.

## Configuration

- `--root DIR`: the repository to check (default: the one containing the working directory, found by its
  `hosts.yaml`).
- `--host NAME`: check only this Host (default: every Host).
- `--path NAME=PATH`: move one [input](#inputs), relative to `--root` or absolute, for every selected Host. It must
  exist (a directory or file, as the input needs) for every selected Host, or each check that reads it fails. Repeat
  it for several. To move an input differently per Host, run once per Host with `--host`.
- `--legacy-volume NAME`: a volume name from before ADR 0010, allowed until its Stack renames it. Repeat it for
  several. A `compose` run over every Host fails while one is given that no Stack declares, so the list only
  shrinks.
- `VALIDATE_BASE`: the comparison ref for `isolation`, rule-test freshness and the Grafana upgrade boot (default
  `origin/<default_branch>`, from `hosts.yaml`). In CI, fetch history and set it to the PR base.
- `GITHUB_ACTIONS`: when set, failures print as GitHub annotations.
- `XDG_CACHE_HOME`: the image-config cache lives in `${XDG_CACHE_HOME:-~/.cache}/homelab-validate/image-volumes/`.

### Inputs

Each input is found by convention under each Host's stack root unless `--path` moves it. When a default is
missing, the Host lacks that component: the check that reads it is skipped and says so. When a `--path` is missing
or the wrong type (a file for a directory input, or the reverse), the check that reads it fails with
`--path NAME=PATH: not a directory` (or `not a file`), so a typo cannot turn a check off.

| Name | Default, under the stack root | When missing |
|---|---|---|
| `prometheus` | `monitoring/prometheus` (`service.yaml`, `prometheus.yml`, `*rules*.yml`, `dependencies.yml` when [used](#dependency-rendering)) | `monitoring` skips the Host |
| `rule-tests` | `monitoring/prometheus/tests` (`*.test.yml` promtool suites) | every rule fails for want of a case |
| `alertmanager` | `monitoring/alertmanager` (`service.yaml`, `config.yml`, or an encrypted-only `config.sops.*`) | amtool skipped |
| `loki` | `monitoring/loki` (`service.yaml`, `loki.yaml`) | Loki check skipped; the Grafana check fails |
| `alloy` | `monitoring/alloy` (`service.yaml`, `config.alloy`) | Alloy check skipped |
| `grafana` | `monitoring/grafana` (`service.yaml`, `provisioning/`, `datasources.sops.env`) | Grafana checks skipped |
| `dashboards-json` | `monitoring/grafana/dashboards` (generated JSON) | no dashboard queries |
| `dashboards` | none: the repo's dashboard generator script | generator parity skipped |
| `declared-containers` | none: runs `homelab-declared-containers` | (always runs when `prometheus` exists) |
| `blackbox` | `monitoring/blackbox-exporter/blackbox.yml` | no `probe.module` is valid |
| `traefik` | `edge/traefik/service.yaml` | `cloudflare` skips the Host |

For example, a repo that keeps its dashboard generator and promtool suites outside the stack root runs
`homelab-validate --path dashboards=tools/dashboards/dashboards.py --path rule-tests=tools/validate/rules`.

Every check reads an input from the files on disk, inside the repo or outside it. amtool checks the
`config.yml` in `alertmanager`. When there is only a `config.sops.*`, it warns that the config is encrypted-only.
With neither, it fails. A `--path` outside the repo has no Git history, so rule-test freshness and the Grafana
upgrade boot warn and skip only their comparison with the base ref; the rest of each check runs.

### Dependency rendering

A Host uses dependency rendering when `<prometheus>/service.yaml` declares a `dependencies-render` Service,
`<prometheus>/prometheus.yml` reads the rendered `dependencies.yml`, or `<prometheus>/dependencies.yml` exists.
Then `env` and `monitoring` fail when the template is missing, and `env` fails when no `dependencies-render`
Service renders it. A Host with none of the three needs no template.

A generator (`dashboards`, `declared-containers`) is run with `--check` and must exit 0 when its committed output
is current. A script path is run with the Python running validate; `dashboards` gets `--out <dashboards-json>`,
`declared-containers` gets `--root <root> --host <host>`.

## The Baseline

The Baseline (ADR 0007) is the settings every Service carries. This tool owns its items:

| Item | Met when |
|---|---|
| `healthcheck` | the Service declares a `healthcheck` test and does not disable it |
| `no-new-privileges` | `security_opt` holds `no-new-privileges[:=]true` |
| `digest` | the image is pinned by digest (`tag@sha256:…`) |
| `memory` | `deploy.resources.limits.memory` or `mem_limit` is set |
| `cap_drop` | `cap_drop` holds `ALL` |
| `anonymous-volumes` | every image `VOLUME` path has a mount (see [Volumes](#volumes)) |

Each Host's stack root holds its own `_baseline.yaml`, with a `baseline` Service carrying the values. Every
Service `extends` its Host's file (`file: ../_baseline.yaml` relative to the declaring file, `service:
baseline`), except the Host's doco-cd Stack (`doco_cd.stack`), whose Services carry the same items inline:
extending would race doco-cd's self-update. Both are held to every item, and doco-cd's Stack fails if it extends
the file. Every template Service must extend it too.

A Service that cannot meet an item records an **Exemption** as a label, `baseline.exempt.<item>: "<reason>"`.
An Exemption naming no item, or one the Service no longer needs, fails. `compose` prints, per Host, how many
Services meet the Baseline and lists every Exemption.

Each Service held to the Baseline must also follow ADR 0006 for its Traefik routers: names that carry their
kind (UI, health, Client Path), health and Client Path rules, and Protected Probes.

## Volumes

`compose` reads each Stack's top-level `volumes:` as written, since the rendered model fills in a default name:

- Every volume has an explicit `name:`.
- A volume is `external: true` (it holds data, so Compose can never remove it), or it carries the label
  `homeops.regenerable: "<what regenerates it>"`.
- The name starts with `<stack>_`. An external volume may instead carry another Stack's prefix, when it borrows
  that Stack's volume, or be a `--legacy-volume`. Templates are checked as Stack `app`.

Anonymous volumes are a Baseline item (`anonymous-volumes`), including for doco-cd's own Services. A volume with
only a target (`- /data`) always fails. Every `VOLUME` path in the image config must have a volume, bind mount or
`tmpfs` at that exact path; a mount on a parent directory does not count, because Docker still creates the
anonymous volume beneath it.

The image config is fetched anonymously from its registry (Docker Hub, GHCR, lscr.io, quay.io and other standard
registries) by the pinned digest; for an index, the `linux/amd64` image. Results are cached by digest in the cache
directory above; in CI, cache it keyed on the digests in the stack roots and `templates/`. Rate limits and server
errors are retried twice; after that, a registry failure fails the check and names the image. validate cannot
inspect an image without a digest, or a Service whose `platform:` is not `linux/amd64`: such a Service fails unless
it carries a `baseline.exempt.anonymous-volumes` reason. A Service with `volumes_from` always fails, because
validate does not follow what it inherits. Services behind a Compose profile are checked too (`--profile '*'`).

To render, `compose` copies the tracked files to a temporary directory, fills each Stack's `.env` with placeholder
values for every key of its `env.template` and every `${VAR}` it uses, and creates empty stand-ins for the
decrypted files Compose references.

## Monitoring checks

- `promtool check config` mounts the real rules, a rendered `dependencies.yml` (when used) with dummy
  URLs/keys at `/etc/prometheus/sd/dependencies.yml`, and dummy Compose secrets at `/run/secrets/<name>`.
- `promtool test rules` runs every `rule-tests/*.test.yml` with the deployed rule files. Every alert needs firing
  and nonfiring cases (except the always-firing Watchdog), and every firing expectation must include the `tier`
  and `stack` labels Alertmanager routes and inhibits on. A templated `tier` or `stack` (`{{ $labels.X }}`) must
  have `X` pinned non-empty in the expression. An alert with `for:` needs, in one test group, a nonfiring
  evaluation at most `for:` before a firing one. Every recording rule needs an assertion. A rule that changed
  since `VALIDATE_BASE` must change its behavioral case too; comment-only changes need no test edit.
- Grafana boots with `provisioning/datasources.yml`, rendered with dummy URLs and credentials (every placeholder
  must be set in `datasources.sops.env`), and the real dashboard provider and JSON. It checks health,
  provisioning logs and the provisioned datasource and dashboard uids, then boots again on the same database,
  seeded from the base ref's datasource template when there is one, so uid/name changes must carry their
  migration.
- Every panel target, query variable and annotation is checked against its datasource with an empty Prometheus
  (`/api/v1/query`) and Loki (`/loki/api/v1/format_query`). These are syntax checks, not proof that metrics or
  logs exist. Failures name the dashboard, panel, variable or annotation and the original query.

Disposable containers publish random ports on loopback only and are removed with their anonymous volumes on exit.
Plugin downloads and Grafana update and telemetry checks are disabled.

## Cloudflare ranges

The Cloudflare edge ranges Traefik trusts for `X-Forwarded-For` appear three times in the Host's Traefik Compose
file: `forwardedHeaders.trustedIPs=` on both entrypoints and the CrowdSec bouncer's `forwardedHeadersTrustedIPs:`.
They must be valid CIDRs and identical. The consuming repo keeps its own updater.

## Test

```sh
python -m venv .venv && .venv/bin/pip install -e repo-tools/validate pytest
.venv/bin/pytest repo-tools/validate
```

The tests build small throwaway Git repositories with one and with several Hosts. The `compose` tests are skipped
without Docker Compose; the `monitoring` tests fake Docker. They never reach a registry: they replay recordings in
`tests/fixtures/registry/`. To re-record them, run `python tests/registry_fixtures.py` from this folder, which
needs network access and redacts tokens.
