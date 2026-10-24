"""Moving a volume's contents into a new volume, for a rename (ADR 0010). Docker cannot rename a volume.

One throwaway container bind-mounts Docker's volumes directory once, so each top-level entry of
<old>/_data moves to <new>/_data with rename(2) within one filesystem: instant, and ownership,
permissions and timestamps stay as they are. The new _data
directory itself takes the old one's owner and mode. Symlinks are renamed, never followed. The image is the
python one hops already runs for `logs --loki` (hops/loki.py), never pulled.

SCRIPT runs in that container (or, in tests, on this machine against a directory). It prints one
JSON document: the filesystem mounted at ROOT and, for each volume, its _data's device and number of
top-level entries. `check` only reads; `move` re-checks, renames, and reports the state either way.

`copy` copies <old>/_data into an empty <new>/_data for a Rollback Volume (hops/rollback_volumes.py),
leaving <old> as it was. The image's cp is BusyBox's, which drops xattrs, POSIX ACLs and setgid bits, so
SCRIPT copies with Python's own calls: every file type, owners, modes, times, hard links and every xattr
(ACLs are xattrs). It then compares a manifest of both trees (each path's type, mode, owner, size, mtime,
device number, symlink target, xattrs and hard-link group) and fails, naming the paths, on any difference.
`verify` is that comparison alone.
"""

import json
import re
from dataclasses import dataclass

from hops import docker, loki, strays
from hops.declared import Declared
from hops.docker import Container
from hops.errors import HopsError, UsageError
from hops.runner import Runner

# Docker's rule for volume names (daemon/names.RestrictedNamePattern). Checked before any Host call,
# so a name is never more than a plain argument.
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")
ROOT = "/volumes"  # where the container sees Docker's volumes directory
TIMEOUT = 600
LABEL = "hops.volume-move"
# Capabilities by mode (False: check, move; True: copy). Every mode reads and renames across owners. The copy
# also keeps setgid on another group's files (FSETID), device nodes (MKNOD), file capabilities (SETFCAP), and
# trusted.* xattrs, which the kernel hides from a process without SYS_ADMIN: without it, both manifests would
# lack them and a lost one would pass verification.
CAPABILITIES = {
    False: ("CHOWN", "DAC_OVERRIDE", "FOWNER"),
    True: ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "MKNOD", "SETFCAP", "SYS_ADMIN"),
}
SCRIPT = r"""
import json, os, sys

mode, root, old, new = sys.argv[1:5]


def mount():
    # The filesystem mounted at root: [type, source]; for ZFS the source is the dataset.
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                if left.split()[4] == root:
                    return right.split()[:2]
    except OSError:
        pass
    return None


def volume(name):
    data = os.path.join(root, name, "_data")
    if os.path.islink(data) or not os.path.isdir(data):
        return None
    return {"dev": os.lstat(data).st_dev, "entries": len(os.listdir(data))}


def state(**extra):
    return {"mount": mount(), "dev": os.stat(root).st_dev, "old": volume(old), "new": volume(new), **extra}


if mode == "check":
    print(json.dumps(state()))
    sys.exit(0)

def xattrs(path):
    # Every extended attribute, POSIX ACLs (system.posix_acl_*) and trusted.* included. Off Linux (this
    # script's tests on macOS) Python has no xattr calls, so there are none to keep or compare.
    if not hasattr(os, "listxattr"):
        return {}
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return {}
        raise
    return {n: os.getxattr(path, n, follow_symlinks=False) for n in sorted(names)}


def walk(top, rel=""):
    # (rel, path, lstat) for top and everything under it, depth first in name order; never follows a symlink.
    path = os.path.join(top, rel) if rel else top
    st = os.lstat(path)
    yield rel or ".", path, st
    if stat.S_ISDIR(st.st_mode):
        for name in sorted(os.listdir(path)):
            yield from walk(top, os.path.join(rel, name))


def records(top):
    # The manifest, one (rel, record) at a time so a large volume is never held in memory: type, mode,
    # owner, size, mtime, device number, symlink target, a hash of each xattr, and the first path of its
    # hard-link group. A record is read before the next is, so the two walks stay in step.
    first = {}
    for rel, path, st in walk(top):
        kind = stat.S_IFMT(st.st_mode)
        link = None
        if kind != stat.S_IFDIR and st.st_nlink > 1:
            link = first.setdefault((st.st_dev, st.st_ino), rel)
        yield rel, {
            "type": kind,
            "mode": stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "size": st.st_size if kind == stat.S_IFREG else None,
            "mtime": st.st_mtime_ns,
            "rdev": st.st_rdev if kind in (stat.S_IFCHR, stat.S_IFBLK) else None,
            "target": os.readlink(path) if kind == stat.S_IFLNK else None,
            "xattrs": {n: hashlib.sha256(v).hexdigest() for n, v in xattrs(path).items()},
            "link": link,
        }


def verify(src, dst):
    # Compare the two manifests; raise naming the first differing paths and fields. Returns the path count.
    count, diffs = 0, []
    for a, b in itertools.zip_longest(records(src), records(dst)):
        count += 1
        if a is not None and b is not None and a[0] == b[0]:
            fields = [k for k in a[1] if a[1][k] != b[1][k]]
            if fields:
                diffs.append(f"{a[0]!r} ({', '.join(fields)})")
            continue
        # Paths out of step: an entry is missing on one side. Name it and stop comparing.
        rel = min(r[0] for r in (a, b) if r is not None)
        side = old if a is not None and a[0] == rel else new
        diffs.append(f"{rel!r} (only in {side})")
        break
    if diffs:
        n = len(diffs)
        shown = ", ".join(diffs[:5]) + (", ..." if n > 5 else "")
        raise RuntimeError(f"{new} differs from {old} at {n} path{'s' if n != 1 else ''}: {shown}")
    return count


def keep(src, dst, st):
    # dst takes src's owner, xattrs, mode and times, in that order: chown clears setuid, setgid and file
    # capabilities, so xattrs and the mode come after it; setting them does not change the mtime.
    kind = stat.S_IFMT(st.st_mode)
    os.chown(dst, st.st_uid, st.st_gid, follow_symlinks=False)
    for name, value in xattrs(src).items():
        os.setxattr(dst, name, value, follow_symlinks=False)
    if kind != stat.S_IFLNK:
        os.chmod(dst, stat.S_IMODE(st.st_mode))
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)


def copy(src, dst, st, links):
    try:
        copy_one(src, dst, st, links)
    except OSError as exc:
        raise RuntimeError(f"{dst}: {exc.strerror or exc}") from None


def copy_one(src, dst, st, links):
    # src (lstat st) into dst, which must not exist except for the top directory; directories get their
    # metadata after their contents, so writing into them does not change the times kept.
    kind = stat.S_IFMT(st.st_mode)
    if kind == stat.S_IFDIR:
        if not os.path.lexists(dst):  # only the top directory, the new volume's _data, exists already
            os.mkdir(dst, 0o700)
        for name in sorted(os.listdir(src)):
            child = os.path.join(src, name)
            copy(child, os.path.join(dst, name), os.lstat(child), links)
    elif kind == stat.S_IFREG:
        if st.st_nlink > 1:
            seen = links.setdefault((st.st_dev, st.st_ino), dst)
            if seen != dst:
                os.link(seen, dst)
                return
        with open(src, "rb") as f, open(dst, "xb") as g:
            shutil.copyfileobj(f, g, 1 << 20)
    elif kind == stat.S_IFLNK:
        os.symlink(os.readlink(src), dst)
    elif kind in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
        os.mknod(dst, kind | 0o600, st.st_rdev)
    else:
        raise RuntimeError(f"{src}: a file type this copy cannot keep")
    keep(src, dst, st)


if mode in ("copy", "verify"):
    verified = None
    try:
        import errno, hashlib, itertools, shutil, stat

        sys.setrecursionlimit(10000)
        before = state()
        if not before["old"] or not before["new"]:
            raise RuntimeError("both volumes' _data must be directories under " + root)
        src, dst = os.path.join(root, old, "_data"), os.path.join(root, new, "_data")
        if mode == "copy":
            if before["new"]["entries"]:
                raise RuntimeError(new + " is not empty")
            copy(src, dst, os.lstat(src), {})
        verified = verify(src, dst)
    except Exception as exc:
        print(json.dumps(state(verified=verified, error=f"{type(exc).__name__}: {exc}")))
        sys.exit(1)
    print(json.dumps(state(verified=verified, error=None)))
    sys.exit(0)

moved = 0
try:
    before = state()
    if not before["old"] or not before["new"]:
        raise RuntimeError("both volumes' _data must be directories under " + root)
    if not before["old"]["dev"] == before["new"]["dev"] == before["dev"]:
        raise RuntimeError("the volumes are not on one filesystem, so rename(2) cannot move them")
    if before["new"]["entries"]:
        raise RuntimeError(new + " is not empty")
    src, dst = os.path.join(root, old, "_data"), os.path.join(root, new, "_data")
    # The new _data takes the old one's owner and mode: a Datastore's often belongs to its user.
    st = os.lstat(src)
    os.chown(dst, st.st_uid, st.st_gid)
    os.chmod(dst, st.st_mode & 0o7777)
    for entry in os.listdir(src):
        if os.path.lexists(os.path.join(dst, entry)):
            raise RuntimeError("an entry already exists in " + new)
        os.rename(os.path.join(src, entry), os.path.join(dst, entry))
        moved += 1
except Exception as exc:
    print(json.dumps(state(moved=moved, error=f"{type(exc).__name__}: {exc}")))
    sys.exit(1)
print(json.dumps(state(moved=moved, error=None)))
"""


@dataclass(frozen=True)
class Probe:
    """What SCRIPT saw: the filesystem under the volumes directory, and each volume's _data."""

    fstype: str | None
    source: str | None  # the ZFS dataset holding the volumes directory
    dev: int
    old: dict | None  # {"dev", "entries"}; None when <old>/_data is missing
    new: dict | None
    moved: int | None = None
    error: str | None = None
    verified: int | None = None  # copy/verify: the paths whose manifests matched

    @classmethod
    def parse(cls, text: str) -> "Probe":
        try:
            data = json.loads(text)
            fstype, source = data["mount"] or (None, None)
            return cls(
                fstype,
                source,
                data["dev"],
                data["old"],
                data["new"],
                data.get("moved"),
                data.get("error"),
                data.get("verified"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise HopsError(
                f"the volume-move container printed something other than its report: {text[:200]!r}"
            ) from None


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise UsageError(f"{name!r} is not a Docker volume name ([a-zA-Z0-9][a-zA-Z0-9_.-]+)")


def convention(declared: Declared, name: str) -> str | None:
    """A warning when `name` does not read <stack>_[<service>_]<purpose> for a declared Stack (ADR 0010)."""
    stack, sep, rest = name.partition("_")
    if sep and rest and stack in declared.stacks:
        return None
    return f"{name} does not read <stack>_[<service>_]<purpose> for a declared Stack"


def volumes_dir(runner: Runner) -> str:
    """Docker's volumes directory on the Host: <DockerRootDir>/volumes, from `docker info`."""
    root = json.loads(runner.docker("info", "--format", "{{json .DockerRootDir}}").stdout or "null")
    if not isinstance(root, str) or not root.startswith("/"):
        raise HopsError(f"docker info gave no DockerRootDir: {root!r}")
    return root.rstrip("/") + "/volumes"


def inspect(runner: Runner, name: str) -> dict | None:
    result = runner.docker("volume", "inspect", name, check=False)
    if result.returncode != 0:
        if "no such volume" in result.stderr.lower():
            return None
        raise HopsError(f"docker volume inspect {name} failed: {result.stderr.strip()}")
    found = json.loads(result.stdout or "[]")
    return found[0] if found else None


def local_problem(volume: dict, directory: str) -> str | None:
    """Why `volume`'s data is not plain files under Docker's volumes directory, or None."""
    name = volume["Name"]
    if volume.get("Driver") != "local":
        return f"its driver is {volume.get('Driver')}, not local"
    if volume.get("Options"):
        opts = ", ".join(f"{k}={v}" for k, v in sorted(volume["Options"].items()))
        return f"it has driver options ({opts}), so its data is not in {directory}/{name}/_data"
    if volume.get("Mountpoint") != f"{directory}/{name}/_data":
        return f"its Mountpoint {volume.get('Mountpoint')} is not {directory}/{name}/_data"
    return None


def run(runner: Runner, image: str, directory: str, mode: str, old: str, new: str):
    """SCRIPT in a throwaway container that mounts `directory` at ROOT (read-only for `check`)."""
    return runner.docker(
        "run",
        "--rm",
        "--label",
        f"{strays.DELIBERATE_LABEL}={strays.DELIBERATE_VALUE}",
        "--label",
        f"{LABEL}={old}",
        "--log-driver",
        "none",
        "--network",
        "none",
        "--pull",
        "never",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        *(arg for cap in CAPABILITIES[mode == "copy"] for arg in ("--cap-add", cap)),
        "--memory",
        "256m",
        "-v",
        f"{directory}:{ROOT}{':ro' if mode == 'check' else ''}",
        image,
        "python3",
        "-c",
        SCRIPT,
        mode,
        ROOT,
        old,
        new,
        check=False,
        timeout=TIMEOUT,
    )


def copy(runner: Runner, image: str, directory: str, old: str, new: str) -> Probe:
    """Copy <old>/_data into the empty <new>/_data, keeping <old>. The Probe carries `error` when the
    copy failed; a HopsError means no report came back at all."""
    result = run(runner, image, directory, "copy", old, new)
    if not result.stdout.strip():
        raise HopsError(
            f"the volume-copy container gave no report: {result.stderr.strip() or f'exit code {result.returncode}'}"
        )
    return Probe.parse(result.stdout.strip().splitlines()[-1])


def check(runner: Runner, image: str, directory: str, old: str, new: str) -> Probe:
    result = run(runner, image, directory, "check", old, new)
    if result.returncode != 0:
        raise HopsError(
            f"the volume-move container could not read {directory}: "
            f"{result.stderr.strip() or f'exit code {result.returncode}'}"
        )
    return Probe.parse(result.stdout)


def move(runner: Runner, image: str, directory: str, old: str, new: str) -> Probe:
    """Rename every top-level entry of <old>/_data into <new>/_data. The Probe carries `error` when it
    stopped part-way; a HopsError means no report came back at all."""
    result = run(runner, image, directory, "move", old, new)
    if not result.stdout.strip():
        raise HopsError(
            f"the volume-move container gave no report: {result.stderr.strip() or f'exit code {result.returncode}'}"
        )
    return Probe.parse(result.stdout.strip().splitlines()[-1])


def image(declared: Declared, host: str) -> str:
    return loki.transport_image(declared, host, "volume move runs its image")


def users(runner: Runner, name: str) -> list[Container]:
    """Every container, running or stopped, that mounts the volume `name`."""
    ids = runner.docker("ps", "-aq", "--no-trunc", "--filter", f"volume={name}").stdout.split()
    return [
        c
        for c in docker.inspect(runner, *ids)
        if any(m.get("Type") == "volume" and m.get("Name") == name for m in c.raw.get("Mounts") or [])
    ]
