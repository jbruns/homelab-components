# homelab-hosts

Shared Repo Tooling: the `homelab_hosts` package is the one loader and validator for a consuming repo's
`hosts.yaml`. Every tool that needs to know a repo's Hosts depends on it. Never parse `hosts.yaml` anywhere
else. Background: [ADR 0012 in jbruns/home-ops](https://github.com/jbruns/home-ops/blob/main/docs/adr/0012-shared-repo-tooling.md).

Python 3.11 or newer. Its only dependency is PyYAML.

## Install

```sh
# the homelab-hosts CLI, pinned to a release tag (or a commit)
uv tool install "git+https://github.com/jbruns/homelab-components@hosts-v1.0.0#subdirectory=repo-tools/hosts"
```

As a dependency of another tool, in its `pyproject.toml`:

```toml
dependencies = [
  "homelab-hosts @ git+https://github.com/jbruns/homelab-components@hosts-v1.0.0#subdirectory=repo-tools/hosts",
]
```

`homelab-hosts` validates the `hosts.yaml` of the repo containing the working directory and lists its Hosts
(`--json` for machine-readable output). It exits 1 and prints every problem when the file is invalid.

## hosts.yaml (version 1)

A committed file at the root of the consuming repo. It declares every Host the repo's tools may reach; a Host it
does not declare is out of their reach.

```yaml
version: 1                      # required; only 1 is supported
default_branch: main            # optional, default main
hosts:                          # required; at least one Host
  nc8kg1101:                    # Host name: [a-z0-9][a-z0-9-]*; used for --host and .env keys
    stack_root: nc8kg1101       # required; dir relative to repo root holding <stack>/compose.yaml
    deployer: doco-cd           # required; only doco-cd is supported
    capabilities: []            # required; a subset of [truenas, zfs], possibly empty
    doco_cd:                    # optional overrides; defaults shown
      stack: doco-cd            # Stack dir under stack_root holding doco-cd
      api_secret: doco-cd/api_secret.sops.txt   # relative to stack_root
      address: doco-cd:80
      network: metrics
```

Paths are compared by directory identity after resolving symlinks, so on a case-insensitive filesystem `tn` and
`TN` are the same stack root, and on a case-sensitive one they are not.

Loading fails, listing every problem with its file and key path, when:

- a key is unknown, at any level;
- `version` is missing or not `1`;
- `hosts` is missing or empty;
- a Host name does not match `[a-z0-9][a-z0-9-]*` (YAML reads an unquoted `no` or `1101` as a boolean or
  number, so quote such a name);
- `stack_root` is missing, is absolute or uses `..`, resolves (following symlinks, including in intermediate
  directories) to the repo root itself or outside it, is not an existing directory, or is the same directory as
  another Host's or contains or sits inside it;
- `deployer` is missing or unknown;
- `capabilities` is missing, is not a list, or names an unknown capability;
- a `doco_cd` value or `default_branch` is not a non-empty string;
- `doco_cd.stack` is not a single Stack directory name (`[a-z0-9][a-z0-9-]*`, no `/`), or resolves (following
  a symlink) outside the Host's stack root; it need not exist yet;
- `doco_cd.api_secret` is absolute, uses `..`, or resolves (following symlinks) outside the Host's stack root;
- `doco_cd.network` is not a Docker network name (`[a-zA-Z0-9][a-zA-Z0-9_.-]*`). `address` is any non-empty string.

### Capabilities

| Capability | Means |
|---|---|
| `truenas` | The Host runs TrueNAS; tools may call its API. Requires the `TRUENAS_*` keys below. |
| `zfs` | The Host's volumes live on ZFS; tools may snapshot them. |

Tools gate Host-specific features on these, never assume them.

## .env keys

Connection details stay in the consuming repo's gitignored `.env`, keyed by Host. `NAME` is the Host name
uppercased with `-` turned into `_` (`nc8kg1101` → `NC8KG1101`, `my-nas` → `MY_NAS`).

| Key | Required | Meaning |
|---|---|---|
| `HOPS_<NAME>_SSH_HOST` | yes | Address (name or IP) to SSH to |
| `HOPS_<NAME>_SSH_USER` | no | SSH user; unset lets ssh decide |
| `HOPS_<NAME>_SSH_KEY` | no | SSH private key path, as written (`~` unexpanded); unset lets ssh decide |
| `HOPS_<NAME>_TRUENAS_URL` | `truenas` Hosts | TrueNAS web UI base URL |
| `HOPS_<NAME>_TRUENAS_API_KEY` | `truenas` Hosts | TrueNAS API key |
| `HOPS_<NAME>_TRUENAS_INSECURE` | no | `1`/`true`/`yes`/`on` skips TLS verification; default off |

An empty value counts as unset.

## API

```python
from homelab_hosts import HostsError, host_settings, load_hosts

hosts = load_hosts()  # finds the repo root from the working directory
host = hosts.pick(args.host, path=args.path)
if host.has("zfs"):
    ...
settings = host_settings(env, host)  # env: a mapping the caller built from .env and/or os.environ
ssh_target = settings.ssh.host
```

Everything below is exported from `homelab_hosts`; nothing else is public.

- `find_repo_root(start=None) -> Path`: the nearest directory at or above `start` (default: the working
  directory) holding `hosts.yaml`.
- `load_hosts(root=None) -> Hosts`: loads and validates `<root>/hosts.yaml`; `root` defaults to
  `find_repo_root()`.
- `Hosts`: a read-only mapping of Host name to `Host`, in file order, with `root` (the repo root),
  `default_branch`, and `pick(name=None, path=None) -> Host`, which returns:
  1. the Host called `name` (e.g. from `--host`), or an error listing the Hosts when there is none;
  2. else the Host whose stack root contains `path` (relative paths resolve against the working directory;
     `path` need not exist);
  3. else the only Host;
  4. else an error listing the Hosts.
- `Host`: `name`, `stack_root` (as written), `path` (the stack root, absolute with symlinks resolved), `deployer`, `capabilities`
  (a frozenset), `doco_cd` (a `DocoCd` with `stack`, `api_secret`, `address`, `network`, defaults filled in), and
  `has(capability) -> bool`, which raises `HostsError` for a capability name it does not know, so a typo cannot
  read as "no".
- `host_settings(env, host) -> HostSettings`: the Host's settings from the `.env` keys above, read from the
  mapping `env`; it never reads a file. `HostSettings.ssh` is an `SshSettings(host, user, key)`;
  `HostSettings.truenas` is a `TrueNasSettings(url, api_key, insecure)` for a `truenas` Host and `None`
  otherwise. Missing required keys raise `HostsError` naming the keys, never their values.
- `env_prefix(host) -> str`: `HOPS_<NAME>_`, the `.env` key prefix for a `Host`.
- `HostsError`: every failure above; `problems` lists each problem, and `str()` joins them with newlines.
- `CAPABILITIES`, `DEPLOYERS`, `HOSTS_FILE`: the known capabilities, the supported deployers, and `"hosts.yaml"`.

## Develop

```sh
cd repo-tools/hosts
python -m pip install -e . pytest ruff  # or install -e . into the repo's .venv
python -m pytest && ruff check . && ruff format --check .
```

A change to the schema is a change for every consuming repo: add to it compatibly (a new optional key with a
default), and bump `version` in the file format only for a breaking change.
