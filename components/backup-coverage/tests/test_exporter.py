import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from backup_coverage import exporter

OLD = 1767225600  # 2026-01-01T00:00:00Z, before every snapshot
FUTURE = 4102444800  # 2100-01-01: a container created "now" for any test run


def volume_mount(name, destination):
    return {"Type": "volume", "Name": name, "Source": f"/var/lib/docker/volumes/{name}/_data", "Destination": destination}


def container(labels, mounts=(), created=OLD, image="example/app:1"):
    return {"Image": image, "Created": created, "Labels": labels, "Mounts": list(mounts)}


def compose(project, service, **labels):
    return {"com.docker.compose.project": project, "com.docker.compose.service": service, **labels}


CONTAINERS = [
    container(
        compose("media", "sonarr", **{"stack-back.volumes": "true"}),
        [
            volume_mount("media_sonarr_config", "/config"),
            {"Type": "bind", "Source": "/srv/media", "Destination": "/media"},
        ],
    ),
    container(
        compose("nc", "mariadb", **{"stack-back.mariadb": "true", "stack-back.volumes": "true"}),
        [volume_mount("nc_db", "/var/lib/mysql"), volume_mount("nc_conf", "/etc/mysql/conf.d")],
    ),
    container(
        compose("p", "s", **{"stack-back.volumes": "true", "stack-back.volumes.include": "keep"}),
        [volume_mount("p_keep_vol", "/keep"), volume_mount("p_drop_vol", "/drop")],
    ),
    container(
        compose("q", "t", **{"stack-back.volumes": "1", "stack-back.volumes.exclude": "cache"}),
        [volume_mount("q_cache", "/cache"), volume_mount("q_data", "/data")],
    ),
    container(
        compose("ops", "stack-back", **{"stack-back.volumes": "true"}),
        [volume_mount("ops_x", "/x")],
        image="ghcr.io/lawndoc/stack-back:v1.5.4",
    ),
    container(
        compose("media", "sonarr", **{"stack-back.volumes": "true", "com.docker.compose.oneoff": "True"}),
        [volume_mount("media_sonarr_config", "/config")],
    ),
    container(compose("media", "plain"), [volume_mount("media_plain", "/data")]),
    container({"stack-back.volumes": "true"}, [volume_mount("solo_data", "/data")]),
    container(compose("nc", "pg", **{"stack-back.postgres": "true"}), created=FUTURE),
]

VOLUMES = [
    {"Name": name, "CreatedAt": "2026-01-01T00:00:00Z"}
    for name in ("media_sonarr_config", "nc_db", "nc_conf", "p_keep_vol", "p_drop_vol", "q_cache")
] + [{"Name": "q_data", "CreatedAt": "2026-10-18T00:00:00Z"}]  # recreated after the newest snapshot

DISK_USAGE = {
    "Volumes": [
        {"Name": "media_sonarr_config", "UsageData": {"Size": 1234}},
        {"Name": "nc_conf", "UsageData": {"Size": 0}},
        {"Name": "p_keep_vol", "UsageData": None},
        {"Name": "q_data", "UsageData": {"Size": 99}},
    ]
}

SNAPSHOTS = [
    {"id": "old", "time": "2026-10-16T02:00:00Z", "paths": ["/volumes"]},
    {"id": "new", "time": "2026-10-17T02:00:00Z", "paths": ["/volumes"]},
    {"id": "d1", "time": "2026-10-16T02:05:00Z", "paths": ["/databases/nc/mariadb/all_databases.sql"]},
    {"id": "d2", "time": "2026-10-17T02:05:00Z", "paths": ["/databases/nc/mariadb/all_databases.sql"]},
    {"id": "o", "time": "2026-10-18T02:00:00Z", "paths": ["/etc"]},
]

LISTING = [
    {"struct_type": "snapshot", "id": "new", "paths": ["/volumes"]},
    {"struct_type": "node", "path": "/volumes/media/sonarr/config"},
    {"struct_type": "node", "path": "/volumes/media/sonarr/config/a"},
    {"struct_type": "node", "path": "/volumes/media/sonarr/config/b"},
    {"struct_type": "node", "path": "/volumes/nc/mariadb/etc/mysql/conf.d"},
    {"struct_type": "node", "path": "/volumes/q/t/data"},
    {"struct_type": "node", "path": "/volumes/q/t/data/x"},
]

EXPECTED_PATHS = [
    "/volumes//data",
    "/volumes/media/sonarr/config",
    "/volumes/nc/mariadb/etc/mysql/conf.d",
    "/volumes/p/s/keep",
    "/volumes/q/t/data",
]

HEADER = """\
# HELP backup_coverage_expected Path stack-back is configured to back up.
# TYPE backup_coverage_expected gauge
# HELP backup_coverage_due 1 once the path should be in a backup (see backup_coverage.py).
# TYPE backup_coverage_due gauge
# HELP backup_coverage_present 1 if the path is in its newest snapshot.
# TYPE backup_coverage_present gauge
# HELP backup_coverage_entries Direct children of a volume path in the newest /volumes snapshot.
# TYPE backup_coverage_entries gauge
# HELP backup_coverage_volume_bytes Live size of the volume now (-1 unknown); 0 means an empty snapshot dir is expected.
# TYPE backup_coverage_volume_bytes gauge
# HELP backup_coverage_snapshot_timestamp Newest snapshot holding the path (volumes: newest /volumes snapshot).
# TYPE backup_coverage_snapshot_timestamp gauge
"""

FOOTER = """\
# HELP backup_coverage_last_success_timestamp When the last check completed.
# TYPE backup_coverage_last_success_timestamp gauge
"""

SONARR = 'kind="volume",project="media",service="sonarr",path="/volumes/media/sonarr/config"'
MARIADB_DB = 'kind="database",project="nc",service="mariadb",path="/databases/nc/mariadb/all_databases.sql"'
MARIADB_CONF = 'kind="volume",project="nc",service="mariadb",path="/volumes/nc/mariadb/etc/mysql/conf.d"'
KEEP = 'kind="volume",project="p",service="s",path="/volumes/p/s/keep"'
DATA = 'kind="volume",project="q",service="t",path="/volumes/q/t/data"'
SOLO = 'kind="volume",project="",service="",path="/volumes//data"'
PG = 'kind="database",project="nc",service="pg",path="/databases/nc/pg/all_databases.sql"'

EXPECTED_SERIES = f"""\
backup_coverage_entries{{{SONARR}}} 2
backup_coverage_volume_bytes{{{SONARR}}} 1234
backup_coverage_expected{{{SONARR}}} 1
backup_coverage_due{{{SONARR}}} 1
backup_coverage_present{{{SONARR}}} 1
backup_coverage_snapshot_timestamp{{{SONARR}}} 1792202400.0
backup_coverage_expected{{{MARIADB_DB}}} 1
backup_coverage_due{{{MARIADB_DB}}} 1
backup_coverage_present{{{MARIADB_DB}}} 1
backup_coverage_snapshot_timestamp{{{MARIADB_DB}}} 1792202700.0
backup_coverage_entries{{{MARIADB_CONF}}} 0
backup_coverage_volume_bytes{{{MARIADB_CONF}}} 0
backup_coverage_expected{{{MARIADB_CONF}}} 1
backup_coverage_due{{{MARIADB_CONF}}} 1
backup_coverage_present{{{MARIADB_CONF}}} 1
backup_coverage_snapshot_timestamp{{{MARIADB_CONF}}} 1792202400.0
backup_coverage_entries{{{KEEP}}} 0
backup_coverage_volume_bytes{{{KEEP}}} -1
backup_coverage_expected{{{KEEP}}} 1
backup_coverage_due{{{KEEP}}} 1
backup_coverage_present{{{KEEP}}} 0
backup_coverage_snapshot_timestamp{{{KEEP}}} 1792202400.0
backup_coverage_entries{{{DATA}}} 1
backup_coverage_volume_bytes{{{DATA}}} 99
backup_coverage_expected{{{DATA}}} 1
backup_coverage_due{{{DATA}}} 0
backup_coverage_present{{{DATA}}} 1
backup_coverage_snapshot_timestamp{{{DATA}}} 1792202400.0
backup_coverage_entries{{{SOLO}}} 0
backup_coverage_volume_bytes{{{SOLO}}} -1
backup_coverage_expected{{{SOLO}}} 1
backup_coverage_due{{{SOLO}}} 1
backup_coverage_present{{{SOLO}}} 0
backup_coverage_snapshot_timestamp{{{SOLO}}} 1792202400.0
backup_coverage_expected{{{PG}}} 1
backup_coverage_due{{{PG}}} 0
backup_coverage_present{{{PG}}} 0
backup_coverage_snapshot_timestamp{{{PG}}} 0
"""

LAST_SUCCESS = re.compile(r"backup_coverage_last_success_timestamp \d+\.\d+\n\Z")


@pytest.fixture
def upstreams(monkeypatch):
    """The legacy script's Docker and restic calls, answered from the fixtures above."""
    state = {"snapshots": SNAPSHOTS, "restic_calls": []}
    docker_answers = {
        "/volumes": {"Volumes": VOLUMES},
        "/containers/json?all=1": CONTAINERS,
        "/system/df?type=volume": DISK_USAGE,
    }

    def docker(path, timeout=30):
        return docker_answers[path]

    def restic(*args):
        state["restic_calls"].append(args)
        if args[0] == "snapshots":
            return [state["snapshots"]]
        return LISTING

    monkeypatch.setattr(exporter, "docker", docker)
    monkeypatch.setattr(exporter, "restic", restic)
    return state


def split(metrics):
    assert metrics.startswith(HEADER)
    body = metrics[len(HEADER) :]
    series, footer_and_last = body.split(FOOTER)
    assert LAST_SUCCESS.fullmatch(footer_and_last)
    return series


def test_reports_every_path_stack_back_should_back_up(upstreams):
    assert split(exporter.collect()) == EXPECTED_SERIES


def test_lists_the_expected_volume_paths_in_the_newest_volumes_snapshot_once(upstreams):
    exporter.collect()
    assert upstreams["restic_calls"] == [("snapshots",), ("ls", "new", *EXPECTED_PATHS)]


def test_without_a_volumes_snapshot_nothing_is_due_or_present(upstreams):
    upstreams["snapshots"] = []
    series = split(exporter.collect())
    assert f"backup_coverage_due{{{SONARR}}} 0\n" in series
    assert f"backup_coverage_present{{{SONARR}}} 0\n" in series
    assert f"backup_coverage_snapshot_timestamp{{{SONARR}}} 0\n" in series
    assert f"backup_coverage_present{{{MARIADB_DB}}} 0\n" in series
    assert f"backup_coverage_due{{{MARIADB_DB}}} 1\n" in series
    assert upstreams["restic_calls"] == [("snapshots",)]


def test_serves_metrics_only_on_the_metrics_path(monkeypatch):
    monkeypatch.setattr(exporter, "metrics", "backup_coverage_expected 1\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), exporter.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as response:
            assert response.read() == b"backup_coverage_expected 1\n"
            assert response.headers["Content-Type"] == "text/plain; version=0.0.4"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/", timeout=5)
        assert missing.value.code == 404
    finally:
        server.shutdown()
