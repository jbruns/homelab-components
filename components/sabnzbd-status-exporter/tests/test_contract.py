"""Contract Tests against a real SABnzbd started from the pin in upstreams.yaml."""

import json
import re
import time
import urllib.parse
import urllib.request

import pytest

from sabnzbd_status_exporter.exporter import Config, collect

# One file, one segment, on a group no server will be asked for: SABnzbd tries the configured server at once.
NZB = b"""<?xml version="1.0" encoding="utf-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
<file poster="contract@test" date="1700000000" subject="&quot;contract.bin&quot; yEnc (1/1)">
<groups><group>alt.binaries.test</group></groups>
<segments><segment bytes="1000" number="1">contract-test-segment@invalid</segment></segments>
</file>
</nzb>
"""


def config(sab, api_key=None):
    return Config(sabnzbd_url=sab.url, api_key=api_key or sab.env["SABNZBD__API_KEY"], port=0, timeout=10)


def api(sab, data=None, content_type=None, **params):
    query = urllib.parse.urlencode({**params, "output": "json", "apikey": sab.env["SABNZBD__API_KEY"]})
    request = urllib.request.Request(f"{sab.url}/api?{query}", data=data)
    if content_type:
        request.add_header("Content-Type", content_type)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def add_nzb(sab, nzb):
    boundary = "contract-test-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="name"; filename="contract.nzb"\r\n'
            "Content-Type: application/x-nzb\r\n\r\n"
        ).encode()
        + nzb
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return api(sab, data=body, content_type=f"multipart/form-data; boundary={boundary}", mode="addfile")


def add_server(sab, name, host=None, port=119, **settings):
    """Configure a Usenet server; by default on a host that never resolves (RFC 2606 .invalid)."""
    host = host or f"{name}.invalid"
    api(sab, mode="set_config", section="servers", keyword=name, host=host, port=port, enable=1, **settings)


def sample(metrics, metric, server):
    m = re.search(rf'^{metric}{{server="{re.escape(server)}"(?:,[^}}]*)?}} (\S+)$', metrics, flags=re.M)
    return m and m[1]


def wait_for(sab, metric, server, value="1", seconds=60):
    """The /metrics body once `metric{server}` reads `value`."""
    deadline = time.monotonic() + seconds
    while sample(metrics := collect(config(sab)), metric, server) != value:
        assert time.monotonic() < deadline, f"{metric}{{server={server!r}}} not {value} within {seconds}s:\n{metrics}"
        time.sleep(1)
    return metrics


@pytest.mark.contract
def test_reports_a_real_sabnzbd_up(upstream):
    metrics = collect(config(upstream("sabnzbd")))
    assert re.search(r"^sabnzbd_status_up 1$", metrics, flags=re.M)


@pytest.mark.contract
def test_reports_down_when_sabnzbd_refuses_the_api_key(upstream):
    metrics = collect(config(upstream("sabnzbd"), api_key="wrong"))
    assert re.search(r"^sabnzbd_status_up 0$", metrics, flags=re.M)


@pytest.mark.contract
def test_status_lists_each_server_with_every_field_the_exporter_reads(upstream):
    # The exporter treats a missing field as 0 or "", so a renamed or dropped field would read as healthy.
    sab = upstream("sabnzbd")
    add_server(sab, "contract-fields")

    servers = api(sab, mode="status")["status"]["servers"]

    (raw,) = [s for s in servers if s.get("servername") == "contract-fields"]
    expected_types = {
        "servername": str,
        "serveractive": bool,
        "serveroptional": int,
        "serverpriority": int,
        "servertotalconn": int,
        "servererror": str,
        "serverwarning": str,
    }
    assert {field: type(raw.get(field)) for field in expected_types} == expected_types


@pytest.mark.contract
def test_reports_each_configured_server(upstream):
    sab = upstream("sabnzbd")
    add_server(sab, "contract-settings", connections=3, priority=2, optional=1)

    metrics = collect(config(sab))

    assert sample(metrics, "sabnzbd_usenet_server_active", "contract-settings") == "1"
    assert sample(metrics, "sabnzbd_usenet_server_optional", "contract-settings") == "1"
    assert sample(metrics, "sabnzbd_usenet_server_priority", "contract-settings") == "2"
    assert sample(metrics, "sabnzbd_usenet_server_connections", "contract-settings") == "3"


@pytest.mark.contract
def test_reports_the_error_sabnzbd_hits_on_a_server(upstream):
    sab = upstream("sabnzbd")
    add_server(sab, "contract-unreachable", connections=1)
    assert add_nzb(sab, NZB)["status"] is True

    metrics = wait_for(sab, "sabnzbd_usenet_server_error", "contract-unreachable")

    assert re.search(
        r'^sabnzbd_usenet_server_error_info\{server="contract-unreachable",error="[^"]+"\} 1$', metrics, flags=re.M
    )


@pytest.mark.contract
def test_reports_the_warning_sabnzbd_hits_on_a_server(upstream):
    # Nothing listens on port 9 (discard) inside the SABnzbd container, so every connection is refused and
    # SABnzbd warns about the server instead of failing it.
    sab = upstream("sabnzbd")
    add_server(sab, "contract-refused", host="127.0.0.1", port=9, connections=1)
    assert add_nzb(sab, NZB)["status"] is True

    metrics = wait_for(sab, "sabnzbd_usenet_server_warning", "contract-refused")

    assert re.search(
        r'^sabnzbd_usenet_server_warning_info\{server="contract-refused",warning="[^"]+"\} 1$', metrics, flags=re.M
    )
    assert sample(metrics, "sabnzbd_usenet_server_error", "contract-refused") == "0"
