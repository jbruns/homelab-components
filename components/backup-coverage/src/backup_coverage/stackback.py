"""The paths stack-back backs up, worked out from Docker's container list the way stack-back v1.5.4 does.

Mirrors restic_compose_backup's containers.py (RunningContainers, Container.volume_backup_enabled, filter_mounts,
get_volume_backup_destination), containers_db.py (backup_destination_path) and config.py, including its settings,
read from the same environment variables so a site can hand this Component stack-back's own env file.

One deliberate difference: stack-back only looks at running containers, while this counts stopped ones too, so a
stopped service whose data is not being backed up shows up as missing.
"""

import functools
from dataclasses import dataclass
from pathlib import PurePosixPath

TRUE_VALUES = {"1", "true", "True", "TRUE"}
FALSE_VALUES = {"0", "false", "False", "FALSE"}
# stack-back's database kinds, each its label suffix and image prefix; a container with several is dumped as the first.
DATABASES = ("mariadb", "mysql", "postgres")
DATABASE_MOUNTS = {"/var/lib/mysql", "/var/lib/mariadb", "/var/lib/postgresql/data"}
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


@dataclass(frozen=True)
class Settings:
    """stack-back's settings that decide which paths it backs up; defaults are stack-back's."""

    exclude_bind_mounts: bool = False
    include_all_compose_projects: bool = False
    include_project_name: bool = False
    auto_backup_all: bool = False

    @classmethod
    def from_env(cls, environ):
        include_all = environ.get("INCLUDE_ALL_COMPOSE_PROJECTS") or ""
        return cls(
            exclude_bind_mounts=environ.get("EXCLUDE_BIND_MOUNTS") in TRUE_VALUES,
            # stack-back tests this one for any value at all, so even "false" includes every project.
            include_all_compose_projects=bool(include_all),
            include_project_name=(include_all or environ.get("INCLUDE_PROJECT_NAME")) in TRUE_VALUES,
            auto_backup_all=(environ.get("AUTO_BACKUP_ALL") or environ.get("INCLUDE_ALL_VOLUMES")) in TRUE_VALUES,
        )


@dataclass(frozen=True)
class BackupPath:
    """A path stack-back writes to its restic repository for one container."""

    kind: str  # "volume" (a mount, under /volumes) or "database" (a dump, under /databases)
    project: str
    service: str
    path: str
    container_created: float
    volume: str | None  # the Docker volume behind a "volume" path; None for a bind mount or a dump


def _patterns(value):
    value = (value or "").strip()
    return value.split(",") if value else None


def _is_stack_back(container):
    return "stack-back" in container.get("Image", "")


def _is_oneoff(labels):
    return labels.get("com.docker.compose.oneoff") == "True"


def _stack_back_projects(containers):
    """Compose projects of the stack-back containers (not their backup-process containers)."""
    projects = set()
    for c in containers:
        labels = c.get("Labels") or {}
        project = labels.get(PROJECT_LABEL, "")
        if _is_stack_back(c) and not _is_oneoff(labels) and labels.get(f"stack-back.process-{project}") != "True":
            projects.add(project)
    if not projects:
        raise LookupError("found no stack-back container to take the Compose project from")
    return projects


def _mounts_for_backup(labels, mounts, settings, is_database):
    enabled = labels.get("stack-back.volumes")
    if not (enabled in TRUE_VALUES or (settings.auto_backup_all and enabled not in FALSE_VALUES)):
        return []
    if settings.exclude_bind_mounts:
        mounts = [m for m in mounts if m.get("Type") == "volume"]
    include = _patterns(labels.get("stack-back.volumes.include"))
    exclude = _patterns(labels.get("stack-back.volumes.exclude"))
    if include:
        return [m for m in mounts if any(p in m.get("Source", "") for p in include)]
    if exclude:
        return [m for m in mounts if not any(p in m.get("Source", "") for p in exclude)]
    return [m for m in mounts if not (is_database and m.get("Destination") in DATABASE_MOUNTS)]


def _database(labels, settings, config):
    """The database stack-back dumps: one labelled true, or with AUTO_BACKUP_ALL one whose image (Config.Image)
    starts with its name, unless labelled false. `config()` is asked only with AUTO_BACKUP_ALL."""
    for kind in DATABASES:
        label = labels.get(f"stack-back.{kind}")
        automatic = settings.auto_backup_all and label not in FALSE_VALUES
        if label in TRUE_VALUES or (automatic and (config()["Image"] or "").startswith(kind)):
            return kind
    return None


def _dump_name(database, config):
    """stack-back dumps MariaDB and MySQL whole, and Postgres as the one database in the container's POSTGRES_DB."""
    if database != "postgres":
        return "all_databases.sql"
    env = dict(entry.partition("=")[::2] for entry in config()["Env"] or [])
    return f"{env.get('POSTGRES_DB')}.sql"  # stack-back writes "None.sql" when it is unset


def _once(inspect, container_id):
    return functools.cache(lambda: inspect(container_id))


def backup_paths(containers, settings, inspect):
    """Every BackupPath for the containers of Docker's `/containers/json?all=1`, in container and mount order.

    `inspect(id)` is the Config of a container's `/containers/{id}/json` (its Env and Image), asked only for Postgres
    containers and, with AUTO_BACKUP_ALL, for the image of every container that might be backed up.
    """
    projects = None if settings.include_all_compose_projects else _stack_back_projects(containers)
    paths = []
    for c in containers:
        labels = c.get("Labels") or {}
        project = labels.get(PROJECT_LABEL, "")
        if _is_stack_back(c) or _is_oneoff(labels) or (projects is not None and project not in projects):
            continue
        service = labels.get(SERVICE_LABEL, "")
        prefix = [project] if settings.include_project_name and project else []
        created = float(c["Created"])
        config = _once(inspect, c["Id"])
        database = _database(labels, settings, config)
        if database:
            path = PurePosixPath("/databases", *prefix, service, _dump_name(database, config))
            paths.append(BackupPath("database", project, service, str(path), created, None))
        for m in _mounts_for_backup(labels, c.get("Mounts") or [], settings, database is not None):
            destination = m.get("Destination", "").strip().removeprefix("/")
            path = PurePosixPath("/volumes", *prefix, service, destination)
            volume = m.get("Name") if m.get("Type") == "volume" else None
            paths.append(BackupPath("volume", project, service, str(path), created, volume))
    return paths
