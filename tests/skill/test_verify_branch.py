import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skill" / "scripts" / "verify_branch.py"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        # Ignored by the script: they must not point git elsewhere.
        "GIT_DIR": str(tmp_path / "elsewhere"),
        "GIT_WORK_TREE": str(tmp_path / "elsewhere"),
    }


def git(repo: Path, env: dict[str, str], *args: str) -> str:
    clean = {k: v for k, v in env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=clean,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def verify(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, env, "init", "-q", "-b", "main")
    (folder / "a.txt").write_text("one\n")
    git(folder, env, "add", "-A")
    git(folder, env, "commit", "-q", "-m", "first")
    git(folder, env, "switch", "-q", "-c", "session")
    return folder


@pytest.fixture
def worker(repo: Path, env: dict[str, str], tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    git(repo, env, "worktree", "add", "-q", "-b", "worker", str(worktree), "session")
    (worktree / "a.txt").write_text("one\ntwo\n")
    (worktree / "b.txt").write_text("new\n")
    git(worktree, env, "add", "-A")
    git(worktree, env, "commit", "-q", "-m", "first change")
    (worktree / "b.txt").write_text("new\nnewer\n")
    git(worktree, env, "commit", "-q", "-am", "second change")
    return worktree


def test_prints_tip_numstat_and_clean_status(
    repo: Path, worker: Path, env: dict[str, str]
) -> None:
    result = verify(
        env,
        "--repo", str(repo),
        "--base", "session",
        "--branch", "worker",
        "--worktree", str(worker),
        "--expect-files", "2",
    )  # fmt: skip
    tip = git(repo, env, "rev-parse", "worker")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == (
        f"tip: {tip}\n"
        "numstat: session...worker\n"
        "1\t0\ta.txt\n"
        "2\t0\tb.txt\n"
        "files: 2\n"
        f"status --short: {worker}\n"
    )


def test_expect_files_mismatch(repo: Path, worker: Path, env: dict[str, str]) -> None:
    result = verify(
        env, "--repo", str(repo), "--base", "session", "--branch", "worker",
        "--expect-files", "1",
    )  # fmt: skip
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[lines.index("files: 2") + 1] == "MISMATCH: expected 1 files; found 2"
    assert lines[-1] == f"status --short: {repo}"


def test_dirty_worktree(repo: Path, worker: Path, env: dict[str, str]) -> None:
    (worker / "a.txt").write_text("changed\n")
    (worker / "c.txt").write_text("untracked\n")
    result = verify(
        env, "--repo", str(repo), "--base", "session", "--branch", "worker",
        "--worktree", str(worker),
    )  # fmt: skip
    assert result.returncode == 0
    assert result.stdout.endswith(f"status --short: {worker}\n M a.txt\n?? c.txt\n")


def test_commit_as_branch(repo: Path, worker: Path, env: dict[str, str]) -> None:
    tip = git(repo, env, "rev-parse", "worker")
    result = verify(env, "--repo", str(repo), "--base", "session", "--branch", tip)
    assert result.returncode == 0
    assert result.stdout.splitlines()[:2] == [
        f"tip: {tip}",
        f"numstat: session...{tip}",
    ]


def test_unknown_branch_exits_2(repo: Path, env: dict[str, str]) -> None:
    result = verify(env, "--repo", str(repo), "--base", "session", "--branch", "nope")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("verify_branch.py: git rev-parse failed: ")


def test_not_a_repository_exits_2(tmp_path: Path, env: dict[str, str]) -> None:
    result = verify(env, "--repo", str(tmp_path), "--base", "a", "--branch", "b")
    assert result.returncode == 2
    assert result.stderr.startswith("verify_branch.py: ")


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--repo", "x", "--base", "a"],
        ["--repo", "x", "--base", "a", "--branch", "b", "--expect-files", "-1"],
        ["--repo", "x", "--base", "a", "--branch", "b", "--expect-files", "two"],
        ["--repo", "x", "--base", "a", "--branch", "b", "--nope"],
    ],
)
def test_usage_error_exits_64(args: list[str], env: dict[str, str]) -> None:
    result = verify(env, *args)
    assert result.returncode == 64
    assert result.stdout == ""
    assert "usage: verify_branch.py" in result.stderr


def test_path_with_tab_is_quoted(repo: Path, env: dict[str, str]) -> None:
    git(repo, env, "switch", "-q", "-c", "tabbed")
    (repo / "x\ty.txt").write_text("tab\n")
    (repo / "café.txt").write_text("accent\n")
    git(repo, env, "add", "-A")
    git(repo, env, "commit", "-q", "-m", "special names")
    result = verify(env, "--repo", str(repo), "--base", "session", "--branch", "tabbed")
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert '1\t0\t"caf\\303\\251.txt"' in lines
    assert '1\t0\t"x\\ty.txt"' in lines
    assert "files: 2" in lines
