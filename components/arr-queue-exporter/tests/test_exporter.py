"""Characterization of the exporter as it ran in jbruns/home-ops (one process watching Sonarr and Radarr).

These pin the /metrics output the home-ops alerts and dashboards rely on, before the refactor to one target.
"""

import importlib
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

SONARR_RECORDS = [
    {"id": 1, "title": "Show.S01E01", "trackedDownloadState": "downloading", "trackedDownloadStatus": "ok"},
    {"id": 2, "title": "Show.S01E02", "trackedDownloadState": "importPending", "trackedDownloadStatus": "ok"},
    {
        "id": 3,
        "title": 'Odd "quoted" \\ title\nline2',
        "trackedDownloadState": "importBlocked",
        "trackedDownloadStatus": "warning",
        "statusMessages": [
            {"title": "a", "messages": []},
            {"title": "b", "messages": ["No files found are eligible for import", "second"]},
        ],
        "errorMessage": "ignored while a status message exists",
    },
    {
        "id": 4,
        "title": "Show.S01E04",
        "trackedDownloadState": "failedPending",
        "trackedDownloadStatus": "error",
        "errorMessage": "Download failed",
    },
    {"id": 6, "title": None},
]
RADARR_RECORDS = [
    {
        "id": 10,
        "title": "Movie (2024)",
        "trackedDownloadState": "importBlocked",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": "Movie (2024)", "messages": ["Not an upgrade"]}],
    },
]

LEGACY_METRICS = """\
# HELP arr_queue_up 1 if the app's queue API answered.
# TYPE arr_queue_up gauge
arr_queue_up{app="sonarr"} 1
# HELP arr_queue_items Queue items by tracked download state and status.
# TYPE arr_queue_items gauge
arr_queue_items{app="sonarr",state="",status=""} 1
arr_queue_items{app="sonarr",state="downloading",status="ok"} 1
arr_queue_items{app="sonarr",state="failedPending",status="error"} 1
arr_queue_items{app="sonarr",state="importBlocked",status="warning"} 1
arr_queue_items{app="sonarr",state="importPending",status="ok"} 1
# HELP arr_queue_item_stuck 1 for each queue item that needs attention.
# TYPE arr_queue_item_stuck gauge
arr_queue_item_stuck{app="sonarr",id="2",title="Show.S01E02"} 1
# HELP arr_queue_item_info A stuck item's state, status and first message, as labels.
# TYPE arr_queue_item_info gauge
arr_queue_item_info{app="sonarr",id="2",title="Show.S01E02",state="importPending",status="ok",message=""} 1
arr_queue_item_stuck{app="sonarr",id="3",title="Odd \\"quoted\\" \\\\ title line2"} 1
arr_queue_item_info{app="sonarr",id="3",title="Odd \\"quoted\\" \\\\ title line2",state="importBlocked",\
status="warning",message="No files found are eligible for import"} 1
arr_queue_item_stuck{app="sonarr",id="4",title="Show.S01E04"} 1
arr_queue_item_info{app="sonarr",id="4",title="Show.S01E04",state="failedPending",status="error",\
message="Download failed"} 1
arr_queue_up{app="radarr"} 1
arr_queue_items{app="radarr",state="importBlocked",status="warning"} 1
arr_queue_item_stuck{app="radarr",id="10",title="Movie (2024)"} 1
arr_queue_item_info{app="radarr",id="10",title="Movie (2024)",state="importBlocked",status="warning",\
message="Not an upgrade"} 1
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def legacy_exporter(monkeypatch, sonarr_url, radarr_url, sonarr_key="key", radarr_key="key"):
    monkeypatch.setenv("SONARR_URL", sonarr_url)
    monkeypatch.setenv("SONARR_API_KEY", sonarr_key)
    monkeypatch.setenv("RADARR_URL", radarr_url)
    monkeypatch.setenv("RADARR_API_KEY", radarr_key)
    monkeypatch.setenv("TIMEOUT", "2")
    module = importlib.reload(importlib.import_module("arr_queue_exporter.exporter"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.headers["Content-Type"], response.read().decode()


def test_reports_queue_counts_and_each_stuck_item_for_both_apps(fake_arr, monkeypatch):
    sonarr, radarr = fake_arr(SONARR_RECORDS), fake_arr(RADARR_RECORDS)
    with legacy_exporter(monkeypatch, sonarr.url, radarr.url) as exporter:
        status, content_type, body = get(f"{exporter}/metrics")
    assert (status, content_type) == (200, "text/plain; version=0.0.4")
    assert body == LEGACY_METRICS


def test_asks_for_unmatched_items_with_the_api_key(fake_arr, monkeypatch):
    sonarr, radarr = fake_arr([], api_key="s-key"), fake_arr([], api_key="r-key")
    with legacy_exporter(monkeypatch, sonarr.url + "/", radarr.url, "s-key", "r-key") as exporter:
        get(f"{exporter}/metrics")
    assert sonarr.requests == [("/api/v3/queue?pageSize=1000&includeUnknownSeriesItems=true", "s-key")]
    assert radarr.requests == [("/api/v3/queue?pageSize=1000&includeUnknownMovieItems=true", "r-key")]


def test_truncates_long_titles_and_messages_to_200_characters(fake_arr, monkeypatch):
    record = {
        "id": 5,
        "title": "t" * 250,
        "trackedDownloadState": "downloading",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"messages": ["m" * 250]}],
    }
    sonarr, radarr = fake_arr([record]), fake_arr([])
    with legacy_exporter(monkeypatch, sonarr.url, radarr.url) as exporter:
        body = get(f"{exporter}/metrics")[2]
    assert f'arr_queue_item_stuck{{app="sonarr",id="5",title="{"t" * 200}"}} 1\n' in body
    assert f'state="downloading",status="warning",message="{"m" * 200}"}} 1\n' in body


def test_reports_an_app_down_and_keeps_serving_when_its_api_fails(fake_arr, monkeypatch):
    sonarr = fake_arr(SONARR_RECORDS, api_key="other")
    with legacy_exporter(monkeypatch, sonarr.url, f"http://127.0.0.1:{free_port()}") as exporter:
        body = get(f"{exporter}/metrics")[2]
    assert body == (
        "# HELP arr_queue_up 1 if the app's queue API answered.\n"
        "# TYPE arr_queue_up gauge\n"
        'arr_queue_up{app="sonarr"} 0\n'
        'arr_queue_up{app="radarr"} 0\n'
    )


def test_reports_only_up_for_an_empty_queue(fake_arr, monkeypatch):
    sonarr, radarr = fake_arr(None), fake_arr([])
    with legacy_exporter(monkeypatch, sonarr.url, radarr.url) as exporter:
        body = get(f"{exporter}/metrics")[2]
    assert 'arr_queue_up{app="sonarr"} 1\n' in body
    assert "arr_queue_items" not in body


def test_answers_404_off_metrics(fake_arr, monkeypatch):
    sonarr, radarr = fake_arr([]), fake_arr([])
    with legacy_exporter(monkeypatch, sonarr.url, radarr.url) as exporter:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(f"{exporter}/")
    assert error.value.code == 404
