import fcntl
import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot

from ember import paths
from ember.app import flows
from ember.app.tasks import TaskRunner
from ember.flows import lock
from ember.flows import record as flow_record
from ember.flows.plan import Plan, Task, Worker
from ember.panels import record as panel_record

FLOW = "flow-20260101-120000-aaaaaa"
PANEL = "panel-20260101-120000-bbbbbb"


def make_flow(
    env: dict[str, str], flow_id: str = FLOW, *, count: int = 2, **changes: object
) -> dict[str, Any]:
    worker = Worker("codex", "test-model", "high", "option")
    plan = Plan(
        Path(env["HOME"]) / "plan.json",
        "{}",
        "/work/project",
        "HEAD",
        (),
        worker,
        worker,
        2,
        4,
        (),
        60,
        "",
        "",
        (),
        tuple(Task(f"task-{n}", f"Task {n}", "Work", (), (), "") for n in range(count)),
    )
    result = flow_record.new_flow(
        flow_id,
        plan,
        top="/work/project",
        main="/work/project",
        worktree="/data/ember/worktrees/" + flow_id,
        base="a" * 40,
        writer=worker,
        reviewer=worker,
        stall_minutes=10,
        claim=None,
        now=100,
    )
    result.update(changes)
    flow_record.create(result, "{}", env)
    return result


def make_member(n: int = 1, **changes: object) -> dict[str, Any]:
    member: dict[str, Any] = {
        "n": n,
        "engine": "codex",
        "model": "test-model",
        "effort": None,
        "source": "option",
        "status": "pending",
        "run": None,
        "exit": None,
        "reason": None,
        "reply": None,
        "bytes": 0,
        "changed": [],
        "seconds": None,
        "closed": False,
        "close_error": None,
        "attempts": [],
    }
    member.update(changes)
    return member


def make_panel(
    env: dict[str, str],
    panel_id: str = PANEL,
    *,
    question: str = "\nA question\nMore",
    **changes: object,
) -> dict[str, Any]:
    result = panel_record.new_panel(
        panel_id,
        dir="/work/project",
        question_file="/work/question.md",
        read=(),
        claim=None,
        stall_minutes=10,
        timeout_minutes=20,
        members=[make_member()],
        left_out=[],
        now=100,
    )
    result.update(changes)
    panel_record.create(result, question, "brief", env)
    return result


@pytest.fixture
def proc_locks(tmp_path: Path) -> Path:
    path = tmp_path / "proc-locks"
    path.write_text("")
    return path


def lock_line(path: Path) -> str:
    info = path.stat()
    return (
        f"1: FLOCK ADVISORY WRITE 123 {os.major(info.st_dev):02x}:"
        f"{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n"
    )


@pytest.mark.parametrize("status", flow_record.TASK_STATUSES)
def test_plan_wave_active_statuses(status: str) -> None:
    """Only active task statuses contribute a plan wave."""
    flow = {
        "plan_waves": [["a"], ["b", "c"], ["d"]],
        "wave": 27,
        "tasks": [{"id": "b", "status": status, "wave": 25}],
    }
    active = {
        "writing",
        "reviewing",
        "fixing",
        "accepted",
        "merging",
        "stopped",
        "conflict",
        "blocked",
    }
    assert flows.plan_wave(flow) == ((2, 3) if status in active else None)


def test_plan_wave_uses_highest_stored_position() -> None:
    """Task order, pass counts and dropped tasks retain stored wave positions."""
    flow = {
        "plan_waves": [["a"], ["b"], ["c"], ["d"]],
        "wave": 99,
        "tasks": [
            {"id": "d", "status": "dropped", "wave": 98},
            {"id": "c", "status": "blocked", "wave": None},
            {"id": "a", "status": "writing", "wave": 99},
            {"id": "b", "status": "merged", "wave": 2},
        ],
    }
    assert flows.plan_wave(flow) == (3, 4)
    flow["tasks"][1]["status"] = "merged"
    assert flows.plan_wave(flow) == (1, 4)


@pytest.mark.parametrize("waves", [None, [], [["other"]]])
def test_plan_wave_without_matching_wave(waves: list[list[str]] | None) -> None:
    """A task absent from stored waves has no plan wave."""
    flow: dict[str, Any] = {"tasks": [{"id": "a", "status": "writing"}]}
    if waves is not None:
        flow["plan_waves"] = waves
    assert flows.plan_wave(flow) is None


def test_plan_wave_without_tasks() -> None:
    """A flow without tasks has no plan wave."""
    assert flows.plan_wave({"plan_waves": [["a"]], "tasks": []}) is None


def test_missing_records_create_nothing(env: dict[str, str], proc_locks: Path) -> None:
    result = flows.read(env, proc_locks=proc_locks)
    assert result.flows == result.panels == []
    assert result.unreadable_flows == result.unreadable_panels == 0
    assert not paths.state_dir(env).exists()
    assert not paths.runtime_dir(env).exists()


def test_order_all_ended_and_broken_records(
    env: dict[str, str], proc_locks: Path
) -> None:
    for n, status in enumerate(("stopped", "running", "starting") + ("done",) * 7):
        make_flow(
            env,
            f"flow-20260101-120000-{n:06x}",
            status=status,
            started_at=100 - n,
            ended_at=n if status == "done" else None,
        )
    for n, status in enumerate(("running",) * 3 + ("done", "incomplete") * 4):
        make_panel(
            env,
            f"panel-20260101-120000-{n:06x}",
            status=status,
            started_at=100 - n,
            ended_at=n if status != "running" else None,
        )
    for prefix, name in (("flow", "flow.json"), ("panel", "panel.json")):
        folder = paths.runs_dir(env) / f"{prefix}-20260101-120000-ffffff"
        folder.mkdir()
        (folder / name).write_text("broken")
    (paths.runs_dir(env) / "codex-20260101-120000-ffffff").mkdir()
    result = flows.read(env, proc_locks=proc_locks)
    assert [int(item.record["id"][-6:], 16) for item in result.flows] == [
        2,
        1,
        0,
        9,
        8,
        7,
        6,
        5,
        4,
        3,
    ]
    assert [int(item.record["id"][-6:], 16) for item in result.panels] == [
        2,
        1,
        0,
        10,
        9,
        8,
        7,
        6,
        5,
        4,
        3,
    ]
    assert result.unreadable_flows == result.unreadable_panels == 1
    assert result.panels[0].question == "A question\nMore"
    assert len(result.watch) == 1 + 11 * 2 + 12 * 2 + 10


@pytest.mark.parametrize(
    ("status", "age", "gone"),
    [
        ("running", 61, True),
        ("starting", 59, False),
        ("checking", 60, False),
        ("stopped", 100, False),
        ("done", 100, False),
    ],
)
def test_flow_gone(
    env: dict[str, str], proc_locks: Path, status: str, age: int, gone: bool
) -> None:
    make_flow(env, status=status)
    item = flows.read(env, proc_locks=proc_locks, now=100 + age).flows[0]
    assert item.held is False
    assert item.gone is gone


def test_held_and_unknown_locks(
    env: dict[str, str], proc_locks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_flow(env, status="running")
    make_panel(env)
    path = lock.path(FLOW, env)
    path.parent.mkdir(parents=True)
    path.touch()
    proc_locks.write_text(lock_line(path))
    snapshot = flows.read(env, proc_locks=proc_locks, now=200)
    assert snapshot.flows[0].held is True
    assert not snapshot.flows[0].gone
    assert snapshot.panels[0].gone
    proc_locks.unlink()
    unknown = flows.read(env, proc_locks=proc_locks, now=200)
    assert unknown.flows[0].held is unknown.panels[0].held is None
    assert not unknown.flows[0].gone and not unknown.panels[0].gone

    def unavailable(_env: object) -> Path:
        raise paths.RuntimeDirError("unavailable")

    proc_locks.write_text("")
    monkeypatch.setattr(paths, "runtime_dir", unavailable)
    unknown = flows.read(env, proc_locks=proc_locks, now=200)
    assert unknown.flows[0].held is unknown.panels[0].held is None


def test_no_lock_probes_or_writes(
    env: dict[str, str], proc_locks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_flow(env)
    make_panel(env)
    root = paths.runs_dir(env)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("record reads must not acquire or probe locks")

    with monkeypatch.context() as patch:
        for module in (lock, panel_record):
            patch.setattr(module, "live", forbidden)
            patch.setattr(module, "hold", forbidden)
        patch.setattr(fcntl, "flock", forbidden)
        flows.read(env, proc_locks=proc_locks)
    assert not paths.runtime_dir(env).exists()
    assert before == {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    with lock.hold(FLOW, env), panel_record.hold(PANEL, env):
        pass


def test_watch_replace_and_activation(
    env: dict[str, str], proc_locks: Path, qtbot: QtBot
) -> None:
    flow = make_flow(env)
    panel = make_panel(env)
    tasks = TaskRunner()
    model = flows.FlowsModel(env, tasks, proc_locks=proc_locks)
    model.set_active(True)
    model.set_active(True)
    assert not model._queued
    qtbot.waitUntil(model.loaded)
    expected = flows.read(env, proc_locks=proc_locks).watch
    assert set(model.watched()) == set(expected)
    flow["status"] = "stopped"
    with qtbot.waitSignal(model.changed, timeout=5000):
        flow_record.save(flow, env)
    qtbot.waitUntil(
        lambda: str(flow_record.folder(FLOW, env) / "flow.json") in model.watched()
    )
    panel["status"] = "done"
    with qtbot.waitSignal(model.changed, timeout=5000):
        panel_record.save(panel, env)
    qtbot.waitUntil(lambda: tasks.pending() == 0)
    model.set_active(False)
    assert not model.watched() and not model._timer.isActive()
    with qtbot.assertNotEmitted(model.changed, wait=100):
        flow["status"] = "running"
        flow_record.save(flow, env)
        model._path_changed(str(paths.runs_dir(env)))
        model.reload()
    with qtbot.waitSignal(model.changed, timeout=5000):
        model.set_active(True)
    assert model.flows()[0].record["status"] == "running"
    model.set_active(False)


def test_coalescing_and_inactive_completion(
    env: dict[str, str], proc_locks: Path, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    reads: list[int] = []
    original = flows.read

    def blocked(
        env: Mapping[str, str],
        *,
        proc_locks: Path = flows.PROC_LOCKS,
        now: float | None = None,
    ) -> flows.Snapshot:
        reads.append(1)
        if len(reads) == 1:
            entered.set()
            assert release.wait(5)
        return original(env, proc_locks=proc_locks, now=now)

    monkeypatch.setattr(flows, "read", blocked)
    tasks = TaskRunner()
    model = flows.FlowsModel(env, tasks, proc_locks=proc_locks)
    model.set_active(True)
    qtbot.waitUntil(entered.is_set)
    model.reload()
    model.reload()
    release.set()
    qtbot.waitUntil(lambda: tasks.pending() == 0)
    assert len(reads) == 2
    model.set_active(False)
    model.set_active(True)
    model.set_active(False)
    qtbot.waitUntil(lambda: tasks.pending() == 0)
    assert model.loaded()
    assert model.watched() == []


def test_folder_failure_and_recovery(
    env: dict[str, str], proc_locks: Path, qtbot: QtBot
) -> None:
    root = paths.runs_dir(env)
    root.parent.mkdir(parents=True)
    root.write_text("not a folder")
    model = flows.FlowsModel(env, TaskRunner(), proc_locks=proc_locks)
    with qtbot.waitSignal(model.failed, timeout=5000):
        model.set_active(True)
    assert model.error()
    root.unlink()
    with qtbot.waitSignal(model.changed, timeout=5000):
        model.reload()
    assert model.error() is None
    model.set_active(False)


@pytest.mark.parametrize("status", flow_record.ENDED)
def test_all_ended_flow_states(
    env: dict[str, str], proc_locks: Path, status: str
) -> None:
    make_flow(env, status=status, ended_at=None)
    item = flows.read(env, proc_locks=proc_locks).flows[0]
    assert item.record["status"] == status
    assert not item.gone


def test_nested_ids_match_prune(env: dict[str, str], proc_locks: Path) -> None:
    flow = make_flow(env)
    flow["tasks"][0]["writer_run"] = "writer"
    flow["tasks"][0]["reviews"] = [{"reviewer_run": "old-review"}]
    flow_record.save(flow, env)
    events = flow_record.folder(FLOW, env) / flow_record.EVENTS_NAME
    events.write_text(json.dumps({"seq": 1, "writer_run": "old-writer"}) + "\n")
    panel = make_panel(env)
    panel["members"][0].update(
        run="member",
        attempts=[{"kind": "start", "at": 100, "exit": 0, "run": "old-member"}],
    )
    panel_record.save(panel, env)
    snapshot = flows.read(env, proc_locks=proc_locks)
    assert snapshot.flows[0].run_ids == {"writer", "old-review", "old-writer"}
    assert snapshot.panels[0].run_ids == {"member", "old-member"}
    assert str(events) in snapshot.watch


def test_flow_run_history(env: dict[str, str], proc_locks: Path) -> None:
    flow = make_flow(env)
    first = {"run": "first-review", "round": 0, "review_round": 1, "verdict": "fix"}
    second = {"run": "second-review", "round": 1, "review_round": 2, "verdict": "keep"}
    flow["tasks"][0].update(
        round=1, writer_run="writer", reviewer_run="second-review", reviews=[first]
    )
    flow["tasks"][1]["reviews"] = [
        {"reviewer_run": "legacy-review", "round": 0, "verdict": "keep"}
    ]
    flow_record.save(flow, env)
    flow_record.append_event(
        FLOW,
        {"kind": "fallback", "task": "task-0", "role": "writer", "run": "old-writer"},
        env,
    )
    flow_record.append_event(
        FLOW,
        {"kind": "task", "task": "task-0", "writer_run": "older-writer", "round": 0},
        env,
    )
    flow_record.append_event(FLOW, {"kind": "task", "run": "unknown"}, env)

    item = flows.read(env, proc_locks=proc_locks).flows[0]
    assert item.run_ids == {
        "old-writer",
        "older-writer",
        "writer",
        "first-review",
        "second-review",
        "legacy-review",
        "unknown",
    }
    assert item.workers == {
        "old-writer": flows.FlowRun("task-0", "writer", None),
        "older-writer": flows.FlowRun("task-0", "writer", 0),
        "writer": flows.FlowRun("task-0", "writer", 1),
        "first-review": flows.FlowRun("task-0", "reviewer", 1, first),
        "second-review": flows.FlowRun("task-0", "reviewer", 2),
        "legacy-review": flows.FlowRun(
            "task-1", "reviewer", 1, flow["tasks"][1]["reviews"][0]
        ),
    }
    flow["tasks"][0].update(reviewer_run=None, reviews=[first, second])
    flow_record.save(flow, env)
    item = flows.read(env, proc_locks=proc_locks).flows[0]
    assert item.workers["second-review"] == flows.FlowRun(
        "task-0", "reviewer", 2, second
    )


def test_event_only_change_and_atomic_replacement(
    env: dict[str, str], proc_locks: Path, qtbot: QtBot
) -> None:
    make_flow(env)
    tasks = TaskRunner()
    model = flows.FlowsModel(env, tasks, proc_locks=proc_locks)
    model.set_active(True)
    qtbot.waitUntil(model.loaded)
    events = flow_record.folder(FLOW, env) / flow_record.EVENTS_NAME
    for run in ("first", "second"):
        replacement = events.with_suffix(".new")
        replacement.write_text(json.dumps({"seq": 1, "run": run}) + "\n")
        replacement.replace(events)
        qtbot.waitUntil(lambda run=run: model.flows()[0].run_ids == {run})
        qtbot.waitUntil(lambda: str(events) in model.watched())
    replacement.write_text(
        json.dumps({"seq": 1, "task": "task-0", "writer_run": "second"}) + "\n"
    )
    with qtbot.waitSignal(model.changed, timeout=5000):
        replacement.replace(events)
    assert model.flows()[0].workers["second"] == flows.FlowRun("task-0", "writer", None)
    model.set_active(False)


def test_panel_run_ids_emit_without_timestamp_change(
    env: dict[str, str], proc_locks: Path, qtbot: QtBot
) -> None:
    panel = make_panel(env)
    model = flows.FlowsModel(env, TaskRunner(), proc_locks=proc_locks)
    model.set_active(True)
    qtbot.waitUntil(model.loaded)
    panel["members"][0]["attempts"] = [
        {"kind": "retry", "at": 110, "exit": 0, "run": "previous"}
    ]
    with qtbot.waitSignal(model.changed, timeout=5000):
        panel_record.save(panel, env)
    assert model.panels()[0].run_ids == {"previous"}
    model.set_active(False)


@pytest.mark.parametrize("error", [PermissionError, IsADirectoryError])
def test_unreadable_events_only_exclude_the_affected_flow(
    env: dict[str, str],
    proc_locks: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[OSError],
) -> None:
    make_flow(env)
    other = make_flow(env, "flow-20260101-120000-cccccc")
    make_panel(env)
    events = flow_record.folder(FLOW, env) / flow_record.EVENTS_NAME
    original = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == events:
            raise error("unreadable events")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    snapshot = flows.read(env, proc_locks=proc_locks)
    assert [item.record["id"] for item in snapshot.flows] == [other["id"]]
    assert [item.record["id"] for item in snapshot.panels] == [PANEL]
    assert snapshot.unreadable_flows == 1
    assert snapshot.unreadable_panels == 0
    assert str(events) in snapshot.watch
