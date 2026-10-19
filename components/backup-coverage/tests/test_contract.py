"""Contract Tests: the pinned Docker Engine API, restic CLI and stack-back, run for real.

The Docker containers below are only created, never started, from an empty image imported into the pinned dind, so
the test needs no registry access from inside it. stack-back's own code (RunningContainers, Container) reads the
same containers, treated as running because this Component counts stopped containers too.
"""

import io
import json
import os
import subprocess
import tarfile
import urllib.request

import pytest

from backup_coverage.exporter import Config, collect, docker_get
from backup_coverage.stackback import Settings, backup_paths

APP_IMAGE = "contract/app:1"
STACK_BACK_IMAGE = "contract/stack-back:1"


def compose(project, service, **labels):
    return {"com.docker.compose.project": project, "com.docker.compose.service": service, **labels}


# name: (image, labels, binds). No container outside Compose: stack-back v1.5.4 raises on one it backs up.
CONTAINERS = {
    "stack-back": (STACK_BACK_IMAGE, compose("ops", "stack-back"), ["ops_stack_back:/cache"]),
    "app": (APP_IMAGE, compose("ops", "app", **{"stack-back.volumes": "true"}), ["ops_app:/data", "/etc:/host-etc"]),
    "db": (
        APP_IMAGE,
        compose("ops", "db", **{"stack-back.mariadb": "true", "stack-back.volumes": "true"}),
        ["ops_db:/var/lib/mysql", "ops_db_conf:/etc/mysql/conf.d"],
    ),
    "include": (
        APP_IMAGE,
        compose("ops", "inc", **{"stack-back.volumes": "true", "stack-back.volumes.include": "keep"}),
        ["ops_keep:/keep", "ops_drop:/drop"],
    ),
    "exclude": (
        APP_IMAGE,
        compose("ops", "exc", **{"stack-back.volumes": "true", "stack-back.volumes.exclude": "cache"}),
        ["ops_cache:/cache", "ops_exc:/data"],
    ),
    "auto": (APP_IMAGE, compose("ops", "auto"), ["ops_auto:/auto"]),
    "opt-out": (APP_IMAGE, compose("ops", "optout", **{"stack-back.volumes": "false"}), ["ops_optout:/x"]),
    "pg": (APP_IMAGE, compose("ops", "pg", **{"stack-back.postgres": "true"}), []),
    # Without a label, only AUTO_BACKUP_ALL dumps these, by image name, and skips their data dirs.
    "auto-pg": ("postgres:17", compose("ops", "autopg"), ["ops_autopg:/var/lib/postgresql/data", "ops_autopg_x:/x"]),
    "auto-mariadb": ("mariadb:11", compose("ops", "automaria"), ["ops_automaria:/var/lib/mysql"]),
    "auto-mysql": ("mysql:8.4", compose("ops", "automysql"), ["ops_automysql:/var/lib/mysql"]),
    "pg-opt-out": (
        "postgres:17",
        compose("ops", "pgoff", **{"stack-back.postgres": "false"}),
        ["ops_pgoff:/var/lib/postgresql/data"],
    ),
    "other-project": (APP_IMAGE, compose("media", "other", **{"stack-back.volumes": "true"}), ["media_other:/data"]),
    "one-off": (
        APP_IMAGE,
        compose("ops", "app", **{"stack-back.volumes": "true", "com.docker.compose.oneoff": "True"}),
        ["ops_app:/data"],
    ),
}

ENV = {"pg": ["POSTGRES_DB=app"], "auto-pg": ["POSTGRES_DB=auto"]}

# stack-back's own selection, given Docker's container inspect JSON on stdin and its container's id as argv[1].
STACK_BACK_SCRIPT = """
import json, socket, sys
from restic_compose_backup import containers, utils

data = json.load(sys.stdin)
for c in data:
    c["State"]["Running"] = True
utils.list_containers = lambda: data
socket.gethostname = lambda: sys.argv[1][:12]
paths = []
for c in containers.RunningContainers().containers_for_backup():
    if c.database_backup_enabled:
        paths.append(["database", str(c.instance.backup_destination_path())])
    if c.volume_backup_enabled:
        paths += [["volume", v["bind"]] for v in c.volumes_for_backup().values()]
print(json.dumps(paths))
"""

SETTINGS = {
    "stack-back defaults": {},
    "home-ops": {"EXCLUDE_BIND_MOUNTS": "true", "INCLUDE_ALL_COMPOSE_PROJECTS": "true"},
    "project names, auto backup": {"INCLUDE_PROJECT_NAME": "true", "AUTO_BACKUP_ALL": "true"},
}


def engine(url, method, path, body=None, content_type="application/json"):
    data = json.dumps(body).encode() if content_type == "application/json" and body is not None else body
    request = urllib.request.Request(f"{url}/v1.44{path}", data=data, method=method)
    request.add_header("Content-Type", content_type)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def empty_image_tar():
    buffer = io.BytesIO()
    tarfile.open(fileobj=buffer, mode="w").close()
    return buffer.getvalue()


_created = {}


@pytest.fixture
def engine_with_containers(upstream):
    """The pinned dind (started once per session by the harness) holding CONTAINERS, and their ids by name."""
    docker = upstream("docker")
    if docker.url not in _created:
        tar = empty_image_tar()
        engine(docker.url, "POST", "/images/create?fromSrc=-&repo=contract/app&tag=1", tar, "application/x-tar")
        for image in {image for image, _, _ in CONTAINERS.values()} - {APP_IMAGE}:
            repo, tag = image.split(":")
            engine(docker.url, "POST", f"/images/{APP_IMAGE}/tag?repo={repo}&tag={tag}")
        ids = {}
        for name, (image, labels, binds) in CONTAINERS.items():
            body = {"Image": image, "Cmd": ["none"], "Labels": labels, "Env": ENV.get(name, [])}
            body["HostConfig"] = {"Binds": binds}
            ids[name] = json.loads(engine(docker.url, "POST", f"/containers/create?name={name}", body))["Id"]
        _created[docker.url] = ids
    return docker, _created[docker.url]


def stack_back_paths(stack_back, docker, ids, environ):
    inspect = [json.loads(engine(docker.url, "GET", f"/containers/{i}/json")) for i in ids.values()]
    env = [arg for key, value in environ.items() for arg in ("--env", f"{key}={value}")]
    command = [*stack_back.command("--entrypoint", "python", *env), "-c", STACK_BACK_SCRIPT, ids["stack-back"]]
    return sorted(tuple(p) for p in json.loads(run(command, json.dumps(inspect))))


@pytest.mark.contract
@pytest.mark.parametrize("environ", SETTINGS.values(), ids=SETTINGS.keys())
def test_expects_the_paths_stack_back_backs_up(engine_with_containers, upstream, environ):
    docker, ids = engine_with_containers
    host = docker.url.replace("http://", "tcp://")
    containers = docker_get(host, "/containers/json?all=1")

    def inspect(container_id):
        return docker_get(host, f"/containers/{container_id}/json")["Config"]

    ours = sorted((p.kind, p.path) for p in backup_paths(containers, Settings.from_env(environ), inspect))
    assert ours == stack_back_paths(upstream("stack-back"), docker, ids, environ)
    assert ours  # the comparison means something only when stack-back backs something up


def run(command, stdin=None):
    result = subprocess.run(command, input=stdin, capture_output=True, text=True)
    if result.returncode:
        pytest.fail(f"{command[-1]!r}... exited {result.returncode}:\n{result.stderr}")
    return result.stdout


def restic_command(restic, repo, *options):
    """Runs restic as this user on `repo` mounted at /repo, with a throwaway cache."""
    user = f"{os.getuid()}:{os.getgid()}"
    cache = ("--tmpfs", "/cache", "--env", "RESTIC_CACHE_DIR=/cache")
    return restic.command("--volume", f"{repo}:/repo", "--user", user, *cache, *options)


def restic_run(restic, repo, *args, options=(), stdin=None):
    return run([*restic_command(restic, repo, *options), "--repo", "/repo", *args], stdin)


@pytest.mark.contract
def test_reports_coverage_from_a_real_repository(engine_with_containers, upstream, tmp_path):
    docker, _ = engine_with_containers
    restic = upstream("restic")
    repo, volumes = tmp_path / "repo", tmp_path / "volumes"
    repo.mkdir()
    (volumes / "ops/app/data").mkdir(parents=True)
    (volumes / "ops/app/data/file").write_text("x")
    (volumes / "ops/db/etc/mysql/conf.d").mkdir(parents=True)
    restic_run(restic, repo, "init")
    restic_run(restic, repo, "backup", "/volumes", options=("--volume", f"{volumes}:/volumes:ro"))
    dump = "/databases/ops/db/all_databases.sql"
    restic_run(restic, repo, "backup", "--stdin", "--stdin-filename", dump, stdin="-- dump\n")

    home_ops = Settings.from_env(SETTINGS["home-ops"])
    command = tuple(restic_command(restic, repo, "--env", "RESTIC_REPOSITORY"))  # passed on from the check
    config = Config(
        restic_repository="/repo",
        docker_host=docker.url.replace("http://", "tcp://"),
        stack_back=home_ops,
        restic_command=command,
    )
    metrics = collect(config)

    def series(name, kind, project, service, path):
        prefix = f'{name}{{kind="{kind}",project="{project}",service="{service}",path="{path}"}} '
        (line,) = [line for line in metrics.splitlines() if line.startswith(prefix)]
        return line.removeprefix(prefix)

    app = ("volume", "ops", "app", "/volumes/ops/app/data")
    conf = ("volume", "ops", "db", "/volumes/ops/db/etc/mysql/conf.d")
    keep = ("volume", "ops", "inc", "/volumes/ops/inc/keep")
    db = ("database", "ops", "db", dump)
    pg = ("database", "ops", "pg", "/databases/ops/pg/app.sql")

    assert [series("backup_coverage_present", *p) for p in (app, conf, keep, db, pg)] == ["1", "1", "0", "1", "0"]
    assert [series("backup_coverage_entries", *p) for p in (app, conf, keep)] == ["1", "0", "0"]
    # Created before the snapshot, so due; databases wait DATABASE_DUE_AFTER after their container's creation.
    assert [series("backup_coverage_due", *p) for p in (app, keep, db)] == ["1", "1", "0"]
    assert [series("backup_coverage_volume_bytes", *p) for p in (app, keep)] == ["0", "0"]
    assert float(series("backup_coverage_snapshot_timestamp", *db)) > 0
    assert series("backup_coverage_snapshot_timestamp", *pg) == "0"
    assert series("backup_coverage_snapshot_timestamp", *app) == series("backup_coverage_snapshot_timestamp", *conf)
