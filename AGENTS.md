# Agents

Before adding or changing a Component, read [CONTRIBUTING.md](CONTRIBUTING.md): test-first, env-only
configuration, and the README rule CI enforces.

Shared Repo Tooling for consuming repos lives in `repo-tools/<tool>/`, one package per tool; read
CONTRIBUTING.md's "Shared Repo Tooling" section first. Only `repo-tools/hosts` (`homelab_hosts`) parses a
consuming repo's `hosts.yaml`; its [README](repo-tools/hosts/README.md) is the schema's source of truth, so update
it with any change. `tools/` is this repo's own Component tooling, not shared.
