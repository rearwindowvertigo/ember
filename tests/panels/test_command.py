import io
import json
import signal
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember import choices, paths, tmux
from ember.cli import build_parser
from ember.panels import brief, command, members, record
from ember.runs import record as runs
from ember.runs.core import Core
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    engine_cache,
    git,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
    tmux_log,
)


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.env = make_env(tmp)
        self.repo = make_repo(tmp, self.env)
        self.question = tmp / "question with spaces.md"
        self.question.write_text("What does this checkout do?\n")
        self.scripts = tmp / "scripts.json"
        self.scripts.write_text("{}")
        self.drivers = {}
        for engine in ("codex", "opencode", "claude"):
            reply = tmp / f"{engine}.md"
            reply.write_text(f"Answer from {engine}.\n")
            self.drivers[engine] = StubDriver(
                engine, script_path(self.env), reply_file=reply, scripts=self.scripts
            )
        self.threads: list[threading.Thread] = []

    def factory(self, env: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        self.threads.append(threading.current_thread())
        return Core(
            env, load_driver=self.drivers.__getitem__, poll_seconds=0.02, **kwargs
        )

    def invoke(
        self, *options: str, stop: threading.Event | None = None
    ) -> tuple[int, str, str]:
        args = build_parser().parse_args(["panel", *options])
        out, err = io.StringIO(), io.StringIO()
        code = command.main(
            args,
            self.env,
            core_factory=self.factory,
            spacing=0,
            poll_seconds=0.02,
            stop=stop,
            out=out,
            err=err,
        )
        return code, out.getvalue(), err.getvalue()

    def start(
        self, *options: str, stop: threading.Event | None = None
    ) -> tuple[int, str, str]:
        return self.invoke(
            "--question-file",
            str(self.question),
            "--dir",
            str(self.repo),
            *options,
            stop=stop,
        )

    def script(self, engine: str, text: str) -> None:
        self.scripts.write_text(json.dumps({f"for Ember run {engine}-": text}))

    def release(self) -> None:
        for folder in paths.runs_dir(self.env).glob("*"):
            if folder.name.startswith(("codex-", "opencode-", "claude-")):
                (folder / "stub").mkdir(exist_ok=True)
                (folder / "stub" / "release").touch()

    def join(self) -> None:
        for thread in set(self.threads):
            if thread is not threading.current_thread():
                thread.join(20)
                assert not thread.is_alive()

    def create(self, **values: Any) -> str:  # noqa: ANN401
        panel_id = record.new_id()
        panel = record.new_panel(
            panel_id,
            dir=str(self.repo),
            question_file=str(self.question),
            read=[],
            claim=None,
            stall_minutes=10,
            timeout_minutes=30,
            members=[members.Member("codex", "gpt-5", None, "option").entry(1)],
            left_out=[],
        )
        panel.update(values)
        with record.hold(panel_id, self.env):
            record.create(
                panel, "Question?", brief.text(panel_id, "Question?"), self.env
            )
        return panel_id

    def no_runs(self) -> None:
        assert not list(paths.runs_dir(self.env).glob("*"))
        assert not tmux_log(self.tmp)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    try:
        yield harness
    finally:
        harness.release()
        try:
            harness.join()
        finally:
            kill_sessions(tmp_path)


def actions(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(("next:", "owner:"))]


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--status", "--member", "codex:x"],
        ["--recover", "x", "--dry-run"],
        ["--question-file", "x", "--dry-run", "--stall", "1"],
        ["--question-file", "x", "--dry-run", "--timeout", "1"],
        ["--question-file", "x", "--status"],
        ["--status", "--recover", "x"],
        ["--status", "--stall", "1"],
        ["--status", "--timeout", "1"],
        ["--status", "--dir", "."],
        ["--status", "--claim", "x"],
        ["--status", "--read", "."],
    ],
)
def test_usage_before_inputs(h: Harness, options: list[str]) -> None:
    code, out, err = h.invoke(*options)
    assert code == 64
    assert not out
    assert actions(err) == [members.HELP_LINE]
    h.no_runs()


@pytest.mark.parametrize(
    "case, expected",
    [
        ("missing question", 66),
        ("blank", 64),
        ("encoding", 66),
        ("directory", 66),
        ("checkout", 65),
        ("read", 66),
        ("gone claim", 64),
        ("unknown claim", 64),
        ("invalid claim", 64),
    ],
)
def test_inputs_create_no_runs(h: Harness, case: str, expected: int) -> None:
    options = ["--member", "codex:x"]
    if case == "missing question":
        h.question.unlink()
    elif case == "blank":
        h.question.write_text(" \n\t")
    elif case == "encoding":
        h.question.write_bytes(b"\xff")
    elif case == "directory":
        h.repo = h.tmp / "absent"
    elif case == "checkout":
        h.repo = h.tmp
    elif case == "read":
        options += ["--read", str(h.tmp / "absent")]
    else:
        claim = "panel-claim"
        if case == "gone claim":
            choices.claim(claim, env=h.env)
            choices.finish(claim, env=h.env)
        elif case == "invalid claim":
            claim = "invalid claim"
        options += ["--claim", claim]
    code, out, err = h.start(*options)
    assert code == expected, err
    assert not out
    assert len(actions(err)) == 1
    if "claim" in case:
        assert actions(err) == [
            "next: emb choices --claim --id <id>, then pass that id to --claim"
        ]
    h.no_runs()


def test_no_members_and_all_excluded(h: Harness) -> None:
    choices.set_settings({"single.writer": "ask", "single.reviewer": "ask"}, env=h.env)
    assert h.start()[0] == 64
    choices.set_settings(
        {"panel.members": ["codex:gpt-5"], "engine.codex": "off"}, env=h.env
    )
    code, out, err = h.start()
    assert code == 64 and not out
    assert "emb panel: left out codex:gpt-5: codex is off" in err
    assert actions(err) == [members.NO_MEMBER_LINE]
    h.no_runs()


@pytest.mark.parametrize(
    "case, expected",
    [
        ("off", 64),
        ("disconnected", 69),
        ("effort", 64),
        ("duplicate", 64),
        ("ninth", 64),
        ("syntax", 64),
    ],
)
def test_explicit_members_fail_before_start(
    h: Harness, case: str, expected: int
) -> None:
    options = ["--member", "codex:x"]
    if case == "off":
        choices.set_settings({"engine.codex": "off"}, env=h.env)
    elif case == "disconnected":
        engine_cache(h.env, {"codex": False})
    elif case == "effort":
        options = ["--member", "codex:x@invalid"]
    elif case == "duplicate":
        options += ["--member", "codex:x@low"]
    elif case == "ninth":
        options = [part for n in range(9) for part in ("--member", f"codex:m{n}")]
    else:
        options = ["--member", "bad"]
    code, out, err = h.start(*options)
    assert code == expected, err
    assert not out and len(actions(err)) == 1
    h.no_runs()


def test_corrupt_panel_preserves_engine_and_sandbox_settings(h: Harness) -> None:
    choices.set_settings({"engine.codex": "off", "worker.network": "off"}, env=h.env)
    with sqlite3.connect(paths.settings_db_path(h.env)) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('panel.members', 'false')"
        )
    code, out, err = h.start("--member", "codex:x")
    assert code == 64 and "switched off" in err and not out
    h.no_runs()
    choices.set_settings({"engine.codex": "on"}, env=h.env)
    code, out, err = h.start("--member", "codex:x", "--json")
    assert code == 0, err
    member = json.loads(out)["members"][0]
    run = runs.load(member["run"], h.env)
    assert run.sandbox == "none" and run.network is False


@pytest.mark.parametrize("as_json", [False, True])
def test_dry_run_validates_without_launch_or_storage(h: Harness, as_json: bool) -> None:
    (h.tmp / "bin" / "tmux").unlink()
    h.env["XDG_RUNTIME_DIR"] = str(h.question)
    code, out, err = h.start(
        "--member", "codex:x@low", "--dry-run", *(["--json"] if as_json else [])
    )
    assert code == 0 and not err
    if as_json:
        assert json.loads(out) == {
            "dir": str(h.repo),
            "members": [
                {
                    "n": 1,
                    "engine": "codex",
                    "model": "x",
                    "effort": "low",
                    "source": "option",
                }
            ],
            "left_out": [],
        }
    else:
        assert out == f"checkout {h.repo}\nmember 1 codex:x · low · option\n"
    h.no_runs()


@pytest.mark.parametrize("tool", ["git", "tmux"])
def test_missing_tools(h: Harness, tool: str) -> None:
    (h.tmp / ("tools" if tool == "git" else "bin") / tool).unlink()
    code, out, err = h.start("--member", "codex:x")
    assert code == 69 and not out
    assert actions(err) == [f"owner: install {tool}"]
    h.no_runs()


def test_saved_members_left_out_in_result(h: Harness) -> None:
    choices.set_settings({"panel.members": ["codex:gpt-5", "claude:opus"]}, env=h.env)
    choices.set_settings({"shortlist.claude": []}, env=h.env)
    code, out, err = h.start("--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["left_out"] == [
        {"value": "claude:opus", "reason": "not on the claude shortlist"}
    ]
    assert "emb panel: left out claude:opus:" in err
    assert "## Left out" in Path(result["summary"]).read_text()


@pytest.mark.parametrize("as_json", [False, True])
def test_start_output_and_run_options(h: Harness, as_json: bool) -> None:
    h.env["GIT_DIR"] = "/nonexistent"
    choices.set_settings({"single.writer": "ask", "single.reviewer": "ask"}, env=h.env)
    choices.claim("panel-claim", env=h.env)
    code, out, err = h.start(
        "--member",
        "codex:x@low",
        "--member",
        "claude:haiku",
        "--read",
        str(h.tmp),
        "--claim",
        "panel-claim",
        *(["--json"] if as_json else []),
    )
    assert code == 0, err
    if as_json:
        result = json.loads(out)
        assert set(result) == {
            "panel",
            "status",
            "exit",
            "dir",
            "folder",
            "summary",
            "members",
            "left_out",
            "next",
        }
        panel_id = result["panel"]
        assert all("attempts" not in m for m in result["members"])
        assert result["next"] == actions(err)[0]
    else:
        assert out.splitlines()[0].startswith("panel panel-")
        panel_id = out.splitlines()[0].split()[1]
        assert "member 1 codex:x · low · answered" in err
        assert " · exit 0 · " in err
        assert "member " not in out
        assert out.splitlines()[-1].startswith("summary ")
    panel = record.load(panel_id, h.env)
    for member in panel["members"]:
        run = runs.load(member["run"], h.env)
        assert run.read_only and run.status == runs.CLOSED
        assert run.claim is None and run.trailers == ()
        assert run.read == (str(h.tmp),)
        assert run.effort == member["effort"]
    assert actions(err) == [
        f"next: read {record.folder(panel_id, h.env) / 'summary.md'}, "
        "then compare the answers"
    ]


@pytest.mark.parametrize("script", ["@fail", "@write notes.txt x"])
def test_terminal_partial_results(h: Harness, script: str) -> None:
    h.script("codex", script)
    code, out, err = h.start("--member", "codex:x", "--member", "claude:x", "--json")
    assert code == command.SOME_FAILED, err
    result = json.loads(out)
    assert result["members"][1]["status"] == "answered"
    assert len(actions(err)) == 1
    assert "report each member that is not answered or changed paths" in actions(err)[0]


def test_stalled_member_has_recovery_action(h: Harness) -> None:
    h.script("codex", "@hang")
    code, out, err = h.start("--member", "codex:x", "--stall", "0.002", "--json")
    assert code == 83, err
    result = json.loads(out)
    assert result["members"][0]["status"] == "stalled"
    assert len(actions(err)) == 1
    assert (
        f"emb panel --recover {result['panel']} (collects 1 unfinished members)" in err
    )
    assert "emb run --close <run> --force" in err


def test_deadline_and_recovery(h: Harness) -> None:
    h.script("codex", "@hang")
    code, out, err = h.start("--member", "codex:x", "--timeout", "0.02", "--json")
    assert code == 83, err
    result = json.loads(out)
    assert result["members"][0]["status"] == "timed out"
    assert result["members"][0]["run"]
    h.release()
    h.join()
    code, out, err = h.invoke("--recover", result["panel"], "--json")
    assert code == 0, err
    member = json.loads(out)["members"][0]
    assert member["closed"] and member["status"] == "answered"
    assert (
        record.load(result["panel"], h.env)["members"][0]["attempts"][-1]["kind"]
        == "recover"
    )


def test_stop_during_turn(h: Harness) -> None:
    h.script("codex", "@hang")
    stop = threading.Event()

    def stop_when_running() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            panels, _ = record.list_panels(h.env)
            if panels and panels[0]["members"][0]["run"]:
                stop.set()
                return
            time.sleep(0.02)
        stop.set()

    timer = threading.Thread(target=stop_when_running)
    timer.start()
    try:
        code, out, err = h.start("--member", "codex:x", "--json", stop=stop)
    finally:
        timer.join(20)
    assert code == 74, err
    result = json.loads(out)
    assert result["members"][0]["status"] == "interrupted"
    assert len(actions(err)) == 1 and "--recover" in actions(err)[0]


def test_stop_without_known_run(h: Harness) -> None:
    stop = threading.Event()
    stop.set()
    code, out, err = h.start("--member", "codex:x", "--json", stop=stop)
    assert code == 74, err
    assert json.loads(out)["members"][0]["status"] == "not started"
    assert len(actions(err)) == 1
    assert "emb run --status" in err and "without a saved run id" in err


def test_signal_handlers_restore_previous_handlers(h: Harness) -> None:
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    stop = threading.Event()
    with command.stop_handlers(stop):
        for signum in signals:
            handler = signal.getsignal(signum)
            assert callable(handler)
            handler(signum, None)
            assert stop.is_set()
            stop.clear()
    assert {signum: signal.getsignal(signum) for signum in signals} == previous


def test_status_empty_and_broken(h: Harness) -> None:
    assert h.invoke("--status") == (0, "", "")
    assert h.invoke("--status", "--json") == (0, "[]\n", "")
    panel_id = h.create()
    broken_id = "panel-20260101-000000-abcdef"
    record.folder(broken_id, h.env).mkdir()
    (record.folder(broken_id, h.env) / "panel.json").write_text("{")
    code, out, err = h.invoke("--status")
    assert code == 0
    assert len(out.splitlines()) == 1 and panel_id in out
    assert f"emb panel: skipped {broken_id}" in err
    code, out, err = h.invoke("--status", "--json")
    assert code == 0
    assert json.loads(out) == [
        {
            "id": panel_id,
            "status": "running",
            "dir": str(h.repo),
            "exit": None,
            "started_at": record.load(panel_id, h.env)["started_at"],
            "live": False,
            "answered": 0,
            "member_count": 1,
            "running_members": [],
            "denied_paths": [],
        }
    ]
    assert h.invoke("--status", broken_id)[0] == 1


def test_status_live_and_null_times(h: Harness) -> None:
    panel_id = h.create()
    with record.hold(panel_id, h.env):
        code, out, err = h.invoke("--status", panel_id, "--json")
        assert code == 0 and not err and json.loads(out)["live"] is True
    code, out, err = h.invoke("--status", panel_id)
    assert code == 0 and not err
    assert "live: false\n" in out
    assert " · pending · - · exit - · 0 bytes · run -" in out
    assert json.loads(h.invoke("--status", panel_id, "--json")[1])["live"] is False


@pytest.mark.parametrize("mode", ["--status", "--recover"])
@pytest.mark.parametrize("panel_id", ["bad", "panel-20260101-000000-abcdef"])
def test_unknown_panel(h: Harness, mode: str, panel_id: str) -> None:
    code, out, err = h.invoke(mode, panel_id)
    assert code == 64 and not out
    assert actions(err) == ["next: emb panel --status"]


def test_recovery_busy_and_no_runs(h: Harness) -> None:
    panel_id = h.create()
    with record.hold(panel_id, h.env):
        code, out, err = h.invoke("--recover", panel_id)
        assert code == 75 and not out and "still running" in err
    code, out, err = h.invoke("--recover", panel_id, "--json")
    assert code == 75 and not out
    assert "no unfinished member" in err
    assert actions(err) == [f"next: emb panel --status {panel_id}"]
    panel = record.load(panel_id, h.env)
    assert panel["status"] == "done" and panel["exit"] == 83
    assert panel["members"][0]["status"] == "failed"
    assert (
        "the panel ended before"
        in (record.folder(panel_id, h.env) / "summary.md").read_text()
    )


def test_lock_precedes_create_and_persistence_failure_reports_id(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = record.create

    def checked_create(
        panel: Mapping[str, Any],
        question: str,
        brief: str,
        env: Mapping[str, str] | None = None,
    ) -> Path:
        assert record.live(panel["id"], env)
        return create(panel, question, brief, env)

    def fail_save(panel: dict[str, Any], env: Mapping[str, str] | None = None) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr(record, "create", checked_create)
    monkeypatch.setattr(record, "save", fail_save)
    code, out, err = h.start("--member", "codex:x", "--json")
    assert code == 1 and not out
    panels, _ = record.list_panels(h.env)
    assert len(panels) == 1
    assert panels[0]["id"] in err
    assert actions(err) == [f"next: emb panel --status {panels[0]['id']}"]
    assert not record.live(panels[0]["id"], h.env)


@pytest.mark.parametrize("mode", ["--retry", "--close"])
@pytest.mark.parametrize("panel_id", ["bad", "panel-20260101-000000-abcdef"])
def test_retry_close_unknown_panel(h: Harness, mode: str, panel_id: str) -> None:
    options = [mode, panel_id, *(["1"] if mode == "--retry" else [])]
    code, out, err = h.invoke(*options, "--json")
    assert code == 65 and not out
    assert actions(err) == ["next: emb panel --status"]


@pytest.mark.parametrize("mode", ["--retry", "--close"])
def test_retry_close_busy(h: Harness, mode: str) -> None:
    panel_id = h.create()
    before = record.load(panel_id, h.env)
    options = [mode, panel_id, *(["1"] if mode == "--retry" else [])]
    with record.hold(panel_id, h.env):
        code, out, err = h.invoke(*options, "--json")
    assert code == 75 and not out and "still running" in err
    assert actions(err) == [f"next: emb panel --status {panel_id}"]
    assert record.load(panel_id, h.env) == before


@pytest.mark.parametrize("number", ["0", "-1", "2", "no", "1.5"])
def test_retry_unknown_member(h: Harness, number: str) -> None:
    panel_id = h.create()
    before = record.load(panel_id, h.env)
    code, out, err = h.invoke("--retry", panel_id, number, "--json")
    assert code == 65 and not out and f"no member {number}" in err
    assert record.load(panel_id, h.env) == before


@pytest.mark.parametrize("status", ["pending", "starting", "running", "answered"])
def test_retry_refuses_member_status(h: Harness, status: str) -> None:
    panel_id = h.create()
    panel = record.load(panel_id, h.env)
    panel["members"][0]["status"] = status
    record.save(panel, h.env)
    code, out, err = h.invoke("--retry", panel_id, "1", "--json")
    assert code == 75 and not out and f"member 1 is {status}" in err
    assert record.load(panel_id, h.env) == panel


@pytest.mark.parametrize("as_json", [False, True])
def test_retry_result_and_empty_close(h: Harness, as_json: bool) -> None:
    h.script("codex", "@fail first failure")
    code, out, _ = h.start("--member", "codex:gpt-5", "--json")
    assert code == 83
    first = json.loads(out)
    old_run = first["members"][0]["run"]
    h.script("codex", "@reply new answer")
    options = ["--json"] if as_json else []
    code, out, err = h.invoke("--retry", first["panel"], "1", *options)
    assert code == 0
    if as_json:
        result = json.loads(out)
        assert result["status"] == "done" and result["exit"] == 0
        assert result["members"][0]["run"] != old_run
        assert "attempts" not in result["members"][0]
    else:
        assert "member 1 codex:gpt-5" in err and " · answered · " in err
        assert "member " not in out
    assert len(actions(err)) == 1
    code, out, err = h.invoke("--close", first["panel"], *options)
    assert code == 0
    if as_json:
        assert json.loads(out) == {
            "panel": first["panel"],
            "exit": 0,
            "members": [],
            "next": f"next: emb panel --status {first['panel']}",
        }
    else:
        assert not out


def test_retry_and_close_refuse_live_run(h: Harness) -> None:
    h.script("codex", "@hang")
    code, out, _ = h.start("--member", "codex:gpt-5", "--stall", "0.005", "--json")
    assert code == 83
    panel = json.loads(out)
    panel_id, old_run = panel["panel"], panel["members"][0]["run"]
    code, out, err = h.invoke("--retry", panel_id, "1", "--json")
    assert code == 75 and not out and old_run in err
    saved = record.load(panel_id, h.env)
    assert saved["members"][0]["run"] == old_run
    assert not any(a["kind"] == "retry" for a in saved["members"][0]["attempts"])
    assert not saved["members"][0]["closed"]
    assert saved["members"][0]["close_error"]
    code, out, _ = h.invoke("--close", panel_id, "--json")
    assert code == 75
    result = json.loads(out)
    assert result["exit"] == 75
    assert result["members"][0]["run"] == old_run
    assert not result["members"][0]["closed"]
    code, out, _ = h.invoke("--close", panel_id)
    assert code == 75 and f"member 1 · run {old_run} · " in out
    assert "running" in out


@pytest.mark.parametrize("as_json", [False, True])
def test_close_prints_successful_run(h: Harness, as_json: bool) -> None:
    _, out, _ = h.start("--member", "codex:gpt-5", "--json")
    panel = json.loads(out)
    run_id = panel["members"][0]["run"]
    runs.update(runs.load(run_id, h.env), {"status": runs.WAITING}, allow_closed=True)
    code, out, err = h.invoke(
        "--close", panel["panel"], *(["--json"] if as_json else [])
    )
    assert code == 0
    if as_json:
        assert json.loads(out)["members"] == [
            {"n": 1, "run": run_id, "closed": True, "close_error": None}
        ]
    else:
        assert out == f"member 1 · run {run_id} · closed\n"
    assert actions(err) == [f"next: emb panel --status {panel['panel']}"]
    assert runs.load(run_id, h.env).status == runs.CLOSED


def test_close_keeps_changed_detached_worktree_and_prints_discard(h: Harness) -> None:
    h.drivers["codex"].foreign = "owner message"
    code, out, err = h.start("--member", "codex:x", "--json")
    assert code == 0, err
    panel_id = json.loads(out)["panel"]
    member = record.load(panel_id, h.env)["members"][0]
    saved = runs.load(member["run"], h.env)
    evidence = Path(saved.worktree) / "evidence.txt"
    evidence.write_text("keep\n")
    h.drivers["codex"].foreign = None
    code, out, err = h.invoke("--close", panel_id, "--json")
    assert code == 0, err
    assert json.loads(out)["members"][0]["closed"]
    assert evidence.read_text() == "keep\n"
    assert f"git worktree remove --force {saved.worktree}" in err
    assert runs.load(saved.id, h.env).worktree == saved.worktree
    code, out, err = h.invoke("--close", panel_id, "--json")
    assert code == 0 and json.loads(out)["members"][0]["closed"]
    assert f"git worktree remove --force {saved.worktree}" in err
    evidence.unlink()
    code, out, err = h.invoke("--close", panel_id, "--json")
    assert code == 0, err
    assert json.loads(out)["members"][0]["run"] == saved.id
    assert not Path(saved.worktree).exists()
    assert saved.worktree not in git(h.repo, "worktree", "list", env=h.env)
    code, out, err = h.invoke("--close", panel_id, "--json")
    assert code == 0 and json.loads(out)["members"] == []


@pytest.mark.parametrize("as_json", [False, True])
def test_progress_before_panel_ends(h: Harness, as_json: bool) -> None:
    h.script("claude", "@hang")
    args = build_parser().parse_args(
        [
            "panel",
            "--question-file",
            str(h.question),
            "--dir",
            str(h.repo),
            "--member",
            "codex:x",
            "--member",
            "claude:x",
            *(["--json"] if as_json else []),
        ]
    )
    finished = threading.Event()

    class ProgressStream(io.StringIO):
        def flush(self) -> None:
            super().flush()
            if "member 1 codex:x · medium · answered" in self.getvalue():
                finished.set()

    out, err = io.StringIO(), ProgressStream()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            command.main,
            args,
            h.env,
            core_factory=h.factory,
            spacing=0,
            poll_seconds=0.02,
            out=out,
            err=err,
        )
        try:
            assert finished.wait(20), err.getvalue()
            assert not future.done()
            assert "member 2 " not in err.getvalue()
            panels, errors = record.list_panels(h.env)
            assert len(panels) == 1 and not errors
            panel_id = panels[0]["id"]
            deadline = time.monotonic() + 20
            while True:
                first = json.loads(h.invoke("--status", panel_id, "--json")[1])
                running = first["members"][1]
                if running["last_screen_change_at"] is not None:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert running["seconds"] > 0
            assert running["last_screen_change_at"] > 0
            before = h.invoke("--status", panel_id)[1]
            time.sleep(1.1)
            second = json.loads(h.invoke("--status", panel_id, "--json")[1])
            assert second["members"][1]["seconds"] > running["seconds"]
            after = h.invoke("--status", panel_id)[1]
            assert before != after
            assert "last screen change " in after
            assert "member 2 claude:x" in after
            listed = json.loads(h.invoke("--status", "--json")[1])[0]
            assert listed["member_count"] == 2 and "members" not in listed
            assert len(listed["running_members"]) == 1
            progress = listed["running_members"][0]
            assert progress["n"] == 2
            assert progress["seconds"] >= running["seconds"]
            saved = runs.load(running["run"], h.env)
            assert progress["last_screen_change_at"] == saved.last_screen_change_at
            assert "member 2 claude:x" in h.invoke("--status")[1]
            assert not future.done()
            if as_json:
                assert out.getvalue() == ""
        finally:
            h.release()
        assert future.result(20) == 0
    assert err.getvalue().count("member 1 codex:x") == 1
    assert err.getvalue().count("member 2 claude:x") == 1
    if as_json:
        assert json.loads(out.getvalue())["exit"] == 0
    else:
        assert out.getvalue().splitlines()[-1].startswith("summary ")


@pytest.mark.parametrize("activity", [None, 0, 1234.5])
def test_status_activity_missing_or_zero(h: Harness, activity: float | None) -> None:
    _, out, _ = h.start("--member", "codex:x", "--json")
    result = json.loads(out)
    saved = runs.load(result["members"][0]["run"], h.env)
    document = saved.to_json()
    if activity is None:
        del document["last_screen_change_at"]
    else:
        document["last_screen_change_at"] = activity
    (saved.folder / "run.json").write_text(json.dumps(document))
    panel_id = result["panel"]
    member = json.loads(h.invoke("--status", panel_id, "--json")[1])["members"][0]
    assert member["last_screen_change_at"] == activity
    assert member["seconds"] == result["members"][0]["seconds"]
    shown = "-" if activity is None else str(float(activity))
    assert f"last screen change {shown}" in h.invoke("--status", panel_id)[1]


@pytest.mark.parametrize("kind", ["start", "recover", "retry"])
def test_status_elapsed_collection_intervals(
    h: Harness, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    panel_id = h.create()
    panel = record.load(panel_id, h.env)
    member = panel["members"][0]
    member.update(
        status="running",
        seconds=12 if kind == "recover" else None,
        attempts=[
            {"kind": kind, "at": 100, "exit": 124},
            {"kind": "stall", "at": 110, "exit": None},
        ],
    )
    record.save(panel, h.env)
    monkeypatch.setattr(command.time, "time", lambda: 120.0)
    result = json.loads(h.invoke("--status", panel_id, "--json")[1])["members"][0]
    assert result["seconds"] == (32 if kind == "recover" else 20)
    assert result["last_screen_change_at"] is None
    assert record.load(panel_id, h.env) == panel


@pytest.mark.parametrize("startup", [False, True])
def test_status_reports_member_denials_with_run_and_turn(
    h: Harness, monkeypatch: pytest.MonkeyPatch, startup: bool
) -> None:
    from ember import sandbox

    folder = Path(h.env["HOME"]) / (".profile" if startup else ".toolchain")
    if startup:
        folder.touch()
    else:
        folder.mkdir()
    denied = folder if startup else folder / "python"
    reason = "startup file" if startup else "hidden"
    choices.set_settings({"sandbox.codex": "on"}, env=h.env)
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    monkeypatch.setattr(Core, "_sandboxed", lambda self, spec, run, common: spec)
    h.script("codex", f"@reply bash: {denied}: Permission denied")
    code, out, _ = h.start("--member", "codex:x", "--json")
    assert code == 0
    panel_id = json.loads(out)["panel"]
    code, out, _ = h.invoke("--status", panel_id, "--json")
    assert code == 0
    panel = json.loads(out)
    member = panel["members"][0]
    item = {
        "path": str(folder),
        "reason": reason,
        "allow_path": None if startup else str(folder),
        "run": member["run"],
        "turn": 1,
    }
    assert member["denied_paths"] == [item]
    assert panel["denied_paths"] == [{**item, "member": 1}]
    assert json.loads(h.invoke("--status", "--json")[1])[0]["denied_paths"] == [
        {**item, "member": 1}
    ]
    # Keep the retained diagnostic when the underlying run record is unavailable.
    (runs.run_dir(member["run"], h.env) / "run.json").unlink()
    assert json.loads(h.invoke("--status", panel_id, "--json")[1])["denied_paths"] == [
        {**item, "member": 1}
    ]
    code, out, _ = h.invoke("--status", panel_id)
    assert code == 0
    assert f"denied {folder} ({reason}) run {member['run']} turn 1" in out


def test_status_preserves_legacy_hidden_startup_denial(h: Harness) -> None:
    panel_id = h.create()
    panel = record.load(panel_id, h.env)
    item = {
        "path": str(Path(h.env["HOME"]) / ".profile"),
        "reason": "hidden",
        "allow_path": None,
        "run": "codex-20260101-000000-abcdef",
        "turn": 1,
    }
    panel["members"][0]["denied_paths"] = [item]
    record.save(panel, h.env)
    code, out, _ = h.invoke("--status", panel_id, "--json")
    assert code == 0
    assert json.loads(out)["denied_paths"] == [{**item, "member": 1}]
    code, out, _ = h.invoke("--status", panel_id)
    assert code == 0
    assert f"denied {item['path']} (hidden) run {item['run']} turn 1" in out


@pytest.mark.parametrize("already_closed", [False, True])
def test_close_after_forced_member_close_with_sent_turn(
    h: Harness, already_closed: bool
) -> None:
    h.script("opencode", "@hang")
    code, out, err = h.start("--member", "opencode:a/b", "--stall", "0.002", "--json")
    assert code == 83, err
    panel_id = json.loads(out)["panel"]
    h.join()
    saved = runs.load(record.load(panel_id, h.env)["members"][0]["run"], h.env)
    assert saved.turn_state == runs.SENT
    assert Path(saved.worktree).is_dir()
    core_ = h.factory(h.env)
    if already_closed:
        # Older Ember versions closed the session but retained the sent worktree.
        tmux.kill(saved.id, env=h.env)
        runs.update(saved, {"status": runs.CLOSED})
    else:
        assert core_.close(saved.id, force=True) == 0
        assert not Path(saved.worktree).exists()
    code, _, err = h.invoke("--close", panel_id, "--json")
    assert code == 0, err
    saved = runs.load(saved.id, h.env)
    assert saved.status == runs.CLOSED and saved.turn_state == runs.SENT
    assert not Path(saved.worktree).exists()
    assert saved.worktree not in git(h.repo, "worktree", "list", env=h.env)
    assert record.load(panel_id, h.env)["members"][0]["closed"]


@pytest.mark.parametrize("last_exit", [71, 73])
@pytest.mark.parametrize("engine", ["opencode", "codex"])
@pytest.mark.parametrize("turn", [1, 2])
def test_panel_close_handles_failed_send_with_no_live_session(
    h: Harness, last_exit: int, engine: str, turn: int
) -> None:
    h.script(engine, "@hang")
    code, out, err = h.start("--member", f"{engine}:a/b", "--stall", "0.002", "--json")
    assert code == 83, err
    panel_id = json.loads(out)["panel"]
    h.join()
    saved = runs.load(record.load(panel_id, h.env)["members"][0]["run"], h.env)
    tmux.kill(saved.id, env=h.env)
    assert saved.turn_state == runs.SENT
    runs.update(saved, {"status": runs.FAILED, "last_exit": last_exit, "turn": turn})
    assert Path(saved.worktree).is_dir()
    code, out, err = h.invoke("--close", panel_id, "--json")
    assert code == 0, err
    assert json.loads(out)["members"][0]["closed"]
    assert runs.load(saved.id, h.env).status == runs.CLOSED
    assert not Path(saved.worktree).exists()
