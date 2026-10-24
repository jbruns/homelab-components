"""Ported from jbruns/home-ops tools/agent-skills/test-setup-agent-skills: one test per scenario."""

import os

import pytest


@pytest.fixture
def revision(source):
    """The default upstream: three published skills (one with a supporting file) and two unpublished ones."""
    source.add_skill("example", {"reference.md": "supporting instructions\n"})
    for skill in ("engineering/nested", "engineering/other", "misc/nested", "misc/extra"):
        source.add_skill(skill)
    return source.publish("example", "engineering/nested", "engineering/other")


@pytest.fixture
def installed(project, revision, run):
    project.write_lock(revision)
    assert run()[0] == 0
    return project.path / ".agents/skills"


def test_the_published_manifest_is_the_default_set_and_setup_is_repeatable(project, revision, run):
    project.write_lock(revision)

    code, out, _ = run()
    assert code == 0
    assert out.startswith(f"Installed 3 agent skills from {revision} ({revision})")
    assert run("check")[0] == 0
    assert run()[0] == 0
    code, out, _ = run("check")
    assert code == 0
    assert out == f"Agent skills match {revision} ({revision}).\n"
    assert project.installed() == ["example", "nested", "other"]
    assert (project.path / ".agents/skills/example/reference.md").read_text() == "supporting instructions\n"


def test_check_fails_when_nothing_is_installed(project, revision, run):
    project.write_lock(revision)

    code, _, err = run("check")
    assert code == 1
    assert "agent skills are not installed in .agents/skills; run setup-agent-skills" in err


def test_check_detects_a_modified_supporting_file_and_setup_repairs_it(installed, run):
    (installed / "example/reference.md").write_text("locally modified\n")

    code, _, err = run("check")
    assert code == 1
    assert "installed skill 'example' in .agents/skills has local changes" in err
    assert run()[0] == 0
    assert run("check")[0] == 0


def test_check_detects_a_missing_supporting_file(installed, run):
    (installed / "example/reference.md").unlink()

    assert run("check")[0] == 1
    assert run()[0] == 0
    assert run("check")[0] == 0


def test_check_detects_a_missing_skill(installed, run):
    (installed / "nested/SKILL.md").unlink()

    code, _, err = run("check")
    assert code == 1
    assert "missing installed skill 'nested'" in err


def test_an_alternate_lock_is_selected_by_environment(project, installed, run, tmp_path, monkeypatch):
    alternate = tmp_path / "alternate.lock"
    project.lock.rename(alternate)

    assert run("check")[0] == 1
    monkeypatch.setenv("AGENT_SKILLS_LOCK", str(alternate))
    assert run("check")[0] == 0


def test_excludes_by_name_or_path_and_includes_adjust_the_set(project, revision, installed, run):
    project.write_lock(revision, "exclude example", "exclude engineering/other", "include misc/extra")

    code, _, err = run("check")
    assert code == 1
    assert "agent skills in .agents/skills are stale" in err
    assert run()[0] == 0
    assert run("check")[0] == 0
    assert project.installed() == ["extra", "nested"]


def test_reordering_lock_entries_is_not_a_change(project, revision, run):
    project.write_lock(revision, "exclude example", "exclude engineering/other", "include misc/extra")
    assert run()[0] == 0

    project.write_lock(revision, "include misc/extra", "exclude engineering/other", "exclude example")
    assert run("check")[0] == 0


def test_an_exclude_matching_nothing_only_warns(project, revision, run):
    project.write_lock(revision, "exclude gone")

    code, _, err = run()
    assert code == 0
    assert f"warning: exclude 'gone' matches no skill in the source manifest at {revision}" in err


def test_an_include_that_does_not_exist_fails(project, revision, run):
    project.write_lock(revision, "include misc/missing")

    code, _, err = run()
    assert code == 1
    assert "skill 'misc/missing' does not exist" in err


def test_two_skills_installing_under_one_name_are_rejected(project, revision, run):
    project.write_lock(revision, "include misc/nested")

    code, _, err = run()
    assert code == 1
    assert "would both install as 'nested'" in err


def test_excluding_one_lets_another_category_take_the_name(project, revision, run):
    project.write_lock(revision, "exclude engineering/nested", "include misc/nested")

    assert run()[0] == 0
    assert run("check")[0] == 0
    state = (project.path / ".agents/skills/.agent-skills-managed").read_text()
    assert "\nskill misc/nested " in state


def test_a_skill_removed_upstream_disappears_on_repin(project, source, installed, run):
    new_revision = source.publish("example", "engineering/nested")

    code, out, _ = run("pin", new_revision)
    assert code == 0
    assert f"Pinned {new_revision} at {new_revision} in .agent-skills.lock." in out
    assert run("check")[0] == 0
    assert project.installed() == ["example", "nested"]
    assert f"revision {new_revision}\n" in project.lock.read_text()


def test_pin_keeps_the_rest_of_the_lock(project, source, revision, run):
    project.write_lock(revision, "# keep this comment", "exclude other")
    new_revision = source.publish("example", "engineering/nested", "engineering/other")

    assert run("pin", new_revision)[0] == 0
    assert project.lock.read_text() == (
        f"source {source.path}\nref {new_revision}\nrevision {new_revision}\n# keep this comment\nexclude other\n"
    )


def test_a_moved_ref_is_refused_and_leaves_the_install_alone(project, source, revision, run):
    released = source.commit("release")
    source.git("tag", "release", released)
    project.write_lock(released, ref="release")
    assert run()[0] == 0
    source.git("tag", "--force", "release", revision)

    code, _, err = run()
    assert code == 1
    assert f"release resolved to {revision}, expected {released}" in err
    assert run("check")[0] == 0


def test_a_failed_pin_preserves_the_lock_and_the_install(project, installed, run):
    before = project.lock.read_text()

    code, _, err = run("pin", "does-not-exist")
    assert code == 1
    assert "error:" in err
    assert project.lock.read_text() == before
    assert run("check")[0] == 0


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (["include ../escape"], "invalid skill path: ../escape"),
        (["include misc/extra", "exclude extra"], "'misc/extra' is both included and excluded"),
        (["skill example"], "'skill' entries were replaced by the source manifest"),
        (["include a b"], "invalid entry at"),
        (["colour blue"], "unknown key 'colour'"),
        (["include misc/extra", "include misc/extra"], "duplicate include 'misc/extra'"),
    ],
)
def test_invalid_locks_are_rejected(project, revision, run, entries, message):
    project.write_lock(revision, *entries)

    code, _, err = run("check")
    assert code == 1
    assert message in err


def test_a_lock_needs_a_full_revision(project, run):
    project.lock.write_text("source x\nref v1\nrevision abc123\n")

    code, _, err = run("check")
    assert code == 1
    assert "revision must be a full lowercase commit SHA" in err


def test_a_missing_lock_is_reported(project, run):
    code, _, err = run("check")
    assert code == 1
    assert "missing lock file" in err


def test_a_source_without_a_manifest_needs_includes(project, source, revision, run):
    source.git("rm", "--quiet", "-r", ".claude-plugin")
    bare = source.commit("drop manifest")
    project.write_lock(bare)
    code, _, err = run()
    assert code == 1
    assert "no skills to install" in err

    project.write_lock(bare, "include misc/extra")
    assert run()[0] == 0
    assert run("check")[0] == 0
    assert project.installed() == ["extra"]


def test_clean_removes_only_managed_skills(project, installed, run):
    (installed / "custom").mkdir()
    (installed / "custom/SKILL.md").write_text("custom skill\n")

    code, out, _ = run("clean")
    assert code == 0
    assert out == "Removed repository-managed agent skills.\n"
    assert project.installed() == ["custom"]
    assert not (installed / ".agent-skills-managed").exists()
    assert run("clean")[1] == "No managed agent skills are installed.\n"


def test_setup_never_overwrites_an_unmanaged_skill(project, revision, run):
    skills = project.path / ".agents/skills"
    (skills / "example").mkdir(parents=True)
    (skills / "example/SKILL.md").write_text("unmanaged example\n")
    project.write_lock(revision)

    code, _, err = run()
    assert code == 1
    assert "exists but is not managed by this helper" in err
    assert (skills / "example/SKILL.md").read_text() == "unmanaged example\n"
    assert not (skills / "nested").exists()


def test_setup_never_overwrites_an_unmanaged_dangling_symlink(project, revision, run, tmp_path):
    skills = project.path / ".agents/skills"
    skills.mkdir(parents=True)
    os.symlink(tmp_path / "missing", skills / "example")
    project.write_lock(revision)

    assert run()[0] == 1
    assert (skills / "example").is_symlink()


@pytest.mark.parametrize(
    "manifest",
    [
        "{invalid json",
        "[]",
        '{"skills": "skills/example"}',
        '{"skills": [42]}',
        '{"skills": ["../example"]}',
        '{"skills": ["skills/"]}',
        '{"skills": ["skills/../example"]}',
    ],
)
def test_a_malformed_manifest_fails_even_with_includes(project, source, revision, run, manifest):
    broken = source.write_manifest(manifest)
    project.write_lock(broken, "include misc/extra")

    code, _, err = run()
    assert code == 1
    assert "source manifest" in err
    assert not (project.path / ".agents/skills/extra").exists()


@pytest.mark.parametrize("args", [["check", "extra"], ["pin"], ["clean", "x"], ["bogus"], ["setup", "x"]])
def test_bad_usage_exits_2(project, run, args):
    code, _, err = run(*args)
    assert code == 2
    assert "Usage:" in err


def test_help_prints_usage(run):
    code, out, _ = run("--help")
    assert code == 0
    assert "Usage: setup-agent-skills" in out
