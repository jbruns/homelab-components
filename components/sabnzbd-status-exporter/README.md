# sabnzbd-status-exporter

A Prometheus exporter for the health of each Usenet provider (server) configured in
[SABnzbd](https://sabnzbd.org). Each scrape reads SABnzbd's `api?mode=status` once and reports, per server,
whether it is enabled, its priority and connections, and the error or warning SABnzbd last hit on it (e.g.
failed authentication, too many connections, a host that does not resolve).

## Why

[exportarr](https://github.com/onedr0p/exportarr)'s `sabnzbd` mode covers the queue, history, quota and disk,
and its per-server metrics are download statistics (bytes and articles), not state. Nothing exposes the
per-server error and warning that SABnzbd shows in its UI, so a provider that starts rejecting logins or
stops resolving goes unnoticed until downloads fail. SABnzbd has no built-in Prometheus endpoint, and no
community exporter reads `api?mode=status`.

## Retire when

exportarr (or SABnzbd itself) exposes each server's enabled state and its current error and warning from
`api?mode=status` as metrics. Then point alerts and dashboards at those metrics and delete this Component.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SABNZBD_URL` | `http://sabnzbd:8080` | Base URL of SABnzbd, including any URL base (e.g. `http://host:8080/sabnzbd`). |
| `SABNZBD_API_KEY` | `required` | SABnzbd API key (Config → General → Security). Never logged. |
| `PORT` | `9712` | Port serving `/metrics` and `/healthz`. |
| `TIMEOUT` | `10` | Seconds to wait for the status API. |

## Metrics

All gauges. `server` is the server's name in SABnzbd. Only enabled servers are listed by SABnzbd, so a disabled
server has no samples.

| Metric | Description |
|---|---|
| `sabnzbd_status_up` | 1 if the status API answered; 0 on any failure (unreachable, wrong API key, bad JSON). |
| `sabnzbd_status_scrape_duration_seconds` | Time taken to read the status API. |
| `sabnzbd_usenet_server_active{server}` | 1 if the server is enabled in SABnzbd. |
| `sabnzbd_usenet_server_optional{server}` | 1 if the server is optional (a failure does not fail the download). |
| `sabnzbd_usenet_server_priority{server}` | Server priority; 0 is tried first. |
| `sabnzbd_usenet_server_connections{server}` | Configured connections. |
| `sabnzbd_usenet_server_error{server}` | 1 while SABnzbd reports an error for the server. |
| `sabnzbd_usenet_server_error_info{server,error}` | The server's current error (first 200 characters); present only while there is one. |
| `sabnzbd_usenet_server_warning{server}` | 1 while SABnzbd reports a warning for the server. |
| `sabnzbd_usenet_server_warning_info{server,warning}` | The server's current warning (first 200 characters); present only while there is one. |

An alert on a failing provider that is in use:

```promql
sabnzbd_usenet_server_error == 1 and on (server) sabnzbd_usenet_server_active == 1
```

## Healthcheck

`healthcheck` exits 0 when the exporter answers on its own `/healthz`, which never calls SABnzbd. The image
declares it as its `HEALTHCHECK`; Compose should declare the same one (home-ops Baseline):

```yaml
healthcheck:
  test: ["CMD", "/usr/bin/python3", "-m", "sabnzbd_status_exporter", "healthcheck"]
```

## Upstreams

| Upstream | Pinned in | Contract |
|---|---|---|
| SABnzbd | [`upstreams.yaml`](upstreams.yaml) | `GET /api?mode=status&output=json&apikey=…` returns `status.servers[]` with `servername`, `serveractive`, `serveroptional`, `serverpriority`, `servertotalconn`, `servererror`, `serverwarning`; a wrong API key is refused |

The Contract Tests seed the API key through the pinned image's `SABNZBD__API_KEY`, configure servers on
`.invalid` hosts through `api?mode=set_config`, and queue a one-segment NZB so SABnzbd reports a real server
error.
