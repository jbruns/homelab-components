import json
import textwrap

import pytest

from homelab_hosts import HostsError, SshSettings, TrueNasSettings, find_repo_root, host_settings, load_hosts
from homelab_hosts.__main__ import main


def write_repo(root, hosts_yaml, stack_roots=("nc8kg1101",)):
    (root / "hosts.yaml").write_text(textwrap.dedent(hosts_yaml))
    for stack_root in stack_roots:
        (root / stack_root).mkdir(parents=True, exist_ok=True)
    return root


MINIMAL = """\
    version: 1
    hosts:
      nc8kg1101:
        stack_root: nc8kg1101
        deployer: doco-cd
        capabilities: []
"""


def test_a_minimal_hosts_file_gets_the_documented_defaults(tmp_path):
    hosts = load_hosts(write_repo(tmp_path, MINIMAL))

    assert hosts.default_branch == "main"
    host = hosts["nc8kg1101"]
    assert host.name == "nc8kg1101"
    assert host.stack_root == "nc8kg1101"
    assert host.path == tmp_path / "nc8kg1101"
    assert host.deployer == "doco-cd"
    assert host.doco_cd.stack == "doco-cd"
    assert host.doco_cd.api_secret == "doco-cd/api_secret.sops.txt"
    assert host.doco_cd.address == "doco-cd:80"
    assert host.doco_cd.network == "metrics"


def test_doco_cd_overrides_and_default_branch_replace_only_what_they_name(tmp_path):
    hosts = load_hosts(
        write_repo(
            tmp_path,
            """\
            version: 1
            default_branch: trunk
            hosts:
              truenas:
                stack_root: tn
                deployer: doco-cd
                capabilities: [truenas, zfs]
                doco_cd:
                  network: monitoring
            """,
            stack_roots=["tn"],
        )
    )

    assert hosts.default_branch == "trunk"
    doco_cd = hosts["truenas"].doco_cd
    assert (doco_cd.stack, doco_cd.address, doco_cd.network) == ("doco-cd", "doco-cd:80", "monitoring")


def host_entry(**overrides):
    entry = {"stack_root": "nc8kg1101", "deployer": "doco-cd", "capabilities": "[]"}
    entry.update(overrides)
    return "".join(f"    {key}: {value}\n" for key, value in entry.items() if value is not None)


@pytest.mark.parametrize(
    ("hosts_yaml", "expected"),
    [
        ("version: 1\nhosts:\n  nc8kg1101:\n" + host_entry() + "colour: blue\n", "colour: unknown key"),
        ("version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(user="root"), "hosts.nc8kg1101.user: unknown key"),
        (
            "version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(doco_cd="{port: 80}"),
            "hosts.nc8kg1101.doco_cd.port: unknown key",
        ),
        ("version: 1\nhosts:\n  NC8KG1101:\n" + host_entry(), "hosts.NC8KG1101: bad Host name"),
        ("version: 1\nhosts:\n  nc8kg_1101:\n" + host_entry(), "hosts.nc8kg_1101: bad Host name"),
        (
            "version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(capabilities="[truenas, zfs, gpu]"),
            "unknown capability 'gpu'",
        ),
        ("version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(deployer="portainer"), "unknown deployer 'portainer'"),
        ("version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(stack_root=None), "hosts.nc8kg1101.stack_root: missing"),
        (
            "version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(stack_root="stacks"),
            "'stacks' is not a directory under",
        ),
        ("version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(stack_root="../elsewhere"), "stay inside it"),
        (
            "version: 1\nhosts:\n  nc8kg1101:\n" + host_entry() + "  other:\n" + host_entry(stack_root="nc8kg1101/"),
            "is already the stack root of Host 'nc8kg1101'",
        ),
        ("version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(deployer="[doco-cd]"), "unknown deployer ['doco-cd']"),
        (
            "version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(capabilities="[{truenas: 1}]"),
            "unknown capability {'truenas': 1}",
        ),
        ("version: 2\nhosts:\n  nc8kg1101:\n" + host_entry(), "version: 2 is not supported"),
        ("version: 1\nhosts: {}\n", "hosts: must map at least one Host"),
    ],
    ids=[
        "unknown top-level key",
        "unknown Host key",
        "unknown doco_cd key",
        "uppercase Host name",
        "underscore in Host name",
        "unknown capability",
        "unknown deployer",
        "missing stack_root",
        "stack_root not on disk",
        "stack_root outside the repo",
        "duplicate stack root",
        "deployer not a string",
        "capability not a string",
        "unsupported version",
        "no Hosts",
    ],
)
def test_an_invalid_hosts_file_is_rejected_naming_the_problem(tmp_path, hosts_yaml, expected):
    write_repo(tmp_path, hosts_yaml)

    with pytest.raises(HostsError) as e:
        load_hosts(tmp_path)

    assert expected in str(e.value)
    assert str(tmp_path / "hosts.yaml") in str(e.value)


def test_every_problem_in_a_hosts_file_is_reported_at_once(tmp_path):
    write_repo(tmp_path, "version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(deployer="portainer", capabilities="[gpu]"))

    with pytest.raises(HostsError) as e:
        load_hosts(tmp_path)

    assert len(e.value.problems) == 2


def test_the_repo_root_is_found_by_walking_up_to_hosts_yaml(tmp_path, monkeypatch):
    write_repo(tmp_path, MINIMAL)
    nested = tmp_path / "nc8kg1101" / "grafana"
    nested.mkdir()

    assert find_repo_root(nested) == tmp_path
    monkeypatch.chdir(nested)
    assert load_hosts().root == tmp_path


def test_a_tree_without_hosts_yaml_has_no_repo_root(tmp_path):
    with pytest.raises(HostsError, match="no hosts.yaml"):
        find_repo_root(tmp_path)


TWO_HOSTS = """\
    version: 1
    hosts:
      truenas:
        stack_root: tn
        deployer: doco-cd
        capabilities: [truenas, zfs]
      nc8kg1101:
        stack_root: nc8kg1101
        deployer: doco-cd
        capabilities: []
"""


@pytest.fixture
def two_hosts(tmp_path):
    return load_hosts(write_repo(tmp_path, TWO_HOSTS, stack_roots=["tn", "nc8kg1101"]))


def test_an_explicit_host_name_wins_over_the_path(two_hosts):
    assert two_hosts.pick("truenas", path=two_hosts.root / "nc8kg1101" / "grafana").name == "truenas"


def test_an_unknown_host_name_is_rejected_listing_the_hosts(two_hosts):
    with pytest.raises(HostsError, match="no Host 'nas'.*truenas, nc8kg1101"):
        two_hosts.pick("nas")


def test_a_path_picks_the_host_whose_stack_root_contains_it(two_hosts, monkeypatch):
    assert two_hosts.pick(path=two_hosts.root / "nc8kg1101" / "grafana" / "compose.yaml").name == "nc8kg1101"
    monkeypatch.chdir(two_hosts.root)
    assert two_hosts.pick(path="tn/plex").name == "truenas"


def test_with_several_hosts_and_no_name_or_matching_path_the_error_lists_the_hosts(two_hosts):
    with pytest.raises(HostsError, match="truenas, nc8kg1101"):
        two_hosts.pick(path=two_hosts.root / "docs")
    with pytest.raises(HostsError, match="truenas, nc8kg1101"):
        two_hosts.pick()


def test_the_only_host_is_picked_when_nothing_else_decides(tmp_path):
    hosts = load_hosts(write_repo(tmp_path, MINIMAL))

    assert hosts.pick().name == "nc8kg1101"
    assert hosts.pick(path=tmp_path / "docs").name == "nc8kg1101"


def test_a_host_has_only_the_capabilities_it_declares(two_hosts):
    assert two_hosts["truenas"].has("zfs")
    assert not two_hosts["nc8kg1101"].has("truenas")


def test_asking_for_an_unknown_capability_is_an_error_not_false(two_hosts):
    with pytest.raises(HostsError, match="unknown capability 'snapshots'"):
        two_hosts["truenas"].has("snapshots")


def test_settings_come_from_env_keys_named_for_the_host(tmp_path):
    hosts = load_hosts(
        write_repo(
            tmp_path,
            "version: 1\nhosts:\n  nc8-kg:\n" + host_entry(),
        )
    )
    env = {"HOPS_NC8_KG_SSH_HOST": "nc8.example", "HOPS_NC8_KG_SSH_USER": "ops", "HOPS_NC8_KG_SSH_KEY": "~/.ssh/k"}

    settings = host_settings(env, hosts["nc8-kg"])

    assert settings.ssh == SshSettings(host="nc8.example", user="ops", key="~/.ssh/k")
    assert settings.truenas is None


def test_a_truenas_host_also_gets_its_truenas_settings(two_hosts):
    env = {
        "HOPS_TRUENAS_SSH_HOST": "nas.example",
        "HOPS_TRUENAS_TRUENAS_URL": "https://nas.example",
        "HOPS_TRUENAS_TRUENAS_API_KEY": "fake-api-key",
        "HOPS_TRUENAS_TRUENAS_INSECURE": "1",
    }

    settings = host_settings(env, two_hosts["truenas"])

    assert settings.ssh == SshSettings(host="nas.example", user=None, key=None)
    assert settings.truenas == TrueNasSettings(url="https://nas.example", api_key="fake-api-key", insecure=True)


def test_missing_settings_are_named_by_key_without_revealing_values(two_hosts):
    env = {"HOPS_TRUENAS_TRUENAS_API_KEY": "fake-api-key", "HOPS_TRUENAS_TRUENAS_INSECURE": "maybe"}

    with pytest.raises(HostsError) as e:
        host_settings(env, two_hosts["truenas"])

    message = str(e.value)
    assert "HOPS_TRUENAS_SSH_HOST" in message
    assert "HOPS_TRUENAS_TRUENAS_URL" in message
    assert "HOPS_TRUENAS_TRUENAS_INSECURE" in message
    assert "fake-api-key" not in message and "maybe" not in message


def test_the_cli_validates_and_lists_the_hosts_of_the_current_repo(two_hosts, monkeypatch, capsys):
    monkeypatch.chdir(two_hosts.root / "tn")

    assert main(["--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [h["name"] for h in listed["hosts"]] == ["truenas", "nc8kg1101"]


def test_the_cli_fails_on_an_invalid_hosts_file(tmp_path, monkeypatch, capsys):
    write_repo(tmp_path, "version: 1\nhosts:\n  nc8kg1101:\n" + host_entry(deployer="portainer"))
    monkeypatch.chdir(tmp_path)

    assert main([]) == 1
    assert "unknown deployer" in capsys.readouterr().err
