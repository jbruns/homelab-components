import pytest

from tools.components import affected_components, parse_release_tag

COMPONENTS = ["_template", "arr-queue-exporter", "sabnzbd-status-exporter"]


def test_a_change_inside_a_component_tests_only_that_component():
    changed = ["components/arr-queue-exporter/src/arr_queue_exporter/__main__.py", "README.md"]
    assert affected_components(changed, COMPONENTS) == ["arr-queue-exporter"]


def test_a_change_to_the_shared_harness_tests_every_component():
    assert affected_components(["tools/contract.py"], COMPONENTS) == COMPONENTS
    assert affected_components(["requirements-dev.txt"], COMPONENTS) == COMPONENTS


def test_a_docs_only_change_tests_no_component():
    assert affected_components(["README.md", "CONTRIBUTING.md", "renovate.json"], COMPONENTS) == []


def test_a_release_tag_names_the_component_and_its_semver():
    assert parse_release_tag("arr-queue-exporter-v1.2.3") == ("arr-queue-exporter", "1.2.3")


@pytest.mark.parametrize("tag", ["_template-v1.0.0", "arr-queue-exporter-v1.2", "arr-queue-exporter-1.2.3"])
def test_a_malformed_or_unreleasable_tag_is_rejected(tag):
    with pytest.raises(ValueError):
        parse_release_tag(tag)
