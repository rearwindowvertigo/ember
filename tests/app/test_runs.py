import json
import os
from dataclasses import replace

from pytestqt.qtbot import QtBot

from ember import paths
from ember.app.runs import RunsModel
from ember.app.tasks import TaskRunner
from ember.runs import record
from ember.runs.record import Run


def make_run(
    env: dict[str, str],
    run_id: str = "codex-20260101-120000-abcdef",
    **changes: object,
) -> Run:
    """Build a run record in the test's state folder (not yet written)."""
    run = Run(
        id=run_id,
        engine="codex",
        model="gpt-5",
        effort="high",
        read_only=False,
        repo="/home/owner/project",
        worktree=f"/home/owner/project/.ember/worktrees/{run_id}",
        branch=f"ember/codex/{run_id}",
        base="a" * 40,
        sandbox="bubblewrap",
        network=False,
        read=(),
        read_effective=(),
        trailers=(),
        claim=None,
        status=record.RUNNING,
        turn=1,
        turn_state=record.SENT,
        reply=None,
        expected_head=None,
        started_at=1_800_000_000.0,
        updated_at=1_800_000_000.0,
        ended_at=None,
        last_exit=None,
        last_reason=None,
        commits=(),
        subject="Add the thing",
        folder=paths.runs_dir(env) / run_id,
        env=env,
    )
    return replace(run, **changes)  # type: ignore[arg-type]


def write_run(run: Run) -> Run:
    """Write a run's record atomically, creating its folder."""
    record.make_run_dir(run.id, run.env)
    record.save(run)
    return run


def _model(env: dict[str, str], qtbot: QtBot, backstop_ms: int = 60_000) -> RunsModel:
    model = RunsModel(env, TaskRunner(), backstop_ms=backstop_ms)
    model.start()
    qtbot.waitUntil(model.loaded)
    return model


def test_missing_runs_folder_is_created_private(
    env: dict[str, str], qtbot: QtBot
) -> None:
    model = _model(env, qtbot)
    folder = paths.runs_dir(env)
    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o700
    assert model.live() == []
    assert model.finished() == []
    assert str(folder) in model.watched()
    model.stop()


def test_sees_a_new_run_and_a_status_change(env: dict[str, str], qtbot: QtBot) -> None:
    model = _model(env, qtbot)
    with qtbot.waitSignal(model.changed, timeout=5000):
        run = write_run(make_run(env))
    qtbot.waitUntil(lambda: len(model.live()) == 1)
    assert model.live()[0].id == run.id
    qtbot.waitUntil(lambda: str(run.folder / "run.json") in model.watched())
    with qtbot.waitSignal(model.changed, timeout=5000):
        write_run(replace(run, status=record.WAITING, updated_at=run.updated_at + 1))
    qtbot.waitUntil(lambda: model.live()[0].status == record.WAITING)
    model.stop()


def test_atomic_replace_is_watched_again(env: dict[str, str], qtbot: QtBot) -> None:
    run = write_run(make_run(env))
    model = _model(env, qtbot)
    file = str(run.folder / "run.json")
    qtbot.waitUntil(lambda: file in model.watched())
    for turn in (2, 3):
        with qtbot.waitSignal(model.changed, timeout=5000):
            write_run(replace(run, turn=turn, updated_at=run.updated_at + turn))
        qtbot.waitUntil(lambda turn=turn: model.live()[0].turn == turn)
        qtbot.waitUntil(lambda: file in model.watched())
    model.stop()


def test_unchanged_records_do_not_emit(env: dict[str, str], qtbot: QtBot) -> None:
    write_run(make_run(env))
    model = _model(env, qtbot)
    with qtbot.assertNotEmitted(model.changed, wait=200):
        model.reload()
        qtbot.waitUntil(lambda: model._tasks.pending() == 0)
    model.stop()


def test_backstop_timer_reloads(env: dict[str, str], qtbot: QtBot) -> None:
    model = _model(env, qtbot, backstop_ms=100)
    model.stop()  # no file watching; only the timer can see the new run
    model._timer.start()
    run = make_run(env)
    folder = record.make_run_dir(run.id, env)
    text = json.dumps(run.to_json())
    (folder / "run.json").write_text(text)
    qtbot.waitUntil(lambda: len(model.live()) == 1, timeout=5000)
    model._timer.stop()


def test_unreadable_records_are_skipped_and_counted(
    env: dict[str, str], qtbot: QtBot
) -> None:
    write_run(make_run(env))
    broken = paths.runs_dir(env) / "codex-20260101-130000-bbbbbb"
    broken.mkdir(mode=0o700)
    (broken / "run.json").write_text("{half")
    model = _model(env, qtbot)
    assert len(model.runs()) == 1
    assert model.unreadable() == 1
    with qtbot.waitSignal(model.changed, timeout=5000):
        (broken / "run.json").unlink()
        os.rmdir(broken)
    qtbot.waitUntil(lambda: model.unreadable() == 0)
    model.stop()


def test_live_and_finished_order(env: dict[str, str], qtbot: QtBot) -> None:
    base = 1_800_000_000.0
    write_run(make_run(env, "codex-20260101-120000-aaaaaa", started_at=base + 2))
    write_run(
        make_run(
            env,
            "codex-20260101-110000-bbbbbb",
            status=record.STARTING,
            started_at=base + 1,
        )
    )
    for index in range(22):
        write_run(
            make_run(
                env,
                f"codex-20260101-100000-c{index:05d}",
                status=record.CLOSED if index % 2 else record.FAILED,
                started_at=base - 100 + index,
                ended_at=base - 50 + index,
                updated_at=base - 50 + index,
            )
        )
    model = _model(env, qtbot)
    assert [run.id for run in model.live()] == [
        "codex-20260101-110000-bbbbbb",
        "codex-20260101-120000-aaaaaa",
    ]
    finished = model.finished()
    assert len(finished) == 20
    assert finished[0].id == "codex-20260101-100000-c00021"
    assert finished[-1].id == "codex-20260101-100000-c00002"
    model.stop()


def test_newly_failed_turn_is_reported_once(env: dict[str, str], qtbot: QtBot) -> None:
    old = write_run(
        make_run(env, "claude-20260101-090000-aaaaaa", engine="claude", status="failed")
    )
    run = write_run(make_run(env))
    model = _model(env, qtbot)
    seen: list[str] = []
    model.turn_failed.connect(seen.append)
    with qtbot.waitSignal(model.turn_failed, timeout=5000):
        write_run(
            replace(
                run,
                status=record.FAILED,
                last_exit=69,
                last_reason="not signed in",
                updated_at=run.updated_at + 1,
            )
        )
    assert seen == ["codex"]
    write_run(replace(old, updated_at=old.updated_at + 5))
    model.reload()
    qtbot.waitUntil(lambda: model._tasks.pending() == 0)
    qtbot.wait(100)
    model.reload()
    qtbot.waitUntil(lambda: model._tasks.pending() == 0)
    assert seen == ["codex"]
    model.stop()


def test_reloads_are_coalesced(env: dict[str, str], qtbot: QtBot) -> None:
    model = RunsModel(env, TaskRunner())
    for _ in range(5):
        model.reload()
    assert model._tasks.pending() == 1
    qtbot.waitUntil(model.loaded)
    qtbot.waitUntil(lambda: model._tasks.pending() == 0)
    assert not model._queued
