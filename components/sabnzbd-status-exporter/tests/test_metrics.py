"""The /metrics a running exporter serves for what SABnzbd's status API returns.

These pin the behaviour of the exporter as it ran in jbruns/home-ops (metric names, labels, values, errors), so
home-ops alerts and dashboards keep working. They drive the real process over HTTP against a stand-in SABnzbd.
"""

import re
import urllib.error
import urllib.parse
import urllib.request

import pytest

API_KEY = "0123456789abcdef0123456789abcdef"


def without_duration(metrics):
    """The body with the one sample that varies between scrapes masked."""
    return re.sub(r"^(sabnzbd_status_scrape_duration_seconds) \S+$", r"\1 <d>", metrics, flags=re.M)


def server(name, **fields):
    return {
        "servername": name,
        "serveractive": True,
        "serveroptional": 0,
        "serverpriority": 0,
        "servertotalconn": 8,
        "servererror": "",
        "serverwarning": "",
        **fields,
    }


HEADER = """\
# HELP sabnzbd_status_up 1 if the status API answered.
# TYPE sabnzbd_status_up gauge
sabnzbd_status_up {up}
# HELP sabnzbd_status_scrape_duration_seconds Time taken to read the status API.
# TYPE sabnzbd_status_scrape_duration_seconds gauge
sabnzbd_status_scrape_duration_seconds <d>
"""


TWO_SERVERS = """\
# HELP sabnzbd_usenet_server_active 1 if the server is enabled in SABnzbd.
# TYPE sabnzbd_usenet_server_active gauge
sabnzbd_usenet_server_active{server="news.example.com"} 1
sabnzbd_usenet_server_active{server="backup"} 1
# HELP sabnzbd_usenet_server_optional 1 if the server is optional (a failure does not fail the download).
# TYPE sabnzbd_usenet_server_optional gauge
sabnzbd_usenet_server_optional{server="news.example.com"} 0
sabnzbd_usenet_server_optional{server="backup"} 1
# HELP sabnzbd_usenet_server_priority Server priority; 0 is tried first.
# TYPE sabnzbd_usenet_server_priority gauge
sabnzbd_usenet_server_priority{server="news.example.com"} 0
sabnzbd_usenet_server_priority{server="backup"} 1
# HELP sabnzbd_usenet_server_connections Configured connections.
# TYPE sabnzbd_usenet_server_connections gauge
sabnzbd_usenet_server_connections{server="news.example.com"} 50
sabnzbd_usenet_server_connections{server="backup"} 10
# HELP sabnzbd_usenet_server_error 1 while SABnzbd reports an error for the server.
# TYPE sabnzbd_usenet_server_error gauge
sabnzbd_usenet_server_error{server="news.example.com"} 0
sabnzbd_usenet_server_error{server="backup"} 1
# HELP sabnzbd_usenet_server_error_info The server's current error, as a label.
# TYPE sabnzbd_usenet_server_error_info gauge
sabnzbd_usenet_server_error_info{server="backup",error="Failed login for server backup"} 1
# HELP sabnzbd_usenet_server_warning 1 while SABnzbd reports a warning for the server.
# TYPE sabnzbd_usenet_server_warning gauge
sabnzbd_usenet_server_warning{server="news.example.com"} 0
sabnzbd_usenet_server_warning{server="backup"} 1
# HELP sabnzbd_usenet_server_warning_info The server's current warning, as a label.
# TYPE sabnzbd_usenet_server_warning_info gauge
sabnzbd_usenet_server_warning_info{server="backup",warning="Too many connections"} 1
"""


def test_reports_each_server_with_its_error_and_warning_grouped_by_metric(sabnzbd, start_exporter):
    sabnzbd.body = {
        "status": {
            "servers": [
                server("news.example.com", serverpriority=0, servertotalconn=50),
                server(
                    "backup",
                    serveroptional=1,
                    serverpriority=1,
                    servertotalconn=10,
                    servererror="  Failed login for server backup  ",
                    serverwarning="Too many connections",
                ),
            ]
        }
    }
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)

    assert without_duration(exporter.metrics()) == HEADER.format(up=1) + TWO_SERVERS


def test_reads_the_status_api_once_per_scrape_with_the_api_key(sabnzbd, start_exporter):
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url + "/", SABNZBD_API_KEY=API_KEY)
    exporter.metrics()
    exporter.metrics()

    assert len(sabnzbd.paths) == 2
    path, _, query = sabnzbd.paths[0].partition("?")
    assert path == "/api"
    assert urllib.parse.parse_qs(query) == {"mode": ["status"], "output": ["json"], "apikey": [API_KEY]}


def test_serves_the_prometheus_text_format(sabnzbd, start_exporter):
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    status, content_type, _ = exporter.get("/metrics")
    assert (status, content_type) == (200, "text/plain; version=0.0.4")


def test_answers_404_off_the_metrics_path(sabnzbd, start_exporter):
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    with pytest.raises(urllib.error.HTTPError) as e:
        exporter.get("/")
    assert e.value.code == 404
    assert sabnzbd.paths == []


@pytest.mark.parametrize("status", [{}, {"servers": None}, {"servers": []}])
def test_reports_up_with_no_servers_when_none_are_configured(sabnzbd, start_exporter, status):
    sabnzbd.body = {"status": status}
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    assert without_duration(exporter.metrics()) == HEADER.format(up=1)


def test_treats_missing_fields_as_zero(sabnzbd, start_exporter):
    sabnzbd.body = {"status": {"servers": [{"servername": "bare", "serverpriority": None, "servererror": None}]}}
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    samples = [line for line in exporter.metrics().splitlines() if line.startswith("sabnzbd_usenet_server_")]
    assert samples == [
        'sabnzbd_usenet_server_active{server="bare"} 0',
        'sabnzbd_usenet_server_optional{server="bare"} 0',
        'sabnzbd_usenet_server_priority{server="bare"} 0',
        'sabnzbd_usenet_server_connections{server="bare"} 0',
        'sabnzbd_usenet_server_error{server="bare"} 0',
        'sabnzbd_usenet_server_warning{server="bare"} 0',
    ]


def test_escapes_and_truncates_label_values(sabnzbd, start_exporter):
    error = 'Bad "auth"\\ for\nserver ' + "x" * 300
    sabnzbd.body = {"status": {"servers": [server('a"b\\c', servererror=error)]}}
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    (info,) = [line for line in exporter.metrics().splitlines() if line.startswith("sabnzbd_usenet_server_error_info")]
    assert info == (
        'sabnzbd_usenet_server_error_info{server="a\\"b\\\\c",error="Bad \\"auth\\"\\\\ for server '
        + "x" * (200 - len('Bad "auth"\\ for\nserver '))
        + '"} 1'
    )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, b"API Key Incorrect"),  # SABnzbd's answer to a wrong API key
        (200, b"not json"),
        (200, b'{"version": "5.1.3"}'),
        (500, b"{}"),
    ],
)
def test_reports_down_and_keeps_serving_when_the_status_api_fails(sabnzbd, start_exporter, status, body):
    sabnzbd.status, sabnzbd.body = status, body
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    assert without_duration(exporter.metrics()) == HEADER.format(up=0)
    assert without_duration(exporter.metrics()) == HEADER.format(up=0)


def test_reports_down_when_sabnzbd_does_not_answer(start_exporter, unused_port):
    exporter = start_exporter(SABNZBD_URL=f"http://127.0.0.1:{unused_port}", SABNZBD_API_KEY=API_KEY)
    assert without_duration(exporter.metrics()) == HEADER.format(up=0)


def test_logs_a_failed_read_without_the_api_key(sabnzbd, start_exporter):
    sabnzbd.status, sabnzbd.body = 403, b"API Key Incorrect"
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    exporter.metrics()
    stdout, stderr = exporter.output()
    assert "status API failed: HTTPError" in stdout + stderr
    assert API_KEY not in stdout + stderr
