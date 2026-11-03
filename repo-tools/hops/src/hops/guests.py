"""Guests on a Proxmox VE Host, against the consuming repo's record of them: what `hops host` and `hops strays`
report for a Host with the proxmox capability that runs no Stacks.

A Guest is a VM (`qemu`) or container (`lxc`). It is a **Kept Guest** when the record lists its ID, a **Scratch
Guest** when it carries the `scratch` tag, and otherwise a **Stray**.

The record is the consuming repo's `host/<Host name>/` directory (`--record DIR` names another). hops reads two
files there and ignores every other key in them, so they can hold the rest of the Host's declared state:

- `guests.yaml`: each top-level key is a Kept Guest, named as Proxmox VE names it, with its `vmid` (required) and
  `type` (`qemu` or `lxc`; optional; when set, a Guest of the other type with that ID is not this one).
- `network.yaml`: optional `exclusive_networks`, the networks a Guest may join only one of: `networks`, each a
  name mapping to a `bridge` and an optional VLAN `tag` (without one, the bridge's untagged traffic), and
  `may_join_several`, the IDs of the Guests that may join more than one.

A missing `guests.yaml` lists no Kept Guests; a missing `exclusive_networks` leaves dual-homing unchecked. Both
say so in the report's notes.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from hops import snapshots
from hops.errors import ConfigError, HopsError, HostTimeout
from hops.proxmox import HostOff, ProxmoxError

RECORD_DIR = "host"  # the record is <repo root>/host/<Host name>/
GUESTS_FILE = "guests.yaml"
NETWORK_FILE = "network.yaml"
SCRATCH_TAG = "scratch"
TYPES = ("qemu", "lxc")
KINDS = ("kept", "scratch", "stray")
NET_KEY = re.compile(r"net\d+")
TAGS = re.compile(r"[;,\s]+")
# One line of `apt list --upgradable`: name/origin version arch [upgradable from: old]
UPGRADABLE = re.compile(r"(?P<package>[^/\s]+)/\S+\s+(?P<version>\S+)\s+\S+\s+\[upgradable from: (?P<old>[^\]]+)\]")
DAY = 86400


@dataclass(frozen=True)
class KeptGuest:
    name: str
    vmid: int
    type: str | None


@dataclass(frozen=True)
class Network:
    name: str
    bridge: str
    tag: int | None


@dataclass(frozen=True)
class Record:
    directory: Path
    shown: str  # the directory as the report names it: relative to the repo root when inside it
    kept: dict[int, KeptGuest] | None  # None: no guests.yaml
    networks: tuple[Network, ...] | None  # None: no exclusive_networks
    may_join_several: frozenset[int] = frozenset()


def record_dir(repo_root: Path, host_name: str, option: str | None, cwd: Path | None) -> Path:
    """`--record DIR` (relative to the working directory), else the convention `host/<Host name>/`."""
    if option:
        return ((cwd or Path.cwd()) / option).resolve()
    return repo_root / RECORD_DIR / host_name


def _yaml(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML ({getattr(exc, 'problem', None) or 'parse error'})") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: must be a mapping")
    return data


def _vmid(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 100 else None


def load_record(directory: Path, repo_root: Path) -> Record:
    """The Kept Guests and exclusive networks the record declares; ConfigError naming the file and key of the first
    problem."""
    try:
        shown = str(directory.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        shown = str(directory)
    guests = _yaml(directory / GUESTS_FILE)
    kept = None if guests is None else _kept(directory / GUESTS_FILE, guests)
    network = _yaml(directory / NETWORK_FILE) or {}
    exclusive = network.get("exclusive_networks")
    networks, several = None, frozenset()
    if exclusive is not None:
        networks, several = _exclusive(directory / NETWORK_FILE, exclusive)
    return Record(directory, shown, kept, networks, several)


def _kept(path: Path, data: dict) -> dict[int, KeptGuest]:
    kept: dict[int, KeptGuest] = {}
    for name, item in data.items():
        where = f"{path}: {name}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: must be a mapping with the Guest's vmid")
        vmid = _vmid(item.get("vmid"))
        if vmid is None:
            raise ConfigError(f"{where}.vmid: must be the Guest's ID, a whole number from 100")
        kind = item.get("type")
        if kind is not None and kind not in TYPES:
            raise ConfigError(f"{where}.type: must be one of {', '.join(TYPES)}")
        if vmid in kept:
            raise ConfigError(f"{where}.vmid: {vmid} is also {kept[vmid].name}'s")
        kept[vmid] = KeptGuest(str(name), vmid, kind)
    return kept


def _exclusive(path: Path, data) -> tuple[tuple[Network, ...], frozenset[int]]:
    where = f"{path}: exclusive_networks"
    if not isinstance(data, dict) or not isinstance(data.get("networks"), dict) or len(data["networks"]) < 2:
        raise ConfigError(f"{where}.networks: must map two or more network names to a bridge and optional tag")
    networks = []
    for name, net in data["networks"].items():
        if not isinstance(net, dict) or not isinstance(net.get("bridge"), str) or not net["bridge"]:
            raise ConfigError(f"{where}.networks.{name}: must give a bridge, e.g. {{bridge: vmbr0}}")
        if set(net) - {"bridge", "tag", "purpose"}:
            raise ConfigError(f"{where}.networks.{name}: only bridge, tag and purpose are allowed")
        tag = net.get("tag")
        if tag is not None and not (isinstance(tag, int) and not isinstance(tag, bool) and 1 <= tag <= 4094):
            raise ConfigError(f"{where}.networks.{name}.tag: must be a VLAN ID from 1 to 4094")
        networks.append(Network(str(name), net["bridge"], tag))
    seen = [(n.bridge, n.tag) for n in networks]
    if len(set(seen)) < len(seen):
        raise ConfigError(f"{where}.networks: two networks name the same bridge and tag")
    several = data.get("may_join_several", [])
    if not isinstance(several, list) or any(_vmid(v) is None for v in several):
        raise ConfigError(f"{where}.may_join_several: must be a list of Guest IDs")
    return tuple(networks), frozenset(several)


def nics(config: dict) -> list[dict]:
    """The properties of each of a Guest's network interfaces that names a bridge (`net0: virtio=…,bridge=vmbr0`)."""
    found = []
    for key, value in sorted(config.items()):
        if NET_KEY.fullmatch(str(key)) and isinstance(value, str):
            props = dict(p.split("=", 1) for p in value.split(",") if "=" in p)
            if props.get("bridge"):
                found.append(props)
    return found


def interfaces(config: dict) -> list[tuple[str, int | None]]:
    """Each (bridge, VLAN) a Guest's network interfaces reach, from its config (`net0: virtio=…,bridge=vmbr0,tag=20`):
    the interface's `tag` (None: the bridge's untagged traffic), then each VLAN its `trunks` passes
    (`trunks=10;20-21`). An interface with neither on a VLAN-aware bridge reaches every VLAN too: see `joined`."""
    found = []
    for props in nics(config):
        tag = props.get("tag")
        found.append((props["bridge"], int(tag) if tag and tag.isdigit() else None))
        found += [(props["bridge"], vlan) for vlan in trunk_vlans(props.get("trunks") or "")]
    return found


def trunk_vlans(text: str) -> list[int]:
    vlans = []
    for part in text.split(";"):
        low, _, high = part.strip().partition("-")
        if low.isdigit() and (not high or high.isdigit()):
            vlans += range(int(low), int(high or low) + 1)
    return vlans


def vlan_aware(network: list | None) -> dict[str, bool] | None:
    """Whether each bridge in `nodes/{node}/network` passes VLANs to an interface that has neither `tag` nor `trunks`:
    a Linux bridge with `bridge_vlan_aware` set (Proxmox VE then adds VLANs 2-4094 to the port), or an Open vSwitch
    bridge (an untagged port trunks every VLAN). None when the network could not be read."""
    if not isinstance(network, list):
        return None
    aware = {}
    for iface in network:
        if isinstance(iface, dict) and iface.get("iface"):
            if iface.get("type") == "bridge":
                aware[iface["iface"]] = bool(iface.get("bridge_vlan_aware"))
            elif iface.get("type") == "OVSBridge":
                aware[iface["iface"]] = True
    return aware


def joined(
    config: dict, networks: tuple[Network, ...], aware: dict[str, bool] | None = None
) -> tuple[list[str], list[str]]:
    """The exclusive networks a Guest has an interface on, in the record's order, and the bridges whose VLAN-awareness
    is unknown (`aware` lacks them) where an interface with neither `tag` nor `trunks` might reach a VLAN network."""
    ifaces = set(interfaces(config))
    every_vlan, unknown = set(), []
    for props in nics(config):
        bridge = props["bridge"]
        if props.get("tag") or props.get("trunks") or not any(n.bridge == bridge and n.tag for n in networks):
            continue
        if aware is None or bridge not in aware:
            unknown.append(bridge)
        elif aware[bridge]:
            every_vlan.add(bridge)
    names = [n.name for n in networks if (n.bridge, n.tag) in ifaces or n.bridge in every_vlan]
    return names, sorted(set(unknown))


def tags(resource: dict) -> list[str]:
    return [t for t in TAGS.split(str(resource.get("tags") or "")) if t]


def classify(resource: dict, record: Record, notes: list[str]) -> str:
    vmid, kind = resource.get("vmid"), resource.get("type")
    scratch = SCRATCH_TAG in (t.lower() for t in tags(resource))
    listed = (record.kept or {}).get(vmid)
    if listed is not None and listed.type not in (None, kind):
        notes.append(
            f"Guest {vmid} is {kind} on the Host, but {record.shown}/{GUESTS_FILE} lists {listed.name} as "
            f"{listed.type}: it is not that Kept Guest"
        )
        listed = None
    if listed is not None:
        if scratch:
            notes.append(
                f"Guest {vmid} ({listed.name}) is listed in {record.shown}/{GUESTS_FILE} and tagged {SCRATCH_TAG}; "
                "a Kept Guest carries no scratch tag"
            )
        return "kept"
    return "scratch" if scratch else "stray"


class Reads:
    """GETs for a report: a failed read becomes a note and its default; once the Host stops answering, every later
    read is skipped. Notes name the read, never the error's text. Anything else (e.g. a rejected token) is fatal."""

    def __init__(self, client, notes: list[str]):
        self.client, self.notes, self.lost = client, notes, False

    def get(self, what: str, path: str, params: dict | None = None, default=None):
        if self.lost:
            return default
        try:
            return self.client.get(path, params)
        except ProxmoxError as exc:
            self.notes.append(f"{what}: Proxmox VE read failed ({failure(exc)})")
        except (HostOff, HostTimeout):
            self.lost = True
            self.notes.append(f"{what} and every later read: the Host stopped answering")
        return default


def failure(exc: ProxmoxError) -> str:
    return f"{exc.status}{' permission denied' if exc.status == 403 else ''} on {exc.path}"


def note_record(record: Record, notes: list[str], *, networks: bool) -> None:
    if record.kept is None:
        notes.append(f"no Kept Guests: {record.shown}/{GUESTS_FILE} does not exist")
    if networks and record.networks is None:
        notes.append(f"dual-homing not checked: {record.shown}/{NETWORK_FILE} declares no exclusive_networks")


def off(host: str, record: Record, exc: HopsError) -> dict:
    return {"host": host, "state": "off", "record": record.shown, "notes": [f"the Host is off: {exc}"]}


RESOURCES = ("cluster/resources", {"type": "vm"})


def guests(resources: list[dict], record: Record, notes: list[str]) -> list[dict]:
    """Every Guest in cluster/resources, classified, by ID."""
    out = []
    for r in sorted(resources, key=lambda r: r.get("vmid") or 0):
        if r.get("type") not in TYPES:
            continue
        out.append(
            {
                "id": r.get("vmid"),
                "name": r.get("name"),
                "type": r.get("type"),
                "node": r.get("node"),
                "kind": classify(r, record, notes),
                "state": r.get("status"),
                "template": bool(r.get("template")),
                "tags": tags(r),
            }
        )
    return out


def strays_report(client, host: str, record: Record) -> dict:
    """Stray Guests: neither listed in the record nor tagged scratch."""
    notes: list[str] = []
    note_record(record, notes, networks=False)
    try:
        found = guests(client.get(*RESOURCES) or [], record, notes)
    except HostOff as exc:
        return {**off(host, record, exc), "guests": None}
    return {
        "host": host,
        "state": "on",
        "record": record.shown,
        "guests": [g for g in found if g["kind"] == "stray"],
        "notes": notes,
    }


Upgradable = Callable[[], list[dict]]


def host_report(client, host: str, record: Record, *, upgradable: Upgradable | None = None) -> dict:
    """Nodes (version, pending updates), every Guest (kind, state, newest vzdump archive, dual-homing), and the Kept
    Guests the Host lacks. `upgradable` lists pending updates another way (e.g. over SSH) for a single node whose
    pending updates the token may not read."""
    notes: list[str] = []
    note_record(record, notes, networks=True)
    try:
        nodes = client.get("nodes") or []
    except HostOff as exc:
        return {**off(host, record, exc), "nodes": None, "guests": None, "missing": None}
    reads = Reads(client, notes)
    found = reads.get("guests", *RESOURCES)
    guest_list = guests(found, record, notes) if found is not None else []
    online = sorted(n["node"] for n in nodes if n.get("status") == "online" and n.get("node"))
    node_list = [
        {
            "node": n.get("node"),
            "status": n.get("status"),
            "version": None,
            "updates": None,
        }
        for n in sorted(nodes, key=lambda n: n.get("node") or "")
    ]
    for entry in node_list:
        if entry["node"] not in online:
            continue
        version = reads.get(f"node {entry['node']} version", f"nodes/{entry['node']}/version")
        entry["version"] = version.get("version") if isinstance(version, dict) else None
        entry["updates"] = updates(reads, entry["node"], upgradable if len(nodes) == 1 else None)
    newest = backups(reads, online)
    bridges: dict[str, dict[str, bool] | None] = {}
    now = snapshots.now().timestamp()
    for g in guest_list:
        archive = newest.get(g["id"])
        g["backup"] = (
            None
            if archive is None
            else {
                "volid": archive["volid"],
                "time": iso(archive["ctime"]),
                "age_days": round((now - archive["ctime"]) / DAY, 1),
            }
        )
        g["exclusive_networks"], g["dual_homed"] = None, None
        g["dual_homing_allowed"] = g["id"] in record.may_join_several
        if record.networks is not None and g["node"] in online:
            config = reads.get(f"Guest {g['id']} config", f"nodes/{g['node']}/{g['type']}/{g['id']}/config")
            if isinstance(config, dict):
                if g["node"] not in bridges:
                    bridges[g["node"]] = vlan_aware(
                        reads.get(f"network of node {g['node']}", f"nodes/{g['node']}/network")
                    )
                names, unknown = joined(config, record.networks, bridges[g["node"]])
                g["exclusive_networks"] = names
                g["dual_homed"] = True if len(names) > 1 else (None if unknown else False)
                if g["dual_homed"] is None:
                    notes.append(
                        f"Guest {g['id']} dual-homing unknown: an interface without a tag or trunks is on bridge "
                        f"{', '.join(unknown)} of node {g['node']}, whose VLAN-awareness is not known"
                    )
    on_host = {g["id"] for g in guest_list if g["kind"] == "kept"}
    missing = (
        None
        if found is None
        else [{"id": k.vmid, "name": k.name} for k in (record.kept or {}).values() if k.vmid not in on_host]
    )
    return {
        "host": host,
        "state": "on",
        "record": record.shown,
        "nodes": node_list,
        "guests": guest_list if found is not None else None,
        "missing": missing,
        "notes": notes,
    }


def updates(reads: Reads, node: str, upgradable: Upgradable | None) -> dict | None:
    """The node's pending package updates: the API's list, or `upgradable`'s when the token may not read it (listing
    them needs Sys.Modify, which an audit-only token lacks)."""
    path = f"nodes/{node}/apt/update"
    if reads.lost:
        return None
    try:
        found = reads.client.get(path)
    except ProxmoxError as exc:
        if exc.status != 403 or upgradable is None:
            reads.notes.append(f"node {node} pending updates: Proxmox VE read failed ({failure(exc)})")
            return None
        try:
            return {"source": "ssh", **packages(upgradable())}
        except ConfigError as err:  # SSH is not configured: hops's own text, naming the missing keys
            reads.notes.append(f"node {node} pending updates: the token may not list them, and {err}")
        except HopsError:
            reads.notes.append(f"node {node} pending updates: the token may not list them, and the SSH read failed")
        return None
    except (HostOff, HostTimeout):
        reads.lost = True
        reads.notes.append(f"node {node} pending updates and every later read: the Host stopped answering")
        return None
    rows = [
        {"package": p.get("Package"), "version": p.get("Version"), "old_version": p.get("OldVersion")}
        for p in found or []
    ]
    return {"source": "api", **packages(rows)}


def packages(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda p: p.get("package") or "")
    return {"count": len(rows), "packages": rows}


def apt_upgradable(runner) -> list[dict]:
    """Pending updates from `apt list --upgradable` over SSH, as the SSH user (no sudo): the package lists the node
    refreshes daily."""
    result = runner.command("apt", "list", "--upgradable", timeout=60)
    return [
        {"package": m["package"], "version": m["version"], "old_version": m["old"]}
        for m in (UPGRADABLE.match(line.strip()) for line in result.stdout.splitlines())
        if m
    ]


def backups(reads: Reads, nodes: list[str]) -> dict[int, dict]:
    """The newest vzdump archive of each Guest, on every active storage holding backups (a shared one read once,
    from the first node that reads it)."""
    newest: dict[int, dict] = {}
    shared: set[str] = set()
    for node in nodes:
        storages = reads.get(
            f"node {node} backup storages", f"nodes/{node}/storage", {"content": "backup", "enabled": 1}, []
        )
        for s in sorted(storages or [], key=lambda s: s.get("storage") or ""):
            name = s.get("storage")
            if not name or name in shared:
                continue
            if not s.get("active"):
                reads.notes.append(f"backups on storage {name} not read: it is not active on node {node}")
                continue
            path = f"nodes/{node}/storage/{name}/content"
            content = reads.get(f"backups on storage {name}", path, {"content": "backup"}, None)
            if content is None:
                continue  # failed: another node may still read a shared storage
            if s.get("shared"):
                shared.add(name)  # read once it has been read
            for item in content:
                vmid, ctime = item.get("vmid"), item.get("ctime")
                if isinstance(vmid, int) and isinstance(ctime, int) and ctime > newest.get(vmid, {}).get("ctime", -1):
                    newest[vmid] = {"volid": item.get("volid"), "ctime": ctime}
    return newest


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
