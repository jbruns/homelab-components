# homelab-declared-containers

Shared Repo Tooling: generates, for **each Host** a consuming repo's `hosts.yaml` declares, the desired state its
ContainerDown alert compares with cAdvisor. It reads the Hosts and their stack roots through
[`homelab_hosts`](../hosts/README.md) and never runs on a Host.

Each Host gets its own file, `<stack root>/monitoring/node-exporter/declared_containers.prom`, which node-exporter
serves through its textfile collector. Every Service in `<stack root>/<stack>/compose.yaml` and its `include:`
files becomes one line:

```text
homeops_container_declared{stack="<stack>",service="<service>",name="<container>",tier="<tier>"} 1
```

- `stack` is the Stack's folder name, matching `com.docker.compose.project`.
- `name` is `container_name`, or Compose's `<stack>-<service>-1` when unset. That is a guess (the number can grow,
  as doco-cd's self-updates show), so alerts should match on `stack` and `service`.
- `tier` is the Service's `tier` label. A Service without one, or a variable `container_name` or `tier`, is an
  error.

The metric name, its labels and the output path are the contract alert rules rely on; changing one is a major
release.

## Install

Python 3.11 or newer. Pin a release tag (or a commit):

```sh
uv tool install "git+https://github.com/jbruns/homelab-components@declared-containers-v1.0.0#subdirectory=repo-tools/declared-containers"
```

As a library (`compose_files()` and `labels_of()` read Compose Services the same way; keep their signatures
stable), depend on the same URL in `pyproject.toml` and `import homelab_declared_containers`.

## Run

```sh
homelab-declared-containers           # rewrite every Host's .prom file
homelab-declared-containers --check   # exit 1, naming each stale file, if any is stale
```

Rerun it and commit the files whenever a Service is added, removed or renamed. [`homelab-validate
monitoring`](../validate/README.md) runs `--check --root <root> --host <host>` for each Host. doco-cd recreates
node-exporter when the file changes.

## Configuration

- `--root DIR`: the repository to read (default: the one containing the working directory, found by its
  `hosts.yaml`).
- `--host NAME`: only this Host (default: every Host).
- `--out FILE`: the file to write or check instead of the default; needs `--host` when `hosts.yaml` declares more
  than one Host.

Exit status is 1 when `hosts.yaml` is invalid, `--host` names no Host, a Service breaks a rule above, or (with
`--check`) a file is stale; 2 on a usage error.

## Test

```sh
python -m venv .venv && .venv/bin/pip install -e repo-tools/declared-containers pytest
.venv/bin/pytest repo-tools/declared-containers
```

The tests build small fixture repositories with one and with several Hosts.
