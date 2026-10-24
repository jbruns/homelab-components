"""Characterization tests for validate.py: they pin what each check accepts and rejects."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CIDRS, SOPS_ENV, commit, configure, write
from registry_fixtures import RECORDINGS, Replay, Response

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOPS_ONLY = "sops_version=3\n"


def messages(validate) -> str:
    return "\n".join(validate.failures)


# --- pure helpers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("PROXY_SUBNET", "10.99.0.0/24"),
        ("DOCKER_HOST_IPV4", "10.99.0.1"),
        ("APP_UID", "1000"),
        ("APP_GID", "1000"),
        ("WEB_PORT", "1000"),
        ("ZFS_POOL_ROOT_HDD", "/mnt/placeholder"),
        ("DOCKER_DATA_ROOT", "/mnt/placeholder"),
        ("EMAIL", "placeholder"),
    ],
)
def test_placeholder(validate, key, value):
    assert validate.placeholder(key) == value


def test_var_re_skips_escaped_dollar(validate):
    assert validate.VAR_RE.findall("a ${ONE} $${TWO} ${THREE:-x}") == ["ONE", "THREE"]


def test_env_keys_ignores_comments_blanks_and_sops_metadata(validate, tmp_path):
    f = tmp_path / "x.env"
    f.write_text("# c\n\nA=1\n B = 2\nsops_version=3\nnot a pair\n")
    assert validate.env_keys(f) == {"A", "B"}
    assert validate.env_keys(tmp_path / "missing") == set()


def test_compose_files_follows_includes(validate, repo):
    files = validate.compose_files(repo / "tn/monitoring/compose.yaml")
    assert [f.relative_to(repo).as_posix() for f in files] == [
        "tn/monitoring/compose.yaml",
        "tn/monitoring/grafana/service.yaml",
        "tn/monitoring/alloy/service.yaml",
        "tn/monitoring/loki/service.yaml",
        "tn/monitoring/alertmanager/service.yaml",
        "tn/monitoring/prometheus/service.yaml",
    ]


def test_sensitive_refs(validate, tmp_path):
    f = tmp_path / "service.yaml"
    f.write_text(
        "services:\n"
        "  a:\n"
        "    env_file: [one.sops.env, {path: two.sops.env}]\n"
        "    volumes:\n"
        "      - {type: bind, source: ./cfg.sops.yml, target: /c, x-sops: true}\n"
        "      - {type: bind, source: ./plain.yml, target: /p}\n"
        "secrets:\n"
        "  pw: {file: pw.sops.txt}\n"
    )
    assert [p.name for p in validate.sensitive_refs(f)] == [
        "one.sops.env",
        "two.sops.env",
        "cfg.sops.yml",
        "pw.sops.txt",
    ]


BASELINE_SVC = {
    "healthcheck": {"test": ["CMD", "true"]},
    "security_opt": ["no-new-privileges:true"],
    "image": "x@sha256:abc",
    "deploy": {"resources": {"limits": {"memory": "64M"}}},
    "cap_drop": ["ALL"],
}


def test_baseline_met(validate):
    assert validate.baseline_problems("app", dict(BASELINE_SVC)) == ([], [])


def test_baseline_missing_item_without_reason(validate):
    svc = {**BASELINE_SVC, "cap_drop": []}
    problems, _ = validate.baseline_problems("app", svc)
    assert problems == ["app: misses Baseline item 'cap_drop' without a baseline.exempt.cap_drop reason"]


def test_baseline_exemption_is_reported(validate):
    svc = {**BASELINE_SVC, "image": "x:1", "labels": {"baseline.exempt.digest": "local build"}}
    assert validate.baseline_problems("app", svc) == ([], ["app: digest: local build"])


def test_baseline_stale_and_unknown_exemptions(validate):
    svc = {**BASELINE_SVC, "labels": {"baseline.exempt.digest": "x", "baseline.exempt.bogus": "y"}}
    problems, _ = validate.baseline_problems("app", svc)
    assert "app: stale Exemption baseline.exempt.digest; the Service meets it" in problems
    assert any("baseline.exempt.bogus names no Baseline item" in p for p in problems)


def test_baseline_anonymous_volumes(validate):
    assert validate.baseline_problems("app", dict(BASELINE_SVC), uncovered=[]) == ([], [])
    problems, _ = validate.baseline_problems("app", dict(BASELINE_SVC), uncovered=["/config", "/plugin"])
    assert problems == [
        "app: misses Baseline item 'anonymous-volumes' without a baseline.exempt.anonymous-volumes "
        "reason (image VOLUME paths with no mount: /config, /plugin)"
    ]


def test_baseline_anonymous_volumes_exemption(validate):
    svc = {**BASELINE_SVC, "labels": {"baseline.exempt.anonymous-volumes": "image writes a scratch dir"}}
    assert validate.baseline_problems("app", svc, uncovered=["/x"]) == (
        [],
        ["app: anonymous-volumes: image writes a scratch dir"],
    )
    problems, _ = validate.baseline_problems("app", svc, uncovered=[])
    assert problems == ["app: stale Exemption baseline.exempt.anonymous-volumes; the Service meets it"]
    # Not checked (registry outage, reported on its own): neither stale nor missing.
    assert validate.baseline_problems("app", svc) == ([], [])


def test_baseline_rejects_declared_anonymous_volume(validate):
    svc = {**BASELINE_SVC, "volumes": [{"type": "volume", "target": "/data", "volume": {}}]}
    problems, _ = validate.baseline_problems("app", svc)
    assert problems == ["app: volume at /data has no source (an anonymous volume); name it, bind it or make it a tmpfs"]


def test_uninspectable_image_needs_an_anonymous_volumes_exemption(validate, tmp_path):
    # A digest Exemption must not hide the anonymous-volume item.
    svc = {**BASELINE_SVC, "image": "nginx:1", "labels": {"baseline.exempt.digest": "built locally"}}
    uncovered = validate.anonymous_volume_check("app", "web", svc, None, {}, tmp_path)
    problems, _ = validate.baseline_problems("web", svc, uncovered)
    assert problems == [
        "web: cannot inspect image VOLUMEs without a digest; mount each VOLUME path and add a "
        "baseline.exempt.anonymous-volumes reason"
    ]
    svc["labels"]["baseline.exempt.anonymous-volumes"] = "image declares none"
    assert validate.baseline_problems("web", svc, uncovered) == (
        [],
        ["web: digest: built locally", "web: anonymous-volumes: image declares none"],
    )


def test_anonymous_volume_check_supports_only_linux_amd64(validate, tmp_path):
    svc = {"image": APPRISE, "platform": "linux/arm64"}
    uncovered = validate.anonymous_volume_check("app", "web", svc, None, {}, tmp_path)
    problems, _ = validate.baseline_problems("web", {**BASELINE_SVC, **svc}, uncovered)
    assert problems == [
        "web: cannot inspect image VOLUMEs for platform linux/arm64 (validate reads only "
        "linux/amd64); mount each VOLUME path and add a baseline.exempt.anonymous-volumes reason"
    ]


def test_baseline_rejects_volumes_from(validate):
    problems, _ = validate.baseline_problems("web", {**BASELINE_SVC, "volumes_from": ["db"]}, uncovered=[])
    assert problems == ["web: volumes_from unsupported by validate; mount the volumes by name instead"]


def test_uncovered_image_volumes_need_a_mount_at_the_exact_path(validate):
    svc = {
        "volumes": [
            {"type": "volume", "source": "data", "target": "/data/"},
            {"type": "bind", "source": "/srv", "target": "/srv"},
            {"type": "tmpfs", "target": "/run"},
            {"type": "volume", "target": "/anon"},
        ],
        "tmpfs": ["/attach:uid=1000,mode=0700", "/tmp"],
    }
    image = ["/data", "/srv", "/run", "/attach", "/tmp", "/anon", "/srv/sub", "/config"]
    # A mount above a VOLUME path does not cover it: Docker still creates the anonymous volume there.
    assert validate.uncovered_volumes(svc, image) == ["/anon", "/config", "/srv/sub"]


VOLUME_STACKS = {"app", "edge", "monitoring"}


@pytest.mark.parametrize(
    "volumes,expected",
    [
        ({"data": {"name": "app_data", "external": True}}, []),
        ({"cache": {"name": "app_cache", "labels": {"homeops.regenerable": "rebuilt at start"}}}, []),
        ({"cache": {"name": "app_cache", "labels": ["homeops.regenerable=rebuilt at start"]}}, []),
        ({"log": {"name": "edge_traefik_log", "external": True}}, []),
        ({"old": {"name": "media_app_config", "external": True}}, []),
        ({"data": {"external": True}}, ["app: volume data has no explicit name:"]),
        (
            {"cache": {"name": "app_cache", "labels": {"homeops.regenerable": None}}},
            [
                "app: volume app_cache must be external: true (it holds data) or carry a homeops.regenerable label "
                "saying what regenerates it"
            ],
        ),
        (
            {"data": None},
            [
                "app: volume data has no explicit name:",
                "app: volume data must be external: true (it holds data) or carry a homeops.regenerable label "
                "saying what regenerates it",
            ],
        ),
        (
            {"cache": {"name": "app_cache", "labels": {"homeops.regenerable": " "}}},
            [
                "app: volume app_cache must be external: true (it holds data) or carry a homeops.regenerable label "
                "saying what regenerates it"
            ],
        ),
        (
            {"data": {"name": "web_data", "external": True}},
            ["app: volume name web_data must start with app_ (ADR 0010)"],
        ),
        (
            {"log": {"name": "edge_log", "labels": {"homeops.regenerable": "x"}}},
            ["app: volume name edge_log must start with app_ (ADR 0010)"],
        ),
    ],
)
def test_volume_problems(validate, monkeypatch, volumes, expected):
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", {"media_app_config"})
    assert validate.volume_problems("app", volumes, VOLUME_STACKS) == expected


def test_router_names_and_rules(validate):
    labels = {
        "traefik.http.routers.app-https.rule": "Host(`a`)",
        "traefik.http.routers.app-https.middlewares": "protected@file",
        "traefik.http.routers.wrong.rule": "Host(`a`)",
        "traefik.http.routers.app-client-api.rule": "Host(`a`) && Header(`X`, `y`)",
        "traefik.http.services.other.loadbalancer.server.port": "80",
    }
    problems = validate.router_problems("app", labels)
    assert problems == [
        "app: router app-client-api skips forward auth, so it needs strip-idp-headers",
        "app: Client Path app-client-api needs crowdsec-bouncer and a rate limit",
        "app: router 'wrong' must be app-http|-https|-health|-client-<protocol>",
        "app: a Protected Service needs a probe.idp_url label",
        "app: a Protected Service needs a app-health router for its Probe",
        "app: Traefik service 'other' must be named 'app' (Http5xxRatio maps it to the container)",
    ]


def test_router_health_rules(validate):
    labels = {
        "traefik.http.routers.app-health.rule": "Host(`a`) && PathPrefix(`/h`)",
        "traefik.http.routers.app-health.middlewares": "idp-forward-auth",
        "traefik.http.routers.app-http.rule": "Host(`a`) && Header(`X`, `y`)",
    }
    assert validate.router_problems("app", labels) == [
        "app: router app-health must match one exact Path()",
        "app: router app-health must not use forward auth",
        "app: router app-health skips forward auth, so it needs strip-idp-headers",
        "app: router app-http matches on a header; only Client Paths may",
        "app: a Protected Service needs a probe.idp_url label",
    ]


def test_routers_skipping_forward_auth_strip_idp_headers(validate):
    labels = {
        "traefik.http.routers.app-https.middlewares": "protected",
        "traefik.http.routers.app-health.rule": "Host(`a`) && Path(`/health`)",
        "traefik.http.routers.app-health.middlewares": "crowdsec-bouncer",
        "traefik.http.routers.app-client-opds.rule": "Host(`a`) && PathPrefix(`/opds`)",
        "traefik.http.routers.app-client-opds.middlewares": (
            "crowdsec-bouncer,strip-idp-headers@docker,client-ratelimit"
        ),
        "probe.idp_url": "https://a/",
    }
    assert validate.router_problems("app", labels) == [
        "app: router app-health skips forward auth, so it needs strip-idp-headers",
    ]


def test_strip_idp_headers_covers_forward_auth_headers(validate):
    mw = "traefik.http.middlewares"
    labels = {
        f"{mw}.idp-forward-auth.forwardAuth.authResponseHeaders": "X-a-user,X-a-groups,X-a-jwt",
        f"{mw}.strip-idp-headers.headers.customRequestHeaders.X-a-user": "",
        f"{mw}.strip-idp-headers.headers.customRequestHeaders.X-a-groups": "x",
    }
    assert validate.router_problems("edge", labels) == [
        "edge: strip-idp-headers must empty every idp-forward-auth header; missing X-a-groups, X-a-jwt",
    ]


def test_probe_problems(validate):
    labels = {"traefik.http.routers.app-https.rule": "Host(`a`)", "probe.module": "nope"}
    assert validate.probe_problems("app", labels, {"http_2xx"}) == [
        "app: has an https router but no probe.url label",
        "app: probe.module 'nope' is not a blackbox.yml module",
    ]
    ok = {"traefik.http.routers.app-https.rule": "Host(`a`)", "probe.url": "https://a"}
    assert validate.probe_problems("app", ok, {"http_2xx"}) == []


# --- image VOLUME paths (recorded registry exchanges) -------------------------------------------


@pytest.mark.parametrize(
    "name,volumes",
    [
        ("dockerhub", ["/attach", "/config", "/plugin"]),  # what apprise mounts anonymously on the Host
        ("dockerhub-single", []),
        ("ghcr", ["/config"]),
        ("lscr", ["/config"]),
        ("quay", []),
    ],
)
def test_image_volumes_from_each_registry(validate, name, volumes):
    http = Replay(name)
    assert validate.image_volumes(RECORDINGS[name], validate.Registry(http)) == volumes


def test_image_volumes_reads_the_linux_amd64_image_of_an_index(validate):
    http = Replay("dockerhub")
    validate.image_volumes(RECORDINGS["dockerhub"], validate.Registry(http))
    # apprise's index lists amd64, arm64 and two attestation manifests; amd64 is fc45ea2c...
    manifests = [url.rsplit("/", 1)[-1] for method, url in http.requests if "/manifests/" in url]
    assert manifests[-1].startswith("sha256:fc45ea2c888f990c")


def test_image_volumes_are_cached_by_digest(validate):
    image = RECORDINGS["ghcr"]
    validate.image_volumes(image, validate.Registry(Replay("ghcr")))
    assert validate.image_volumes(image, validate.Registry(validate.REGISTRY_HTTP)) == ["/config"]


def test_image_without_an_amd64_image_is_an_error(validate):
    index = (
        b'{"mediaType":"application/vnd.oci.image.index.v1+json",'
        b'"manifests":[{"digest":"sha256:1","platform":{"os":"linux","architecture":"arm64"}}]}'
    )

    def http(method, url, headers):
        return Response(200, {}, index)

    with pytest.raises(validate.RegistryError, match="has no linux/amd64 image"):
        validate.image_volumes("registry.example/app:1@sha256:abc", validate.Registry(http))


# --- secrets ----------------------------------------------------------------------------------


def test_secrets_ok(validate, capsys):
    validate.check_secrets()
    assert validate.failures == []
    assert "secrets ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    "files,expected",
    [
        ({"tn/app/web/secret.sops.env": "TOKEN=plain\n"}, "file is named *.sops.* but is not SOPS-encrypted"),
        ({"tn/app/web/secret.env": "TOKEN=x\n"}, "plaintext twin of a SOPS file is tracked: tn/app/web/secret.env"),
        ({"tn/app/extra.env": "X=1\n"}, "plaintext .env file is tracked; encrypt it as *.sops.env"),
        (
            {"tn/app/docker-compose.yml": "services: {}\n"},
            "rename to service.yaml (one Service per file) or compose.yaml",
        ),
        ({"tn/lonely/service.yaml": "services: {}\n"}, "doco-cd never discovers this"),
        ({"tn/app/orphan/service.yaml": "services: {}\n"}, "no Stack's include: lists this service.yaml"),
        (
            {"tn/app/compose.yaml": "include: [web/other.yaml]\n", "tn/app/web/other.yaml": "services: {}\n"},
            "app: included file tn/app/web/other.yaml must be tn/app/<service>/service.yaml",
        ),
        ({".doco-cd.yml": "auto_discovery:\n  depth: 0\n"}, "auto_discovery.depth must stay 1 (found 0)"),
        (
            {"tn/app/web/service.yaml": "services:\n  web:\n    env_file: missing.sops.env\n"},
            "referenced file tn/app/web/missing.sops.env is not tracked",
        ),
        (
            {"tn/app/web/service.yaml": "services:\n  web:\n    env_file: plain.cfg\n", "tn/app/web/plain.cfg": "x\n"},
            "sensitive file tn/app/web/plain.cfg must be SOPS-encrypted and named *.sops.*",
        ),
    ],
)
def test_secrets_rejects(validate, repo, files, expected):
    write(repo, files)
    commit(repo)
    validate.check_secrets()
    assert expected in messages(validate)


LEGACY_STACK = {
    "tn/old/docker-compose.yaml": "include:\n  - web/docker-compose.yaml\n",
    "tn/old/web/docker-compose.yaml": "services:\n  web:\n    image: nginx:1\n",
}


def test_a_legacy_stack_keeps_its_docker_compose_files(validate, repo, capsys):
    write(repo, LEGACY_STACK)
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert validate.failures == []
    assert "secrets ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    "files,expected",
    [
        ({"tn/old/stack.env": "X=1\n"}, "plaintext .env file is tracked; encrypt it as *.sops.env"),
        ({"tn/old/web/secret.sops.env": "TOKEN=plain\n"}, "file is named *.sops.* but is not SOPS-encrypted"),
        ({"tn/old/orphan/service.yaml": "services: {}\n"}, "no Stack's include: lists this service.yaml"),
        ({"tn/older/docker-compose.yaml": "services: {}\n"}, "rename to service.yaml"),
    ],
)
def test_a_legacy_stack_is_held_to_every_other_rule(validate, repo, files, expected):
    write(repo, {**LEGACY_STACK, **files})
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert expected in messages(validate)


@pytest.mark.parametrize(
    "files,expected",
    [
        (
            {
                "tn/old/web/docker-compose.yaml": "services:\n  web:\n    image: nginx:1\n    secrets: [pw]\n"
                "secrets:\n  pw:\n    file: password.yaml\n",
                "tn/old/web/password.yaml": "hunter2\n",
            },
            "sensitive file tn/old/web/password.yaml must be SOPS-encrypted and named *.sops.*",
        ),
        (
            {
                "tn/old/docker-compose.yaml": "include:\n  - web/docker-compose.yaml\nservices:\n  db:\n"
                "    image: postgres:16\n    env_file: foo.txt\n",
                "tn/old/foo.txt": "PASSWORD=hunter2\n",
            },
            "sensitive file tn/old/foo.txt must be SOPS-encrypted and named *.sops.*",
        ),
        (
            {
                "tn/old/web/docker-compose.yaml": "services:\n  web:\n    image: nginx:1\n    volumes:\n"
                "      - {type: bind, source: ./key.pem, target: /key.pem, x-sops: true}\n",
                "tn/old/web/key.pem": "plain\n",
            },
            "sensitive file tn/old/web/key.pem must be SOPS-encrypted and named *.sops.*",
        ),
    ],
)
def test_a_legacy_stack_references_sensitive_files_only_encrypted(validate, repo, files, expected):
    write(repo, {**LEGACY_STACK, **files})
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert expected in messages(validate)


@pytest.mark.parametrize("ref,twin", [("web.env", "web.sops.env"), (".env", ".sops.env"), ("web.yml", "web.sops.yml")])
def test_a_legacy_stack_may_reference_the_decrypted_copy_of_a_tracked_sops_file(validate, repo, ref, twin):
    write(
        repo,
        {
            **LEGACY_STACK,
            "tn/old/web/docker-compose.yaml": f"services:\n  web:\n    image: nginx:1\n    env_file: {ref}\n",
            f"tn/old/web/{twin}": "TOKEN=ENC[x]\n" + SOPS_ENV,
        },
    )
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert validate.failures == []


def test_a_legacy_stack_fails_on_an_untracked_file_without_a_sops_twin(validate, repo):
    write(
        repo,
        {
            **LEGACY_STACK,
            "tn/old/web/docker-compose.yaml": "services:\n  web:\n    image: nginx:1\n    env_file: web.env\n",
        },
    )
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert (
        "referenced file tn/old/web/web.env is not tracked and no SOPS-encrypted tn/old/web/web.sops.env is"
        in messages(validate)
    )


@pytest.mark.parametrize(
    "ref,twin,content",
    [
        ("password", "password.sops", "hunter2\n"),  # extensionless: the repo-wide *.sops.* scan misses password.sops
        (".yaml", ".sops.yaml", "creation_rules: []\n"),  # the SOPS config file, which the scan exempts
        ("web.env", "web.sops.env", "TOKEN=hunter2\n"),  # named *.sops.* but not encrypted
    ],
)
def test_a_legacy_stack_refuses_a_twin_that_is_not_a_sops_payload(validate, repo, ref, twin, content):
    write(
        repo,
        {
            **LEGACY_STACK,
            "tn/old/web/docker-compose.yaml": f"services:\n  web:\n    image: nginx:1\n    env_file: '{ref}'\n",
            f"tn/old/web/{twin}": content,
        },
    )
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert f"referenced file tn/old/web/{ref} is not tracked" in messages(validate)


def test_a_legacy_stack_may_reference_an_encrypted_file(validate, repo):
    write(
        repo,
        {
            **LEGACY_STACK,
            "tn/old/web/docker-compose.yaml": "services:\n  web:\n    image: nginx:1\n    env_file: web.sops.env\n",
            "tn/old/web/web.sops.env": "TOKEN=ENC[x]\n" + SOPS_ENV,
        },
    )
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert validate.failures == []


def test_a_legacy_stack_that_has_been_cut_over_fails(validate, repo):
    write(repo, {**LEGACY_STACK, "tn/old/compose.yaml": "include: []\n"})
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_secrets()
    assert "--legacy-stack old: tn/old is a Stack now (it has compose.yaml); drop its --legacy-stack" in (
        validate.failures
    )


@pytest.mark.parametrize("name", ["gone", "leftover"])
def test_a_legacy_stack_without_legacy_compose_files_fails(validate, repo, name):
    write(repo, {"tn/leftover/notes.conf": "x\n"})
    commit(repo)
    configure(validate, repo, legacy_stacks={name})
    validate.check_secrets()
    expected = (
        f"--legacy-stack {name}: no Host has a legacy Stack {name} (docker-compose.yaml files and no compose.yaml)"
    )
    assert expected in messages(validate)


def test_secrets_requires_tracked_stack_env(validate, repo):
    (repo / "tn/app/.sops.env").unlink()
    commit(repo)
    validate.check_secrets()
    assert "app: Stack has no tracked .sops.env" in messages(validate)


def test_secrets_ignores_host_paths_outside_repo(validate, repo):
    write(repo, {"tn/app/web/service.yaml": "services:\n  web:\n    env_file: /mnt/age.key\n"})
    commit(repo)
    validate.check_secrets()
    assert validate.failures == []


# --- env --------------------------------------------------------------------------------------


def test_env_ok_warns_on_unused_template_keys(validate, repo, capsys):
    write(repo, {"tn/app/env.template": "GREETING=\nUNUSED=\n"})
    validate.check_env()
    out = capsys.readouterr().out
    assert validate.failures == []
    assert "WARN: app: env.template keys no Compose file uses: UNUSED" in out
    assert "env checked: app" in out


def test_env_missing_keys(validate, repo):
    write(repo, {"tn/app/env.template": "", "tn/app/.sops.env": SOPS_ONLY})
    validate.check_env()
    assert messages(validate).splitlines() == [
        "app: variables used but missing from env.template: GREETING",
        "app: variables used but missing from .sops.env (deploy would get blanks): GREETING",
    ]


def test_env_dependencies_placeholders(validate, repo):
    write(repo, {"tn/monitoring/prometheus/dependencies.yml": "- ['${DEP_HOST}', '${OTHER}']\n"})
    validate.check_env()
    assert messages(validate) == "dependencies.yml placeholders not passed to dependencies-render: OTHER"


# --- isolation --------------------------------------------------------------------------------


def isolation(validate, repo, monkeypatch, files):
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    write(repo, files)
    commit(repo)
    monkeypatch.setenv("VALIDATE_BASE", base)
    validate.check_isolation()
    return messages(validate)


def test_isolation_ok(validate, repo, monkeypatch):
    assert isolation(validate, repo, monkeypatch, {"tn/app/web/note.txt": "x", "tn/edge/x.yml": "x"}) == ""


def test_isolation_doco_cd_alone(validate, repo, monkeypatch):
    out = isolation(validate, repo, monkeypatch, {"tn/doco-cd/x.yml": "x", "tn/app/x.yml": "x"})
    assert out == "isolation: tn/doco-cd changes must ship in their own PR (also changed: app)"


def test_isolation_baseline_alone(validate, repo, monkeypatch):
    out = isolation(validate, repo, monkeypatch, {"tn/_baseline.yaml": "x", "tn/app/x.yml": "x"})
    assert out == "isolation: tn/_baseline.yaml changes must ship in their own PR (also changed: app)"


def test_isolation_bad_base(validate, monkeypatch):
    monkeypatch.setenv("VALIDATE_BASE", "no-such-ref")
    validate.check_isolation()
    assert messages(validate).startswith("isolation: cannot diff against no-such-ref")


# --- cloudflare -------------------------------------------------------------------------------


def test_cloudflare(validate, repo, capsys):
    validate.check_cloudflare()
    assert validate.failures == [] and "cloudflare ok" in capsys.readouterr().out

    write(repo, {"tn/edge/traefik/service.yaml": f"forwardedHeadersTrustedIPs: {CIDRS}\n"})
    validate.check_cloudflare()
    assert messages(validate) == "expected 3 Cloudflare range lists, found 1"


# --- monitoring -------------------------------------------------------------------------------


def test_monitoring_checks_referenced_files_with_dummy_dependencies_and_secrets(validate, repo, runtime):
    write(
        repo,
        {
            "tn/monitoring/prometheus/service.yaml": """
            services:
              prometheus:
                image: prom/prometheus:v1
            secrets:
              prometheus_password:
                file: ./prometheus_password.sops.txt
        """,
        },
    )
    validate.check_monitoring()
    assert not validate.failures
    config = next(r for r in runtime.runs if r.args[1:3] == ["check", "config"])
    assert "--syntax-only" not in config.args
    assert "/etc/prometheus/alerting-rules.yml" in config.mounts
    assert "${DEP_HOST}" not in config.mounts["/etc/prometheus/sd/dependencies.yml"]
    assert config.mounts["/run/secrets/prometheus_password"].strip()


def test_monitoring_runs_each_tool_with_the_deployed_image(validate, runtime):
    validate.check_monitoring()
    assert validate.failures == []
    config = runtime.runs[0]
    assert (config.image, config.args) == (
        "prom/prometheus:v1",
        ["promtool", "check", "config", "/etc/prometheus/prometheus.yml"],
    )
    assert set(config.mounts) == {
        "/etc/prometheus/prometheus.yml",
        "/etc/prometheus/alerting-rules.yml",
        "/etc/prometheus/sd/dependencies.yml",
    }
    assert [(r.image, r.args[0]) for r in runtime.runs] == [
        ("prom/prometheus:v1", "promtool"),
        ("prom/prometheus:v1", "promtool"),
        ("prom/alertmanager:v2", "amtool"),
        ("grafana/loki:3", "/usr/bin/loki"),
        ("grafana/alloy:1", "/bin/alloy"),
    ]


def test_monitoring_skips_an_encrypted_only_alertmanager_config_and_reports_stale_dashboards(
    validate, repo, capsys, runtime
):
    subprocess.run(["git", "rm", "-q", "tn/monitoring/alertmanager/config.yml"], cwd=repo, check=True)
    write(repo, {"tn/monitoring/alertmanager/config.sops.yml": "sops: {}\n"})
    write(repo, {"tools/dashboards/dashboards.py": "print('overview.json: stale'); raise SystemExit(1)\n"})
    validate.check_monitoring()
    assert "amtool" not in [r.args[0] for r in runtime.runs]
    assert "alertmanager config is encrypted-only" in capsys.readouterr().out
    assert messages(validate) == "overview.json: stale"


def test_monitoring_checks_an_alertmanager_config_outside_the_repo(validate, repo, runtime):
    external = repo.parent / "alertmanager"
    write(external, {"service.yaml": "services:\n  alertmanager:\n    image: prom/alertmanager:v3\n"})
    write(external, {"config.yml": "route: invalid\n"})
    configure(validate, repo, {"alertmanager": str(external)})
    validate.check_monitoring()
    [amtool] = [r for r in runtime.runs if r.args[0] == "amtool"]
    assert (amtool.image, amtool.mounts) == (
        "prom/alertmanager:v3",
        {"/etc/alertmanager/config.yml": "route: invalid\n"},
    )


def test_monitoring_checks_an_untracked_alertmanager_config(validate, repo, runtime):
    subprocess.run(["git", "rm", "-q", "--cached", "tn/monitoring/alertmanager/config.yml"], cwd=repo, check=True)
    validate.check_monitoring()
    assert "amtool" in [r.args[0] for r in runtime.runs]


def test_monitoring_fails_on_an_alertmanager_without_a_config(validate, repo, runtime):
    subprocess.run(["git", "rm", "-q", "tn/monitoring/alertmanager/config.yml"], cwd=repo, check=True)
    commit(repo)
    validate.check_monitoring()
    assert messages(validate) == "tn/monitoring/alertmanager: no config.yml or config.sops.*"


# --- docker-backed checks ---------------------------------------------------------------------


def docker_compose_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


APPRISE = RECORDINGS["dockerhub"]  # VOLUME /attach /config /plugin


WEB = f"""
    services:
      web:
        extends: {{file: ../../_baseline.yaml, service: baseline}}
        image: {APPRISE}
        healthcheck: {{test: ["CMD", "true"]}}
        deploy: {{resources: {{limits: {{memory: 64M}}}}}}
"""


def compose_fixture(repo, service_yaml: str) -> None:
    shutil.copy(FIXTURES / "_baseline.yaml", repo / "tn/_baseline.yaml")
    write(
        repo,
        {
            "tn/monitoring/blackbox-exporter/blackbox.yml": "modules:\n  http_2xx: {}\n",
            "tn/app/web/service.yaml": service_yaml,
        },
    )
    commit(repo)


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_compose_reports_every_rule(validate, repo, capsys, monkeypatch):
    monkeypatch.setattr(validate, "REGISTRY_HTTP", Replay("dockerhub"))
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", {"media_web_config", "media_gone"})
    compose_fixture(
        repo,
        WEB
        + """
        env_file: [{path: secret.sops.env, format: raw}]
        labels:
          startup-order: "2"
          traefik.http.routers.web-https.rule: Host(`a`)
          greeting: ${GREETING}
        tmpfs: [/attach]
        volumes:
          - config:/config
          - legacy:/legacy
          - /scratch
      bare:
        image: nginx:1
    volumes:
      config:
        name: app_config
      legacy:
        name: media_web_config
        external: true
    """,
    )
    validate.check_compose()
    out = messages(validate)
    assert "app: services without a valid startup-order label (0-3, ADR-0003): bare" in out
    assert "app: web: has an https router but no probe.url label" in out
    assert "app: services without json-file logging at max-size 10m, max-file 3: bare" in out
    assert "app: services without restart: unless-stopped: bare" in out
    assert (
        "app: env_file format is unsupported by doco-cd (use the default and single-quote values containing $): web"
        in out
    )
    assert "app: Baseline (ADR 0007): bare: must extend tn/_baseline.yaml" in out
    assert (
        "web: misses Baseline item 'anonymous-volumes' without a baseline.exempt.anonymous-volumes reason "
        "(image VOLUME paths with no mount: /plugin)"
    ) in out
    assert "web: volume at /scratch has no source (an anonymous volume)" in out
    assert "app: volumes (ADR 0010): app: volume app_config must be external: true" in out
    assert "volumes (ADR 0010): legacy name media_gone is no longer declared; drop its --legacy-volume" in out
    assert "media_web_config" not in out
    assert "compose ok: monitoring" not in out
    assert "Baseline: " in capsys.readouterr().out


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_compose_checks_profile_gated_services(validate, repo, monkeypatch):
    monkeypatch.setattr(validate, "REGISTRY_HTTP", Replay("dockerhub"))
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", set())
    compose_fixture(
        repo,
        WEB
        + """
        profiles: [debug]
        labels: {startup-order: "2"}
        tmpfs: [/attach, /config, /plugin]
        volumes: [/scratch]
    """,
    )
    validate.check_compose()
    assert "web: volume at /scratch has no source (an anonymous volume)" in messages(validate)


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_a_stack_that_fails_to_render_still_declares_its_legacy_volumes(validate, repo, monkeypatch):
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", {"media_web_config"})
    compose_fixture(
        repo,
        WEB
        + """
        depends_on: [missing]
    volumes:
      legacy:
        name: media_web_config
        external: true
    """,
    )
    validate.check_compose()
    assert "docker compose config failed" in messages(validate)
    assert "no longer declared" not in messages(validate)


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_compose_fails_clearly_when_the_registry_is_down(validate, repo, monkeypatch):
    def down(method, url, headers):
        return validate.urllib_http(method, "http://127.0.0.1:9/unreachable", headers)

    monkeypatch.setattr(validate, "REGISTRY_HTTP", down)
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", set())
    compose_fixture(
        repo,
        WEB
        + """
        labels: {startup-order: "2", greeting: "${GREETING}"}
    """,
    )
    validate.check_compose()
    assert messages(validate).startswith(
        f"app: web: cannot read the image config of {APPRISE} for its VOLUME paths (anonymous volumes, ADR 0007): "
        "registry request failed: GET http://127.0.0.1:9/unreachable"
    )
    assert "anonymous-volumes" not in messages(validate).split("ADR 0007): ", 1)[1]


def test_registry_requests_retry_rate_limits_and_server_errors(validate):
    replies = [Response(429, {"retry-after": "120"}), Response(503), Response(200, body=b"ok")]
    sent, slept = [], []

    def http(method, url, headers):
        sent.append(url)
        return replies[len(sent) - 1]

    assert validate.patient(http, sleep=slept.append)("GET", "u", {}).body == b"ok"
    assert slept == [30, 8]
    replies[:] = [Response(503)] * 3
    sent.clear()
    assert validate.patient(http, sleep=slept.append)("GET", "u", {}).status == 503
    assert len(sent) == 3


# --- the command line ---------------------------------------------------------------------------


def test_cli_passes_from_inside_the_repository(repo):
    r = run_cli("secrets", "env", "cloudflare", cwd=repo / "tn/app")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "secrets ok" in r.stdout and "cloudflare ok" in r.stdout


def test_cli_rejects_unknown_check():
    r = run_cli("nope")
    assert r.returncode == 1 and "unknown check 'nope'" in r.stderr


def test_cli_reports_an_invalid_hosts_file(repo):
    write(repo, {"hosts.yaml": "hosts: {}\n"})
    r = run_cli("--root", str(repo), "secrets")
    assert r.returncode == 1 and "hosts.yaml" in r.stderr


def test_cli_accepts_a_hosts_file_naming_a_validate_command(repo):
    hosts = (repo / "hosts.yaml").read_text()
    write(repo, {"hosts.yaml": "validate: [homelab-validate, secrets]\n" + hosts})
    r = run_cli("--root", str(repo), "secrets")
    assert r.returncode == 0, r.stdout + r.stderr


def test_github_annotations(validate, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    validate.fail("a\nb", validate.ROOT / "tn/x.yml")
    validate.warn("w")
    assert capsys.readouterr().out == "::error file=tn/x.yml::a%0Ab\n::warning::w\n"


# --- configuration ----------------------------------------------------------------------------


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    return subprocess.run(
        [sys.executable, "-m", "homelab_validate", *args], capture_output=True, text=True, env=env, cwd=cwd
    )


def test_cli_root_and_path_override(repo):
    write(repo, {"elsewhere/traefik.yaml": "forwardedHeadersTrustedIPs: 1.1.1.0/24\n"})
    assert run_cli("--root", str(repo), "cloudflare").returncode == 0
    r = run_cli("--root", str(repo), "--path", "traefik=elsewhere/traefik.yaml", "cloudflare")
    assert r.returncode == 1
    assert "ERROR: expected 3 Cloudflare range lists, found 1" in r.stdout


def test_cli_legacy_stack(repo):
    write(repo, LEGACY_STACK)
    commit(repo)
    assert run_cli("--root", str(repo), "secrets").returncode == 1
    r = run_cli("--root", str(repo), "--legacy-stack", "old", "secrets")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("arg", ["a/b", "..", "Old", ""])
def test_cli_rejects_a_legacy_stack_that_is_not_a_folder_name(arg):
    r = run_cli("--legacy-stack", arg, "secrets")
    assert r.returncode == 2 and "--legacy-stack takes a Stack folder name" in r.stderr


@pytest.mark.parametrize("arg", ["nope=x", "traefik"])
def test_cli_rejects_bad_path_override(arg):
    r = run_cli("--path", arg, "cloudflare")
    assert r.returncode == 2 and "--path takes NAME=PATH" in r.stderr


def test_monitoring_paths_are_configurable(validate, repo, runtime):
    write(
        repo,
        {
            "mon/prom/service.yaml": "services:\n  prometheus:\n    image: prom/prometheus:v9\n",
            "mon/prom/prometheus.yml": "",
            "mon/prom/dependencies.yml": "[]\n",
            "mon/prom/x-rules.yml": "",
        },
    )
    commit(repo)
    configure(validate, repo, {"prometheus": "mon/prom"})
    validate.check_monitoring()
    assert (runtime.runs[0].image, set(runtime.runs[0].mounts)) == (
        "prom/prometheus:v9",
        {
            "/etc/prometheus/prometheus.yml",
            "/etc/prometheus/x-rules.yml",
            "/etc/prometheus/sd/dependencies.yml",
        },
    )


def test_monitoring_checks_dashboards_json_under_root(validate, repo, runtime):
    write(repo, {"tools/dashboards/dashboards.py": "import sys; print(*sys.argv[1:]); raise SystemExit(1)\n"})
    configure(validate, repo, {"dashboards-json": "json"})
    write(repo, {"json/overview.json": '{"uid":"overview","title":"Overview","panels":[]}\n'})
    validate.check_monitoring()
    assert messages(validate) == f"--check --out {repo / 'json'}"


def test_monitoring_reports_stale_declared_containers_under_root(validate, repo, runtime):
    write(
        repo,
        {
            "tools/declared-containers/declared_containers.py": (
                "import sys; print('stale', *sys.argv[1:]); raise SystemExit(1)\n"
            )
        },
    )
    validate.check_monitoring()
    assert messages(validate) == f"stale --check --root {repo} --host truenas"


# --- dependency rendering ---------------------------------------------------------------------

DEPENDENCIES = "tn/monitoring/prometheus/dependencies.yml"
PROM_SERVICE = "tn/monitoring/prometheus/service.yaml"


def test_a_deleted_dependencies_template_fails_while_its_renderer_remains(validate, repo, runtime):
    subprocess.run(["git", "rm", "-q", DEPENDENCIES], cwd=repo, check=True)
    commit(repo)
    expected = f"{DEPENDENCIES} is missing, but dependencies-render or prometheus.yml still uses it"
    validate.check_env()
    assert messages(validate) == expected
    validate.failures.clear()
    validate.check_monitoring()
    assert expected in validate.failures


def test_a_dependencies_template_fails_while_prometheus_reads_its_output(validate, repo, runtime):
    write(
        repo,
        {
            PROM_SERVICE: "services:\n  prometheus:\n    image: prom/prometheus:v1\n",
            "tn/monitoring/prometheus/prometheus.yml": "scrape_configs:\n  - file_sd_configs:\n"
            "      - files: [/etc/prometheus/sd/dependencies.yml]\n",
        },
    )
    subprocess.run(["git", "rm", "-q", DEPENDENCIES], cwd=repo, check=True)
    commit(repo)
    validate.check_monitoring()
    assert f"{DEPENDENCIES} is missing, but dependencies-render or prometheus.yml still uses it" in validate.failures


def test_a_host_without_dependency_rendering_needs_no_template(validate, repo, runtime):
    write(repo, {PROM_SERVICE: "services:\n  prometheus:\n    image: prom/prometheus:v1\n"})
    subprocess.run(["git", "rm", "-q", DEPENDENCIES], cwd=repo, check=True)
    commit(repo)
    validate.check_env()
    validate.check_monitoring()
    assert messages(validate) == ""
    assert "/etc/prometheus/sd/dependencies.yml" not in runtime.runs[0].mounts


def test_a_dependencies_template_needs_its_renderer(validate, repo):
    write(repo, {PROM_SERVICE: "services:\n  prometheus:\n    image: prom/prometheus:v1\n"})
    validate.check_env()
    assert messages(validate) == f"{DEPENDENCIES}: no dependencies-render Service in {PROM_SERVICE} renders it"


# --- explicit inputs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arg,check,expected",
    [
        ("traefik=edge/typo.yaml", "cloudflare", "--path traefik=edge/typo.yaml: not a file"),
        ("traefik=tn", "cloudflare", "--path traefik=tn: not a file"),
        ("prometheus=tn/monitoring/typo", "monitoring", "--path prometheus=tn/monitoring/typo: not a directory"),
    ],
)
def test_cli_fails_on_a_missing_explicit_path(repo, arg, check, expected):
    r = run_cli("--root", str(repo), "--path", arg, check)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"ERROR: {expected}" in r.stdout
    assert "skipped" not in r.stdout


def test_env_fails_on_a_missing_explicit_prometheus(validate, repo):
    configure(validate, repo, {"prometheus": "typo"})
    validate.check_env()
    assert messages(validate) == "--path prometheus=typo: not a directory"


@pytest.mark.parametrize("name", ["alertmanager", "loki", "alloy", "grafana", "rule-tests", "dashboards-json"])
def test_monitoring_fails_on_a_missing_explicit_directory(validate, repo, runtime, name):
    configure(validate, repo, {name: "typo"})
    validate.check_monitoring()
    assert f"--path {name}=typo: not a directory" in validate.failures


@pytest.mark.parametrize("name", ["dashboards", "declared-containers"])
def test_monitoring_fails_on_a_missing_explicit_generator(validate, repo, runtime, name):
    configure(validate, repo, {name: "typo.py"})
    validate.check_monitoring()
    assert f"--path {name}=typo.py: not a file" in validate.failures


def test_monitoring_fails_on_a_missing_explicit_dashboards_json_without_a_generator(validate, repo, runtime):
    validate.configure(repo, {"dashboards-json": "typo"})
    validate.check_monitoring()
    assert "--path dashboards-json=typo: not a directory" in validate.failures


def test_monitoring_fails_on_a_missing_explicit_path_for_a_host_without_prometheus(validate, repo, runtime):
    subprocess.run(["git", "rm", "-rq", "tn/monitoring/prometheus"], cwd=repo, check=True)
    commit(repo)
    validate.configure(repo, {"grafana": "typo"})
    validate.check_monitoring()
    assert messages(validate) == "--path grafana=typo: not a directory"


@pytest.mark.parametrize("service", ["prometheus", "alertmanager", "loki", "alloy", "grafana"])
def test_monitoring_fails_clearly_on_a_component_without_its_service_file(validate, repo, runtime, service):
    subprocess.run(["git", "rm", "-q", f"tn/monitoring/{service}/service.yaml"], cwd=repo, check=True)
    commit(repo)
    validate.check_monitoring()
    assert f"monitoring: tn/monitoring/{service}/service.yaml: missing; it must declare the {service} Service" in (
        validate.failures
    )


def test_monitoring_fails_clearly_on_a_service_file_without_its_image(validate, repo, runtime):
    write(repo, {"tn/monitoring/loki/service.yaml": "services:\n  other:\n    image: x:1\n"})
    commit(repo)
    validate.check_monitoring()
    assert "monitoring: tn/monitoring/loki/service.yaml: no loki Service with an image" in validate.failures


def legacy_monitoring(repo) -> None:
    """tn/monitoring before its cutover: docker-compose.yaml files, no compose.yaml or service.yaml."""
    for f in sorted((repo / "tn/monitoring").rglob("*.yaml")):
        if f.name in ("compose.yaml", "service.yaml"):
            subprocess.run(["git", "rm", "-q", str(f)], cwd=repo, check=True)
    write(repo, {"tn/monitoring/prometheus/docker-compose.yaml": "services:\n  prometheus:\n    image: prom/prom:v1\n"})
    commit(repo)


def test_monitoring_skips_a_legacy_stack(validate, repo, runtime, capsys):
    legacy_monitoring(repo)
    configure(validate, repo, legacy_stacks={"monitoring"})
    validate.check_monitoring()
    validate.check_env()
    assert validate.failures == []
    assert runtime.runs == []
    assert "monitoring: tn/monitoring/prometheus is in --legacy-stack monitoring; skipped" in capsys.readouterr().out


def test_monitoring_fails_clearly_on_a_legacy_stack_not_declared_legacy(validate, repo, runtime):
    legacy_monitoring(repo)
    validate.check_monitoring()
    assert "monitoring: tn/monitoring/prometheus/service.yaml: missing; it must declare the prometheus Service" in (
        validate.failures
    )


def test_an_explicit_path_into_a_legacy_stack_is_still_checked(validate, repo, runtime):
    legacy_monitoring(repo)
    configure(validate, repo, {"prometheus": "tn/monitoring/prometheus"}, legacy_stacks={"monitoring"})
    validate.check_monitoring()
    assert "monitoring: tn/monitoring/prometheus/service.yaml: missing; it must declare the prometheus Service" in (
        validate.failures
    )


def test_a_dependencies_template_without_its_service_file_fails(validate, repo):
    subprocess.run(["git", "rm", "-q", PROM_SERVICE], cwd=repo, check=True)
    commit(repo)
    validate.check_env()
    assert messages(validate) == f"{DEPENDENCIES}: no dependencies-render Service in {PROM_SERVICE} renders it"


def test_probe_modules_fails_on_a_missing_explicit_blackbox(validate, repo):
    configure(validate, repo, {"blackbox": "typo.yml"})
    assert validate.probe_modules(validate.HOSTS[0]) == set()
    assert messages(validate) == "--path blackbox=typo.yml: not a file"


def test_configure_rejects_unknown_path(validate, repo):
    with pytest.raises(KeyError):
        validate.configure(repo, {"nope": "x"})


def test_template_volumes_are_named_for_stack_app(validate):
    # Templates are not Stacks yet, so "app" is not among the Stack names.
    assert validate.volume_problems("app", {"app_config": {"name": "app_config", "external": True}}, {"edge"}) == []


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_compose_leaves_a_legacy_stack_to_its_cutover(validate, repo, capsys, monkeypatch):
    """Deliberate: a --legacy-stack gets no Stack-level checks (render, Compose rules, Baseline, env) and its
    Services are not counted in the Baseline report, until its cutover renames it to compose.yaml."""
    monkeypatch.setattr(validate, "REGISTRY_HTTP", Replay("dockerhub"))
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", set())
    compose_fixture(repo, WEB + '        labels: {startup-order: "2"}\n        tmpfs: [/attach, /config, /plugin]\n')
    configure(validate, repo)
    validate.check_compose()
    before = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Baseline: ")]
    assert before

    write(repo, {**LEGACY_STACK, "tn/old/web/docker-compose.yaml": "services:\n  web:\n    image: ${UNSET}\n"})
    commit(repo)
    configure(validate, repo, legacy_stacks={"old"})
    validate.check_compose()
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("Baseline: ")] == before
    assert "old" not in out + messages(validate)
    validate.check_env()
    assert "old" not in capsys.readouterr().out + messages(validate)
