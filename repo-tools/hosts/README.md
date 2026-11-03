# homelab-hosts

Shared Repo Tooling: the `homelab_hosts` package is the one loader and validator for a consuming repo's
`hosts.yaml`. Every tool that needs to know a repo's Hosts depends on it. Never parse `hosts.yaml` anywhere
else.

Python 3.11 or newer. Its only dependency is PyYAML.

## Install

```sh
# the homelab-hosts CLI, pinned to a release tag (or a commit)
uv tool install "git+https://github.com/jbruns/homelab-components@hosts-v1.2.0#subdirectory=repo-tools/hosts"
```

As a dependency of another tool, in its `pyproject.toml`:

```toml
dependencies = [
  "homelab-hosts @ git+https://github.com/jbruns/homelab-components@hosts-v1.2.0#subdirectory=repo-tools/hosts",
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
validate: [python3, tools/validate/validate.py]   # optional: the repo's check, run from the repo root before
                                # a tool (e.g. hops bump) commits a change
hosts:                          # required; at least one Host
  host-a:                       # Host name: [a-z0-9][a-z0-9-]*; used for --host and .env keys
    stack_root: host-a          # required with doco-cd; dir relative to repo root holding <stack>/compose.yaml
    deployer: doco-cd           # required; doco-cd or none
    capabilities: []            # required; a subset of [proxmox, truenas, zfs], possibly empty
    doco_cd:                    # optional overrides with doco-cd; defaults shown
      stack: doco-cd            # Stack dir under stack_root holding doco-cd
      api_secret: doco-cd/api_secret.sops.txt   # relative to stack_root
      address: doco-cd:80
      network: metrics
  host-b:                       # a Host that runs no Stacks (e.g. Proxmox VE Guests)
    deployer: none              # no stack_root, no doco_cd
    capabilities: [proxmox]
```

### Deployers

| Deployer | Means |
|---|---|
| `doco-cd` | The Host runs Stacks from `<stack_root>/<stack>/compose.yaml`, deployed by doco-cd. `stack_root` is required and `doco_cd` optional. |
| `none` | The Host runs no Stacks. `stack_root` and `doco_cd` must be absent. Stack-based tools (Stack checks, declared containers, commands that act on Stacks) skip it, or refuse it when it is named. |

Paths are compared by directory identity after resolving symlinks, so on a case-insensitive filesystem `tn` and
`TN` are the same stack root, and on a case-sensitive one they are not.

Loading fails, listing every problem with its file and key path, when:

- a key is unknown, at any level;
- `version` is missing or not `1`;
- `hosts` is missing or empty;
- `validate` is not a non-empty list of non-empty strings;
- a Host name does not match `[a-z0-9][a-z0-9-]*` (YAML reads an unquoted `no` or `123` as a boolean or
  number, so quote such a name);
- `stack_root` is missing with `deployer: doco-cd`, is absolute or uses `..`, resolves (following symlinks, including in intermediate
  directories) to the repo root itself or outside it, is not an existing directory, or is the same directory as
  another Host's or contains or sits inside it;
- `deployer` is missing or unknown;
- `stack_root` or `doco_cd` is present with `deployer: none`;
- `capabilities` is missing, is not a list, or names an unknown capability;
- a `doco_cd` value or `default_branch` is not a non-empty string;
- `doco_cd.stack` is not a single Stack directory name (`[a-z0-9][a-z0-9-]*`, no `/`), or resolves (following
  a symlink) outside the Host's stack root; it need not exist yet;
- `doco_cd.api_secret` is absolute, uses `..`, or resolves (following symlinks) outside the Host's stack root;
- `doco_cd.network` is not a Docker network name (`[a-zA-Z0-9][a-zA-Z0-9_.-]*`). `address` is any non-empty string.

### Capabilities

| Capability | Means |
|---|---|
| `proxmox` | The Host runs Proxmox VE; tools may call its API. Requires the `PROXMOX_*` keys below. |
| `truenas` | The Host runs TrueNAS; tools may call its API. Requires the `TRUENAS_*` keys below. |
| `zfs` | The Host's volumes live on ZFS; tools may snapshot them. |

Tools gate Host-specific features on these, never assume them.

## .env keys

Connection details stay in the consuming repo's gitignored `.env`, keyed by Host. `NAME` is the Host name
uppercased with `-` turned into `_` (`host-a` → `HOST_A`, `my-nas` → `MY_NAS`).

| Key | Required | Meaning |
|---|---|---|
| `HOPS_<NAME>_SSH_HOST` | yes | Address (name or IP) to SSH to |
| `HOPS_<NAME>_SSH_USER` | no | SSH user; unset lets ssh decide |
| `HOPS_<NAME>_SSH_KEY` | no | SSH private key path, as written (`~` unexpanded); unset lets ssh decide |
| `HOPS_<NAME>_TRUENAS_URL` | `truenas` Hosts | TrueNAS web UI base URL |
| `HOPS_<NAME>_TRUENAS_API_KEY` | `truenas` Hosts | TrueNAS API key |
| `HOPS_<NAME>_TRUENAS_INSECURE` | no | `1`/`true`/`yes`/`on` skips TLS verification; default off |
| `HOPS_<NAME>_PROXMOX_URL` | `proxmox` Hosts | Proxmox VE API base URL |
| `HOPS_<NAME>_PROXMOX_TOKEN_ID` | `proxmox` Hosts | Proxmox VE API token ID (`user@realm!name`) |
| `HOPS_<NAME>_PROXMOX_TOKEN_SECRET` | `proxmox` Hosts | Proxmox VE API token secret |
| `HOPS_<NAME>_PROXMOX_INSECURE` | no | `1`/`true`/`yes`/`on` skips TLS verification; default off |

An empty value counts as unset. A key is missing only when a tool asks for it: a tool that uses only SSH never
needs the TrueNAS or Proxmox keys.

## API

```python
from homelab_hosts import HostsError, host_settings, load_hosts

hosts = load_hosts()  # finds the repo root from the working directory
host = hosts.pick(args.host, path=args.path)  # any Host
stack_host = hosts.pick(args.host, path=args.path, stacks=True)  # a command that works on Stacks
api_host = hosts.pick(args.host, path=args.path, capability="truenas")  # a command calling a Host API
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
  `default_branch`, `validate` (the validation command's argv as a tuple, empty when not declared), and
  `pick(name=None, path=None, *, stacks=False, capability=None) -> Host`, which returns:
  1. the Host called `name` (e.g. from `--host`), or an error listing the Hosts when there is none;
  2. else the Host whose stack root contains `path` (relative paths resolve against the working directory;
     `path` need not exist);
  3. else the only Host;
  4. else an error listing the Hosts.

  With `stacks=True`, for a tool that works on Stacks, only Hosts that run Stacks count: a named Host with
  `deployer: none` is an error stating that it runs no Stacks, and step 3 picks the only Host that runs
  Stacks, so a repo with one `doco-cd` Host and any number of `deployer: none` Hosts needs no `--host` for
  Stack work. A repo-wide Stack check iterates `[h for h in hosts.values() if h.runs_stacks]`.

  With `capability=` (e.g. `"truenas"`, for a tool that calls that Host API), only Hosts with that capability
  count in the same way: a named Host without it is an error stating that the command needs it, and step 3
  picks the only Host with it, so a repo with one TrueNAS Host and a `deployer: none` Proxmox Host needs no
  `--host` for TrueNAS calls. With both, a Host must meet both.
- `Host`: `name`, `stack_root` (as written), `path` (the stack root, absolute with symlinks resolved), `deployer`, `capabilities`
  (a frozenset), `doco_cd` (a `DocoCd` with `stack`, `api_secret`, `address`, `network`, defaults filled in),
  `runs_stacks` (false for `deployer: none`, whose `stack_root`, `path` and `doco_cd` are `None`), and
  `has(capability) -> bool`, which raises `HostsError` for a capability name it does not know, so a typo cannot
  read as "no".
- `ssh_settings(env, host) -> SshSettings`: the Host's `SshSettings(host, user, key)` from the `.env` keys above,
  read from the mapping `env` (it never reads a file). Needs only `SSH_HOST`, whatever the Host's capabilities.
- `truenas_settings(env, host) -> TrueNasSettings`: the Host's `TrueNasSettings(url, api_key, insecure)`. Needs
  only the TrueNAS keys; a Host without `truenas` raises `HostsError`.
- `proxmox_settings(env, host) -> ProxmoxSettings`: the Host's `ProxmoxSettings(url, token_id, token_secret,
  insecure)`. Needs only the Proxmox keys; a Host without `proxmox` raises `HostsError`.
- `host_settings(env, host) -> HostSettings`: all at once: `HostSettings.ssh`, `HostSettings.truenas` for a
  `truenas` Host and `HostSettings.proxmox` for a `proxmox` Host (`None` otherwise). It requires every key the
  Host could need, so a tool that uses only SSH, TrueNAS or Proxmox should call the narrower function.

  Each raises `HostsError` naming every missing or invalid key, never a value.
- `env_prefix(host) -> str`: `HOPS_<NAME>_`, the `.env` key prefix for a `Host`.
- `HostsError`: every failure above; `problems` lists each problem, and `str()` joins them with newlines.
- `CAPABILITIES`, `DEPLOYERS`, `NO_DEPLOYER`, `HOSTS_FILE`: the known capabilities, the supported deployers,
  `"none"`, and `"hosts.yaml"`.

## Develop

```sh
cd repo-tools/hosts
python -m pip install -e . pytest ruff  # or install -e . into the repo's .venv
python -m pytest && ruff check . && ruff format --check .
```

A change to the schema is a change for every consuming repo: add to it compatibly (a new optional key with a
default), and bump `version` in the file format only for a breaking change.
