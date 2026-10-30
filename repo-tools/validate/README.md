# homelab-validate

Shared Repo Tooling: the repository checks a consuming repo runs in CI on every PR and in its pre-commit hook,
for **every Host** its `hosts.yaml` declares. It reads the Hosts and their stack roots through
[`homelab_hosts`](../hosts/README.md), never runs on a Host, and needs no decryption key, production credential
or Host access. Its messages state the rule each check enforces.

| Check | What it enforces, for each Host |
|---|---|
| `secrets` | Sensitive files are tracked only SOPS-encrypted as `*.sops.*`, with no plaintext twin; Stack layout (`<stack root>/<stack>/compose.yaml`, each included file `<service>/service.yaml` and included by some Stack); at least one doco-cd deploy config (`.doco-cd*.y(a)ml`, anywhere) is tracked and every one keeps `auto_discovery.depth: 1` |
| `env` | Every `${VAR}` a Stack uses is in its `env.template` and `.sops.env`; when the Host uses [dependency rendering](#dependency-rendering), its `dependencies.yml` exists and its placeholders reach `dependencies-render` |
| `compose` | `docker compose config` renders every Stack, and every `templates/<name>/` as a Stack of each Host; startup-order, logging, restart and Probe rules; no `env_file` `format` (doco-cd supports only the default); the [Baseline](#the-baseline); [volume names](#volumes) |
| `monitoring` | Full Prometheus config and rule checks, behavioral rule suites, amtool/Loki/Alloy config checks, dashboard generation and query checks, declared-containers freshness, and Grafana provisioning boots, using the images the Host deploys; see [Monitoring checks](#monitoring-checks) |
| `cloudflare` | Traefik's trusted Cloudflare ranges are valid and identical (see [Cloudflare ranges](#cloudflare-ranges)) |
| `isolation` | A change to the Host's doco-cd Stack, or to its `_baseline.yaml`, ships in its own PR |
| `privacy` | Repository-wide, not per Host: no prose matches a pattern of the denylist the environment supplies; warn-only by default, skipped without a denylist (see [Privacy](#privacy)) |

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
homelab-validate --host host-a monitoring        # one Host
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
- `--legacy-volume NAME`: a volume name from before the [naming rule](#volumes), allowed until its Stack renames it. Repeat it for
  several. A `compose` run over every Host fails while one is given that no Stack declares, so the list only
  shrinks.
- `--legacy-stack NAME`: a folder under each selected Host's stack root that is still in a pre-layout form
  (`docker-compose.yaml` files deployed by another tool, no `compose.yaml`), allowed until its cutover. Repeat it
  for several. **`--legacy-stack` exempts the folder from Stack-level checks (Baseline, Compose rules, env) until
  it is renamed to `compose.yaml`; only repository-wide and secret-reference checks still apply.** In detail:
  - not a Stack: `compose` does not render it or apply the Compose rules, the Baseline or the volume rules, and
    its Services are not counted in the Baseline report; `env` does not check its `${VAR}`s; it needs no
    `.sops.env`;
  - `secrets` allows its `docker-compose.y(a)ml` files;
  - every [input](#inputs) whose default lies in it counts as missing, so its check is skipped and says so; an
    explicit `--path` into it is still checked;
  - still enforced: plaintext `.env` and unencrypted `*.sops.*` files, `service.yaml` files no Stack includes,
    and the secret references of its `docker-compose.y(a)ml` files and what they include (`env_file`,
    `secrets.<x>.file`, `x-sops` bind mounts). Each must be a tracked `*.sops.*` file, or the untracked plaintext
    copy of one (`x.env` for a tracked `x.sops.env`, `web.yml` for `web.sops.yml`) that the other tool's deploy
    decrypts on the Host. The twin must be SOPS-encrypted, the same check as for every `*.sops.*` file; an
    extensionless reference or one whose twin would be the SOPS config `.sops.yaml` gets no twin. A tracked plaintext file it references fails.

  `secrets` fails while one is given whose folder has a `compose.yaml` (it has been cut over), and a `secrets` run
  over every Host fails while one is given that no Host has, so the list only shrinks.
- `--privacy-mode warn|fail`: whether a [privacy](#privacy) match prints a warning (default; exit status
  unaffected) or fails the run.
- `--privacy-allow REGEX`: text a privacy match may lie within and still pass, such as a load-bearing identifier
  in prose (`git\+https://\S+` for install commands). Repeat it for several.
- `HOMELAB_PRIVACY_DENYLIST`: the [privacy](#privacy) denylist; unset or blank skips the check.
- `VALIDATE_BASE`: the comparison ref for `isolation`, rule-test freshness and the Grafana upgrade boot (default
  `origin/<default_branch>`, from `hosts.yaml`). In CI, fetch history and set it to the PR base.
- `GITHUB_ACTIONS`: when set, failures print as GitHub annotations.
- `XDG_CACHE_HOME`: the image-config cache lives in `${XDG_CACHE_HOME:-~/.cache}/homelab-validate/image-volumes/`.

### Inputs

Each input is found by convention under each Host's stack root unless `--path` moves it. When a default is
missing, or lies in a `--legacy-stack`, the Host lacks that component: the check that reads it is skipped and says so. When a `--path` is missing
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

Each monitoring component's `service.yaml` must declare its Service (`prometheus`, `alertmanager`, `loki`,
`alloy`, `grafana`) with an `image`; a missing file or Service fails that component's check by name.

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

The Baseline is the settings every Service carries. This tool owns its items:

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

Each Service held to the Baseline must also meet the rules for its Traefik routers: names that carry
their kind (UI, health, Client Path), health and Client Path rules, and Protected Probes. A Service that
signs users in through the IdP itself (native OIDC) declares `probe.idp_auth: oidc` and needs an `https://`
`probe.idp_url`; `oidc` is the label's only valid value.

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

## Privacy

`privacy` keeps names (people, logins, Hosts, other repos) out of the prose a public repo shows. The denylist is
never committed: `HOMELAB_PRIVACY_DENYLIST` holds one Python regex per line (blank lines ignored), and in CI it
comes from a secret (see the `privacy-denylist` secret of `repo-validate.yaml`). Unset or blank, the check prints
that it skipped and passes, so a local run or pre-commit hook without the denylist is unaffected. An invalid
pattern fails the check, naming its line number only, never the pattern or the regex error.

It scans every tracked file it can read as text, by type:

| Files | Scanned |
|---|---|
| Markdown (`*.md`, `*.markdown`, `*.rst`) | every line |
| Python (`*.py`, `*.pyi`, or no extension with a `python` shebang) | comments and string literals: docstrings, help and output text; from where a file stops tokenizing (a syntax error), every remaining line |
| `#`-comment files: shell (`*.sh`, `*.bash`, `*.zsh`, or no extension with an `sh` shebang), YAML, TOML, `*.cfg`, `*.ini`, `*.conf`, `*.alloy`, `*.tmpl`, `*.env.example`, `Dockerfile`, `Containerfile`, `Makefile`, `.gitignore`, `.dockerignore`, `.gitattributes` | comments (from a `#` at the start of the line or after whitespace) |
| Renovate config: `renovate.json`, `renovate.json5`, `.renovaterc`, `.renovaterc.json(5)` in any folder (`.github/` included), and presets `renovate/*.json(5)` | comments, and the strings of the fields Renovate publishes in PRs and commits, at any depth (inside `packageRules` too): `description`, `prBodyNotes`, `prBodyTemplate`, `prTitle`, `groupName`, `prHeader`, `prFooter`, `commitBody`, `commitMessage*` and any `*Notes`, and the values (not the keys) of `prBodyDefinitions` and `prBodyHeadingDefinitions` |

Load-bearing identifiers are exempt, because renaming them would break something:

- code outside comments and strings: file paths, config keys and values, commands, Python identifiers, and
  `uses:` refs in workflows;
- a `uses: <ref>` anywhere, Markdown examples included;
- files that hold no prose: `LICENSE`, `COPYING` and `NOTICE` (any extension), lockfiles (`*.lock`,
  `*-lock.json`, `*-lock.yaml`, `go.sum`), SOPS-encrypted files (`*.sops.*`, `.sops.env`), binary files and
  every other type (JSON other than Renovate config, for one);
- every other Renovate config field, such as `extends`, `matchPackageNames`, `matchDepNames`, `matchStrings`,
  `depNameTemplate`, `datasourceTemplate`, `registryUrls` and `prBodyColumns`;
- any text a `--privacy-allow` pattern covers: a match is exempt when it lies wholly inside an allowlist match
  on the same line.

Renovate config is read as JSON5, which covers JSON (comments, unquoted keys, single quotes, trailing commas),
with no extra dependency. A field's string is reported at the line it starts on, and an array's strings belong to
the field that holds the array. Escapes are decoded first, in unquoted keys too, and a surrogate pair becomes its
character (a lone surrogate prints as U+FFFD). From where a file stops being valid JSON5 (an unterminated string, say), every
remaining line is scanned.

Each match is reported as `privacy: denylisted text at <file>:<line>: <excerpt>`, a warning or, with
`--privacy-mode fail`, an error (in GitHub Actions, an annotation on that line). The excerpt is the scanned prose
on that line with every denylisted match replaced by `[redacted]`, allowlisted ones included; no report repeats a match or a pattern. Start
in `warn`, clear the warnings, then switch to `fail` so new prose stays clean.

## Test

```sh
python -m venv .venv && .venv/bin/pip install -e repo-tools/validate pytest
.venv/bin/pytest repo-tools/validate
```

The tests build small throwaway Git repositories with one and with several Hosts. The `compose` tests are skipped
without Docker Compose; the `monitoring` tests fake Docker. They never reach a registry: they replay recordings in
`tests/fixtures/registry/`. To re-record them, run `python tests/registry_fixtures.py` from this folder, which
needs network access and redacts tokens.
