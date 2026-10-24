"""hops restart, start, stop --for and recreate: runtime state only, with the Disruptive guardrails."""

import json

import pytest
from hops_fakes import container, hosts_yaml, undeclare_monitoring, write

from hops import doco
from hops.errors import HopsError, HostTimeout

KEY = "fake-doco-cd-key"
PASSWORD = ["exec", "prometheus", "cat", "/run/secrets/prometheus_password"]


def amtool_args(call):
    """The arguments the remote sh passes to amtool ("$@" after $0=amtool)."""
    alertmanager = call.docker[1]
    return alertmanager[alertmanager.index("amtool") + 1 :]


@pytest.fixture
def api_key(monkeypatch):
    """Stands in for `sops -d`; records the file it was asked to decrypt."""
    asked = []

    def decrypt(path):
        asked.append(path)
        return KEY + "\n"

    monkeypatch.setattr(doco, "decrypt", decrypt)
    return asked


def http_response(status: str, body: dict) -> str:
    """doco-cd's answer as the Runner returns it: text mode turns its CRLFs into LFs."""
    text = json.dumps(body)
    return f"HTTP/1.0 {status}\nContent-Type: application/json\nContent-Length: {len(text)}\n\n{text}"


# restart and start


def test_restart_prints_the_containers_then_restarts_them_in_one_call(hops, fake_host):
    fake_host.respond("restart")
    outcome = hops("restart", "nextcloud")
    assert outcome.code == 0
    assert fake_host.docker_calls == [["restart", "nextcloud-exporter", "mariadb", "nextcloud", "valkey"]]
    assert "nextcloud/mariadb (mariadb)" in outcome.out
    assert outcome.out.index("nextcloud/valkey") < outcome.out.index("Restarted")


def test_start_finds_a_container_without_container_name_by_its_compose_labels(hops, fake_host):
    fake_host.respond("ps", stdout="abc\n")
    fake_host.respond("inspect", "abc", stdout=json.dumps([container("doco-cd-doco-cd-1", status="exited")]))
    fake_host.respond("start")
    assert hops("start", "doco-cd").code == 0
    assert fake_host.docker_calls[-1] == ["start", "doco-cd-doco-cd-1"]


def test_lifecycle_rejects_an_unknown_name_before_touching_the_host(hops, fake_host):
    outcome = hops("restart", "sonar")
    assert outcome.code == 2 and "did you mean sonarr" in outcome.err
    assert fake_host.calls == []


@pytest.mark.parametrize("verb", ["restart", "start"])
def test_restart_and_start_need_no_declared_monitoring_stack(hops, fake_host, repo, verb):
    undeclare_monitoring(repo)
    fake_host.respond(verb)
    outcome = hops(verb, "sonarr/sonarr")
    assert outcome.code == 0, outcome.err
    assert fake_host.docker_calls == [[verb, "sonarr"]]


def test_restart_reports_a_docker_failure(hops, fake_host):
    fake_host.respond("restart", returncode=1, stderr="Error response from daemon: boom")
    outcome = hops("restart", "sonarr/sonarr")
    assert outcome.code == 1 and "boom" in outcome.err


# stop


def test_stop_without_for_is_refused_before_touching_the_host(hops, fake_host):
    outcome = hops("stop", "sonarr/sonarr")
    assert outcome.code == 2 and "--for" in outcome.err
    assert fake_host.calls == []


@pytest.mark.parametrize("duration", ["2", "1 h", "0h", "-1h", "1x", "h"])
def test_stop_rejects_a_bad_duration(hops, fake_host, duration):
    outcome = hops("stop", "sonarr/sonarr", f"--for={duration}")
    assert outcome.code == 2 and "--for" in outcome.err
    assert fake_host.calls == []


def test_stop_of_a_critical_service_needs_force(hops, fake_host):
    outcome = hops("stop", "traefik", "--for", "30m")
    assert outcome.code == 2 and "Critical" in outcome.err and "--force" in outcome.err
    assert fake_host.calls == []


def test_stop_of_a_stack_with_any_critical_service_needs_force(hops, fake_host):
    outcome = hops("stop", "nextcloud", "--for", "30m")
    assert outcome.code == 2 and "nextcloud/mariadb" in outcome.err
    assert fake_host.calls == []


def silences(fake_host):
    """The matchers of each `amtool silence add`, in order."""
    found = []
    for call in fake_host.calls:
        if call.docker[0] == PASSWORD:
            args = amtool_args(call)
            assert args[:2] == ["silence", "add"]
            found.append([a for a in args[2:] if not a.startswith("--")])
    return found


def test_stop_silences_the_service_then_stops_it(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", stdout="silence-1\n")
    fake_host.respond("stop")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h30m")
    assert outcome.code == 0
    assert silences(fake_host) == [['stack="sonarr"', 'service="sonarr"'], ['container="sonarr"'], ['name="sonarr"']]
    for call in fake_host.calls[:-1]:
        args = amtool_args(call)
        assert "--duration=1h30m" in args
        assert any(a.startswith("--comment=hops stop sonarr/sonarr") for a in args)
    assert fake_host.calls[-1].docker == [["stop", "sonarr"]]
    assert 'Silenced container="sonarr" for 1h30m (silence-1): sonarr/sonarr: alerts on its container' in outcome.out
    assert "Nothing restarts" in outcome.out and "hops start sonarr/sonarr" in outcome.out


def test_stop_of_a_critical_service_with_force(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", stdout="silence-2\n")
    fake_host.respond("stop")
    assert hops("stop", "traefik", "--for", "10m", "--force").code == 0
    assert silences(fake_host)[0] == ['stack="edge"', 'service="traefik"']
    assert fake_host.docker_calls[-1] == ["stop", "traefik"]


def test_stop_does_not_stop_when_the_silence_fails(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", returncode=1, stderr="amtool: unauthorized")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code == 1 and "unauthorized" in outcome.err
    assert [c for c in fake_host.docker_calls if c[0] == "stop"] == []


def test_stop_without_a_declared_monitoring_stack_stops_and_warns_that_nothing_is_silenced(hops, fake_host, repo):
    undeclare_monitoring(repo)
    fake_host.respond("stop")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code == 0, outcome.err
    assert fake_host.docker_calls == [["stop", "sonarr"]]
    assert "no silences were created" in outcome.err and "alerts may fire" in outcome.err
    assert "Stopped 1 container" in outcome.out


@pytest.mark.parametrize("text", ["", "services: {}\n", "servics:\n  prometheus:\n    image: prom/prometheus\n"])
def test_stop_with_a_monitoring_compose_file_that_declares_no_services_still_fails(hops, fake_host, repo, text):
    (repo / "tn" / "monitoring" / "compose.yaml").write_text(text)
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code == 2 and "declares no prometheus in its monitoring Stack" in outcome.err
    assert [c for c in fake_host.docker_calls if c[0] == "stop"] == []


def test_stop_with_a_monitoring_stack_under_other_service_names_still_fails(hops, fake_host, repo):
    compose = repo / "tn" / "monitoring" / "compose.yaml"
    compose.write_text(
        compose.read_text()
        .replace("  prometheus:", "  prometheus-server:")
        .replace("  alertmanager:", "  alertmanager-server:")
    )
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code == 2 and "declares no prometheus in its monitoring Stack" in outcome.err
    assert [c for c in fake_host.docker_calls if c[0] == "stop"] == []


def test_stop_with_prometheus_and_alertmanager_in_another_stack_still_fails(hops, fake_host, repo):
    monitoring = repo / "tn" / "monitoring"
    (repo / "tn" / "observability").mkdir()
    (monitoring / "compose.yaml").rename(repo / "tn" / "observability" / "compose.yaml")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code == 2 and "declares no prometheus in its monitoring Stack" in outcome.err
    assert [c for c in fake_host.docker_calls if c[0] == "stop"] == []


def test_stop_with_only_one_of_prometheus_and_alertmanager_declared_still_fails(hops, fake_host, repo):
    compose = repo / "tn" / "monitoring" / "compose.yaml"
    compose.write_text(compose.read_text().split("  alertmanager:")[0])
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code != 0 and "declares no alertmanager" in outcome.err
    assert [c for c in fake_host.docker_calls if c[0] == "stop"] == []


# recreate

# On the metrics network: prometheus, traefik, sonarr's exporter (no nc -e), and a stopped alertmanager.
METRICS = [
    container("prometheus", project="monitoring", service="prometheus"),
    container("traefik", project="edge", service="traefik"),
    container("sonarr-exporter", project="sonarr", service="exporter"),
    container("alertmanager", project="monitoring", service="alertmanager", status="exited"),
]


@pytest.fixture
def metrics(fake_host):
    """The metrics network; every member but sonarr-exporter passes the transport probe."""
    fake_host.respond("network", "inspect", "metrics", stdout=" ".join(c["Name"].lstrip("/") for c in METRICS) + " \n")
    fake_host.respond("inspect", stdout=json.dumps(METRICS))
    for c in METRICS:
        fake_host.respond("exec", c["Name"].lstrip("/"), "sh", returncode=1 if c["Name"] == "/sonarr-exporter" else 0)
    return fake_host


def requests(fake_host):
    return [c for c in fake_host.calls if c.docker[-1][:2] == ["exec", "-i"]]


def probes(fake_host):
    return [c.docker[-1][1] for c in fake_host.calls if c.docker[-1][0] == "exec" and c.docker[-1][1] != "-i"]


def test_recreate_sends_the_api_key_on_stdin_never_in_argv(hops, metrics, api_key, repo):
    metrics.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {"content": "ok"}))
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 0, outcome.err
    assert api_key == [repo / "tn" / "doco-cd" / "api_secret.sops.txt"]
    assert all(KEY not in arg for call in metrics.calls for arg in call.argv)
    (call,) = requests(metrics)
    assert call.docker[0][:4] == ["exec", "-i", "prometheus", "sh"]
    request = call.input
    assert request.startswith("POST /v1/api/project/sonarr/recreate?service=sonarr HTTP/1.0\r\n")
    assert f"\r\nx-api-key: {KEY}\r\n" in request and request.endswith("\r\n\r\n")
    assert "Recreated sonarr/sonarr" in outcome.out


def test_recreate_reaches_doco_cd_where_hosts_yaml_says(hops, fake_host, api_key, repo):
    write(
        repo,
        {
            "hosts.yaml": hosts_yaml(
                extra="""
        doco_cd:
          stack: deployer
          api_secret: deployer/key.sops.txt
          address: cd.internal:8080
          network: ops
    """
            )
        },
    )
    fake_host.respond("network", "inspect", "ops", stdout="prometheus \n")
    fake_host.respond("inspect", stdout=json.dumps(METRICS[:1]))
    fake_host.respond("exec", "prometheus", "sh")
    fake_host.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {}))

    assert hops("recreate", "sonarr/sonarr").code == 0

    assert api_key == [repo / "tn" / "deployer" / "key.sops.txt"]
    (call,) = requests(fake_host)
    assert call.docker[0][-3:] == ["600", "cd.internal", "8080"]
    assert "\r\nHost: cd.internal\r\n" in call.input


def test_recreate_reports_doco_cds_error(hops, metrics, api_key):
    metrics.respond(
        "exec", "-i", "prometheus", stdout=http_response("404 Not Found", {"error": "service not found", "code": 404})
    )
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "404" in outcome.err and "service not found" in outcome.err


def test_recreate_fails_without_an_http_response(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", returncode=1, stderr="nc: bad address 'doco-cd'")
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "bad address" in outcome.err


def test_recreate_refuses_doco_cd_itself(hops, fake_host, api_key):
    outcome = hops("recreate", "doco-cd")
    assert outcome.code == 2 and "doco-cd" in outcome.err
    assert fake_host.calls == [] and api_key == []


def test_recreate_stops_when_sops_fails(hops, metrics, monkeypatch):
    def fail(path):
        raise HopsError("sops -d failed: no key")

    monkeypatch.setattr(doco, "decrypt", fail)
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "sops" in outcome.err
    assert requests(metrics) == []


def test_recreate_needs_no_declared_monitoring_stack(hops, metrics, api_key, repo):
    """Mid-cutover the legacy prometheus container still carries the request: any running one on the network can."""
    undeclare_monitoring(repo)
    metrics.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {"content": "ok"}))
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 0, outcome.err
    assert "Recreated sonarr/sonarr" in outcome.out


def test_recreate_of_prometheus_sends_through_another_container(hops, metrics, api_key):
    """Prometheus would stop mid-request; sonarr-exporter lacks nc -e, alertmanager is not running."""
    metrics.respond("exec", "-i", "traefik", stdout=http_response("200 OK", {}))
    assert hops("recreate", "monitoring/prometheus").code == 0
    assert probes(metrics) == ["sonarr-exporter", "traefik"]
    (call,) = requests(metrics)
    assert call.docker[0][:3] == ["exec", "-i", "traefik"]


def test_recreate_of_a_stack_picks_the_transport_once_outside_it(hops, metrics, api_key):
    metrics.respond("exec", "-i", "traefik", stdout=http_response("200 OK", {}))
    outcome = hops("recreate", "monitoring")
    assert outcome.code == 0, outcome.err
    assert [c.docker[0][2] for c in requests(metrics)] == ["traefik", "traefik"]
    first_request = metrics.calls.index(requests(metrics)[0])
    assert all(c.docker[-1][:2] == ["exec", "-i"] for c in metrics.calls[first_request:])


def test_recreate_refuses_when_no_container_can_carry_the_request(hops, metrics, api_key):
    metrics.respond("exec", "traefik", "sh", returncode=1)
    outcome = hops("recreate", "monitoring/prometheus")
    assert outcome.code == 1 and "metrics" in outcome.err
    assert api_key == [] and requests(metrics) == []


def test_recreate_bounds_the_request_on_the_host_and_here(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {}))
    assert hops("recreate", "sonarr/sonarr", "--timeout", "90").code == 0
    (call,) = requests(metrics)
    script_args = call.docker[0][call.docker[0].index("-c") + 1 :]
    assert "timeout" in script_args[0] and script_args[2:] == ["90", "doco-cd", "80"]
    assert call.timeout > 90


def test_recreate_defaults_to_a_generous_deadline(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {}))
    assert hops("recreate", "sonarr/sonarr").code == 0
    assert requests(metrics)[0].docker[0][-3] == "600"


@pytest.mark.parametrize("value", ["0", "-5"])
def test_recreate_rejects_a_bad_timeout(hops, fake_host, api_key, value):
    assert hops("recreate", "sonarr/sonarr", "--timeout", value).code == 2
    assert fake_host.calls == [] and api_key == []


def test_recreate_reports_the_host_side_deadline(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", returncode=143)
    outcome = hops("recreate", "sonarr/sonarr", "--timeout", "30")
    assert outcome.code == 1 and "30s" in outcome.err and "may have been interrupted" in outcome.err


def test_recreate_reports_the_client_side_deadline(hops, metrics, api_key, runner):
    fake = runner.process

    def expire(argv, *, input=None, capture=True, timeout=None):
        if input is not None:
            raise HostTimeout(f"ssh did not finish within {timeout}s")
        return fake(argv, input=input, capture=capture, timeout=timeout)

    runner.process = expire
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "may have been interrupted" in outcome.err and "sonarr/sonarr" in outcome.err
