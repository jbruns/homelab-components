import json
import urllib.request

import pytest

from arr_queue_exporter.exporter import QUEUE_PATH, Config, collect

APPS = [("sonarr", "SONARR__AUTH__APIKEY"), ("radarr", "RADARR__AUTH__APIKEY")]


def config(arr, app, api_key):
    return Config(url=arr.url, api_key=api_key, app=app, port=0, timeout=10)


@pytest.mark.contract
@pytest.mark.parametrize(("app", "key_var"), APPS)
def test_the_queue_answers_one_page_of_records_including_unmatched_items(upstream, app, key_var):
    arr = upstream(app)
    request = urllib.request.Request(arr.url + QUEUE_PATH, headers={"X-Api-Key": arr.env[key_var]})
    with urllib.request.urlopen(request, timeout=10) as response:
        queue = json.load(response)
    assert queue["pageSize"] == 1000
    assert isinstance(queue["records"], list)


@pytest.mark.contract
@pytest.mark.parametrize(("app", "key_var"), APPS)
def test_reports_the_app_up_with_its_queue(upstream, app, key_var):
    arr = upstream(app)
    metrics = collect(config(arr, app, arr.env[key_var]))
    assert f'arr_queue_up{{app="{app}"}} 1\n' in metrics


@pytest.mark.contract
@pytest.mark.parametrize(("app", "key_var"), APPS)
def test_reports_the_app_down_when_it_rejects_the_api_key(upstream, app, key_var):
    arr = upstream(app)
    metrics = collect(config(arr, app, "not-" + arr.env[key_var]))
    assert f'arr_queue_up{{app="{app}"}} 0\n' in metrics
