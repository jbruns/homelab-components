"""Rollback Volumes (GLOSSARY): the pre-change copy of a volume, kept with its change and a release date.

`hops volume move` keeps one on a Host without ZFS, where no Rollback Snapshot can be taken. It is never a
Backup. Its labels name the change's issue, the volume it copies, and its release date; past that date it
is a Stray (hops strays), which `hops strays --apply --volumes` removes.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime

ISSUE, OF, EXPIRES = "hops.rollback.issue", "hops.rollback.of", "hops.rollback.expires"


@dataclass(frozen=True)
class RollbackVolume:
    name: str
    of: str  # the volume it copies
    issue: str
    expires: str  # ISO date, UTC

    def to_json(self) -> dict:
        return asdict(self)

    def labels(self) -> list[str]:
        """`docker volume create` arguments that label it."""
        pairs = ((ISSUE, self.issue), (OF, self.of), (EXPIRES, self.expires))
        return [arg for key, value in pairs for arg in ("--label", f"{key}={value}")]


def plan(old: str, issue: str, expires: str, at: datetime) -> RollbackVolume:
    """The Rollback Volume for changing `old`: `<old>.rollback-<issue>-<UTC time>`."""
    return RollbackVolume(name=f"{old}.rollback-{issue}-{at.strftime('%Y%m%dT%H%M%SZ')}", of=old, issue=issue,
                          expires=expires)


def expired(labels: dict[str, str], on: date) -> bool | None:
    """None when the labels are no Rollback Volume's; else whether its release date is before `on`.
    A missing or unreadable date counts as expired, so the volume is reported rather than forgotten."""
    if not any(key in labels for key in (ISSUE, OF, EXPIRES)):
        return None
    try:
        return date.fromisoformat(labels.get(EXPIRES) or "") < on
    except ValueError:
        return True


def describe(labels: dict[str, str]) -> dict:
    return {"of": labels.get(OF), "issue": labels.get(ISSUE), "expires": labels.get(EXPIRES)}
