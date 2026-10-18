import json
import subprocess
import time
import urllib.request

import pytest

from arr_queue_exporter.exporter import QUEUE_PATH, Config, collect

APPS = ["sonarr", "radarr"]
# A finished download the app never grabbed and cannot match: it stays in the queue as importBlocked.
UNMATCHED = {
    "sonarr": "Unknown.Show.S01E01.720p.HDTV.x264-GRP.mkv",
    "radarr": "Unknown.Movie.2020.720p.BluRay.x264-GRP.mkv",
}
# The Usenet Blackhole download client reports each file in its watch folder as a download, so the queue needs
# no download client container. A new file counts as downloading for 30s before it completes.
BLACKHOLE = {
    "name": "contract-blackhole",
    "enable": True,
    "protocol": "usenet",
    "priority": 1,
    "implementation": "UsenetBlackhole",
    "configContract": "UsenetBlackholeSettings",
    "fields": [{"name": "nzbFolder", "value": "/downloads/nzb"}, {"name": "watchFolder", "value": "/downloads/watch"}],
    "tags": [],
}
SEED_TIMEOUT = 180
_seeded = {}


def api_key(arr):
    """The key upstreams.yaml started the app with (`<APP>__AUTH__APIKEY`)."""
    return arr.env[f"{arr.name.upper()}__AUTH__APIKEY"]


def api(arr, path, body=None):
    request = urllib.request.Request(
        arr.url + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-Api-Key": api_key(arr), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def unmatched_records(arr, path=QUEUE_PATH):
    return [r for r in api(arr, path)["records"] if r.get("outputPath", "").endswith(UNMATCHED[arr.name])]


def config(arr, key):
    return Config(url=arr.url, api_key=key, app=arr.name, port=0, timeout=10)


@pytest.fixture
def seeded(upstream):
    """Start both apps, each with one unmatched download stuck in its queue; returns {app: running upstream}."""
    if not _seeded:
        apps = {app: upstream(app) for app in APPS}
        for app, arr in apps.items():
            script = f"mkdir -p /downloads/nzb /downloads/watch && touch '/downloads/watch/{UNMATCHED[app]}'"
            subprocess.run(
                ["docker", "exec", arr.container, "sh", "-c", script + " && chown -R abc:abc /downloads"], check=True
            )
            api(arr, "/api/v3/downloadclient", BLACKHOLE)
        deadline = time.monotonic() + SEED_TIMEOUT
        waiting = dict(apps)
        while waiting and time.monotonic() < deadline:
            for app, arr in list(waiting.items()):
                api(arr, "/api/v3/command", {"name": "RefreshMonitoredDownloads"})
                if any(r.get("trackedDownloadState") == "importBlocked" for r in unmatched_records(arr)):
                    del waiting[app]
            time.sleep(3)
        if waiting:
            pytest.fail(f"no importBlocked queue item after {SEED_TIMEOUT}s in {', '.join(waiting)}")
        _seeded.update(apps)
    return _seeded


@pytest.mark.contract
@pytest.mark.parametrize("app", APPS)
def test_the_queue_lists_an_unmatched_download_with_the_fields_the_exporter_reads(seeded, app):
    arr = seeded[app]
    queue = api(arr, QUEUE_PATH)
    assert queue["pageSize"] == 1000
    [record] = unmatched_records(arr)
    assert isinstance(record["id"], int)
    assert isinstance(record["title"], str) and record["title"]
    assert (record["trackedDownloadState"], record["trackedDownloadStatus"]) == ("importBlocked", "warning")
    messages = [text for m in record["statusMessages"] for text in m["messages"]]
    assert messages and all(isinstance(text, str) for text in messages)
    assert isinstance(record.get("errorMessage"), (str, type(None)))
    assert unmatched_records(arr, "/api/v3/queue?pageSize=1000") == [], "unmatched items need the include parameter"


@pytest.mark.contract
@pytest.mark.parametrize("app", APPS)
def test_reports_the_unmatched_download_as_stuck_with_its_message(seeded, app):
    arr = seeded[app]
    [record] = unmatched_records(arr)
    message = record["statusMessages"][0]["messages"][0]
    item = f'app="{app}",id="{record["id"]}",title="{record["title"]}"'
    metrics = collect(config(arr, api_key(arr)))
    assert f'arr_queue_up{{app="{app}"}} 1\n' in metrics
    assert f'arr_queue_items{{app="{app}",state="importBlocked",status="warning"}} 1\n' in metrics
    assert f"arr_queue_item_stuck{{{item}}} 1\n" in metrics
    assert (
        f'arr_queue_item_info{{{item},state="importBlocked",status="warning",message="{message[:200]}"}} 1\n' in metrics
    )


@pytest.mark.contract
@pytest.mark.parametrize("app", APPS)
def test_reports_the_app_down_when_it_rejects_the_api_key(upstream, app):
    arr = upstream(app)
    metrics = collect(config(arr, "not-" + api_key(arr)))
    assert f'arr_queue_up{{app="{app}"}} 0\n' in metrics
