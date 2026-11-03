"""Guests on a Proxmox VE Host: the record (hops.guests) and `hops host`/`hops strays --host <Proxmox VE Host>`,
against FakeProxmox."""

import io
import json
from datetime import UTC, datetime

import pytest
from hops_fakes import FakeProxmox, hosts_yaml, write

from hops import cli, guests, proxmox, snapshots
from hops.errors import ConfigError
from hops.runner import Result, Runner

PVE = "\n      pve:\n        deployer: none\n        capabilities: [proxmox]\n"
ENV = {
    "HOPS_PVE_PROXMOX_URL": "https://pve.example.com:8006",
    "HOPS_PVE_PROXMOX_TOKEN_ID": FakeProxmox.TOKEN,
    "HOPS_PVE_PROXMOX_TOKEN_SECRET": FakeProxmox.SECRET,
    "HOPS_PVE_SSH_HOST": "pve.example.com",
    "HOPS_PVE_SSH_USER": "agent",
}
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
DAY = 86400
GUESTS_YAML = """\
router:
  vmid: 100
  type: qemu
  purpose: routes between the networks
dns:
  vmid: 101
  type: lxc
nas:
  vmid: 102
"""
NETWORK_YAML = """\
bridges:
  vmbr0: {ports: [enp1s0]}
exclusive_networks:
  purpose: networks a Guest joins only one of
  networks:
    lan: {bridge: vmbr0, tag: 10}
    iot: {bridge: vmbr0, tag: 20}
    mgmt: {bridge: vmbr1}
  may_join_several: [100]
"""
RESOURCES = [
    {"vmid": 101, "name": "dns", "type": "lxc", "node": "pve", "status": "running"},
    {"vmid": 100, "name": "router", "type": "qemu", "node": "pve", "status": "running"},
    {"vmid": 200, "name": "try-it", "type": "qemu", "node": "pve", "status": "stopped", "tags": "Scratch;test"},
    {"vmid": 300, "name": "mystery", "type": "lxc", "node": "pve", "status": "running"},
    {"vmid": 301, "name": "leaky", "type": "qemu", "node": "pve", "status": "running"},
    {"id": "storage/pve/local", "type": "storage"},
]
CONFIGS = {
    "nodes/pve/qemu/100/config": {"net0": "virtio=AA,bridge=vmbr0,tag=10", "net1": "virtio=BB,bridge=vmbr0,tag=20"},
    "nodes/pve/lxc/101/config": {"net0": "name=eth0,bridge=vmbr0,tag=10,ip=dhcp"},
    "nodes/pve/qemu/200/config": {"net0": "virtio=CC,bridge=vmbr0,tag=30"},
    "nodes/pve/lxc/300/config": {"net0": "name=eth0,bridge=vmbr1"},
    "nodes/pve/qemu/301/config": {"net0": "virtio=DD,bridge=vmbr1", "net1": "virtio=EE,bridge=vmbr0,tag=20"},
}
NOW_S = int(NOW.timestamp())


def routes() -> dict:
    return {
        "nodes": [{"node": "pve", "status": "online"}],
        "cluster/resources": RESOURCES,
        "nodes/pve/version": {"version": "9.1.4", "release": "9.1"},
        "nodes/pve/network": [
            {"iface": "enp1s0", "type": "eth"},
            {"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1},
            {"iface": "vmbr1", "type": "bridge"},
        ],
        "nodes/pve/apt/update": [{"Package": "pve-manager", "Version": "9.1.5", "OldVersion": "9.1.4"}],
        "nodes/pve/storage": [
            {"storage": "local", "active": 1, "shared": 0},
            {"storage": "nas", "active": 1, "shared": 1},
            {"storage": "usb", "active": 0, "shared": 0},
        ],
        "nodes/pve/storage/local/content": [
            {"volid": "local:backup/vzdump-qemu-100-old.vma.zst", "vmid": 100, "ctime": NOW_S - 9 * DAY},
        ],
        "nodes/pve/storage/nas/content": [
            {"volid": "nas:backup/vzdump-qemu-100-new.vma.zst", "vmid": 100, "ctime": NOW_S - 2 * DAY},
            {"volid": "nas:backup/vzdump-lxc-101.tar.zst", "vmid": 101, "ctime": NOW_S - DAY // 2},
        ],
        **CONFIGS,
    }


class FakeSsh:
    """The ssh process for `apt list --upgradable`: records each remote command and answers with `stdout`."""

    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode, self.remotes = stdout, returncode, []

    def __call__(self, argv, *, input=None, capture=True, timeout=None):
        self.remotes.append(argv[-1])
        return Result(list(argv), self.returncode, self.stdout, "")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(snapshots, "now", lambda: NOW)


@pytest.fixture
def server() -> FakeProxmox:
    return FakeProxmox(routes())


@pytest.fixture
def ssh() -> FakeSsh:
    return FakeSsh()


@pytest.fixture
def pve_repo(repo):
    write(
        repo,
        {
            "hosts.yaml": hosts_yaml(extra=PVE),
            "host/pve/guests.yaml": GUESTS_YAML,
            "host/pve/network.yaml": NETWORK_YAML,
        },
    )
    return repo


@pytest.fixture
def run(pve_repo, server, ssh, host_config, capsys):
    """hops against the fake Proxmox VE Host: run("host", "--host", "pve", "--json")."""

    def go(*argv, cwd=None):
        cfg = proxmox.load_config(pve_repo, cli.config.load_hosts(pve_repo)["pve"], ENV)
        ctx = cli.Context(
            repo_root=pve_repo,
            proxmox=proxmox.Client(cfg, connect=server.connect),
            runner=Runner(host_config, process=ssh),
            env=ENV,
            cwd=cwd or pve_repo,
            stdin=io.StringIO(""),
        )
        code = cli.main(list(argv), ctx=ctx)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return go


def host_json(run, *argv):
    code, out, err = run("host", "--host", "pve", "--json", *argv)
    assert (code, err) == (0, "")
    return json.loads(out)


# hops host


def test_host_reports_nodes_guests_backups_and_dual_homing(run, server):
    report = host_json(run)
    assert report["host"] == "pve" and report["state"] == "on" and report["record"] == "host/pve"
    assert report["nodes"] == [
        {
            "node": "pve",
            "status": "online",
            "version": "9.1.4",
            "updates": {
                "source": "api",
                "count": 1,
                "packages": [{"package": "pve-manager", "version": "9.1.5", "old_version": "9.1.4"}],
            },
        }
    ]
    by_id = {g["id"]: g for g in report["guests"]}
    assert [g["id"] for g in report["guests"]] == [100, 101, 200, 300, 301]
    assert {i: g["kind"] for i, g in by_id.items()} == {
        100: "kept",
        101: "kept",
        200: "scratch",
        300: "stray",
        301: "stray",
    }
    assert by_id[100]["backup"] == {
        "volid": "nas:backup/vzdump-qemu-100-new.vma.zst",
        "time": "2026-10-21T12:00:00Z",
        "age_days": 2.0,
    }
    assert by_id[101]["backup"]["age_days"] == 0.5
    assert by_id[300]["backup"] is None
    assert (by_id[100]["exclusive_networks"], by_id[100]["dual_homed"], by_id[100]["dual_homing_allowed"]) == (
        ["lan", "iot"],
        True,
        True,
    )
    assert (by_id[301]["exclusive_networks"], by_id[301]["dual_homed"], by_id[301]["dual_homing_allowed"]) == (
        ["iot", "mgmt"],
        True,
        False,
    )
    assert (by_id[200]["exclusive_networks"], by_id[200]["dual_homed"]) == ([], False)
    assert by_id[200]["tags"] == ["Scratch", "test"]
    assert report["missing"] == [{"id": 102, "name": "nas"}]
    assert report["notes"] == ["backups on storage usb not read: it is not active on node pve"]
    assert ("nodes/pve/storage", {"content": "backup", "enabled": "1"}) in server.requests
    assert ("nodes/pve/storage/nas/content", {"content": "backup"}) in server.requests


def test_host_text_shows_each_guest(run):
    code, out, err = run("host", "--host", "pve")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == "Host pve: on"
    assert "Pending updates on pve" in lines
    row = next(line for line in lines if line.startswith("100 "))
    assert row.split() == ["100", "router", "qemu", "kept", "running", "pve", "2d", "yes", "(allowed)"]
    assert next(line for line in lines if line.startswith("301 ")).split()[-2:] == ["none", "yes"]
    assert next(line for line in lines if line.startswith("200 ")).split()[-1] == "no"
    assert "Kept Guests not on the Host (listed in host/pve)" in lines


def test_a_switched_off_host_is_reported_as_off_with_exit_0(run, server):
    server.off = True
    code, out, err = run("host", "--host", "pve", "--json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert (report["state"], report["nodes"], report["guests"], report["missing"]) == ("off", None, None, None)
    assert "off or unreachable" in report["notes"][0]
    code, out, _ = run("host", "--host", "pve")
    assert code == 0 and out.startswith("Host pve: off\n")


def test_a_failed_read_becomes_a_note_and_the_report_goes_on(run, server):
    server.routes["nodes/pve/storage/nas/content"] = (500, "storage 'nas' is not online")
    server.routes["nodes/pve/qemu/301/config"] = (403, "Permission check failed")
    report = host_json(run)
    by_id = {g["id"]: g for g in report["guests"]}
    assert by_id[100]["backup"]["volid"] == "local:backup/vzdump-qemu-100-old.vma.zst"
    assert by_id[301]["dual_homed"] is None
    assert "backups on storage nas: Proxmox VE read failed (500 on nodes/pve/storage/nas/content)" in report["notes"]
    assert (
        "Guest 301 config: Proxmox VE read failed (403 permission denied on nodes/pve/qemu/301/config)"
        in report["notes"]
    )
    assert all("not online" not in n for n in report["notes"])  # notes never carry the API's text


def test_pending_updates_fall_back_to_ssh_when_the_token_may_not_list_them(run, server, ssh):
    server.routes["nodes/pve/apt/update"] = (403, "Permission check failed (/nodes/pve, Sys.Modify)")
    ssh.stdout = (
        "Listing...\n"
        "pve-manager/stable 9.1.5 all [upgradable from: 9.1.4]\n"
        "libc6/stable-security 2.41-12 amd64 [upgradable from: 2.41-11]\n"
    )
    updates = host_json(run)["nodes"][0]["updates"]
    assert updates == {
        "source": "ssh",
        "count": 2,
        "packages": [
            {"package": "libc6", "version": "2.41-12", "old_version": "2.41-11"},
            {"package": "pve-manager", "version": "9.1.5", "old_version": "9.1.4"},
        ],
    }
    assert ssh.remotes == ["apt list --upgradable"]  # as the SSH user: no sudo


def test_without_ssh_settings_pending_updates_are_a_note(run, server, monkeypatch):
    server.routes["nodes/pve/apt/update"] = (403, "Permission check failed")
    monkeypatch.setattr(Runner, "command", lambda self, *a, **k: (_ for _ in ()).throw(ConfigError("no SSH")))
    report = host_json(run)
    assert report["nodes"][0]["updates"] is None
    assert "node pve pending updates: the token may not list them, and no SSH" in report["notes"]


def test_a_rejected_token_is_fatal(run, server):
    server.routes["cluster/resources"] = (401, "authentication failure")
    code, out, err = run("host", "--host", "pve", "--json")
    assert (code, out) == (1, "")
    assert "rejected the token" in err and FakeProxmox.SECRET not in err


def test_host_names_a_proxmox_host_but_defaults_to_the_stacks_host(run, server):
    code, out, _ = run("host", "--json")
    assert code == 0 and "docker" in json.loads(out)
    assert server.requests == []  # host-a, which runs Stacks


def test_a_kept_guest_of_the_wrong_type_is_missing(run, pve_repo):
    write(pve_repo, {"host/pve/guests.yaml": "box: {vmid: 300, type: qemu}\n"})
    report = host_json(run)
    assert {g["id"]: g["kind"] for g in report["guests"]}[300] == "stray"
    assert report["missing"] == [{"id": 300, "name": "box"}]
    assert any("Guest 300 is lxc on the Host" in n for n in report["notes"])


def test_record_option_names_another_directory(run, pve_repo):
    write(pve_repo, {"elsewhere/guests.yaml": "router: {vmid: 300}\n"})
    report = host_json(run, "--record", "elsewhere")
    assert report["record"] == "elsewhere"
    assert {g["id"]: g["kind"] for g in report["guests"]}[300] == "kept"
    assert "dual-homing not checked: elsewhere/network.yaml declares no exclusive_networks" in report["notes"]
    assert all(g["dual_homed"] is None for g in report["guests"])


def test_without_a_record_no_guest_is_kept(run, pve_repo):
    (pve_repo / "host/pve/guests.yaml").unlink()
    report = host_json(run)
    assert {g["kind"] for g in report["guests"]} == {"scratch", "stray"}
    assert "no Kept Guests: host/pve/guests.yaml does not exist" in report["notes"]


def test_record_on_a_stacks_host_is_refused(run):
    code, _, err = run("host", "--host", "host-a", "--record", "x")
    assert code == 2 and "--record applies only" in err


def test_a_host_without_stacks_or_proxmox_is_refused(run, pve_repo, server):
    write(
        pve_repo,
        {"hosts.yaml": hosts_yaml(extra=PVE + "      bare:\n        deployer: none\n        capabilities: []\n")},
    )
    code, _, err = run("host", "--host", "bare")
    assert code == 2 and "needs a Host with the proxmox capability" in err
    assert server.requests == []


# hops strays


def test_strays_lists_only_stray_guests(run, server):
    code, out, err = run("strays", "--host", "pve", "--json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert [(g["id"], g["kind"]) for g in report["guests"]] == [(300, "stray"), (301, "stray")]
    assert report["state"] == "on" and report["notes"] == []
    assert server.paths() == ["cluster/resources"]


def test_strays_text(run):
    code, out, _ = run("strays", "--host", "pve")
    assert code == 0
    assert "Stray Guests (against host/pve)" in out
    assert "mystery" in out and "router" not in out


def test_strays_on_a_switched_off_host_is_exit_0(run, server):
    server.off = True
    code, out, err = run("strays", "--host", "pve", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out)["state"] == "off"


def test_strays_apply_is_refused_on_a_proxmox_host(run, server):
    code, _, err = run("strays", "--host", "pve", "--apply")
    assert code == 2 and "never removes them" in err
    assert server.requests == []


# The record


def load(tmp_path, guests_yaml=None, network_yaml=None):
    files = {}
    if guests_yaml is not None:
        files["guests.yaml"] = guests_yaml
    if network_yaml is not None:
        files["network.yaml"] = network_yaml
    write(tmp_path / "rec", files)
    return guests.load_record(tmp_path / "rec", tmp_path)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("router: 100\n", "router: must be a mapping"),
        ("router: {type: qemu}\n", "router.vmid: must be the Guest's ID"),
        ("router: {vmid: 99}\n", "router.vmid: must be the Guest's ID"),
        ("router: {vmid: 100, type: vm}\n", "router.type: must be one of qemu, lxc"),
        ("a: {vmid: 100}\nb: {vmid: 100}\n", "b.vmid: 100 is also a's"),
        ("- 100\n", "must be a mapping"),
        ("a: [\n", "not valid YAML"),
    ],
)
def test_bad_guests_yaml_is_a_config_error(tmp_path, text, problem):
    with pytest.raises(ConfigError, match=problem.replace("(", r"\(").replace(".", r"\.")):
        load(tmp_path, guests_yaml=text)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("exclusive_networks: {networks: {lan: {bridge: vmbr0}}}\n", "two or more"),
        ("exclusive_networks: {networks: {a: {bridge: vmbr0}, b: {}}}\n", "networks.b: must give a bridge"),
        ("exclusive_networks: {networks: {a: {bridge: v0, tag: 0}, b: {bridge: v1}}}\n", "a.tag: must be a VLAN"),
        ("exclusive_networks: {networks: {a: {bridge: v0}, b: {bridge: v0}}}\n", "the same bridge and tag"),
        ("exclusive_networks: {networks: {a: {bridge: v0, x: 1}, b: {bridge: v1}}}\n", "only bridge, tag"),
        ("exclusive_networks: {networks: {a: {bridge: v0}, b: {bridge: v1}}, may_join_several: [x]}\n", "IDs"),
    ],
)
def test_bad_exclusive_networks_is_a_config_error(tmp_path, text, problem):
    with pytest.raises(ConfigError, match=problem):
        load(tmp_path, network_yaml=text)


def test_network_yaml_without_exclusive_networks_checks_nothing(tmp_path):
    record = load(tmp_path, guests_yaml="", network_yaml="bridges: {}\n")
    assert (record.kept, record.networks) == ({}, None)


def test_a_kept_guest_tagged_scratch_stays_kept_with_a_note(tmp_path):
    record = load(tmp_path, guests_yaml="router: {vmid: 100}\n")
    notes = []
    assert guests.classify({"vmid": 100, "type": "qemu", "tags": "scratch"}, record, notes) == "kept"
    assert notes == [
        "Guest 100 (router) is listed in rec/guests.yaml and tagged scratch; a Kept Guest carries no scratch tag"
    ]


def test_a_guest_of_the_other_type_is_not_the_kept_one(tmp_path):
    record = load(tmp_path, guests_yaml="router: {vmid: 100, type: qemu}\n")
    notes = []
    assert guests.classify({"vmid": 100, "type": "lxc"}, record, notes) == "stray"
    assert "is lxc on the Host" in notes[0]


def test_interfaces_reads_bridge_and_tag():
    config = {"net0": "virtio=AA,bridge=vmbr0,tag=20,firewall=1", "net1": "virtio=BB,bridge=vmbr1", "net2": "x=1"}
    assert guests.interfaces(config) == [("vmbr0", 20), ("vmbr1", None)]


# Review follow-ups


def test_trunk_vlans_count_as_network_membership():
    config = {"net0": "virtio=AA,bridge=vmbr0,trunks=10;20-21", "net1": "virtio=BB,bridge=vmbr1,tag=5,trunks=7"}
    assert guests.interfaces(config) == [
        ("vmbr0", None),
        ("vmbr0", 10),
        ("vmbr0", 20),
        ("vmbr0", 21),
        ("vmbr1", 5),
        ("vmbr1", 7),
    ]


def test_a_trunked_guest_is_dual_homed(run, server):
    server.routes["nodes/pve/lxc/300/config"] = {"net0": "name=eth0,bridge=vmbr0,tag=10,trunks=20"}
    by_id = {g["id"]: g for g in host_json(run)["guests"]}
    assert (by_id[300]["exclusive_networks"], by_id[300]["dual_homed"]) == (["lan", "iot"], True)


def test_the_guest_reports_are_scrubbed_of_the_secret(run, server):
    secret = FakeProxmox.SECRET
    server.routes["cluster/resources"] = [
        {"vmid": 300, "name": f"x-{secret}", "type": "lxc", "node": "pve", "status": "running", "tags": secret}
    ]
    for argv in (("host",), ("host", "--json"), ("strays",), ("strays", "--json")):
        code, out, err = run(*argv[:1], "--host", "pve", *argv[1:])
        assert code == 0 and secret not in out + err
        assert "[REDACTED]" in out


def test_a_host_off_on_the_first_read_is_scrubbed_in_the_report(run, server):
    secret = FakeProxmox.SECRET

    def leaky(target, headers):
        raise proxmox.HostOff(f"Proxmox VE is off or unreachable ({secret})")

    server.get = leaky
    for argv in (("host",), ("host", "--json"), ("strays",), ("strays", "--json")):
        code, out, err = run(*argv[:1], "--host", "pve", *argv[1:])
        assert code == 0 and secret not in out + err
        assert "off" in out


def test_a_shared_storage_is_read_on_the_next_node_when_it_failed_or_was_inactive(run, server):
    server.routes["nodes"] = [{"node": "pve", "status": "online"}, {"node": "pve2", "status": "online"}]
    server.routes["nodes/pve2/version"] = {"version": "9.1.4"}
    server.routes["nodes/pve2/apt/update"] = []
    server.routes["nodes/pve/storage/nas/content"] = (500, "storage 'nas' is not online")
    server.routes["nodes/pve2/storage"] = [
        {"storage": "nas", "active": 1, "shared": 1},
        {"storage": "usb", "active": 1, "shared": 1},
    ]
    server.routes["nodes/pve2/storage/nas/content"] = routes()["nodes/pve/storage/nas/content"]
    server.routes["nodes/pve2/storage/usb/content"] = [
        {"volid": "usb:backup/vzdump-lxc-300.tar.zst", "vmid": 300, "ctime": NOW_S - DAY}
    ]
    by_id = {g["id"]: g for g in host_json(run)["guests"]}
    assert by_id[100]["backup"]["volid"] == "nas:backup/vzdump-qemu-100-new.vma.zst"
    assert by_id[300]["backup"]["volid"] == "usb:backup/vzdump-lxc-300.tar.zst"


# Re-review follow-ups


def run_built_in_cli(pve_repo, server, monkeypatch, capsys, env, *argv):
    """hops with the Proxmox VE client built by the CLI from `env`, as in real use."""
    monkeypatch.setattr(proxmox, "https_connect", server.connect)
    ctx = cli.Context(repo_root=pve_repo, env=env, cwd=pve_repo, stdin=io.StringIO(""))
    code = cli.main(list(argv), ctx=ctx)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("command", ["host", "strays"])
def test_an_error_naming_a_url_that_holds_the_secret_is_scrubbed(pve_repo, server, monkeypatch, capsys, command):
    secret = FakeProxmox.SECRET
    server.routes["nodes"] = (401, "authentication failure")
    server.routes["cluster/resources"] = (401, "authentication failure")
    env = {**ENV, "HOPS_PVE_PROXMOX_URL": f"https://{secret}.example.com:8006"}
    code, out, err = run_built_in_cli(pve_repo, server, monkeypatch, capsys, env, command, "--host", "pve")
    assert code == 1 and "[REDACTED]" in err
    assert secret not in out + err


@pytest.mark.parametrize("command", ["host", "strays"])
def test_a_record_error_naming_the_secret_is_scrubbed(pve_repo, server, monkeypatch, capsys, command):
    secret = FakeProxmox.SECRET
    write(pve_repo, {"host/pve/guests.yaml": f"{secret}:\n  vmid: nope\n"})
    code, out, err = run_built_in_cli(pve_repo, server, monkeypatch, capsys, ENV, command, "--host", "pve")
    assert code == 2 and "[REDACTED]" in err
    assert secret not in out + err


def test_an_untagged_nic_on_a_vlan_aware_bridge_joins_every_vlan_network_on_it(run, server):
    server.routes["nodes/pve/lxc/300/config"] = {"net0": "name=eth0,bridge=vmbr0"}
    by_id = {g["id"]: g for g in host_json(run)["guests"]}
    assert (by_id[300]["exclusive_networks"], by_id[300]["dual_homed"]) == (["lan", "iot"], True)


def test_an_untagged_nic_on_a_bridge_that_is_not_vlan_aware_joins_only_its_untagged_traffic(run, server):
    server.routes["nodes/pve/network"] = [
        {"iface": "vmbr0", "type": "bridge"},
        {"iface": "vmbr1", "type": "bridge", "bridge_vlan_aware": 1},
    ]
    server.routes["nodes/pve/lxc/300/config"] = {"net0": "name=eth0,bridge=vmbr0"}
    by_id = {g["id"]: g for g in host_json(run)["guests"]}
    assert (by_id[300]["exclusive_networks"], by_id[300]["dual_homed"]) == ([], False)
    # vmbr1 is VLAN-aware, but mgmt is its only exclusive network: an untagged NIC on it joins only that one.
    assert (by_id[301]["exclusive_networks"], by_id[301]["dual_homed"]) == (["iot", "mgmt"], True)


def test_dual_homing_is_unknown_when_vlan_awareness_cannot_be_read(run, server):
    server.routes["nodes/pve/network"] = (403, "Permission check failed")
    server.routes["nodes/pve/lxc/300/config"] = {"net0": "name=eth0,bridge=vmbr0"}
    report = host_json(run)
    by_id = {g["id"]: g for g in report["guests"]}
    assert (by_id[300]["exclusive_networks"], by_id[300]["dual_homed"]) == ([], None)
    # A tagged NIC needs no VLAN-awareness: Guest 100 is still known to be dual-homed.
    assert by_id[100]["dual_homed"] is True
    assert "network of node pve: Proxmox VE read failed (403 permission denied on nodes/pve/network)" in report["notes"]
    assert any(n.startswith("Guest 300 dual-homing unknown") for n in report["notes"])
    code, out, _ = run("host", "--host", "pve")
    row = next(line for line in out.splitlines() if line.startswith("300 "))
    assert row.split()[-1] == "unknown"


def test_an_unexpected_error_holding_the_secret_is_scrubbed_without_a_traceback(run, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError(f"bad {FakeProxmox.SECRET}")

    monkeypatch.setattr(guests, "host_report", broken)
    code, out, err = run("host", "--host", "pve")
    assert (code, err) == (1, "hops: error: ValueError: bad [REDACTED]\n")
