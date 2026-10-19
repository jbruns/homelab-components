import dataclasses
import json
import re
import socket
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backup_coverage import exporter as exporter_module
from backup_coverage.exporter import Config, Exporter, collect, healthcheck, main, serve
from backup_coverage.stackback import Settings

FAKE_RESTIC = Path(__file__).with_name("fake_restic.py")

OLD = 1767225600  # 2026-01-01T00:00:00Z, before every snapshot
FUTURE = 4102444800  # 2100-01-01: a container created "now" for any test run


def volume_mount(name, destination):
    return {
        "Type": "volume",
        "Name": name,
        "Source": f"/var/lib/docker/volumes/{name}/_data",
        "Destination": destination,
    }


def container(labels, mounts=(), created=OLD, image="example/app:1", id="c"):
    return {"Id": id, "Image": image, "Created": created, "Labels": labels, "Mounts": list(mounts)}


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
    container(compose("nc", "pg", **{"stack-back.postgres": "true"}), created=FUTURE, id="pg"),
]
POSTGRES_INSPECT = {"Id": "pg", "Config": {"Env": ["PATH=/usr/bin", "POSTGRES_DB=nextcloud"]}}

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
    "/volumes/data",
    "/volumes/media/sonarr/config",
    "/volumes/nc/mariadb/etc/mysql/conf.d",
    "/volumes/p/s/keep",
    "/volumes/q/t/data",
]

HEADER = """\
# HELP backup_coverage_expected Path stack-back is configured to back up.
# TYPE backup_coverage_expected gauge
# HELP backup_coverage_due 1 once the path should be in a backup (see the backup-coverage README).
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
SOLO = 'kind="volume",project="",service="",path="/volumes/data"'
PG = 'kind="database",project="nc",service="pg",path="/databases/nc/pg/nextcloud.sql"'

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


# home-ops runs stack-back with these two on; the characterized script assumed both.
HOME_OPS = Settings.from_env({"EXCLUDE_BIND_MOUNTS": "true", "INCLUDE_ALL_COMPOSE_PROJECTS": "true"})


class FakeDocker:
    """A Docker Engine API stand-in serving fixed answers, on TCP or a Unix socket."""

    def __init__(self, server, host):
        self.server, self.host = server, host
        self.answers = {
            "/v1.44/volumes": {"Volumes": VOLUMES},
            "/v1.44/containers/json?all=1": CONTAINERS,
            "/v1.44/system/df?type=volume": DISK_USAGE,
            "/v1.44/containers/pg/json": POSTGRES_INSPECT,
        }
        self.requests = []
        server.fake = self


class DockerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        fake = self.server.fake
        fake.requests.append(self.path)
        if self.path not in fake.answers:
            self.send_error(404)
            return
        body = json.dumps(fake.answers[self.path]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        return "docker-client"

    def log_message(self, *args):
        pass


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def docker():
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), DockerHandler))
    yield FakeDocker(server, f"tcp://127.0.0.1:{server.server_port}")
    server.shutdown()


class FakeRestic:
    def __init__(self, path):
        self.path = path
        self.answers = {"repository": "/backups", "snapshots": SNAPSHOTS, "ls": LISTING}

    @property
    def command(self):
        self.path.write_text(json.dumps(self.answers))
        return (sys.executable, str(FAKE_RESTIC), str(self.path))

    @property
    def calls(self):
        calls = Path(f"{self.path}.calls")
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


@pytest.fixture
def restic(tmp_path):
    return FakeRestic(tmp_path / "restic.json")


def config(docker, restic, **overrides):
    values = {"restic_repository": "/backups", "docker_host": docker.host, "stack_back": HOME_OPS, **overrides}
    return Config(**values, restic_command=restic.command)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def split(metrics):
    assert metrics.startswith(HEADER)
    body = metrics[len(HEADER) :]
    series, footer_and_last = body.split(FOOTER)
    assert LAST_SUCCESS.fullmatch(footer_and_last)
    return series


def test_reports_every_path_stack_back_should_back_up(docker, restic):
    assert split(collect(config(docker, restic))) == EXPECTED_SERIES


def test_lists_the_expected_volume_paths_in_the_newest_volumes_snapshot_once(docker, restic):
    collect(config(docker, restic))
    read_only = ["--no-lock", "--json"]
    assert restic.calls == [[*read_only, "snapshots"], [*read_only, "ls", "new", *EXPECTED_PATHS]]


def test_without_a_volumes_snapshot_nothing_is_due_or_present(docker, restic):
    restic.answers["snapshots"] = []
    series = split(collect(config(docker, restic)))
    assert f"backup_coverage_due{{{SONARR}}} 0\n" in series
    assert f"backup_coverage_present{{{SONARR}}} 0\n" in series
    assert f"backup_coverage_snapshot_timestamp{{{SONARR}}} 0\n" in series
    assert f"backup_coverage_present{{{MARIADB_DB}}} 0\n" in series
    assert f"backup_coverage_due{{{MARIADB_DB}}} 1\n" in series
    assert [call[-1] for call in restic.calls] == ["snapshots"]


def test_a_database_is_due_once_its_container_is_older_than_the_configured_age(docker, restic):
    series = split(collect(config(docker, restic, database_due_after=200 * 365 * 86400)))
    assert f"backup_coverage_due{{{MARIADB_DB}}} 0\n" in series


def test_asks_the_docker_engine_api_at_a_pinned_version(docker, restic):
    collect(config(docker, restic))
    assert docker.requests and all(path.startswith("/v1.44/") for path in docker.requests)


def test_reaches_docker_over_a_unix_socket(tmp_path, monkeypatch, restic):
    monkeypatch.chdir(tmp_path)  # a relative socket path stays within the Unix socket path limit
    server = start(type("UnixServer", (socketserver.ThreadingUnixStreamServer,), {})("d.sock", DockerHandler))
    try:
        unix_docker = FakeDocker(server, "unix://d.sock")
        assert split(collect(config(unix_docker, restic))) == EXPECTED_SERIES
    finally:
        server.shutdown()


@pytest.mark.parametrize("failure", ["docker", "restic"])
def test_a_failed_check_raises_naming_the_upstream(docker, restic, failure):
    if failure == "docker":
        del docker.answers["/v1.44/volumes"]
    else:
        restic.answers["fail"] = True
    with pytest.raises(RuntimeError, match=f"^{failure}.*(HTTP 404|exited 1: Fatal: unable to open repository)"):
        collect(config(docker, restic))


SECRET_REPOSITORY = "s3:https://AKIAEXAMPLE:s3cr3t@minio.example/backups"


@pytest.mark.parametrize("failure", ["fail", "hang"])
def test_a_failed_restic_call_never_logs_the_repository(docker, restic, monkeypatch, capsys, failure):
    monkeypatch.setattr(exporter_module, "RESTIC_TIMEOUT", 1)
    restic.answers.update({"repository": SECRET_REPOSITORY, failure: True})
    Exporter(config(docker, restic, restic_repository=SECRET_REPOSITORY)).refresh()
    out, err = capsys.readouterr()
    assert "check failed: RuntimeError: restic snapshots " in err
    assert "s3cr3t" not in out + err and "AKIAEXAMPLE" not in out + err


def test_an_unreachable_docker_raises_naming_its_host(tmp_path, docker, restic):
    host = f"unix://{tmp_path}/missing.sock"
    with pytest.raises(RuntimeError, match=f"^docker GET /containers/json\\?all=1 on {host}: "):
        collect(dataclasses.replace(config(docker, restic), docker_host=host))


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.read().decode(), response.headers["Content-Type"]


UP_HEADER = (
    "# HELP backup_coverage_up 1 if the last check reached Docker and restic.\n# TYPE backup_coverage_up gauge\n"
)


def test_serves_the_last_good_check_and_keeps_it_when_a_check_fails(docker, restic):
    exporter = Exporter(config(docker, restic, port=free_port()))
    server = start(serve(exporter))
    try:
        assert get(exporter.config.port, "/metrics") == ("", "text/plain; version=0.0.4")
        exporter.refresh()
        good, _ = get(exporter.config.port, "/metrics")
        checked, up = good.split(UP_HEADER)
        assert split(checked) == EXPECTED_SERIES and up == "backup_coverage_up 1\n"
        del docker.answers["/v1.44/containers/json?all=1"]
        exporter.refresh()
        assert get(exporter.config.port, "/metrics")[0] == f"{checked}{UP_HEADER}backup_coverage_up 0\n"
        with pytest.raises(urllib.error.HTTPError) as missing:
            get(exporter.config.port, "/")
        assert missing.value.code == 404
    finally:
        server.shutdown()


def test_a_first_check_that_fails_serves_only_up_0(docker, restic):
    del docker.answers["/v1.44/volumes"]
    exporter = Exporter(config(docker, restic))
    exporter.refresh()
    assert exporter.metrics() == f"{UP_HEADER}backup_coverage_up 0\n"


def test_healthcheck_passes_only_while_serving_and_leaves_the_upstreams_alone(docker, restic):
    exporter = Exporter(config(docker, restic, port=free_port()))
    assert healthcheck(exporter.config.port) == 1
    server = start(serve(exporter))
    try:
        assert healthcheck(exporter.config.port) == 0
        assert main(["healthcheck"], {"PORT": str(exporter.config.port)}) == 0
    finally:
        server.shutdown()
    assert docker.requests == [] and restic.calls == []


def test_configuration_comes_from_the_environment_with_documented_defaults():
    assert Config.from_env({"RESTIC_REPOSITORY": "/r"}) == Config(
        restic_repository="/r",
        docker_host="unix:///var/run/docker.sock",
        port=8002,
        refresh_interval=3600.0,
        database_due_after=93600.0,
        stack_back=Settings(),
    )
    environ = {
        "RESTIC_REPOSITORY": "s3:example/backups",
        "DOCKER_HOST": "tcp://socket-proxy:2375",
        "PORT": "9200",
        "REFRESH_INTERVAL": "60",
        "DATABASE_DUE_AFTER": "7200",
        "EXCLUDE_BIND_MOUNTS": "true",
    }
    assert Config.from_env(environ) == Config(
        restic_repository="s3:example/backups",
        docker_host="tcp://socket-proxy:2375",
        port=9200,
        refresh_interval=60.0,
        database_due_after=7200.0,
        stack_back=Settings(exclude_bind_mounts=True),
    )


def test_refuses_to_start_without_a_restic_repository(capsys):
    assert main([], {}) == 2
    assert "RESTIC_REPOSITORY is required" in capsys.readouterr().err
