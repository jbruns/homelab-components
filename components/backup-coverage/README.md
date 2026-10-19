# backup-coverage

A Prometheus exporter that checks every path [stack-back](https://github.com/lawndoc/stack-back) should back up is
in the newest restic snapshot. It reads the containers, volumes and volume sizes from the Docker Engine API, works
out the paths stack-back v1.5.4 would back up from their `stack-back.*` labels and stack-back's own settings, then
looks for each path in the restic repository with `restic snapshots` and `restic ls` (read-only, `--no-lock`).

- A **volume** path (`/volumes/[project/]service/destination`) is present when it is in the newest `/volumes`
  snapshot. It is due once both its container and its Docker volume predate that snapshot, so a recreate waits for
  the next backup. Its direct child count and the volume's live size tell an expected empty directory (an empty
  volume) from a lost one.
- A volume or bind mount shared by several containers is expected once, as stack-back backs it up: under the last of
  them in Docker's container list (the oldest).
- A **database** path (`/databases/[project/]service/<dump>.sql`) is present when any snapshot holds it. It is due
  once its container is older than `DATABASE_DUE_AFTER`. A container is a database when its `stack-back.mariadb`,
  `stack-back.mysql` or `stack-back.postgres` label is true or, with `AUTO_BACKUP_ALL`, when its image starts with
  `mariadb`, `mysql` or `postgres` and that label is not false; its data directory is then left out of the volume
  paths. MariaDB and MySQL dump to `all_databases.sql`, Postgres to `<POSTGRES_DB>.sql`, as stack-back names them.

It checks every `REFRESH_INTERVAL` and serves the last good result on `/metrics`, which stays empty until the first
check ends. A failed check is logged to stderr, sets `backup_coverage_up` to 0 and leaves the last result, and its
`backup_coverage_last_success_timestamp`, in place.

Unlike stack-back, it counts stopped containers too, so a stopped service whose data is not being backed up still
shows as missing. A container outside Compose has no service, so its paths skip that part (`/volumes/data`);
stack-back v1.5.4 fails on such a container.

## Why

stack-back and [restic-exporter](https://github.com/ngosang/restic-exporter) report whether backups run and how fresh
each snapshot is, but neither checks what is in them: a volume whose label was dropped, a mount stack-back skips, or
a database dump that stopped landing all look like a healthy backup. Nothing upstream or in the community compares
what should be backed up with what the newest snapshot holds.

## Retire when

stack-back, restic-exporter or another maintained exporter reports, per labelled volume and database, whether it
is in the newest snapshot.

## Configuration

Every environment variable the code reads, with its default (`required` when there is none). CI fails if this
table and the code disagree. The stack-back settings use stack-back's names, meanings and defaults, so the
Component can share stack-back's env file; set them as the stack-back container has them.

| Variable | Default | Description |
|---|---|---|
| `RESTIC_REPOSITORY` | required | The restic repository stack-back backs up to. |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker Engine API, `unix://` socket path or `tcp://host:port`. |
| `PORT` | `8002` | Port serving `/metrics` and `/healthz`. |
| `REFRESH_INTERVAL` | `3600` | Seconds between checks. |
| `DATABASE_DUE_AFTER` | `93600` | Seconds after a database container's creation before its dump is due. |
| `EXCLUDE_BIND_MOUNTS` | `false` | stack-back setting: back up named volumes only. |
| `INCLUDE_ALL_COMPOSE_PROJECTS` | unset | stack-back setting: any value, even `false`, backs up every Compose project (and turns on `INCLUDE_PROJECT_NAME` when `true`); unset, only stack-back's own project. |
| `INCLUDE_PROJECT_NAME` | `false` | stack-back setting: put the Compose project in each path. |
| `AUTO_BACKUP_ALL` | `false` | stack-back setting: back up every volume unless labelled `stack-back.volumes=false`. |
| `INCLUDE_ALL_VOLUMES` | `false` | stack-back's older name for `AUTO_BACKUP_ALL`. |

restic itself reads the rest of its environment: give it the repository password with `RESTIC_PASSWORD_FILE` (or
`RESTIC_PASSWORD`), and any backend credentials the repository needs. The repository reaches restic through its
environment, never its command line. A failed restic call is logged with its exit status and known phrases only
(such as `wrong password or no key found`), never restic's own output, which can echo the repository URL. Its cache goes to `RESTIC_CACHE_DIR`, or
under `$HOME/.cache`, which must be writable.

The process must be able to read the Docker socket and the repository. A local repository whose files restic wrote
as `0400 root` needs the container to run as root (`user: "0:0"`).

## Metrics

Every per-path metric has the labels `kind` (`volume` or `database`), `project`, `service` and `path`.

| Metric | Description |
|---|---|
| `backup_coverage_expected` | 1 for each path stack-back is configured to back up. |
| `backup_coverage_due` | 1 once the path should be in a backup (see above). |
| `backup_coverage_present` | 1 if the path is in its newest snapshot. |
| `backup_coverage_entries` | Volumes only: direct children of the path in the newest `/volumes` snapshot. |
| `backup_coverage_volume_bytes` | Volumes only: live size of the Docker volume, -1 when unknown (a bind mount, or Docker did not report it). |
| `backup_coverage_snapshot_timestamp` | Newest snapshot holding the path (volumes: the newest `/volumes` snapshot), 0 when none. |
| `backup_coverage_last_success_timestamp` | When the last check completed; no labels. |
| `backup_coverage_up` | 1 if the last check reached Docker and restic, 0 if it failed; no labels. |

An alert on `backup_coverage_due == 1 and backup_coverage_present == 0` finds missing paths; one on
`backup_coverage_entries == 0 and backup_coverage_volume_bytes > 0` finds volumes backed up empty.

## Healthcheck

`healthcheck` exits 0 when the exporter answers on its own `/healthz`, which never calls Docker or restic. The image
declares it as its `HEALTHCHECK`; Compose should declare the same one (home-ops Baseline):

```yaml
healthcheck:
  test: ["CMD", "/usr/bin/python3", "-m", "backup_coverage", "healthcheck"]
```

## Upstreams

| Upstream | Pinned in | Contract |
|---|---|---|
| Docker Engine API | [`upstreams.yaml`](upstreams.yaml), API version `v1.44` in `exporter.DOCKER_API` | `/containers/json?all=1`, `/containers/{id}/json`, `/volumes` and `/system/df?type=volume` answer with the fields the check reads |
| restic | [`upstreams.yaml`](upstreams.yaml) and the [`Dockerfile`](Dockerfile), which ships the restic binary from the same image; move the two pins together | `snapshots --json` and `ls --json` on a real repository report the paths stack-back wrote |
| stack-back | [`upstreams.yaml`](upstreams.yaml) | stack-back's own code picks the same paths as this Component, across its settings |
