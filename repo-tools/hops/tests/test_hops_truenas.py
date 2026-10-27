"""TrueNAS: the JSON-RPC client (hops.truenas) and `hops truenas call`, against FakeTrueNAS."""

import json

import pytest
from hops_fakes import FakeTrueNAS, FakeTrueNASError, write

from hops import cli, truenas
from hops.errors import ConfigError, HopsError

KEY = FakeTrueNAS.KEY
CFG = truenas.TrueNASConfig(url="wss://tn.example/api/current", api_key=KEY, key_name="HOPS_HOST_A_TRUENAS_API_KEY")


@pytest.fixture
def server() -> FakeTrueNAS:
    return FakeTrueNAS({"system.info": lambda: {"version": "25.10.4", "hostname": "tn"}})


@pytest.fixture
def sleeps() -> list:
    return []


@pytest.fixture
def client(server, sleeps):
    return truenas.Client(CFG, connect=server.connect, sleep=sleeps.append)


@pytest.fixture
def hops_tn(repo, client, capsys):
    """Run the CLI with the fake TrueNAS: hops_tn("truenas", "call", "system.info")."""

    def run(*argv):
        code = cli.main(list(argv), ctx=cli.Context(repo_root=repo, truenas=client))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


# Client


def test_call_logs_in_with_the_key_then_calls(client, server):
    assert client.call("system.info") == {"version": "25.10.4", "hostname": "tn"}
    assert server.connects == [{"url": "wss://tn.example/api/current", "verify": True, "timeout": 30.0}]
    assert [(r["method"], r["params"]) for r in server.requests] == [
        ("auth.login_with_api_key", [KEY]),
        ("system.info", []),
    ]
    assert all(r["jsonrpc"] == "2.0" for r in server.requests)

    client.call("system.info")
    assert server.called() == ["auth.login_with_api_key", "system.info", "system.info"]  # one login


def test_call_skips_notifications(client, server):
    server.notifications = [{"jsonrpc": "2.0", "method": "collection_update", "params": {"msg": "added"}}]
    assert client.call("system.info")["version"] == "25.10.4"


def test_rejected_key_fails_without_showing_it(server):
    server.methods["auth.login_with_api_key"] = lambda key: False
    with pytest.raises(HopsError, match="rejected HOPS_HOST_A_TRUENAS_API_KEY") as exc:
        truenas.Client(CFG, connect=server.connect).call("system.info")
    assert KEY not in str(exc.value)
    assert server.closed


def rate_limited(times: int):
    """A handler that answers EBUSY `times` times, then accepts the key (as auth.login_with_api_key)."""
    left = iter(range(times))

    def handler(*params):
        if next(left, None) is not None:
            raise FakeTrueNASError("EBUSY", "[EBUSY] Rate Limit Exceeded")
        return params == (KEY,)

    return handler


def test_a_rate_limited_login_is_retried_on_a_new_connection(client, server, sleeps):
    server.methods["auth.login_with_api_key"] = rate_limited(2)
    assert client.call("system.info")["version"] == "25.10.4"
    assert sleeps == list(truenas.LOGIN_BACKOFF[:2])
    assert len(server.connects) == 3
    assert server.called() == ["auth.login_with_api_key"] * 3 + ["system.info"]


def test_a_login_still_rate_limited_after_the_backoff_raises(client, server, sleeps):
    server.methods["auth.login_with_api_key"] = rate_limited(99)
    with pytest.raises(truenas.TrueNASError, match="auth.login_with_api_key: EBUSY") as exc:
        client.call("system.info")
    assert exc.value.errname == "EBUSY"
    assert sleeps == list(truenas.LOGIN_BACKOFF) and sum(sleeps) == 60
    assert server.called() == ["auth.login_with_api_key"] * (len(truenas.LOGIN_BACKOFF) + 1)


def test_only_a_rate_limited_login_is_retried(client, server, sleeps):
    def denied(*params):
        raise FakeTrueNASError("EACCES", "Not authorized")

    server.methods["auth.login_with_api_key"] = denied
    with pytest.raises(truenas.TrueNASError, match="EACCES"):
        client.call("system.info")
    server.methods["auth.login_with_api_key"] = lambda key: True
    server.methods["pool.query"] = rate_limited(99)
    with pytest.raises(truenas.TrueNASError, match="pool.query: EBUSY"):
        client.call("pool.query")
    assert sleeps == []
    assert server.called().count("pool.query") == 1


def test_method_error_names_the_errno_and_hides_the_key(client, server):
    def denied(*params):
        raise FakeTrueNASError("EACCES", f"Not authorized (key {KEY})")

    server.methods["pool.snapshot.create"] = denied
    with pytest.raises(truenas.TrueNASError) as exc:
        client.call("pool.snapshot.create", {"dataset": "tank/a", "name": "s"})
    assert exc.value.errname == "EACCES"
    assert str(exc.value) == "TrueNAS pool.snapshot.create: EACCES: Not authorized (key [REDACTED])"


def test_job_waits_for_the_result(client, server, sleeps):
    states = iter(["WAITING", "RUNNING", "SUCCESS"])
    server.methods["cloudsync.sync"] = lambda task_id: 41
    server.methods["core.get_jobs"] = lambda filters, options: {
        "id": filters[0][2],
        "state": next(states),
        "result": "done",
        "error": None,
    }

    assert client.job("cloudsync.sync", 3) == "done"
    polls = [r["params"] for r in server.requests if r["method"] == "core.get_jobs"]
    assert polls == [[[["id", "=", 41]], {"get": True}]] * 3
    assert len(sleeps) == 2


def test_failed_job_raises_its_error(client, server):
    server.methods["cloudsync.sync"] = lambda task_id: 41
    server.methods["core.get_jobs"] = lambda filters, options: {
        "id": 41,
        "state": "FAILED",
        "result": None,
        "error": "bucket not found",
    }
    with pytest.raises(truenas.TrueNASError, match="cloudsync.sync: job 41 failed: bucket not found"):
        client.job("cloudsync.sync", 3)


# Config


def load(repo, **values):
    from hops import config

    host = config.load_hosts(repo).hosts["host-a"]
    env = {"HOPS_HOST_A_SSH_HOST": "tn.example", **{f"HOPS_HOST_A_TRUENAS_{k}": v for k, v in values.items()}}
    return truenas.load_config(repo, host, env=env)


def test_config_reads_url_and_key_from_dotenv(repo):
    write(
        repo,
        {
            ".env": f"HOPS_HOST_A_SSH_HOST=tn.example\nHOPS_HOST_A_TRUENAS_URL=https://tn.example:8443/ui/\n"
            f"HOPS_HOST_A_TRUENAS_API_KEY={KEY}\n"
        },
    )
    from hops import config

    cfg = truenas.load_config(repo, config.load_hosts(repo).hosts["host-a"], env={})
    assert (cfg.url, cfg.api_key, cfg.verify) == ("wss://tn.example:8443/api/current", KEY, True)
    assert cfg.key_name == "HOPS_HOST_A_TRUENAS_API_KEY"
    assert KEY not in repr(cfg)


def test_insecure_tls_is_an_explicit_opt_in(repo):
    assert load(repo, URL="https://tn.example", API_KEY=KEY, INSECURE="1").verify is False


def test_without_truenas_keys_it_names_the_missing_key(repo):
    with pytest.raises(ConfigError, match="HOPS_HOST_A_TRUENAS_API_KEY is not set"):
        load(repo, URL="https://tn.example")


def test_a_host_without_the_truenas_capability_has_none(plain_host):
    with pytest.raises(truenas.Unavailable, match="Host 'host-a' has no TrueNAS"):
        load(plain_host, URL="https://tn.example", API_KEY=KEY)


def test_plain_http_url_is_refused(repo):
    with pytest.raises(ConfigError, match="HOPS_HOST_A_TRUENAS_URL must be an https"):
        load(repo, URL="http://tn.example", API_KEY=KEY)


def test_url_with_userinfo_is_refused_without_echoing_it(repo):
    with pytest.raises(ConfigError) as exc:
        load(repo, URL="https://admin:hunter2@tn.example", API_KEY=KEY)
    assert "user" in str(exc.value) and "hunter2" not in str(exc.value) and "admin" not in str(exc.value)


# hops truenas call


def test_call_prints_the_result_as_json(hops_tn):
    code, out, err = hops_tn("truenas", "call", "system.info", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"version": "25.10.4", "hostname": "tn"}


def test_call_passes_json_params(hops_tn, server):
    server.methods["pool.query"] = lambda filters, options: [{"name": "tank"}]
    code, out, _ = hops_tn("truenas", "call", "pool.query", '[["name","=","tank"]]', '{"order_by":["name"],"limit":1}')
    assert code == 0
    assert server.requests[-1]["params"] == [[["name", "=", "tank"]], {"order_by": ["name"], "limit": 1}]


def test_bad_json_param_is_a_usage_error(hops_tn, server):
    code, _, err = hops_tn("truenas", "call", "pool.query", "[name]")
    assert code == 2 and "not JSON" in err
    assert server.requests == []


@pytest.mark.parametrize(
    "method",
    [
        "pool.query",
        "pool.get_instance",
        "pool.dataset.query",
        "pool.dataset.details",
        "pool.snapshot.query",
        "pool.snapshottask.query",
        "pool.scrub.query",
        "disk.query",
        "boot.environment.query",
        "replication.query",
        "cloudsync.query",
        "service.query",
        "interface.query",
        "alert.list",
        "system.info",
        "system.version",
        "sharing.smb.query",
        "sharing.nfs.query",
        "smb.config",
        "nfs.config",
        "ssh.config",
        "user.query",
        "group.query",
        "certificate.query",
        "api_key.query",
        "system.general.config",
        "system.advanced.config",
        "network.configuration.config",
        "staticroute.query",
        "docker.config",
        "docker.status",
        "catalog.config",
        "pool.resilver.config",
        "update.config",
        "system.ntpserver.query",
        "audit.config",
        "system.security.config",
        "auth.twofactor.config",
        "ups.config",
        "tunable.query",
        "initshutdownscript.query",
        "cronjob.query",
        "filesystem.stat",
        "filesystem.getacl",
    ],
)
def test_call_accepts_reads_in_allowed_namespaces(hops_tn, server, method):
    server.methods[method] = lambda *params: []
    code, _, err = hops_tn("truenas", "call", method)
    assert (code, err) == (0, "")


@pytest.mark.parametrize(
    "method",
    [
        "pool.snapshot.create",
        "pool.snapshot.delete",
        "cloudsync.sync",
        "api_key.create",
        "api_key.delete",
        "api_key.update",
        "system.reboot",
        "auth.login_with_api_key",
        "core.get_jobs",
        "pool.query.x",
        "../pool.query",
    ],
)
def test_call_refuses_anything_but_a_read(hops_tn, server, method):
    code, out, err = hops_tn("truenas", "call", method, "{}")
    assert code == 2 and out == ""
    assert "not a read method" in err
    assert server.connects == []  # refused before connecting


@pytest.mark.parametrize(
    "method",
    [
        "alertservice.query",
        "snmp.config",
        "keychaincredential.query",
        "cloudsync.credentials.query",
        "app.query",
        "vm.query",
        "virt.instance.query",
        "mail.config",
        "directoryservices.config",
        "kerberos.config",
        "reporting.exporters.query",
        "replication.config.config",
        "alert.query",
    ],
)
def test_call_refuses_namespaces_off_the_allowlist(hops_tn, server, method):
    code, out, err = hops_tn("truenas", "call", method)
    assert code == 2 and out == ""
    assert "allowlist" in err and "README" in err
    assert server.connects == []


@pytest.mark.parametrize(
    "options",
    [
        '{"select": ["attributes.url"]}',  # drops `type`, which defeats TrueNAS's own masking
        '{"select": [["provider.key", "name"]]}',  # renames a secret
        '{"select": []}',
        '{"extra": {"passwords": true}}',  # disk.query: include SED passwords
        '{"selct": ["name"]}',
    ],
)
def test_call_refuses_projections_and_unknown_query_options(hops_tn, server, options):
    code, out, err = hops_tn("truenas", "call", "disk.query", "[]", options)
    assert code == 2 and out == ""
    assert "query option" in err
    assert server.connects == []


def test_call_redacts_credentials(hops_tn, server):
    server.methods["cloudsync.query"] = lambda: [
        {
            "id": 1,
            "path": "/mnt/tank/a",
            "encryption_password": "pw",
            "encryption_salt": "salt",
            "credentials": {"id": 2, "provider": {"type": "B2", "account": "acct", "key": "k"}},
            "attributes": {"bucket": "b", "folder": "/x"},
            "pre_script": "run-backup.sh",
            "api_key": None,
        }
    ]
    _, out, _ = hops_tn("truenas", "call", "cloudsync.query", "--json")
    assert json.loads(out) == [
        {
            "id": 1,
            "path": "/mnt/tank/a",
            "encryption_password": "[REDACTED]",
            "encryption_salt": "[REDACTED]",
            "credentials": "[REDACTED]",
            "attributes": {"bucket": "b", "folder": "/x"},
            "pre_script": "[REDACTED]",
            "api_key": None,
        }
    ]


def test_call_without_truenas_keys_fails_clearly(repo, capsys):
    ctx = cli.Context(repo_root=repo, env={"HOME": "/h"})
    assert cli.main(["truenas", "call", "system.info"], ctx=ctx) == 2
    assert "HOPS_HOST_A_TRUENAS_URL is not set" in capsys.readouterr().err


def test_call_on_a_host_without_the_truenas_capability_is_refused(plain_host, capsys):
    ctx = cli.Context(repo_root=plain_host, env={"HOME": "/h"})
    assert cli.main(["truenas", "call", "system.info"], ctx=ctx) == 2
    assert capsys.readouterr().err == (
        "hops: error: hops truenas call needs a Host with the truenas capability; Host 'host-a' has none (hosts.yaml)\n"
    )


# redact: defense in depth behind the allowlist


@pytest.mark.parametrize(
    "method, result, shown",
    [
        ("ups.config", {"monuser": "upsmon", "monpwd": "pw"}, {"monuser": "upsmon", "monpwd": "[REDACTED]"}),
        ("mail.config", {"user": "me", "pass": "pw"}, {"user": "me", "pass": "[REDACTED]"}),
        ("snmp.config", {"location": "rack", "community": "public"}, {"location": "rack", "community": "[REDACTED]"}),
        (
            "alertservice.query",
            [{"attributes": {"snmp": {"community": "c"}}}],
            [{"attributes": {"snmp": {"community": "[REDACTED]"}}}],
        ),
        ("kerberos.keytab.query", [{"name": "k", "file": "a2V5"}], [{"name": "k", "file": "[REDACTED]"}]),
        (
            "cloudsync.credentials.query",
            [{"name": "b2", "provider": {"type": "B2", "account": "a", "key": "k"}}],
            [{"name": "b2", "provider": {"type": "B2", "account": "[REDACTED]", "key": "[REDACTED]"}}],
        ),
    ],
)
def test_redact_secrets_with_terse_names(method, result, shown):
    assert truenas.redact(result, method) == shown


@pytest.mark.parametrize(
    "method, result, shown",
    [
        (
            "user.query",
            [{"username": "a", "unixhash": "$6$x", "smbhash": "*", "sshpubkey": None, "password_disabled": False}],
            [
                {
                    "username": "a",
                    "unixhash": "[REDACTED]",
                    "smbhash": "[REDACTED]",
                    "sshpubkey": None,
                    "password_disabled": False,
                }
            ],
        ),
        (
            "ssh.config",
            {"passwordauth": True, "privatekey": "k", "host_ed25519_key": "k", "host_ed25519_key_pub": "p"},
            {
                "passwordauth": True,
                "privatekey": "[REDACTED]",
                "host_ed25519_key": "[REDACTED]",
                "host_ed25519_key_pub": "[REDACTED]",
            },
        ),
        (
            "api_key.query",
            [{"name": "ci", "key": "k", "expires_at": None}],
            [{"name": "ci", "key": "[REDACTED]", "expires_at": None}],
        ),
        (
            "system.advanced.config",
            {"sed_passwd": "pw", "anonstats_token": "t", "motd": "hi"},
            {"sed_passwd": "[REDACTED]", "anonstats_token": "[REDACTED]", "motd": "hi"},
        ),
        (
            "system.general.config",
            {"ui_certificate": {"name": "ui", "certificate": "PEM", "privatekey": "PEM"}},
            {"ui_certificate": {"name": "ui", "certificate": "PEM", "privatekey": "[REDACTED]"}},
        ),
        (
            "ups.config",
            {"monuser": "upsmon", "extrausers": "[admin]\npassword = pw"},
            {"monuser": "upsmon", "extrausers": "[REDACTED]"},
        ),
    ],
)
def test_redact_secrets_in_host_configuration_reads(method, result, shown):
    assert truenas.redact(result, method) == shown


def test_redact_udev_tunables_but_not_sysctl_or_zfs():
    assert truenas.redact(
        [
            {"type": "UDEV", "var": "10-x", "value": "ACTION==add, RUN+=/x", "orig_value": ""},
            {"type": "SYSCTL", "var": "vm.swappiness", "value": "10", "orig_value": "60"},
            {"type": "ZFS", "var": "zfs_arc_max", "value": "0", "orig_value": "0"},
        ],
        "tunable.query",
    ) == [
        {"type": "UDEV", "var": "10-x", "value": "[REDACTED]", "orig_value": "[REDACTED]"},
        {"type": "SYSCTL", "var": "vm.swappiness", "value": "10", "orig_value": "60"},
        {"type": "ZFS", "var": "zfs_arc_max", "value": "0", "orig_value": "0"},
    ]


@pytest.mark.parametrize("method", ["initshutdownscript.query", "cronjob.query"])
def test_call_shows_init_and_cron_commands_but_not_scripts(hops_tn, server, method):
    server.methods[method] = lambda: [{"id": 1, "type": "COMMAND", "command": "zfs mount -a", "script": "/mnt/a.sh"}]
    _, out, _ = hops_tn("truenas", "call", method)
    assert json.loads(out) == [{"id": 1, "type": "COMMAND", "command": "zfs mount -a", "script": "[REDACTED]"}]


def test_redact_userinfo_in_any_url():
    assert truenas.redact(
        {
            "httpproxy": "http://user:p%40ss@proxy.lan:3128",
            "nested": [{"mirrors": ["https://bob:pw@reg.example/v2", "https://reg.example/v2"]}],
            "note": "see https://a@b.example and ftp://x:y@z/",
        }
    ) == {
        "httpproxy": "http://[REDACTED]@proxy.lan:3128",
        "nested": [{"mirrors": ["https://[REDACTED]@reg.example/v2", "https://reg.example/v2"]}],
        "note": "see https://[REDACTED]@b.example and ftp://[REDACTED]@z/",
    }


def test_redact_user_written_scripts_and_commands_at_any_depth():
    assert truenas.redact(
        [
            {
                "id": 1,
                "pre_script": "run-backup.sh",
                "post_script": "",
                "args": "--b2-key x",
                "job": {"state": "SUCCESS", "description": None},
                "tasks": [
                    {
                        "command": "backup --password x",
                        "script": "/mnt/a.sh",
                        "extra": ["--rsh=ssh -i k"],
                        "sudo_commands": ["/bin/x"],
                        "sudo_commands_nopasswd": [],
                        "shutdowncmd": "/bin/off",
                        "options": "x",
                        "optionsupsd": "y",
                        "smb_options": "z",
                        "auxsmbconf": "z",
                        "cmdline": None,
                    }
                ],
            }
        ]
    ) == [
        {
            "id": 1,
            "pre_script": "[REDACTED]",
            "post_script": "[REDACTED]",
            "args": "[REDACTED]",
            "job": {"state": "SUCCESS"},
            "tasks": [
                {
                    "command": "[REDACTED]",
                    "script": "[REDACTED]",
                    "extra": "[REDACTED]",
                    "sudo_commands": "[REDACTED]",
                    "sudo_commands_nopasswd": "[REDACTED]",
                    "shutdowncmd": "[REDACTED]",
                    "options": "[REDACTED]",
                    "optionsupsd": "[REDACTED]",
                    "smb_options": "[REDACTED]",
                    "auxsmbconf": "[REDACTED]",
                    "cmdline": None,
                }
            ],
        }
    ]


def test_call_reduces_embedded_jobs_to_their_status(hops_tn, server):
    leak = "CLOUD_SYNC_KEY=fake-b2-key"
    job = {
        "id": 9,
        "method": "cloudsync.sync",
        "arguments": [1, leak],
        "state": "FAILED",
        "progress": {"percent": 40, "description": leak, "extra": None},
        "result": leak,
        "error": f"script failed: {leak}",
        "exception": leak,
        "exc_info": {"repr": leak},
        "logs_excerpt": f"{leak}\n{leak}",
        "logs_path": "/var/log/jobs/9.log",
        "time_started": {"$date": 1},
        "time_finished": {"$date": 2},
    }
    server.methods["cloudsync.query"] = lambda: [{"id": 1, "job": job, "nested": [{"job": dict(job)}]}]
    _, out, _ = hops_tn("truenas", "call", "cloudsync.query")
    assert "fake-b2-key" not in out
    status = {
        "id": 9,
        "method": "cloudsync.sync",
        "state": "FAILED",
        "progress": {"percent": 40},
        "time_started": {"$date": 1},
        "time_finished": {"$date": 2},
    }
    assert json.loads(out) == [{"id": 1, "job": status, "nested": [{"job": status}]}]
