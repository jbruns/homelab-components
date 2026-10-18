"""The exporter through its public interface: the /metrics and /healthz it serves, its healthcheck and config.

LEGACY_METRICS is what the exporter served in jbruns/home-ops, where one process watched Sonarr and Radarr; the
home-ops alerts and dashboards rely on those series. One instance now watches one app, and two instances must
serve the same series.
"""

import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from arr_queue_exporter.exporter import Config, healthcheck, main, serve

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
def exporter(arr, app="sonarr", api_key="key", url=None):
    config = Config.from_env(
        {"ARR_URL": url or arr.url, "ARR_API_KEY": api_key, "ARR_APP": app, "PORT": "0", "TIMEOUT": "2"}
    )
    server = serve(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.headers["Content-Type"], response.read().decode()


def series(body):
    return {line for line in body.splitlines() if not line.startswith("#")}


def test_reports_queue_counts_and_each_stuck_item(fake_arr):
    with exporter(fake_arr(RADARR_RECORDS), app="radarr") as radarr:
        status, content_type, body = get(f"{radarr}/metrics")
    assert (status, content_type) == (200, "text/plain; version=0.0.4")
    assert body == (
        "# HELP arr_queue_up 1 if the app's queue API answered.\n"
        "# TYPE arr_queue_up gauge\n"
        'arr_queue_up{app="radarr"} 1\n'
        "# HELP arr_queue_items Queue items by tracked download state and status.\n"
        "# TYPE arr_queue_items gauge\n"
        'arr_queue_items{app="radarr",state="importBlocked",status="warning"} 1\n'
        "# HELP arr_queue_item_stuck 1 for each queue item that needs attention.\n"
        "# TYPE arr_queue_item_stuck gauge\n"
        'arr_queue_item_stuck{app="radarr",id="10",title="Movie (2024)"} 1\n'
        "# HELP arr_queue_item_info A stuck item's state, status and first message, as labels.\n"
        "# TYPE arr_queue_item_info gauge\n"
        'arr_queue_item_info{app="radarr",id="10",title="Movie (2024)",state="importBlocked",status="warning",'
        'message="Not an upgrade"} 1\n'
    )


def test_two_instances_serve_the_series_the_combined_home_ops_exporter_did(fake_arr):
    with (
        exporter(fake_arr(SONARR_RECORDS), app="sonarr") as sonarr,
        exporter(fake_arr(RADARR_RECORDS), app="radarr") as radarr,
    ):
        sonarr_body, radarr_body = get(f"{sonarr}/metrics")[2], get(f"{radarr}/metrics")[2]
    assert series(sonarr_body) | series(radarr_body) == series(LEGACY_METRICS)
    assert sonarr_body == LEGACY_METRICS[: LEGACY_METRICS.index('arr_queue_up{app="radarr"}')]


def test_asks_for_unmatched_items_with_the_api_key(fake_arr):
    arr = fake_arr([], api_key="s-key")
    with exporter(arr, api_key="s-key", url=arr.url + "/") as exporter_url:
        get(f"{exporter_url}/metrics")
    assert arr.requests == [
        ("/api/v3/queue?pageSize=1000&includeUnknownSeriesItems=true&includeUnknownMovieItems=true", "s-key")
    ]


def test_truncates_long_titles_and_messages_to_200_characters(fake_arr):
    record = {
        "id": 5,
        "title": "t" * 250,
        "trackedDownloadState": "downloading",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"messages": ["m" * 250]}],
    }
    with exporter(fake_arr([record])) as exporter_url:
        body = get(f"{exporter_url}/metrics")[2]
    assert f'arr_queue_item_stuck{{app="sonarr",id="5",title="{"t" * 200}"}} 1\n' in body
    assert f'state="downloading",status="warning",message="{"m" * 200}"}} 1\n' in body


DOWN = (
    '# HELP arr_queue_up 1 if the app\'s queue API answered.\n# TYPE arr_queue_up gauge\narr_queue_up{app="sonarr"} 0\n'
)


def test_reports_the_app_down_and_keeps_serving_when_it_does_not_answer(fake_arr, capsys):
    with exporter(fake_arr([]), url=f"http://127.0.0.1:{free_port()}") as exporter_url:
        assert get(f"{exporter_url}/metrics")[2] == DOWN
        assert get(f"{exporter_url}/metrics")[2] == DOWN
    assert "sonarr queue API failed: URLError" in capsys.readouterr().err


def test_reports_the_app_down_when_it_rejects_the_api_key(fake_arr, capsys):
    with exporter(fake_arr(SONARR_RECORDS, api_key="other"), api_key="wrong") as exporter_url:
        assert get(f"{exporter_url}/metrics")[2] == DOWN
    err = capsys.readouterr().err
    assert "sonarr queue API failed: HTTPError" in err
    assert "wrong" not in err


def test_reports_only_up_for_an_empty_queue(fake_arr):
    with exporter(fake_arr(None)) as exporter_url:
        body = get(f"{exporter_url}/metrics")[2]
    assert body == DOWN.replace("} 0", "} 1")


def test_answers_404_off_metrics_and_healthz(fake_arr):
    with exporter(fake_arr([])) as exporter_url:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(f"{exporter_url}/")
    assert error.value.code == 404


def test_healthcheck_passes_only_while_serving_and_leaves_the_app_alone(fake_arr):
    arr = fake_arr([])
    config = Config.from_env({"ARR_URL": arr.url, "ARR_API_KEY": "key", "ARR_APP": "sonarr", "PORT": str(free_port())})
    assert healthcheck(config) == 1
    server = serve(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert healthcheck(config) == 0
        assert get(f"http://127.0.0.1:{config.port}/healthz")[2] == "ok\n"
    finally:
        server.shutdown()
    assert arr.requests == []


def test_configuration_comes_from_the_environment_with_documented_defaults():
    required = {"ARR_URL": "http://radarr:7878/", "ARR_API_KEY": "k", "ARR_APP": "radarr"}
    assert Config.from_env(required) == Config(
        url="http://radarr:7878", api_key="k", app="radarr", port=9713, timeout=10.0
    )
    assert Config.from_env({**required, "PORT": "9200", "TIMEOUT": "3"}).port == 9200
    assert Config.from_env({**required, "PORT": "9200", "TIMEOUT": "3"}).timeout == 3.0


def test_refuses_to_start_without_the_target(capsys):
    assert main([], {"ARR_URL": "http://sonarr:8989"}) == 2
    err = capsys.readouterr().err
    assert "ARR_API_KEY" in err and "ARR_APP" in err
