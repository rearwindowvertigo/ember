import contextlib
import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ember import engines
from ember.engines import DriverError, codex
from ember.engines.codex import CodexDriver
from ember.runs.record import Run

RUN_ID = "codex-20261101-120000-abc123"
THREAD = "thread-0001"
BRIEF = f"Brief from Claude, the lead, for Ember run {RUN_ID}.\n\nDo the work."

MODELS = {
    "models": [
        {
            "slug": "gpt-5.5",
            "display_name": "GPT-5.5",
            "visibility": "list",
            "supported_reasoning_levels": [
                {"effort": "low"},
                {"effort": "medium"},
                {"effort": "xhigh"},
            ],
            "default_reasoning_level": "medium",
        },
        {"slug": "plain", "visibility": "list"},
    ]
}


@dataclasses.dataclass
class Setup:
    tmp: Path
    env: dict[str, str]
    repo: Path
    home: Path

    @property
    def codex_home(self) -> Path:
        return Path(self.env["CODEX_HOME"])


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env=_git_env(cwd)
    )


def _git_env(cwd: Path) -> dict[str, str]:
    return {"PATH": os.defpath, "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    data = tmp_path / "models.json"
    data.write_text(json.dumps(MODELS))
    stub = bin_dir / "codex"
    stub.write_text(
        f'#!/bin/sh\n[ "$1 $2" = "debug models" ] || exit 9\nexec /bin/cat {data}\n'
    )
    stub.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(bin_dir), str(Path(git).parent)]),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "CODEX_HOME": str(home / ".codex"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        f'[projects.{json.dumps(str(repo))}]\ntrust_level = "trusted"\n'
    )
    return Setup(tmp=tmp_path, env=env, repo=repo, home=home)


def _run(setup: Setup, **changes: Any) -> Run:  # noqa: ANN401
    folder = setup.tmp / "run"
    folder.mkdir(exist_ok=True)
    run = Run(
        id=RUN_ID,
        engine="codex",
        model="gpt-5.5",
        effort="xhigh",
        read_only=False,
        repo=str(setup.repo),
        worktree=str(setup.repo),
        branch=f"ember/codex/{RUN_ID}",
        base=None,
        sandbox="bubblewrap",
        read=(),
        trailers=(),
        claim=None,
        status="running",
        turn=1,
        turn_state="sent",
        reply=None,
        expected_head=None,
        started_at=0.0,
        updated_at=0.0,
        ended_at=None,
        last_exit=None,
        last_reason=None,
        commits=(),
        subject="Do the work.",
        folder=folder,
        env=setup.env,
    )
    return dataclasses.replace(run, **changes)


def _line(kind: str, **payload: object) -> str:
    return json.dumps({"type": "event_msg", "payload": {"type": kind, **payload}})


def _rollout(
    setup: Setup,
    *events: str,
    run_id: str = RUN_ID,
    cwd: Path | None = None,
    thread: str = THREAD,
    name: str = "rollout-1.jsonl",
) -> Path:
    folder = setup.codex_home / "sessions" / "2026" / "11" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": thread, "cwd": str(cwd or setup.repo)},
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"run {run_id}"}],
                },
            }
        ),
        *events,
    ]
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _append(path: Path, *events: str) -> None:
    with path.open("a") as stream:
        stream.writelines(line + "\n" for line in events)


def _started(setup: Setup, run: Run | None = None) -> tuple[CodexDriver, Run]:
    driver = CodexDriver(setup.env)
    run = run or _run(setup)
    driver.command(run, BRIEF)
    return driver, run


def _queue(setup: Setup, *threads: str, wal: bool = False) -> sqlite3.Connection:
    database = sqlite3.connect(setup.codex_home / codex.QUEUE_NAME)
    if wal:
        assert database.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        database.execute("PRAGMA wal_autocheckpoint=0")
    database.execute(
        "CREATE TABLE queued_items (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL)"
    )
    database.executemany(
        "INSERT INTO queued_items VALUES (?, ?)",
        [(str(index), thread) for index, thread in enumerate(threads)],
    )
    database.commit()
    return database


@contextlib.contextmanager
def _tmux_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[str, tuple[object, ...]]]]:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def record(name: str) -> Callable[..., None]:
        def call(*args: object, **kwargs: object) -> None:
            calls.append((name, args))

        return call

    for name in ("send_text", "send_keys"):
        monkeypatch.setattr(codex.tmux, name, record(name))
    yield calls


# The factory


def test_load_driver_returns_the_codex_driver(setup: Setup) -> None:
    driver = engines.load_driver("codex", setup.env)
    assert isinstance(driver, CodexDriver)
    assert driver.name == "codex"
    assert driver.env is setup.env


# Starting


def test_command_leaves_the_brief_to_paste(setup: Setup) -> None:
    driver = CodexDriver(setup.env)
    run = _run(setup)
    spec = driver.command(run, BRIEF)
    assert spec.argv == (
        str(setup.tmp / "bin" / "codex"),
        "-C",
        str(setup.repo),
        "-m",
        "gpt-5.5",
        "-c",
        "check_for_update_on_startup=false",
        "-c",
        'model_reasoning_effort="xhigh"',
        "-a",
        "never",
        "--search",
    )
    assert spec.cwd == setup.repo
    assert spec.first_message == BRIEF
    assert dict(spec.env) == {}
    state = json.loads((run.folder / codex.STATE_NAME).read_text())
    assert state["rollout"] == "" and state["turns"] == {}
    assert isinstance(state["launched"], float)


def test_command_without_effort_or_sandbox(setup: Setup) -> None:
    run = _run(setup, effort=None, sandbox="none", read_only=True)
    spec = CodexDriver(setup.env).command(run, BRIEF)
    assert not any(arg.startswith("model_reasoning_effort") for arg in spec.argv)
    index = spec.argv.index("--sandbox")
    assert spec.argv[index + 1] == "danger-full-access"
    assert "read-only" not in spec.argv


@pytest.mark.parametrize("method", ["bubblewrap", "codex-bundled"])
def test_command_leaves_a_sandboxed_run_to_the_core(setup: Setup, method: str) -> None:
    for read_only in (False, True):
        run = _run(setup, sandbox=method, read_only=read_only)
        spec = CodexDriver(setup.env).command(run, BRIEF)
        assert "--sandbox" not in spec.argv
        assert "-s" not in spec.argv


def test_command_fails_without_codex(setup: Setup) -> None:
    (setup.tmp / "bin" / "codex").unlink()
    with pytest.raises(DriverError, match="not on PATH"):
        CodexDriver(setup.env).command(_run(setup), BRIEF)


def test_command_keeps_a_long_brief_off_the_command_line(setup: Setup) -> None:
    # tmux refuses a session command of more than about 16 KB.
    brief = BRIEF + "x" * 200_000
    spec = CodexDriver(setup.env).command(_run(setup), brief)
    assert spec.first_message == brief
    assert sum(len(arg) for arg in spec.argv) < 1000


def test_command_refuses_an_untrusted_repository(setup: Setup) -> None:
    (setup.codex_home / "config.toml").write_text("not = [valid")
    with pytest.raises(DriverError, match=f"Codex does not trust {setup.repo}"):
        CodexDriver(setup.env).command(_run(setup), BRIEF)
    (setup.codex_home / "config.toml").unlink()
    with pytest.raises(DriverError, match="does not trust"):
        CodexDriver(setup.env).command(_run(setup), BRIEF)


def test_command_trusts_a_worktree_through_its_repository(setup: Setup) -> None:
    _git(setup.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "base")  # fmt: skip
    worktree = setup.tmp / "worktree"
    _git(setup.repo, "worktree", "add", "-q", str(worktree))
    spec = CodexDriver(setup.env).command(_run(setup, worktree=str(worktree)), BRIEF)
    assert spec.cwd == worktree


def test_command_fails_outside_a_repository(setup: Setup) -> None:
    folder = setup.tmp / "plain"
    folder.mkdir()
    with pytest.raises(DriverError, match="not a git repository"):
        CodexDriver(setup.env).command(_run(setup, worktree=str(folder)), BRIEF)


def _saved(run: Run) -> tuple[str, str]:
    state = json.loads((run.folder / codex.STATE_NAME).read_text())
    return state["rollout"], state["thread"]


def test_ready_once_the_composer_waits(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, run = _started(setup)
    screens = iter(
        [
            "  Update available\n› 1. Update now\n  2. Skip\n",
            "› Ask Codex to do anything\n  model · folder\n",
            "› Ask Codex to do anything\n  model · folder\n  ? for shortcuts\n",
        ]
    )
    monkeypatch.setattr(codex.tmux, "capture", lambda *a, **k: next(screens))
    assert [driver.ready(run) for _ in range(3)] == [False, False, True]


def test_the_first_message_is_pasted_before_the_rollout_exists(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, run = _started(setup)
    with _tmux_calls(monkeypatch) as calls:
        driver.send(run, BRIEF)
    assert calls == [("send_text", (RUN_ID, BRIEF))]
    assert driver.wait(run) == engines.TurnOutcome(engines.RUNNING)
    _rollout(
        setup, _line("task_started"), _line("task_complete", last_agent_message="ok")
    )
    assert driver.wait(run) == engines.TurnOutcome(engines.DONE, reply="ok")


def test_discovery_finds_the_rollout_that_names_the_run(setup: Setup) -> None:
    driver, run = _started(setup)
    _rollout(setup, run_id="codex-20261101-120000-ffffff", name="rollout-0.jsonl")
    _rollout(setup, cwd=setup.tmp, name="rollout-2.jsonl")
    old = _rollout(setup, name="rollout-3.jsonl")
    os.utime(old, (1, 1))
    assert driver.wait(run).state == engines.RUNNING
    assert _saved(run) == ("", "")
    path = _rollout(setup)
    assert driver.wait(run).state == engines.RUNNING
    assert _saved(run) == (str(path), THREAD)


def test_discovery_waits_for_the_first_message(setup: Setup) -> None:
    driver, run = _started(setup)
    folder = setup.codex_home / "sessions"
    folder.mkdir()
    path = folder / "rollout-1.jsonl"
    meta = {"type": "session_meta", "payload": {"id": THREAD, "cwd": str(setup.repo)}}
    path.write_text(json.dumps(meta) + "\n")
    assert driver.wait(run) == engines.TurnOutcome(engines.RUNNING)
    path.unlink()
    _rollout(setup, _line("task_started"))
    assert driver.wait(run).state == engines.RUNNING


# Turns


def test_a_completed_turn_gives_its_final_reply(setup: Setup) -> None:
    driver, run = _started(setup)
    path = _rollout(
        setup,
        _line("task_started"),
        _line("agent_message", message="Looking at the files."),
        _line("agent_message", message="Running the tests."),
    )
    assert driver.wait(run).state == engines.RUNNING
    _append(path, _line("task_complete", last_agent_message="All done.\nTwo lines."))
    outcome = driver.wait(run)
    assert outcome == engines.TurnOutcome(engines.DONE, reply="All done.\nTwo lines.")


def test_an_empty_reply_is_no_reply(setup: Setup) -> None:
    driver, run = _started(setup)
    _rollout(
        setup, _line("task_started"), _line("task_complete", last_agent_message="")
    )
    assert driver.wait(run) == engines.TurnOutcome(engines.DONE)
    _rollout(setup, _line("task_started"), _line("task_complete"))
    assert driver.wait(run) == engines.TurnOutcome(engines.DONE)


def test_an_unfinished_last_line_is_ignored(setup: Setup) -> None:
    driver, run = _started(setup)
    path = _rollout(setup, _line("task_started"))
    with path.open("a") as stream:
        stream.write(_line("task_complete", last_agent_message="half")[:30])
    assert driver.wait(run).state == engines.RUNNING


def test_an_interrupted_turn_has_no_reply(setup: Setup) -> None:
    driver, run = _started(setup)
    _rollout(setup, _line("task_started"), _line("turn_aborted", reason="interrupted"))
    assert driver.wait(run) == engines.TurnOutcome(engines.DONE)


def test_a_turn_aborted_by_an_error_fails(setup: Setup) -> None:
    driver, run = _started(setup)
    _rollout(
        setup,
        _line("task_started"),
        _line("error", message="stream disconnected"),
        _line("turn_aborted"),
    )
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.provider_error and not outcome.signed_out
    assert outcome.reason == "Codex reported an error: stream disconnected"


@pytest.mark.parametrize(
    ("message", "signed_out"),
    [
        ("You've hit your usage limit. Try again later.", False),
        ("unexpected status 503 Service Unavailable", False),
        ("unexpected status 401 Unauthorized: token expired", True),
        ("Your access token could not be refreshed. Please log in again.", True),
    ],
)
def test_an_error_fails_the_turn(setup: Setup, message: str, signed_out: bool) -> None:
    driver, run = _started(setup)
    _rollout(setup, _line("task_started"), _line("error", message=message))
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.signed_out is signed_out
    assert outcome.provider_error is not signed_out
    assert outcome.reason == f"Codex reported an error: {message}"


def test_send_counts_from_where_the_rollout_stood(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, run = _started(setup)
    path = _rollout(
        setup,
        _line("task_started"),
        _line("error", message="old failure"),
        _line("task_complete", last_agent_message="first"),
    )
    assert driver.wait(run).reply == "first"
    run = dataclasses.replace(run, turn=2)
    with _tmux_calls(monkeypatch) as calls:
        driver.send(run, "Next step.\nWith detail.")
    assert calls == [("send_text", (RUN_ID, "Next step.\nWith detail."))]
    assert driver.wait(run).state == engines.RUNNING
    _append(path, _line("task_started"))
    assert driver.idle_state(run).state == engines.BUSY
    _append(path, _line("task_complete", last_agent_message="second"))
    assert driver.wait(run) == engines.TurnOutcome(engines.DONE, reply="second")
    # A turn typed later in the session does not change this turn's reply.
    _append(path, _line("task_started"), _line("task_complete", last_agent_message="x"))
    assert driver.wait(run).reply == "second"


def test_a_later_turn_with_no_send_record_cannot_be_read(setup: Setup) -> None:
    driver, run = _started(setup)
    _rollout(setup, _line("task_started"), _line("task_complete"))
    with pytest.raises(DriverError, match="no record of when turn 2 was sent"):
        driver.wait(dataclasses.replace(run, turn=2))


def test_send_needs_the_session(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    driver, run = _started(setup)
    with _tmux_calls(monkeypatch) as calls, pytest.raises(DriverError, match="find"):
        driver.send(dataclasses.replace(run, turn=2), "hello")
    assert calls == []


def test_a_missing_rollout_or_state_is_an_error(setup: Setup) -> None:
    driver, run = _started(setup)
    path = _rollout(setup, _line("task_started"))
    assert driver.wait(run).state == engines.RUNNING
    path.unlink()
    with pytest.raises(DriverError, match="rollout .* is missing"):
        driver.wait(run)
    assert driver.idle_state(run).state == engines.UNKNOWN
    (run.folder / codex.STATE_NAME).write_text("[]")
    with pytest.raises(DriverError, match="not a Codex driver state"):
        driver.wait(run)
    (run.folder / codex.STATE_NAME).write_text("{")
    with pytest.raises(DriverError, match="cannot read"):
        driver.wait(run)


def test_interrupt_presses_escape(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, run = _started(setup)
    with _tmux_calls(monkeypatch) as calls:
        driver.interrupt(run)
    assert calls == [("send_keys", (RUN_ID, "Escape"))]


def test_close_is_safe_on_a_run_that_never_started(setup: Setup) -> None:
    driver = CodexDriver(setup.env)
    run = _run(setup)
    driver.close(run)
    driver.close(run)


# Idle state


def test_idle_state_follows_turns_and_the_queue(setup: Setup) -> None:
    driver, run = _started(setup)
    assert driver.idle_state(run).state == engines.UNKNOWN
    path = _rollout(setup, _line("task_started"))
    assert driver.idle_state(run) == engines.IdleState("busy", "a turn is running")
    _append(path, _line("task_complete", last_agent_message="done"))
    idle = driver.idle_state(run)
    assert idle.state == engines.UNKNOWN
    assert "cannot read Codex's queue" in idle.reason
    assert not (setup.codex_home / codex.QUEUE_NAME).exists()
    database = _queue(setup, "other-thread", "x' OR 1=1 --")
    assert driver.idle_state(run) == engines.IdleState("idle")
    database.execute("INSERT INTO queued_items VALUES ('9', ?)", (THREAD,))
    database.commit()
    database.close()
    busy = driver.idle_state(run)
    assert busy == engines.IdleState("busy", "1 message(s) wait in Codex's queue")


def _files(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}


def test_idle_state_reads_committed_messages_in_the_log(setup: Setup) -> None:
    driver, run = _started(setup)
    _rollout(setup, _line("task_started"), _line("task_complete"))
    database = _queue(setup, wal=True)
    database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    base = (setup.codex_home / codex.QUEUE_NAME).read_bytes()
    database.executemany(
        "INSERT INTO queued_items VALUES (?, ?)",
        [(f"o{index}", "other" * 200) for index in range(20)] + [("t", THREAD)],
    )
    database.commit()
    assert (setup.codex_home / codex.QUEUE_NAME).read_bytes() == base
    before = _files(setup.codex_home)
    assert driver.idle_state(run).state == engines.BUSY
    assert _files(setup.codex_home) == before
    database.execute("DELETE FROM queued_items")
    database.commit()
    assert driver.idle_state(run).state == engines.IDLE
    database.close()


HOLDER = """
import fcntl, sqlite3, sys
path = sys.argv[1]
if sys.argv[2] == "writer":
    database = sqlite3.connect(path)
    database.execute("BEGIN IMMEDIATE")
else:
    shm = open(path + "-shm", "r+b")
    fcntl.lockf(shm, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 121)
print("held", flush=True)
sys.stdin.read()
"""


@contextlib.contextmanager
def _held(setup: Setup, kind: str) -> Iterator[None]:
    path = str(setup.codex_home / codex.QUEUE_NAME)
    with subprocess.Popen(
        [sys.executable, "-c", HOLDER, path, kind],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        assert holder.stdout is not None and holder.stdin is not None
        assert holder.stdout.readline() == "held\n"
        try:
            yield
        finally:
            holder.stdin.close()


@pytest.mark.parametrize("kind", ["writer", "checkpoint"])
def test_idle_state_with_a_busy_queue_is_unknown(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(codex.time, "sleep", lambda seconds: None)
    driver, run = _started(setup)
    _rollout(setup, _line("task_started"), _line("task_complete"))
    database = _queue(setup, wal=True)
    assert driver.idle_state(run).state == engines.IDLE
    with _held(setup, kind):
        before = _files(setup.codex_home)
        assert driver.idle_state(run).state == engines.UNKNOWN
        assert _files(setup.codex_home) == before
    database.close()


def test_idle_state_refuses_a_damaged_log(setup: Setup) -> None:
    driver, run = _started(setup)
    _rollout(setup, _line("task_started"), _line("task_complete"))
    database = _queue(setup, wal=True)
    database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.execute("INSERT INTO queued_items VALUES ('t', ?)", (THREAD,))
    database.commit()
    wal = setup.codex_home / f"{codex.QUEUE_NAME}-wal"
    original = wal.read_bytes()
    bad_header = bytearray(original)
    bad_header[24] ^= 1
    bad_frame = bytearray(original)
    bad_frame[48] ^= 1
    try:
        for content in (original[:20], original[:-1], bad_header, bad_frame):
            wal.write_bytes(bytes(content))
            assert driver.idle_state(run).state == engines.UNKNOWN
    finally:
        wal.write_bytes(original)
    assert driver.idle_state(run).state == engines.BUSY
    database.close()


def test_wal_boundary_checks_the_index_header() -> None:
    with pytest.raises(ValueError, match="missing or inconsistent"):
        codex.wal_boundary(b"\x00" * 10)
    header = bytearray(136)
    header[48:96] = header[:48]
    with pytest.raises(ValueError, match="unsupported"):
        codex.wal_boundary(bytes(header))


def test_apply_wal_refuses_what_is_not_a_database() -> None:
    with pytest.raises(ValueError, match="invalid queue header"):
        codex.apply_wal(b"not sqlite", b"", None)


# Efforts


def test_efforts_come_from_the_codex_model_list(setup: Setup) -> None:
    driver = CodexDriver(setup.env)
    assert driver.valid_effort("xhigh")
    assert not driver.valid_effort("max")
    assert driver.default_effort("gpt-5.5") == "medium"
    assert driver.default_effort("plain") is None
    assert driver.default_effort("unknown") is None


def test_efforts_without_a_model_list_are_words(setup: Setup) -> None:
    (setup.tmp / "bin" / "codex").unlink()
    driver = CodexDriver(setup.env)
    assert driver.valid_effort("ultra")
    assert not driver.valid_effort("High")
    assert driver.default_effort("gpt-5.5") is None


def test_events_tolerate_a_missing_file(tmp_path: Path) -> None:
    assert list(codex.events(tmp_path / "missing.jsonl")) == []
    assert codex.read_turns(tmp_path / "missing.jsonl") == codex.Turns()


def test_discovery_skips_rollouts_older_than_the_launch(setup: Setup) -> None:
    driver, run = _started(setup)
    path = _rollout(setup, _line("task_started"))
    past = time.time() - 3600
    os.utime(path, (past, past))
    assert driver.wait(run).state == engines.RUNNING
    assert _saved(run) == ("", "")
