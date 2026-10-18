import pytest

from template_exporter.exporter import Config, collect


@pytest.mark.contract
def test_reports_the_hostname_a_real_whoami_returns(upstream):
    whoami = upstream("whoami")
    metrics = collect(Config(upstream_url=whoami.url, port=0, timeout=5))
    assert "template_upstream_up 1\n" in metrics
    assert 'template_upstream_info{hostname="' in metrics
