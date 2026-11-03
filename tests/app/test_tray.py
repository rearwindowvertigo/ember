import time
from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QSystemTrayIcon
from pytestqt.qtbot import QtBot

from ember.app import tray as tray_module
from ember.app.context import AppContext
from ember.app.tray import Tray
from ember.runs import record

from .test_runs import make_run, write_run  # pyrefly: ignore[missing-import]


def _tray(env: dict[str, str], qtbot: QtBot) -> tuple[Tray, AppContext, list[str]]:
    context = AppContext(env)
    calls: list[str] = []
    tray = Tray(context, lambda: calls.append("open"), lambda: calls.append("quit"))
    context.runs.start()
    qtbot.waitUntil(context.runs.loaded)
    tray.menu.aboutToShow.emit()
    return tray, context, calls


def _hhmm(stamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(stamp))


def test_status_lines() -> None:
    assert tray_module.status_line(0) == "No runs live"
    assert tray_module.status_line(1) == "1 run live"
    assert tray_module.status_line(2) == "2 runs live"
    assert tray_module.tooltip(2) == "Ember · 2 runs live"
    assert tray_module.tooltip(0) == "Ember · No runs live"


def test_menu_with_no_runs_live(env: dict[str, str], qtbot: QtBot) -> None:
    write_run(make_run(env, status=record.CLOSED, ended_at=1_800_000_100.0))
    tray, context, _calls = _tray(env, qtbot)
    assert tray.lines() == ["No runs live", "-", "Open Ember", "Quit Ember"]
    assert not tray.menu.actions()[0].isEnabled()
    assert tray.icon.toolTip() == "Ember · No runs live"
    assert not tray.icon.icon().isNull()
    context.runs.stop()


def test_menu_with_one_and_two_runs_live(env: dict[str, str], qtbot: QtBot) -> None:
    first = write_run(make_run(env, repo="/home/owner/alpha"))
    tray, context, _calls = _tray(env, qtbot)
    assert tray.lines() == [
        "1 run live",
        f"Codex · alpha · {_hhmm(first.started_at)}",
        "-",
        "Open Ember",
        "Quit Ember",
    ]
    second = make_run(
        env,
        "claude-20260101-130000-bbbbbb",
        engine="claude",
        repo="/home/owner/beta",
        status=record.WAITING,
        started_at=first.started_at + 3600,
    )
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        write_run(second)
    tray.menu.aboutToHide.emit()
    tray.menu.aboutToShow.emit()
    assert tray.lines() == [
        "2 runs live",
        f"Codex · alpha · {_hhmm(first.started_at)}",
        f"Claude · beta · {_hhmm(second.started_at)}",
        "-",
        "Open Ember",
        "Quit Ember",
    ]
    assert tray.icon.toolTip() == "Ember · 2 runs live"
    context.runs.stop()


def test_actions(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = write_run(make_run(env))
    tray, context, calls = _tray(env, qtbot)
    opened: list[str] = []
    monkeypatch.setattr(context, "open_run_terminal", opened.append)
    actions = tray.menu.actions()
    actions[1].trigger()
    assert opened == [run.id]
    actions[3].trigger()
    actions[4].trigger()
    assert calls == ["open", "quit"]
    tray._activated(QSystemTrayIcon.ActivationReason.Trigger)
    tray._activated(QSystemTrayIcon.ActivationReason.Context)
    assert calls == ["open", "quit", "open"]
    context.runs.stop()


def test_menu_is_not_rebuilt_while_open(env: dict[str, str], qtbot: QtBot) -> None:
    first = write_run(make_run(env, repo="/home/owner/alpha"))
    tray, context, _calls = _tray(env, qtbot)
    tray.menu.aboutToShow.emit()
    shown = tray.menu.actions()
    second = make_run(
        env,
        "claude-20260101-130000-bbbbbb",
        engine="claude",
        repo="/home/owner/beta",
        started_at=first.started_at + 3600,
    )
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        write_run(second)
    qtbot.waitUntil(lambda: tray.icon.toolTip() == "Ember · 2 runs live")
    assert tray.menu.actions() == shown
    assert tray.lines()[0] == "1 run live"
    tray.menu.aboutToHide.emit()
    QCoreApplication.processEvents()
    assert tray.menu.actions() == shown
    tray.menu.aboutToShow.emit()
    assert tray.lines()[0] == "2 runs live"
    assert len(tray.lines()) == 6
    context.runs.stop()


@pytest.mark.parametrize("change_after_hide", [False, True])
def test_stale_menu_actions_run_after_hide(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    change_after_hide: bool,
) -> None:
    run = write_run(make_run(env))
    tray, context, calls = _tray(env, qtbot)
    opened: list[str] = []
    monkeypatch.setattr(context, "open_run_terminal", opened.append)
    shown = tray.menu.actions()
    live_icon = tray.icon.icon().cacheKey()
    if change_after_hide:
        tray.menu.aboutToHide.emit()
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        write_run(replace(run, status=record.CLOSED, ended_at=run.started_at + 1))
    assert tray.icon.toolTip() == "Ember · No runs live"
    assert tray.icon.icon().cacheKey() != live_icon
    if not change_after_hide:
        tray.menu.aboutToHide.emit()
    QCoreApplication.processEvents()
    assert tray.menu.actions() == shown
    shown[1].trigger()
    shown[3].trigger()
    shown[4].trigger()
    assert opened == [run.id]
    assert calls == ["open", "quit"]
    tray.menu.aboutToShow.emit()
    assert tray.lines() == ["No runs live", "-", "Open Ember", "Quit Ember"]
    unchanged = tray.menu.actions()
    tray.menu.aboutToHide.emit()
    tray.menu.aboutToShow.emit()
    assert tray.menu.actions() == unchanged
    context.runs.stop()
