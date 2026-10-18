import threading

from sabnzbd_status_exporter.exporter import Config, healthcheck, serve

API_KEY = "0123456789abcdef0123456789abcdef"


def test_refuses_to_start_without_an_api_key(run_exporter, unused_port):
    result = run_exporter(PORT=str(unused_port))
    assert result.returncode == 2
    assert "SABNZBD_API_KEY is required" in result.stderr


def test_healthcheck_passes_only_while_the_exporter_answers(sabnzbd, run_exporter, unused_port):
    config = Config(sabnzbd_url=sabnzbd.url, api_key=API_KEY, port=unused_port, timeout=2)
    assert healthcheck(config) == 1
    server = serve(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert healthcheck(config) == 0
        assert run_exporter("healthcheck", PORT=str(config.port)).returncode == 0
    finally:
        server.shutdown()
    assert sabnzbd.paths == []


def test_logs_where_it_serves_without_the_api_key(sabnzbd, start_exporter):
    exporter = start_exporter(SABNZBD_URL=sabnzbd.url, SABNZBD_API_KEY=API_KEY)
    stdout, stderr = exporter.output()
    assert f"serving /metrics on :{exporter.port} for {sabnzbd.url}" in stdout
    assert API_KEY not in stdout + stderr


def test_configuration_comes_from_the_environment_with_documented_defaults():
    assert Config.from_env({}) == Config(sabnzbd_url="http://sabnzbd:8080", api_key="", port=9712, timeout=10.0)
    env = {"SABNZBD_URL": "https://sab.example/sabnzbd/", "SABNZBD_API_KEY": "k", "PORT": "9200", "TIMEOUT": "3"}
    assert Config.from_env(env) == Config(
        sabnzbd_url="https://sab.example/sabnzbd", api_key="k", port=9200, timeout=3.0
    )
