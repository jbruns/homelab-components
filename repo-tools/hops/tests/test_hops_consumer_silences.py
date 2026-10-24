"""The silences of `hops stop`, checked against a consuming repo's real alert rules and scrape config.

These read the consuming repo's Stacks and its monitoring Stack (Prometheus's alerting-rules.yml and
prometheus.yml), so they run only against one:

    HOPS_CONSUMER_REPO=~/src/home-ops pytest -m consumer

The Host is HOPS_CONSUMER_HOST, else the consuming repo's only Host. The Stacks and Services named
below are home-ops's.
"""
import os
import re
from pathlib import Path

import pytest
import yaml

from hops import config
from hops import silences as silences_module
from hops.declared import Declared

CONSUMER = os.environ.get("HOPS_CONSUMER_REPO")
pytestmark = [pytest.mark.consumer,
              pytest.mark.skipif(not CONSUMER, reason="reads a consuming repo; set HOPS_CONSUMER_REPO")]

if CONSUMER:
    REPO = Path(CONSUMER).expanduser().resolve()
    STACK_ROOT = config.load_hosts(REPO).pick(os.environ.get("HOPS_CONSUMER_HOST")).stack_root
    MONITORING = REPO / STACK_ROOT / "monitoring" / "prometheus"


def rule(name: str) -> dict:
    groups = yaml.safe_load((MONITORING / "alerting-rules.yml").read_text())["groups"]
    return next(r for g in groups for r in g["rules"] if r.get("alert") == name)


def scrape_target(job: str, **having) -> dict:
    """The labels Prometheus gives a static target's series: job, instance and the target labels."""
    prom = yaml.safe_load((MONITORING / "prometheus.yml").read_text())
    for sc in prom["scrape_configs"]:
        for static in sc.get("static_configs") or []:
            labels = static.get("labels") or {}
            if sc["job_name"] == job and having.items() <= labels.items():
                return {"job": job, "instance": static["targets"][0], **labels}
    raise AssertionError(f"no {job} target with {having}")


def alert(name: str, series: dict) -> dict:
    """The labels the alert carries for a series: its expr's label_replace copies, then the rule's labels."""
    r = rule(name)
    labels = dict(series)
    for dst, src in re.findall(r'"(\w+)", "\$1", "(\w+)", "\(\.\+\)"', r["expr"]):
        if src in labels:
            labels[dst] = labels[src]
    return {**labels, **(r.get("labels") or {}), "alertname": name}


def silenced(matcher_sets: list[dict], labels: dict) -> bool:
    return any(m.items() <= labels.items() for m in matcher_sets)


def stop_plan(*stopped: tuple[str, str]) -> list:
    """The silences `hops stop` would create for (stack/service, container) pairs, from the real repo."""
    declared = Declared.load(REPO, STACK_ROOT)
    found = [(declared.resolve_one(key), container) for key, container in stopped]
    return silences_module.plan(declared, found, silences_module.Monitoring.load(REPO, STACK_ROOT))


def stop_silences(key: str, container: str) -> list[dict]:
    return [s.matchers for s in stop_plan((key, container))]


def probe_failed(stack: str, service: str, container: str, job: str = "probe", **labels) -> dict:
    """ProbeFailed as the probe jobs relabel it from the probed container's Compose labels."""
    service_ = Declared.load(REPO, STACK_ROOT).get(stack, service)
    return alert("ProbeFailed", {"job": job, "instance": "https://example", "container": container,
                                 "service": service, "stack": stack, "tier": service_.tier, **labels})


def test_stopping_traefik_silences_its_target_down_page():
    found = stop_silences("edge/traefik", "traefik")
    assert silenced(found, alert("TargetDown", scrape_target("traefik")))
    assert not silenced(found, alert("TargetDown", scrape_target("crowdsec")))


def test_stopping_a_datastore_silences_datastore_down():
    found = stop_silences("nextcloud/mariadb", "nextcloud-mariadb")
    assert silenced(found, alert("DatastoreDown", scrape_target("mysqld", datastore="nextcloud-mariadb")))
    assert not silenced(found, alert("DatastoreDown", scrape_target("mysqld", datastore="vaultwarden-mariadb")))


def test_stopping_a_service_without_container_name_silences_its_target_by_service_name():
    found = stop_silences("doco-cd/doco-cd", "doco-cd-doco-cd-6")
    assert silenced(found, alert("TargetDown", scrape_target("doco-cd")))


def test_stopping_a_service_silences_container_down():
    found = stop_silences("sonarr/sonarr", "sonarr")
    assert silenced(found, alert("ContainerDown", {"stack": "sonarr", "service": "sonarr", "name": "sonarr",
                                                   "tier": "warning"}))


def test_stopping_traefik_silences_every_probe_its_own_missing_probe_no_longer_inhibits():
    """Docker SD drops a stopped container's Probe, so ProbeFailed{container="traefik"} never fires."""
    plan = stop_plan(("edge/traefik", "traefik"))
    found = [s.matchers for s in plan]
    assert silenced(found, probe_failed("vaultwarden", "vaultwarden", "vaultwarden"))
    assert silenced(found, probe_failed("nextcloud", "nextcloud", "nextcloud"))
    assert silenced(found, probe_failed("calibre-web-automated", "calibre-web-automated", "calibre-web-automated",
                                        job="probe-idp", auth="forward"))
    assert silenced(found, alert("IdpForwardAuthFailing", {}))
    (every_probe,) = [s for s in plan if s.matchers == {"alertname": "ProbeFailed"}]
    assert "all Probes" in every_probe.reason


def test_stopping_other_services_leaves_other_probes_paging():
    for key, container in [("sonarr/sonarr", "sonarr"), ("edge/crowdsec", "crowdsec"),
                           ("nextcloud/mariadb", "nextcloud-mariadb")]:
        found = stop_silences(key, container)
        assert not silenced(found, probe_failed("vaultwarden", "vaultwarden", "vaultwarden")), key


def test_stopping_a_datastore_silences_the_services_in_its_stack_that_depend_on_it():
    found = stop_silences("vaultwarden/mariadb", "vaultwarden-mariadb")
    assert silenced(found, probe_failed("vaultwarden", "vaultwarden", "vaultwarden"))
    assert silenced(found, alert("TargetDown", scrape_target("mysqld", datastore="vaultwarden-mariadb")))
    assert not silenced(found, alert("DatastoreDown", scrape_target("mysqld", datastore="nextcloud-mariadb")))
    nextcloud = stop_silences("nextcloud/mariadb", "nextcloud-mariadb")
    assert silenced(nextcloud, probe_failed("nextcloud", "nextcloud", "nextcloud"))
    assert silenced(nextcloud, alert("ContainerDown", {"stack": "nextcloud", "service": "nextcloud-cron",
                                                       "name": "nextcloud-cron", "tier": "warning"}))
    assert not silenced(found, probe_failed("nextcloud", "nextcloud", "nextcloud"))


def test_stopping_a_service_silences_its_stacks_alerts_that_name_no_service():
    found = stop_silences("ops/stack-back", "stack-back")
    assert silenced(found, alert("BackupStale", {"kind": "volumes"}))
    assert silenced(found, alert("BackupMetricsMissing", {}))
    assert not silenced(stop_silences("sonarr/sonarr", "sonarr"), alert("BackupStale", {"kind": "volumes"}))


def test_stopping_a_monitoring_service_never_silences_the_heartbeat():
    found = stop_silences("monitoring/grafana", "grafana")
    assert not silenced(found, alert("Watchdog", {}))


@pytest.mark.parametrize("key, container", [("edge/traefik", "traefik"), ("ops/stack-back", "stack-back"),
                                            ("vaultwarden/mariadb", "vaultwarden-mariadb"),
                                            ("monitoring/prometheus", "prometheus")])
def test_every_silence_is_narrow_and_explained(key, container):
    for s in stop_plan((key, container)):
        assert s.matchers and set(s.matchers) != {"stack"} and s.reason, s
