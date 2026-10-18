# arr-queue-exporter

A Prometheus exporter for the queue of one [Sonarr](https://sonarr.tv) or [Radarr](https://radarr.video)
instance. On each scrape it reads `/api/v3/queue`, including items the app could not match to a series or
movie, and reports the queue by tracked download state and status, plus one series for each item that needs
attention: downloaded but not imported (`importPending`, `importBlocked`), failed and waiting for the app
(`failedPending`), or flagged `warning` or `error`. An alert on `arr_queue_item_stuck` with a `for` then measures
how long that one item has been stuck, whichever stuck state it moves through.

One instance watches one app. Run one per Sonarr or Radarr, for example as a sidecar next to each, and give
each its own `ARR_APP`.

## Why

[exportarr](https://github.com/onedr0p/exportarr) is the community exporter for the *arr apps, but its
`sonarr_queue_total` / `radarr_queue_total` cannot drive a per-item alert: it emits one sample, labelled with
only the last item's state and status, so a stuck item disappears from the metric as soon as another item
joins the queue. Neither app exports Prometheus metrics itself, and their own health checks do not report a
queue item stuck on import. Nothing upstream or in the community reports each stuck queue item with its title
and message.

## Retire when

exportarr (or Sonarr and Radarr themselves) exports a series per queue item, or a count per tracked download
state and status, that keeps every item rather than only the last one, with the item's title. Then alert on
that instead and delete this Component.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ARR_URL` | `required` | Base URL of the Sonarr or Radarr instance, e.g. `http://sonarr:8989`; include the URL base if one is set. |
| `ARR_API_KEY` | `required` | The app's API key (Settings > General > Security). Never logged. |
| `ARR_APP` | `required` | Value of the `app` label on every series, e.g. `sonarr` or `radarr`. Unique per instance. |
| `PORT` | `9713` | Port serving `/metrics` and `/healthz`. |
| `TIMEOUT` | `10` | Seconds to wait for the queue API on each scrape. |

## Metrics

Every series carries `app` (from `ARR_APP`). Labels taken from the queue (`title`, `message`) are cut to 200
characters.

| Metric | Description |
|---|---|
| `arr_queue_up{app}` | 1 if the queue API answered; 0 if it did not (unreachable, timeout, rejected key), with no other series. |
| `arr_queue_items{app,state,status}` | Queue items by `trackedDownloadState` and `trackedDownloadStatus` (empty for unmatched items that lack them). |
| `arr_queue_item_stuck{app,id,title}` | 1 for each queue item that needs attention. |
| `arr_queue_item_info{app,id,title,state,status,message}` | A stuck item's state, status and first status message (or error message). |

Example alerts:

```yaml
- alert: ArrImportStuck
  expr: arr_queue_item_stuck == 1
  for: 2h
- alert: ArrQueueDown
  expr: arr_queue_up == 0
  for: 10m
```

## Healthcheck

`healthcheck` exits 0 when the exporter answers on its own `/healthz`, which never calls the app. The image
declares it as its `HEALTHCHECK`; Compose should declare the same one (home-ops Baseline):

```yaml
healthcheck:
  test: ["CMD", "/usr/bin/python3", "-m", "arr_queue_exporter", "healthcheck"]
```

## Upstreams

| Upstream | Pinned in | Contract |
|---|---|---|
| Sonarr | [`upstreams.yaml`](upstreams.yaml) | `GET /api/v3/queue` with `X-Api-Key`, `pageSize` and `includeUnknownSeriesItems` lists an unmatched download as `importBlocked`/`warning` with `id`, `title`, `statusMessages[].messages` and `errorMessage`; a wrong key is refused |
| Radarr | [`upstreams.yaml`](upstreams.yaml) | the same, with `includeUnknownMovieItems` |

The Contract Tests give each app a Usenet Blackhole download client and drop a file it cannot match into the
watch folder (with `docker exec`), so the queue holds a real stuck record without a download client container.
