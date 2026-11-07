import contextlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ember import choices, paths, prepare, processes


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    _git(folder, "init", "-q", "-b", "main", env=env)
    (folder / "README.md").write_text("hello\n")
    (folder / ".gitignore").write_text("ignored/\n")
    _git(folder, "add", "-A", env=env)
    _git(folder, "commit", "-q", "-m", "first", env=env)
    return folder


def _common(repo: Path, env: dict[str, str]) -> Path:
    return Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env)
    )


def test_lookup_unset(repo: Path, env: dict[str, str]) -> None:
    assert prepare.lookup(_common(repo, env), env=env) is None


@pytest.mark.parametrize("command", ["", "npm ci && npm test"])
def test_lookup_set_command(repo: Path, env: dict[str, str], command: str) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): command}}, env=env)
    assert prepare.lookup(_common(repo, env), env=env) == command


def test_lookup_separate_clone(repo: Path, env: dict[str, str], tmp_path: Path) -> None:
    other = tmp_path / "clone"
    _git(repo, "clone", "-q", str(repo), str(other), env=env)
    choices.set_settings({"worktree.prepare": {str(repo): "npm ci"}}, env=env)
    assert prepare.lookup(_common(other, env), env=env) is None


def test_lookup_linked_worktree(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), env=env)
    choices.set_settings({"worktree.prepare": {str(repo): "npm ci"}}, env=env)
    assert prepare.lookup(_common(linked, env), env=env) == "npm ci"


def test_lookup_symlinked_paths(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    common_alias = tmp_path / "common-alias"
    common_alias.symlink_to(_common(repo, env), target_is_directory=True)
    choices.set_settings({"worktree.prepare": {str(alias): "npm ci"}}, env=env)
    assert prepare.lookup(common_alias, env=env) == "npm ci"


def test_lookup_ignores_git_location_environment(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): "npm ci"}}, env=env)
    common = _common(repo, env)
    env["GIT_DIR"] = str(tmp_path / "not-git")
    env["GIT_WORK_TREE"] = str(tmp_path / "not-a-worktree")
    assert prepare.lookup(common, env=env) == "npm ci"


def test_lookup_skips_removed_checkout_for_other_repository(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    removed = tmp_path / "removed"
    removed.mkdir()
    choices.set_settings(
        {"worktree.prepare": {str(removed): "old", str(repo): "current"}}, env=env
    )
    shutil.rmtree(removed)
    assert prepare.lookup(_common(repo, env), env=env) == "current"


def test_lookup_existing_non_repository_is_error(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    other = tmp_path / "not-a-repo"
    other.mkdir()
    choices.set_settings({"worktree.prepare": {str(other): "npm ci"}}, env=env)
    with pytest.raises(prepare.PreparationError, match="git rev-parse"):
        prepare.lookup(_common(repo, env), env=env)


def test_lookup_settings_corruption_is_error(repo: Path, env: dict[str, str]) -> None:
    choices.set_settings({"worktree.prepare": {}}, env=env)
    with sqlite3.connect(paths.settings_db_path(env)) as conn:
        conn.execute(
            "UPDATE settings SET value = 'broken' WHERE key = 'worktree.prepare'"
        )
    with pytest.raises(prepare.PreparationError, match="cannot read worktree.prepare"):
        prepare.lookup(repo / ".git", env=env)


def test_lookup_database_failure_is_error(repo: Path, env: dict[str, str]) -> None:
    database = paths.settings_db_path(env)
    database.parent.mkdir(parents=True)
    database.write_text("not a database")
    with pytest.raises(prepare.PreparationError, match="cannot read worktree.prepare"):
        prepare.lookup(repo / ".git", env=env)


def test_lookup_git_start_failure_is_error(repo: Path, env: dict[str, str]) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): "npm ci"}}, env=env)
    env["PATH"] = str(repo / "no-bin")
    with pytest.raises(prepare.PreparationError, match="git rev-parse"):
        prepare.lookup(repo / ".git", env=env)


def test_lookup_refuses_multiple_common_folder_matches(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), env=env)
    choices.set_settings(
        {"worktree.prepare": {str(repo): "first", str(linked): "second"}}, env=env
    )
    with pytest.raises(prepare.PreparationError, match="several.*entries match"):
        prepare.lookup(_common(repo, env), env=env)


def test_run_output_environment_stdin_and_private_log(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    log = tmp_path / "prepare.log"
    log.write_text("old log")
    log.chmod(0o644)
    env["PREPARE_TEST"] = "from caller"
    recorded: list[tuple[int, int]] = []

    def started(pid: int, ticks: int) -> None:
        assert log.stat().st_mode & 0o777 == 0o600
        assert prepare.start_ticks(pid) == ticks
        assert os.getpgid(pid) == pid
        recorded.append((pid, ticks))

    result = prepare.run(
        'pwd; printf "%s\\n" "$PREPARE_TEST"; printf "stderr\\n" >&2; '
        'test "$(readlink /proc/self/fd/0)" = /dev/null',
        repo,
        log,
        env=env,
        started=started,
    )
    assert len(recorded) == 1
    assert result.state == "ok"
    assert result.exit == 0
    assert result.seconds > 0
    assert result.log == str(log)
    assert result.left_behind == []
    assert result.message is None
    assert log.read_text() == f"{repo}\nfrom caller\nstderr\n"
    assert prepare.start_ticks(recorded[0][0]) is None
    assert json.loads(json.dumps(result.as_dict()))["state"] == "ok"


@pytest.mark.parametrize("exit_code", [0, 1, 127])
def test_run_exit(
    repo: Path, env: dict[str, str], tmp_path: Path, exit_code: int
) -> None:
    result = prepare.run(
        f"exit {exit_code}",
        repo,
        tmp_path / "prepare.log",
        env=env,
        started=lambda *_: None,
    )
    assert result.state == ("failed" if exit_code else "ok")
    assert result.exit == exit_code
    assert result.left_behind == []
    assert result.message == (f"preparation exited {exit_code}" if exit_code else None)


def test_run_unknown_command(repo: Path, env: dict[str, str], tmp_path: Path) -> None:
    result = prepare.run(
        "ember_test_missing_command",
        repo,
        tmp_path / "prepare.log",
        env=env,
        started=lambda *_: None,
    )
    assert result.state == "failed"
    assert result.exit == 127
    assert "not found" in Path(result.log).read_text()


def test_run_shell_start_failure(
    repo: Path, env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("cannot execute shell")

    monkeypatch.setattr(prepare.subprocess, "Popen", fail)
    recorded: list[tuple[int, int]] = []
    result = prepare.run(
        "true",
        repo,
        tmp_path / "prepare.log",
        env=env,
        started=lambda pid, ticks: recorded.append((pid, ticks)),
    )
    assert result.state == "not-started"
    assert result.exit is None
    assert "cannot start preparation" in (result.message or "")
    assert recorded == []


def test_run_git_status_failure(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    result = prepare.run(
        "rm -rf .git", repo, tmp_path / "prepare.log", env=env, started=lambda *_: None
    )
    assert result.state == "failed"
    assert result.exit == 0
    assert "git status" in (result.message or "")


def test_run_git_status_start_failure(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    env["PATH"] = str(tmp_path / "no-bin")
    result = prepare.run(
        "true", repo, tmp_path / "prepare.log", env=env, started=lambda *_: None
    )
    assert result.state == "failed"
    assert result.exit == 0
    assert "git status" in (result.message or "")


def test_run_status_uses_worktree_while_command_inherits_git_environment(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    env["GIT_DIR"] = str(tmp_path / "not-git")
    result = prepare.run(
        'test -n "$GIT_DIR" && printf changed > README.md',
        repo,
        tmp_path / "prepare.log",
        env=env,
        started=lambda *_: None,
    )
    assert result.exit == 0
    assert result.state == "failed"
    assert result.left_behind == ["README.md"]


@pytest.mark.parametrize("name", ["untracked.txt", 'a "quoted"\nfile'])
def test_run_left_behind_untracked_file(
    repo: Path, env: dict[str, str], tmp_path: Path, name: str
) -> None:
    result = prepare.run(
        f"printf created > {shlex.quote(name)}",
        repo,
        tmp_path / "prepare.log",
        env=env,
        started=lambda *_: None,
    )
    assert result.state == "failed"
    assert result.exit == 0
    assert result.left_behind == [name]
    assert name in (result.message or "")
    assert (repo / name).read_text() == "created"


@pytest.mark.parametrize("command", ["printf changed > README.md", "git rm README.md"])
def test_run_left_behind_tracked_change(
    repo: Path, env: dict[str, str], tmp_path: Path, command: str
) -> None:
    result = prepare.run(
        command, repo, tmp_path / "prepare.log", env=env, started=lambda *_: None
    )
    assert result.state == "failed"
    assert result.left_behind == ["README.md"]


def test_run_left_behind_staged_rename(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    result = prepare.run(
        "git mv README.md renamed.md",
        repo,
        tmp_path / "prepare.log",
        env=env,
        started=lambda *_: None,
    )
    assert result.state == "failed"
    assert result.left_behind == ["renamed.md", "README.md"]


def test_run_ignored_output_is_ok(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    result = prepare.run(
        "mkdir ignored; printf dependency > ignored/file",
        repo,
        tmp_path / "prepare.log",
        env=env,
        started=lambda *_: None,
    )
    assert result.state == "ok"
    assert result.left_behind == []


def test_run_log_open_failure(repo: Path, env: dict[str, str], tmp_path: Path) -> None:
    result = prepare.run(
        "touch must-not-start",
        repo,
        tmp_path / "absent" / "prepare.log",
        env=env,
        started=lambda *_: None,
    )
    assert result.state == "not-started"
    assert result.exit is None
    assert "cannot open preparation log" in (result.message or "")
    assert not (repo / "must-not-start").exists()


def test_run_stop_before_start(repo: Path, env: dict[str, str], tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    log = tmp_path / "prepare.log"
    result = prepare.run(
        "touch must-not-start", repo, log, env=env, started=lambda *_: None, stop=stop
    )
    assert result.state == "not-started"
    assert result.exit is None
    assert not log.exists()
    assert not (repo / "must-not-start").exists()


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() or not path.read_text().strip():
        assert time.monotonic() < deadline, f"not written: {path}"
        time.sleep(0.01)


@pytest.mark.parametrize("stop_early", [False, True])
def test_run_ends_children_on_timeout_or_stop(
    repo: Path, env: dict[str, str], tmp_path: Path, stop_early: bool
) -> None:
    marker = tmp_path / "child.pid"
    stop = threading.Event()
    recorded: list[tuple[int, int]] = []
    child: list[processes.Process] = []
    timer = threading.Timer(0.1, stop.set)

    def started(pid: int, ticks: int) -> None:
        recorded.append((pid, ticks))
        _wait_for_file(marker)
        child_pid = int(marker.read_text())
        child_ticks = prepare.start_ticks(child_pid)
        assert child_ticks is not None
        child.append((child_pid, child_ticks))
        if stop_early:
            timer.start()

    try:
        # The child ignores TERM, and the shell waits for it. Both must end.
        command = (
            "sh -c 'trap \"\" TERM; exec sleep 60' & "
            f"echo $! > {shlex.quote(str(marker))}; wait"
        )
        result = prepare.run(
            command,
            repo,
            tmp_path / "prepare.log",
            env=env,
            started=started,
            stop=stop,
            timeout=0.5 if not stop_early else 30,
        )
        assert result.state == ("interrupted" if stop_early else "timeout")
        assert result.exit == -signal.SIGKILL
        assert recorded and not processes.running(recorded[0])
        deadline = time.monotonic() + 5
        while processes.running(child[0]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not processes.running(child[0])
        assert result.seconds < 10
    finally:
        timer.cancel()
        if stop_early:
            timer.join(timeout=1)
        for pid, _ticks in [*recorded, *child]:
            if processes.running((pid, _ticks)):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("interrupt", [False, True])
def test_run_recording_exception_ends_group(
    repo: Path, env: dict[str, str], tmp_path: Path, interrupt: bool
) -> None:
    recorded: list[processes.Process] = []

    def started(pid: int, ticks: int) -> None:
        recorded.append((pid, ticks))
        if interrupt:
            raise KeyboardInterrupt
        raise RuntimeError("cannot persist process identity")

    if interrupt:
        result = prepare.run(
            "sleep 60", repo, tmp_path / "prepare.log", env=env, started=started
        )
        assert result.state == "interrupted"
        assert result.exit == -signal.SIGKILL
    else:
        with pytest.raises(RuntimeError, match="cannot persist"):
            prepare.run(
                "sleep 60", repo, tmp_path / "prepare.log", env=env, started=started
            )
    assert recorded and not processes.running(recorded[0])


@pytest.fixture
def group() -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 60 & wait"], start_new_session=True
    )
    yield process
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def test_end_group_matches_start_ticks(group: subprocess.Popen[bytes]) -> None:
    ticks = prepare.start_ticks(group.pid)
    assert ticks is not None
    assert prepare.end_group(group.pid, ticks)
    assert group.wait(timeout=5) == -signal.SIGKILL


def test_end_group_refuses_reused_pid(group: subprocess.Popen[bytes]) -> None:
    ticks = prepare.start_ticks(group.pid)
    assert ticks is not None
    assert not prepare.end_group(group.pid, ticks + 1)
    assert group.poll() is None


def test_end_group_missing_leader() -> None:
    assert not prepare.end_group(2**22 + 1, 1)
