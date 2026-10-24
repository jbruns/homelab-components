"""amtool inside the alertmanager container, authenticated without the password leaving the Host.

Alertmanager requires basic auth (its web.sops.yml). Prometheus holds the shared `prometheus`
password as a Compose secret (prometheus.yml `alerting:`), so the Host pipes that file from the
prometheus container into the alertmanager container, where sh writes amtool's HTTP config to
amtool's stdin. The password is never in a process argument or on this machine.
"""

from hops import docker
from hops.declared import MONITORING, Declared
from hops.runner import Result, Runner

USERNAME = "prometheus"
PASSWORD_FILE = "/run/secrets/prometheus_password"
URL = "http://127.0.0.1:9093"
# `read` and `printf` are sh builtins, and sed gets the password on stdin; YAML single quotes
# escape ' as ''. "$@" holds amtool's arguments.
SCRIPT = (
    'IFS= read -r p; p=$(printf %s "$p" | sed "s/\'/\'\'/g"); '
    f'printf "basic_auth:\\n  username: {USERNAME}\\n  password: \'%s\'\\n" "$p" | '
    f'amtool --alertmanager.url={URL} --http.config.file=/dev/stdin "$@"'
)


SERVICES = ("prometheus", "alertmanager")  # the monitoring Stack's Services amtool runs through


def monitoring_undeclared(declared: Declared) -> bool:
    """True only on a Host mid-cutover: no Stack is named monitoring and no Stack has a Service named
    prometheus or alertmanager. Anything else is declared monitoring, misconfigured or not, and amtool fails
    naming what is missing, so a stop is never left unsilenced by a typo or a renamed Service."""
    return MONITORING not in declared.stacks and not any(s.name in SERVICES for s in declared.services)


def amtool(runner: Runner, declared, host: str, *args: str, check: bool = True) -> Result:
    """Run `amtool <args>` against Alertmanager on Host `host`; returns its captured output."""
    prometheus, alertmanager = (declared.monitoring(host, name) for name in SERVICES)
    prometheus = docker.container_ref(runner, prometheus)
    alertmanager = docker.container_ref(runner, alertmanager)
    return runner.pipeline(
        ["exec", prometheus, "cat", PASSWORD_FILE],
        ["exec", "-i", alertmanager, "sh", "-c", SCRIPT, "amtool", *args],
        check=check,
    )
