import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ember import ground
from ember.cli import main


def _git(cwd: Path, *args: str, env: Mapping[str, str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=dict(env),
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("")
    found = shutil.which("git")
    assert found is not None
    return {
        "HOME": str(home),
        "PATH": f"{Path(found).parent}{os.pathsep}{os.defpath}",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    _git(folder, "init", "-q", "-b", "main", env=env)
    (folder / "README.md").write_text("hello\n")
    _git(folder, "add", "-A", env=env)
    _git(folder, "commit", "-q", "-m", "first", env=env)
    return folder


def _settings(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text)


def _ground_json(
    argv: list[str], env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    assert main(["ground", "--json", *argv], env=env) == 0
    return json.loads(capsys.readouterr().out)


def test_clean_repository(
    repo: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "CLAUDE.md").write_text("x\n")
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", "second", env=env)
    commit = _git(repo, "rev-parse", "HEAD", env=env)
    result = _ground_json(["--dir", str(repo)], env, capsys)
    assert result["machine"]
    folder = result["directory"]
    assert folder["path"] == str(repo)
    assert folder["git"] is True
    assert folder["top"] == str(repo)
    assert folder["branch"] == {"name": "main", "commit": commit}
    assert folder["dirty"] == {"value": False, "paths": 0}
    assert folder["files"] == {"CLAUDE.md": True, "AGENTS.md": False}
    assert folder["remote"] is None
    assert folder["remote_reason"] == "the repository has no remote"


def test_subfolder_reads_the_top(repo: Path, env: dict[str, str]) -> None:
    sub = repo / "sub"
    sub.mkdir()
    (repo / "AGENTS.md").write_text("x\n")
    folder = ground.read(str(sub), env)["directory"]
    assert folder["path"] == str(sub)
    assert folder["top"] == str(repo)
    assert folder["files"] == {"CLAUDE.md": False, "AGENTS.md": True}


def test_dirty_counts_changed_paths(repo: Path, env: dict[str, str]) -> None:
    (repo / "README.md").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt", env=env)
    _git(repo, "commit", "-q", "-m", "a", env=env)
    _git(repo, "mv", "a.txt", "b.txt", env=env)
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": True, "paths": 3}


def test_ignored_files_are_not_dirty(repo: Path, env: dict[str, str]) -> None:
    (repo / ".git" / "info" / "exclude").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "out").write_text("x\n")
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": False, "paths": 0}


def test_detached_head(repo: Path, env: dict[str, str]) -> None:
    commit = _git(repo, "rev-parse", "HEAD", env=env)
    _git(repo, "checkout", "-q", "--detach", env=env)
    branch = ground.read(str(repo), env)["directory"]["branch"]
    assert branch == {"name": None, "name_reason": "detached HEAD", "commit": commit}
    lines = ground.lines(ground.read(str(repo), env))
    assert f"branch: detached at {commit}" in lines


def test_branch_without_commit(tmp_path: Path, env: dict[str, str]) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    _git(folder, "init", "-q", "-b", "start", env=env)
    branch = ground.read(str(folder), env)["directory"]["branch"]
    assert branch["name"] == "start"
    assert branch["commit"] is None
    assert branch["commit_reason"] == "the branch has no commit"


def test_outside_a_checkout(tmp_path: Path, env: dict[str, str]) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    env = dict(env, GIT_CEILING_DIRECTORIES=str(tmp_path))
    folder = ground.read(str(plain), env)["directory"]
    assert folder["git"] is False
    for field in ("top", "branch", "dirty", "remote", "files"):
        assert folder[field] is None
        assert folder[f"{field}_reason"] == "not a git checkout"


def test_git_location_variables_are_ignored(
    repo: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "elsewhere", env=env)
    env = dict(env, GIT_DIR=str(other / ".git"))
    assert ground.read(str(repo), env)["directory"]["branch"]["name"] == "main"


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://github.com/owner/repo.git", "github"),
        ("git@github.com:owner/repo.git", "github"),
        ("ssh://git@GitHub.com/owner/repo", "github"),
        ("https://gitlab.com/owner/repo.git", "other"),
        ("git@example.com:github.com/repo.git", "other"),
        ("https://github.com.example.org/repo", "other"),
        ("/srv/git/repo.git", "other"),
    ],
)
def test_remote_kind(url: str, kind: str) -> None:
    assert ground.remote_kind(url) == kind


def test_origin_comes_first(repo: Path, env: dict[str, str]) -> None:
    _git(repo, "remote", "add", "backup", "/srv/backup.git", env=env)
    _git(repo, "remote", "add", "origin", "git@github.com:o/r.git", env=env)
    remote = ground.read(str(repo), env)["directory"]["remote"]
    assert remote == {
        "name": "origin",
        "url": "git@github.com:o/r.git",
        "kind": "github",
    }


def test_first_remote_without_origin(repo: Path, env: dict[str, str]) -> None:
    _git(repo, "remote", "add", "alpha", "https://example.com/a.git", env=env)
    _git(repo, "remote", "add", "beta", "https://github.com/b.git", env=env)
    remote = ground.read(str(repo), env)["directory"]["remote"]
    assert remote == {
        "name": "alpha",
        "url": "https://example.com/a.git",
        "kind": "other",
    }


def test_surface(env: dict[str, str]) -> None:
    assert ground.surface(env) == {
        "entrypoint": None,
        "desktop": False,
        "entrypoint_reason": "CLAUDE_CODE_ENTRYPOINT is not set",
    }
    desktop = dict(env, CLAUDE_CODE_ENTRYPOINT="claude-desktop")
    assert ground.surface(desktop) == {"entrypoint": "claude-desktop", "desktop": True}
    cli = dict(env, CLAUDE_CODE_ENTRYPOINT="cli")
    assert ground.surface(cli) == {"entrypoint": "cli", "desktop": False}


def test_worktree_base_without_settings(repo: Path, env: dict[str, str]) -> None:
    result = ground.read(str(repo), env)
    assert result["worktree_base"] is None
    assert result["worktree_base_reason"].startswith("worktree.baseRef is not set in")


def test_worktree_base_later_file_wins(repo: Path, env: dict[str, str]) -> None:
    user = Path(env["HOME"]) / ".claude" / "settings.json"
    _settings(user, {"worktree": {"baseRef": "head"}})
    assert ground.read(str(repo), env)["worktree_base"] == {
        "value": "head",
        "file": str(user),
    }
    project = repo / ".claude" / "settings.json"
    _settings(project, {"worktree": {"baseRef": "fresh"}})
    local = repo / ".claude" / "settings.local.json"
    _settings(local, {"other": 1})
    assert ground.read(str(repo), env)["worktree_base"] == {
        "value": "fresh",
        "file": str(project),
    }
    _settings(local, {"worktree": {"baseRef": "local"}})
    assert ground.read(str(repo), env)["worktree_base"] == {
        "value": "local",
        "file": str(local),
    }


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("{not json", "is not valid JSON"),
        ("[1]", "does not hold a JSON object"),
        ({"worktree": "x"}, "is not an object"),
        ({"worktree": {"baseRef": ""}}, "is not a non-empty string"),
        ({"worktree": {"baseRef": 3}}, "is not a non-empty string"),
    ],
)
def test_worktree_base_invalid(
    repo: Path,
    env: dict[str, str],
    content: object,
    reason: str,
) -> None:
    _settings(repo / ".claude" / "settings.json", {"worktree": {"baseRef": "head"}})
    _settings(repo / ".claude" / "settings.local.json", content)
    result = ground.read(str(repo), env)
    assert result["worktree_base"] is None
    assert reason in result["worktree_base_reason"]


def test_worktree_base_unreadable(repo: Path, env: dict[str, str]) -> None:
    (Path(env["HOME"]) / ".claude" / "settings.json").mkdir(parents=True)
    result = ground.read(str(repo), env)
    assert result["worktree_base"] is None
    assert "cannot be read" in result["worktree_base_reason"]


def test_outside_a_checkout_reads_only_user_settings(
    tmp_path: Path, env: dict[str, str]
) -> None:
    plain = tmp_path / "plain"
    _settings(plain / ".claude" / "settings.json", {"worktree": {"baseRef": "x"}})
    env = dict(env, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert ground.read(str(plain), env)["worktree_base"] is None


def test_text_lines(
    repo: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/o/r.git", env=env)
    commit = _git(repo, "rev-parse", "HEAD", env=env)
    env = dict(env, CLAUDE_CODE_ENTRYPOINT="claude-desktop")
    assert main(["ground", "--dir", str(repo)], env=env) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split(":", 1)[0] for line in out] == [
        "machine",
        "surface",
        "worktree_base",
        "directory",
        "git",
        "top",
        "branch",
        "dirty",
        "remote",
        "files",
    ]
    assert "surface: claude-desktop (desktop)" in out
    assert f"branch: main at {commit}" in out
    assert "dirty: no" in out
    assert "remote: origin https://github.com/o/r.git (github)" in out
    assert "files: CLAUDE.md no, AGENTS.md no" in out
    assert out[2].startswith("worktree_base: null (worktree.baseRef is not set")


def test_text_lines_outside_a_checkout(
    tmp_path: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    env = dict(env, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert main(["ground", "--dir", str(plain)], env=env) == 0
    out = capsys.readouterr().out.splitlines()
    assert "git: no" in out
    assert "remote: null (not a git checkout)" in out


def test_missing_dir_exits_66(
    tmp_path: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing"
    assert main(["ground", "--dir", str(missing)], env=env) == 66
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"emb ground: {missing} does not exist" in captured.err
    assert "next: emb ground --dir" in captured.err


def test_file_as_dir_exits_66(
    tmp_path: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "file"
    path.write_text("x")
    assert main(["ground", "--dir", str(path)], env=env) == 66
    assert "is not a folder" in capsys.readouterr().err


def test_usage_error_exits_64(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["ground", "--nope"])
    assert exit_info.value.code == 64


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["ground", "--help"])
    assert exit_info.value.code == 0
    assert "--dir" in capsys.readouterr().out


def test_git_missing_gives_reasons(tmp_path: Path, env: dict[str, str]) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    env = dict(env, PATH=str(tmp_path / "nothing"))
    result = ground.read(str(folder), env)["directory"]
    assert result["git"] is None
    assert "git cannot run" in result["git_reason"]
    assert result["top"] is None
    assert "git cannot run" in result["top_reason"]


def test_status_does_not_write_the_index(repo: Path, env: dict[str, str]) -> None:
    readme = repo / "README.md"
    stat = readme.stat()
    os.utime(readme, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    index = repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": False, "paths": 0}
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert not (repo / ".git" / "index.lock").exists()


def test_nested_worktree_is_not_dirty(repo: Path, env: dict[str, str]) -> None:
    nested = repo / ".claude" / "worktrees" / "x"
    _git(repo, "worktree", "add", "-q", "-b", "x", str(nested), env=env)
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": False, "paths": 0}
    (repo / "new.txt").write_text("new\n")
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": True, "paths": 1}


def test_folder_beside_a_nested_worktree_is_dirty(
    repo: Path, env: dict[str, str]
) -> None:
    nested = repo / ".claude" / "worktrees" / "x"
    _git(repo, "worktree", "add", "-q", "-b", "x", str(nested), env=env)
    (repo / ".claude" / "worktrees" / "y").mkdir()
    (repo / ".claude" / "worktrees" / "y" / "f").write_text("f\n")
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": True, "paths": 1}


def test_worktree_outside_the_top_is_not_excluded(
    repo: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    _git(repo, "worktree", "add", "-q", "-b", "w", str(tmp_path / "w"), env=env)
    (repo / "new.txt").write_text("new\n")
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": True, "paths": 1}
    linked = ground.read(str(tmp_path / "w"), env)["directory"]
    assert linked["dirty"] == {"value": False, "paths": 0}


def test_untracked_repository_is_dirty(repo: Path, env: dict[str, str]) -> None:
    other = repo / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main", env=env)
    folder = ground.read(str(repo), env)["directory"]
    assert folder["dirty"] == {"value": True, "paths": 1}


@pytest.mark.parametrize(
    ("count", "text"),
    [(1, "dirty: yes, 1 changed path"), (2, "dirty: yes, 2 changed paths")],
)
def test_dirty_text(repo: Path, env: dict[str, str], count: int, text: str) -> None:
    for number in range(count):
        (repo / f"new{number}.txt").write_text("new\n")
    assert text in ground.lines(ground.read(str(repo), env))


def test_unreadable_current_folder_exits_66(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert main(["ground"], env=env) == 66
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "emb ground: the current folder cannot be read" in captured.err
    assert "next: emb ground --dir" in captured.err


def test_claude_config_dir_moves_user_settings(
    repo: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    home_file = Path(env["HOME"]) / ".claude" / "settings.json"
    _settings(home_file, {"worktree": {"baseRef": "head"}})
    config = tmp_path / "config"
    moved = config / "settings.json"
    _settings(moved, {"worktree": {"baseRef": "fresh"}})
    with_config = dict(env, CLAUDE_CONFIG_DIR=str(config))
    assert ground.settings_files(None, with_config) == [moved]
    assert ground.read(str(repo), with_config)["worktree_base"] == {
        "value": "fresh",
        "file": str(moved),
    }
    empty = dict(env, CLAUDE_CONFIG_DIR="")
    assert ground.settings_files(None, empty) == [home_file]
    assert ground.read(str(repo), empty)["worktree_base"] == {
        "value": "head",
        "file": str(home_file),
    }
