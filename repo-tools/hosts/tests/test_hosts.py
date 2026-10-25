import json
import textwrap

import pytest

from homelab_hosts import (
    DocoCd,
    HostsError,
    SshSettings,
    TrueNasSettings,
    find_repo_root,
    host_settings,
    load_hosts,
    ssh_settings,
    truenas_settings,
)
from homelab_hosts.__main__ import main


def write_repo(root, hosts_yaml, stack_roots=("host-b",)):
    (root / "hosts.yaml").write_text(textwrap.dedent(hosts_yaml))
    for stack_root in stack_roots:
        (root / stack_root).mkdir(parents=True, exist_ok=True)
    return root


MINIMAL = """\
    version: 1
    hosts:
      host-b:
        stack_root: host-b
        deployer: doco-cd
        capabilities: []
"""


def test_a_minimal_hosts_file_gets_the_documented_defaults(tmp_path):
    hosts = load_hosts(write_repo(tmp_path, MINIMAL))

    assert hosts.default_branch == "main"
    assert hosts.validate == ()
    host = hosts["host-b"]
    assert host.name == "host-b"
    assert host.stack_root == "host-b"
    assert host.path == tmp_path / "host-b"
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
              host-a:
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
    doco_cd = hosts["host-a"].doco_cd
    assert (doco_cd.stack, doco_cd.address, doco_cd.network) == ("doco-cd", "doco-cd:80", "monitoring")


def test_validate_is_the_repos_validation_argv(tmp_path):
    hosts = load_hosts(write_repo(tmp_path, MINIMAL + "    validate: [python3, tools/validate.py, compose]\n"))

    assert hosts.validate == ("python3", "tools/validate.py", "compose")


def host_entry(**overrides):
    entry = {"stack_root": "host-b", "deployer": "doco-cd", "capabilities": "[]"}
    entry.update(overrides)
    return "".join(f"    {key}: {value}\n" for key, value in entry.items() if value is not None)


@pytest.mark.parametrize(
    ("hosts_yaml", "expected"),
    [
        ("version: 1\nhosts:\n  host-b:\n" + host_entry() + "colour: blue\n", "colour: unknown key"),
        ("version: 1\nhosts:\n  host-b:\n" + host_entry(user="root"), "hosts.host-b.user: unknown key"),
        (
            "version: 1\nhosts:\n  host-b:\n" + host_entry(doco_cd="{port: 80}"),
            "hosts.host-b.doco_cd.port: unknown key",
        ),
        ("version: 1\nhosts:\n  HOST-B:\n" + host_entry(), "hosts.HOST-B: bad Host name"),
        ("version: 1\nhosts:\n  host_b:\n" + host_entry(), "hosts.host_b: bad Host name"),
        (
            "version: 1\nhosts:\n  host-b:\n" + host_entry(capabilities="[truenas, zfs, gpu]"),
            "unknown capability 'gpu'",
        ),
        ("version: 1\nhosts:\n  host-b:\n" + host_entry(deployer="portainer"), "unknown deployer 'portainer'"),
        ("version: 1\nhosts:\n  host-b:\n" + host_entry(stack_root=None), "hosts.host-b.stack_root: missing"),
        (
            "version: 1\nhosts:\n  host-b:\n" + host_entry(stack_root="stacks"),
            "'stacks' is not a directory under",
        ),
        ("version: 1\nhosts:\n  host-b:\n" + host_entry(stack_root="../elsewhere"), "stay inside it"),
        (
            "version: 1\nhosts:\n  host-b:\n" + host_entry() + "  other:\n" + host_entry(stack_root="host-b/"),
            "is already the stack root of Host 'host-b'",
        ),
        ("version: 1\nhosts:\n  host-b:\n" + host_entry(deployer="[doco-cd]"), "unknown deployer ['doco-cd']"),
        (
            "version: 1\nhosts:\n  host-b:\n" + host_entry(capabilities="[{truenas: 1}]"),
            "unknown capability {'truenas': 1}",
        ),
        ("version: 2\nhosts:\n  host-b:\n" + host_entry(), "version: 2 is not supported"),
        ("version: 1\nhosts: {}\n", "hosts: must map at least one Host"),
        (
            "version: 1\nvalidate: make check\nhosts:\n  host-b:\n" + host_entry(),
            "validate: must be a non-empty list",
        ),
        ("version: 1\nvalidate: []\nhosts:\n  host-b:\n" + host_entry(), "validate: must be a non-empty list"),
        ("version: 1\nvalidate: [make, 1]\nhosts:\n  host-b:\n" + host_entry(), "validate: every argument"),
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
        "validate not a list",
        "validate empty",
        "validate argument not a string",
    ],
)
def test_an_invalid_hosts_file_is_rejected_naming_the_problem(tmp_path, hosts_yaml, expected):
    write_repo(tmp_path, hosts_yaml)

    with pytest.raises(HostsError) as e:
        load_hosts(tmp_path)

    assert expected in str(e.value)
    assert str(tmp_path / "hosts.yaml") in str(e.value)


def test_every_problem_in_a_hosts_file_is_reported_at_once(tmp_path):
    write_repo(tmp_path, "version: 1\nhosts:\n  host-b:\n" + host_entry(deployer="portainer", capabilities="[gpu]"))

    with pytest.raises(HostsError) as e:
        load_hosts(tmp_path)

    assert len(e.value.problems) == 2


def test_the_repo_root_is_found_by_walking_up_to_hosts_yaml(tmp_path, monkeypatch):
    write_repo(tmp_path, MINIMAL)
    nested = tmp_path / "host-b" / "grafana"
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
      host-a:
        stack_root: tn
        deployer: doco-cd
        capabilities: [truenas, zfs]
      host-b:
        stack_root: host-b
        deployer: doco-cd
        capabilities: []
"""


@pytest.fixture
def two_hosts(tmp_path):
    return load_hosts(write_repo(tmp_path, TWO_HOSTS, stack_roots=["tn", "host-b"]))


def test_an_explicit_host_name_wins_over_the_path(two_hosts):
    assert two_hosts.pick("host-a", path=two_hosts.root / "host-b" / "grafana").name == "host-a"


def test_an_unknown_host_name_is_rejected_listing_the_hosts(two_hosts):
    with pytest.raises(HostsError, match="no Host 'nas'.*host-a, host-b"):
        two_hosts.pick("nas")


def test_a_path_picks_the_host_whose_stack_root_contains_it(two_hosts, monkeypatch):
    assert two_hosts.pick(path=two_hosts.root / "host-b" / "grafana" / "compose.yaml").name == "host-b"
    monkeypatch.chdir(two_hosts.root)
    assert two_hosts.pick(path="tn/plex").name == "host-a"


def test_with_several_hosts_and_no_name_or_matching_path_the_error_lists_the_hosts(two_hosts):
    with pytest.raises(HostsError, match="host-a, host-b"):
        two_hosts.pick(path=two_hosts.root / "docs")
    with pytest.raises(HostsError, match="host-a, host-b"):
        two_hosts.pick()


def test_the_only_host_is_picked_when_nothing_else_decides(tmp_path):
    hosts = load_hosts(write_repo(tmp_path, MINIMAL))

    assert hosts.pick().name == "host-b"
    assert hosts.pick(path=tmp_path / "docs").name == "host-b"


def test_a_host_has_only_the_capabilities_it_declares(two_hosts):
    assert two_hosts["host-a"].has("zfs")
    assert not two_hosts["host-b"].has("truenas")


def test_asking_for_an_unknown_capability_is_an_error_not_false(two_hosts):
    with pytest.raises(HostsError, match="unknown capability 'snapshots'"):
        two_hosts["host-a"].has("snapshots")


def test_settings_come_from_env_keys_named_for_the_host(tmp_path):
    hosts = load_hosts(
        write_repo(
            tmp_path,
            "version: 1\nhosts:\n  my-nas:\n" + host_entry(),
        )
    )
    env = {"HOPS_MY_NAS_SSH_HOST": "nas.example", "HOPS_MY_NAS_SSH_USER": "ops", "HOPS_MY_NAS_SSH_KEY": "~/.ssh/k"}

    settings = host_settings(env, hosts["my-nas"])

    assert settings.ssh == SshSettings(host="nas.example", user="ops", key="~/.ssh/k")
    assert settings.truenas is None


def test_a_truenas_host_also_gets_its_truenas_settings(two_hosts):
    env = {
        "HOPS_HOST_A_SSH_HOST": "nas.example",
        "HOPS_HOST_A_TRUENAS_URL": "https://nas.example",
        "HOPS_HOST_A_TRUENAS_API_KEY": "fake-api-key",
        "HOPS_HOST_A_TRUENAS_INSECURE": "1",
    }

    settings = host_settings(env, two_hosts["host-a"])

    assert settings.ssh == SshSettings(host="nas.example", user=None, key=None)
    assert settings.truenas == TrueNasSettings(url="https://nas.example", api_key="fake-api-key", insecure=True)


def test_missing_settings_are_named_by_key_without_revealing_values(two_hosts):
    env = {"HOPS_HOST_A_TRUENAS_API_KEY": "fake-api-key", "HOPS_HOST_A_TRUENAS_INSECURE": "maybe"}

    with pytest.raises(HostsError) as e:
        host_settings(env, two_hosts["host-a"])

    message = str(e.value)
    assert "HOPS_HOST_A_SSH_HOST" in message
    assert "HOPS_HOST_A_TRUENAS_URL" in message
    assert "HOPS_HOST_A_TRUENAS_INSECURE" in message
    assert "fake-api-key" not in message and "maybe" not in message


def test_ssh_settings_of_a_truenas_host_need_no_truenas_keys(two_hosts):
    env = {"HOPS_HOST_A_SSH_HOST": "nas.example", "HOPS_HOST_A_SSH_USER": "ops"}

    assert ssh_settings(env, two_hosts["host-a"]) == SshSettings(host="nas.example", user="ops", key=None)


def test_ssh_settings_need_the_ssh_host(two_hosts):
    with pytest.raises(HostsError, match="HOPS_HOST_A_SSH_HOST is not set"):
        ssh_settings({"HOPS_HOST_A_TRUENAS_URL": "https://nas.example"}, two_hosts["host-a"])


def test_truenas_settings_need_only_the_truenas_keys(two_hosts):
    env = {"HOPS_HOST_A_TRUENAS_URL": "https://nas.example", "HOPS_HOST_A_TRUENAS_API_KEY": "fake-api-key"}

    assert truenas_settings(env, two_hosts["host-a"]) == TrueNasSettings(
        url="https://nas.example", api_key="fake-api-key", insecure=False
    )
    with pytest.raises(HostsError) as e:
        truenas_settings({"HOPS_HOST_A_TRUENAS_INSECURE": "maybe"}, two_hosts["host-a"])
    assert e.value.problems == [
        "HOPS_HOST_A_TRUENAS_URL is not set (needed for Host 'host-a')",
        "HOPS_HOST_A_TRUENAS_API_KEY is not set (needed for Host 'host-a')",
        "HOPS_HOST_A_TRUENAS_INSECURE must be 1/0, true/false, yes/no or on/off",
    ]


def test_truenas_settings_of_a_host_without_truenas_are_an_error(two_hosts):
    other = next(h for h in two_hosts.values() if not h.has("truenas"))
    with pytest.raises(HostsError, match="does not have the truenas capability"):
        truenas_settings({}, other)


def test_the_cli_validates_and_lists_the_hosts_of_the_current_repo(two_hosts, monkeypatch, capsys):
    monkeypatch.chdir(two_hosts.root / "tn")

    assert main(["--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [h["name"] for h in listed["hosts"]] == ["host-a", "host-b"]


def test_the_cli_fails_on_an_invalid_hosts_file(tmp_path, monkeypatch, capsys):
    write_repo(tmp_path, "version: 1\nhosts:\n  host-b:\n" + host_entry(deployer="portainer"))
    monkeypatch.chdir(tmp_path)

    assert main([]) == 1
    assert "unknown deployer" in capsys.readouterr().err


def load_problems(root):
    with pytest.raises(HostsError) as e:
        load_hosts(root)
    return str(e.value)


@pytest.fixture
def repo_and_outside(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    (outside / "host-b").mkdir(parents=True)
    return repo, outside


def test_a_stack_root_that_is_a_symlink_out_of_the_repo_is_rejected(repo_and_outside):
    repo, outside = repo_and_outside
    (repo / "host-b").symlink_to(outside / "host-b", target_is_directory=True)
    write_repo(repo, MINIMAL, stack_roots=[])

    assert "hosts.host-b.stack_root: 'host-b' resolves outside the repo root" in load_problems(repo)


def test_a_stack_root_under_a_symlinked_directory_out_of_the_repo_is_rejected(repo_and_outside):
    repo, outside = repo_and_outside
    (repo / "stacks").symlink_to(outside, target_is_directory=True)
    write_repo(repo, "version: 1\nhosts:\n  host-b:\n" + host_entry(stack_root="stacks/host-b"), [])

    assert "'stacks/host-b' resolves outside the repo root" in load_problems(repo)


def test_a_symlinked_stack_root_inside_the_repo_is_allowed(tmp_path):
    write_repo(tmp_path, "version: 1\nhosts:\n  host-b:\n" + host_entry(stack_root="current"), ["real"])
    (tmp_path / "current").symlink_to(tmp_path / "real", target_is_directory=True)

    assert load_hosts(tmp_path)["host-b"].path == tmp_path / "real"


def test_the_repo_root_itself_is_not_a_stack_root(tmp_path):
    write_repo(tmp_path, "version: 1\nhosts:\n  host-b:\n" + host_entry(stack_root="."), [])

    assert "'.' must be a directory under the repo root, not the root itself" in load_problems(tmp_path)


@pytest.mark.parametrize(("outer", "inner"), [("tn", "tn/apps"), ("tn/apps", "tn")])
def test_nested_stack_roots_are_rejected(tmp_path, outer, inner):
    hosts_yaml = (
        "version: 1\nhosts:\n  outer:\n" + host_entry(stack_root=outer) + "  inner:\n" + host_entry(stack_root=inner)
    )
    write_repo(tmp_path, hosts_yaml, ["tn/apps"])

    problems = load_problems(tmp_path)
    assert "stack roots must not contain one another" in problems
    assert "'tn'" in problems and "'tn/apps'" in problems


@pytest.mark.parametrize(
    ("doco_cd", "expected"),
    [
        ("{stack: deploy/doco-cd}", "doco_cd.stack: 'deploy/doco-cd' must be one Stack directory name"),
        ("{stack: Doco-CD}", "doco_cd.stack: 'Doco-CD' must be one Stack directory name"),
        ("{stack: ..}", "doco_cd.stack: '..' must be one Stack directory name"),
        ("{api_secret: /etc/doco-cd/secret}", "doco_cd.api_secret: '/etc/doco-cd/secret' must be relative"),
        ("{api_secret: ../other/secret}", "doco_cd.api_secret: '../other/secret' must be relative"),
        ("{api_secret: doco-cd/../../secret}", "doco_cd.api_secret: 'doco-cd/../../secret' must be relative"),
        ("{api_secret: .}", "doco_cd.api_secret: '.' must be relative"),
        ("{network: 'my network'}", "doco_cd.network: 'my network' is not a Docker network name"),
        ("{network: -metrics}", "doco_cd.network: '-metrics' is not a Docker network name"),
        ("{address: ''}", "doco_cd.address: must be a non-empty string"),
    ],
)
def test_an_invalid_doco_cd_override_is_rejected(tmp_path, doco_cd, expected):
    write_repo(tmp_path, "version: 1\nhosts:\n  host-b:\n" + host_entry(doco_cd=doco_cd))

    assert expected in load_problems(tmp_path)


def test_a_doco_cd_api_secret_reached_through_a_symlink_out_of_the_stack_root_is_rejected(repo_and_outside):
    repo, outside = repo_and_outside
    write_repo(repo, MINIMAL)
    (repo / "host-b" / "doco-cd").symlink_to(outside, target_is_directory=True)

    assert "doco_cd.api_secret: 'doco-cd/api_secret.sops.txt' resolves outside the stack root" in load_problems(repo)


def test_valid_doco_cd_overrides_are_kept(tmp_path):
    doco_cd = (
        "{stack: deployer, api_secret: deployer/secrets/api.sops.txt, address: 'deployer:8080', network: ops_net.1}"
    )
    write_repo(tmp_path, "version: 1\nhosts:\n  host-b:\n" + host_entry(doco_cd=doco_cd))

    assert load_hosts(tmp_path)["host-b"].doco_cd == DocoCd(
        stack="deployer", api_secret="deployer/secrets/api.sops.txt", address="deployer:8080", network="ops_net.1"
    )


@pytest.fixture
def case_insensitive_fs(tmp_path):
    (tmp_path / "probe").mkdir()
    if not (tmp_path / "PROBE").exists():
        pytest.skip("the filesystem is case-sensitive")
    (tmp_path / "probe").rmdir()


def test_stack_roots_differing_only_in_case_are_duplicates_on_a_case_insensitive_fs(tmp_path, case_insensitive_fs):
    hosts_yaml = "version: 1\nhosts:\n  a:\n" + host_entry(stack_root="tn") + "  b:\n" + host_entry(stack_root="TN")
    write_repo(tmp_path, hosts_yaml, ["tn"])

    assert "'TN' is already the stack root of Host 'a'" in load_problems(tmp_path)


def test_a_stack_root_nested_under_a_case_variant_is_rejected(tmp_path, case_insensitive_fs):
    hosts_yaml = (
        "version: 1\nhosts:\n  a:\n" + host_entry(stack_root="tn") + "  b:\n" + host_entry(stack_root="TN/monitoring")
    )
    write_repo(tmp_path, hosts_yaml, ["tn/monitoring"])

    assert "stack roots must not contain one another" in load_problems(tmp_path)


def test_a_path_spelled_in_another_case_picks_the_same_host(two_hosts, case_insensitive_fs):
    (two_hosts.root / "tn" / "plex").mkdir()

    assert two_hosts.pick(path=two_hosts.root / "TN" / "Plex" / "compose.yaml").name == "host-a"


def test_a_path_through_a_symlink_into_a_stack_root_picks_that_host(two_hosts):
    (two_hosts.root / "shortcut").symlink_to(two_hosts.root / "tn", target_is_directory=True)

    assert two_hosts.pick(path=two_hosts.root / "shortcut" / "plex" / "compose.yaml").name == "host-a"


def test_a_doco_cd_stack_symlinked_out_of_the_stack_root_is_rejected(repo_and_outside):
    repo, outside = repo_and_outside
    write_repo(repo, "version: 1\nhosts:\n  host-b:\n" + host_entry(doco_cd="{stack: deployer}"))
    (repo / "host-b" / "deployer").symlink_to(outside, target_is_directory=True)

    assert "doco_cd.stack: 'deployer' resolves outside the stack root" in load_problems(repo)


def test_a_doco_cd_stack_need_not_exist_yet(tmp_path):
    write_repo(tmp_path, "version: 1\nhosts:\n  host-b:\n" + host_entry(doco_cd="{stack: deployer}"))

    assert load_hosts(tmp_path)["host-b"].doco_cd.stack == "deployer"


def test_a_missing_api_secret_under_a_symlinked_intermediate_dir_out_of_the_stack_root_is_rejected(
    repo_and_outside,
):
    repo, outside = repo_and_outside
    write_repo(repo, "version: 1\nhosts:\n  host-b:\n" + host_entry(doco_cd="{api_secret: doco-cd/keys/api.txt}"))
    (repo / "host-b" / "doco-cd").mkdir()
    (repo / "host-b" / "doco-cd" / "keys").symlink_to(outside, target_is_directory=True)

    assert "doco_cd.api_secret: 'doco-cd/keys/api.txt' resolves outside the stack root" in load_problems(repo)
