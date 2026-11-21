import io
import json
import math
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember import choices, engines, prepare, tmux
from ember.panels import brief, record, run
from ember.runs import record as runs
from ember.runs.core import Core, StartOptions
from panels.test_record import member  # pyrefly: ignore[missing-import]
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
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
            stall_minutes=limits.get("stall_minutes", 10),
            timeout_minutes=limits.get("timeout_minutes", 1),
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

    def retry(self, number: str = "1", **kwargs: Any) -> run.Outcome:  # noqa: ANN401
        kwargs.setdefault("stall_minutes", 10)
        kwargs.setdefault("timeout_minutes", 1)
        with record.hold(self.id, self.env):
            outcome = run.retry(
                self.id,
                number,
                self.env,
                core_factory=self.factory,
                poll_seconds=0.02,
                err=self.err,
                **kwargs,
            )
        self.threads.extend(outcome.threads)
        return outcome

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


@pytest.mark.parametrize("command", [None, ""])
def test_unset_preparation_preserves_panel_records(
    h: Harness, command: str | None
) -> None:
    if command is not None:
        choices.set_settings({"worktree.prepare": {str(h.repo): command}}, env=h.env)
    h.create(1)
    outcome = h.start()
    assert outcome.panel["exit"] == 0
    assert "admission" not in outcome.panel
    saved = runs.load(outcome.panel["members"][0]["run"], h.env)
    assert saved.preparation is None
    assert not (saved.folder / "prepare.log").exists()


def test_prepares_every_member_before_admission_and_deadline(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    choices.set_settings({"worktree.prepare": {str(h.repo): "echo ready"}}, env=h.env)
    h.create()
    entered, release = threading.Event(), threading.Event()
    prepared = set()
    coordinators = []
    original_admit = run._Coordinator.admit

    class DelayedPreparation(Core):
        def _prepare(
            self, run: runs.Run, command: str, stop: threading.Event | None
        ) -> runs.Run:
            run = super()._prepare(run, command, stop)
            if run.engine == "opencode":
                entered.set()
                assert release.wait(20)
            prepared.add(run.engine)
            return run

    def admit(self: run._Coordinator, job: run._Job) -> None:
        coordinators.append(self)
        original_admit(self, job)

    original_session = tmux.new_session

    def session(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        assert prepared == set(h.drivers)
        saved, errors = runs.list_runs(h.env)
        assert len(saved) == 3 and not errors
        assert all((item.preparation or {}).get("state") == "ok" for item in saved)
        assert all(c.deadline >= admitted_at + 60 for c in coordinators)
        original_session(*args, **kwargs)

    h.core_type = DelayedPreparation
    monkeypatch.setattr(run._Coordinator, "admit", admit)
    monkeypatch.setattr(tmux, "new_session", session)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.start)
        try:
            assert entered.wait(20)
            wait_for(lambda: len(coordinators) == 2)
            assert all(math.isinf(c.deadline) for c in coordinators)
            assert h.panel()["admission"] == "preparing"
            assert not any(item[0] == "new-session" for item in tmux_log(h.tmp))
            assert not future.done()
        finally:
            admitted_at = time.monotonic()
            release.set()
        outcome = future.result(20)
    assert outcome.panel["exit"] == 0 and outcome.panel["admission"] == "admitted"


@pytest.mark.parametrize(
    "failure", ["exit 12", ": > stray", "timeout", "inspect", "start"]
)
def test_preparation_failure_stops_batch_and_keeps_artifacts(
    h: Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    failed = threading.Event()
    original_prepare = prepare.run

    class OrderedPreparation(Core):
        def _prepare(
            self, run: runs.Run, command: str, stop: threading.Event | None
        ) -> runs.Run:
            if run.engine == "opencode":
                assert failed.wait(20)
            return super()._prepare(run, command, stop)

    def preparing(
        command: str,
        worktree: str | Path,
        log: str | Path,
        **kwargs: Any,  # noqa: ANN401
    ) -> prepare.Result:
        if Path(worktree).name.startswith("opencode-"):
            if failure == "timeout":
                kwargs["timeout"] = 0.1
                command = "/bin/sleep 60"
            elif failure == "inspect":
                command = "/bin/rm .git"
            elif failure == "start":
                command = "true"
                worktree = Path(worktree) / "missing"
            else:
                command = failure
        result = original_prepare(command, worktree, log, **kwargs)
        if Path(worktree).name.startswith("codex-"):
            failed.set()
        return result

    choices.set_settings({"worktree.prepare": {str(h.repo): "echo ready"}}, env=h.env)
    h.core_type = OrderedPreparation
    h.create(2)
    monkeypatch.setattr(prepare, "run", preparing)
    outcome = h.start()
    first, second = outcome.panel["members"]
    assert outcome.panel["exit"] == 79 and outcome.panel["status"] == "incomplete"
    assert outcome.panel["admission"] == "stopped"
    assert first["status"] == "not started" and second["status"] == "failed"
    assert second["exit"] == 79
    assert not any(item[0] == "new-session" for item in tmux_log(h.tmp))
    before = h.panel()
    for item in outcome.panel["members"]:
        saved = runs.load(item["run"], h.env)
        assert saved.turn == 0 and saved.last_exit == 79 and not item["closed"]
        assert saved.status == runs.FAILED and Path(saved.worktree).is_dir()
        assert (saved.folder / "prepare.log").is_file()
        assert (record.folder(h.id, h.env) / "members" / f"{item['n']}.log").is_file()
    for number in ("1", "2"):
        with pytest.raises(run.RetryRefused, match="stopped admission"):
            h.retry(number)
    assert h.panel() == before
    assert h.recover().panel["exit"] == 79
    assert not any(item[0] == "new-session" for item in tmux_log(h.tmp))


@pytest.mark.parametrize("cancel", [False, True])
def test_stopping_ends_preparation_groups_and_never_admits(
    h: Harness, cancel: bool
) -> None:
    release = h.tmp / "fail-preparation"
    command = "/bin/sleep 60 & wait"
    if not cancel:
        command = (
            'case "$PWD" in *opencode-*) '
            f"while [ ! -f {shlex.quote(str(release))} ]; do /bin/sleep 0.02; done; "
            f"exit 12;; *) {command};; esac"
        )
    choices.set_settings({"worktree.prepare": {str(h.repo): command}}, env=h.env)
    h.create(2)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(h.start, stop=stop)
        try:
            wait_for(
                lambda: all(
                    item["run"]
                    and (runs.load(item["run"], h.env).preparation or {}).get("pid")
                    for item in h.panel()["members"]
                )
            )
        finally:
            if cancel:
                stop.set()
            else:
                release.touch()
        outcome = future.result(20)
    assert outcome.stopped == cancel and outcome.panel["exit"] == 79
    assert outcome.panel["status"] == "incomplete"
    for item in outcome.panel["members"]:
        saved = runs.load(item["run"], h.env)
        assert item["status"] == "failed" and item["exit"] == 79
        assert saved.preparation is not None
        expected = (
            "failed" if not cancel and saved.engine == "opencode" else "interrupted"
        )
        assert saved.preparation["state"] == expected
        assert prepare.start_ticks(saved.preparation["pid"]) is None
        assert Path(saved.worktree).is_dir()
    assert all(not thread.is_alive() for thread in outcome.threads)
    assert not any(item[0] == "new-session" for item in tmux_log(h.tmp))


def test_recovery_ends_abandoned_preparation_without_restarting(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    choices.set_settings({"worktree.prepare": {str(h.repo): "exit 12"}}, env=h.env)
    h.create(1)
    panel = h.start().panel
    item = panel["members"][0]
    saved = runs.load(item["run"], h.env)
    process = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        ticks = prepare.start_ticks(process.pid)
        assert ticks is not None
        runs.update(
            saved,
            {
                "status": runs.STARTING,
                "last_exit": None,
                "preparation": {
                    **(saved.preparation or {}),
                    "state": "running",
                    "pid": process.pid,
                    "start": ticks,
                },
            },
        )
        panel.update(status="running", admission="preparing", exit=None, ended_at=None)
        item.update(status="starting", exit=None)
        record.save(panel, h.env)

        def forbidden(*args: Any, **kwargs: Any) -> prepare.Result:  # noqa: ANN401
            raise AssertionError("recovery repeated preparation")

        monkeypatch.setattr(prepare, "run", forbidden)
        outcome = h.recover()
        assert outcome.panel["status"] == "incomplete" and outcome.panel["exit"] == 79
        assert outcome.panel["members"][0]["status"] == "failed"
        assert process.wait(timeout=5) < 0
        saved = runs.load(saved.id, h.env)
        assert saved.status == runs.FAILED and saved.last_exit == 79
        assert (saved.preparation or {})["state"] == "interrupted"
        assert Path(saved.worktree).is_dir()
        assert not any(item[0] == "new-session" for item in tmux_log(h.tmp))
        with pytest.raises(run.RetryRefused, match="stopped admission"):
            h.retry()
    finally:
        process.kill()
        process.wait(timeout=5)


def test_parallel_members_and_run_options(h: Harness) -> None:
    choices.set_settings(
        {"shortlist.codex": ["model@low"], "fast.codex": ["model"]}, env=h.env
    )
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
        assert (
            item["speed"]
            == saved.speed
            == ("fast" if saved.engine == "codex" else None)
        )
        if saved.engine == "codex":
            assert "codex:model · low · fast · answered" in run.member_line(item)
        assert saved.read_only and saved.claim is None
        assert saved.read == (str(h.tmp),) and saved.trailers == ()
        assert saved.worktree != str(h.repo) and saved.branch is None
        assert saved.detached and not Path(saved.worktree).exists()
        assert saved.base == outcome.panel["base"]
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
    assert first["status"] == "failed" and reason in first["reason"], first
    assert first["exit"] == code and first["closed"], first
    assert all(item["status"] == "answered" for item in others), others
    assert outcome.panel["exit"] == 83 and outcome.panel["status"] == "done"


def test_changed_paths_and_warnings(h: Harness) -> None:
    h.create(1)
    h.script(codex="@write notes.txt x")
    outcome = h.start()
    item = outcome.panel["members"][0]
    saved = runs.load(item["run"], h.env)
    changed = str(Path(saved.worktree) / "notes.txt")
    assert item["status"] == "answered" and item["changed"] == [changed]
    assert Path(changed).exists() and item["closed"]
    assert outcome.panel["exit"] == 83
    assert (
        f"WARNING: member 1 (codex:model): read-only run {item['run']} "
        f"changed {changed}"
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
    assert item["seconds"] > first["seconds"]
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
        saved = runs.load(item["run"], h.env)
        assert item["changed"] == [str(Path(saved.worktree) / "notes.txt")]


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
            active = h.panel()["members"][0]
            assert active["status"] == "running"
            assert active["seconds"] > 0
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


def start_unrecorded_member(h: Harness) -> tuple[runs.Run, int]:
    core = h.factory(h.env, out=io.StringIO(), err=io.StringIO())
    code = core.execute(
        lambda: core.start(
            StartOptions(
                "codex",
                dir=str(h.repo),
                task_file=str(record.folder(h.id, h.env) / record.BRIEF_NAME),
                model="model",
                effort="low",
                read_only=True,
            )
        )
    )
    saved, errors = runs.list_runs(h.env)
    assert len(saved) == 1 and not errors
    data = h.panel()
    data["members"][0].update(
        status="running",
        run=saved[0].id,
        attempts=[{"kind": "start", "at": time.time(), "exit": None}],
    )
    with record.hold(h.id, h.env):
        record.save(data, h.env)
    return saved[0], code


def test_recovery_settles_collected_reply_after_stall(h: Harness) -> None:
    h.create(1)
    saved, code = start_unrecorded_member(h)
    assert code == 0 and saved.turn_state == runs.COLLECTED
    runs.update(saved, {"last_exit": 124, "last_reason": "earlier wait stalled"})

    outcome = h.recover()

    item = outcome.panel["members"][0]
    assert item["status"] == "answered" and item["exit"] == 0
    assert item["reason"] is None and item["closed"] and item["bytes"] > 0
    assert item["attempts"][-1]["exit"] == 0
    assert runs.load(saved.id, h.env).status == runs.CLOSED
    assert outcome.panel["status"] == "done" and outcome.panel["exit"] == 0
    assert (
        "Answer from codex"
        in (record.folder(h.id, h.env) / record.SUMMARY_NAME).read_text()
    )


def test_recovery_preserves_failure_before_turn_started(h: Harness) -> None:
    class FailedLaunch(StubDriver):
        def command(self, run: runs.Run, first_message: str) -> engines.LaunchSpec:
            raise engines.DriverError("launch failed before the first turn")

    h.drivers["codex"] = FailedLaunch("codex", script_path(h.env))
    h.create(1)
    saved, code = start_unrecorded_member(h)
    assert code == saved.last_exit == 71 and saved.turn_state is None

    outcome = h.recover()

    item = outcome.panel["members"][0]
    assert item["status"] == "failed" and item["exit"] == 71
    assert item["reason"] == saved.last_reason and item["closed"]
    assert item["attempts"][-1]["exit"] == 71
    assert runs.load(saved.id, h.env).status == runs.CLOSED
    assert outcome.panel["status"] == "done" and outcome.panel["exit"] == 83


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


@pytest.mark.parametrize("status", ["failed", "stalled", "timed out", "interrupted"])
def test_retry_replaces_answer_and_keeps_old_run(h: Harness, status: str) -> None:
    h.create(2)
    h.script(codex="@fail old failure")
    original = h.start().panel
    old = original["members"][0]
    old["status"] = status
    old["changed"] = ["old-evidence"]
    record.save(original, h.env)
    h.script(codex="@reply replacement")

    class CheckedCore(Core):
        def close(self, run_id: str, *, force: bool = False) -> int:
            assert not force and record.live(h.id, h.env)
            return super().close(run_id, force=force)

        def start(self, options: StartOptions) -> int:
            assert record.live(h.id, h.env)
            assert runs.load(old["run"], h.env).status == runs.CLOSED
            panel = h.panel()
            assert panel["status"] == "running" and panel["ended_at"] is None
            assert panel["exit"] is None
            assert panel["members"][0]["seconds"] is None
            assert panel["members"][0]["status"] == "starting"
            assert options.read_only and options.dir == str(h.repo)
            assert options.engine == old["engine"] and options.model == old["model"]
            assert options.effort == old["effort"]
            assert options.read == (str(h.tmp),)
            assert Path(options.task_file or "").read_text() == brief.text(
                h.id, "Question?"
            )
            return super().start(options)

    h.core_type = CheckedCore
    outcome = h.retry()
    assert outcome.panel == h.panel()
    assert outcome.panel["status"] == "done" and outcome.panel["exit"] == 0
    assert outcome.panel["ended_at"] >= original["ended_at"]
    first, second = outcome.panel["members"]
    assert second == original["members"][1]
    assert first["run"] != old["run"] and first["changed"] == []
    assert first["attempts"][:-1] == old["attempts"]
    retained = first["attempts"][-1]
    assert retained["kind"] == "retry" and retained["run"] == old["run"]
    assert retained["exit"] == 0
    summary = (record.folder(h.id, h.env) / "summary.md").read_text()
    assert "replacement" in summary and "old failure" not in summary
    assert old["run"] not in summary and "Answer from opencode" in summary


def test_retry_not_started_and_failed_result(h: Harness) -> None:
    h.create(2)
    data = h.panel()
    for item in data["members"]:
        item["status"] = "not started"
    record.save(data, h.env)
    h.script(codex="@fail another failure")
    outcome = h.retry()
    assert outcome.panel["status"] == "incomplete" and outcome.panel["exit"] == 83
    assert outcome.panel["members"][1] == data["members"][1]
    assert outcome.panel["members"][0]["attempts"][0]["run"] is None
    h.script(codex="@reply answered now")
    outcome = h.retry()
    assert outcome.panel["status"] == "incomplete" and outcome.panel["exit"] == 83
    outcome = h.retry("2")
    assert outcome.panel["status"] == "done" and outcome.panel["exit"] == 0


@pytest.mark.parametrize("interrupt", [False, True])
def test_retry_timeout_and_interrupt(h: Harness, interrupt: bool) -> None:
    h.create(1)
    data = h.panel()
    data["members"][0]["status"] = "not started"
    record.save(data, h.env)
    h.script(codex="@hang")
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            h.retry, stop=stop, timeout_minutes=1 if interrupt else 0.03
        )
        wait_for(lambda: h.panel()["members"][0]["run"] is not None)
        assert record.live(h.id, h.env)
        assert h.panel()["ended_at"] is None
        if interrupt:
            stop.set()
        outcome = future.result(20)
    assert outcome.panel["status"] == "incomplete"
    assert outcome.panel["exit"] == (74 if interrupt else 83)
    assert outcome.panel["members"][0]["status"] == (
        "interrupted" if interrupt else "timed out"
    )
    assert outcome.panel["ended_at"] is not None
    h.release()
    h.join()
    assert outcome.panel == h.panel()


def test_recovery_ignores_run_retained_only_by_retry(h: Harness) -> None:
    h.create(1)
    data = h.panel()
    data["members"][0].update(
        status="failed",
        attempts=[{"kind": "retry", "at": 1, "exit": 74, "run": "codex-old"}],
    )
    record.save(data, h.env)

    class NoRecovery(Core):
        def recover(self, run_id: str) -> int:
            raise AssertionError("retained run must not be recovered")

    h.core_type = NoRecovery
    with pytest.raises(run.NothingToRecover):
        h.recover()
    assert h.panel()["members"] == data["members"]


def test_close_current_and_retained_runs_continues_after_refusal(h: Harness) -> None:
    h.create(3)
    data = h.start().panel
    current, retained, failed = [m["run"] for m in data["members"]]
    for run_id, status in [
        (current, runs.WAITING),
        (retained, runs.WAITING),
        (failed, runs.FAILED),
    ]:
        runs.update(runs.load(run_id, h.env), {"status": status}, allow_closed=True)
    first = data["members"][0]
    first["closed"] = True
    first["attempts"].extend(
        [
            {"kind": "retry", "at": 1, "exit": 74, "run": retained},
            {"kind": "retry", "at": 2, "exit": 71, "run": failed},
            {"kind": "retry", "at": 3, "exit": 74, "run": retained},
        ]
    )
    data["members"] = [first]
    record.save(data, h.env)
    closed = []

    class RefuseCurrent(Core):
        def close(self, run_id: str, *, force: bool = False) -> int:
            assert not force and record.live(h.id, h.env)
            closed.append(run_id)
            if run_id == current:
                print("emb run: current turn is running", file=self.err)
                return 75
            assert h.panel()["members"][0]["close_error"]
            return super().close(run_id)

    h.core_type = RefuseCurrent
    with record.hold(h.id, h.env):
        results = run.close(h.id, h.env, core_factory=h.factory)
    assert closed == [current, retained]
    assert [r["closed"] for r in results] == [False, True]
    assert not h.panel()["members"][0]["closed"]
    assert "current turn is running" in h.panel()["members"][0]["close_error"]
    assert runs.load(retained, h.env).status == runs.CLOSED
    assert runs.load(failed, h.env).status == runs.FAILED
    h.core_type = Core
    with record.hold(h.id, h.env):
        results = run.close(h.id, h.env, core_factory=h.factory)
    assert len(results) == 1 and results[0]["closed"]
    assert h.panel()["members"][0]["closed"]
    assert h.panel()["members"][0]["close_error"] is None
    with record.hold(h.id, h.env):
        assert run.close(h.id, h.env, core_factory=h.factory) == []


def test_members_use_one_commit_when_source_head_moves(h: Harness) -> None:
    h.create(2)
    original = git(h.repo, "rev-parse", "HEAD", env=h.env)

    class MoveHead(Core):
        def start(self, options: StartOptions) -> int:
            if options.engine == "codex":
                (h.repo / "README.md").write_text("lead committed edit\n")
                git(h.repo, "commit", "-qam", "lead edit", env=h.env)
            return super().start(options)

    h.core_type = MoveHead
    outcome = h.start(spacing=0.2)
    assert outcome.panel["exit"] == 0
    assert outcome.panel["base"] == original
    assert git(h.repo, "rev-parse", "HEAD", env=h.env) != original
    for item in outcome.panel["members"]:
        saved = runs.load(item["run"], h.env)
        assert saved.base == original and saved.detached


def test_retry_reads_original_commit_after_source_head_moves(h: Harness) -> None:
    h.create(1)
    h.script(codex="@fail first attempt")
    first = h.start().panel
    original = first["base"]
    (h.repo / "README.md").write_text("new version\n")
    git(h.repo, "commit", "-qam", "next commit", env=h.env)
    h.script(codex="@reply retry answer")
    observed = []

    def read_tree(saved: runs.Run) -> None:
        observed.append(Path(saved.worktree, "README.md").read_text())

    h.drivers["codex"].on_wait = read_tree
    outcome = h.retry()
    saved = runs.load(outcome.panel["members"][0]["run"], h.env)
    assert outcome.panel["exit"] == 0
    assert saved.base == outcome.panel["base"] == original
    assert observed and set(observed) == {"hello\n"}


def test_panel_close_removes_open_member_worktree(h: Harness) -> None:
    h.create(1, stall_minutes=0.01)
    h.script(codex="@hang")
    item = h.start().panel["members"][0]
    saved = runs.load(item["run"], h.env)
    assert Path(saved.worktree).exists()
    h.release()
    h.recover()
    assert not Path(saved.worktree).exists()
    assert saved.worktree not in git(h.repo, "worktree", "list", env=h.env)


def test_panel_close_removes_detached_worktree(h: Harness) -> None:
    h.drivers["codex"].foreign = "owner message"
    h.create(1)
    item = h.start().panel["members"][0]
    saved = runs.load(item["run"], h.env)
    assert not item["closed"] and Path(saved.worktree).exists()
    h.drivers["codex"].foreign = None
    with record.hold(h.id, h.env):
        results = run.close(h.id, h.env, core_factory=h.factory)
    assert len(results) == 1 and results[0]["closed"]
    assert not Path(saved.worktree).exists()
    assert saved.worktree not in git(h.repo, "worktree", "list", env=h.env)


@pytest.mark.parametrize("status", [runs.CLOSED, runs.FAILED])
@pytest.mark.parametrize("retained", [False, True])
def test_close_cleans_terminal_member_worktree_after_changes_are_removed(
    h: Harness, status: str, retained: bool
) -> None:
    h.create(1)
    h.script(codex="@write notes.txt evidence\n@fail first attempt")
    item = h.start().panel["members"][0]
    saved = runs.load(item["run"], h.env)
    evidence = Path(saved.worktree) / "notes.txt"
    assert saved.status == runs.CLOSED and evidence.exists()
    if retained:
        h.script(codex="@reply replacement")
        replacement = h.retry().panel["members"][0]
        assert replacement["run"] != saved.id
        assert any(a.get("run") == saved.id for a in replacement["attempts"])
    runs.update(saved, {"status": status}, allow_closed=True)

    class NoSessionClose(Core):
        def close(self, run_id: str, *, force: bool = False) -> int:
            raise AssertionError("terminal run's session was closed again")

    h.core_type = NoSessionClose
    errors = io.StringIO()
    with record.hold(h.id, h.env):
        results = run.close(h.id, h.env, core_factory=h.factory, err=errors)
    assert [r["run"] for r in results] == [saved.id]
    assert results[0]["closed"]
    assert evidence.read_text() == "evidence\n"
    command = f"git worktree remove --force {saved.worktree}"
    assert command in errors.getvalue()
    log = record.folder(h.id, h.env) / "members" / "1.log"
    assert command in log.read_text()
    assert runs.load(saved.id, h.env).status == status
    assert runs.load(saved.id, h.env).worktree == saved.worktree

    evidence.unlink()
    errors = io.StringIO()
    with record.hold(h.id, h.env):
        results = run.close(h.id, h.env, core_factory=h.factory, err=errors)
    assert [r["run"] for r in results] == [saved.id]
    assert results[0]["closed"] and not errors.getvalue()
    assert not Path(saved.worktree).exists()
    assert saved.worktree not in git(h.repo, "worktree", "list", env=h.env)
    assert runs.load(saved.id, h.env).status == status
    assert h.panel()["members"][0]["closed"]
    with record.hold(h.id, h.env):
        assert run.close(h.id, h.env, core_factory=h.factory) == []


@pytest.mark.parametrize("startup", [False, True])
def test_member_denial_is_kept_in_diagnostics_and_record(
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
    h.create(count=1)
    panel = h.panel()
    panel["read"] = []
    record.save(panel, h.env)
    h.script(codex=f"@reply bash: {denied}: Permission denied")
    panel = h.start().panel
    member = panel["members"][0]
    assert member["denied_paths"] == [
        {
            "path": str(folder),
            "reason": reason,
            "allow_path": None if startup else str(folder),
            "run": member["run"],
            "turn": 1,
        }
    ]
    log = record.folder(h.id, h.env) / "members" / "1.log"
    assert f"WARNING: {member['run']} was denied {folder}" in log.read_text()
    assert ("Settings, Sandbox read paths" in log.read_text()) is not startup
    assert ("owner:" in log.read_text()) is not startup
    assert f"was denied {folder}" in h.err.getvalue()


@pytest.mark.parametrize("retained", [False, True])
def test_close_keeps_member_with_uncertain_delivery(h: Harness, retained: bool) -> None:
    h.create(1)
    h.script(codex="@write evidence.txt keep\n@fail attempt")
    item = h.start().panel["members"][0]
    saved = runs.load(item["run"], h.env)
    if retained:
        h.script(codex="@reply replacement")
        h.retry()
    runs.update(
        saved,
        {
            "status": runs.FAILED,
            "turn_state": runs.SENT,
            "last_reason": "capacity retry delivery is uncertain; ask the lead",
        },
        allow_closed=True,
    )
    (Path(saved.worktree) / "evidence.txt").unlink()
    with record.hold(h.id, h.env):
        results = run.close(h.id, h.env, core_factory=h.factory, err=h.err)
    [result] = [r for r in results if r["run"] == saved.id]
    assert not result["closed"]
    assert "delivery is uncertain" in result["close_error"]
    assert Path(saved.worktree).exists()
    assert saved.worktree in git(h.repo, "worktree", "list", env=h.env)
    assert runs.load(saved.id, h.env).status == runs.FAILED
    assert not h.panel()["members"][0]["closed"]
    assert "delivery is uncertain" in h.panel()["members"][0]["close_error"]


def test_close_keeps_terminal_member_while_collection_lock_is_held(h: Harness) -> None:
    h.create(1)
    h.script(codex="@write evidence.txt keep\n@fail attempt")
    item = h.start().panel["members"][0]
    saved = runs.load(item["run"], h.env)
    (Path(saved.worktree) / "evidence.txt").unlink()
    with record.hold(h.id, h.env), Core(h.env)._turn_lock(saved.id):
        [result] = run.close(h.id, h.env, core_factory=h.factory)
    assert not result["closed"]
    assert "another emb run command" in result["close_error"]
    assert Path(saved.worktree).exists()


@pytest.mark.parametrize("retry", [False, True])
def test_legacy_panel_start_and_retry_keep_openrouter_routing(
    h: Harness, retry: bool
) -> None:
    h.create(count=2)
    data = h.panel()
    data["members"][1]["model"] = "anthropic/claude-x"
    if retry:
        data["members"][1]["status"] = "failed"
    data.pop("opencode_id_format")
    path = record.folder(h.id, h.env) / record.PANEL_NAME
    path.write_text(json.dumps(data))
    result = h.retry("2") if retry else h.start()
    member = result.panel["members"][1]
    assert member["model"] == "openrouter/anthropic/claude-x"
    assert runs.load(member["run"], h.env).model == "openrouter/anthropic/claude-x"
