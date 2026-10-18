import json
import urllib.request

import pytest

from arr_queue_exporter.exporter import QUEUE_PATH, Config, collect

APPS = ["sonarr", "radarr"]


def api_key(arr):
    """The key upstreams.yaml started the app with (`<APP>__AUTH__APIKEY`)."""
    return arr.env[f"{arr.name.upper()}__AUTH__APIKEY"]


def config(arr, key):
    return Config(url=arr.url, api_key=key, app=arr.name, port=0, timeout=10)


@pytest.mark.contract
@pytest.mark.parametrize("app", APPS)
def test_the_queue_answers_one_page_of_records_including_unmatched_items(upstream, app):
    arr = upstream(app)
    request = urllib.request.Request(arr.url + QUEUE_PATH, headers={"X-Api-Key": api_key(arr)})
    with urllib.request.urlopen(request, timeout=10) as response:
        queue = json.load(response)
    assert queue["pageSize"] == 1000
    assert isinstance(queue["records"], list)


@pytest.mark.contract
@pytest.mark.parametrize("app", APPS)
def test_reports_the_app_up_with_its_queue(upstream, app):
    arr = upstream(app)
    metrics = collect(config(arr, api_key(arr)))
    assert f'arr_queue_up{{app="{app}"}} 1\n' in metrics


@pytest.mark.contract
@pytest.mark.parametrize("app", APPS)
def test_reports_the_app_down_when_it_rejects_the_api_key(upstream, app):
    arr = upstream(app)
    metrics = collect(config(arr, "not-" + api_key(arr)))
    assert f'arr_queue_up{{app="{app}"}} 0\n' in metrics
