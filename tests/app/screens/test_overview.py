import shutil
from collections.abc import Iterator
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from pytestqt.qtbot import QtBot

from ember import paths, sandbox
from ember.app.context import AppContext
from ember.app.screens import overview
from ember.app.screens.overview import OverviewScreen
from ember.runs import record

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
    screen.resize(1000, 900)
    screen.show()
    screen.shown()
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
