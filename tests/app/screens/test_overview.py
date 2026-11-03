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

from ember import paths, prune, sandbox
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
    assert screen.subtitle.text() == "No runs live"
    assert screen.live_empty.isVisible()
    assert screen.finished_empty.isVisible()
    assert screen.live_card.isVisible()
    assert screen.finished_card.isVisible()


def test_rows_and_badges(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    assert screen.subtitle.text() == "2 runs live"
    assert list(screen.live_rows) == [LIVE_A, LIVE_B]
    first, second = screen.live_rows.values()
    assert (first.badge.text(), first.badge.tone()) == ("Starting", "neutral")
    assert (second.badge.text(), second.badge.tone()) == (
        "Waiting for Claude",
        "warning",
    )
    assert first.title_label.text() == "Run · Codex · gpt-5 · high"
    assert second.title_label.text() == "Run · Claude · opus"
    assert first.detail_label.text().startswith(
        "project · turn 1 · 0 commits · started "
    )
    assert second.detail_label.text().startswith("beta · turn 3 · read-only · started ")
    assert first.time_label.text() == "Live"
    assert first.time_label.toolTip().startswith("Started at ")

    assert list(screen.finished_rows) == [DONE_B, DONE_A]
    failed, closed = screen.finished_rows.values()
    assert (failed.badge.text(), failed.badge.tone()) == ("Failed", "danger")
    assert (closed.badge.text(), closed.badge.tone()) == ("Done", "success")
    assert closed.title_label.text() == "Run · OpenCode · openrouter/x · high"
    assert closed.detail_label.text().startswith("project · 1 commit · ")
    assert closed.detail_label.text().endswith(" · 8 min")
    assert failed.detail_label.text().startswith("project · sign-in needed · ")
    assert not closed.time_label.text().endswith("min")
    assert closed.time_label.toolTip().startswith("Ended at ")
    assert not screen.state_view.empty.isVisible()


def test_running_badge(env: dict[str, str], context: AppContext, qtbot: QtBot) -> None:
    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(context.runs.live()) == 1)
    screen = screen_for(context, qtbot)
    row = screen.live_rows[LIVE_A]
    assert (row.badge.text(), row.badge.tone()) == ("Running", "info")
    assert screen.subtitle.text() == "1 run live"
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
    assert row.detail_label.text().startswith("project · 1 commit · ")
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
    assert row.detail_label.text().endswith(" · 2 min")
    assert row.question_label.text() == "A question"
    qtbot.mouseClick(row.expand_button, Qt.MouseButton.LeftButton)
    text = labels(row)
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
        message = panel.findChild(QMessageBox)
        assert message is not None
        assert message.icon() == QMessageBox.Icon.Warning
        assert message.textFormat() == Qt.TextFormat.PlainText
        assert message.text() == f"No viewer opened {summary}."
        message.accept()
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
        not button.isEnabled()
        for row in (flow, panel)
        for button in row.file_buttons.values()
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
        button.isEnabled()
        for row in (flow, panel)
        for button in row.file_buttons.values()
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
        not button.isEnabled()
        for row in (flow, panel)
        for button in row.file_buttons.values()
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
    assert row.time_label.text() == "Live"
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
    assert row.detail_label.text().endswith(" · 2 min")
    assert row.time_label.toolTip().startswith("Ended at ")
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
    assert flow_row.detail_label.toolTip() == flow_row.detail_label.text()
    assert flow_row.details is not None
    assert all(
        label.sizePolicy().hasHeightForWidth()
        for label in flow_row.details.findChildren(QLabel)
    )


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_cancelled_parent_keeps_live_workers_and_their_controls(
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
        assert screen.live_rows_layout.count() == 0
        item = screen.finished_rows_layout.itemAt(0)
        assert item is not None
        assert item.widget() is row
        assert row.badge.text() == "Incomplete" and row.badge.tone() == "incomplete"
        assert row.detail_label.text().startswith("2 live runs · ")
        assert "Owner stopped it" in row.detail_label.text()
        assert row.title_label.text().startswith("Flow · ")
        assert row.nested.count() == 2
        child = screen.live_rows[LIVE_A]
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
        assert row.detail_label.text().startswith("1 live run · ")
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
    open_finished(screen)
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
    assert screen.finished_empty.isVisible() and not screen.clear_button.isEnabled()
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
        combo.currentText() == "all"
        for combo in (fresh.type_filter, fresh.status_filter, fresh.engine_filter)
    )


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
    screen.engine_filter.setCurrentText("all")
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
        run = write_run(make_run(env, ident, status=record.CLOSED, ended_at=200))
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
    assert not screen.clear_button.isEnabled()


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
def test_subtitle_matches_tray_top_level_counts(
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
    assert screen.subtitle.text() == icon.lines()[0] == expected
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
            overview.run_ended(widget.run)
        )
    assert order == [early, late]
    assert screen.finished_rows[early].detail_label.text().endswith(" · 1 h 6 min")


def test_clear_is_a_red_outline_with_its_tooltip(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    open_finished(screen)
    assert screen.clear_button.property("kind") == "danger-outline"
    assert screen.clear_button.accessibleName() == "Clear"
    assert screen.clear_button.accessibleDescription() == "Remove shown records"
    assert not screen.clear_button.icon().isNull()
    assert screen.clear_button.toolTip() == "Remove shown records"
    assert not hasattr(screen, "clear_caption")


def test_rows_show_the_end_time_on_the_right(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    for row in screen.live_rows.values():
        assert row.time_label.text() == "Live"
    for row in screen.finished_rows.values():
        ended = overview.run_ended(row.run)
        assert ended is not None
        assert row.time_label.text() == overview.when(ended)


def _stamp(day: int, hour: int, minute: int, second: int = 0) -> float:
    return datetime.datetime(2026, 11, day, hour, minute, second).timestamp()


def test_span_text() -> None:
    now = _stamp(3, 12, 0)
    assert overview.span_text(_stamp(3, 8, 17), _stamp(3, 8, 33), now) == (
        "08:17 to 08:33 · 16 min"
    )
    assert overview.span_text(_stamp(2, 23, 50), _stamp(3, 0, 10), now) == (
        "yesterday 23:50 to 00:10 · 20 min"
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


def test_end_text_and_tooltip() -> None:
    now = _stamp(3, 12, 0)
    assert overview.end_text(None, now) == "Live"
    assert overview.end_text(_stamp(3, 8, 33), now) == "Today 08:33"
    assert overview.end_tooltip(_stamp(3, 8, 17), _stamp(3, 8, 33)) == (
        "Ended at 08:33 on 3 Nov"
    )
    assert overview.end_tooltip(_stamp(3, 8, 17), None) == "Started at 08:17 on 3 Nov"


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
