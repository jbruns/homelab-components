# startup-order

Stops and starts a Host's Docker containers in order, by a level label, across every Compose project. At shutdown it
records what was running and stops it from the highest level down to 0. At boot it waits for Docker, restarts what
dockerd brought up out of order, then starts each level from 0 upwards, waiting for every container in a level to be
running and (if it has a healthcheck) healthy before starting the next.

It is a POSIX `sh` script that runs on the Host itself (not in a container), from the Host's boot and shutdown hooks.
It uses only the `docker` CLI, `awk`, `sort`, `sed`, and util-linux `flock` and `setsid` (`start`, `run` and `plan`
exit 2 when `flock` is missing). TrueNAS SCALE and busybox both provide them.

## Why

Compose `depends_on` orders containers only within one Compose project, and only when Compose starts them. At boot,
dockerd starts every container with a restart policy at once, in no particular order and ignoring `depends_on`, so an
app can come up before the database or reverse proxy it needs from another project. Docker Engine has no start order
or cross-container dependency for restart policies, and Compose rejects `depends_on` on a service from another
project (docker/compose#3951, closed). Moving every Stack into one project, or replacing restart policies with
systemd units or Podman Quadlets, would cover it but changes how the Host runs containers; on an appliance such as
TrueNAS, OS-level units can also be discarded by an update. Nothing else orders containers across projects on a
plain Docker Host.

## Retire when

Docker Engine starts restart-policy containers at daemon start in an order derived from declared dependencies or
health, across Compose projects (for example, dockerd honouring Compose `depends_on: condition: service_healthy` at
boot, or an engine-level start priority), or the Host's platform (e.g. TrueNAS Apps) offers cross-app start ordering
it keeps across updates.

## Levels

Each container carries the label `startup-order=<level>` (the label name is configurable): a whole number from 0 to
`STARTUP_ORDER_MAX_LEVEL`, without leading zeros. A container without a valid level is treated as
`STARTUP_ORDER_DEFAULT_LEVEL`. A container labelled `startup-order=skip` is never stopped or started. One convention,
with the defaults:

| Level | For |
|---|---|
| 0 | edge: reverse proxy, DNS |
| 1 | datastores and monitoring core |
| 2 | applications (and anything unlabelled) |
| 3 | sidecars and exporters |

```yaml
services:
  db:
    labels:
      startup-order: "1"
```

## Usage

```sh
startup-order.sh stop    # at shutdown: record running containers, stop them highest level first
startup-order.sh start   # at boot: run `run` in the background (setsid) and return at once
startup-order.sh run     # wait for Docker and the restart-policy starts to settle, then start level by level
startup-order.sh plan    # print what `run` would do, changing nothing
```

At boot, `run` starts every container that is running now or was recorded at the last `stop`; a recorded name that
no longer exists is dropped. Containers already running at `STARTUP_ORDER_REORDER_FROM` and above are stopped first
so they come back after the levels below them. Before stopping anything, `run` writes every target to the state
file, and removes it only when it finishes, so a run interrupted mid-reorder leaves the next run what it stopped. A
level that is not ready within `STARTUP_ORDER_LEVEL_TIMEOUT` logs a `WARNING` and the next level starts anyway.

`run` holds an exclusive `flock` on `run.lock` (`plan` on `plan.lock`), so two runs never overlap. The kernel drops
the lock when the run exits, is killed or the Host reboots, so a crash never leaves a lock to clear by hand.

Exit status: 0 on success, 1 when Docker never answered or another run holds the lock, 2 for a bad command, an
invalid setting or missing `flock`, 130/143 when interrupted.

Logs, all under `STARTUP_ORDER_DIR`: `shutdown.log` (appended by `stop`), `last.log` (the latest `start`). `run` and
`plan` log to stdout.

## Configuration

Set these in the environment of the hook that calls the script; `start` passes them on to the background `run`.

| Variable | Default | Description |
|---|---|---|
| `STARTUP_ORDER_DIR` | `/var/lib/startup-order` | Directory for the state file, lock files and logs. Must survive a reboot. |
| `STARTUP_ORDER_LABEL` | `startup-order` | Container label holding the level. |
| `STARTUP_ORDER_MAX_LEVEL` | `3` | Highest level; valid levels are 0 to this. |
| `STARTUP_ORDER_DEFAULT_LEVEL` | `2` | Level for containers without a valid level label. |
| `STARTUP_ORDER_REORDER_FROM` | `2` | At boot, running containers at this level and above are restarted in order. Above `STARTUP_ORDER_MAX_LEVEL` restarts none. |
| `STARTUP_ORDER_LEVEL_TIMEOUT` | `420` | Seconds to wait for a level to be running and healthy before moving on. |
| `STARTUP_ORDER_DOCKER_WAIT` | `900` | Seconds `run` waits for the Docker daemon to answer before giving up. |
| `STARTUP_ORDER_SETTLE` | `20` | Seconds the set of running containers must stay unchanged before `run` reorders (dockerd's own restart-policy starts settling). |
| `STARTUP_ORDER_SETTLE_MAX` | `180` | Most seconds to wait for that to settle. |

## Install

Each release attaches the script as `startup-order.sh` to the GitHub Release `startup-order-vX.Y.Z`. Install it by a
pinned version and its SHA-256 checksum; rerunning the same commands is safe and replaces the file only once the
download verifies.

1. Read the checksum of the release asset (GitHub records it at upload) and pin it beside the version:

   ```sh
   gh release view startup-order-vX.Y.Z --repo jbruns/homelab-components \
     --json assets --jq '.assets[] | select(.name == "startup-order.sh") | .digest'
   ```

2. Install on the Host (as root):

   ```sh
   version=X.Y.Z
   sha256=<64 hex characters from step 1, without the sha256: prefix>
   dest=/usr/local/bin/startup-order.sh   # any path that survives the Host's updates
   curl -fsSL -o "$dest.new" \
     "https://github.com/jbruns/homelab-components/releases/download/startup-order-v$version/startup-order.sh"
   echo "$sha256  $dest.new" | sha256sum -c - && chmod 0755 "$dest.new" && mv "$dest.new" "$dest"
   ```

3. Register the hooks, with any configuration in front:

   ```sh
   STARTUP_ORDER_DIR=/srv/startup-order sh /usr/local/bin/startup-order.sh stop    # at shutdown, before dockerd stops
   STARTUP_ORDER_DIR=/srv/startup-order sh /usr/local/bin/startup-order.sh start   # at boot, after dockerd starts
   ```

   On TrueNAS, these are a SHUTDOWN and a POSTINIT command under System → Advanced → Init/Shutdown Scripts, and the
   script and `STARTUP_ORDER_DIR` belong on a pool dataset, since an update replaces the boot environment.

Run `startup-order.sh plan` after installing to check what a boot would do.

## Upstreams

| Upstream | Pinned in | Contract |
|---|---|---|
| Docker CLI and Engine | [`upstreams.yaml`](upstreams.yaml) (`docker:<version>-dind`) | `ps -a` with `{{.Label}}`, `ps`, `inspect` of `.State.Status` and `.State.Health.Status`, multi-name `start`/`stop` and `info` behave as the script expects, against a real engine |

The Contract Tests copy the script into the pinned dind container and run it there, so the `docker` CLI and the
engine are both the pinned version (or the Canary's), and busybox `sh` and `awk` keep it POSIX. They label busybox
containers at each level, leave one unlabelled and mark one `skip`, then check a shutdown followed by a boot, a boot
after dockerd already started everything, and `plan`.
