"""What generalising the tool for any repo adds: each repo's own pin, skill set and install targets."""

import pytest


def test_two_repos_pin_different_sources_and_skill_sets(make_source, make_project, monkeypatch, run):
    home = make_source("home-skills")
    for skill in ("engineering/tdd", "engineering/grill", "misc/wizard"):
        home.add_skill(skill)
    home_revision = home.publish("engineering/tdd", "engineering/grill")
    other = make_source("other-skills")
    other.add_skill("ops/runbook", {"steps.md": "1. look\n"})
    other.add_skill("ops/triage")
    other.publish("ops/runbook", "ops/triage")
    other_revision = other.publish("ops/runbook")

    first = make_project("home-ops", home)
    first.write_lock(home_revision, "exclude grill", "include misc/wizard")
    second = make_project("ops", other)
    second.write_lock(other_revision, "include ops/triage", "target .claude/skills", ref="main")

    monkeypatch.chdir(first.path)
    assert run()[0] == 0
    monkeypatch.chdir(second.path)
    assert run()[0] == 0

    assert first.installed() == ["tdd", "wizard"]
    assert second.installed(".claude/skills") == ["runbook", "triage"]
    assert not (second.path / ".agents").exists()
    code, out, _ = run("check")
    assert (code, out) == (0, f"Agent skills match main ({other_revision}).\n")
    monkeypatch.chdir(first.path)
    assert run("check")[0] == 0


def test_check_and_setup_find_the_repo_root_from_a_subdirectory(project, source, run, monkeypatch):
    source.add_skill("tdd")
    project.write_lock(source.publish("tdd"))
    (project.path / "docs/deep").mkdir(parents=True)
    monkeypatch.chdir(project.path / "docs/deep")

    assert run()[0] == 0
    assert run("check")[0] == 0
    assert project.installed() == ["tdd"]


def test_outside_a_git_repository_is_an_error(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)

    code, _, err = run("check")
    assert code == 1
    assert "inside a git repository" in err


@pytest.fixture
def two_targets(project, source, run):
    source.add_skill("tdd", {"notes.md": "red, green\n"})
    project.write_lock(source.publish("tdd"), "target .agents/skills", "target .claude/skills")
    assert run()[0] == 0
    return project


def test_every_target_gets_the_skills_and_is_checked(two_targets, run):
    assert two_targets.installed() == ["tdd"]
    assert two_targets.installed(".claude/skills") == ["tdd"]
    assert run("check")[0] == 0

    (two_targets.path / ".claude/skills/tdd/notes.md").write_text("edited\n")
    code, _, err = run("check")
    assert code == 1
    assert "installed skill 'tdd' in .claude/skills has local changes" in err


def test_a_target_added_to_the_lock_is_not_installed_until_setup(two_targets, source, run):
    lock = two_targets.lock.read_text()
    two_targets.lock.write_text(lock + "target .github/skills\n")

    code, _, err = run("check")
    assert code == 1
    assert "agent skills are not installed in .github/skills" in err
    assert run()[0] == 0
    assert run("check")[0] == 0


def test_an_unmanaged_skill_in_any_target_blocks_setup_before_anything_changes(two_targets, source, run):
    (two_targets.path / ".claude/skills/.agent-skills-managed").unlink()
    source.add_skill("grill")
    revision = source.publish("tdd", "grill")

    code, _, err = run("pin", revision)
    assert code == 1
    assert ".claude/skills/tdd exists but is not managed by this helper" in err
    assert two_targets.installed() == ["tdd"]
    assert f"revision {revision}" not in two_targets.lock.read_text()


def test_clean_empties_every_target(two_targets, run):
    assert run("clean")[0] == 0
    assert two_targets.installed() == []
    assert two_targets.installed(".claude/skills") == []


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("../outside", "target must be a relative path inside the repo"),
        ("/abs/skills", "target must be a relative path inside the repo"),
        (".git/skills", "target must be a relative path inside the repo"),
        ("a/./b", "target must be a relative path inside the repo"),
    ],
)
def test_a_target_outside_the_repo_is_rejected(project, source, run, target, message):
    project.write_lock("0" * 40, f"target {target}")

    code, _, err = run("check")
    assert code == 1
    assert message in err


def test_nested_targets_are_rejected(project, run):
    project.write_lock("0" * 40, "target .agents", "target .agents/skills")

    code, _, err = run("check")
    assert code == 1
    assert "target '.agents/skills' is inside target '.agents'" in err
