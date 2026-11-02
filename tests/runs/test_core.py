import json
import stat
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import choices, engines, paths, sandbox
from ember.engines import Driver, LaunchSpec
from ember.runs import core, exits, record
from ember.runs.core import Core, RunError, StartOptions

from .stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    engine_cache,
    git,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
    session_folder,
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, str]]:
    environment = make_env(tmp_path)
    yield environment
    kill_sessions(tmp_path)


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    return make_repo(tmp_path, env)


def _driver(
    env: dict[str, str],
    name: str = "claude",
    **options: Any,  # noqa: ANN401
) -> StubDriver:
    return StubDriver(name, script_path(env), **options)


def _core(
    env: dict[str, str],
    driver: Driver | None = None,
    *,
    stall: float = 0.1,
    quiet: bool = False,
) -> Core:
    chosen = driver or _driver(env)
    return Core(
        env,
        load_driver=lambda name: chosen,
        poll_seconds=0.02,
        stall_minutes=stall,
        quiet=quiet,
    )


def _brief(tmp_path: Path, text: str, name: str = "brief.md") -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _start(
    core_: Core,
    repo: Path,
    brief: str,
    **options: Any,  # noqa: ANN401
) -> int:
    options.setdefault("engine", "claude")
    options.setdefault("window", False)
    return core_.execute(
        lambda: core_.start(StartOptions(dir=str(repo), task_file=brief, **options))
    )


def _run_id(out: str) -> str:
    first = out.splitlines()[0]
    assert first.startswith("run ")
    return first.removeprefix("run ")


def _fields(out: str) -> dict[str, str]:
    result = {}
    for line in out.split(core.REPLY_MARK)[0].splitlines()[1:]:
        name, _, value = line.partition(" ")
        result[name] = value
    return result


def _events(tmp_path: Path, run_id: str) -> list[dict[str, str]]:
    text = (session_folder(tmp_path, run_id) / "input.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


# Starting an editing run


def test_editing_run_commits_its_turn(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(
        tmp_path, "Add a greeting\n\n@write hello.txt hi there\n@reply all done\n"
    )
    driver = _driver(env)
    code = _start(
        _core(env, driver), repo, brief, trailers=("Co-Authored-By: X <x@y>",)
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    run_id = _run_id(out)
    assert run_id.startswith("claude-")
    lines = out.splitlines()
    assert lines[1].startswith("claude opus · high · ")
    assert lines[1].endswith(" · exit 0 · 1 files · turn 1")
    run = record.load(run_id, env)
    worktree = repo / ".ember" / "worktrees" / run_id
    fields = _fields(out)
    assert fields["run"] == run_id
    assert fields["worktree"] == str(worktree)
    assert fields["branch"] == f"ember/claude/{run_id}"
    assert fields["commit"] == run.commits[0]
    assert fields["window"] == "-"
    assert fields["reply"] == str(run.folder / "turn-1.md")
    assert out.split(core.REPLY_MARK + "\n")[1] == "all done\n"
    assert run.status == record.WAITING
    assert run.turn == 1
    assert run.turn_state == record.COMMITTED
    assert run.repo == str(repo)
    assert run.worktree == str(worktree)
    assert run.base == git(repo, "rev-parse", "HEAD", env=env)
    assert run.expected_head == run.commits[0]
    assert run.sandbox == "none"
    assert run.network is False
    assert run.read_effective == ()
    assert run.subject == "Add a greeting"
    assert run.last_exit == 0
    assert (worktree / "hello.txt").read_text() == "hi there\n"
    message = git(worktree, "log", "-1", "--format=%B", env=env)
    assert message == "Add a greeting\n\nCo-Authored-By: X <x@y>"
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD", env=env) == run.branch
    exclude = Path(git(repo, "rev-parse", "--git-common-dir", env=env))
    assert ".ember/" in (repo / exclude / "info" / "exclude").read_text().splitlines()
    assert git(repo, "status", "--porcelain", env=env) == ""
    assert (run.folder / "brief.md").read_text() == Path(brief).read_text()
    assert (run.folder / "turn-1.md").read_text() == "all done"
    assert stat.S_IMODE(run.folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((run.folder / "run.json").stat().st_mode) == 0o600
    pasted = "".join(item.get("text", "") for item in _events(tmp_path, run_id))
    assert pasted == core.first_message(run_id, Path(brief).read_text())
    assert pasted.startswith(
        f"Brief from Claude, the lead, for Ember run {run_id}.\n\n"
    )
    assert driver.commands[0][1] == pasted


def test_turn_without_changes_makes_no_commit(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Look around\n"))
    out, _ = capsys.readouterr()
    assert code == 0
    assert " · exit 0 · 0 files · turn 1" in out
    run = record.load(_run_id(out), env)
    assert run.commits == ()
    assert run.turn_state == record.COMMITTED
    assert _fields(out)["commit"] == "-"


def test_quiet_prints_first_line_and_summary(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env, quiet=True), repo, _brief(tmp_path, "Task\n"))
    out, _ = capsys.readouterr()
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("run claude-")
    assert lines[1].endswith("turn 1")


def test_long_subject_is_cut_and_later_turns_are_numbered(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subject = "x" * 80
    brief = _brief(tmp_path, f"\n{subject}\n@write a.txt one\n")
    code = _start(_core(env), repo, brief)
    out, _ = capsys.readouterr()
    assert code == 0
    run_id = _run_id(out)
    run = record.load(run_id, env)
    log = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert log == "x" * 72
    message = _brief(tmp_path, "@write b.txt two\n", "message.md")
    code = _core(env).execute(lambda: _core(env).send(run_id, message))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert out.splitlines()[0].endswith(" · exit 0 · 1 files · turn 2")
    log = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert log == "x" * 72 + " (turn 2)"
    run = record.load(run_id, env)
    assert len(run.commits) == 2
    assert run.turn == 2
    assert run.reply == str(run.folder / "turn-2.md")
    # The message goes in as written, with no line above it.
    texts = [item["text"] for item in _events(tmp_path, run_id) if "text" in item]
    assert texts[-1] == "@write b.txt two\n"


def test_effort_from_driver_when_menu_row_names_another_model(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), model="sonnet")
    out, _ = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert (run.model, run.effort) == ("sonnet", "medium")


def test_explicit_effort_wins(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), effort="low")
    out, _ = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert (run.model, run.effort) == ("opus", "low")


def test_brief_on_the_command_line_is_not_pasted(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver = _driver(env, paste=False)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@write c.txt c\n"))
    out, err = capsys.readouterr()
    assert code == 0, err
    run_id = _run_id(out)
    assert _events(tmp_path, run_id) == []
    assert " · 1 files · turn 1" in out


def test_window_opens_with_terminal_setting(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"terminal": "true"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), window=True)
    out, _ = capsys.readouterr()
    assert code == 0
    assert _fields(out)["window"].isdigit()


def test_failed_window_warns_and_run_continues(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"terminal": str(tmp_path / "no-terminal")}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), window=True)
    out, err = capsys.readouterr()
    assert code == 0
    assert "WARNING: no window for claude-" in err
    assert _fields(out)["window"] == "-"


# Read-only runs


def test_read_only_run_works_in_the_checkout(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (repo / "dirty.txt").write_text("before\n")
    code = _start(
        _core(env), repo, _brief(tmp_path, "Review\n@reply fine\n"), read_only=True
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    run = record.load(_run_id(out), env)
    assert run.read_only
    assert run.worktree == str(repo)
    assert run.branch is None
    assert run.expected_head is None
    assert run.commits == ()
    assert run.turn_state == record.COLLECTED
    assert run.last_reason is None
    assert "WARNING: read-only" not in err
    assert not (repo / ".ember").exists()
    assert _fields(out)["branch"] == "-"


def test_read_only_run_that_changes_files_is_reported(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Review\n@write README.md changed\n@write new.txt n\n")
    code = _start(_core(env), repo, brief, read_only=True)
    out, err = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert f"WARNING: read-only run {run.id} changed README.md\n" in err
    assert f"WARNING: read-only run {run.id} changed new.txt\n" in err
    assert run.last_reason == "changed README.md, new.txt"
    assert git(repo, "log", "--format=%s", env=env) == "first"


# Turn failures


def test_stall_leaves_the_session_running_and_recover_collects(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Slow\n@hang\n@write late.txt late\n@reply late\n")
    code = _start(_core(env, stall=0.01), repo, brief)
    out, err = capsys.readouterr()
    assert code == 124
    run_id = _run_id(out)
    assert "did not change for 0.01 min" in err
    assert err.splitlines()[-1] == (
        f"next: emb run --recover {run_id} "
        "(waits for the turn still running in its session, then collects it)"
    )
    run = record.load(run_id, env)
    assert run.status == record.RUNNING
    assert run.turn_state == record.SENT
    assert run.last_exit == 124
    (run.folder / "stub" / "release").write_text("")
    code = _core(env).execute(lambda: _core(env).recover(run_id))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert " · 1 files · turn 1" in out
    run = record.load(run_id, env)
    assert run.status == record.WAITING
    assert run.turn_state == record.COMMITTED
    assert len(run.commits) == 1


def test_screen_changes_keep_a_turn_waiting(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Busy\n@sleep 1.5\n")
    code = _start(_core(env, stall=0.01), repo, brief)
    _, err = capsys.readouterr()
    assert code == 0, err


@pytest.mark.parametrize(
    ("line", "code", "state", "reason"),
    [
        ("@provider rate limited", 73, record.FAILED, "provider error"),
        ("@fail model crashed", 71, record.FAILED, "engine failure"),
        ("@empty", 80, record.WAITING, "no reply"),
    ],
)
def test_turn_outcomes(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    line: str,
    code: int,
    state: str,
    reason: str,
) -> None:
    brief = _brief(tmp_path, f"Task\n@write work.txt w\n{line}\n")
    result = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert result == code
    run = record.load(_run_id(out), env)
    assert run.status == state
    assert run.last_exit == code
    assert f" · exit {code} · 1 files · turn 1" in out
    assert len(run.commits) == 1
    assert err.splitlines()[-1] == exits.line(code, run=run.id)
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == f"Task (turn 1, incomplete: {reason})"


def test_incomplete_later_turn_and_long_subject(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _started(tmp_path, env, repo, capsys, text="y" * 80 + "\n")
    message = _brief(tmp_path, "@write m.txt m\n@fail broke\n", "m.md")
    core_ = _core(env)
    assert core_.execute(lambda: core_.send(run_id, message)) == 71
    run = record.load(run_id, env)
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == "y" * 72 + " (turn 2, incomplete: engine failure)"


def test_recover_commits_a_collected_failed_turn_with_its_reason(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    brief = _brief(tmp_path, "Task\n@write p.txt p\n@provider rate limited\n")
    code = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert code == 73
    assert "also exit 77" in err
    run_id = _run_id(out)
    run = record.load(run_id, env)
    assert run.turn_state == record.COLLECTED
    assert run.commits == ()
    hook.unlink()
    core_ = _core(env)
    assert core_.execute(lambda: core_.recover(run_id)) == 73
    run = record.load(run_id, env)
    assert run.turn_state == record.COMMITTED
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == "Task (turn 1, incomplete: provider error)"


def test_signed_out_turn_refreshes_status_and_exits_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_dir = Path(env["PATH"].split(":")[0])
    for name in ("claude", "codex", "opencode"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    code = _start(
        _core(env),
        repo,
        _brief(tmp_path, "Task\n@write s.txt s\n@signedout not logged in\n"),
    )
    out, err = capsys.readouterr()
    assert code == 69
    assert "emb run: not logged in" in err
    worktree = Path(record.load(_run_id(out), env).worktree)
    subject = git(worktree, "log", "-1", "--format=%s", env=env)
    assert subject == "Task (turn 1, incomplete: sign-in failure)"
    held = json.loads(Path(env["XDG_CACHE_HOME"], "ember", "engines.json").read_text())
    assert held["engines"]["claude"]["checked_at"] != 1.0
    assert err.splitlines()[-1].startswith("owner: ")
    assert record.load(_run_id(out), env).status == record.FAILED


def test_engine_that_exits_mid_turn_is_71(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Task\n@write e.txt e\n@exit\n")
    code = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert code == 71
    assert "exited before the turn ended" in err
    run = record.load(_run_id(out), env)
    assert run.status == record.FAILED
    assert run.ended_at is not None
    assert run.turn_state == record.COMMITTED
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == "Task (turn 1, incomplete: engine failure)"


def test_read_only_failed_turn_commits_nothing(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Review\n@write r.txt r\n@fail broke\n")
    assert _start(_core(env), repo, brief, read_only=True) == 71
    assert git(repo, "log", "--format=%s", env=env) == "first"


def test_moved_head_is_76(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(
        tmp_path,
        "Task\n@write x.txt x\n@git commit -q --allow-empty -m own\n",
    )
    code = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert code == 76
    run = record.load(_run_id(out), env)
    assert "nothing committed" in err
    assert err.splitlines()[-1].startswith(f"next: git -C {run.worktree} log")
    assert run.commits == ()
    assert run.status == record.WAITING
    assert run.turn_state == record.COLLECTED


def test_hook_refusal_is_77(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused by hook >&2\nexit 1\n")
    hook.chmod(0o755)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n@write y.txt y\n"))
    out, err = capsys.readouterr()
    assert code == 77
    assert "refused by hook" in err
    run = record.load(_run_id(out), env)
    assert run.commits == ()
    assert run.last_exit == 77


def test_ctrl_c_while_waiting_is_74_and_turn_keeps_running(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def stop(run: record.Run) -> None:
        raise KeyboardInterrupt

    driver = _driver(env, on_wait=stop)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, err = capsys.readouterr()
    assert code == 74
    run_id = _run_id(out)
    assert err.splitlines()[-1].startswith(f"next: emb run --recover {run_id}")
    run = record.load(run_id, env)
    assert run.status == record.RUNNING
    assert run.last_exit == 74


def test_ctrl_c_while_committing_is_74_and_recover_finishes(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stop(run: record.Run) -> object:
        raise KeyboardInterrupt

    core_ = _core(env)
    monkeypatch.setattr(core_, "_commit", stop)
    code = _start(core_, repo, _brief(tmp_path, "Task\n@write k.txt k\n"))
    out, err = capsys.readouterr()
    assert code == 74
    run_id = _run_id(out)
    assert err.splitlines()[-1] == (
        f"next: emb run --recover {run_id} (finishes the saved turn)"
    )
    run = record.load(run_id, env)
    assert run.turn_state == record.COLLECTED
    assert run.last_exit == 74
    assert _core(env).execute(lambda: _core(env).recover(run_id)) == 0
    run = record.load(run_id, env)
    assert run.turn_state == record.COMMITTED
    assert len(run.commits) == 1


def test_driver_error_after_forced_close_is_72(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[int] = []

    def close_then_fail(run: record.Run) -> None:
        calls.append(1)
        if len(calls) == 2:
            _core(env).close(run.id, force=True)
            raise engines.DriverError("the session's files are gone")

    driver = _driver(env, on_wait=close_then_fail)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, err = capsys.readouterr()
    assert code == 72
    assert "was closed" in err
    assert record.load(_run_id(out), env).status == record.CLOSED


def test_close_while_waiting_stops_the_waiting_command_with_72(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[int] = []

    def close_once(run: record.Run) -> None:
        calls.append(1)
        if len(calls) == 2:
            _core(env).close(run.id, force=True)

    driver = _driver(env, on_wait=close_once)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, err = capsys.readouterr()
    assert code == 72
    run = record.load(_run_id(out), env)
    assert run.status == record.CLOSED
    assert "was closed" in err


# Start refusals


def test_missing_files_are_66(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _start(_core(env), repo, str(tmp_path / "none.md")) == 66
    brief = _brief(tmp_path, "Task\n")
    assert _start(_core(env), repo, brief, read=(str(tmp_path / "gone"),)) == 66
    assert _start(_core(env), tmp_path / "nowhere", brief) == 66
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert not paths.runs_dir(env).exists()


def test_not_a_checkout_is_65(
    tmp_path: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    assert _start(_core(env), folder, _brief(tmp_path, "Task\n")) == 65
    assert "not in a git checkout" in capsys.readouterr().err


def test_empty_brief_is_64(tmp_path: Path, env: dict[str, str], repo: Path) -> None:
    assert _start(_core(env), repo, _brief(tmp_path, "\n  \n")) == 64


@pytest.mark.parametrize(
    ("settings", "options", "needle"),
    [
        ({"engine.claude": "off"}, {}, "claude is switched off"),
        ({"single.writer": "ask"}, {}, "menu row single.writer is ask"),
        ({"single.writer": "codex:gpt-5"}, {}, "names codex, not claude"),
        ({"single.writer_as": "subagent"}, {}, "runs as a subagent inside Claude"),
        ({}, {"effort": "extreme"}, "does not take the effort 'extreme'"),
        ({}, {"claim": "never-claimed"}, "claim never-claimed is unknown"),
        ({}, {"claim": "../bad"}, "is not a valid id"),
    ],
)
def test_selection_refusals_are_64(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    settings: dict[str, object],
    options: dict[str, object],
    needle: str,
) -> None:
    if settings:
        choices.set_settings(settings, env=env)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n"), **options) == 64
    assert needle in capsys.readouterr().err
    assert not paths.runs_dir(env).exists()


def test_switched_off_engine_named_explicitly_is_64(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"engine.claude": "off"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), model="opus")
    assert code == 64
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        "owner: switch claude on: emb choices --set engine.claude=on"
    )


def test_claimed_choice_selects_the_model(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"shortlist.claude": ["opus@high", "haiku@low"]}, env=env)
    choices.set_once({"single.writer": "claude:haiku"}, env=env)
    choices.claim("run-claim-1", env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), claim="run-claim-1")
    out, _ = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert (run.model, run.effort, run.claim) == ("haiku", "low", "run-claim-1")


def test_disconnected_engine_is_69_before_menu_row(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine_cache(env, {"claude": False})
    choices.set_settings({"single.writer": "ask"}, env=env)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 69
    err = capsys.readouterr().err
    assert "claude is not connected: signed out" in err
    assert err.splitlines()[-1] == "owner: claude login"


def test_missing_status_cache_is_refreshed_once(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Path(env["XDG_CACHE_HOME"], "ember", "engines.json").unlink()
    bin_dir = Path(env["PATH"].split(":")[0])
    for name in ("claude", "codex", "opencode"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 69
    assert Path(env["XDG_CACHE_HOME"], "ember", "engines.json").exists()
    assert "claude is not connected" in capsys.readouterr().err


def test_missing_driver_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(name: str) -> Driver:
        raise engines.DriverMissingError(f"Ember has no driver for {name}")

    core_ = Core(env, load_driver=missing)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 69
    err = capsys.readouterr().err
    assert "Ember has no driver for claude" in err
    assert err.splitlines()[-1] == (
        "owner: this Ember build has no driver for claude; update Ember"
    )


def test_subagent_row_is_ignored_with_explicit_model(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"single.writer_as": "subagent"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), model="opus")
    out, err = capsys.readouterr()
    assert code == 0, err
    run = record.load(_run_id(out), env)
    # The row's effort is high; the driver's default is medium.
    assert (run.model, run.effort) == ("opus", "medium")


@pytest.mark.parametrize(
    ("error", "code", "last"),
    [
        (
            paths.RuntimeDirError("XDG_RUNTIME_DIR is unset"),
            69,
            "owner: set XDG_RUNTIME_DIR to a private folder of this user "
            "(the message names the problem)",
        ),
        (
            sandbox.SandboxError("no bwrap"),
            69,
            "owner: install or connect claude; emb engines shows what is missing",
        ),
        (OSError("disk full"), 1, "next: emb run --status"),
        (ValueError("bad"), 1, "next: emb run --status"),
    ],
)
def test_start_failure_after_the_run_folder_exists_is_recorded(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
    last: str,
) -> None:
    class Raising(StubDriver):
        def command(self, run: record.Run, first_message: str) -> LaunchSpec:
            raise error

    driver = Raising("claude", script_path(env))
    assert _start(_core(env, driver), repo, _brief(tmp_path, "Task\n")) == code
    out, err = capsys.readouterr()
    assert err.splitlines()[-1] == last
    run = record.load(_run_id(out), env)
    assert run.status == record.FAILED
    assert run.last_exit == code
    assert str(error) in (run.last_reason or "")


def test_failed_start_calls_driver_close_and_keeps_the_start_error(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    closed: list[str] = []

    class Raising(StubDriver):
        def command(self, run: record.Run, first_message: str) -> LaunchSpec:
            raise OSError("disk full")

        def close(self, run: record.Run) -> None:
            closed.append(run.id)
            raise RuntimeError("clean-up failed too")

    driver = Raising("claude", script_path(env))
    assert _start(_core(env, driver), repo, _brief(tmp_path, "Task\n")) == 1
    out = capsys.readouterr().out
    run_id = _run_id(out)
    assert closed == [run_id]
    assert "disk full" in (record.load(run_id, env).last_reason or "")


def test_settings_database_error_is_1(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = paths.settings_db_path(env)
    path.unlink()
    path.write_bytes(b"not a database at all, just bytes" * 10)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 1
    err = capsys.readouterr().err
    assert "settings database" in err
    assert err.splitlines()[-1].startswith("owner: ")


def test_repository_without_main_checkout_is_70(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare), env=env)
    linked = tmp_path / "linked"
    git(bare, "worktree", "add", "-q", str(linked), "main", env=env)
    assert _start(_core(env), linked, _brief(tmp_path, "Task\n")) == 70
    assert "has no main checkout" in capsys.readouterr().err
    assert not paths.runs_dir(env).exists()


def test_run_from_linked_worktree_uses_main_checkout(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked), env=env)
    code = _start(_core(env), linked / ".", _brief(tmp_path, "Task\n"))
    out, err = capsys.readouterr()
    assert code == 0, err
    run = record.load(_run_id(out), env)
    assert run.repo == str(linked)
    assert run.worktree == str(repo / ".ember" / "worktrees" / run.id)


def test_tmux_missing_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (Path(env["PATH"].split(":")[0]) / "tmux").unlink()
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 69
    assert capsys.readouterr().err.splitlines()[-1] == "owner: install tmux"


def test_engine_that_exits_before_ready_is_71(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken(StubDriver):
        def command(self, run: record.Run, first_message: str) -> LaunchSpec:
            return LaunchSpec(
                argv=("false",), cwd=Path(run.worktree), first_message="x"
            )

    code = _start(
        _core(env, Broken("claude", script_path(env))), repo, _brief(tmp_path, "T\n")
    )
    out, err = capsys.readouterr()
    assert code == 71
    assert "exited before it took its brief" in err
    run = record.load(_run_id(out), env)
    assert run.status == record.FAILED
    assert run.turn == 0


# Sandbox


def test_sandbox_none_warns_and_is_recorded(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    core_ = _core(env, _driver(env, "codex"))
    code = _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine="codex", model="gpt-5"
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "WARNING: codex runs without a sandbox" in err
    run = record.load(_run_id(out), env)
    assert run.sandbox == "none"
    assert (run.model, run.effort) == ("gpt-5", "medium")


def test_claude_sandbox_off_warns(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "WARNING: claude runs without a sandbox" in err
    assert record.load(_run_id(out), env).sandbox == "none"


@pytest.mark.parametrize(("engine", "model"), [("claude", "opus"), ("codex", "gpt-5")])
def test_sandbox_on_without_bwrap_is_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    engine: str,
    model: str,
) -> None:
    choices.set_settings({f"sandbox.{engine}": "on"}, env=env)
    core_ = _core(env, _driver(env, engine))
    code = _start(core_, repo, _brief(tmp_path, "Task\n"), engine=engine, model=model)
    assert code == 69
    err = capsys.readouterr().err
    assert "bwrap is not" in err
    assert err.splitlines()[-1] == (
        "owner: install bubblewrap (the bwrap command) or make it work, "
        f"or set sandbox.{engine} to off"
    )
    assert not paths.runs_dir(env).exists()


def test_sandbox_on_with_a_blocked_bwrap_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bwrap = Path(env["PATH"].split(":")[0]) / "bwrap"
    bwrap.write_text(
        "#!/bin/sh\necho 'bwrap: setting up uid map: denied' >&2\nexit 1\n"
    )
    bwrap.chmod(0o755)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    assert code == 69
    err = capsys.readouterr().err
    assert "setting up uid map: denied" in err
    assert err.splitlines()[-1].endswith("or set sandbox.claude to off")
    assert not paths.runs_dir(env).exists()


def test_claude_run_records_its_sandbox_network_and_read_paths(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    shared = tmp_path / "shared"
    shared.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    choices.set_settings(
        {
            "sandbox.claude": "on",
            "worker.network": "on",
            "sandbox.read": [str(shared)],
        },
        env=env,
    )
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), read=(str(extra),))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "WARNING" not in err
    run = record.load(_run_id(out), env)
    assert run.sandbox == "bubblewrap"
    assert run.network is True
    assert run.read == (str(extra),)
    assert run.read_effective == (str(shared), str(extra))


def test_codex_sandbox_conflict_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('sandbox_mode = "danger"\n')
    core_ = _core(env, _driver(env, "codex"))
    code = _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine="codex", model="gpt-5"
    )
    assert code == 69
    err = capsys.readouterr().err
    assert str(repo / ".codex" / "config.toml") in err
    assert err.splitlines()[-1].startswith("owner: remove the sandbox_mode key from ")
    assert not paths.runs_dir(env).exists()


def test_codex_sandbox_conflict_in_the_new_worktree_is_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('sandbox_mode = "danger"\n')
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", "codex config", env=env)
    # The main checkout no longer has the file; the commit still does.
    (repo / ".codex" / "config.toml").unlink()
    core_ = _core(env, _driver(env, "codex"))
    code = _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine="codex", model="gpt-5"
    )
    out, err = capsys.readouterr()
    assert code == 69
    run = record.load(_run_id(out), env)
    conflict = Path(run.worktree) / ".codex" / "config.toml"
    assert str(conflict) in err
    assert err.splitlines()[-1].startswith("owner: remove the sandbox_mode key from ")
    assert run.status == record.FAILED
    assert conflict.exists()
    assert git(repo, "branch", "--list", run.branch or "", env=env)


def _bare_run(
    env: dict[str, str], tmp_path: Path, engine: str, provider: str
) -> record.Run:
    return record.Run(
        id=f"{engine}-20260101-000000-abcdef",
        engine=engine,
        model="m",
        effort=None,
        read_only=False,
        repo=str(tmp_path),
        worktree=str(tmp_path / "wt"),
        branch="b",
        base="0" * 40,
        sandbox=provider,
        network=False,
        read=(str(tmp_path / "extra"),),
        read_effective=(str(tmp_path / "setting"), str(tmp_path / "extra")),
        trailers=(),
        claim=None,
        status=record.STARTING,
        turn=0,
        turn_state=None,
        reply=None,
        expected_head=None,
        started_at=1.0,
        updated_at=1.0,
        ended_at=None,
        last_exit=None,
        last_reason=None,
        commits=(),
        subject="s",
        folder=tmp_path,
        env=env,
    )


def test_codex_command_gets_the_permission_profile(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "setting").mkdir()
    monkeypatch.setattr(sandbox, "codex_bwrap", lambda binary, env=None: ("b", None))
    run = replace(_bare_run(env, tmp_path, "codex", "codex"), network=True)
    spec = LaunchSpec(argv=("codex", "--model", "m"), cwd=tmp_path / "wt")
    wrapped = _core(env)._sandboxed(spec, run, tmp_path / ".git")
    assert wrapped.argv[0] == "codex"
    assert wrapped.argv[1] == "-c"
    assert wrapped.argv[-2:] == ("--model", "m")
    assert any("default_permissions" in arg for arg in wrapped.argv)
    assert "permissions.ember.network.enabled=true" in wrapped.argv
    assert "PATH" not in wrapped.env


def test_codex_without_a_system_bwrap_uses_the_bundled_one(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wt").mkdir()
    folder = tmp_path / "bundled"
    monkeypatch.setattr(sandbox, "codex_bwrap", lambda binary, env=None: ("b", folder))
    run = _bare_run(env, tmp_path, "codex", "codex")
    spec = LaunchSpec(argv=("codex",), cwd=tmp_path / "wt")
    wrapped = _core(env)._sandboxed(spec, run, tmp_path / ".git")
    assert wrapped.env["PATH"] == f"{folder}:{env['PATH']}"


def test_opencode_command_runs_inside_bwrap(
    tmp_path: Path, env: dict[str, str]
) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "extra").mkdir()
    run = _bare_run(env, tmp_path, "opencode", "bubblewrap")
    spec = LaunchSpec(argv=("opencode", "run"), cwd=tmp_path / "wt")
    wrapped = _core(env)._sandboxed(spec, run, tmp_path / ".git")
    assert wrapped.argv[0] == "bwrap"
    assert wrapped.argv[-2:] == ("opencode", "run")
    extra = str(tmp_path / "extra")
    assert ("--ro-bind", extra, extra) in zip(
        wrapped.argv, wrapped.argv[1:], wrapped.argv[2:], strict=False
    )


@pytest.mark.parametrize(
    ("engine", "provider"), [("claude", "none"), ("claude", "bubblewrap")]
)
def test_claude_and_no_sandbox_leave_the_command(
    tmp_path: Path, env: dict[str, str], engine: str, provider: str
) -> None:
    run = _bare_run(env, tmp_path, engine, provider)
    spec = LaunchSpec(argv=("claude",), cwd=tmp_path)
    assert _core(env)._sandboxed(spec, run, tmp_path) == spec
    codex = _bare_run(env, tmp_path, "codex", "none")
    assert _core(env)._sandboxed(spec, codex, tmp_path) == spec


# Commands on a run


def _started(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    text: str = "Task\n",
    driver: Driver | None = None,
) -> str:
    code = _start(_core(env, driver), repo, _brief(tmp_path, text))
    out, err = capsys.readouterr()
    assert code == 0, err
    return _run_id(out)


def test_second_command_on_a_run_is_75(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    holder = _core(env)
    with holder._turn_lock(run_id):
        core_ = _core(env)
        assert core_.execute(lambda: core_.recover(run_id)) == 75
        message = _brief(tmp_path, "more\n", "m.md")
        assert core_.execute(lambda: core_.send(run_id, message)) == 75
        assert core_.execute(lambda: core_.close(run_id)) == 75
        assert core_.execute(lambda: core_.status(run_id)) == 0
        assert core_.execute(lambda: core_.screen(run_id)) == 0
    err = capsys.readouterr().err
    assert f"another emb run command is working on {run_id}" in err


def test_send_while_busy_is_75(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env, idle=engines.BUSY)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    message = _brief(tmp_path, "more\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith(f"next: emb run --recover {run_id}")


def test_send_to_dead_session_is_72(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    kill_sessions(tmp_path)
    folder = session_folder(tmp_path, run_id)
    deadline = time.monotonic() + 5
    while (Path("/proc") / (folder / "pid").read_text()).exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    message = _brief(tmp_path, "more\n", "m.md")
    core_ = _core(env)
    assert core_.execute(lambda: core_.send(run_id, message)) == 72
    err = capsys.readouterr().err
    assert "has ended" in err
    status_out = _status(env, run_id, capsys)
    assert status_out["session_state"] == "dead"


class _SendFails(StubDriver):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self.failure: BaseException | None = None

    def send(self, run: record.Run, text: str) -> None:
        if self.failure is not None:
            raise self.failure
        super().send(run, text)


@pytest.mark.parametrize(
    ("failure", "code"),
    [(engines.DriverError("paste failed"), 71), (KeyboardInterrupt(), 74)],
)
def test_send_that_fails_before_delivery_restores_the_turn(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    code: int,
) -> None:
    driver = _SendFails("claude", script_path(env))
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    before = record.load(run_id, env)
    driver.failure = failure
    message = _brief(tmp_path, "@write n.txt n\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message)) == code
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        f"next: emb run --send {run_id} --message-file <file> "
        "(the message was not sent)"
    )
    run = record.load(run_id, env)
    assert (run.status, run.turn, run.turn_state, run.reply, run.expected_head) == (
        before.status,
        before.turn,
        before.turn_state,
        before.reply,
        before.expected_head,
    )
    assert run.last_exit == code
    driver.failure = None
    assert core_.execute(lambda: core_.send(run_id, message)) == 0
    assert record.load(run_id, env).turn == 2


def test_send_first_commits_a_collected_turn(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    run = record.load(run_id, env)
    run = record.update(run, {"turn_state": record.COLLECTED})
    Path(run.worktree, "left.txt").write_text("left\n")
    message = _brief(tmp_path, "@write next.txt n\n", "m.md")
    core_ = _core(env)
    assert core_.execute(lambda: core_.send(run_id, message)) == 0
    run = record.load(run_id, env)
    assert len(run.commits) == 2
    subjects = git(Path(run.worktree), "log", "--format=%s", env=env).splitlines()
    assert subjects[:2] == ["Task (turn 2)", "Task"]


FOREIGN = "1 message(s) in the session were not sent by Ember"


def test_send_refuses_a_session_holding_foreign_messages_before_committing(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    run = record.update(record.load(run_id, env), {"turn_state": record.COLLECTED})
    Path(run.worktree, "left.txt").write_text("left\n")
    driver.foreign = FOREIGN
    message = _brief(tmp_path, "@write next.txt n\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message)) == 75
    err = capsys.readouterr().err.splitlines()
    assert err[0] == (
        f"emb run: the session of {run_id} holds messages Ember did not send "
        f"({FOREIGN}); Claude asks the owner before going on"
    )
    assert err[1] == (
        f"next: ask the owner about the messages in the session of {run_id}; "
        "with their go-ahead, repeat the command with --force"
    )
    after = record.load(run_id, env)
    assert (after.turn, after.turn_state, after.commits) == (
        1,
        record.COLLECTED,
        run.commits,
    )
    assert core_.execute(lambda: core_.send(run_id, message, force=True)) == 0
    after = record.load(run_id, env)
    assert after.turn == 2
    assert len(after.commits) == 2


def test_send_with_force_keeps_the_busy_check(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env, idle=engines.BUSY, foreign=FOREIGN)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    message = _brief(tmp_path, "more\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message, force=True)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith(f"next: emb run --recover {run_id}")
    assert driver.foreign_checks == 0


def test_close_refuses_a_session_holding_foreign_messages_unless_forced(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env, foreign=FOREIGN)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.close(run_id)) == 75
    assert "repeat the command with --force" in capsys.readouterr().err
    assert record.load(run_id, env).status != record.CLOSED
    assert driver.closed == []
    assert core_.execute(lambda: core_.close(run_id, force=True)) == 0
    assert record.load(run_id, env).status == record.CLOSED


def test_recover_adopts_a_commit_it_made_but_did_not_record(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    run = record.update(record.load(run_id, env), {"turn_state": record.COLLECTED})
    worktree = Path(run.worktree)
    (worktree / "z.txt").write_text("z\n")
    git(worktree, "add", "-A", env=env)
    git(worktree, "commit", "-q", "-m", "Task", env=env)
    head = git(worktree, "rev-parse", "HEAD", env=env)
    core_ = _core(env)
    assert core_.execute(lambda: core_.recover(run_id)) == 0
    run = record.load(run_id, env)
    assert run.commits == (head,)
    assert run.turn_state == record.COMMITTED
    assert " · 1 files · turn 1" in capsys.readouterr().out


def test_recover_with_nothing_to_collect(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    core_ = _core(env)
    assert core_.execute(lambda: core_.recover(run_id)) == 0
    assert "nothing to recover" in capsys.readouterr().err


def test_unknown_run_is_64(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    core_ = _core(env)
    assert core_.execute(lambda: core_.status("claude-none-000000")) == 64
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "emb run: no run claude-none-000000",
        "next: emb run --status",
    ]


def test_screen_interrupt_and_attach(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"terminal": "true"}, env=env)
    run_id = _started(tmp_path, env, repo, capsys)
    core_ = _core(env)
    assert core_.execute(lambda: core_.screen(run_id)) == 0
    assert capsys.readouterr().out == "turn 1 done\n"
    assert core_.execute(lambda: core_.interrupt(run_id)) == 0
    assert _events(tmp_path, run_id)[-1] == {"key": "C-c"}
    assert core_.execute(lambda: core_.attach(run_id)) == 0
    assert capsys.readouterr().out.startswith("window ")


def test_close_keeps_worktree_and_branch(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.close(run_id)) == 0
    assert capsys.readouterr().out == f"closed {run_id}\n"
    run = record.load(run_id, env)
    assert run.status == record.CLOSED
    assert run.ended_at is not None
    assert driver.closed == [run_id]
    assert not session_folder(tmp_path, run_id).exists()
    assert Path(run.worktree).is_dir()
    assert git(repo, "branch", "--list", run.branch or "", env=env)
    assert core_.execute(lambda: core_.close(run_id)) == 0
    message = _brief(tmp_path, "more\n", "m.md")
    assert core_.execute(lambda: core_.send(run_id, message)) == 72


def test_close_refused_while_a_turn_is_collected_but_not_committed(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    run = record.update(record.load(run_id, env), {"turn_state": record.COLLECTED})
    Path(run.worktree, "late.txt").write_text("late\n")
    core_ = _core(env)
    assert core_.execute(lambda: core_.close(run_id)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        f"next: emb run --recover {run_id} (commits the collected turn), "
        f"then emb run --close {run_id}"
    )
    assert core_.execute(lambda: core_.recover(run_id)) == 0
    assert len(record.load(run_id, env).commits) == 1
    assert core_.execute(lambda: core_.close(run_id)) == 0
    assert record.load(run_id, env).status == record.CLOSED


def test_close_refused_while_turn_runs_unless_forced(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _start(_core(env, stall=0.01), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, _ = capsys.readouterr()
    assert code == 124
    run_id = _run_id(out)
    core_ = _core(env)
    assert core_.execute(lambda: core_.close(run_id)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1].endswith(f"or emb run --close {run_id} --force")
    assert core_.execute(lambda: core_.close(run_id, force=True)) == 0
    assert record.load(run_id, env).status == record.CLOSED


def _status(
    env: dict[str, str], run_id: str | None, capsys: pytest.CaptureFixture[str]
) -> Any:  # noqa: ANN401
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(run_id, as_json=True)) == 0
    return json.loads(capsys.readouterr().out)


def test_status_one_and_all(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    one = _status(env, run_id, capsys)
    assert one["id"] == run_id
    assert one["session_state"] == "live"
    assert one["status"] == "waiting"
    broken = paths.runs_dir(env) / "claude-broken-000000"
    broken.mkdir()
    (broken / "run.json").write_text("{")
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(None, as_json=True)) == 0
    out, err = capsys.readouterr()
    listing = json.loads(out)
    assert [item["id"] for item in listing] == [run_id]
    assert "skipped claude-broken-000000: run.json is not valid JSON" in err
    _core(env).close(run_id, force=True)
    capsys.readouterr()
    assert _status(env, run_id, capsys)["session_state"] == "missing"


def test_status_text(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(None)) == 0
    assert capsys.readouterr().out == f"{run_id}  waiting  live  turn 1  Task\n"
    assert core_.execute(lambda: core_.status(run_id)) == 0
    out = capsys.readouterr().out
    assert f"id: {run_id}\n" in out
    assert "session_state: live\n" in out


def test_run_error_carries_context() -> None:
    error = RunError(75, "busy", "close", run="r")
    assert (error.code, str(error), error.variant, error.context) == (
        75,
        "busy",
        "close",
        {"run": "r"},
    )


def test_subject_helpers() -> None:
    assert core.brief_subject("\n  \nFirst line  \nsecond") == "First line"
    assert core.brief_subject("") == ""
    assert core.commit_subject("a" * 80, 1) == "a" * 72
    assert core.commit_subject("abc", 3) == "abc (turn 3)"
    assert core.commit_subject("a" * 80, 1, "no reply") == (
        "a" * 72 + " (turn 1, incomplete: no reply)"
    )
    done = engines.TurnOutcome(engines.DONE, reply="ok")
    assert core.incomplete_reason(done) is None
    assert core.incomplete_reason(replace(done, reply="")) == "no reply"
    failed = engines.TurnOutcome(engines.FAILED, signed_out=True)
    assert core.incomplete_reason(failed) == "sign-in failure"
    assert core.first_message("r", "brief") == (
        "Brief from Claude, the lead, for Ember run r.\n\nbrief"
    )
