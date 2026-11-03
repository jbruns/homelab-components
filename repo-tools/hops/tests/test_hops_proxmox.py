"""Proxmox VE: the HTTPS client (hops.proxmox) and `hops proxmox call`/`batch`, against FakeProxmox."""

import io
import json

import pytest
from hops_fakes import FakeProxmox, hosts_yaml, write

from hops import cli, proxmox
from hops.commands import proxmox as commands
from hops.errors import ConfigError, HopsError

SECRET = FakeProxmox.SECRET
PVE = "\n      pve:\n        deployer: none\n        capabilities: [proxmox]\n"
ENV = {
    "HOPS_PVE_PROXMOX_URL": "https://pve.example.com:8006",
    "HOPS_PVE_PROXMOX_TOKEN_ID": FakeProxmox.TOKEN,
    "HOPS_PVE_PROXMOX_TOKEN_SECRET": SECRET,
}
CFG = proxmox.ProxmoxConfig(
    url="https://pve.example.com:8006",
    netloc="pve.example.com:8006",
    token_id=FakeProxmox.TOKEN,
    token_secret=SECRET,
    key_name="HOPS_PVE_PROXMOX_TOKEN_ID and HOPS_PVE_PROXMOX_TOKEN_SECRET",
    insecure_key="HOPS_PVE_PROXMOX_INSECURE",
)


@pytest.fixture
def server() -> FakeProxmox:
    return FakeProxmox({"nodes": [{"node": "pve", "status": "online"}]})


@pytest.fixture
def client(server):
    return proxmox.Client(CFG, connect=server.connect)


@pytest.fixture
def pve_repo(repo):
    write(repo, {"hosts.yaml": hosts_yaml(extra=PVE)})
    return repo


@pytest.fixture
def hops_px(pve_repo, client, capsys):
    """Run the CLI against the fake Proxmox VE: hops_px("proxmox", "call", "nodes"); `stdin` feeds a batch."""

    def run(*argv, stdin="", env=None, px=client):
        ctx = cli.Context(repo_root=pve_repo, proxmox=px, env=ENV if env is None else env, stdin=io.StringIO(stdin))
        code = cli.main(list(argv), ctx=ctx)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def batch(hops_px, *calls):
    code, out, err = hops_px("proxmox", "batch", "--json", stdin=json.dumps(list(calls)))
    return code, (json.loads(out) if out else None), err


# Configuration


def config_for(pve_repo, **env):
    hosts = cli.config.load_hosts(pve_repo)
    return proxmox.load_config(pve_repo, hosts["pve"], {**ENV, **env})


def test_config_reads_the_hosts_keys(pve_repo):
    cfg = config_for(pve_repo, HOPS_PVE_PROXMOX_INSECURE="1")
    assert (cfg.url, cfg.netloc, cfg.token_id, cfg.verify) == (
        "https://pve.example.com:8006",
        "pve.example.com:8006",
        FakeProxmox.TOKEN,
        False,
    )


@pytest.mark.parametrize(
    ("key", "value", "problem"),
    [
        ("HOPS_PVE_PROXMOX_URL", "http://pve.example.com:8006", "must be an https:// URL"),
        ("HOPS_PVE_PROXMOX_URL", "https://pve.example.com:8006/api2/json", "must be an https:// URL"),
        ("HOPS_PVE_PROXMOX_URL", "https://root:pw@pve.example.com:8006", "must not contain a user name"),
        ("HOPS_PVE_PROXMOX_TOKEN_ID", "hops-reader", "must be a Proxmox VE token ID"),
        ("HOPS_PVE_PROXMOX_TOKEN_SECRET", "has space", "must be printable"),
    ],
)
def test_bad_config_is_refused_without_echoing_it(pve_repo, key, value, problem):
    with pytest.raises(ConfigError, match=problem) as exc:
        config_for(pve_repo, **{key: value})
    assert value not in str(exc.value)


def test_a_host_without_the_capability_has_no_proxmox(repo):
    hosts = cli.config.load_hosts(repo)
    with pytest.raises(proxmox.Unavailable, match="no Proxmox VE"):
        proxmox.load_config(repo, hosts["host-a"], ENV)


# Client


def test_get_sends_the_token_in_a_header_and_returns_data(client, server):
    assert client.get("nodes") == [{"node": "pve", "status": "online"}]
    assert server.connects == [{"netloc": "pve.example.com:8006", "verify": True}]
    assert server.headers[0]["Authorization"] == f"PVEAPIToken={FakeProxmox.TOKEN}={SECRET}"
    client.get("nodes")
    assert len(server.connects) == 1  # one kept-alive connection


def test_query_parameters_send_booleans_as_numbers(client, server):
    server.routes["nodes/pve/storage"] = []
    client.get("nodes/pve/storage", {"content": "backup", "enabled": True})
    assert server.requests[-1] == ("nodes/pve/storage", {"content": "backup", "enabled": "1"})


def test_an_error_status_is_a_call_error_naming_only_refused_parameter_names(client, server):
    def bad(query):
        return 400, "Parameter verification failed."

    server.routes["nodes/pve/storage"] = bad
    with pytest.raises(proxmox.ProxmoxError) as exc:
        client.get("nodes/pve/storage", {"content": "backup"})
    assert (exc.value.status, exc.value.path) == (400, "nodes/pve/storage")
    assert "400 Parameter verification failed." in str(exc.value)


def test_a_rejected_token_is_fatal_and_names_the_keys_not_the_secret(client, server):
    server.routes["nodes"] = (401, "authentication failure")
    with pytest.raises(HopsError) as exc:
        client.get("nodes")
    assert not isinstance(exc.value, proxmox.ProxmoxError)
    assert "HOPS_PVE_PROXMOX_TOKEN_SECRET" in str(exc.value) and SECRET not in str(exc.value)


def test_a_switched_off_host_is_host_off(server):
    server.off = True
    with pytest.raises(proxmox.HostOff, match="off or unreachable"):
        proxmox.Client(CFG, connect=server.connect).get("nodes")


def test_a_dropped_kept_alive_connection_is_retried_once(client, server):
    client.get("nodes")
    answers = iter([proxmox.HostOff("lost the connection")])

    def flaky(target, headers, real=server.get):
        error = next(answers, None)
        if error:
            raise error
        return real(target, headers)

    server.get = flaky
    assert client.get("nodes") == [{"node": "pve", "status": "online"}]
    assert len(server.connects) == 2


def test_an_untrusted_certificate_names_the_insecure_key(server):
    def untrusted(netloc, **kw):
        raise proxmox.Untrusted(f"Proxmox VE at https://{netloc}: TLS certificate not trusted")

    with pytest.raises(proxmox.Untrusted, match="HOPS_PVE_PROXMOX_INSECURE=1"):
        proxmox.Client(CFG, connect=untrusted).get("nodes")


def test_the_secret_is_scrubbed_from_client_errors(server):
    def leaky(netloc, **kw):
        raise proxmox.HostOff(f"could not reach {netloc} with {SECRET}")

    with pytest.raises(proxmox.HostOff) as exc:
        proxmox.Client(CFG, connect=leaky).get("nodes")
    assert SECRET not in str(exc.value) and "[REDACTED]" in str(exc.value)


# Redaction


def test_credential_fields_are_masked_and_lookalikes_kept():
    config = {
        "cipassword": "pw",
        "sshkeys": "ssh-ed25519%20AAAA",
        "keyboard": "en-us",
        "args": "-fw_cfg name=opt/x,string=s",
        "description": "notes",
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=20",
        "protection": True,
        "lock": None,
    }
    assert proxmox.redact(config, "nodes/pve/qemu/100/config") == {
        "cipassword": "[REDACTED]",
        "sshkeys": "[REDACTED]",
        "keyboard": "en-us",
        "args": "[REDACTED]",
        "description": "[REDACTED]",
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=20",
        "protection": True,
        "lock": None,
    }


def test_free_text_is_masked_only_in_a_guest_config():
    assert proxmox.redact({"description": "a node"}, "nodes/pve/version") == {"description": "a node"}


def test_a_users_token_ids_are_kept_and_two_factor_keys_masked():
    users = [{"userid": "a@pve", "keys": "x", "tokens": [{"tokenid": "hops", "expire": 0}]}]
    assert proxmox.redact(users, "access/users") == [
        {"userid": "a@pve", "keys": "[REDACTED]", "tokens": [{"tokenid": "hops", "expire": 0}]}
    ]


def test_notification_target_credentials_are_masked():
    webhook = {
        "name": "alerts",
        "url": "https://hooks.example.com/abc",
        "header": ["name=Authorization,value=QmVhcmVy"],
        "secret": ["name=token,value=c2VjcmV0"],
        "body": "eyJ0ZXh0Ijp9",
        "method": "post",
    }
    out = proxmox.redact(webhook, "cluster/notifications/endpoints/webhook/alerts")
    assert out["header"] == ["name=Authorization,value=[REDACTED]"]
    assert out["secret"] == ["name=token,value=[REDACTED]"]
    assert out["body"] == "[REDACTED]"
    assert out["url"].startswith("[REDACTED sha256:") and "hooks" not in out["url"]
    assert out["method"] == "post"
    smtp = {"name": "mail", "password": "pw", "username": "u"}
    assert proxmox.redact(smtp, "cluster/notifications/endpoints/smtp/mail")["password"] == "[REDACTED]"


def test_url_userinfo_is_masked_in_any_string():
    out = proxmox.redact({"server": "https://user:pw@pbs.example.com/x"}, "storage")
    assert out == {"server": "https://[REDACTED]@pbs.example.com/x"}


# Allowlist


@pytest.mark.parametrize(
    "path",
    [
        "nodes",
        "/nodes/pve/qemu/100/config/",
        "nodes/pve/lxc/101/status/current",
        "nodes/pve/storage/local/content",
        "nodes/pve/apt/update",
        "nodes/pve/tasks",
        "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:vzdump::root@pam:/log",
        "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:vzdump:100:hops-reader@pve!hops:/log",
        "cluster/resources",
        "cluster/firewall/groups/web/0",
        "cluster/notifications/endpoints/webhook/alerts",
        "access/users",
    ],
)
def test_allowlisted_paths_pass(path):
    commands.Backend(None).check(path, [])


@pytest.mark.parametrize(
    "path",
    [
        "access/ticket",
        "nodes/pve/qemu/100/vncproxy",
        "nodes/pve/qemu/100/config/extra",
        "nodes/pve/qemu/abc/config",
        "nodes/pve/storage/../../access/ticket",
        "nodes/pve/firewall/log",
        "cluster/notifications/endpoints/email/x",
        "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:qmstart:100:root@pam:/log",  # only vzdump's logs
        "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:vzdump::root@pam:",
        "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:vzdump::root@pam:/status",
    ],
)
def test_other_paths_are_refused(path):
    with pytest.raises(cli.UsageError, match="allowlist") as exc:
        commands.Backend(None).check(path, [])
    assert path.strip("/") not in str(exc.value)


@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ([{"fake-proxmox-guess": "x"}], "a query parameter is refused; nodes/{node}/storage takes content"),
        ([{"content": "backup"}, {}], "at most one JSON object"),
        (["content"], "at most one JSON object"),
        ([{"content": ["backup"]}], "strings, numbers or booleans"),
    ],
)
def test_bad_params_are_refused_without_echoing_them(params, problem):
    with pytest.raises(cli.UsageError) as exc:
        commands.Backend(None).check("nodes/pve/storage", params)
    assert problem in str(exc.value) and "fake-proxmox-guess" not in str(exc.value)


def test_task_params_are_allowlisted():
    commands.Backend(None).check("nodes/pve/tasks", [{"typefilter": "vzdump", "source": "all", "limit": 100}])
    commands.Backend(None).check(
        "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:vzdump::root@pam:/log", [{"start": 0, "limit": 500}]
    )
    with pytest.raises(cli.UsageError, match="takes limit, start"):
        commands.Backend(None).check(
            "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:vzdump::root@pam:/log", [{"download": 1}]
        )


def test_call_reads_a_vzdump_task_log(hops_px, server):
    path = "nodes/pve/tasks/UPID:pve:00001234:00ABCDEF:6719A0C0:vzdump::root@pam:/log"
    server.routes[path] = [{"n": 1, "t": "INFO: Starting Backup of VM 100 (qemu)"}]
    code, out, err = hops_px("proxmox", "call", path, '{"limit": 10}', "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == [{"n": 1, "t": "INFO: Starting Backup of VM 100 (qemu)"}]
    assert server.requests == [(path, {"limit": "10"})]


# hops proxmox call / batch


def test_call_prints_the_redacted_result(hops_px, server):
    server.routes["nodes/pve/qemu/100/config"] = {"name": "router", "cipassword": "pw"}
    code, out, err = hops_px("proxmox", "call", "nodes/pve/qemu/100/config", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"name": "router", "cipassword": "[REDACTED]"}


def test_call_passes_query_params(hops_px, server):
    server.routes["nodes/pve/storage/local/content"] = []
    code, _, _ = hops_px("proxmox", "call", "nodes/pve/storage/local/content", '{"content": "backup"}')
    assert code == 0
    assert server.requests == [("nodes/pve/storage/local/content", {"content": "backup"})]


def test_a_refused_call_never_connects(hops_px, server):
    code, out, err = hops_px("proxmox", "call", "access/ticket")
    assert (code, out) == (2, "")
    assert "allowlist" in err and "access/ticket" not in err
    assert server.connects == []


def test_batch_refuses_a_call_redacts_its_params_and_runs_the_rest(hops_px, server):
    code, out, _ = batch(
        hops_px,
        {"method": "access/ticket", "params": [{"password": "fake-guess"}]},
        {"method": "nodes"},
    )
    assert code == 0
    assert out[0] == {
        "method": "access/ticket",
        "params": "[REDACTED]",
        "error": out[0]["error"],
    }
    assert "allowlist" in out[0]["error"] and "fake-guess" not in json.dumps(out)
    assert out[1] == {"method": "nodes", "params": [], "result": [{"node": "pve", "status": "online"}]}
    assert server.paths() == ["nodes"]


def test_a_per_call_error_does_not_end_the_batch(hops_px, server):
    server.routes["nodes/pve/apt/update"] = (403, "Permission check failed (/nodes/pve, Sys.Modify)")
    code, out, _ = batch(hops_px, {"method": "nodes/pve/apt/update"}, {"method": "nodes"})
    assert code == 0
    assert out[0]["error"] == (
        "Proxmox VE GET nodes/pve/apt/update: 403 Permission check failed (/nodes/pve, Sys.Modify)"
    )
    assert out[1]["result"] == [{"node": "pve", "status": "online"}]


def test_a_rejected_token_ends_the_batch(hops_px, server):
    server.routes["nodes"] = (401, "authentication failure")
    code, out, err = batch(hops_px, {"method": "nodes"}, {"method": "nodes"})
    assert code == 1
    assert len(out) == 1 and "rejected the token" in out[0]["error"]
    assert SECRET not in err + json.dumps(out)


def test_call_on_a_switched_off_host_fails(hops_px, server):
    server.off = True
    code, out, err = hops_px("proxmox", "call", "nodes")
    assert (code, out) == (1, "")
    assert "off or unreachable" in err


def test_the_secret_is_scrubbed_even_when_the_config_is_invalid(hops_px):
    env = {**ENV, "HOPS_PVE_PROXMOX_URL": f"https://{SECRET}@pve.example.com"}
    code, out, err = hops_px("proxmox", "call", "nodes", env=env, px=None)
    assert code == 2
    assert SECRET not in out + err
    assert "must not contain a user name" in err


def test_call_refuses_a_host_without_the_capability(hops_px, server):
    code, _, err = hops_px("--host", "host-a", "proxmox", "call", "nodes")
    assert code == 2 and "proxmox" in err
    assert server.connects == []


def test_scrub_reads_the_secret_even_when_the_config_is_invalid(pve_repo):
    ctx = cli.Context(repo_root=pve_repo, host="pve", env={**ENV, "HOPS_PVE_PROXMOX_URL": "http://x"}, stacks=False)
    backend = commands.Backend(ctx)
    with pytest.raises(ConfigError):
        backend.open()
    assert backend.scrub(f"echoed {SECRET}") == "echoed [REDACTED]"
    assert backend.redact_params([{"content": SECRET}]) == [{"content": "[REDACTED]"}]


# Review follow-ups


def test_call_and_batch_scrub_the_secret_from_results(hops_px, server):
    server.routes["nodes/pve/version"] = {"version": f"9.1 {SECRET}", SECRET: "x"}
    code, out, _ = hops_px("proxmox", "call", "nodes/pve/version")
    assert code == 0 and SECRET not in out
    # The key names a secret, so field-name redaction masks its value too; the scrub removes it from the key.
    assert json.loads(out) == {"version": "9.1 [REDACTED]", "[REDACTED]": "[REDACTED]"}
    code, out, _ = batch(hops_px, {"method": "nodes/pve/version"})
    assert code == 0 and SECRET not in json.dumps(out)


@pytest.mark.parametrize("url", [f"https://pve.example.com:{SECRET}", f"https://[{SECRET}]", "https://pve:99999"])
def test_an_unparseable_url_is_a_config_error_that_echoes_nothing(pve_repo, url):
    with pytest.raises(ConfigError) as exc:
        config_for(pve_repo, HOPS_PVE_PROXMOX_URL=url)
    assert "HOPS_PVE_PROXMOX_URL" in str(exc.value)
    assert SECRET not in str(exc.value) and url not in str(exc.value)


def test_a_first_request_host_off_is_scrubbed(server):
    def leaky(target, headers):
        raise proxmox.HostOff(f"lost {SECRET}")

    server.get = leaky
    with pytest.raises(proxmox.HostOff) as exc:
        proxmox.Client(CFG, connect=server.connect).get("nodes")
    assert SECRET not in str(exc.value)


class FakeConnection:
    """http.client.HTTPSConnection whose connect raises `error`."""

    error: Exception

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        raise self.error

    def close(self):
        pass


def connect_failing(monkeypatch, error):
    import http.client

    monkeypatch.setattr(http.client, "HTTPSConnection", type("C", (FakeConnection,), {"error": error}))
    return proxmox.https_connect("pve.example.com:8006", verify=True, connect_timeout=5, timeout=30)


def test_a_tls_failure_that_is_not_the_certificate_is_fatal_not_off(monkeypatch):
    import ssl

    with pytest.raises(HopsError) as exc:
        connect_failing(monkeypatch, ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))
    assert not isinstance(exc.value, proxmox.HostOff)
    assert "TLS" in str(exc.value)


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError(61, "refused"), TimeoutError(), OSError(65, "No route to host")],
    ids=["refused", "timeout", "no-route"],
)
def test_unreachable_is_host_off(monkeypatch, error):
    with pytest.raises(proxmox.HostOff):
        connect_failing(monkeypatch, error)


def test_dns_failure_is_host_off(monkeypatch):
    import socket

    with pytest.raises(proxmox.HostOff):
        connect_failing(monkeypatch, socket.gaierror(8, "nodename nor servname provided"))
