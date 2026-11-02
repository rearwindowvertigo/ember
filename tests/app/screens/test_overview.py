import shutil
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QWidget
from pytestqt.qtbot import QtBot

from ember import paths, sandbox
from ember.app import flows
from ember.app.context import AppContext
from ember.app.screens import overview
from ember.app.screens.overview import OverviewScreen
from ember.flows import record as flow_record
from ember.panels import record as panel_record
from ember.runs import record

from ..test_flows import FLOW, PANEL, make_flow, make_member, make_panel  # pyrefly: ignore[missing-import]
from ..test_mirror import stub_tmux  # pyrefly: ignore[missing-import]
from ..test_runs import make_run, write_run  # pyrefly: ignore[missing-import]

LIVE_A = "codex-20260101-120000-aaaaaa"
LIVE_B = "claude-20260101-121000-bbbbbb"
DONE_A = "opencode-20260101-090000-cccccc"
DONE_B = "codex-20260101-100000-dddddd"


@pytest.fixture
def context(env: dict[str, str], qtbot: QtBot) -> Iterator[AppContext]:
    stub_tmux(env)
    result = AppContext(env)
    result.runs.start()
    qtbot.waitUntil(result.runs.loaded)
    yield result
    result.runs.stop()


def screen_for(context: AppContext, qtbot: QtBot) -> OverviewScreen:
    screen = OverviewScreen(context)
    qtbot.addWidget(screen)
    proc_locks = Path(context.env["HOME"]) / "proc-locks"
    proc_locks.write_text("")
    screen.records._proc_locks = proc_locks
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
    assert screen.state_view.empty.isVisible()
    assert screen.state_view.empty_title.text() == "No runs yet"
    assert "emb run" in screen.state_view.empty_line.text()
    assert not screen.live_card.isVisible()
    assert not screen.finished_card.isVisible()


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
    assert first.title_label.text() == "Codex · gpt-5 · high"
    assert second.title_label.text() == "Claude · opus"
    assert first.detail_label.text() == f"project · ember/codex/{LIVE_A} · turn 1"
    assert second.detail_label.text() == "beta · read-only · turn 3"
    assert first.time_label.text().endswith(("min", "h"))

    assert list(screen.finished_rows) == [DONE_B, DONE_A]
    failed, closed = screen.finished_rows.values()
    assert (failed.badge.text(), failed.badge.tone()) == ("Failed", "danger")
    assert (closed.badge.text(), closed.badge.tone()) == ("Closed", "success")
    assert closed.title_label.text() == "OpenCode · openrouter/x · high"
    assert closed.detail_label.text() == f"Fix the bug · ember/codex/{DONE_A}"
    assert closed.time_label.text().endswith("· 8 min")
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
    mirror = screen.mirror()
    assert mirror is not None
    assert mirror.accessibleName() == f"Screen of run {LIVE_A}"
    assert mirror.accessibleDescription() == (
        f"Screen of run {LIVE_A}, read-only. Updates when the worker writes."
    )
    assert first.expand_button.accessibleName() == f"Hide screen of run {LIVE_A}"
    qtbot.waitUntil(lambda: mirror.last_screen() is not None)

    qtbot.mouseClick(second.expand_button, Qt.MouseButton.LeftButton)
    assert screen.expanded_live == LIVE_B
    assert first.mirror is None
    assert second.mirror is not None
    assert first.expand_button.accessibleName() == f"Show screen of run {LIVE_A}"

    qtbot.mouseClick(second.expand_button, Qt.MouseButton.LeftButton)
    assert screen.expanded_live is None
    assert second.mirror is None


def test_mirror_closes_when_the_run_ends(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    screen.toggle_live(LIVE_A)
    assert screen.mirror() is not None
    run = record.load(LIVE_A, env)
    write_run(make_run(env, LIVE_A, status=record.CLOSED, ended_at=run.started_at + 60))
    context.runs.reload()
    qtbot.waitUntil(lambda: screen.expanded_live is None)
    assert LIVE_A not in screen.live_rows
    assert LIVE_A in screen.finished_rows


def test_mirror_closes_when_the_screen_is_left(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    screen.toggle_live(LIVE_A)
    screen.hide()
    assert screen.expanded_live is None
    assert screen.live_rows[LIVE_A].mirror is None


def test_mirror_closes_when_the_window_hides(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    screen = loaded(env, context, qtbot)
    context.set_visible(True)
    screen.toggle_live(LIVE_B)
    context.set_visible(False)
    assert screen.mirror() is None


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
    assert screen.mirror() is None
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
    assert screen.unreadable_alert.title_label.text() == (
        "1 run record could not be read."
    )
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
    assert len(screen.sandbox_alerts) == 3
    with qtbot.waitActive(screen):
        screen.activateWindow()
    controls: list[QPushButton] = []
    for alert in screen.sandbox_alerts.values():
        controls.extend(alert.buttons)
    for row in screen.live_rows.values():
        controls.extend((row.expand_button, row.open_button))
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
    assert screen.state_view.empty.isVisible()
    assert not screen.live_card.isVisible()


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
    assert row.detail_label.text().startswith("Fix it · ")
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
    assert overview.flow_title(flow) == "project · 1 task · 0/1 merged"
    assert overview.flow_detail(flow) == f"ember/flow/{FLOW} · wave 0"
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
    assert overview.panel_detail(item) == "project · " + "x" * 80
    assert overview.panel_detail(replace(item, question="")) == "project"
    assert overview.item_time(0, None, 180) == "3 min"
    assert overview.item_time(0, 120, 180).endswith(" · 2 min")
    flow["tasks"] = []
    assert overview.flow_title(flow) == "project · 0 tasks · 0/0 merged"
    panel["members"] = []
    assert overview.panel_title(panel) == "Panel · 0 members · 0/0 answered"


def test_flow_details_and_run_links(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    write_run(make_run(env, LIVE_A))
    write_run(make_run(env, DONE_A, status=record.CLOSED, ended_at=200))
    flow = make_flow(env, status="stopped", wave=2, reason="Needs an answer")
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
    assert screen.flows_card.isVisible()
    assert screen.panels_card.isHidden()
    row = screen.flow_rows[FLOW]
    assert row.badge.text() == "Waiting for Claude"
    assert row.title_label.text() == "project · 2 tasks · 1/2 merged"
    assert row.detail_label.text().endswith("wave 2 · Needs an answer")
    qtbot.mouseClick(row.expand_button, Qt.MouseButton.LeftButton)
    assert "Choose a format" in labels(row)
    assert "1 review · last fix · 2 must, 1 should, 1 lead" in labels(row)
    assert "task-0 · Merged · round 0 · Codex · test-model" in labels(row)
    assert len(row.links) == 2
    assert [link.accessibleName() for link in row.links] == [
        f"Show run {DONE_A}",
        f"Show run {LIVE_A}",
    ]
    qtbot.mouseClick(row.links[0], Qt.MouseButton.LeftButton)
    assert screen.expanded_finished == DONE_A
    qtbot.mouseClick(row.links[1], Qt.MouseButton.LeftButton)
    assert screen.expanded_live == LIVE_A
    mirror = screen.mirror()
    assert mirror is not None
    qtbot.waitUntil(lambda: mirror.last_screen() is not None)
    screen.show_run(LIVE_A)
    assert screen.mirror() is mirror
    assert set(
        button.text() for button in screen.flows_card.findChildren(QPushButton)
    ) == {
        "",
        "Show run",
    }


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
    assert row.detail_label.text() == "project · A question"
    qtbot.mouseClick(row.expand_button, Qt.MouseButton.LeftButton)
    text = labels(row)
    assert all(overview.member_line(member) in text for member in panel["members"])
    assert "No reply" in text
    assert "Changed: a.py" in text
    assert "Left out claude:opus: engine off" in text
    assert f"Summary: {panel_record.folder(PANEL, env) / 'summary.md'}" in text
    assert f"Run {LIVE_A}" in text
    assert row.links == []
    assert [
        button.text() for button in screen.panels_card.findChildren(QPushButton)
    ] == [""]


def test_record_rows_survive_other_changes_and_refresh_links(
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
    assert row.time_label.text() == "10 min"
    assert row.details is details
    write_run(make_run(env, LIVE_A))
    context.runs.reload()
    qtbot.waitUntil(lambda: len(row.links) == 1 and len(panel_row.links) == 1)
    assert row.details is not details
    shutil.rmtree(paths.runs_dir(env) / LIVE_A)
    context.runs.reload()
    qtbot.waitUntil(lambda: not row.links and not panel_row.links)
    flow["status"] = "done"
    flow["ended_at"] = 220
    flow_record.save(flow, env)
    qtbot.waitUntil(lambda: row.badge.text() == "Done")
    assert row.expanded
    assert row.time_label.text().endswith(" · 2 min")
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
    assert screen.flow_rows[FLOW].badge.text() == "Runner gone"
    assert screen.panel_rows[PANEL].badge.text() == "Process gone"
    assert screen.flows_unreadable.text() == "1 flow record could not be read."
    assert screen.panels_unreadable.text() == "1 panel record could not be read."
    assert not screen.state_view.empty.isVisible()


def test_records_hide_and_reactivate(
    env: dict[str, str], context: AppContext, qtbot: QtBot
) -> None:
    make_flow(env)
    screen = screen_for(context, qtbot)
    assert screen.records.active() and screen.records.watched()
    screen.hide()
    assert not screen.records.active() and not screen.records.watched()
    screen.show()
    context.set_visible(True)
    screen.shown()
    assert not screen.records._queued
    qtbot.waitUntil(lambda: bool(screen.records.watched()))
    context.set_visible(False)
    assert not screen.records.active() and not screen.records.watched()
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
    assert not screen.flows_card.isVisible()
    assert screen.records_alert.title_label.text() == (
        "Ember could not read the flow and panel records: flow read refused."
    )
    monkeypatch.setattr(flow_record, "list_flows", original)
    with qtbot.waitSignal(screen.records.changed, timeout=5000):
        screen.records_alert.buttons[0].click()
    assert screen.flows_card.isVisible()
    assert not screen.records_alert.isVisible()
    monkeypatch.setattr(record, "list_runs", refuse)
    with qtbot.waitSignal(context.runs.failed, timeout=5000):
        context.runs.reload()
    assert screen.state_view.error.isVisible()
    assert not screen.flows_card.isVisible()
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
    screen.shown()
    assert not screen.state_view.empty.isVisible()
    assert not screen.flows_card.isVisible()
    release.set()
    qtbot.waitUntil(screen.records.loaded)
    assert screen.state_view.empty.isVisible()
    assert screen.state_view.empty_line.text() == (
        "Runs, flows and panels appear here when Claude starts them with "
        "`emb run`, `emb flow` or `emb panel`."
    )
    assert not screen.panels_card.isVisible()


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
        *flow_row.links,
        panel_row.expand_button,
        *panel_row.links,
        screen.live_rows[LIVE_A].expand_button,
        screen.live_rows[LIVE_A].open_button,
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
    assert flow_row.detail_label.sizePolicy().hasHeightForWidth()
    assert flow_row.details is not None
    assert all(
        label.sizePolicy().hasHeightForWidth()
        for label in flow_row.details.findChildren(QLabel)
    )
    screen.show_run(LIVE_A)
    qtbot.waitUntil(lambda: area.verticalScrollBar().value() > 0)
