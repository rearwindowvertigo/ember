import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ember import choices, engines
from ember.engines import DriverError, claude, claude_host
from ember.engines.claude import ClaudeDriver, final_reply, turn_files
from ember.runs.record import Run

# A stub `claude -p`: one turn as stream-json on standard output. The prompt
# picks the outcome; every call is logged with its argv, prompt and variables.
CLAUDE_STUB = r"""#!{python}
import json, os, signal, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "prompt": prompt, "cwd": os.getcwd(),
                          "env": sorted(os.environ)}}) + "\n")
session = args[args.index("--resume") + 1] if "--resume" in args else "session-1"
def emit(event):
    print(json.dumps(event), flush=True)
emit({{"type": "system", "subtype": "init", "session_id": session, "model": "m"}})
if "@crash" in prompt:
    sys.stderr.write("Error: something broke\n")
    sys.exit(3)
if "@sleep" in prompt:
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    open(os.environ["STUB_LOG"] + ".sleeping", "w").close()
    time.sleep(30)
if "@signedout" in prompt:
    emit({{"type": "assistant", "error": "authentication_failed", "message": {{
        "content": [{{"type": "text", "text": "Not logged in"}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": True,
          "result": "Not logged in · Please run /login", "session_id": session}})
    sys.exit(1)
emit({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "Write", "input": {{"file_path": "a.txt"}}}},
    {{"type": "text", "text": "Stub answer."}}]}}}})
emit({{"type": "result", "subtype": "success", "is_error": False,
      "result": "Stub answer.", "session_id": session, "duration_ms": 1200}})
"""

WAIT_SECONDS = 20


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(CLAUDE_STUB.format(python=sys.executable))
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "STUB_LOG": str(tmp_path / "claude.jsonl"),
    }


def make_run(
    tmp_path: Path,
    env: dict[str, str],
    *,
    read_only: bool = False,
    effort: str | None = "high",
    turn: int = 0,
    network: bool = False,
    sandbox: str = "none",
    read_effective: tuple[str, ...] = (),
) -> Run:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    run_folder = tmp_path / "runs" / "claude-20261101-000000-abcdef"
    run_folder.mkdir(parents=True, exist_ok=True)
    return Run(
        id="claude-20261101-000000-abcdef",
        engine="claude",
        model="opus",
        effort=effort,
        read_only=read_only,
        repo=str(worktree),
        worktree=str(worktree),
        branch=None if read_only else "ember/claude/claude-20261101-000000-abcdef",
        base=None,
        sandbox=sandbox,
        network=network,
        read=(),
        read_effective=read_effective,
        trailers=(),
        claim=None,
        status="starting",
        turn=turn,
        turn_state=None,
        reply=None,
        expected_head=None,
        started_at=0.0,
        updated_at=0.0,
        ended_at=None,
        last_exit=None,
        last_reason=None,
        commits=(),
        subject="Do it",
        folder=run_folder,
        env=env,
    )


def with_turn(run: Run, turn: int) -> Run:
    return dataclasses.replace(run, turn=turn)


def request_argv(run: Run, turn: int) -> list[str]:
    files = turn_files(claude.folder(run), turn)
    return json.loads(files["json"].read_text())["argv"]


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def write_turn(
    run: Run,
    turn: int,
    events: list[dict[str, Any]],
    code: int | None,
    *,
    stderr: str = "",
    interrupted: bool = False,
) -> None:
    base = claude.folder(run)
    base.mkdir(parents=True, exist_ok=True)
    files = turn_files(base, turn)
    files["json"].write_text(json.dumps({"turn": turn, "argv": ["claude"]}))
    files["jsonl"].write_text("".join(json.dumps(event) + "\n" for event in events))
    files["stderr"].write_text(stderr)
    if interrupted:
        files["interrupt"].write_text("interrupted\n")
    if code is not None:
        files["exit"].write_text(json.dumps({"code": code}))


def text(value: str) -> dict[str, str]:
    return {"type": "text", "text": value}


def assistant(*blocks: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {"content": list(blocks)},
    }


def user(*blocks: dict[str, Any], **extra: object) -> dict[str, Any]:
    return {
        "type": "user",
        "parent_tool_use_id": None,
        "message": {"content": list(blocks)},
        **extra,
    }


def result(value: str, *, error: bool = False, session: str = "s1") -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": error,
        "result": value,
        "session_id": session,
    }


# The factory and efforts


def test_load_driver_returns_the_claude_driver(env: dict[str, str]) -> None:
    driver = engines.load_driver("claude", env)
    assert isinstance(driver, ClaudeDriver)
    assert driver.name == "claude"
    assert driver.env == env


def test_efforts(env: dict[str, str]) -> None:
    driver = claude.driver(env)
    for effort in ("low", "medium", "high", "xhigh", "max"):
        assert driver.valid_effort(effort)
    assert not driver.valid_effort("minimal")
    assert not driver.valid_effort("default")
    assert driver.default_effort("opus") is None


# Commands


def test_command_requests_turn_one_and_runs_the_host(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(tmp_path, env)
    spec = claude.driver(env).command(run, "Brief line\n\nDo it")
    base = claude.folder(run)
    assert spec.argv == (
        sys.executable,
        "-P",
        "-m",
        "ember.engines.claude_host",
        str(base),
        run.worktree,
        "1",
    )
    assert spec.cwd == Path(run.worktree)
    assert spec.first_message is None
    assert dict(spec.env) == {}
    assert base.stat().st_mode & 0o777 == 0o700
    files = turn_files(base, 1)
    request = json.loads(files["json"].read_text())
    assert request["turn"] == 1
    assert request["prompt"] == str(files["prompt"])
    assert files["prompt"].read_text() == "Brief line\n\nDo it\n"
    assert not list(base.glob("*.tmp"))
    argv = request["argv"]
    assert argv[0] == str(tmp_path / "bin" / "claude")
    assert argv[1:5] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert option(argv, "--model") == "opus"
    assert option(argv, "--effort") == "high"
    for flag in ("--restricted", "--safe-mode", "--strict-mcp-config"):
        assert flag in argv
    assert option(argv, "--tools") == "Bash,Read,Edit,Write,Glob,Grep"
    assert option(argv, "--allowedTools") == "Bash,Read,Edit,Write,Glob,Grep"
    disallowed = option(argv, "--disallowedTools").split(",")
    assert "Bash(git commit:*)" in disallowed
    assert "Bash(git push:*)" in disallowed
    assert option(argv, "--permission-mode") == "acceptEdits"
    assert option(argv, "--permission-prompts") == "none"
    assert "--resume" not in argv
    assert not (run.folder / "turn-1.json").exists()


def test_read_only_run_gets_only_the_read_tools(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(tmp_path, env, read_only=True, effort=None)
    claude.driver(env).command(run, "Look")
    argv = request_argv(run, 1)
    assert option(argv, "--tools") == "Read,Glob,Grep"
    assert option(argv, "--allowedTools") == "Read,Glob,Grep"
    assert option(argv, "--permission-mode") == "default"
    assert "--effort" not in argv


def test_stored_network_adds_the_web_tools(tmp_path: Path, env: dict[str, str]) -> None:
    # The setting changed after the start; the run keeps its stored value.
    choices.set_settings({"worker.network": "off"}, env=env)
    run = make_run(tmp_path, env, network=True)
    claude.driver(env).command(run, "Brief")
    assert option(request_argv(run, 1), "--tools").endswith(",WebSearch,WebFetch")


def test_read_paths_are_added_and_kept_from_edits(
    tmp_path: Path, env: dict[str, str]
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    notes = tmp_path / "notes" / "plan.md"
    notes.parent.mkdir()
    notes.write_text("plan\n")
    run = make_run(tmp_path, env)
    inside = Path(run.worktree) / "docs"
    run = dataclasses.replace(
        run, read_effective=(str(shared), str(notes), str(inside), str(tmp_path))
    )
    claude.driver(env).command(run, "Brief")
    argv = request_argv(run, 1)
    added = [argv[index + 1] for index, item in enumerate(argv) if item == "--add-dir"]
    assert added == [str(shared), str(notes.parent)]
    disallowed = option(argv, "--disallowedTools").split(",")
    assert f"Edit(/{shared}/**)" in disallowed
    assert f"Edit(/{notes.parent}/**)" in disallowed
    assert not any(str(inside) in rule for rule in disallowed)


def test_request_names_no_sandbox_without_bubblewrap(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(tmp_path, env)
    claude.driver(env).command(run, "Brief")
    request = json.loads(turn_files(claude.folder(run), 1)["json"].read_text())
    assert request["sandbox"] is None


def test_request_names_the_sandbox_with_bubblewrap(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(
        tmp_path,
        env,
        sandbox="bubblewrap",
        network=True,
        read_effective=("/opt/data",),
    )
    subprocess.run(
        ["git", "init", "-q"], cwd=run.worktree, check=True, capture_output=True
    )
    claude.driver(env).command(run, "Brief")
    request = json.loads(turn_files(claude.folder(run), 1)["json"].read_text())
    assert request["sandbox"] == {
        "worktree": run.worktree,
        "common": str(Path(run.worktree).resolve() / ".git"),
        "read": ["/opt/data"],
        "read_only": False,
        "network": True,
    }


def test_sandboxed_request_outside_a_repository_is_a_driver_error(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(tmp_path, env, sandbox="bubblewrap")
    with pytest.raises(DriverError, match="not a git repository"):
        claude.driver(env).command(run, "Brief")


def test_missing_cli_is_a_driver_error(tmp_path: Path, env: dict[str, str]) -> None:
    env["PATH"] = str(tmp_path / "empty")
    run = make_run(tmp_path, env)
    with pytest.raises(DriverError, match="not on PATH"):
        claude.driver(env).command(run, "Brief")
    assert not turn_files(claude.folder(run), 1)["json"].exists()


def test_send_resumes_the_session_of_the_newest_earlier_turn(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    driver.command(run, "Brief")
    write_turn(run, 1, [{"type": "system", "session_id": "s-init"}, result("ok")], 0)
    driver.send(with_turn(run, 2), "Fix it")
    assert option(request_argv(run, 2), "--resume") == "s1"
    assert turn_files(claude.folder(run), 2)["prompt"].read_text() == "Fix it\n"
    # A turn that reported no session leaves the earlier one in use.
    write_turn(run, 2, [], 1)
    driver.send(with_turn(run, 3), "Again")
    assert option(request_argv(run, 3), "--resume") == "s1"


def test_send_of_a_requested_turn_is_refused(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    driver.command(run, "Brief")
    with pytest.raises(DriverError, match="already requested"):
        driver.send(with_turn(run, 1), "Again")


# Turns


def test_wait_is_running_until_the_exit_file(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [assistant(text("Partial"))], None)
    driver = claude.driver(env)
    assert driver.wait(run).state == engines.RUNNING
    assert driver.idle_state(run).state == engines.BUSY


def test_wait_of_a_turn_never_requested_is_a_driver_error(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    with pytest.raises(DriverError, match="not requested"):
        claude.driver(env).wait(run)


def test_done_turn_gives_the_final_reply(tmp_path: Path, env: dict[str, str]) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    events = [
        assistant(text("Reading first.")),
        assistant({"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
        user({"type": "tool_result", "tool_use_id": "t1", "content": "a"}),
        assistant(text("First part, row 1.\n")),
        user(text("Output token limit hit. Resume directly."), isSynthetic=True),
        assistant(text("Second part.")),
        result("Second part."),
    ]
    write_turn(run, 1, events, 0)
    outcome = claude.driver(env).wait(run)
    assert outcome == engines.TurnOutcome(
        engines.DONE, reply="First part, row 1.\nSecond part."
    )
    assert claude.driver(env).idle_state(run).state == engines.IDLE


def test_done_turn_without_text_uses_the_result_text(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [result("Only here.")], 0)
    assert claude.driver(env).wait(run).reply == "Only here."


def test_empty_reply_is_done_and_empty(tmp_path: Path, env: dict[str, str]) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [result("")], 0)
    assert claude.driver(env).wait(run) == engines.TurnOutcome(engines.DONE, reply="")


def test_provider_error(tmp_path: Path, env: dict[str, str]) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [result("API Error: overloaded", error=True)], 1)
    outcome = claude.driver(env).wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.provider_error
    assert not outcome.signed_out
    assert outcome.reason == "the model server refused the turn: API Error: overloaded"


def test_usage_limit_on_stderr_is_a_provider_error(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [], 1, stderr="Claude usage limit reached\n")
    assert claude.driver(env).wait(run).provider_error


def test_sign_in_failure(tmp_path: Path, env: dict[str, str]) -> None:
    # The events the CLI writes when it has no sign-in (Claude Code 2.1.316).
    run = with_turn(make_run(tmp_path, env), 1)
    events = [
        {"type": "assistant", "error": "authentication_failed", "message": {
            "content": [text("Not logged in · Please run /login")]}},
        result("Not logged in · Please run /login", error=True),
    ]  # fmt: skip
    write_turn(run, 1, events, 1)
    outcome = claude.driver(env).wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.signed_out
    assert not outcome.provider_error
    assert outcome.reason == (
        "Claude Code is not signed in: Not logged in · Please run /login"
    )


def test_cli_error_result_fails_with_its_text(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [result("Something odd", error=True)], 1, stderr="trace\n")
    outcome = claude.driver(env).wait(run)
    assert outcome == engines.TurnOutcome(
        engines.FAILED, reason="claude failed: Something odd trace"
    )


def test_cli_that_exits_without_a_result_fails(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [assistant(text("half"))], 137)
    outcome = claude.driver(env).wait(run)
    assert outcome == engines.TurnOutcome(
        engines.FAILED, reason="claude failed: the CLI exited 137 without a result"
    )


def test_result_with_a_failed_exit_code_fails(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [result("ok")], 2)
    assert claude.driver(env).wait(run).state == engines.FAILED


def test_unreadable_exit_file_fails(tmp_path: Path, env: dict[str, str]) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [result("ok")], None)
    turn_files(claude.folder(run), 1)["exit"].write_text("not json")
    assert claude.driver(env).wait(run).state == engines.FAILED


def test_interrupted_turn_fails(tmp_path: Path, env: dict[str, str]) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [assistant(text("half"))], 130, interrupted=True)
    outcome = claude.driver(env).wait(run)
    assert outcome == engines.TurnOutcome(
        engines.FAILED, reason=f"turn 1 of {run.id} was interrupted"
    )


def test_incomplete_last_line_is_left_for_the_next_read(tmp_path: Path) -> None:
    path = tmp_path / "turn-1.jsonl"
    path.write_text('{"type": "system"}\nnot json\n[1]\n{"type": "res')
    assert claude.read_events(path) == [{"type": "system"}]
    assert claude.read_events(tmp_path / "missing.jsonl") == []


def test_final_reply_spans_messages_cut_at_the_output_limit() -> None:
    events = [
        {"type": "system", "subtype": "init", "session_id": "s"},
        assistant({"type": "thinking", "thinking": ""}),
        assistant(text("I will read the plan.")),
        assistant({"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
        user({"type": "tool_result", "tool_use_id": "t1", "content": "plan"}),
        assistant(text("Now the skill.")),
        assistant({"type": "tool_use", "id": "t2", "name": "Grep", "input": {}}),
        user({"type": "tool_result", "tool_use_id": "t2", "content": "none"}),
        {"type": "system", "subtype": "thinking_tokens"},
        assistant(text("## Files\n\n| 93 | kept |\n| 94 | kept |")),
        user(text("Output token limit hit. Resume directly."), isSynthetic=True),
        assistant(text("| 95 | kept |\n\nNot covered\n- nothing\n")),
        result("| 95 | kept |\n\nNot covered\n- nothing\n"),
    ]
    assert final_reply(events) == (
        "## Files\n\n| 93 | kept |\n| 94 | kept |\n| 95 | kept |\n\n"
        "Not covered\n- nothing"
    )


def test_final_reply_without_tools_keeps_every_text_but_subagents() -> None:
    events = [
        assistant(text("One.")),
        assistant(text("   ")),
        assistant(text("Inside a subagent."), parent="toolu_task"),
        {
            "type": "user",
            "parent_tool_use_id": "toolu_task",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]},
        },
        assistant(text("Two."), text("Three.")),
        {"type": "assistant", "message": {"content": "Four."}},
    ]
    assert final_reply(events) == "One.\n\nTwo.\n\nThree.\n\nFour."
    assert final_reply([]) == ""
    assert (
        final_reply(
            [
                assistant({"type": "tool_use", "id": "t", "name": "Read"}),
                user({"type": "tool_result", "tool_use_id": "t"}),
            ]
        )
        == ""
    )


def test_session_of() -> None:
    assert claude.session_of([{"type": "system", "session_id": "a"}]) == "a"
    assert (
        claude.session_of([{"type": "system", "session_id": "a"}, result("x")]) == "s1"
    )
    assert claude.session_of([assistant(text("x"))]) is None


# Idle state, ready and interrupt


def test_idle_and_ready_before_anything_ran(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    assert driver.idle_state(run).state == engines.IDLE
    assert not driver.ready(run)
    driver.close(run)
    driver.close(run)


def test_interrupt_with_no_running_turn_is_a_driver_error(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [result("ok")], 0)
    with pytest.raises(DriverError, match="no turn is running"):
        claude.driver(env).interrupt(run)


def test_interrupt_without_a_process_is_a_driver_error(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [], None)
    with pytest.raises(DriverError, match="cannot read the process of turn 1"):
        claude.driver(env).interrupt(run)
    assert not turn_files(claude.folder(run), 1)["interrupt"].exists()


def test_interrupt_with_a_damaged_pid_file_marks_nothing(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = with_turn(make_run(tmp_path, env), 1)
    write_turn(run, 1, [], None)
    turn_files(claude.folder(run), 1)["pid"].write_text("not a pid")
    with pytest.raises(DriverError, match="cannot read the process of turn 1"):
        claude.driver(env).interrupt(run)
    assert not turn_files(claude.folder(run), 1)["interrupt"].exists()


# The host program, with the stub CLI


@pytest.fixture
def host_env(env: dict[str, str]) -> dict[str, str]:
    return {
        **env,
        "PYTHONPATH": os.pathsep.join(sys.path),
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "ANTHROPIC_API_KEY": "test-value",
    }


@pytest.fixture
def host() -> Iterator[list[subprocess.Popen]]:
    started: list[subprocess.Popen] = []
    yield started
    for process in started:
        process.kill()
        process.wait()


def start_host(
    run: Run, argv: tuple[str, ...], env: dict[str, str], started: list
) -> subprocess.Popen:
    screen = open(run.folder / "screen.txt", "wb")  # noqa: SIM115
    process = subprocess.Popen(
        argv, cwd=run.worktree, env=env, stdout=screen, stderr=subprocess.STDOUT
    )
    screen.close()
    started.append(process)
    return process


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def calls(env: dict[str, str]) -> list[dict[str, Any]]:
    path = Path(env["STUB_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_host_runs_turns_and_resumes_the_session(
    tmp_path: Path, env: dict[str, str], host_env: dict[str, str], host: list
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    spec = driver.command(run, "Brief")
    start_host(run, spec.argv, host_env, host)
    run = with_turn(run, 1)
    until(lambda: driver.wait(run).state != engines.RUNNING)
    assert driver.ready(run)
    assert driver.wait(run) == engines.TurnOutcome(engines.DONE, reply="Stub answer.")
    assert driver.idle_state(run).state == engines.IDLE
    first = calls(env)[0]
    assert first["prompt"] == "Brief\n"
    assert first["cwd"] == run.worktree
    hidden = [name for name in first["env"] if name.startswith(("CLAUDE", "ANTHROPIC"))]
    assert hidden == []
    assert "STUB_LOG" in first["env"]

    run = with_turn(run, 2)
    driver.send(run, "Next")
    until(lambda: driver.wait(run).state != engines.RUNNING)
    assert driver.wait(run).state == engines.DONE
    second = calls(env)[1]
    assert option(second["args"], "--resume") == "session-1"
    screen = (run.folder / "screen.txt").read_text()
    assert "=== turn 1 ===" in screen
    assert "→ Write: a.txt" in screen
    assert "Stub answer." in screen
    assert "=== turn 2 ===" in screen


def test_host_reports_a_crash_and_a_sign_in_failure(
    tmp_path: Path, env: dict[str, str], host_env: dict[str, str], host: list
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    spec = driver.command(run, "@crash")
    start_host(run, spec.argv, host_env, host)
    run = with_turn(run, 1)
    until(lambda: driver.wait(run).state != engines.RUNNING)
    assert driver.wait(run) == engines.TurnOutcome(
        engines.FAILED, reason="claude failed: Error: something broke"
    )
    run = with_turn(run, 2)
    driver.send(run, "@signedout")
    until(lambda: driver.wait(run).state != engines.RUNNING)
    assert driver.wait(run).signed_out


def test_host_with_a_cli_that_cannot_start(
    tmp_path: Path, env: dict[str, str], host_env: dict[str, str], host: list
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    spec = driver.command(run, "Brief")
    files = turn_files(claude.folder(run), 1)
    request = json.loads(files["json"].read_text())
    request["argv"][0] = str(tmp_path / "missing" / "claude")
    files["json"].write_text(json.dumps(request))
    start_host(run, spec.argv, host_env, host)
    run = with_turn(run, 1)
    until(lambda: driver.wait(run).state != engines.RUNNING)
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.reason is not None
    assert outcome.reason.startswith("claude failed: cannot start claude:")
    assert json.loads(files["exit"].read_text()) == {"code": 127}


def test_interrupt_ends_the_turn_and_the_host_goes_on(
    tmp_path: Path, env: dict[str, str], host_env: dict[str, str], host: list
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    spec = driver.command(run, "@sleep")
    start_host(run, spec.argv, host_env, host)
    run = with_turn(run, 1)
    until(lambda: Path(env["STUB_LOG"] + ".sleeping").exists())
    assert driver.idle_state(run).state == engines.BUSY
    driver.interrupt(run)
    until(lambda: driver.wait(run).state != engines.RUNNING)
    assert driver.wait(run).reason == f"turn 1 of {run.id} was interrupted"
    assert json.loads(turn_files(claude.folder(run), 1)["exit"].read_text()) == {
        "code": 130
    }
    run = with_turn(run, 2)
    driver.send(run, "Next")
    until(lambda: driver.wait(run).state != engines.RUNNING)
    assert driver.wait(run).state == engines.DONE


def test_host_ctrl_c_marks_the_running_turn_interrupted(tmp_path: Path) -> None:
    host = claude_host.Host(tmp_path, str(tmp_path), {})
    host.on_interrupt(signal.SIGINT, None)
    assert not list(tmp_path.iterdir())
    host.running = 3
    host.on_interrupt(signal.SIGINT, None)
    assert (tmp_path / "turn-3.interrupt").exists()


def test_host_with_an_unreadable_request(tmp_path: Path) -> None:
    host = claude_host.Host(tmp_path, str(tmp_path), {})
    (tmp_path / "turn-1.json").write_text("{")
    assert host.run_turn(1) == 127
    assert json.loads((tmp_path / "turn-1.exit").read_text()) == {"code": 127}
    assert "cannot read the request" in (tmp_path / "turn-1.stderr").read_text()


def test_host_wraps_a_sandboxed_turn(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    worktree = tmp_path / "repo"
    (worktree / ".git").mkdir(parents=True)
    outer = {
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "CLAUDECODE": "1",
    }
    host = claude_host.Host(tmp_path, str(worktree), outer)
    argv = ["/usr/bin/claude", "-p"]
    assert host.wrap(argv, None) == argv
    box = {
        "worktree": str(worktree),
        "common": str(worktree / ".git"),
        "read": [],
        "read_only": True,
        "network": False,
    }
    wrapped = host.wrap(argv, box)
    assert wrapped[0] == "bwrap"
    assert wrapped[-2:] == argv
    triples = list(zip(wrapped, wrapped[1:], wrapped[2:], strict=False))
    assert ("--bind", str(home / ".claude"), str(home / ".claude")) in triples
    assert ("--ro-bind", str(worktree), str(worktree)) in triples
    assert "--unshare-net" not in wrapped


def test_host_with_a_damaged_sandbox_request(tmp_path: Path) -> None:
    host = claude_host.Host(tmp_path, str(tmp_path), {"HOME": str(tmp_path)})
    request = {"turn": 1, "prompt": "p", "argv": ["claude"], "sandbox": {"read": []}}
    (tmp_path / "turn-1.json").write_text(json.dumps(request))
    assert host.run_turn(1) == 127
    assert "cannot build the sandbox" in (tmp_path / "turn-1.stderr").read_text()


def test_host_prints_a_status_line_during_a_long_tool_call(
    tmp_path: Path, env: dict[str, str], host_env: dict[str, str], host: list
) -> None:
    run = make_run(tmp_path, env)
    driver = claude.driver(env)
    spec = driver.command(run, "@sleep")
    folder, worktree, turn = spec.argv[-3:]
    code = (
        "import sys; from pathlib import Path; from ember.engines import claude_host;"
        f"claude_host.Host(Path({folder!r}), {worktree!r}, __import__('os').environ,"
        " status_seconds=0.2).serve(1)"
    )
    start_host(run, (sys.executable, "-c", code), host_env, host)
    run = with_turn(run, 1)
    until(lambda: Path(env["STUB_LOG"] + ".sleeping").exists())
    screen = run.folder / "screen.txt"
    until(lambda: "· waiting for the model for " in screen.read_text())
    driver.interrupt(run)
    until(lambda: driver.wait(run).state != engines.RUNNING)


def test_progress_follows_tool_calls() -> None:
    progress = claude_host.Progress(0.0, every=60.0)
    assert progress.line(59.0) is None
    assert progress.line(60.0) == "· waiting for the model for 1 min"
    progress.event(assistant({"type": "tool_use", "name": "Bash", "input": {}}), 70.0)
    assert progress.line(119.0) is None
    progress.shown(100.0)
    assert progress.line(159.0) is None
    assert progress.line(250.0) == "· running Bash for 3 min"
    progress.event(user({"type": "tool_result", "content": "ok"}), 260.0)
    assert progress.line(310.0) == "· waiting for the model for 50 s"
    progress.event(assistant(text("thinking")), 315.0)
    assert progress.line(400.0) == "· waiting for the model for 2 min"
    sub = assistant({"type": "tool_use", "name": "Grep", "input": {}}, parent="t1")
    progress.event(sub, 401.0)
    assert progress.line(500.0) == "· waiting for the model for 4 min"


def test_host_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert claude_host.main(["claude_host", "folder"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_cli_environment_drops_the_hidden_variables() -> None:
    env = {"PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_X": "y", "ANTHROPIC_Z": "w"}
    assert claude_host.cli_environment(env) == {"PATH": "/bin"}


def test_show() -> None:
    show = claude_host.show
    assert show(
        {"type": "system", "subtype": "init", "session_id": "s", "model": "m"}
    ) == ("· session s · m")
    assert show(
        assistant(
            text("Hello\n"),
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls\nmore"}},
            {"type": "tool_use", "name": "Read", "input": None},
        )
    ) == ("Hello\n→ Bash: ls\n→ Read: ")
    error = {"type": "tool_result", "is_error": True, "content": "denied\nmore"}
    assert show(user(error)) == "  ✗ denied"
    assert show(user({"type": "tool_result", "content": "fine"})) == ""
    assert show({"type": "result", "subtype": "success", "duration_ms": 2600}) == (
        "· turn ended · success · 3s"
    )
    assert show({"type": "rate_limit_event"}) == ""
