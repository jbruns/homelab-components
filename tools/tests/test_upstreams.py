import pytest

from tools.upstreams import contract_image, load_upstreams, newest_stable


def test_canary_picks_newest_stable_release_with_the_pinned_tag_shape():
    tags = ["latest", "v1.10.3", "v1.11.0", "v1.12.0", "v1.12", "v1", "v1.11.0-arm64", "v1.13.0-rc.1"]
    assert newest_stable("v1.11.0", tags) == "v1.12.0"


def test_canary_compares_versions_numerically_across_any_number_of_parts():
    tags = ["4.0.9.2244", "4.0.15.2941", "4.0.14.2939", "rolling"]
    assert newest_stable("4.0.14.2939", tags) == "4.0.15.2941"


def test_canary_keeps_the_pinned_variant_suffix():
    tags = ["16.4", "17.0", "16.4-alpine", "17.0-alpine", "18.0-rc1-alpine"]
    assert newest_stable("16.4-alpine", tags) == "17.0-alpine"


def test_canary_has_no_candidate_when_the_pin_is_not_a_release_tag():
    assert newest_stable("rolling", ["rolling", "1.0.0"]) is None


DIGEST = "sha256:" + "a" * 64


def write_upstreams(component, text):
    component.mkdir(exist_ok=True)
    (component / "upstreams.yaml").write_text(text)
    return component


def test_contract_tests_use_the_pinned_image(tmp_path):
    component = write_upstreams(
        tmp_path / "demo",
        f"""
upstreams:
  whoami:
    image: traefik/whoami:v1.11.0@{DIGEST}
    port: 80
""",
    )
    (whoami,) = load_upstreams(component)
    assert whoami.name == "whoami"
    assert whoami.image == f"traefik/whoami:v1.11.0@{DIGEST}"
    assert (whoami.repository, whoami.tag) == ("traefik/whoami", "v1.11.0")
    assert contract_image(whoami, environ={}) == f"traefik/whoami:v1.11.0@{DIGEST}"


def test_a_canary_overrides_only_the_upstream_it_names(tmp_path):
    component = write_upstreams(
        tmp_path / "demo",
        f"""
upstreams:
  whoami:
    image: traefik/whoami:v1.11.0@{DIGEST}
    port: 80
  other:
    image: ghcr.io/example/other:2.0.0@{DIGEST}
    port: 8080
""",
    )
    whoami, other = load_upstreams(component)
    environ = {"CONTRACT_UPSTREAM": "whoami", "CONTRACT_IMAGE": "traefik/whoami:v1.12.0"}
    assert contract_image(whoami, environ) == "traefik/whoami:v1.12.0"
    assert contract_image(other, environ) == f"ghcr.io/example/other:2.0.0@{DIGEST}"


@pytest.mark.parametrize("image", ["traefik/whoami:v1.11.0", f"traefik/whoami@{DIGEST}", "traefik/whoami"])
def test_an_upstream_must_pin_both_tag_and_digest(tmp_path, image):
    component = write_upstreams(tmp_path / "demo", f"upstreams:\n  whoami:\n    image: {image}\n    port: 80\n")
    with pytest.raises(ValueError, match="whoami.*tag@sha256"):
        load_upstreams(component)


def test_an_upstream_without_a_port_is_a_cli(tmp_path):
    component = write_upstreams(
        tmp_path / "demo",
        f"upstreams:\n  restic:\n    image: restic/restic:0.19.0@{DIGEST}\n    env:\n      RESTIC_PASSWORD: x\n",
    )
    (restic,) = load_upstreams(component)
    assert restic.port is None
    assert restic.env == {"RESTIC_PASSWORD": "x"}
