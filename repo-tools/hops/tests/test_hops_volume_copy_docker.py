"""The Rollback Volume copy against real Docker: volumes.copy in the python image hops runs, on real volumes.

Needs a Docker daemon (CI's runner has one); skipped without one. The Runner's ssh is replaced by running the
same docker argv locally, so the container gets exactly the flags, capabilities and mounts it gets on a Host.
"""

import json
import shlex
import subprocess
import uuid

import pytest

from hops import volumes
from hops.config import HostConfig
from hops.runner import Result, Runner

pytestmark = pytest.mark.docker

# python:3.14-alpine (BusyBox userland, as on a Host's dependencies-render), pinned by its index digest.
IMAGE = "python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72"
SETUP_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "MKNOD", "SETFCAP")
CAPS = ["--cap-drop", "ALL", *(f"--cap-add={c}" for c in SETUP_CAPS)]

# A POSIX ACL as the kernel stores it in system.posix_acl_*: version 2, then (tag u16, perm u16, id u32) entries.
# A file capability as security.capability: revision 2 with the effective bit, then cap_net_bind_service permitted.
SETUP = r"""
import os, stat, struct

def acl(*entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)

ANY = 0xFFFFFFFF
ACL = acl((0x01, 7, ANY), (0x02, 5, 1001), (0x04, 5, ANY), (0x08, 4, 2002), (0x10, 5, ANY), (0x20, 0, ANY))
d = "/v"  # the volume, mounted as its _data
os.makedirs(d + "/dir/sub")
with open(d + "/dir/file", "w") as f:
    f.write("data")
os.chown(d + "/dir/file", 1234, 5678)
os.chmod(d + "/dir/file", 0o4750)
os.setxattr(d + "/dir/file", "user.note", b"kept")
os.setxattr(d + "/dir/file", "trusted.note", b"kept too")
os.setxattr(d + "/dir/file", "system.posix_acl_access", ACL)
os.setxattr(d + "/dir/file", "security.capability", struct.pack("<5I", 0x02000001, 1 << 10, 0, 0, 0))
os.setxattr(d + "/dir", "system.posix_acl_default", ACL)
os.utime(d + "/dir/file", ns=(1, 1_700_000_000_123_456_789))
os.link(d + "/dir/file", d + "/hardlink")
os.symlink("dir/file", d + "/symlink")
os.chown(d + "/symlink", 42, 43, follow_symlinks=False)
os.mkfifo(d + "/fifo")
os.mknod(d + "/null", stat.S_IFCHR | 0o666, os.makedev(1, 3))
os.chown(d + "/dir/sub", 7, 8)
os.chmod(d + "/dir/sub", 0o2770)
os.chown(d, 1234, 5678)
os.chmod(d, 0o750)
"""

# Independent of volumes.SCRIPT: every path's metadata, xattrs (ACLs included) and hard-link group.
MANIFEST = r"""
import json, os, stat, sys

def walk(top):
    out, inodes = {}, {}
    for dirpath, dirs, files in os.walk(top):
        names = [os.path.relpath(dirpath, top)] + [os.path.relpath(os.path.join(dirpath, n), top) for n in dirs + files]
        for rel in names:
            if rel in out:
                continue
            path = os.path.join(top, rel)
            st = os.lstat(path)
            out[rel] = {
                "type": stat.S_IFMT(st.st_mode), "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid,
                "gid": st.st_gid, "size": st.st_size if stat.S_ISREG(st.st_mode) else None,
                "mtime": st.st_mtime_ns, "rdev": st.st_rdev,
                "target": os.readlink(path) if stat.S_ISLNK(st.st_mode) else None,
                "xattrs": {n: os.getxattr(path, n, follow_symlinks=False).hex()
                           for n in sorted(os.listxattr(path, follow_symlinks=False))},
            }
            inodes.setdefault((st.st_dev, st.st_ino), []).append(rel)
    for group in inodes.values():
        for rel in group:
            out[rel]["links"] = sorted(group)
    return out

print(json.dumps({name: walk(f"/volumes/{name}/_data") for name in sys.argv[1:]}))
"""


def docker(*args: str, check=True, input=None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, input=input)


def available() -> bool:
    try:
        return docker("info", check=False).returncode == 0
    except OSError:
        return False


def local(argv, *, input=None, capture=True, timeout=None) -> Result:
    """The Runner's ssh, run here: `sudo -n docker …` becomes `docker …`."""
    remote = shlex.split(argv[-1])
    assert remote[:3] == ["sudo", "-n", "docker"], remote
    done = subprocess.run(["docker", *remote[3:]], input=input, capture_output=True, text=True, timeout=timeout)
    return Result(argv, done.returncode, done.stdout, done.stderr)


@pytest.fixture
def real(tmp_path):
    if not available():
        pytest.skip("no Docker daemon")
    docker("pull", "-q", IMAGE)
    runner = Runner(HostConfig("localhost", "me", None, tmp_path / "cm"), process=local)
    names = [f"hops-test-{uuid.uuid4().hex[:8]}_{n}" for n in ("old", "rollback")]
    for name in names:
        docker("volume", "create", name)
    yield runner, *names
    docker("volume", "rm", "-f", *names, check=False)


def manifest(directory: str, *names: str) -> dict:
    done = docker(
        "run", "--rm", "--network", "none", *CAPS, "--cap-add=SYS_ADMIN", "-v", f"{directory}:/volumes:ro",
        IMAGE, "python3", "-c", MANIFEST, *names,
    )  # fmt: skip
    return json.loads(done.stdout)


def test_the_rollback_copy_keeps_owners_modes_times_links_devices_xattrs_and_acls(real):
    runner, old, rollback = real
    docker("run", "--rm", "--network", "none", *CAPS, "--cap-add=SYS_ADMIN", "-v", f"{old}:/v", IMAGE,
           "python3", "-c", SETUP)  # fmt: skip
    directory = volumes.volumes_dir(runner)

    copied = volumes.copy(runner, IMAGE, directory, old, rollback)

    assert copied.error is None
    assert copied.verified == 8  # _data and the 7 paths under it
    found = manifest(directory, old, rollback)
    source = found[old]
    assert set(source["dir/file"]["xattrs"]) == {
        "user.note",
        "trusted.note",
        "system.posix_acl_access",
        "security.capability",
    }, "the fixture lost its xattrs; the test would prove nothing"
    assert "system.posix_acl_default" in source["dir"]["xattrs"]
    assert source["dir/file"]["mode"] == 0o4750 and source["dir/sub"]["mode"] == 0o2770
    assert found[rollback] == source
