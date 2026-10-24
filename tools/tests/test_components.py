import pytest

from tools.components import (
    affected_components,
    all_components,
    all_repo_tools,
    main,
    parse_release_tag,
    release_target,
)

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


def test_a_component_whose_readme_was_deleted_still_gets_tested(tmp_path):
    (tmp_path / "with-readme" / "src").mkdir(parents=True)
    (tmp_path / "with-readme" / "README.md").write_text("# with-readme\n")
    (tmp_path / "without-readme" / "src").mkdir(parents=True)
    components = all_components(tmp_path)
    assert components == ["with-readme", "without-readme"]
    assert affected_components(["components/without-readme/README.md"], components) == ["without-readme"]


def test_a_repo_tools_only_change_tests_no_component():
    changed = ["repo-tools/hosts/src/homelab_hosts/__init__.py", "repo-tools/hosts/README.md"]
    assert affected_components(changed, COMPONENTS) == []


def test_every_repo_tool_folder_is_listed(tmp_path):
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hops").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "README.md").write_text("")
    assert all_repo_tools(tmp_path) == ["hops", "hosts"]
    assert all_repo_tools(tmp_path / "missing") == []


@pytest.fixture
def release_dirs(tmp_path):
    components, repo_tools = tmp_path / "components", tmp_path / "repo-tools"
    (components / "arr-queue-exporter").mkdir(parents=True)
    (components / "arr-queue-exporter" / "Dockerfile").write_text("FROM scratch\n")
    (components / "startup-order").mkdir()
    (repo_tools / "hosts").mkdir(parents=True)
    return {"components_dir": components, "repo_tools_dir": repo_tools}


def test_a_component_tag_releases_its_image_or_its_files(release_dirs):
    assert release_target("arr-queue-exporter-v1.2.3", **release_dirs) == {
        "kind": "component",
        "name": "arr-queue-exporter",
        "version": "1.2.3",
        "image": "true",
    }
    assert release_target("startup-order-v0.1.0", **release_dirs)["image"] == "false"


def test_a_repo_tool_tag_releases_without_an_image(release_dirs):
    assert release_target("hosts-v1.0.0", **release_dirs) == {
        "kind": "repo-tool",
        "name": "hosts",
        "version": "1.0.0",
        "image": "false",
    }


def test_a_repo_ci_tag_releases_the_reusable_workflows_and_renovate_preset(release_dirs):
    assert release_target("repo-ci-v1.0.0", **release_dirs) == {
        "kind": "repo-ci",
        "name": "repo-ci",
        "version": "1.0.0",
        "image": "false",
    }


@pytest.mark.parametrize("folder", ["components_dir", "repo_tools_dir"])
def test_a_component_or_repo_tool_named_repo_ci_is_rejected(release_dirs, folder):
    (release_dirs[folder] / "repo-ci").mkdir()
    with pytest.raises(ValueError, match="repo-ci"):
        release_target("hosts-v1.0.0", **release_dirs)


def test_a_tag_naming_nothing_is_rejected(release_dirs):
    with pytest.raises(ValueError, match="names no Component"):
        release_target("nothing-v1.0.0", **release_dirs)


def test_a_tag_naming_both_a_component_and_a_repo_tool_is_rejected(release_dirs):
    (release_dirs["repo_tools_dir"] / "startup-order").mkdir()
    with pytest.raises(ValueError, match="both"):
        release_target("startup-order-v1.0.0", **release_dirs)


def test_ci_fails_when_a_repo_tool_shares_a_component_name(release_dirs, monkeypatch, capsys):
    monkeypatch.setattr("tools.components.COMPONENTS_DIR", release_dirs["components_dir"])
    monkeypatch.setattr("tools.components.REPO_TOOLS_DIR", release_dirs["repo_tools_dir"])
    assert main(["repo-tools"]) == 0
    assert capsys.readouterr().out.strip() == '["hosts"]'

    (release_dirs["repo_tools_dir"] / "startup-order").mkdir()
    assert main(["repo-tools"]) == 1
    assert "startup-order" in capsys.readouterr().err
