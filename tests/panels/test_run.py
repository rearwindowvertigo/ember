import io
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember import choices
from ember.panels import brief, record, run
from ember.runs import record as runs
from ember.runs.core import Core, StartOptions
from panels.test_record import member  # pyrefly: ignore[missing-import]
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
)


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.env = make_env(tmp)
        self.repo = make_repo(tmp, self.env)
        self.id = record.new_id()
        self.scripts = tmp / "scripts.json"
        self.scripts.write_text("{}")
        self.drivers: dict[str, StubDriver] = {}
        for engine in ("codex", "opencode", "claude"):
            reply = tmp / f"{engine}.md"
            reply.write_text(f"Answer from {engine}: café\n")
            self.drivers[engine] = StubDriver(
                engine, script_path(self.env), reply_file=reply, scripts=self.scripts
            )
        self.threads: list[threading.Thread] = []
        self.err = io.StringIO()
        self.core_type: type[Core] = Core

    def factory(self, env: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        self.threads.append(threading.current_thread())
        return self.core_type(
            env, load_driver=self.drivers.__getitem__, poll_seconds=0.02, **kwargs
        )

    def create(
        self,
        count: int = 3,
        question: str = "Question?",
        **limits: Any,  # noqa: ANN401
    ) -> None:  # noqa: ANN401
        data = record.new_panel(
            self.id,
            dir=str(self.repo),
            question_file=str(self.tmp / "question.md"),
            read=[str(self.tmp)],
            claim="panel-claim",
            members=[member(n, engine) for n, engine in enumerate(self.drivers, 1)][
                :count
            ],
            left_out=[],
            **{"stall_minutes": 10, "timeout_minutes": 1, **limits},
        )
        with record.hold(self.id, self.env):
            record.create(data, question, brief.text(self.id, question), self.env)

    def panel(self) -> dict[str, Any]:
        return record.load(self.id, self.env)

    def start(self, **kwargs: Any) -> run.Outcome:  # noqa: ANN401
        kwargs.setdefault("spacing", 0)
        with record.hold(self.id, self.env):
            outcome = run.start(
                self.id,
                self.env,
                core_factory=self.factory,
                poll_seconds=0.02,
                err=self.err,
                **kwargs,
            )
        self.threads.extend(outcome.threads)
        return outcome

    def recover(self, **kwargs: Any) -> run.Outcome:  # noqa: ANN401
        kwargs.setdefault("stall_minutes", 10)
        kwargs.setdefault("timeout_minutes", 1)
        with record.hold(self.id, self.env):
            outcome = run.recover(
                self.id,
                self.env,
                core_factory=self.factory,
                poll_seconds=0.02,
                err=self.err,
                **kwargs,
            )
        self.threads.extend(outcome.threads)
        return outcome

    def script(self, **scripts: str) -> None:
        self.scripts.write_text(
            json.dumps(
                {f"for Ember run {engine}-": text for engine, text in scripts.items()}
            )
        )

    def release(self) -> None:
        for folder in record.folder(self.id, self.env).parent.glob("*"):
            if folder.name.startswith(("codex-", "opencode-", "claude-")):
                (folder / "stub").mkdir(exist_ok=True)
                (folder / "stub" / "release").touch()

    def join(self) -> None:
        for thread in set(self.threads):
            if thread is not threading.current_thread():
                thread.join(20)
                assert not thread.is_alive()


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


def wait_for(predicate: Callable[[], bool]) -> None:
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached")


def test_parallel_members_and_run_options(h: Harness) -> None:
    h.create()
    h.script(codex="@hang", opencode="@hang", claude="@hang")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.start)
        try:
            wait_for(lambda: all(item["run"] for item in h.panel()["members"]))
            wait_for(
                lambda: (
                    len(list(record.folder(h.id, h.env).parent.glob("*/stub/ready")))
                    == 3
                )
            )
            assert not future.done()
        finally:
            h.release()
        outcome = future.result(20)
    assert outcome.panel["exit"] == 0 and outcome.panel["status"] == "done"
    assert all(thread.daemon for thread in outcome.threads)
    summary = (record.folder(h.id, h.env) / "summary.md").read_text()
    for item in outcome.panel["members"]:
        assert item["status"] == "answered"
        saved = runs.load(item["run"], h.env)
        assert saved.status == "closed" and item["closed"]
        assert saved.effort == item["effort"] == "low"
        assert saved.read_only and saved.claim is None
        assert saved.read == (str(h.tmp),) and saved.trailers == ()
        assert saved.worktree == str(h.repo) and saved.branch is None
        assert saved.subject.startswith(f"Panel {h.id}: ")
        assert f"Answer from {item['engine']}: café" in summary
        assert item["bytes"] == len(Path(saved.reply or "").read_bytes())
        assert [a["kind"] for a in item["attempts"]] == ["start"]
        assert item["attempts"][0]["exit"] == 0
        log = record.folder(h.id, h.env) / "members" / f"{item['n']}.log"
        assert f"run {saved.id}" in log.read_text()
    assert "next:" not in h.err.getvalue()


@pytest.mark.parametrize("default", ["medium", None])
def test_member_effort_ignores_reviewer(h: Harness, default: str | None) -> None:
    class DefaultDriver(StubDriver):
        def default_effort(self, model: str) -> str | None:
            return default

    h.drivers["codex"] = DefaultDriver("codex", script_path(h.env))
    choices.set_settings({"single.reviewer": "codex:gpt-5"}, env=h.env)
    h.create(1)
    data = h.panel()
    data["members"][0].update(model="gpt-5", effort=None)
    with record.hold(h.id, h.env):
        record.save(data, h.env)
    outcome = h.start()
    assert outcome.panel["exit"] == 0
    assert runs.load(outcome.panel["members"][0]["run"], h.env).effort == default


@pytest.mark.parametrize(
    ("script", "reason", "code"),
    [
        ("@fail broken", "broken", 71),
        ("@empty", "ended with no reply", 80),
        ("@reply   ", "empty answer", 0),
    ],
)
def test_failure_keeps_other_answers(
    h: Harness, script: str, reason: str, code: int
) -> None:
    h.create()
    h.script(codex=script)
    outcome = h.start()
    first, *others = outcome.panel["members"]
    assert first["status"] == "failed" and reason in first["reason"]
    assert first["exit"] == code and first["closed"]
    assert all(item["status"] == "answered" for item in others)
    assert outcome.panel["exit"] == 83 and outcome.panel["status"] == "done"


def test_changed_paths_and_warnings(h: Harness) -> None:
    h.create(1)
    h.script(codex="@write notes.txt x")
    outcome = h.start()
    item = outcome.panel["members"][0]
    assert item["status"] == "answered" and item["changed"] == ["notes.txt"]
    assert outcome.panel["exit"] == 83
    assert (
        f"WARNING: member 1 (codex:model): read-only run {item['run']} "
        "changed notes.txt"
    ) in h.err.getvalue()


def test_stall_collected_once(h: Harness) -> None:
    h.create(1, stall_minutes=0.02)
    h.script(codex="@hang")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.start)
        try:
            wait_for(lambda: len(h.panel()["members"][0]["attempts"]) == 2)
            attempts = h.panel()["members"][0]["attempts"]
            assert attempts[0]["exit"] == 124 and attempts[1]["exit"] is None
        finally:
            h.release()
        outcome = future.result(20)
    item = outcome.panel["members"][0]
    assert item["status"] == "answered"
    assert [(a["kind"], a["exit"]) for a in item["attempts"]] == [
        ("start", 124),
        ("stall", 0),
    ]


def test_two_stalls_leave_run_open(h: Harness) -> None:
    h.create(1, stall_minutes=0.01)
    h.script(codex="@hang")
    outcome = h.start()
    item = outcome.panel["members"][0]
    assert item["status"] == "stalled" and not item["closed"]
    assert [a["exit"] for a in item["attempts"]] == [124, 124]
    assert outcome.panel["status"] == "incomplete" and outcome.panel["exit"] == 83


def test_timeout_freezes_artifacts_and_recovery_collects(h: Harness) -> None:
    h.create(2, timeout_minutes=0.06)
    h.script(codex="@hang")
    outcome = h.start()
    first, second = outcome.panel["members"]
    assert first["status"] == "timed out" and second["status"] == "answered"
    folder = record.folder(h.id, h.env)
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    stderr = h.err.getvalue()
    h.release()
    h.join()
    assert {p: p.read_bytes() for p in before} == before
    assert h.err.getvalue() == stderr
    assert runs.load(first["run"], h.env).status != "closed"
    recovered = h.recover()
    assert recovered.panel["exit"] == 0 and recovered.panel["status"] == "done"
    item = recovered.panel["members"][0]
    assert item["closed"] and item["attempts"][-1]["kind"] == "recover"
    with pytest.raises(run.NothingToRecover):
        h.recover()


def test_stop_leaves_unstarted_members(h: Harness) -> None:
    h.create()
    h.script(codex="@hang")
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.start, spacing=5, stop=stop)
        try:
            wait_for(lambda: h.panel()["members"][0]["run"] is not None)
        finally:
            stop.set()
        outcome = future.result(20)
    assert outcome.stopped and outcome.panel["exit"] == 74
    assert [m["status"] for m in outcome.panel["members"]] == [
        "interrupted",
        "not started",
        "not started",
    ]


def test_unexpected_member_error_does_not_stop_others(h: Harness) -> None:
    class BrokenCore(Core):
        def start(self, options: StartOptions) -> int:
            if options.engine == "opencode":
                raise RuntimeError("member failed unexpectedly")
            return super().start(options)

    h.core_type = BrokenCore
    h.create()
    outcome = h.start()
    assert [m["status"] for m in outcome.panel["members"]] == [
        "answered",
        "failed",
        "answered",
    ]
    assert outcome.panel["members"][1]["reason"] == (
        "unexpected error: RuntimeError: member failed unexpectedly"
    )


def test_no_run_recovery_reconciles_and_persists(h: Harness) -> None:
    h.create(2)
    data = h.panel()
    data["members"][0]["status"] = "starting"
    with record.hold(h.id, h.env):
        record.save(data, h.env)
    with pytest.raises(run.NothingToRecover):
        h.recover()
    data = h.panel()
    assert data["exit"] == 83 and data["status"] == "done"
    assert all(m["status"] == "failed" for m in data["members"])
    assert all(
        m["reason"] == "the panel ended before this member's run started"
        for m in data["members"]
    )
    assert "Status: failed" in (record.folder(h.id, h.env) / "summary.md").read_text()
    assert not h.threads


@pytest.mark.parametrize("failed", [False, True])
def test_closed_recovery_keeps_outcome_and_changes(h: Harness, failed: bool) -> None:
    h.create(1)
    h.script(codex="@fail original failure" if failed else "@write notes.txt x")
    data = h.start().panel
    data["status"] = "running"
    data["members"][0].update(status="running", changed=[])
    with record.hold(h.id, h.env):
        record.save(data, h.env)

    class NoRecover(Core):
        def recover(self, run_id: str) -> int:
            raise AssertionError("closed run was reopened")

    h.core_type = NoRecover
    outcome = h.recover()
    item = outcome.panel["members"][0]
    assert item["closed"] and item["status"] == ("failed" if failed else "answered")
    assert item["attempts"][-1]["exit"] == (71 if failed else 0)
    assert outcome.panel["exit"] == 83
    if failed:
        assert item["reason"] == "original failure"
    else:
        assert item["changed"] == ["notes.txt"]


def test_pending_member_in_crashed_panel_is_never_started(h: Harness) -> None:
    h.create(1)
    data = h.start().panel
    data["members"][0]["status"] = "running"
    data["members"].append(member(2, "opencode"))
    data["status"] = "running"
    with record.hold(h.id, h.env):
        record.save(data, h.env)
    outcome = h.recover()
    assert [m["status"] for m in outcome.panel["members"]] == ["answered", "failed"]
    assert not h.drivers["opencode"].commands


def test_failed_attempt_diagnostic_excludes_recovery_progress(h: Harness) -> None:
    h.create(1, stall_minutes=0.01)
    h.script(codex="@hang\n@fail final failure")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.start)
        try:
            wait_for(lambda: len(h.panel()["members"][0]["attempts"]) == 2)
        finally:
            h.release()
        outcome = future.result(20)
    assert outcome.panel["members"][0]["reason"] == "final failure"


def test_failed_attempt_keeps_readable_reply(h: Harness) -> None:
    class NonzeroCore(Core):
        def start(self, options: StartOptions) -> int:
            code = super().start(options)
            assert code == 0
            self._warn("emb run: a later failure")
            return 71

    h.core_type = NonzeroCore
    h.create(1)
    outcome = h.start()
    item = outcome.panel["members"][0]
    assert item["status"] == "failed" and item["exit"] == 71 and item["bytes"] > 0
    assert (
        "Answer from codex" in (record.folder(h.id, h.env) / "summary.md").read_text()
    )


def test_close_refusal_is_warning_and_not_failure(h: Harness) -> None:
    h.drivers["codex"].foreign = "owner message"
    h.create(1)
    outcome = h.start()
    item = outcome.panel["members"][0]
    assert outcome.panel["exit"] == 0 and item["status"] == "answered"
    assert not item["closed"] and "owner message" in item["close_error"]
    assert f"emb run --close {item['run']}" in h.err.getvalue()
    assert "next:" not in h.err.getvalue()


def test_delayed_thread_after_finalization_starts_nothing(
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h.create(1, timeout_minutes=0.001)
    release = threading.Event()
    original = run._Coordinator.job

    def delayed(self: run._Coordinator, job: run._Job, recovering: bool) -> None:
        assert release.wait(20)
        original(self, job, recovering)

    monkeypatch.setattr(run._Coordinator, "job", delayed)
    try:
        outcome = h.start()
        before = h.panel()
        assert before["members"][0]["status"] == "not started"
    finally:
        release.set()
    for thread in outcome.threads:
        thread.join(20)
    assert h.panel() == before
    assert not h.drivers["codex"].commands


def test_close_crossing_deadline_is_reconciled(h: Harness) -> None:
    entered, release = threading.Event(), threading.Event()

    class SlowClose(Core):
        def close(self, run_id: str, *, force: bool = False) -> int:
            entered.set()
            assert release.wait(20)
            return super().close(run_id, force=force)

    h.core_type = SlowClose
    h.create(1, timeout_minutes=0.06)
    try:
        outcome = h.start()
        assert entered.is_set()
        assert outcome.panel["members"][0]["status"] == "timed out"
        before = h.panel()
    finally:
        release.set()
    h.join()
    assert h.panel() == before
    assert runs.load(before["members"][0]["run"], h.env).status == "closed"
    assert h.recover().panel["exit"] == 0


@pytest.mark.parametrize("stopping", [True, False])
def test_active_recovery_gets_current_cutoff_status(h: Harness, stopping: bool) -> None:
    h.create(1, stall_minutes=0.01)
    h.script(codex="@hang")
    assert h.start().panel["members"][0]["status"] == "stalled"
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.recover, stop=stop, timeout_minutes=0.02)
        try:
            wait_for(
                lambda: h.panel()["members"][0]["attempts"][-1]["kind"] == "recover"
            )
        finally:
            if stopping:
                stop.set()
        outcome = future.result(20)
    assert outcome.panel["members"][0]["status"] == (
        "interrupted" if stopping else "timed out"
    )
    assert outcome.panel["exit"] == (74 if stopping else 83)


@pytest.mark.parametrize("operation", ["save", "write_summary", "append_log"])
def test_persistence_failure_surfaces_and_stops_scheduling(
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    h.create()
    folder = record.folder(h.id, h.env)
    original_summary = (folder / "summary.md").read_bytes()

    def fail(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise OSError("storage unavailable")

    monkeypatch.setattr(record, operation, fail)
    with pytest.raises(record.PanelError, match=f"{h.id}.*storage unavailable"):
        h.start(spacing=10)
    assert not h.drivers["opencode"].commands
    assert not h.drivers["claude"].commands
    assert (folder / "summary.md").read_bytes() == original_summary


def test_constructor_failure_is_recorded(h: Harness) -> None:
    h.create(1)

    def broken(env: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        raise RuntimeError("constructor failed")

    with record.hold(h.id, h.env):
        outcome = run.start(
            h.id, h.env, core_factory=broken, spacing=0, poll_seconds=0.01
        )
    for thread in outcome.threads:
        thread.join(20)
    item = outcome.panel["members"][0]
    assert item["status"] == "failed" and item["exit"] == 1
    assert item["reason"] == "unexpected error: RuntimeError: constructor failed"


def test_closed_stall_is_terminal_failure(h: Harness) -> None:
    h.create(1)
    data = h.start().panel
    item = data["members"][0]
    saved = runs.load(item["run"], h.env)
    runs.update(
        saved,
        {"last_exit": 124, "last_reason": "closed after stall"},
        allow_closed=True,
    )
    item.update(status="timed out", exit=124)
    with record.hold(h.id, h.env):
        record.save(data, h.env)
    outcome = h.recover()
    item = outcome.panel["members"][0]
    assert item["status"] == "failed" and item["exit"] == 124
    assert item["reason"] == "closed after stall"
    assert item["bytes"] > 0 and outcome.panel["status"] == "done"


def test_deadline_retains_reply_already_collected(h: Harness) -> None:
    release = threading.Event()

    class LateReturn(Core):
        def start(self, options: StartOptions) -> int:
            code = super().start(options)
            assert release.wait(20)
            return code

    h.core_type = LateReturn
    h.create(1, timeout_minutes=0.06)
    try:
        outcome = h.start()
        item = outcome.panel["members"][0]
        assert item["status"] == "timed out" and item["bytes"] > 0
        assert (
            "Answer from codex"
            in (record.folder(h.id, h.env) / "summary.md").read_text()
        )
    finally:
        release.set()
