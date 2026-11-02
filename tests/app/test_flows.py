import fcntl
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


def test_lock_parser_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    path.touch()
    held = flows.held_locks(
        lock_line(path) + "2: FLOCK ADVISORY WRITE 1 0a:ff:42 0 EOF\n"
        "3: POSIX ADVISORY WRITE 1 00:01:3 0 EOF\n"
        "4: -> FLOCK ADVISORY WRITE 1 00:01:4 0 EOF\n"
        "5: FLOCK bad\n6: FLOCK ADVISORY WRITE 1 invalid 0 EOF\n"
        "7: FLOCK ADVISORY WRITE 1 zz:01:2 0 EOF\n"
    )
    assert len(held) == 2
    assert (10, 255, 42) in held
    assert flows.lock_held(path, held) is True
    assert flows.lock_held(path, set()) is False
    assert flows.lock_held(tmp_path / "absent", held) is False
    assert flows.lock_held(path, None) is None


def test_missing_records_create_nothing(env: dict[str, str], proc_locks: Path) -> None:
    result = flows.read(env, proc_locks=proc_locks)
    assert result.flows == result.panels == []
    assert result.unreadable_flows == result.unreadable_panels == 0
    assert not paths.state_dir(env).exists()
    assert not paths.runtime_dir(env).exists()


def test_order_limits_and_broken_records(env: dict[str, str], proc_locks: Path) -> None:
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
    ]
    assert result.unreadable_flows == result.unreadable_panels == 1
    assert result.panels[0].question == "A question"
    assert len(result.watch) == 1 + 11 * 2 + 12 * 2


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
