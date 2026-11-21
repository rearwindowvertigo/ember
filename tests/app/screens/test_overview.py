import datetime
import json
import os
import shutil
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, QUrl
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QWidget,
)
from pytestqt.qtbot import QtBot

from ember import choices, paths, prune, sandbox
from ember.app import flows, state, theme, tray
from ember.app.context import AppContext
from ember.app.screens import overview
from ember.app.screens.overview import OverviewScreen
from ember.flows import record as flow_record
from ember.panels import record as panel_record
from ember.runs import record

from ..test_flows import FLOW, PANEL, make_flow, make_member, make_panel  # pyrefly: ignore[missing-import]
from ..test_runs import make_run, write_run  # pyrefly: ignore[missing-import]

LIVE_A = "codex-20260101-120000-aaaaaa"
LIVE_B = "claude-20260101-121000-bbbbbb"
DONE_A = "opencode-20260101-090000-cccccc"
DONE_B = "codex-20260101-100000-dddddd"


@pytest.fixture
def context(env: dict[str, str], qtbot: QtBot) -> Iterator[AppContext]:
    result = AppContext(env)
    result.runs.start()
    qtbot.waitUntil(result.runs.loaded)
    yield result
    result.runs.stop()
    result.flows.set_active(False)


def screen_for(context: AppContext, qtbot: QtBot) -> OverviewScreen:
    screen = OverviewScreen(context)
    qtbot.addWidget(screen)
    proc_locks = Path(context.env["HOME"]) / "proc-locks"
    proc_locks.write_text("")
    screen.records._proc_locks = proc_locks
    context.flows.set_active(True)
    screen.resize(1000, 900)
    screen.show()
    screen.shown()
    qtbot.waitUntil(
        lambda: screen.records.loaded() or screen.records.error() is not None
    )
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    return screen


def write_mix(env: dict[str, str]) -> None:
    write_run(make_run(env, LIVE_A, status=record.STARTING, started_at=1_800_000_000.0))
    write_run(
        make_run(
            env,
            LIVE_B,
            engine="claude",
            model="opus",
            effort=None,
            status=record.WAITING,
            read_only=True,
            branch=None,
            repo="/home/owner/beta",
            turn=3,
            started_at=1_800_000_100.0,
        )
    )
    write_run(
        make_run(
            env,
            DONE_A,
            engine="opencode",
            model="openrouter/x",
            status=record.CLOSED,
            ended_at=1_800_000_500.0,
            started_at=1_800_000_000.0,
            commits=("a" * 40,),
            subject="Fix the bug",
        )
    )
    write_run(
        make_run(
            env,
            DONE_B,
            status=record.FAILED,
            ended_at=1_800_000_900.0,
            last_exit=3,
            last_reason="sign-in needed",
        )
    )


def open_finished(screen: OverviewScreen) -> None:
    """Open the full Finished list, with its pickers and Clear."""
    if not screen.finished_open:
        screen.toggle_finished_list()


def loaded(env: dict[str, str], context: AppContext, qtbot: QtBot) -> OverviewScreen:
    write_mix(env)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 4)
    screen = screen_for(context, qtbot)
    screen.refresh()
    return screen


def test_empty(context: AppContext, qtbot: QtBot) -> None:
    screen = screen_for(context, qtbot)
    assert screen.subtitle.isHidden()
    assert screen.live_empty.isVisible()
    assert screen.finished_empty.isVisible()
    assert screen.live_card.isVisible()
    assert screen.finished_card.isVisible()


def test_tmp_volume_warning_follows_run_reloads(
    context: AppContext, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = screen_for(context, qtbot)
    alert = screen.tmp_volume_alert
    assert not alert.isVisible()

    def reload(volume: paths.RunTmpVolume | None) -> None:
        def read_volume() -> paths.RunTmpVolume | None:
            assert threading.current_thread() is not threading.main_thread()
            return volume

        with monkeypatch.context() as patch:
            patch.setattr(paths, "run_tmp_volume", read_volume)
            with qtbot.waitSignal(context.runs.reloaded):
                context.runs.reload()

    for volume, counts in (
        (paths.RunTmpVolume(49_999, 512 * 1024**2, True), "49,999 free files, 512"),
        (paths.RunTmpVolume(60_000, 255 * 1024**2 + 1, True), "60,000 free files, 255"),
        (paths.RunTmpVolume(80_000, 512 * 1024**2, False), None),
        (
            paths.RunTmpVolume(None, 128 * 1024**2, True),
            "free file count unavailable, 128",
        ),
        (None, None),
    ):
        reload(volume)
        if counts is None:
            assert not alert.isVisible()
        else:
            text = (
                f"{paths.RUN_TMP_ROOT} is nearly full: {counts} MiB free. "
                "Ember refuses new Codex runs until files or space are freed."
            )
            assert alert.isVisible()
            assert alert.tone() == "warning"
            assert alert.title_label.text() == alert.accessibleName() == text
            assert [
                a for a in screen.findChildren(overview.Alert) if a.isVisible()
            ] == [alert]


def test_live_header_collapses_when_empty(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = screen_for(context, qtbot)
    heading = next(
        label
        for label in screen.live_card.findChildren(QLabel)
        if label.text() == "Live"
    )
    empty = screen.live_empty
    assert empty.text() == "Nothing running"
    assert empty.property("role") == "secondary"
    assert empty.isVisible()
    # Font metrics differ between machines, so the centres may differ by 1 px.
    assert (
        abs(
            heading.mapTo(screen.live_card, heading.rect().center()).y()
            - empty.mapTo(screen.live_card, empty.rect().center()).y()
        )
        <= 1
    )
    assert empty.geometry().left() > heading.geometry().right()
    layout = screen.live_card.layout()
    assert layout is not None
    margins = layout.contentsMargins()
    assert screen.live_card.height() == (
        max(heading.height(), empty.height()) + margins.top() + margins.bottom()
    )

    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: LIVE_A in screen.live_rows)
    row = screen.live_rows[LIVE_A]
    qtbot.waitUntil(lambda: row.isVisible() and row.y() > heading.geometry().bottom())
    assert not empty.isVisible()


def test_rows_and_badges(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    assert screen.subtitle.isHidden()
    assert list(screen.live_rows) == [LIVE_A, LIVE_B]
    first, second = screen.live_rows.values()
    assert (first.badge.text(), first.badge.tone()) == ("Starting", "neutral")
    assert (second.badge.text(), second.badge.tone()) == (
        "Waiting for Claude",
        "warning",
    )
    assert first.title_label.text() == f"Run · {first.run.subject}"
    assert second.title_label.text() == f"Run · {second.run.subject}"
    assert first.detail_label.text().startswith(
        "Codex · gpt-5 · high · project · turn 1 · 0 commits · started "
    )
    assert second.detail_label.text().startswith(
        "Claude · opus · beta · turn 3 · read-only · started "
    )
    assert first.time_label.text() == ""
    assert first.time_label.toolTip() == ""

    assert list(screen.finished_rows) == [DONE_B, DONE_A]
    failed, closed = screen.finished_rows.values()
    assert (failed.badge.text(), failed.badge.tone()) == ("Failed", "danger")
    assert (closed.badge.text(), closed.badge.tone()) == ("Done", "success")
    assert closed.title_label.text() == "Run · Fix the bug"
    assert closed.detail_label.text().startswith(
        "OpenCode · openrouter/x · high · project · 1 commit · "
    )
    assert " to " in closed.detail_label.text()
    assert "sign-in needed" in failed.detail_label.text()
    assert closed.time_label.text() == "Elapsed 8 min 20 s"
    assert closed.time_label.toolTip() == ""
    assert not screen.state_view.empty.isVisible()


def test_running_badge(env: dict[str, str], context: AppContext, qtbot: QtBot) -> None:
    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 1)
    screen = screen_for(context, qtbot)
    row = screen.live_rows[LIVE_A]
    assert (row.badge.text(), row.badge.tone()) == ("Running", "info")
    assert screen.subtitle.isHidden()
    assert not screen.finished_empty.isHidden()


def test_single_expansion(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    first, second = screen.live_rows.values()
    qtbot.mouseClick(first.expand_button, Qt.MouseButton.LeftButton)
    assert screen.expanded_live == LIVE_A
    assert first.details is not None
    assert first.expand_button.accessibleName() == f"Hide details of run {LIVE_A}"
    rows = dict(overview.record_rows(first.run))
    assert rows["Run"] == LIVE_A
    assert rows["Ended"] == "Not yet"

    qtbot.mouseClick(second.expand_button, Qt.MouseButton.LeftButton)
    assert screen.expanded_live == LIVE_B
    assert first.details is None
    assert second.details is not None
    assert first.expand_button.accessibleName() == f"Show details of run {LIVE_A}"

    qtbot.mouseClick(second.expand_button, Qt.MouseButton.LeftButton)
    assert screen.expanded_live is None
    assert second.details is None


def test_live_details_follow_the_run(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    screen.toggle_live(LIVE_A)
    row = screen.live_rows[LIVE_A]
    run = record.load(LIVE_A, env)
    write_run(replace(run, turn=2, updated_at=run.started_at + 30))
    context.runs.reload()
    qtbot.waitUntil(
        lambda: dict(row.detail_values).get("Turns and commits") == "2 turns, 0 commits"
    )
    assert row.details is not None


def test_expanded_live_row_closes_when_the_run_ends(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    screen.toggle_live(LIVE_A)
    run = record.load(LIVE_A, env)
    write_run(make_run(env, LIVE_A, status=record.CLOSED, ended_at=run.started_at + 60))
    context.runs.reload()
    qtbot.waitUntil(lambda: screen.expanded_live is None)
    assert LIVE_A not in screen.live_rows
    assert LIVE_A in screen.finished_rows


def test_hidden_screen_redraws_when_shown(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = screen_for(context, qtbot)
    screen.hide()
    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 1)
    assert screen.live_rows == {}
    screen.show()
    screen.shown()
    assert list(screen.live_rows) == [LIVE_A]


def test_finished_details(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    failed = screen.finished_rows[DONE_B]
    qtbot.mouseClick(failed.expand_button, Qt.MouseButton.LeftButton)
    assert failed.details is not None
    rows = dict(overview.record_rows(failed.run))
    assert list(rows) == [
        "Run",
        "Subject",
        "Engine",
        "Repository",
        "Branch",
        "Sandbox",
        "Turns and commits",
        "Started",
        "Ended",
        "Last exit",
        "Last reason",
    ]
    assert rows["Run"] == DONE_B
    assert rows["Sandbox"] == "bubblewrap"
    assert rows["Turns and commits"] == "1 turn, 0 commits"
    assert rows["Last exit"] == "3"
    assert rows["Last reason"] == "sign-in needed"
    closed = screen.finished_rows[DONE_A]
    screen.toggle_finished(DONE_A)
    assert failed.details is None
    assert closed.details is not None
    screen.toggle_finished(DONE_A)
    assert closed.details is None
    assert screen.expanded_finished is None


def test_open_terminal_calls_the_context(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(context, "open_run_terminal", opened.append)
    screen = loaded(env, context, qtbot)
    qtbot.mouseClick(screen.live_rows[LIVE_B].open_button, Qt.MouseButton.LeftButton)
    assert opened == [LIVE_B]


def use_settings(
    context: AppContext, monkeypatch: pytest.MonkeyPatch, values: dict[str, str]
) -> None:
    monkeypatch.setattr(context, "settings_ready", lambda: True)
    monkeypatch.setattr(context, "setting", values.get)


def test_sandbox_alert(
    context: AppContext, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {f"engine.{engine}": "on" for engine in sandbox.ENGINES}
    values |= {"sandbox.codex": "on", "sandbox.opencode": "off"}
    values["sandbox.claude"] = "off"
    use_settings(context, monkeypatch, values)
    screen = screen_for(context, qtbot)
    assert list(screen.sandbox_alerts) == ["opencode", "claude"]
    alert = screen.sandbox_alerts["opencode"]
    assert alert.tone() == "warning"
    assert alert.title_label.text() == (
        "OpenCode runs without a sandbox. Its workers can read every file you "
        "can, including logins and keys. Turn its sandbox on in Engines."
    )
    with qtbot.waitSignal(context.screen_requested) as blocker:
        qtbot.mouseClick(alert.buttons[0], Qt.MouseButton.LeftButton)
    assert blocker.args == ["engines"]

    values["sandbox.opencode"] = "on"
    context.settings_changed.emit({})
    assert list(screen.sandbox_alerts) == ["claude"]
    assert context.tasks.pending() == 0


def test_sandbox_alerts_read_the_snapshot(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    screen = screen_for(context, qtbot)
    expected = [
        engine
        for engine in sandbox.ENGINES
        if context.setting(f"engine.{engine}") == "on"
        and context.setting(f"sandbox.{engine}") != "on"
    ]
    assert list(screen.sandbox_alerts) == expected
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.save_settings({"sandbox.claude": "off", "engine.claude": "on"})
    assert "claude" in screen.sandbox_alerts


def test_no_alert_before_the_settings(context: AppContext, qtbot: QtBot) -> None:
    screen = screen_for(context, qtbot)
    assert screen.sandbox_alerts == {}


def test_engine_off_has_no_alert(
    context: AppContext, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {f"engine.{engine}": "on" for engine in sandbox.ENGINES}
    values |= {f"sandbox.{engine}": "off" for engine in sandbox.ENGINES}
    values["engine.claude"] = "off"
    use_settings(context, monkeypatch, values)
    screen = screen_for(context, qtbot)
    assert list(screen.sandbox_alerts) == ["codex", "opencode"]


def test_unreadable_records(env: dict[str, str], qtbot: QtBot) -> None:
    write_run(make_run(env, LIVE_A))
    broken = paths.runs_dir(env) / "codex-20260101-130000-bbbbbb"
    broken.mkdir(mode=0o700)
    (broken / "run.json").write_text("{half")
    context = AppContext(env)
    context.runs.start()
    qtbot.waitUntil(context.runs.loaded)
    screen = screen_for(context, qtbot)
    assert screen.unreadable_alert.isVisible()
    assert screen.unreadable_alert.title_label.text() == ("1 run could not be read.")
    assert screen.unreadable_alert.buttons == []
    context.runs.stop()


def test_keyboard_reach(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {f"engine.{engine}": "on" for engine in sandbox.ENGINES}
    values |= {f"sandbox.{engine}": "off" for engine in sandbox.ENGINES}
    use_settings(context, monkeypatch, values)
    screen = loaded(env, context, qtbot)
    open_finished(screen)
    assert len(screen.sandbox_alerts) == 3
    with qtbot.waitActive(screen):
        screen.activateWindow()
    controls: list[QWidget] = []
    for alert in screen.sandbox_alerts.values():
        controls.extend(alert.buttons)
    for row in screen.live_rows.values():
        controls.extend((row.expand_button, row.open_button))
    controls.extend(
        (
            screen.finished_toggle,
            screen.type_filter,
            screen.status_filter,
            screen.engine_filter,
            screen.clear_button,
        )
    )
    controls.extend(row.expand_button for row in screen.finished_rows.values())
    controls[0].setFocus()
    seen = [QApplication.focusWidget()]
    for _ in range(len(controls) - 1):
        screen.focusNextChild()
        seen.append(QApplication.focusWidget())
    assert seen == controls


def test_runs_folder_error_and_try_again(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = loaded(env, context, qtbot)

    def refuse(_env: object) -> object:
        raise PermissionError(13, "Permission denied", "runs")

    monkeypatch.setattr(record, "list_runs", refuse)
    with qtbot.waitSignal(context.runs.failed, timeout=5000):
        context.runs.reload()
    assert screen.state_view.error.isVisible()
    assert "could not read the run records" in screen.state_view.error_text.text()
    assert "Permission denied" in screen.state_view.error_text.text()
    assert screen.state_view.retry_button.isVisible()
    assert not screen.live_card.isVisible()
    assert not screen.finished_card.isVisible()

    monkeypatch.undo()
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        qtbot.mouseClick(screen.state_view.retry_button, Qt.MouseButton.LeftButton)
        assert context.runs._in_flight
    assert not screen.state_view.error.isVisible()
    assert screen.live_card.isVisible()
    assert list(screen.live_rows) == [LIVE_A, LIVE_B]


def test_tmp_volume_recovers_while_runs_folder_is_unreadable(
    context: AppContext, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = screen_for(context, qtbot)

    def refuse(_env: object) -> object:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(record, "list_runs", refuse)
    with monkeypatch.context() as patch:
        volume = paths.RunTmpVolume(49_999, 512 * 1024**2, True)
        patch.setattr(paths, "run_tmp_volume", lambda: volume)
        with qtbot.waitSignal(context.runs.failed):
            context.runs.reload()
        assert screen.state_view.error.isVisible()
        assert screen.tmp_volume_alert.isVisible()
        assert (
            "49,999 free files, 512 MiB free"
            in screen.tmp_volume_alert.accessibleName()
        )

        volume = paths.RunTmpVolume(60_000, 512 * 1024**2, False)
        with qtbot.waitSignal(context.runs.failed):
            qtbot.mouseClick(screen.state_view.retry_button, Qt.MouseButton.LeftButton)
    assert screen.state_view.error.isVisible()
    assert "Permission denied" in screen.state_view.error_text.text()
    assert not screen.tmp_volume_alert.isVisible()


def test_empty_state_after_the_last_run_goes(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 1)
    screen = screen_for(context, qtbot)
    assert screen.live_card.isVisible()
    shutil.rmtree(paths.runs_dir(env) / LIVE_A)
    context.runs.reload()
    qtbot.waitUntil(lambda: not context.runs.runs())
    assert screen.live_empty.isVisible()
    assert screen.live_card.isVisible()


def test_finished_rows_survive_a_live_run_saving(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    with qtbot.waitActive(screen):
        screen.activateWindow()
    row = screen.finished_rows[DONE_A]
    screen.toggle_finished(DONE_A)
    details = row.details
    assert details is not None
    value = details.findChildren(QLabel)[1]
    value.setSelection(0, 3)
    row.expand_button.setFocus()

    run = record.load(LIVE_A, env)
    write_run(replace(run, status=record.RUNNING, updated_at=run.updated_at + 5))
    qtbot.waitUntil(
        lambda: screen.live_rows[LIVE_A].badge.text() == "Running", timeout=5000
    )
    assert screen.finished_rows[DONE_A] is row
    assert row.details is details
    assert value.hasSelectedText()
    assert QApplication.focusWidget() is row.expand_button
    assert screen.expanded_finished == DONE_A


def test_finished_row_redraws_when_its_values_change(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    row = screen.finished_rows[DONE_A]
    screen.toggle_finished(DONE_A)
    run = record.load(DONE_A, env)
    write_run(replace(run, subject="Fix it", updated_at=run.updated_at + 5))
    # The watcher can report the folder change before the new record is in
    # place, so wait for the record the test wrote rather than one signal.
    qtbot.waitUntil(lambda: row.run.subject == "Fix it", timeout=5000)
    assert screen.finished_rows[DONE_A] is row
    assert row.detail_label.text().startswith(
        f"{overview.worker_line(row.run)} · project · 1 commit · "
    )
    assert row.details is not None
    assert dict(overview.record_rows(row.run))["Subject"] == "Fix it"
    texts = [label.text() for label in row.details.findChildren(QLabel)]
    assert "Fix it" in texts

    new = make_run(
        env,
        "codex-20260101-110000-eeeeee",
        status=record.CLOSED,
        ended_at=1_800_000_950.0,
    )
    write_run(new)
    qtbot.waitUntil(lambda: new.id in screen.finished_rows, timeout=5000)
    assert list(screen.finished_rows) == [new.id, DONE_B, DONE_A]
    assert screen.finished_rows[DONE_A] is row
    layout = screen.finished_rows_layout
    order = [layout.indexOf(row) for row in screen.finished_rows.values()]
    assert order == list(range(len(order)))


def labels(widget: QWidget) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


def test_record_formatters(env: dict[str, str]) -> None:
    flow = make_flow(env, count=1)
    assert overview.flow_title(flow) == "Flow · project · 1 task · 0/1 merged"
    assert overview.flow_detail(flow) == f"ember/flow/{FLOW}"
    task = flow["tasks"][0]
    assert overview.task_line(flow, task) == (
        "task-0 · Pending · round 0 · Codex · test-model"
    )
    assert overview.review_line(task) is None
    task["writer"] = {"engine": "claude", "model": "opus"}
    task["status"] = "future"
    assert overview.task_line(flow, task).endswith("future · round 0 · Claude · opus")
    task["reviews"] = [{"verdict": "keep", "must": 0, "should": 0, "lead": 0}]
    assert (
        overview.review_line(task) == "1 review · last keep · 0 must, 0 should, 0 lead"
    )
    panel = make_panel(env)
    assert overview.panel_title(panel) == "Panel · 1 member · 0/1 answered"
    assert overview.member_line(panel["members"][0]) == (
        "1. Codex · test-model · default · Pending · -s"
    )
    assert overview.member_line(make_member(seconds=12.6)).endswith("13s")
    assert " · default · fast · " in overview.member_line(make_member(speed="fast"))
    item = flows.PanelItem(panel, "x" * 100, None, False)
    assert overview.panel_detail(item) == "project"
    assert overview.panel_detail(replace(item, question="")) == "project"
    flow["tasks"] = []
    assert overview.flow_title(flow) == "Flow · project · 0 tasks · 0/0 merged"
    panel["members"] = []
    assert overview.panel_title(panel) == "Panel · 0 members · 0/0 answered"


def test_flow_details_and_nested_runs(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    write_run(make_run(env, LIVE_A))
    write_run(make_run(env, DONE_A, status=record.CLOSED, ended_at=200))
    flow = make_flow(
        env,
        status="stopped",
        wave=12,
        reason="Needs an answer",
        plan_waves=[["task-0"], ["task-1"]],
    )
    flow["tasks"][0].update(status="merged", writer_run=DONE_A)
    flow["tasks"][1].update(
        status="stopped",
        reason="Choose a format",
        writer_run=LIVE_A,
        reviewer_run="codex-20260101-120000-ffffff",
        round=1,
        reviews=[{"verdict": "fix", "must": 2, "should": 1, "lead": 1}],
    )
    flow_record.save(flow, env)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 2)
    screen = screen_for(context, qtbot)
    assert screen.live_card.isVisible()
    assert screen.finished_empty.isVisible()
    row = screen.flow_rows[FLOW]
    assert row.badge.text() == "Waiting for Claude"
    assert row.title_label.text() == "Flow · project · 2 tasks · 1/2 merged"
    assert row.base_detail == (
        f"1 live run · ember/flow/{FLOW} · wave 2 of 2 · Needs an answer"
    )
    qtbot.mouseClick(row.expand_button, Qt.MouseButton.LeftButton)
    assert row.details is not None
    assert labels(row.details)[0] == "passes 12"
    assert "Choose a format" in labels(row)
    assert "1 review · last fix · 2 must, 1 should, 1 lead" in labels(row)
    assert "task-0 · Merged · round 0 · Codex · test-model" in labels(row)
    assert row.nested.count() == 2
    assert row.nested.indexOf(screen.finished_rows[DONE_A]) >= 0
    assert row.nested.indexOf(screen.live_rows[LIVE_A]) >= 0
    assert screen.finished_rows_layout.count() == 0
    assert screen.live_rows_layout.count() == 1


@pytest.mark.parametrize("stored_waves", [False, True])
def test_flow_without_plan_wave_shows_passes_zero(
    env: dict[str, str], context: AppContext, qtbot: QtBot, stored_waves: bool
) -> None:
    """Flow details show zero passes when the flow line has no plan wave."""
    flow = make_flow(env, reason="Waiting")
    if not stored_waves:
        flow["tasks"][0]["status"] = "writing"
        del flow["plan_waves"]
        path = flow_record.folder(FLOW, env) / flow_record.FLOW_NAME
        path.write_text(json.dumps(flow))
    screen = screen_for(context, qtbot)
    row = screen.flow_rows[FLOW]
    assert row.detail_label.text().startswith(
        f"runner gone · ember/flow/{FLOW} · Waiting · started "
    )
    row.expand_button.click()
    assert row.details is not None
    assert labels(row.details)[0] == "passes 0"


def test_panel_details(env: dict[str, str], context: AppContext, qtbot: QtBot) -> None:
    panel = make_panel(
        env,
        status="incomplete",
        ended_at=220,
        members=[
            make_member(1, status="answered", seconds=12.2),
            make_member(2, status="failed", reason="No reply", changed=["a.py"]),
            make_member(3, status="timed out", run=LIVE_A),
        ],
        left_out=[{"value": "claude:opus", "reason": "engine off"}],
    )
    screen = screen_for(context, qtbot)
    row = screen.panel_rows[PANEL]
    assert row.badge.text() == "Incomplete"
    assert row.title_label.text() == "Panel · 3 members · 1/3 answered"
    assert row.detail_label.text().startswith("project · ")
    assert " to " in row.detail_label.text()
    assert row.time_label.text() == "Elapsed 2 min 0 s"
    assert not hasattr(row, "question_label")
    qtbot.mouseClick(row.expand_button, Qt.MouseButton.LeftButton)
    text = labels(row)
    assert text.index("Question") + 1 == text.index("A question\nMore")
    assert all(overview.member_line(member) in text for member in panel["members"])
    assert "No reply" in text
    assert "Changed: a.py" in text
    assert "Left out claude:opus: engine off" in text
    assert not any(line.startswith("Summary:") for line in text)
    assert f"Run {LIVE_A}" not in text
    assert [
        button.text()
        for button in screen.finished_card.findChildren(QPushButton)
        if button is not screen.finished_toggle
        and not screen.filter_bar.isAncestorOf(button)
    ] == ["", "Summary"]


def test_newest_check_log(tmp_path: Path) -> None:
    assert overview.newest_check_log(tmp_path) is None
    (tmp_path / "check-99-99.log").mkdir()
    (tmp_path / "check-99-99.log" / "check-100-1.log").touch()
    (tmp_path / "other.log").touch()
    assert overview.newest_check_log(tmp_path) is None
    older = tmp_path / "check-legacy.log"
    newer = tmp_path / "check-recent.log"
    older.touch()
    newer.touch()
    os.utime(older, ns=(200, 200))
    os.utime(newer, ns=(300, 300))
    assert overview.newest_check_log(tmp_path) == newer
    for name in ("check-9-99.log", "check-10-2.log", "check-10-10.log"):
        path = tmp_path / name
        path.touch()
        os.utime(path, ns=(100, 100))
    assert overview.newest_check_log(tmp_path) == tmp_path / "check-10-10.log"


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("ended", [False, True])
def test_record_file_buttons(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    ended: bool,
) -> None:
    env["XDG_STATE_HOME"] = str(Path(env["XDG_STATE_HOME"]) / "space # and <text>")
    make_flow(env, status="done" if ended else "stopped")
    make_panel(env, status="done" if ended else "running")
    folder = flow_record.folder(FLOW, env)
    briefs = folder / "briefs"
    briefs.mkdir(exist_ok=True)
    log = folder / "check-10-2.log"
    log.touch()
    summary = panel_record.folder(PANEL, env) / "summary.md"
    summary.touch()
    opened: list[QUrl] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(overview.QDesktopServices, "openUrl", open_url)
    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        screen = screen_for(context, qtbot)
        flow = screen.flow_rows[FLOW]
        panel = screen.panel_rows[PANEL]
        assert not flow.file_buttons and not panel.file_buttons
        flow.expand_button.click()
        panel.expand_button.click()
        assert list(flow.file_buttons) == ["Briefs", "Check logs"]
        assert list(panel.file_buttons) == ["Summary"]
        for row in (flow, panel):
            for button in row.file_buttons.values():
                qtbot.waitUntil(button.isVisible)
                assert button.isEnabled() and button.isVisible()
                assert row.details is not None and row.details.isAncestorOf(button)
                button.click()
        assert opened == [QUrl.fromLocalFile(str(p)) for p in (briefs, log, summary)]
        assert all(url.isLocalFile() for url in opened)
        monkeypatch.setattr(overview.QDesktopServices, "openUrl", lambda _url: False)
        panel.file_buttons["Summary"].click()
        assert panel.findChild(QMessageBox) is None
        assert panel.file_note.isVisible()
        assert panel.file_note.text() == f"No viewer opened {summary}."
    finally:
        theme.apply(qapp, previous)


def test_record_file_buttons_refresh_only_when_expanded(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_flow(env)
    make_panel(env)
    folder = flow_record.folder(FLOW, env)
    briefs = folder / "briefs"
    if briefs.exists():
        shutil.rmtree(briefs)
    summary = panel_record.folder(PANEL, env) / "summary.md"
    summary.unlink(missing_ok=True)
    calls: list[str] = []
    original = overview._RecordRow._refresh_files

    def refresh(row: overview._RecordRow) -> None:
        calls.append(row._item_id)
        original(row)

    monkeypatch.setattr(overview._RecordRow, "_refresh_files", refresh)
    screen = screen_for(context, qtbot)
    flow = screen.flow_rows[FLOW]
    panel = screen.panel_rows[PANEL]
    with qtbot.waitSignal(context.flows.reloaded):
        context.flows.reload()
    assert calls == []
    flow.expand_button.click()
    panel.expand_button.click()
    assert calls == [FLOW, PANEL]
    assert all(
        path is None for row in (flow, panel) for path in row.file_paths.values()
    )
    assert all(
        button.isEnabled()
        for row in (flow, panel)
        for button in row.file_buttons.values()
    )
    panel.file_buttons["Summary"].click()
    assert panel.file_note.text() == (
        "Summary: the panel writes summary.md when it ends."
    )
    details = flow.details, panel.details
    briefs.mkdir()
    summary.touch()
    log = folder / "check-1-1.log"
    log.touch()
    with qtbot.waitSignal(context.flows.reloaded):
        context.flows.reload()
    assert (flow.details, panel.details) == details
    assert all(
        path is not None for row in (flow, panel) for path in row.file_paths.values()
    )
    newest = folder / "check-2-1.log"
    newest.touch()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    assert flow.file_paths["Check logs"] == newest
    briefs.rmdir()
    summary.unlink()
    log.unlink()
    newest.unlink()
    with qtbot.waitSignal(context.flows.reloaded):
        context.flows.reload()
    assert all(
        path is None for row in (flow, panel) for path in row.file_paths.values()
    )
    flow.expand_button.click()
    panel.expand_button.click()
    calls.clear()
    with qtbot.waitSignal(context.flows.reloaded):
        context.flows.reload()
    assert calls == []
    summary.touch()
    panel.expand_button.click()
    assert panel.file_buttons["Summary"].isEnabled()


def test_record_rows_survive_other_changes_and_refresh_workers(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    flow = make_flow(env, status="running")
    flow["tasks"][0]["writer_run"] = LIVE_A
    flow_record.save(flow, env)
    other = make_flow(env, "flow-20260101-120000-cccccc")
    panel = make_panel(env)
    panel["members"][0]["run"] = LIVE_A
    panel_record.save(panel, env)
    screen = screen_for(context, qtbot)
    row = screen.flow_rows[FLOW]
    panel_row = screen.panel_rows[PANEL]
    row.expand_button.click()
    panel_row.expand_button.click()
    details = row.details
    assert details is not None
    other["status"] = "stopped"
    flow_record.save(other, env)
    qtbot.waitUntil(
        lambda: screen.flow_rows[other["id"]].badge.text() == "Waiting for Claude"
    )
    assert screen.flow_rows[FLOW] is row and row.details is details and row.expanded
    assert panel_row.expanded
    row.update_elapsed(flow["started_at"] + 600)
    assert row.time_label.text() == ""
    assert row.detail_label.text().endswith(" · 10 min 0 s")
    assert row.details is details
    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: row.nested.count() == 1)
    assert panel_row.nested.count() == 0
    assert "1 live run" in row.detail_label.text()
    shutil.rmtree(paths.runs_dir(env) / LIVE_A)
    context.runs.reload()
    qtbot.waitUntil(lambda: row.nested.count() == 0)
    flow["status"] = "done"
    flow["ended_at"] = 220
    flow_record.save(flow, env)
    qtbot.waitUntil(lambda: row.badge.text() == "Done")
    assert row.expanded
    assert row.time_label.text() == "Elapsed 2 min 0 s"
    assert row.time_label.toolTip() == ""
    screen.flow_rows[other["id"]].expand_button.click()
    assert not row.expanded
    assert panel_row.expanded


def test_gone_records_and_unreadable_counts(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    make_flow(env, status="running")
    make_panel(env)
    for kind in ("flow", "panel"):
        folder = paths.runs_dir(env) / f"{kind}-20260101-120000-ffffff"
        folder.mkdir()
        (folder / f"{kind}.json").write_text("broken")
    screen = screen_for(context, qtbot)
    assert screen.flow_rows[FLOW].badge.text() == "Failed"
    assert screen.flow_rows[FLOW].detail_label.text().startswith("runner gone · ")
    assert screen.panel_rows[PANEL].badge.text() == "Failed"
    assert (
        screen.panel_rows[PANEL].detail_label.text().startswith("panel process gone · ")
    )
    assert (
        screen.unreadable_alert.title_label.text()
        == "1 flow, 1 panel could not be read."
    )
    assert screen.live_rows_layout.count() == 2
    assert not screen.state_view.empty.isVisible()


def test_records_remain_active_while_hidden(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    make_flow(env)
    screen = screen_for(context, qtbot)
    assert screen.records.active() and screen.records.watched()
    screen.hide()
    assert screen.records.active() and screen.records.watched()
    screen.show()
    context.set_visible(True)
    screen.shown()
    assert not screen.records._queued
    qtbot.waitUntil(lambda: bool(screen.records.watched()))
    context.set_visible(False)
    assert screen.records.active() and screen.records.watched()
    assert not screen._elapsed.isActive()


def test_records_error_retry_and_runs_error_precedence(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_flow(env)
    screen = screen_for(context, qtbot)
    original = flow_record.list_flows

    def refuse(_env: object) -> object:
        raise PermissionError("flow read refused")

    monkeypatch.setattr(flow_record, "list_flows", refuse)
    with qtbot.waitSignal(screen.records.failed, timeout=5000):
        screen.records.reload()
    assert screen.records_alert.isVisible()
    assert screen.live_card.isVisible()
    assert screen.records_alert.title_label.text() == (
        "Ember could not read the flow and panel records: flow read refused."
    )
    monkeypatch.setattr(flow_record, "list_flows", original)
    with qtbot.waitSignal(screen.records.changed, timeout=5000):
        screen.records_alert.buttons[0].click()
    assert screen.live_card.isVisible()
    assert not screen.records_alert.isVisible()
    monkeypatch.setattr(record, "list_runs", refuse)
    with qtbot.waitSignal(context.runs.failed, timeout=5000):
        context.runs.reload()
    assert screen.state_view.error.isVisible()
    assert not screen.live_card.isVisible()
    assert not screen.records_alert.isVisible()


def test_initial_load_waits_for_records(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    original = flows.read
    proc_locks = Path(env["HOME"]) / "proc-locks"
    proc_locks.write_text("")

    def delayed(_env: object, **_kwargs: object) -> flows.Snapshot:
        assert release.wait(5)
        return original(env, proc_locks=proc_locks)

    monkeypatch.setattr(flows, "read", delayed)
    screen = OverviewScreen(context)
    qtbot.addWidget(screen)
    screen.show()
    context.flows.set_active(True)
    screen.shown()
    assert not screen.state_view.empty.isVisible()
    assert not screen.live_card.isVisible()
    release.set()
    qtbot.waitUntil(screen.records.loaded)
    assert screen.live_empty.isVisible()
    assert screen.finished_empty.isVisible()


def test_record_keyboard_order_and_long_text(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    write_run(make_run(env, LIVE_A))
    flow = make_flow(env, status="stopped", reason="Reason " * 200)
    flow["tasks"][0].update(writer_run=LIVE_A, reason="x" * 1000)
    flow_record.save(flow, env)
    panel = make_panel(env, question="q" * 1000)
    panel["members"][0].update(run=LIVE_A, reason="r" * 1000)
    panel_record.save(panel, env)
    context.runs.reload()
    qtbot.waitUntil(lambda: bool(context.runs.live()))
    screen = screen_for(context, qtbot)
    screen.resize(900, 600)
    flow_row = screen.flow_rows[FLOW]
    panel_row = screen.panel_rows[PANEL]
    flow_row.expand_button.click()
    panel_row.expand_button.click()
    with qtbot.waitActive(screen):
        screen.activateWindow()
    controls = [
        flow_row.expand_button,
        screen.live_rows[LIVE_A].expand_button,
        screen.live_rows[LIVE_A].open_button,
        panel_row.expand_button,
    ]
    qtbot.waitUntil(lambda: all(control.isVisible() for control in controls))
    controls[0].setFocus()
    seen = [QApplication.focusWidget()]
    for _ in controls[1:]:
        screen.focusNextChild()
        seen.append(QApplication.focusWidget())
    assert seen == controls, (
        [(w.accessibleName(), w.isVisible()) for w in seen if w is not None],
        [(w.accessibleName(), w.isVisible()) for w in controls],
    )
    area = screen.findChild(QScrollArea)
    assert area is not None
    assert area.horizontalScrollBar().maximum() == 0
    assert screen.width() == 900
    assert not flow_row.detail_label.wordWrap()
    assert flow_row.detail_label.toolTip() == ""
    assert flow_row.details is not None
    assert all(
        label.sizePolicy().hasHeightForWidth()
        for label in flow_row.details.findChildren(QLabel)
    )


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_cancelled_parent_moves_live_workers_to_live_with_their_controls(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    qapp: QApplication,
    mode: str,
) -> None:
    from ember.app import theme

    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        flow = make_flow(
            env, status="cancelled", reason="Owner stopped it", ended_at=300
        )
        for ident, task in zip((LIVE_A, LIVE_B), flow["tasks"], strict=True):
            write_run(make_run(env, ident, status=record.WAITING))
            task["writer_run"] = ident
        flow_record.save(flow, env)
        context.runs.reload()
        qtbot.waitUntil(lambda: len(context.runs.live()) == 2)
        screen = screen_for(context, qtbot)
        row = screen.flow_rows[FLOW]
        assert screen.live_rows_layout.count() == 2
        item = screen.finished_rows_layout.itemAt(0)
        assert item is not None
        assert item.widget() is row
        assert row.badge.text() == "Incomplete" and row.badge.tone() == "incomplete"
        assert "live run" not in row.detail_label.text()
        assert "Owner stopped it" in row.detail_label.text()
        assert row.title_label.text().startswith("Flow · ")
        assert row.nested.count() == 0
        child = screen.live_rows[LIVE_A]
        assert child.detail_label.text().startswith("Flow · ")
        assert child.badge.text() == "Waiting for Claude"
        assert child.open_button.isVisible()
        child.expand_button.click()
        assert child.details is not None
        write_run(
            replace(child.run, status=record.CLOSED, ended_at=310, updated_at=310)
        )
        context.runs.reload()
        qtbot.waitUntil(lambda: LIVE_A in screen.finished_rows)
        assert screen.expanded_live is None
        assert row.nested.indexOf(screen.finished_rows[LIVE_A]) >= 0
        assert screen.live_rows_layout.count() == 1
        assert screen.finished_rows[LIVE_A].badge.text() == "Done"
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


def test_finished_cap_merges_types_and_keeps_all_nested_runs(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    top_ids = []
    for n in range(24):
        kind = ("flow", "panel", "codex")[n % 3]
        ident = f"{kind}-20260101-120000-{n:06x}"
        top_ids.append(ident)
        if kind == "flow":
            make_flow(env, ident, status="done", started_at=1, ended_at=1000 + n)
        elif kind == "panel":
            make_panel(env, ident, status="done", started_at=1, ended_at=1000 + n)
        else:
            write_run(
                make_run(
                    env, ident, status=record.CLOSED, started_at=1, ended_at=1000 + n
                )
            )
    hidden_child = "codex-20260101-120000-ff0000"
    write_run(make_run(env, hidden_child, status=record.CLOSED, ended_at=9000))
    hidden = flow_record.load(top_ids[0], env)
    hidden["tasks"][0]["writer_run"] = hidden_child
    flow_record.save(hidden, env)
    events = flow_record.folder(top_ids[21], env) / flow_record.EVENTS_NAME
    nested_ids = []
    for n in range(22):
        ident = f"codex-20260101-120000-ff{n + 1:04x}"
        nested_ids.append(ident)
        write_run(make_run(env, ident, status=record.CLOSED, started_at=1, ended_at=2))
    events.write_text(
        "".join(
            json.dumps({"seq": n + 1, "writer_run": ident}) + "\n"
            for n, ident in enumerate(nested_ids)
        )
    )
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 31)
    screen = screen_for(context, qtbot)
    assert screen.finished_toggle.text() == f"Newest {overview.FINISHED_LIMIT}"
    assert not screen.finished_cap.isVisible()
    open_finished(screen)
    assert screen.finished_cap.isVisible()
    assert screen.finished_cap.text() == "Filters cover the newest 20 records."
    layout = screen.finished_rows_layout
    assert layout.count() == 20
    row_ids = []
    for n in range(20):
        item = layout.itemAt(n)
        assert item is not None
        widget = item.widget()
        assert isinstance(widget, overview._ExpandableRow)
        row_ids.append(widget._item_id)
    assert row_ids == top_ids[:3:-1]
    assert hidden_child not in screen.live_rows
    assert hidden_child not in screen.finished_rows
    parent = screen.flow_rows[top_ids[21]]
    assert parent.nested.count() == 22
    assert all(
        parent.nested.indexOf(screen.finished_rows[ident]) >= 0 for ident in nested_ids
    )
    assert all(
        screen.finished_rows_layout.indexOf(screen.finished_rows[ident]) == -1
        for ident in nested_ids
    )


@pytest.mark.parametrize("panel_start", [99, 100, 101])
def test_multiple_parents_choose_oldest_then_id(
    env: dict[str, str], context: AppContext, qtbot: QtBot, panel_start: int
) -> None:
    flow = make_flow(env, status="stopped", started_at=100)
    flow["tasks"][0]["writer_run"] = LIVE_A
    flow_record.save(flow, env)
    panel = make_panel(env, started_at=panel_start)
    panel["members"][0]["run"] = LIVE_A
    panel_record.save(panel, env)
    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: bool(context.runs.live()))
    screen = screen_for(context, qtbot)
    flow_row, panel_row = screen.flow_rows[FLOW], screen.panel_rows[PANEL]
    owner, other = (panel_row, flow_row) if panel_start < 100 else (flow_row, panel_row)
    assert owner.nested.count() == 1 and other.nested.count() == 0
    item = owner.nested.itemAt(0)
    assert item is not None
    assert item.widget() is screen.live_rows[LIVE_A]
    assert "1 live run" in owner.detail_label.text()
    assert "live run" not in other.detail_label.text()
    assert screen.live_rows_layout.count() == 2
    item = screen.live_rows_layout.itemAt(0)
    assert item is not None
    assert item.widget() is owner


def test_mixed_order_uses_start_end_update_and_id(env: dict[str, str]) -> None:
    run = make_run(env, LIVE_A, status=record.RUNNING, started_at=10)
    flow = flows.FlowItem(make_flow(env, status="stopped", started_at=10), False, False)
    panel = flows.PanelItem(make_panel(env, started_at=9), "", False, False)
    live, finished, _ = overview.sections([run], [flow, panel])
    assert live == [panel, run, flow] and not finished
    run = replace(run, status=record.CLOSED, ended_at=20, updated_at=30)
    flow.record.update(status="cancelled", ended_at=20, updated_at=30)
    panel.record.update(status="incomplete", ended_at=None, updated_at=20)
    live, finished, _ = overview.sections([run], [panel, flow])
    assert not live and finished == [flow, run, panel]


def test_live_worker_of_an_ended_parent_is_a_top_level_row(
    env: dict[str, str],
) -> None:
    live_run = make_run(env, LIVE_A, status=record.RUNNING, started_at=10)
    done_run = make_run(env, DONE_A, status=record.CLOSED, started_at=5, ended_at=8)
    data = make_panel(env, started_at=1, status="done", ended_at=30)
    panel = flows.PanelItem(data, "", False, False, frozenset({LIVE_A, DONE_A}))
    live, finished, children = overview.sections([live_run, done_run], [panel])
    assert live == [live_run]
    assert finished == [panel]
    assert children[PANEL] == [done_run]


def test_live_worker_of_an_ended_panel_shows_in_live_with_its_parent(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    panel = make_panel(env, started_at=1, status="done", ended_at=2)
    panel["members"][0]["run"] = LIVE_A
    panel_record.save(panel, env)
    for index, run_id in enumerate((DONE_A, DONE_B, "codex-20260101-110000-eeeeee")):
        write_run(
            make_run(
                env, run_id, status=record.CLOSED, started_at=10 + index, ended_at=20
            )
        )
    write_run(make_run(env, LIVE_A, status=record.RUNNING, started_at=1))
    context.runs.reload()
    qtbot.waitUntil(lambda: bool(context.runs.live()))
    screen = screen_for(context, qtbot)
    row = screen.live_rows[LIVE_A]
    item = screen.live_rows_layout.itemAt(0)
    assert item is not None and item.widget() is row
    assert row.detail_label.text().startswith("Panel · 1 member · ")
    assert PANEL not in [
        key for key, panel_row in screen.panel_rows.items() if panel_row.isVisible()
    ]


def test_unreadable_line_combines_counts_inside_live(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    for kind, filename in (("codex", "run"), ("flow", "flow"), ("panel", "panel")):
        folder = paths.runs_dir(env) / f"{kind}-20260101-120000-abcdef"
        folder.mkdir()
        (folder / f"{filename}.json").write_text("broken")
    context.runs.reload()
    qtbot.waitUntil(lambda: context.runs.unreadable() == 1)
    screen = screen_for(context, qtbot)
    assert screen.unreadable_alert.isVisible()
    assert screen.live_card.isAncestorOf(screen.unreadable_alert)
    assert (
        screen.unreadable_alert.title_label.text()
        == "1 run, 1 flow, 1 panel could not be read."
    )
    assert screen.live_empty.isVisible() and screen.finished_empty.isVisible()
    qtbot.waitUntil(
        lambda: screen.unreadable_alert.y() > screen.live_empty.geometry().bottom()
    )
    assert overview.unreadable_line(0, 2, 0) == "2 flows could not be read."


@pytest.mark.parametrize(
    ("status", "text", "tone"),
    [
        ("starting", "Starting", "neutral"),
        ("checking", "Running", "info"),
        ("stopped", "Waiting for Claude", "warning"),
        ("done", "Done", "success"),
        ("cancelled", "Incomplete", "incomplete"),
        ("failed", "Failed", "danger"),
    ],
)
def test_flow_status_chips(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    status: str,
    text: str,
    tone: str,
) -> None:
    make_flow(env, status=status, updated_at=9_000_000_000)
    screen = screen_for(context, qtbot)
    row = screen.flow_rows[FLOW]
    assert (row.badge.text(), row.badge.tone()) == (text, tone)
    assert row.detail_label.text().startswith(f"ember/flow/{FLOW}")


def test_finished_filters_combine_and_persist(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    open_finished(screen)
    flow = make_flow(env, status="cancelled", ended_at=1_800_000_600)
    flow["tasks"][0]["writer_run"] = DONE_A
    flow_record.save(flow, env)
    panel = make_panel(env, status="done", ended_at=1_800_000_700)
    panel["members"][0]["run"] = LIVE_B
    panel_record.save(panel, env)
    context.flows.reload()
    qtbot.waitUntil(lambda: FLOW in screen.flow_rows and PANEL in screen.panel_rows)
    screen.engine_filter.setCurrentText("OpenCode")
    assert screen._shown_finished == (FLOW,)
    assert DONE_A in screen.finished_rows
    screen.status_filter.setCurrentText("Done")
    assert not screen._shown_finished
    assert screen.finished_empty.isVisible()
    assert not screen.clear_button.isVisibleTo(screen)
    screen.status_filter.setCurrentText("Incomplete")
    assert screen._shown_finished == (FLOW,)
    screen.type_filter.setCurrentText("Run")
    assert not screen._shown_finished
    screen.type_filter.setCurrentText("Flow")
    screen.hide()
    context.runs.reload()
    context.flows.reload()
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    screen.show()
    screen.shown()
    assert screen._shown_finished == (FLOW,)
    assert screen.type_filter.currentText() == "Flow"
    assert screen.status_filter.currentText() == "Incomplete"
    assert screen.engine_filter.currentText() == "OpenCode"
    screen.type_filter.setCurrentText("Panel")
    screen.status_filter.setCurrentText("Done")
    screen.engine_filter.setCurrentText("Claude")
    assert screen._shown_finished == (PANEL,)
    assert LIVE_B in screen.live_rows
    screen.type_filter.setCurrentText("Run")
    screen.status_filter.setCurrentText("Failed")
    screen.engine_filter.setCurrentText("Codex")
    assert screen._shown_finished == (DONE_B,)
    fresh = OverviewScreen(context)
    qtbot.addWidget(fresh)
    assert all(
        combo.value() == "all"
        for combo in (fresh.type_filter, fresh.status_filter, fresh.engine_filter)
    )
    assert fresh.type_filter.currentText() == "All types"


def test_filters_apply_after_cap_and_ignore_unreadable_workers(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    flow = make_flow(env, status="done", ended_at=1000)
    flow["tasks"][0]["writer_run"] = DONE_A
    flow_record.save(flow, env)
    folder = paths.runs_dir(env) / DONE_A
    folder.mkdir()
    (folder / "run.json").write_text("broken")
    for n in range(20):
        write_run(
            make_run(
                env, f"codex-20260101-120000-{n:06x}", status=record.CLOSED, ended_at=n
            )
        )
    screen = screen_for(context, qtbot)
    open_finished(screen)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 20)
    screen.type_filter.setCurrentText("Run")
    assert len(screen._shown_finished) == 19
    screen.type_filter.setCurrentText("Flow")
    assert screen._shown_finished == (FLOW,)
    screen.engine_filter.setCurrentText("Codex")
    assert not screen._shown_finished
    screen.engine_filter.setCurrentText("OpenCode")
    assert not screen._shown_finished
    screen.engine_filter.setCurrentText("All engines")
    assert screen._shown_finished == (FLOW,)


def wait_prune_dialog(screen: OverviewScreen, qtbot: QtBot) -> overview._PruneDialog:
    qtbot.waitUntil(lambda: screen._prune_dialog is not None)
    dialog = screen._prune_dialog
    assert dialog is not None
    return dialog


@pytest.mark.parametrize("cancel", [True, False])
def test_clear_uses_frozen_ids_and_background_tasks(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    screen = loaded(env, context, qtbot)
    open_finished(screen)
    screen.engine_filter.setCurrentText("Codex")
    calls: list[tuple[int, tuple[str, ...], bool]] = []
    release = threading.Event()

    def remove(
        older_than: int,
        record_ids: Sequence[str] | None = None,
        *,
        dry_run: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> prune.Result:
        assert threading.current_thread() is not threading.main_thread()
        assert env == context.env
        calls.append((older_than, tuple(record_ids or ()), dry_run))
        assert release.wait(5)
        return prune.Result(removed=[dict(kind="run", id=DONE_B)])

    monkeypatch.setattr(prune, "remove", remove)
    screen.clear_button.click()
    assert not screen.clear_button.isEnabled()
    screen.engine_filter.setCurrentText("OpenCode")
    screen.refresh()
    assert not screen.clear_button.isEnabled()
    screen.clear_finished()
    release.set()
    dialog = wait_prune_dialog(screen, qtbot)
    assert dialog.windowTitle() == "Remove finished records?"
    assert DONE_B in dialog.report.text() and DONE_A not in dialog.report.text()
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
    if cancel:
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        assert screen._prune_dialog is None
    else:
        release.clear()
        dialog.accept()
        assert not screen.clear_button.isEnabled()
        release.set()
        result = wait_prune_dialog(screen, qtbot)
        assert result.report.text() == f"Removed run {DONE_B}"
        reloaded: list[str] = []
        monkeypatch.setattr(context.runs, "reload", lambda: reloaded.append("runs"))
        monkeypatch.setattr(context.flows, "reload", lambda: reloaded.append("parents"))
        result.accept()
        assert reloaded == ["runs", "parents"]
    assert calls == [(0, (DONE_B,), True)] + ([] if cancel else [(0, (DONE_B,), False)])
    assert screen.engine_filter.currentText() == "OpenCode"
    assert screen.clear_button.isEnabled()


@pytest.mark.parametrize("failure", ["skipped", "exception", "errors", "remove_error"])
def test_clear_reports_skips_and_errors(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    screen = loaded(env, context, qtbot)
    calls: list[bool] = []

    def remove(
        older_than: int,
        record_ids: Sequence[str] | None = None,
        *,
        dry_run: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> prune.Result:
        calls.append(dry_run)
        if failure == "exception" or (failure == "remove_error" and not dry_run):
            raise OSError("read refused")
        if failure == "skipped":
            return prune.Result(skipped=[dict(id=DONE_B, reason="busy")])
        return prune.Result(
            removed=[dict(kind="run", id=DONE_B)],
            errors=["read refused"] if failure == "errors" else [],
        )

    monkeypatch.setattr(prune, "remove", remove)
    screen.clear_button.click()
    dialog = wait_prune_dialog(screen, qtbot)
    if failure == "remove_error":
        dialog.accept()
        dialog = wait_prune_dialog(screen, qtbot)
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel) is None
    assert (
        f"Skipped {DONE_B}: busy" if failure == "skipped" else "read refused"
    ) in dialog.report.text()
    assert calls == ([True, False] if failure == "remove_error" else [True])
    assert screen.clear_button.isEnabled()
    dialog.accept()


def test_clear_prunes_nested_records_and_rechecks_state(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Artifact inspection is covered by prune's repository tests.
    monkeypatch.setattr(prune, "_artifacts", lambda *_args, **_kwargs: prune.Result())
    flow = make_flow(env, status="done", ended_at=200)
    flow["tasks"][0]["writer_run"] = DONE_A
    flow_record.save(flow, env)
    for ident in (DONE_A, DONE_B):
        run = write_run(
            make_run(
                env,
                ident,
                status=record.CLOSED,
                turn_state=record.COMMITTED,
                ended_at=200,
            )
        )
        (run.folder / "worker.log").write_text("log")
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 2)
    screen = screen_for(context, qtbot)
    screen.clear_button.click()
    dialog = wait_prune_dialog(screen, qtbot)
    assert all(ident in dialog.report.text() for ident in (FLOW, DONE_A, DONE_B))
    write_run(replace(record.load(DONE_B, env), status=record.WAITING, ended_at=None))
    dialog.accept()
    result = wait_prune_dialog(screen, qtbot)
    assert f"Removed flow {FLOW}" in result.report.text()
    assert f"Removed run {DONE_A}" in result.report.text()
    assert f"Skipped {DONE_B}: not ended" in result.report.text()
    assert not (paths.runs_dir(env) / FLOW).exists()
    assert not (paths.runs_dir(env) / DONE_A).exists()
    assert (paths.runs_dir(env) / DONE_B / "worker.log").exists()
    result.accept()
    qtbot.waitUntil(lambda: not screen._shown_finished)
    assert not screen.clear_button.isVisibleTo(screen)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_prune_dialog_scrolls_and_lists_commands(
    qapp: QApplication, qtbot: QtBot, mode: str
) -> None:
    from ember.app import theme

    result = prune.Result(
        removed=[dict(kind="run", id=f"run-{n}") for n in range(100)],
        skipped=[dict(id="busy-id", reason="busy")],
        branches=[dict(branch="worker", command="git -C /repo branch -D worker")],
        worktrees=[
            dict(
                branch="worker",
                worktree="/repo/tree",
                command="git -C /repo worktree remove /repo/tree",
            ),
            dict(
                branch="other",
                worktree="/repo/other",
                command="git -C /repo worktree remove /repo/other",
            ),
        ],
        errors=["read error"],
    )
    text = overview.prune_text(result, dry_run=True)
    assert (
        text.index("Would remove run run-99")
        < text.index("Skipped busy-id: busy")
        < text.index("Branch worker:")
    )
    assert (
        "Branch worker: git -C /repo branch -D worker\n"
        "  Worktree /repo/tree: git -C /repo worktree remove /repo/tree" in text
    )
    assert "Branch other\n  Worktree /repo/other:" in text
    assert text.endswith("Error: read error")
    parent = QWidget()
    qtbot.addWidget(parent)
    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        dialog = overview._PruneDialog(
            "Remove finished records?", text, parent, confirm=True
        )
        dialog.show()
        qtbot.waitUntil(lambda: dialog.area.verticalScrollBar().maximum() > 0)
        assert (
            dialog.frameGeometry().height()
            <= dialog.screen().availableGeometry().height()
        )
        assert dialog.area.horizontalScrollBar().maximum() == 0
        assert sorted(button.text() for button in dialog.buttons.buttons()) == [
            "Cancel",
            "Remove",
        ]
        qtbot.keyClick(dialog, Qt.Key.Key_Return)
        assert dialog.result() == QDialog.DialogCode.Rejected
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


@pytest.fixture(params=["flow", "panel"])
def nested_screen(
    env: dict[str, str],
    context: AppContext,
    qtbot: QtBot,
    request: pytest.FixtureRequest,
) -> OverviewScreen:
    for ident, status in ((LIVE_A, record.RUNNING), (DONE_A, record.CLOSED)):
        write_run(make_run(env, ident, status=status))
    if request.param == "flow":
        flow = make_flow(env, status="running")
        flow["tasks"][0]["writer_run"] = LIVE_A
        flow["tasks"][1]["writer_run"] = DONE_A
        flow_record.save(flow, env)
    else:
        make_panel(
            env, members=[make_member(1, run=LIVE_A), make_member(2, run=DONE_A)]
        )
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 2)
    return screen_for(context, qtbot)


@pytest.mark.parametrize("finished", [False, True])
def test_nested_button_keeps_focus_on_refresh(
    nested_screen: OverviewScreen, qtbot: QtBot, finished: bool
) -> None:
    screen = nested_screen
    row = screen.finished_rows[DONE_A] if finished else screen.live_rows[LIVE_A]
    screen.activateWindow()
    row.expand_button.setFocus()
    qtbot.waitUntil(row.expand_button.hasFocus)
    screen.refresh()
    assert row.expand_button.hasFocus()


@pytest.mark.parametrize("finished", [False, True])
def test_nested_row_is_not_hidden_or_reparented_on_refresh(
    nested_screen: OverviewScreen, finished: bool
) -> None:
    class Events(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[QEvent.Type] = []

        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            self.seen.append(event.type())
            return False

    screen = nested_screen
    row = screen.finished_rows[DONE_A] if finished else screen.live_rows[LIVE_A]
    events = Events()
    row.installEventFilter(events)
    screen.refresh()
    assert QEvent.Type.ParentChange not in events.seen
    assert QEvent.Type.Hide not in events.seen


@pytest.mark.parametrize(
    ("status", "expected"),
    [("running", "1 flow, 1 panel live"), ("cancelled", "1 run, 1 panel live")],
)
def test_subtitle_stays_hidden_with_live_parents(
    env: dict[str, str], context: AppContext, qtbot: QtBot, status: str, expected: str
) -> None:
    flow = make_flow(env, status=status)
    flow["tasks"][0]["writer_run"] = LIVE_A
    flow_record.save(flow, env)
    make_panel(env, members=[make_member(run=LIVE_B)])
    for ident in (LIVE_A, LIVE_B):
        write_run(make_run(env, ident))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 2)
    screen = screen_for(context, qtbot)
    icon = tray.Tray(context, lambda: None, lambda: None, lambda: None)
    assert screen.subtitle.isHidden()
    assert icon.lines()[0] == expected
    icon.menu.deleteLater()


def test_nested_worker_survives_removed_parent(
    nested_screen: OverviewScreen, context: AppContext, qtbot: QtBot
) -> None:
    screen = nested_screen
    child = screen.live_rows[LIVE_A]
    parent = next(iter([*screen.flow_rows.values(), *screen.panel_rows.values()]))
    shutil.rmtree(paths.runs_dir(context.env) / parent.item.record["id"])
    context.flows.reload()
    qtbot.waitUntil(lambda: not screen.flow_rows and not screen.panel_rows)
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    assert screen.live_rows[LIVE_A] is child
    assert child.isVisible()
    assert screen.live_rows_layout.indexOf(child) == 0
    child.expand_button.click()
    assert screen.expanded_live == LIVE_A


def _write_closed(env: dict[str, str], count: int) -> None:
    for n in range(count):
        write_run(
            make_run(
                env,
                f"codex-20260101-120000-{n:06x}",
                status=record.CLOSED,
                ended_at=1_800_000_000 + n,
            )
        )


def test_finished_preview_shows_three_and_show_all_is_kept(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    _write_closed(env, 5)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 5)
    screen = screen_for(context, qtbot)
    assert not screen.finished_open
    assert screen.finished_rows_layout.count() == overview.FINISHED_PREVIEW
    assert screen._shown_finished == tuple(
        f"codex-20260101-120000-{n:06x}" for n in (4, 3, 2)
    )
    assert not screen.filter_bar.isVisible()
    assert screen.finished_toggle.text() == "Show all 5"
    screen.finished_toggle.click()
    assert screen.finished_open
    assert screen.finished_rows_layout.count() == 5
    assert screen.filter_bar.isVisible()
    assert screen.finished_toggle.text() == "Show fewer"
    assert state.load(env).finished_open
    fresh = screen_for(context, qtbot)
    assert fresh.finished_open
    assert fresh.filter_bar.isVisible()
    fresh.finished_toggle.click()
    assert not state.load(env).finished_open


def test_show_all_is_hidden_when_the_preview_holds_everything(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    _write_closed(env, 2)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 2)
    screen = screen_for(context, qtbot)
    assert screen.finished_toggle.isHidden()
    assert screen.finished_rows_layout.count() == 2


def test_rows_line_up_in_fixed_columns(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    flow = make_flow(env, status="done", ended_at=1_800_000_900)
    flow_record.save(flow, env)
    screen = loaded(env, context, qtbot)
    open_finished(screen)
    qtbot.waitUntil(lambda: FLOW in screen.flow_rows)
    rows = [
        *screen.live_rows.values(),
        *screen.finished_rows.values(),
        screen.flow_rows[FLOW],
    ]
    qtbot.waitUntil(lambda: all(row.isVisible() for row in rows))

    def left(widget: QWidget, row: QWidget) -> int:
        return widget.mapTo(row, widget.rect().topLeft()).x()

    for part in ("expand_button", "badge", "title_label"):
        assert len({left(getattr(row, part), row) for row in rows}) == 1, part

    def middle(widget: QWidget, row: QWidget) -> int:
        return widget.mapTo(row, widget.rect().center()).y()

    for row in rows:
        lines = (
            row.title_label.mapTo(row, row.title_label.rect().topLeft()).y()
            + row.detail_label.mapTo(row, row.detail_label.rect().bottomLeft()).y()
        ) // 2
        assert abs(middle(row.expand_button, row) - lines) <= 1
        assert abs(middle(row.badge, row) - lines) <= 1
    for row in rows:
        assert row.expand_button.property("kind") == "secondary"
        assert not row.detail_label.wordWrap()


def test_nested_runs_are_oldest_first(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    late = "codex-20260101-130000-aaaaaa"
    early = "claude-20260101-110000-bbbbbb"
    write_run(make_run(env, late, status=record.CLOSED, started_at=3000, ended_at=3100))
    write_run(
        make_run(env, early, status=record.CLOSED, started_at=1000, ended_at=5000)
    )
    flow = make_flow(env, status="done", ended_at=6000)
    flow["tasks"][0]["writer_run"] = late
    flow["tasks"][0]["review_run"] = early
    flow_record.save(flow, env)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 2)
    screen = screen_for(context, qtbot)
    nested = screen.flow_rows[FLOW].nested
    order = []
    for n in range(nested.count()):
        item = nested.itemAt(n)
        assert item is not None
        widget = item.widget()
        assert isinstance(widget, overview._Row)
        order.append(widget.run.id)
        assert widget.nested
        assert widget.time_label.text() == overview.end_text(
            widget.run.started_at, overview.run_ended(widget.run)
        )
    assert order == [early, late]
    assert screen.finished_rows[early].time_label.text() == "Elapsed 1 h 6 min"


def test_clear_is_a_red_outline_without_a_tooltip(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    open_finished(screen)
    assert screen.clear_button.property("kind") == "danger-outline"
    assert screen.clear_button.accessibleName() == "Clear"
    assert screen.clear_button.accessibleDescription() == "Remove shown records"
    assert not screen.clear_button.icon().isNull()
    assert screen.clear_button.toolTip() == ""
    assert not hasattr(screen, "clear_caption")


def test_rows_show_the_end_time_on_the_right(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    for row in screen.live_rows.values():
        assert row.time_label.text() == ""
    for row in screen.finished_rows.values():
        ended = overview.run_ended(row.run)
        assert ended is not None
        assert row.time_label.text() == (
            f"Elapsed {overview.took(ended - row.run.started_at)}"
        )


def _stamp(day: int, hour: int, minute: int, second: int = 0) -> float:
    return datetime.datetime(2026, 11, day, hour, minute, second).timestamp()


def test_span_text() -> None:
    now = _stamp(3, 12, 0)
    assert overview.span_text(_stamp(3, 8, 17), _stamp(3, 8, 33), now) == (
        "08:17 to 08:33"
    )
    assert overview.span_text(_stamp(2, 23, 50), _stamp(3, 0, 10), now) == (
        "yesterday 23:50 to 00:10"
    )
    assert overview.span_text(_stamp(3, 11, 55, 48), None, now) == (
        "started 11:55 · 4 min 12 s"
    )
    assert overview.span_text(_stamp(2, 23, 0), None, now).startswith(
        "started yesterday 23:00 · 13 h 0 min 0 s"
    )


def test_elapsed() -> None:
    assert overview.elapsed(0) == "0 s"
    assert overview.elapsed(45.9) == "45 s"
    assert overview.elapsed(252) == "4 min 12 s"
    assert overview.elapsed(3600) == "1 h 0 min 0 s"
    assert overview.elapsed(-5) == "0 s"


def test_end_text() -> None:
    assert overview.end_text(_stamp(3, 8, 17), None) == ""
    assert overview.end_text(_stamp(3, 8, 17), _stamp(3, 8, 33)) == "Elapsed 16 min 0 s"
    assert overview.end_text(_stamp(3, 8, 0), _stamp(3, 9, 12, 30)) == (
        "Elapsed 1 h 12 min"
    )
    assert overview.end_text(0, 45.9) == "Elapsed 45 s"


def test_run_result(env: dict[str, str]) -> None:
    live = make_run(env, status=record.RUNNING, turn=2, commits=["a"])
    assert overview.run_result(live) == "turn 2 · 1 commit"
    reader = make_run(env, status=record.WAITING, read_only=True, turn=1)
    assert overview.run_result(reader) == "turn 1 · read-only"
    done = make_run(env, status=record.CLOSED, commits=["a", "b", "c"])
    assert overview.run_result(done) == "3 commits"
    failed = make_run(env, status=record.FAILED, last_reason="Sign-in needed")
    assert overview.run_result(failed) == "Sign-in needed"
    unknown = make_run(env, status=record.FAILED, last_reason=None)
    assert overview.run_result(unknown) == "No reason recorded"


def test_finished_parent_hides_its_runs_until_expanded(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    first = "codex-20260101-130000-aaaaaa"
    second = "claude-20260101-110000-bbbbbb"
    write_run(
        make_run(env, first, status=record.CLOSED, started_at=3000, ended_at=3100)
    )
    write_run(
        make_run(env, second, status=record.CLOSED, started_at=1000, ended_at=5000)
    )
    flow = make_flow(env, status="done", ended_at=6000)
    flow["tasks"][0]["writer_run"] = first
    flow["tasks"][0]["review_run"] = second
    flow_record.save(flow, env)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.runs()) == 2)
    screen = screen_for(context, qtbot)
    row = screen.flow_rows[FLOW]
    assert row.collapsed_runs
    assert not row.nested_box.isVisible()
    assert row.detail_label.text().startswith("2 runs · ")
    row.expand_button.click()
    assert row.nested_box.isVisible()
    assert screen.finished_rows[first].isVisible()
    row.expand_button.click()
    assert not row.nested_box.isVisible()


def test_a_run_without_a_subject_keeps_its_worker_in_the_title(
    env: dict[str, str],
) -> None:
    run = replace(make_run(env, LIVE_A), subject="  ")
    assert overview.worker_line(
        replace(run, engine="codex", effort="low", speed="fast")
    ) == (f"Codex · {run.model} · low · fast")
    assert overview.run_title(run) == f"Run · {overview.worker_line(run)}"
    assert not overview.run_detail(run).startswith(overview.worker_line(run))
    run = replace(run, subject="Fix the\nbug")
    assert overview.run_title(run) == "Run · Fix the bug"


def _write_denial(
    run: record.Run, path: Path, *, reason: str = "hidden", allow: str | None = None
) -> None:
    record.write_private(
        record.outcome_path(run, run.turn),
        json.dumps(
            {
                "state": "done",
                "reason": None,
                "provider_error": False,
                "signed_out": False,
                "incomplete": None,
                "denied_paths": [
                    {"path": str(path), "reason": reason, "allow_path": allow}
                ],
            }
        ),
    )


def test_denial_notice_confirms_and_saves_read_permission(
    context: AppContext,
    qtbot: QtBot,
    env: dict[str, str],
) -> None:
    from ember import choices

    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, folder, allow=str(folder))
    stamp = datetime.datetime(2026, 11, 5, 14, 35).timestamp()
    os.utime(record.outcome_path(run, run.turn), (stamp, stamp))
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    assert len(screen.denied_notices) == 1
    notice = next(iter(screen.denied_notices.values()))
    assert notice.title_label.text() == f"A worker could not read {folder}"
    assert notice.text_label.text() == (
        "1 report · 0 flows, 0 panels, 1 standalone run"
        " · first 2026-11-05 · latest 2026-11-05 14:35 (local)"
    )
    assert notice.offer_label.text() == f"Allow reading {folder} for future workers."
    assert run.id in notice.reports_label.text()
    assert not notice.runs_toggle.isChecked()
    assert not notice.reports_label.isVisible()
    notice.runs_toggle.click()
    assert notice.reports_label.isVisible()
    notice.runs_toggle.click()
    assert not notice.reports_label.isVisible()
    assert [b.text() for b in notice.buttons] == [
        "Allow reading",
        "Dismiss",
        "Don't show again",
    ]
    # Redrawing an unchanged notice keeps its buttons and keyboard focus.
    screen.check_denials()
    assert next(iter(screen.denied_notices.values())) is notice
    notice.buttons[0].click()
    dialog = screen._denied_dialog
    assert dialog is not None
    assert f"Every future worker can read {folder}" in dialog.report.text()
    assert "No worker can change it" in dialog.report.text()
    assert choices.read(env=env)["answers"]["sandbox.read"]["value"] == []
    dialog.reject()
    assert choices.read(env=env)["answers"]["sandbox.read"]["value"] == []
    notice.buttons[0].click()
    assert screen._denied_dialog is not None
    with qtbot.waitSignal(context.settings_changed):
        screen._denied_dialog.accept()
    assert choices.read(env=env)["answers"]["sandbox.read"]["value"] == [str(folder)]
    assert screen.denied_notices == {}


@pytest.mark.parametrize("reason", ["secret location", "protected"])
def test_restricted_denial_notice_has_no_allow_reading(
    context: AppContext,
    qtbot: QtBot,
    env: dict[str, str],
    reason: str,
) -> None:
    folder = Path(env["HOME"]) / ".ssh"
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, folder, reason=reason)
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    notice = next(iter(screen.denied_notices.values()))
    assert [b.text() for b in notice.buttons] == ["Dismiss", "Don't show again"]
    assert ("credentials" in notice.offer_label.text()) is (reason == "secret location")
    notice.buttons[0].click()
    assert screen.denied_notices == {}
    screen.check_denials()
    assert screen.denied_notices == {}
    # Persisted dismissal survives a fresh screen, until another turn reports it.
    screen.close()
    screen = screen_for(context, qtbot)
    assert screen.denied_notices == {}
    later = write_run(replace(run, turn=2, updated_at=run.updated_at + 1))
    _write_denial(later, folder, reason=reason)
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    qtbot.waitUntil(lambda: len(screen.denied_notices) == 1)


@pytest.mark.parametrize("reason", ["hidden", "secret location", "protected"])
@pytest.mark.parametrize("parent", ["run", "flow", "panel"])
def test_dont_show_again_hides_resolved_folder_for_later_reports(
    context: AppContext,
    qtbot: QtBot,
    env: dict[str, str],
    reason: str,
    parent: str,
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    alias = folder.with_name("alias")
    alias.symlink_to(folder, target_is_directory=True)
    other = folder.with_name("other")
    run = write_run(make_run(env, LIVE_A))
    if parent == "flow":
        flow = make_flow(env)
        flow["tasks"][0]["writer_run"] = run.id
        flow_record.save(flow, env)
    elif parent == "panel":
        make_panel(env, members=[make_member(run=run.id)])
    _write_denial(run, alias, reason=reason)
    outcome = record.outcome_path(run, run.turn).read_bytes()
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    before = context.runs.runs()
    records = (screen.records.flows(), screen.records.panels())
    with qtbot.waitSignal(context.settings_changed):
        screen.denied_notices[str(folder)].buttons[-1].click()
    assert screen.denied_notices == {}
    assert context.setting("sandbox.hidden_notices") == [str(folder)]
    assert state.load(env).denied_hidden == []
    assert state.load(env).denied_dismissals == {}
    assert context.runs.runs() == before
    assert (screen.records.flows(), screen.records.panels()) == records
    assert record.outcome_path(run, run.turn).read_bytes() == outcome
    assert record.run_denials(run)
    screen.close()
    screen = screen_for(context, qtbot)
    assert screen.denied_notices == {}
    later = write_run(replace(run, turn=2, updated_at=run.updated_at + 1))
    _write_denial(later, folder, reason=reason)
    new_run = write_run(make_run(env, LIVE_B))
    _write_denial(new_run, alias, reason=reason)
    unrelated = write_run(make_run(env, DONE_A))
    _write_denial(unrelated, other)
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    qtbot.waitUntil(lambda: list(screen.denied_notices) == [str(other)])
    assert len(context.runs.live()) == 3


def test_settings_show_again_restores_dismissed_denial_notice(
    context: AppContext, qtbot: QtBot, env: dict[str, str]
) -> None:
    from ember.app.screens.settings import SettingsScreen

    folder = Path(env["HOME"]) / ".toolchain"
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, folder)
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    settings = SettingsScreen(context)
    qtbot.addWidget(settings)
    assert settings.hidden_rows == {}
    with qtbot.waitSignal(context.settings_changed):
        screen.denied_notices[str(folder)].buttons[-1].click()
    saved = state.load(env)
    state.save(replace(saved, denied_dismissals={str(folder): [f"{run.id}:1"]}), env)
    screen.hide()
    settings.shown()
    with qtbot.waitSignal(context.settings_changed):
        settings.hidden_rows[str(folder)].click()
    assert context.setting("sandbox.hidden_notices") == []
    assert state.load(env).denied_hidden == []
    assert state.load(env).denied_dismissals == {}
    screen.show()
    screen.shown()
    assert list(screen.denied_notices) == [str(folder)]


@pytest.mark.parametrize("spelling", ["trailing slash", "alias", "parent"])
def test_hidden_notice_setting_matches_resolved_folders(
    context: AppContext, qtbot: QtBot, env: dict[str, str], spelling: str
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    (folder / "bin").mkdir(parents=True)
    alias = folder.with_name("alias")
    alias.symlink_to(folder, target_is_directory=True)
    denied = folder / "bin" if spelling == "parent" else folder
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, denied)
    value = {
        "trailing slash": f"{folder}/",
        "alias": str(alias),
        "parent": str(folder),
    }[spelling]
    choices.set_settings({"sandbox.hidden_notices": [value]}, env=env)
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    assert screen.denied_notices == {}


def test_folders_hidden_in_the_state_file_move_into_the_setting(
    context: AppContext, qtbot: QtBot, env: dict[str, str]
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, folder)
    state.save(replace(state.load(env), denied_hidden=[str(folder)]), env)
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    with qtbot.waitSignal(context.settings_changed):
        screen = screen_for(context, qtbot)
    assert screen.denied_notices == {}
    assert context.setting("sandbox.hidden_notices") == [str(folder)]
    qtbot.waitUntil(lambda: state.load(env).denied_hidden == [])
    assert screen.denied_notices == {}


def test_showing_overview_rechecks_paths_allowed_while_hidden(
    context: AppContext, qtbot: QtBot, env: dict[str, str]
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, folder, allow=str(folder))
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    assert len(screen.denied_notices) == 1
    screen.hide()
    with qtbot.waitSignal(context.settings_changed):
        context.save_settings({"sandbox.read": [str(folder)]})
    screen.show()
    screen.shown()
    assert screen.denied_notices == {}


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("hidden", "secret location", "secret location"),
        ("secret location", "hidden", "secret location"),
        ("secret location", "protected", "protected"),
        ("protected", "secret location", "protected"),
    ],
)
def test_denial_notice_keeps_strongest_reason_across_turns(
    context: AppContext,
    qtbot: QtBot,
    env: dict[str, str],
    first: str,
    second: str,
    expected: str,
) -> None:
    folder = Path(env["HOME"]) / ".docker"
    folder.mkdir()
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, folder, reason=first)
    later = write_run(replace(run, turn=2, updated_at=run.updated_at + 1))
    _write_denial(later, folder, reason=second)
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    assert len(screen.denied_notices) == 1
    notice = next(iter(screen.denied_notices.values()))
    assert [action.text() for action in notice.buttons] == [
        "Dismiss",
        "Don't show again",
    ]
    assert ("holds credentials" in notice.offer_label.text()) is (
        expected == "secret location"
    )
    assert ("protected from workers" in notice.offer_label.text()) is (
        expected == "protected"
    )


def test_denial_reports_count_distinct_sources_and_keep_expansion(
    context: AppContext, qtbot: QtBot, env: dict[str, str]
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    alias = folder.with_name("alias")
    alias.symlink_to(folder, target_is_directory=True)
    runs = [
        write_run(make_run(env, run_id, claim="test-claim"))
        for run_id in (LIVE_A, LIVE_B, DONE_A, DONE_B)
    ]
    first = datetime.datetime(2026, 11, 5, 10).timestamp()
    last = datetime.datetime(2026, 11, 7, 18, 42).timestamp()
    for run, stamp in zip(runs, (last, last, first + 60, first), strict=True):
        _write_denial(run, folder, allow=str(folder))
        os.utime(record.outcome_path(run, 1), (stamp, stamp))
    flow = make_flow(env)
    flow["tasks"][0].update(writer_run=LIVE_A, review_run=LIVE_B)
    flow_record.save(flow, env)
    make_panel(env, members=[make_member(run=DONE_A)])
    # Two aliases in one turn are one report for the resolved folder.
    outcome = record.outcome_path(runs[-1], 1)
    value = json.loads(outcome.read_text())
    value["denied_paths"].append(
        {"path": str(alias), "reason": "hidden", "allow_path": str(alias)}
    )
    outcome.write_text(json.dumps(value))
    os.utime(outcome, (first, first))
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    notice = screen.denied_notices[str(folder)]
    assert notice.text_label.text() == (
        "4 reports · 1 flow, 1 panel, 1 standalone run"
        " · first 2026-11-05 · latest 2026-11-07 18:42 (local)"
    )
    lines = notice.reports_label.text().splitlines()
    assert len(lines) == 4
    assert [next(run.id for run in runs if run.id in line) for line in lines] == [
        LIVE_B,
        LIVE_A,
        DONE_A,
        DONE_B,
    ]
    assert all("claim test-claim" in line and "turn 1" in line for line in lines)
    assert all(
        f"Flow · project · claim test-claim · {FLOW}" in line for line in lines[:2]
    )
    assert f"Panel · 1 member · claim test-claim · {PANEL}" in lines[2]
    assert " · Run · claim test-claim · " in lines[3]
    notice.runs_toggle.click()
    screen.check_denials()
    assert screen.denied_notices[str(folder)] is notice
    assert notice.reports_label.isVisible()

    later = write_run(replace(runs[-1], turn=2))
    _write_denial(later, folder, allow=str(folder))
    os.utime(record.outcome_path(later, 2), (last + 60, last + 60))
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen.check_denials()
    updated = screen.denied_notices[str(folder)]
    assert updated is not notice
    assert updated.text_label.text().startswith(
        "5 reports · 1 flow, 1 panel, 1 standalone run"
    )
    assert updated.runs_toggle.isChecked()
    assert updated.reports_label.isVisible()
    assert f"{DONE_B} · turn 2" in updated.reports_label.text().splitlines()[0]
    updated.runs_toggle.click()
    # A changed timestamp redraws the notice without reopening the list.
    os.utime(record.outcome_path(later, 2), (last + 120, last + 120))
    screen.check_denials()
    assert not screen.denied_notices[str(folder)].runs_toggle.isChecked()


@pytest.mark.parametrize("all_unknown", [False, True])
def test_denial_report_missing_mtimes_sort_last(
    context: AppContext,
    qtbot: QtBot,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    all_unknown: bool,
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    runs = [write_run(make_run(env, run_id, turn=2)) for run_id in (LIVE_A, LIVE_B)]
    for run in runs:
        for turn in (1, 2):
            _write_denial(replace(run, turn=turn), folder)
            stamp = datetime.datetime(2026, 11, 5, 10, 30).timestamp()
            os.utime(record.outcome_path(run, turn), (stamp, stamp))
    stat = Path.stat
    missing = {
        record.outcome_path(run, turn)
        for run in runs
        for turn in (1, 2)
        if all_unknown or (run.id, turn) == (LIVE_B, 1)
    }

    def missing_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path in missing:
            raise FileNotFoundError(path)
        return stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", missing_stat)
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    notice = screen.denied_notices[str(folder)]
    lines = notice.reports_label.text().splitlines()
    expected = (
        [(LIVE_B, 1), (LIVE_B, 2), (LIVE_A, 1), (LIVE_A, 2)]
        if all_unknown
        else [(LIVE_B, 2), (LIVE_A, 1), (LIVE_A, 2), (LIVE_B, 1)]
    )
    assert all(
        f"{run_id} · turn {turn}" in line
        for line, (run_id, turn) in zip(lines, expected, strict=True)
    )
    assert lines[-1].startswith("time unknown · ")
    assert ("first time unknown · latest time unknown" in notice.text_label.text()) is (
        all_unknown
    )


@pytest.mark.parametrize("later_valid", [False, True])
def test_unrelated_offer_is_removed_before_coverage_and_merging(
    context: AppContext, qtbot: QtBot, env: dict[str, str], later_valid: bool
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    unrelated = folder.with_name("readable")
    unrelated.mkdir()
    alias = folder.with_name("unrelated-alias")
    alias.symlink_to(unrelated, target_is_directory=True)
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, folder, allow=str(alias))
    if later_valid:
        later = write_run(replace(run, turn=2))
        _write_denial(later, folder, allow=str(folder))
    with qtbot.waitSignal(context.settings_changed):
        context.save_settings({"sandbox.read": [str(unrelated)]})
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    notice = screen.denied_notices[str(folder)]
    assert [button.text() for button in notice.buttons] == [
        "Dismiss",
        "Don't show again",
    ]
    assert notice.offer_label.text() == (
        "No folder can be offered for automatic read permission."
    )
    assert notice.text_label.text().startswith(
        "2 reports" if later_valid else "1 report"
    )


@pytest.mark.parametrize("relationship", ["equal", "inside", "contains", "alias"])
@pytest.mark.parametrize("covered", [False, True])
def test_related_offers_remain_available_unless_covered(
    context: AppContext,
    qtbot: QtBot,
    env: dict[str, str],
    relationship: str,
    covered: bool,
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    (folder / "child").mkdir(parents=True)
    hidden, offered = folder, folder
    if relationship == "inside":
        offered = folder / "child"
    elif relationship == "contains":
        hidden = folder / "child"
    elif relationship == "alias":
        offered = folder.with_name("alias")
        offered.symlink_to(folder, target_is_directory=True)
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, hidden, allow=str(offered))
    with qtbot.waitSignal(context.settings_changed):
        context.save_settings({"sandbox.read": [str(offered)] if covered else []})
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    if covered:
        assert screen.denied_notices == {}
    else:
        notice = screen.denied_notices[str(hidden)]
        assert [button.text() for button in notice.buttons] == [
            "Allow reading",
            "Dismiss",
            "Don't show again",
        ]
        assert (
            notice.offer_label.text() == f"Allow reading {offered} for future workers."
        )


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_denial_notice_wraps_paths_and_reports_at_640_pixels(
    context: AppContext, qtbot: QtBot, env: dict[str, str], mode: str
) -> None:
    from .test_common import assert_screen_fits  # pyrefly: ignore[missing-import]

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    theme.apply(app, mode)
    folder = Path(env["HOME"]) / ("long-folder-" * 18) / ("<path>" * 35)
    folder.mkdir(parents=True)
    other = folder.with_name("other")
    other.mkdir()
    for run_id, denied in ((LIVE_A, folder), (LIVE_B, other)):
        run = write_run(make_run(env, run_id, claim="long-claim-" * 20))
        _write_denial(run, denied, allow=str(denied))
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    screen.resize(640, 900)
    qtbot.wait(50)
    assert screen.width() == 640
    assert_screen_fits(screen)
    notice = screen.denied_notices[str(folder)]
    other_notice = screen.denied_notices[str(other)]
    notice.runs_toggle.click()
    qtbot.wait(50)
    assert notice.reports_label.isVisible()
    assert not other_notice.reports_label.isVisible()
    assert_screen_fits(screen)
    for label in (notice.title_label, notice.reports_label, notice.offer_label):
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.wordWrap()
        assert label.heightForWidth(label.width()) > label.fontMetrics().height()
    action_top = notice.buttons[0].mapTo(notice, notice.buttons[0].rect().topLeft()).y()
    offer_bottom = notice.offer_label.mapTo(
        notice, notice.offer_label.rect().bottomLeft()
    ).y()
    assert action_top > offer_bottom


def test_orphaned_badge(env: dict[str, str], context: AppContext, qtbot: QtBot) -> None:
    parent = record.parent("gapcheck")
    assert parent is not None
    write_run(make_run(env, LIVE_A, parent={**parent, "start": parent["start"] + 1}))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 1)
    screen = screen_for(context, qtbot)
    row = screen.live_rows[LIVE_A]
    assert (row.badge.text(), row.badge.tone()) == ("Orphaned", "warning")


def test_flow_wait_badge(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    flow = make_flow(env, status="stopped")
    parent = record.parent("flow", FLOW)
    assert parent is not None
    write_run(make_run(env, LIVE_A, parent={**parent, "start": parent["start"] + 1}))
    flow["tasks"][0].update(status="stopped", writer_run=LIVE_A)
    flow_record.save(flow, env)
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 1)
    screen = screen_for(context, qtbot)
    row = screen.live_rows[LIVE_A]
    assert (row.badge.text(), row.badge.tone()) == (
        "Waiting for Claude",
        "warning",
    )
    qtbot.waitUntil(row.isVisible)
    chip_column = row.badge.parentWidget()
    assert chip_column is not None
    assert chip_column.width() == overview._chip_width()
    assert row.badge.sizeHint().width() <= row.badge.width() <= chip_column.width()
    waiting = f"Waiting for Claude's answer to flow {FLOW}"
    assert row.detail_label.text().startswith(waiting)
    screen.resize(1600, 900)
    qtbot.waitUntil(lambda: waiting in row.detail_label.display_text())
    assert row.detail_label.isVisible()
    flow["status"] = "failed"
    flow_record.save(flow, env)
    screen.refresh()
    assert (row.badge.text(), row.badge.tone()) == ("Waiting for Claude", "warning")
    assert waiting not in row.detail_label.text()
    assert row.detail_label.text().startswith(
        f"Waiting for flow {FLOW} to resume (last updated "
    )
    flow["status"] = "done"
    flow_record.save(flow, env)
    screen.refresh()
    assert (row.badge.text(), row.badge.tone()) == ("Orphaned", "warning")
    assert "Waiting" not in row.detail_label.text()


@pytest.mark.parametrize("parent", ["run", "flow", "panel"])
@pytest.mark.parametrize(
    "name, reason",
    [
        (".profile", "startup file"),
        (".profile", "hidden"),
        (".toolrc", "hidden"),
    ],
)
def test_home_file_denial_notice_uses_recorded_reason(
    context: AppContext,
    qtbot: QtBot,
    env: dict[str, str],
    parent: str,
    name: str,
    reason: str,
) -> None:
    file = Path(env["HOME"]) / name
    run = write_run(make_run(env, LIVE_A))
    _write_denial(run, file, reason=reason)
    if name == ".profile" and reason == "hidden":
        later = write_run(replace(run, turn=2, updated_at=run.updated_at + 1))
        _write_denial(later, file, reason="startup file")
    if parent == "flow":
        flow = make_flow(env)
        flow["tasks"][0]["writer_run"] = run.id
        flow_record.save(flow, env)
    elif parent == "panel":
        make_panel(env, members=[make_member(run=run.id)])
    with qtbot.waitSignal(context.settings_changed):
        context.reload_settings()
    with qtbot.waitSignal(context.runs.reloaded):
        context.runs.reload()
    screen = screen_for(context, qtbot)
    if reason == "startup file":
        assert screen.denied_notices == {}
    else:
        assert len(screen.denied_notices) == 1
        notice = next(iter(screen.denied_notices.values()))
        assert notice.title_label.text() == f"A worker could not read {file}"
        assert notice.offer_label.text() == (
            "No folder can be offered for automatic read permission."
        )
        assert [button.text() for button in notice.buttons] == [
            "Dismiss",
            "Don't show again",
        ]


@pytest.mark.parametrize("width", [576, 1400])
def test_screen_fits_width(context: AppContext, qtbot: QtBot, width: int) -> None:
    from PySide6.QtWidgets import QApplication

    from ember.app import theme

    from .test_common import assert_screen_fits  # pyrefly: ignore[missing-import]

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    theme.apply(app, "light")
    write_mix(dict(context.env))
    context.runs.reload()
    screen = screen_for(context, qtbot)
    screen.resize(width, 480)
    if not screen.finished_open:
        screen.toggle_finished_list()
    qtbot.wait(50)
    assert screen.width() == width
    assert_screen_fits(screen)
    for row in (*screen.live_rows.values(), *screen.finished_rows.values()):
        row.show_details(True)
    qtbot.wait(30)
    assert_screen_fits(screen)


@pytest.mark.parametrize(
    ("kind", "word"),
    [
        ("capacity", "At capacity"),
        ("usage_limit", "At usage limit"),
        ("outage", "Waiting for provider"),
    ],
)
def test_provider_wait_badge(
    env: dict[str, str], context: AppContext, qtbot: QtBot, kind: str, word: str
) -> None:
    run = write_run(make_run(env, LIVE_A))
    outcome = {
        **dict.fromkeys(
            ("state", "reason", "provider_error", "signed_out", "incomplete", "exit")
        ),
        "seconds": 1.0,
        "waiting": {"kind": kind, "message": "the provider's words", "since": 1.0},
    }
    record.outcome_path(run, 1).write_text(json.dumps(outcome))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 1)
    row = screen_for(context, qtbot).live_rows[LIVE_A]
    assert (row.badge.text(), row.badge.tone()) == (word, "warning")
    collected = replace(run, turn_state=record.COLLECTED)
    assert overview.run_badge(collected) == ("Running", "info")
    assert overview.run_badge(replace(run, status=record.FAILED)) == (
        "Failed",
        "danger",
    )
