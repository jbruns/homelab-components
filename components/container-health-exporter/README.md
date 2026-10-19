# container-health-exporter

A Prometheus exporter for Docker health-check status. It reads the Docker Engine API (API version 1.44,
pinned in every request path), ideally through a read-only [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
that allows `CONTAINERS=1`, and reports for each container that defines a health check whether that check
reports healthy. A stopped container with a health check reports 0, and a container without one is not
reported. It also reports containers still named `<12-hex id>_<name>`, which an interrupted Compose recreate
leaves behind.

## Why

A Service's own health check is the best signal of whether it is up when no external Probe reaches it, and it
must stay visible when the Service stops. Checked options:

- **cAdvisor** (v0.55 and later) exports `container_health_state` for running containers, but a stopped
  container drops out of its metrics instead of reporting 0, so a stopped Service has no series to alert on.
  It also keeps a container's first name, so it cannot show a container left under Compose's temporary
  `<id>_<name>` name.
- **The Docker engine's metrics endpoint** (`metrics-addr`) counts health checks engine-wide, not per container.
- **[leandry/container-health-exporter](https://hub.docker.com/r/leandry/container-health-exporter)** reads
  `/var/run/docker.sock` directly, does not report leftover names, and has not been updated since February 2025.

## Retire when

cAdvisor's `container_health_state` (or the Docker engine's metrics endpoint) keeps reporting a container with a
health check after it stops, and something already scraped reports containers under Compose's temporary
`<id>_<name>` name.

## Configuration

Every environment variable the code reads, with its default (`required` when there is none). CI fails if this
table and the code disagree.

| Variable | Default | Description |
|---|---|---|
| `DOCKER_HOST` | `docker-socket-proxy:2375` | Docker Engine API over plain TCP, as `host:port` or `tcp://host:port` (port defaults to 2375). |
| `PORT` | `9714` | Port serving `/metrics` and `/healthz`. |
| `TIMEOUT` | `10` | Seconds to wait for each Docker API request. Overlapping scrapes share one collection; a scrape joining one already running waits at most `TIMEOUT` + 1 s, then reports `container_health_up 0`. |
| `CONTAINER_LABELS` | empty (none) | Comma-separated container labels copied onto `container_healthy` as metric labels (`tier`, `com.example.team`); characters invalid in a metric label name become `_`. A container without the label gets `""`. |
| `IGNORE_LABEL` | empty (ignore none) | Ignore containers carrying this label, as `key=value`, or `key` for any value (e.g. `startup-order=skip` for ad-hoc jobs). |

## Metrics

| Metric | Description |
|---|---|
| `container_health_up` | 1 if the Docker API answered. |
| `container_healthy{container,stack,<CONTAINER_LABELS>}` | 1 while the container's health check reports healthy; 0 while unhealthy, starting or stopped. `stack` is the `com.docker.compose.project` label. |
| `container_leftover_name{container,stack,service}` | 1 for a container still named `<id>_<name>` by an interrupted Compose recreate; `service` is `com.docker.compose.service`. |

Scrape with `honor_labels: true` so each series keeps its own `container` and `stack`.

## Healthcheck

`healthcheck` exits 0 when the exporter answers on its own `/healthz`, which never calls Docker. The image
declares it as its `HEALTHCHECK`; Compose should declare the same one (home-ops Baseline):

```yaml
healthcheck:
  test: ["CMD", "/usr/bin/python3", "-m", "container_health_exporter", "healthcheck"]
```

## Upstreams

| Upstream | Pinned in | Contract |
|---|---|---|
| docker | [`upstreams.yaml`](upstreams.yaml) (`upstreams`) | `GET /v1.44/containers/json?all=1` lists `Id`, `Names`, `State`, `Status` (with `(healthy)`, `(unhealthy)` or `(health: starting)`) and `Labels`; `GET /v1.44/containers/<id>/json` gives `Config.Healthcheck.Test` |

The Contract Tests run their workload containers inside dind from the `busybox` image pinned under
`test_images` in [`upstreams.yaml`](upstreams.yaml), where Renovate bumps it; the harness starts only `upstreams`.
