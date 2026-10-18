# _template

> **Copy-from template.** Not a real Component and never released (names starting with `_` cannot be tagged
> for release). CI runs it on every change to the shared harness so the pipeline stays proven. Start a new
> Component by copying this folder; see [CONTRIBUTING.md](../../CONTRIBUTING.md).

A Prometheus exporter for a [traefik/whoami](https://github.com/traefik/whoami) upstream: it reports whether
the upstream's `/api` answered and the hostname it returned.

## Why

State the gap: what the Component does that no upstream or community option covers, and which options were
checked. Here: the template needs a small real upstream to prove the Contract Test and Canary pipeline.

## Retire when

State the Retirement Condition: the upstream or community capability whose arrival makes this Component
unnecessary, specific enough to check. Here: when the repo has a real Component small enough to serve as the
pipeline's proof.

## Configuration

Every environment variable the code reads, with its default (`required` when there is none). CI fails if this
table and the code disagree.

| Variable | Default | Description |
|---|---|---|
| `TEMPLATE_UPSTREAM_URL` | `http://whoami` | Base URL of the whoami upstream. |
| `PORT` | `9100` | Port serving `/metrics`. |
| `TIMEOUT` | `10` | Seconds to wait for the upstream, and for the healthcheck. |

## Metrics

| Metric | Description |
|---|---|
| `template_upstream_up` | 1 if the upstream API answered. |
| `template_upstream_info{hostname}` | The hostname the upstream reported. |

## Healthcheck

`healthcheck` exits 0 when the exporter answers on its own `/healthz`, which never calls the upstream. The image
declares it as its `HEALTHCHECK`; Compose should declare the same one (home-ops Baseline):

```yaml
healthcheck:
  test: ["CMD", "/usr/bin/python3", "-m", "template_exporter", "healthcheck"]
```

## Upstreams

| Upstream | Pinned in | Contract |
|---|---|---|
| whoami | [`upstreams.yaml`](upstreams.yaml) | `GET /api` returns JSON with a `hostname` |
