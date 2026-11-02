import os
from pathlib import Path

import pytest

from ember import skill


def make_skill(folder: Path, name: str = "ember") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A skill.\n---\n\n# Skill\n"
    )
    return folder


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


@pytest.fixture
def env(home: Path) -> dict[str, str]:
    return {"HOME": str(home)}


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return make_skill(tmp_path / "checkout" / "skill").resolve()


def link_of(home: Path) -> Path:
    return home / ".claude" / "skills" / "ember"


# The link path


def test_link_path_defaults_to_home(env: dict[str, str], home: Path) -> None:
    assert skill.link_path(env) == link_of(home)


def test_link_path_uses_claude_config_dir(env: dict[str, str], tmp_path: Path) -> None:
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "config")
    assert skill.link_path(env) == tmp_path / "config" / "skills" / "ember"


def test_link_path_ignores_empty_claude_config_dir(
    env: dict[str, str], home: Path
) -> None:
    env["CLAUDE_CONFIG_DIR"] = ""
    assert skill.link_path(env) == link_of(home)


# The source


def test_is_ember_skill(tmp_path: Path) -> None:
    assert skill.is_ember_skill(make_skill(tmp_path / "a"))
    assert skill.is_ember_skill(make_skill(tmp_path / "b", '"ember"'))
    assert not skill.is_ember_skill(make_skill(tmp_path / "c", "other"))
    assert not skill.is_ember_skill(tmp_path / "missing")
    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "SKILL.md").write_text("name: ember\n")
    assert not skill.is_ember_skill(folder)
    (folder / "SKILL.md").write_text("---\ndescription: x\n---\nname: ember\n")
    assert not skill.is_ember_skill(folder)


def test_find_source_takes_the_first_ember_skill(tmp_path: Path) -> None:
    other = make_skill(tmp_path / "other", "other")
    first = make_skill(tmp_path / "first")
    second = make_skill(tmp_path / "second")
    assert skill.find_source([tmp_path / "none", other, first, second]) == first
    assert skill.find_source([other]) is None


def test_checkout_skill_is_beside_src() -> None:
    assert skill.checkout_skill() == Path(__file__).resolve().parents[1] / "skill"
    assert skill.source_candidates()[1] == skill.SYSTEM_SKILL


# Status


def test_status_missing(env: dict[str, str], home: Path, source: Path) -> None:
    current = skill.status(env, [source])
    assert current == skill.Status(link_of(home), "missing", None, source, None)
    assert skill.status_lines(current) == [
        f"link: {link_of(home)}",
        "state: missing",
        "target: null",
        f"source: {source}",
    ]


def test_status_installed(env: dict[str, str], home: Path, source: Path) -> None:
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(source)
    current = skill.status(env, [source])
    assert (current.state, current.target) == ("installed", source)


def test_status_other_link(
    env: dict[str, str], home: Path, source: Path, tmp_path: Path
) -> None:
    other = make_skill(tmp_path / "other").resolve()
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(other)
    current = skill.status(env, [source])
    assert (current.state, current.target) == ("other link", other)


def test_status_broken_link(
    env: dict[str, str], home: Path, source: Path, tmp_path: Path
) -> None:
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "removed-worktree" / "skill")
    current = skill.status(env, [source])
    assert current.state == "broken link"
    assert current.target == (tmp_path / "removed-worktree" / "skill").resolve()


def test_status_folder_and_file(env: dict[str, str], home: Path, source: Path) -> None:
    link = link_of(home)
    link.mkdir(parents=True)
    assert skill.status(env, [source]).state == "folder"
    link.rmdir()
    link.write_text("x")
    current = skill.status(env, [source])
    assert (current.state, current.target) == ("file", None)


def test_status_without_source(env: dict[str, str], tmp_path: Path) -> None:
    candidates = [tmp_path / "a", tmp_path / "b"]
    current = skill.status(env, candidates)
    reason = f"the skill is missing from {tmp_path / 'a'}, {tmp_path / 'b'}"
    assert current.source is None
    assert current.source_reason == reason
    assert current.to_json() == {
        "link": str(current.link),
        "state": "missing",
        "target": None,
        "source": None,
        "source_reason": reason,
    }
    assert skill.status_lines(current)[-1] == f"source: null ({reason})"


# Install


def test_install_creates_parents_and_link(
    env: dict[str, str], home: Path, source: Path
) -> None:
    result = skill.install(env, [source])
    link = link_of(home)
    assert result == skill.Result(link, "installed", source)
    assert result.line() == f"installed: {link} -> {source}"
    assert os.readlink(link) == str(source)
    for folder in (home / ".claude", home / ".claude" / "skills"):
        assert folder.stat().st_mode & 0o777 == 0o755
    assert [path.name for path in link.parent.iterdir()] == ["ember"]


def test_install_in_claude_config_dir(
    env: dict[str, str], tmp_path: Path, source: Path
) -> None:
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "config")
    skill.install(env, [source])
    assert (tmp_path / "config" / "skills" / "ember").resolve() == source


def test_install_with_existing_skills_folder(
    env: dict[str, str], home: Path, source: Path
) -> None:
    (home / ".claude" / "skills").mkdir(parents=True, mode=0o700)
    skill.install(env, [source])
    assert link_of(home).resolve() == source
    assert (home / ".claude" / "skills").stat().st_mode & 0o777 == 0o700


def test_install_when_installed_changes_nothing(
    env: dict[str, str], home: Path, source: Path
) -> None:
    skill.install(env, [source])
    before = link_of(home).lstat()
    result = skill.install(env, [source])
    assert result.action == "already installed"
    assert result.line() == f"already installed: {link_of(home)} -> {source}"
    assert link_of(home).lstat().st_ino == before.st_ino


@pytest.mark.parametrize("force", [False, True])
def test_install_replaces_broken_link(
    env: dict[str, str], home: Path, source: Path, tmp_path: Path, force: bool
) -> None:
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "gone")
    assert skill.install(env, [source], force=force).action == "installed"
    assert link.resolve() == source


def test_install_refuses_other_link_without_force(
    env: dict[str, str], home: Path, source: Path, tmp_path: Path
) -> None:
    other = make_skill(tmp_path / "other")
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(other)
    with pytest.raises(skill.SkillError) as error:
        skill.install(env, [source])
    assert error.value.code == 75
    assert error.value.line == "next: emb skill install --force"
    assert link.resolve() == other.resolve()
    assert skill.install(env, [source], force=True).action == "installed"
    assert link.resolve() == source
    assert (other / "SKILL.md").exists()


@pytest.mark.parametrize("kind", ["folder", "file"])
@pytest.mark.parametrize("force", [False, True])
def test_install_never_removes_folder_or_file(
    env: dict[str, str], home: Path, source: Path, kind: str, force: bool
) -> None:
    link = link_of(home)
    if kind == "folder":
        link.mkdir(parents=True)
        (link / "notes.md").write_text("mine")
    else:
        link.parent.mkdir(parents=True)
        link.write_text("mine")
    with pytest.raises(skill.SkillError) as error:
        skill.install(env, [source], force=force)
    assert error.value.code == 75
    assert error.value.line == f"owner: move or remove {link}, then emb skill install"
    assert f"is a {kind}" in str(error.value)
    if kind == "folder":
        assert (link / "notes.md").read_text() == "mine"
    else:
        assert link.read_text() == "mine"


def test_install_without_source_exits_66(env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(skill.SkillError) as error:
        skill.install(env, [tmp_path / "a"])
    assert error.value.code == 66
    assert str(error.value) == "no skill source found"
    assert error.value.line == (
        f"owner: reinstall Ember; the skill is missing from {tmp_path / 'a'}"
    )
    assert not (Path(env["HOME"]) / ".claude").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any folder")
def test_install_into_unwritable_folder_exits_73(
    env: dict[str, str], home: Path, source: Path
) -> None:
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    skills.chmod(0o555)
    try:
        with pytest.raises(skill.SkillError) as error:
            skill.install(env, [source])
    finally:
        skills.chmod(0o755)
    assert error.value.code == 73
    assert error.value.line.startswith(f"owner: check {skills}: ")
    assert list(skills.iterdir()) == []


def test_install_under_a_file_exits_73(
    env: dict[str, str], home: Path, source: Path
) -> None:
    (home / ".claude").write_text("not a folder")
    with pytest.raises(skill.SkillError) as error:
        skill.install(env, [source])
    assert error.value.code == 73
    assert (home / ".claude").read_text() == "not a folder"


# Uninstall


def test_uninstall_installed(env: dict[str, str], home: Path, source: Path) -> None:
    skill.install(env, [source])
    result = skill.uninstall(env, [source])
    assert result == skill.Result(link_of(home), "removed", source)
    assert result.line() == f"removed: {link_of(home)}"
    assert not link_of(home).is_symlink()
    assert (source / "SKILL.md").exists()


def test_uninstall_missing(env: dict[str, str], home: Path, source: Path) -> None:
    result = skill.uninstall(env, [source])
    assert result == skill.Result(link_of(home), "not installed", None)
    assert result.line() == f"not installed: {link_of(home)}"
    assert result.to_json() == {
        "link": str(link_of(home)),
        "action": "not installed",
        "target": None,
    }


def test_uninstall_broken_link_without_source(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "gone")
    result = skill.uninstall(env, [tmp_path / "no-source"])
    assert (result.action, result.target) == ("removed", (tmp_path / "gone").resolve())
    assert not link.is_symlink()


def test_uninstall_other_ember_link(
    env: dict[str, str], home: Path, source: Path, tmp_path: Path
) -> None:
    other = make_skill(tmp_path / "older-checkout" / "skill")
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(other)
    assert skill.uninstall(env, [source]).action == "removed"
    assert (other / "SKILL.md").exists()


def test_uninstall_refuses_link_to_other_skill(
    env: dict[str, str], home: Path, source: Path, tmp_path: Path
) -> None:
    other = make_skill(tmp_path / "other", "other")
    link = link_of(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(other)
    with pytest.raises(skill.SkillError) as error:
        skill.uninstall(env, [source])
    assert error.value.code == 75
    assert error.value.line == f"owner: move or remove {link}, then emb skill install"
    assert link.is_symlink()


@pytest.mark.parametrize("kind", ["folder", "file"])
def test_uninstall_never_removes_folder_or_file(
    env: dict[str, str], home: Path, source: Path, kind: str
) -> None:
    link = link_of(home)
    if kind == "folder":
        make_skill(link)
    else:
        link.parent.mkdir(parents=True)
        link.write_text("mine")
    with pytest.raises(skill.SkillError) as error:
        skill.uninstall(env, [source])
    assert error.value.code == 75
    assert link.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any folder")
def test_uninstall_from_unwritable_folder_exits_73(
    env: dict[str, str], home: Path, source: Path
) -> None:
    skill.install(env, [source])
    skills = link_of(home).parent
    skills.chmod(0o555)
    try:
        with pytest.raises(skill.SkillError) as error:
            skill.uninstall(env, [source])
    finally:
        skills.chmod(0o755)
    assert error.value.code == 73
    assert link_of(home).is_symlink()
