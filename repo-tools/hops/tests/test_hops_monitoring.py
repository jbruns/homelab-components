"""Commands that run in the monitoring Stack's Services fail clearly when the Host does not declare them."""

import pytest
from hops_fakes import write

PROMETHEUS_ONLY = """
services:
  prometheus:
    image: prom/prometheus:v3.15.0@sha256:2222
    container_name: prometheus
"""


@pytest.mark.parametrize(
    ("argv", "service"),
    [
        (["logs", "traefik", "--loki"], "dependencies-render"),
        (["volume", "move", "a_b", "c_d", "--issue", "1"], "dependencies-render"),
        (["alerts"], "alertmanager"),
    ],
)
def test_a_missing_monitoring_service_is_named_before_the_host_is_touched(hops, repo, fake_host, argv, service):
    write(repo, {"tn/monitoring/compose.yaml": PROMETHEUS_ONLY})
    outcome = hops(*argv)
    assert outcome.code == 2
    assert f"Host 'truenas' declares no {service} in its monitoring Stack" in outcome.err
    assert "Traceback" not in outcome.err
    assert fake_host.calls == []


def test_a_host_without_a_monitoring_stack_names_the_service(hops, repo, fake_host):
    (repo / "tn" / "monitoring" / "compose.yaml").unlink()
    outcome = hops("alerts")
    assert outcome.code == 2
    assert "Host 'truenas' declares no prometheus in its monitoring Stack" in outcome.err
    assert fake_host.calls == []


def test_dependencies_render_without_a_pinned_image_is_named(hops, repo, fake_host):
    write(repo, {"tn/monitoring/compose.yaml": PROMETHEUS_ONLY + "  dependencies-render:\n    build: .\n"})
    outcome = hops("logs", "traefik", "--loki")
    assert outcome.code == 2
    assert "Host 'truenas' declares dependencies-render in its monitoring Stack without a pinned image" in outcome.err
    assert fake_host.calls == []
