import pytest

from backup_coverage.stackback import BackupPath, Settings, backup_paths

OLD = 1767225600


def container(project, service, labels=None, mounts=(), image="example/app:1"):
    compose = {"com.docker.compose.project": project, "com.docker.compose.service": service}
    labels = {**compose, **(labels or {})}
    return {"Id": service, "Image": image, "Created": OLD, "Labels": labels, "Mounts": list(mounts)}


ENV = {"db": ["POSTGRES_USER=app", "POSTGRES_DB=app"], "nodb": ["POSTGRES_USER=app"], "pg": ["POSTGRES_DB=pg"]}
# Config.Image from container inspect, which stack-back reads; the list's Image can differ (e.g. after a re-tag).
IMAGES = {"pg": "postgres:17", "maria": "mariadb:11", "my": "mysql:8.4", "pgoff": "postgres:17", "app": "app:1"}


def inspect(container_id):
    return {"Env": ENV.get(container_id, []), "Image": IMAGES.get(container_id, "example/app:1")}


VOLUME = {
    "Type": "volume",
    "Name": "app_data",
    "Source": "/var/lib/docker/volumes/app_data/_data",
    "Destination": "/data",
}
BIND = {"Type": "bind", "Source": "/srv/app", "Destination": "/srv"}
STACK_BACK = container("ops", "stack-back", image="ghcr.io/lawndoc/stack-back:v1.5.4")


def paths(containers, **environ):
    return [p.path for p in backup_paths(containers, Settings.from_env(environ), inspect)]


def test_by_default_backs_up_bind_mounts_too_and_only_stack_backs_own_project_without_its_name():
    containers = [
        STACK_BACK,
        container("ops", "app", {"stack-back.volumes": "true"}, [VOLUME, BIND]),
        container("media", "other", {"stack-back.volumes": "true"}, [VOLUME]),
    ]
    assert backup_paths(containers, Settings(), inspect) == [
        BackupPath("volume", "ops", "app", "/volumes/app/data", OLD, "app_data"),
        BackupPath("volume", "ops", "app", "/volumes/app/srv", OLD, None),
    ]


def test_every_stack_back_project_counts_but_not_its_backup_process_containers():
    process = container("media", "backup", {"stack-back.process-media": "True"}, image="ghcr.io/lawndoc/stack-back:v1")
    containers = [
        STACK_BACK,
        container("nc", "stack-back", image="ghcr.io/lawndoc/stack-back:v1.5.4"),
        process,
        container("ops", "a", {"stack-back.volumes": "true"}, [VOLUME]),
        container("nc", "b", {"stack-back.volumes": "true"}, [VOLUME]),
        container("media", "c", {"stack-back.volumes": "true"}, [VOLUME]),
    ]
    assert paths(containers) == ["/volumes/a/data", "/volumes/b/data"]


def test_without_a_stack_back_container_there_is_no_project_to_back_up():
    with pytest.raises(LookupError, match="no stack-back container"):
        paths([container("ops", "app", {"stack-back.volumes": "true"}, [VOLUME])])


@pytest.mark.parametrize("value", ["true", "false"])  # stack-back only asks whether it is set
def test_include_all_compose_projects_takes_every_project_and_names_it_when_true(value):
    containers = [container("media", "app", {"stack-back.volumes": "true"}, [VOLUME])]
    expected = "/volumes/media/app/data" if value == "true" else "/volumes/app/data"
    assert paths(containers, INCLUDE_ALL_COMPOSE_PROJECTS=value) == [expected]


def test_include_project_name_alone_names_the_project():
    containers = [STACK_BACK, container("ops", "db", {"stack-back.postgres": "true"})]
    assert paths(containers, INCLUDE_PROJECT_NAME="true") == ["/databases/ops/db/app.sql"]


def test_auto_backup_all_backs_up_every_container_not_opted_out():
    containers = [
        STACK_BACK,
        container("ops", "a", mounts=[VOLUME]),
        container("ops", "b", {"stack-back.volumes": "false"}, [VOLUME]),
    ]
    assert paths(containers, AUTO_BACKUP_ALL="true") == ["/volumes/a/data"]
    assert paths(containers, INCLUDE_ALL_VOLUMES="1") == ["/volumes/a/data"]
    assert paths(containers) == []


def test_dumps_mariadb_and_mysql_whole_and_postgres_as_its_one_database():
    containers = [
        STACK_BACK,
        container("ops", "maria", {"stack-back.mariadb": "true", "stack-back.postgres": "true"}),
        container("ops", "my", {"stack-back.mysql": "1"}),
        container("ops", "db", {"stack-back.postgres": "true"}),
        container("ops", "nodb", {"stack-back.postgres": "true"}),
    ]
    assert paths(containers) == [
        "/databases/maria/all_databases.sql",
        "/databases/my/all_databases.sql",
        "/databases/db/app.sql",
        "/databases/nodb/None.sql",
    ]


def test_auto_backup_all_dumps_databases_by_image_and_skips_their_data_unless_opted_out():
    data = {"Type": "volume", "Name": "db", "Source": "/x/db", "Destination": "/var/lib/postgresql/data"}
    mysql = {"Type": "volume", "Name": "my", "Source": "/x/my", "Destination": "/var/lib/mysql"}
    containers = [
        STACK_BACK,
        container("ops", "pg", mounts=[data, VOLUME], image="sha256:0123"),
        container("ops", "maria", mounts=[mysql]),
        container("ops", "my", mounts=[mysql]),
        container("ops", "pgoff", {"stack-back.postgres": "false"}, [data]),
        container("ops", "app", {"stack-back.postgres": "true"}),
    ]
    assert paths(containers, AUTO_BACKUP_ALL="true") == [
        "/databases/pg/pg.sql",
        "/volumes/pg/data",
        "/databases/maria/all_databases.sql",
        "/databases/my/all_databases.sql",
        "/volumes/pgoff/var/lib/postgresql/data",
        "/databases/app/None.sql",
    ]
    assert paths(containers) == ["/databases/app/None.sql"]
