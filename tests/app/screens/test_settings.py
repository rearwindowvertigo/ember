import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from pytestqt.qtbot import QtBot

from ember import choices
from ember.app import theme
from ember.app.context import AppContext
from ember.app.screens import settings as settings_module
from ember.app.screens.settings import OTHER, SettingsScreen


@pytest.fixture
def stub_env(env: dict[str, str]) -> dict[str, str]:
    """The test environment with `PATH` limited to the stub folder."""
    return {**env, "PATH": env["PATH"].split(":")[0]}


def _stub(env: dict[str, str], name: str) -> None:
    path = Path(env["PATH"].split(":")[0]) / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def _screen(env: dict[str, str], qtbot: QtBot) -> tuple[AppContext, SettingsScreen]:
    context = AppContext(env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    screen = SettingsScreen(context)
    qtbot.addWidget(screen)
    return context, screen


def _stored(env: dict[str, str], name: str) -> Any:  # noqa: ANN401 - a setting
    return choices.snapshot(env=env)["answers"][name]["value"]


def _items(screen: SettingsScreen) -> list[tuple[str, Any]]:
    picker = screen.terminal
    return [
        (picker.itemText(row), picker.itemData(row, Qt.ItemDataRole.UserRole))
        for row in range(picker.count())
    ]


def _pick_terminal(screen: SettingsScreen, value: str) -> None:
    picker = screen.terminal
    row = next(
        r
        for r in range(picker.count())
        if picker.itemData(r, Qt.ItemDataRole.UserRole) == value
    )
    picker.setCurrentIndex(row)
    picker.activated.emit(row)


def test_switches_write_their_keys(env: dict[str, str], qtbot: QtBot) -> None:
    context, screen = _screen(env, qtbot)
    assert screen.autostart.isChecked()
    assert not screen.network.isChecked()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.autostart.click()
    assert _stored(env, "app.autostart") == "off"
    assert not screen.autostart.isChecked()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.network.click()
    assert _stored(env, "worker.network") == "on"
    assert screen.network.isChecked()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.autostart.click()
    assert _stored(env, "app.autostart") == "on"


@pytest.fixture
def restore_theme(qapp: QApplication) -> Iterator[None]:
    yield
    theme.apply(qapp, "light")
    qapp.setStyleSheet("")


@pytest.mark.usefixtures("restore_theme")
def test_theme_saves_and_applies_at_once(env: dict[str, str], qtbot: QtBot) -> None:
    context, screen = _screen(env, qtbot)
    assert screen.theme.value() == "system"
    with qtbot.waitSignal(theme.signals.changed, timeout=5000) as blocker:
        screen.theme.buttons()[2].click()
    assert blocker.args == ["dark"]
    assert _stored(env, "app.theme") == "dark"
    assert theme.current_mode() == "dark"
    with qtbot.waitSignal(theme.signals.changed, timeout=5000):
        screen.theme.buttons()[1].click()
    assert _stored(env, "app.theme") == "light"
    assert theme.current_mode() == "light"
    assert context.setting("app.theme") == "light"


def test_autostart_reason_shows_under_the_switch(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context, screen = _screen(env, qtbot)
    assert not screen.autostart_line.isVisibleTo(screen)
    context.set_autostart_reason(
        "emb is not on PATH; the autostart entry was not written"
    )
    assert screen.autostart_line.isVisibleTo(screen)
    assert screen.autostart_line.text() == (
        "emb is not on PATH; the autostart entry was not written"
    )
    context.set_autostart_reason(None)
    assert not screen.autostart_line.isVisibleTo(screen)


def test_reason_known_before_the_screen_is_shown(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = AppContext(env)
    context.set_autostart_reason("no emb")
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    screen = SettingsScreen(context)
    qtbot.addWidget(screen)
    assert screen.autostart_line.text() == "no emb"
    assert screen.autostart_line.isVisibleTo(screen)


def test_terminal_lists_found_terminals_then_other(
    stub_env: dict[str, str], qtbot: QtBot
) -> None:
    _stub(stub_env, "gnome-terminal")
    _stub(stub_env, "foot")
    _context, screen = _screen(stub_env, qtbot)
    assert _items(screen) == [
        ("System default (not installed)", "xdg-terminal-exec"),
        ("GNOME Terminal", "gnome-terminal --"),
        ("foot", "foot"),
        ("Other command", OTHER),
    ]
    assert screen.terminal.value() == "xdg-terminal-exec"
    assert not screen.other.isVisibleTo(screen)


def test_picking_a_terminal_saves_its_prefix(
    stub_env: dict[str, str], qtbot: QtBot
) -> None:
    _stub(stub_env, "xdg-terminal-exec")
    _stub(stub_env, "gnome-terminal")
    context, screen = _screen(stub_env, qtbot)
    assert _items(screen)[0] == ("System default", "xdg-terminal-exec")
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        _pick_terminal(screen, "gnome-terminal --")
    assert _stored(stub_env, "terminal") == "gnome-terminal --"
    assert screen.terminal.value() == "gnome-terminal --"


def test_other_command_saves_on_enter(stub_env: dict[str, str], qtbot: QtBot) -> None:
    context, screen = _screen(stub_env, qtbot)
    _pick_terminal(screen, OTHER)
    assert screen.other.isVisibleTo(screen)
    screen.other.setText("myterm --execute")
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.other.editingFinished.emit()
    assert _stored(stub_env, "terminal") == "myterm --execute"
    assert screen.terminal.value() == OTHER
    assert screen.other.isVisibleTo(screen)
    assert screen.other.text() == "myterm --execute"


def _focused(screen: SettingsScreen, qtbot: QtBot) -> None:
    screen.show()
    with qtbot.waitActive(screen):
        screen.activateWindow()
    screen.other.setFocus()
    qtbot.waitUntil(screen.other.hasFocus)


def test_other_command_refuses_empty_and_unparsable_text_on_enter(
    stub_env: dict[str, str], qtbot: QtBot
) -> None:
    _context, screen = _screen(stub_env, qtbot)
    _pick_terminal(screen, OTHER)
    _focused(screen, qtbot)
    screen.other.setText("  ")
    qtbot.keyClick(screen.other, Qt.Key.Key_Return)
    assert screen.terminal_line.isVisibleTo(screen)
    assert screen.terminal_line.text().startswith("Enter a command line")
    assert screen.terminal.value() == OTHER
    screen.other.setText("myterm 'open")
    qtbot.keyClick(screen.other, Qt.Key.Key_Return)
    assert screen.terminal_line.text().startswith("Not saved: terminal:")
    assert screen.terminal.value() == OTHER
    assert screen.other.isVisibleTo(screen)
    assert _stored(stub_env, "terminal") == "xdg-terminal-exec"


def test_leaving_other_command_without_valid_text_returns_to_the_saved_value(
    stub_env: dict[str, str], qtbot: QtBot
) -> None:
    _context, screen = _screen(stub_env, qtbot)
    _pick_terminal(screen, OTHER)
    _focused(screen, qtbot)
    screen.other.setText("myterm 'open")
    screen.add_folder.setFocus()
    qtbot.waitUntil(lambda: screen.terminal.value() == "xdg-terminal-exec")
    assert not screen.other.isVisibleTo(screen)
    assert screen.terminal_line.text().startswith("Not saved: terminal:")
    assert _stored(stub_env, "terminal") == "xdg-terminal-exec"


def test_leaving_other_command_keeps_a_saved_command(
    stub_env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings({"terminal": "myterm -x"}, env=stub_env)
    _context, screen = _screen(stub_env, qtbot)
    _focused(screen, qtbot)
    screen.other.setText("")
    screen.add_folder.setFocus()
    qtbot.waitUntil(lambda: screen.other.text() == "myterm -x")
    assert screen.terminal.value() == OTHER
    assert _stored(stub_env, "terminal") == "myterm -x"


def test_choosing_the_saved_theme_saves_nothing(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context, screen = _screen(env, qtbot)
    saved = context.setting("app.theme")
    with qtbot.assertNotEmitted(context.settings_changed, wait=200):
        screen.theme.changed.emit(saved)
    assert context.tasks.pending() == 0


def test_a_stored_unknown_command_shows_as_other(
    stub_env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings({"terminal": "myterm -x"}, env=stub_env)
    _context, screen = _screen(stub_env, qtbot)
    assert screen.terminal.value() == OTHER
    assert screen.terminal.currentText() == "Other command"
    assert screen.other.isVisibleTo(screen)
    assert screen.other.text() == "myterm -x"


def test_a_saved_known_terminal_that_is_missing_stays_listed(
    stub_env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings({"terminal": "konsole  -e"}, env=stub_env)
    _context, screen = _screen(stub_env, qtbot)
    assert ("Konsole (not installed)", "konsole -e") in _items(screen)
    assert screen.terminal.value() == "konsole -e"
    assert not screen.other.isVisibleTo(screen)


def test_read_paths_add_duplicate_and_remove(
    env: dict[str, str],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "notes"
    second = tmp_path / "docs"
    first.mkdir()
    second.mkdir()
    answers = iter([str(first), str(second), str(first), ""])
    starts: list[str] = []

    def choose(_parent: QWidget, start: str) -> str:
        starts.append(start)
        return next(answers)

    monkeypatch.setattr(settings_module, "choose_folder", choose)
    context, screen = _screen(env, qtbot)
    assert screen.path_rows == {}
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.add_folder.click()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.add_folder.click()
    assert _stored(env, "sandbox.read") == [str(first), str(second)]
    assert list(screen.path_rows) == [str(first), str(second)]
    assert starts[0] == env["HOME"]

    screen.add_folder.click()
    assert screen.paths_line.isVisibleTo(screen)
    assert screen.paths_line.text() == f"{first} is already in the list."
    screen.add_folder.click()
    assert _stored(env, "sandbox.read") == [str(first), str(second)]

    remove = screen.path_rows[str(first)]
    assert remove.accessibleName() == f"Remove {first}"
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        remove.click()
    assert _stored(env, "sandbox.read") == [str(second)]
    assert list(screen.path_rows) == [str(second)]
    assert not screen.paths_line.isVisibleTo(screen)


def test_stale_save_reloads_the_screen(env: dict[str, str], qtbot: QtBot) -> None:
    context, screen = _screen(env, qtbot)
    choices.set_settings({"worker.network": "on"}, env=env)
    with qtbot.waitSignal(context.stale, timeout=5000):
        screen.autostart.click()
    assert _stored(env, "app.autostart") == "on"
    assert screen.autostart.isChecked()
    assert screen.network.isChecked()


def test_a_refused_save_puts_the_control_back(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, screen = _screen(env, qtbot)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise choices.InvalidChoiceError("refused")

    monkeypatch.setattr(choices, "set_settings", refuse)
    with qtbot.waitSignal(context.error, timeout=5000):
        screen.network.click()
    assert not screen.network.isChecked()


def focus_chain(screen: QWidget, qtbot: QtBot, first: QWidget) -> list[QWidget]:
    screen.show()
    qtbot.waitExposed(screen)
    handle = screen.windowHandle()
    assert handle is not None
    handle.requestActivate()
    qtbot.waitUntil(screen.isActiveWindow, timeout=5000)
    first.setFocus(Qt.FocusReason.TabFocusReason)
    seen: list[QWidget] = []
    for _ in range(40):
        widget = QApplication.focusWidget()
        if widget is None or widget in seen:
            break
        seen.append(widget)
        screen.focusNextChild()
    return seen


def test_every_control_is_reached_with_tab(
    env: dict[str, str], qtbot: QtBot, tmp_path: Path
) -> None:
    choices.set_settings({"sandbox.read": [os.fspath(tmp_path)]}, env=env)
    _context, screen = _screen(env, qtbot)
    chain = focus_chain(screen, qtbot, screen.autostart)
    expected = [
        screen.autostart,
        screen.theme.buttons()[0],
        screen.network,
        screen.terminal,
        screen.add_folder,
        screen.path_rows[os.fspath(tmp_path)],
    ]
    # Tab reaches a segmented control's chosen option; arrows move within it.
    assert [w for w in chain if w in expected] == expected


def test_settings_rows_are_names_with_info_icons(
    env: dict[str, str], qtbot: QtBot
) -> None:
    _context, screen = _screen(env, qtbot)
    names = ["Start at login", "Theme", "Worker network", "Terminal"]
    names.append("Sandbox read paths")
    assert list(screen.tip_rows) == names
    for row in screen.tip_rows.values():
        assert [label.text() for label in row.findChildren(QLabel)] == [
            row.heading.text()
        ]
        assert row.info.toolTip()
    assert len({row.slot.width() for row in screen.tip_rows.values()}) == 1
    widget = screen.tip_rows["Start at login"].info.nextInFocusChain()
    while not widget.focusPolicy() & Qt.FocusPolicy.TabFocus:
        widget = widget.nextInFocusChain()
    assert widget is screen.autostart
