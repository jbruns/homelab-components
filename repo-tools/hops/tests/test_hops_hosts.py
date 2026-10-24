"""Host selection: --host (before or after the command), else the working directory, else the only Host;
and the capability gates on commands a Host cannot serve."""
import json

import pytest
from hops_fakes import NAS_2, FakeTrueNAS, write

from hops import cli, truenas


@pytest.fixture
def two_hosts(repo):
    write(repo, NAS_2)
    return repo


@pytest.fixture
def run(two_hosts, runner, capsys):
    """hops in the two-Host repo, from `cwd` (default: the repo root)."""
    def run(*argv, cwd=None, client=None):
        ctx = cli.Context(repo_root=two_hosts, runner=runner, truenas=client, env={}, cwd=cwd or two_hosts)
        code = cli.main(list(argv), ctx=ctx)
        captured = capsys.readouterr()
        return code, captured.out, captured.err, ctx

    return run


@pytest.mark.parametrize("argv", [["--host", "nas-2", "restart", "sonarr"], ["restart", "sonarr", "--host", "nas-2"]])
def test_host_flag_picks_the_host_and_its_stack_root(run, fake_host, argv):
    code, _, err, ctx = run(*argv)
    assert code == 2 and "unknown Stack or Service 'sonarr'" in err
    assert ctx.host.name == "nas-2" and fake_host.calls == []


def test_the_host_whose_stack_root_holds_the_working_directory_is_picked(run, two_hosts, fake_host):
    fake_host.respond("restart")
    code, _, err, ctx = run("restart", "web", cwd=two_hosts / "hosts" / "nas-2" / "web")
    assert (code, err) == (0, "")
    assert ctx.host.name == "nas-2" and fake_host.docker_calls == [["restart", "web"]]


def test_with_several_hosts_and_none_picked_it_lists_them(run, fake_host):
    code, _, err, _ = run("restart", "web")
    assert code == 2
    assert "pick one of: truenas, nas-2 (e.g. --host)" in err
    assert fake_host.calls == []


def test_an_unknown_host_is_refused(run, fake_host):
    code, _, err, _ = run("--host", "nas-3", "restart", "web")
    assert code == 2 and "no Host 'nas-3' in hosts.yaml; Hosts: truenas, nas-2" in err
    assert fake_host.calls == []


def test_truenas_call_runs_on_the_host_with_truenas(run, two_hosts):
    server = FakeTrueNAS({"system.info": lambda: {"hostname": "tn"}})
    client = truenas.Client(truenas.TrueNASConfig(url="wss://tn.example/api/current", api_key=FakeTrueNAS.KEY),
                            connect=server.connect)
    code, out, err, _ = run("truenas", "call", "system.info", "--json", cwd=two_hosts / "tn", client=client)
    assert (code, err) == (0, "")
    assert json.loads(out) == {"hostname": "tn"}


@pytest.mark.parametrize("argv, what", [
    (["truenas", "call", "system.info"], "hops truenas call needs a Host with the truenas capability"),
    (["snapshot", "list"], "hops snapshot (Rollback Snapshots) needs a Host with the truenas and zfs capabilities"),
    (["snapshot", "take", "--issue", "638"], "hops snapshot (Rollback Snapshots) needs a Host with the truenas and zfs capabilities"),
    (["snapshot", "release", "luna/x@hops-638-x"], "hops snapshot (Rollback Snapshots) needs a Host with the truenas and zfs capabilities"),
    (["volume", "move", "a_b_data", "c_d_data", "--issue", "638", "--take-snapshot"],
     "--take-snapshot (a Rollback Snapshot) needs a Host with the truenas and zfs capabilities"),
])
def test_truenas_and_zfs_commands_are_refused_on_a_host_without_them(run, fake_host, argv, what):
    code, _, err, _ = run(*argv, "--host", "nas-2")
    assert code == 2
    assert err == f"hops: error: {what}; Host 'nas-2' has none (hosts.yaml)\n"
    assert fake_host.calls == []
