import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QStyle,
    QWidget,
)
from pytestqt.qtbot import QtBot

from ember import choices
from ember.app import state, theme, widgets
from ember.app.context import AppContext
from ember.app.screens import common
from ember.app.screens import settings as settings_module
from ember.app.screens.settings import OTHER, PreparationCommandDialog, SettingsScreen


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


def test_preparation_add_edit_empty_and_remove(
    env: dict[str, str],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    folders = iter([str(alias), str(second)])
    commands = iter([("npm ci && npm run build", True), ("uv sync", True), ("", True)])
    starts: list[str] = []
    prompts: list[tuple[str, str]] = []

    def choose(_parent: QWidget, start: str) -> str:
        starts.append(start)
        return next(folders)

    def command(_parent: QWidget, repository: str, saved: str) -> tuple[str, bool]:
        prompts.append((repository, saved))
        return next(commands)

    monkeypatch.setattr(settings_module, "choose_repository", choose)
    monkeypatch.setattr(settings_module, "preparation_command", command)
    context, screen = _screen(env, qtbot)
    assert screen.preparation_rows == {}
    for _ in range(2):
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            screen.add_repository.click()
    assert starts == [env["HOME"], env["HOME"]]
    assert _stored(env, "worktree.prepare") == {
        str(first): "npm ci && npm run build",
        str(second): "uv sync",
    }
    edit, remove = screen.preparation_rows[str(first)]
    assert edit.text() == "Edit"
    assert remove.text() == "Remove"
    assert edit.accessibleName() == f"Edit preparation for {first}"
    assert remove.accessibleName() == f"Remove preparation for {first}"
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        edit.click()
    assert prompts == [
        (str(first), ""),
        (str(second), ""),
        (str(first), "npm ci && npm run build"),
    ]
    assert _stored(env, "worktree.prepare") == {str(first): "", str(second): "uv sync"}
    assert "No preparation" in [
        text.text() for text in screen.preparation_box.findChildren(QLabel)
    ]
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.preparation_rows[str(first)][1].click()
    assert _stored(env, "worktree.prepare") == {str(second): "uv sync"}
    assert list(screen.preparation_rows) == [str(second)]
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.preparation_rows[str(second)][1].click()
    assert _stored(env, "worktree.prepare") == {}
    assert screen.preparation_rows == {}


def test_preparation_cancel_and_unchanged_command_save_nothing(
    env: dict[str, str],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = str(tmp_path / "repository")
    choices.set_settings({"worktree.prepare": {repository: "uv sync"}}, env=env)
    folders = iter(["", str(tmp_path / "another")])
    commands = iter([("ignored", False), ("ignored", False), ("uv sync", True)])
    monkeypatch.setattr(
        settings_module, "choose_repository", lambda *args: next(folders)
    )
    monkeypatch.setattr(
        settings_module, "preparation_command", lambda *args: next(commands)
    )
    context, screen = _screen(env, qtbot)
    with qtbot.assertNotEmitted(context.settings_changed, wait=100):
        screen.add_repository.click()
        screen.add_repository.click()
        screen.preparation_rows[repository][0].click()
        screen.preparation_rows[repository][0].click()
    assert context.tasks.pending() == 0
    assert _stored(env, "worktree.prepare") == {repository: "uv sync"}


@pytest.mark.parametrize("mode", theme.MODES)
def test_preparation_dialog_layout(qapp: QApplication, qtbot: QtBot, mode: str) -> None:
    theme.apply(qapp, mode)
    repository = "/srv/" + "long-repository-name/" * 16 + "<checkout>"
    dialog = PreparationCommandDialog(repository, "npm ci")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    assert dialog.windowTitle() == dialog.title.text() == "Worktree preparation"
    title_width = dialog.fontMetrics().horizontalAdvance(dialog.windowTitle())
    title_controls = 4 * dialog.style().pixelMetric(
        QStyle.PixelMetric.PM_TitleBarHeight
    )
    assert dialog.minimumWidth() >= title_width + title_controls
    assert dialog.title.width() >= dialog.title.sizeHint().width()
    assert dialog.repository.text() == repository
    assert dialog.repository.textFormat() == Qt.TextFormat.PlainText
    assert dialog.repository.property("role") == "secondary"
    assert dialog.repository.wordWrap()
    assert dialog.repository.heightForWidth(dialog.repository.width()) > (
        dialog.repository.fontMetrics().height()
    )
    assert dialog.repository.height() >= dialog.repository.heightForWidth(
        dialog.repository.width()
    )
    assert dialog.command.text() == "npm ci"
    assert dialog.command.accessibleName() == "Command"
    assert dialog.command.placeholderText() == "npm ci"
    assert dialog.info.toolTip() == (
        "Runs in each new worktree outside the worker sandbox, before any "
        "worker or check starts. Leave empty for no preparation."
    )
    assert dialog.cancel_button.isDefault()
    assert not dialog.save_button.isDefault()
    assert dialog.focusWidget() is dialog.cancel_button
    assert dialog.cancel_button.property("kind") == "secondary"
    assert dialog.save_button.property("kind") == "primary"
    for child in dialog.findChildren(QWidget):
        if child.isVisibleTo(dialog):
            assert dialog.rect().contains(child.mapTo(dialog, child.rect().topLeft()))
            assert dialog.rect().contains(
                child.mapTo(dialog, child.rect().bottomRight())
            )


@pytest.mark.parametrize("mode", theme.MODES)
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("action", ["cancel", "escape", "enter"])
def test_real_preparation_dialog_cancels_without_saving(
    env: dict[str, str],
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    edit: bool,
    action: str,
) -> None:
    theme.apply(qapp, mode)
    repository = str(tmp_path / "repository")
    before = {repository: "uv sync"} if edit else {}
    choices.set_settings({"worktree.prepare": before}, env=env)
    context, screen = _screen(env, qtbot)
    monkeypatch.setattr(settings_module, "choose_repository", lambda *args: repository)
    opened: list[PreparationCommandDialog] = []

    def cancel() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, PreparationCommandDialog)
        opened.append(dialog)
        try:
            assert dialog.command.text() == ("uv sync" if edit else "")
            assert dialog.cancel_button.isDefault()
            assert dialog.focusWidget() is dialog.cancel_button
            dialog.command.setText("must not be saved")
            if action == "cancel":
                qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
            elif action == "escape":
                qtbot.keyClick(dialog.command, Qt.Key.Key_Escape)
            else:
                qtbot.keyClick(dialog.cancel_button, Qt.Key.Key_Return)
            assert not dialog.isVisible()
            assert dialog.result() == QDialog.DialogCode.Rejected
        finally:
            dialog.reject()

    QTimer.singleShot(0, cancel)
    with qtbot.assertNotEmitted(context.settings_changed, wait=100):
        if edit:
            screen.preparation_rows[repository][0].click()
        else:
            screen.add_repository.click()
    assert len(opened) == 1
    assert context.tasks.pending() == 0
    assert _stored(env, "worktree.prepare") == before


@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("command", ["", "npm ci && npm run build"])
@pytest.mark.parametrize("mode", theme.MODES)
def test_real_preparation_dialog_saves(
    env: dict[str, str],
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: bool,
    command: str,
    mode: str,
) -> None:
    theme.apply(qapp, mode)
    repository = str(tmp_path / "repository")
    before = {repository: "uv sync"} if edit else {}
    choices.set_settings({"worktree.prepare": before}, env=env)
    context, screen = _screen(env, qtbot)
    monkeypatch.setattr(settings_module, "choose_repository", lambda *args: repository)
    opened: list[PreparationCommandDialog] = []

    def save() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, PreparationCommandDialog)
        opened.append(dialog)
        try:
            assert dialog.command.text() == ("uv sync" if edit else "")
            dialog.command.setText(command)
            qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            assert dialog.result() == QDialog.DialogCode.Accepted
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, save)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        if edit:
            screen.preparation_rows[repository][0].click()
        else:
            screen.add_repository.click()
    assert len(opened) == 1
    assert _stored(env, "worktree.prepare") == {repository: command}
    assert screen.preparation_card.isEnabled()
    assert command or "No preparation" in [
        text.text() for text in screen.preparation_box.findChildren(QLabel)
    ]


def test_preparation_cancelled_folder_opens_no_dialog(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, screen = _screen(env, qtbot)
    monkeypatch.setattr(settings_module, "choose_repository", lambda *args: "")

    def unexpected(*_args: object) -> None:
        pytest.fail("a cancelled folder chooser must not open a command dialog")

    monkeypatch.setattr(settings_module, "PreparationCommandDialog", unexpected)
    screen.add_repository.click()
    assert context.tasks.pending() == 0
    assert _stored(env, "worktree.prepare") == {}


def test_preparation_add_empty_command(
    env: dict[str, str],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = str(tmp_path / "repository")
    monkeypatch.setattr(settings_module, "choose_repository", lambda *args: repository)
    monkeypatch.setattr(
        settings_module, "preparation_command", lambda *args: ("", True)
    )
    context, screen = _screen(env, qtbot)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.add_repository.click()
    assert _stored(env, "worktree.prepare") == {repository: ""}
    assert [
        text.text()
        for text in screen.preparation_box.findChildren(QLabel)
        if text.isVisibleTo(screen)
    ] == [
        repository,
        "No preparation",
    ]


def test_preparation_refuses_duplicate_resolved_folder(
    env: dict[str, str],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    choices.set_settings({"worktree.prepare": {str(repository): ""}}, env=env)
    monkeypatch.setattr(settings_module, "choose_repository", lambda *args: str(alias))

    def command(*_args: object) -> tuple[str, bool]:
        pytest.fail("a duplicate must not open the command dialog")

    monkeypatch.setattr(settings_module, "preparation_command", command)
    context, screen = _screen(env, qtbot)
    screen.add_repository.click()
    assert screen.preparation_line.text() == f"{repository} is already in the list."
    assert context.tasks.pending() == 0
    assert _stored(env, "worktree.prepare") == {str(repository): ""}


@pytest.mark.parametrize("fail", [False, True])
def test_preparation_disables_card_until_save_finishes(
    env: dict[str, str],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    repository = str(tmp_path / "repository")
    peer = str(tmp_path / "peer")
    before = {repository: "npm ci", peer: ""}
    choices.set_settings({"worktree.prepare": before}, env=env)
    context, screen = _screen(env, qtbot)
    write = choices.set_settings
    started = Event()
    release = Event()

    def save(
        values: dict[str, object],
        expect_generation: int | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        started.set()
        assert release.wait(5)
        if fail:
            raise choices.InvalidChoiceError("preparation write refused")
        return write(values, expect_generation=expect_generation, env=env)

    monkeypatch.setattr(choices, "set_settings", save)
    monkeypatch.setattr(
        settings_module, "preparation_command", lambda *args: ("", True)
    )
    signal = context.error if fail else context.settings_changed
    with qtbot.waitSignal(signal, timeout=5000):
        try:
            screen.preparation_rows[repository][0].click()
            qtbot.waitUntil(started.is_set)
            assert not screen.preparation_card.isEnabled()
            assert not screen.add_repository.isEnabled()
            for edit, remove in screen.preparation_rows.values():
                assert not edit.isEnabled()
                assert not remove.isEnabled()
            screen._redraw()
            assert not screen.preparation_rows[repository][0].isEnabled()
            assert _stored(env, "worktree.prepare") == before
        finally:
            release.set()
    assert screen.preparation_card.isEnabled()
    assert screen.add_repository.isEnabled()
    for edit, remove in screen.preparation_rows.values():
        assert edit.isEnabled()
        assert remove.isEnabled()
    if fail:
        assert _stored(env, "worktree.prepare") == before
        assert "preparation write refused" in screen.preparation_line.text()
        assert screen.preparation_line.isVisibleTo(screen)
        # A retry succeeds and clears the failure reason.
        monkeypatch.setattr(choices, "set_settings", write)
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            screen.preparation_rows[repository][0].click()
    assert _stored(env, "worktree.prepare") == {repository: "", peer: ""}
    assert not screen.preparation_line.isVisibleTo(screen)


def test_stale_preparation_save_redraws_saved_mapping(
    env: dict[str, str], qtbot: QtBot, tmp_path: Path
) -> None:
    repository = str(tmp_path / "repository")
    peer = str(tmp_path / "peer")
    choices.set_settings({"worktree.prepare": {repository: "npm ci"}}, env=env)
    context, screen = _screen(env, qtbot)
    choices.set_settings({"worktree.prepare": {peer: ""}}, env=env)
    with qtbot.waitSignal(context.stale, timeout=5000):
        screen.preparation_rows[repository][1].click()
    assert list(screen.preparation_rows) == [peer]
    assert _stored(env, "worktree.prepare") == {peer: ""}
    assert screen.preparation_card.isEnabled()
    assert screen.preparation_line.isVisibleTo(screen)


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
    repository = str(tmp_path / "repository")
    choices.set_settings(
        {"sandbox.read": [os.fspath(tmp_path)], "worktree.prepare": {repository: ""}},
        env=env,
    )
    hidden = [str(tmp_path / "hidden-a"), str(tmp_path / "hidden-b")]
    state.save(state.AppState(denied_hidden=hidden), env)
    context, screen = _screen(env, qtbot)
    for _ in range(2):
        screen.show()
        qtbot.waitUntil(
            lambda: all(
                widget.isVisible()
                for widget in (
                    *screen.path_rows.values(),
                    *screen.preparation_rows[repository],
                )
            )
        )
        chain = focus_chain(screen, qtbot, screen.autostart)
        expected = [
            screen.autostart,
            screen.theme.buttons()[0],
            screen.tip_rows["Hidden notices"].info,
            *(screen.hidden_rows[path] for path in hidden),
            screen.network,
            screen.terminal,
            screen.add_folder,
            screen.path_rows[os.fspath(tmp_path)],
            screen.tip_rows["Repositories"].info,
            screen.add_repository,
            *screen.preparation_rows[repository],
        ]
        # Tab reaches a segmented control's chosen option; arrows move within it.
        assert [w for w in chain if w in expected] == expected
        info_index = chain.index(screen.tip_rows["Repositories"].info)
        assert chain[info_index - 1] is screen.path_rows[os.fspath(tmp_path)]
        assert chain[info_index + 1] is screen.add_repository
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            context.reload_settings()


def test_settings_rows_are_names_with_info_icons(
    env: dict[str, str], qtbot: QtBot
) -> None:
    _context, screen = _screen(env, qtbot)
    names = ["Start at login", "Theme", "Hidden notices", "Worker network", "Terminal"]
    names.extend(["Sandbox read paths", "Repositories"])
    assert screen.tip_rows["Worker network"].info.toolTip() == (
        "Lets Codex workers and Claude's web tools reach the network. "
        "OpenCode keeps network for its providers."
    )
    assert list(screen.tip_rows) == names
    for row in screen.tip_rows.values():
        assert [label.text() for label in row.findChildren(QLabel)] == [
            row.heading.text()
        ]
        assert row.info.toolTip()
    assert len({row.slot.width() for row in screen.tip_rows.values()}) == 1
    widget = screen.tip_rows["Start at login"].info.nextInFocusChain()
    while widget is not None and not widget.focusPolicy() & Qt.FocusPolicy.TabFocus:
        widget = widget.nextInFocusChain()
    assert widget is screen.autostart


def test_show_again_clears_only_the_selected_folders_hiding_and_dismissal(
    env: dict[str, str], qtbot: QtBot
) -> None:
    first, other = "/home/owner/.tools", "/home/owner/.other"
    saved = state.AppState(
        width=1234,
        height=789,
        screen="settings",
        finished_open=True,
        denied_dismissals={first: ["run:1"], other: ["run:2"]},
        denied_hidden=[first, other],
        sidebar_hidden=True,
    )
    state.save(saved, env)
    _context, screen = _screen(env, qtbot)
    assert set(screen.hidden_rows) == {first, other}
    assert all(button.text() == "Show again" for button in screen.hidden_rows.values())
    screen.hidden_rows[first].click()
    assert state.load(env) == replace(
        saved, denied_hidden=[other], denied_dismissals={other: ["run:2"]}
    )
    assert list(screen.hidden_rows) == [other]
    screen.hidden_rows[other].click()
    assert screen.hidden_rows == {}
    assert state.load(env).denied_hidden == []
    assert state.load(env).denied_dismissals == {}
    assert [
        text.text()
        for text in screen.hidden_box.findChildren(QLabel)
        if text.isVisibleTo(screen)
    ] == ["No folders hidden."]


@pytest.mark.parametrize("mode", theme.MODES)
def test_add_repository_uses_the_add_folder_icon(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str
) -> None:
    _context, screen = _screen(env, qtbot)
    theme.apply(qapp, mode)
    repository_icon = screen.add_repository.icon()
    assert not repository_icon.isNull()
    assert repository_icon.pixmap(16, 16).toImage() == (
        screen.add_folder.icon().pixmap(16, 16).toImage()
    )


def test_add_folder_refuses_ember_private_paths(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ember import paths

    context, screen = _screen(env, qtbot)
    private = paths.state_dir(env) / "runs"
    monkeypatch.setattr(settings_module, "choose_folder", lambda *args: str(private))
    screen._add_folder()
    assert _stored(env, "sandbox.read") == []
    assert "Ember's configuration or state" in screen.paths_line.text()
    assert context.tasks.pending() == 0


@pytest.mark.parametrize("width", [576, 1400])
def test_screen_fits_width(
    env: dict[str, str], qtbot: QtBot, tmp_path: Path, width: int
) -> None:
    from .test_common import assert_screen_fits  # pyrefly: ignore[missing-import]

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    theme.apply(app, "light")
    repository = str(tmp_path / ("repository-" * 20))
    command = "npm ci && " + "npm run check " * 20
    choices.set_settings({"worktree.prepare": {repository: command}}, env=env)
    state.save(state.AppState(denied_hidden=[repository]), env)
    _context, screen = _screen(env, qtbot)
    screen.resize(width, 480)
    screen.show()
    qtbot.wait(50)
    assert screen.width() == width
    assert_screen_fits(screen)
    assert list(screen.hidden_rows) == [repository]


def test_rows_shrink_stack_and_return(env: dict[str, str], qtbot: QtBot) -> None:
    from .test_common import assert_rows_reflow  # pyrefly: ignore[missing-import]

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    theme.apply(app, "light")
    _context, screen = _screen(env, qtbot)
    assert_rows_reflow(screen, list(screen.tip_rows.values()), qtbot)


@pytest.mark.parametrize("mode", theme.MODES)
def test_preparation_buttons_follow_entry_layout(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str
) -> None:
    theme.apply(qapp, mode)
    commands = {
        "/srv/repository": "npm ci && npm ci --prefix db",
        "/srv/no-preparation": "",
        "/srv/" + "long-repository-" * 10: "npm ci && " + "npm run check " * 20,
    }
    choices.set_settings({"worktree.prepare": commands}, env=env)
    _context, screen = _screen(env, qtbot)
    screen.show()
    for width in (1400, 900, 576, 900, 1400):
        screen.resize(width, 900)
        stacked = width == 576
        qtbot.wait(30)
        qtbot.waitUntil(
            lambda stacked=stacked: screen.tip_rows["Repositories"].stacked == stacked
        )
        for redraw in (False, True):
            if redraw:
                screen.shown()
                qtbot.wait(30)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            for repository, command in commands.items():
                edit, remove = screen.preparation_rows[repository]
                row = edit.parentWidget()
                assert row is not None
                repository_text, command_text = row.findChildren(QLabel)
                assert repository_text.text() == repository
                assert command_text.text() == (command or "No preparation")
                assert edit.accessibleName() == f"Edit preparation for {repository}"
                assert remove.accessibleName() == f"Remove preparation for {repository}"
                assert edit.geometry().right() < remove.geometry().left()
                assert edit.geometry().center().y() == remove.geometry().center().y()
                if stacked:
                    assert edit.geometry().top() > command_text.geometry().bottom()
                    assert edit.geometry().left() == repository_text.geometry().left()
                else:
                    assert (
                        max(
                            repository_text.geometry().right(),
                            command_text.geometry().right(),
                        )
                        < edit.geometry().left()
                    )
                    text_center = (
                        repository_text.geometry().top()
                        + command_text.geometry().bottom()
                    ) / 2
                    assert abs(edit.geometry().center().y() - text_center) <= 1
                    assert remove.mapTo(screen, remove.rect().topRight()).x() == (
                        screen.add_repository.mapTo(
                            screen, screen.add_repository.rect().topRight()
                        ).x()
                    )


@pytest.mark.parametrize("mode", theme.MODES)
@pytest.mark.parametrize("changed", [False, True])
def test_list_actions_follow_control_edges_and_stacked_layout(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str, changed: bool
) -> None:
    theme.apply(qapp, mode)
    path, repository, hidden = "/srv/read", "/srv/repository", "/srv/hidden"
    choices.set_settings(
        {"sandbox.read": [path], "worktree.prepare": {repository: "npm ci"}},
        env=env,
    )
    state.save(state.AppState(denied_hidden=[hidden]), env)
    _context, screen = _screen(env, qtbot)
    for row in screen.tip_rows.values():
        row.set_changed(changed)

    def right(widget: QWidget) -> int:
        return widget.mapTo(screen, widget.rect().topRight()).x()

    screen.show()
    for width in (1400, 576, 1400):
        screen.resize(width, 900)
        stacked = width == 576
        qtbot.wait(30)
        qtbot.waitUntil(
            lambda stacked=stacked: all(
                row.stacked == stacked for row in screen.tip_rows.values()
            )
        )
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        edit, remove = screen.preparation_rows[repository]
        path_remove = screen.path_rows[path]
        pill = path_remove.parentWidget()
        assert isinstance(pill, widgets.PillRow)
        assert pill.mapTo(screen, QPoint()).x() == (
            screen.paths_box.mapTo(screen, QPoint()).x()
        )
        assert right(path_remove) == right(pill) - pill.line.contentsMargins().right()
        if stacked:
            assert right(pill) == right(screen.paths_box)
            assert right(screen.hidden_rows[hidden]) == right(screen.hidden_box)
            assert edit.mapTo(screen, QPoint()).x() == (
                screen.preparation_box.mapTo(screen, QPoint()).x()
            )
            assert edit.geometry().right() < remove.geometry().left()
            for layout in (
                screen.paths_layout,
                screen.preparation_layout,
                screen.hidden_layout,
            ):
                assert layout.contentsMargins().right() == 0
        else:
            assert {
                right(screen.add_folder),
                right(pill),
                right(remove),
                right(screen.hidden_rows[hidden]),
            } == {right(screen.add_folder)}

    reset = screen.tip_rows["Theme"].reset
    assert reset is not None
    reset_width = reset.sizeHint().width()
    font = reset.font()
    font.setPixelSize(reset.fontInfo().pixelSize() + 4)
    reset.setFont(font)
    assert reset.sizeHint().width() > reset_width
    common.align_controls(screen.tip_rows.values())
    qtbot.wait(30)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
    pill = screen.path_rows[path].parentWidget()
    assert isinstance(pill, widgets.PillRow)
    assert {
        right(screen.add_folder),
        right(pill),
        right(screen.preparation_rows[repository][1]),
        right(screen.hidden_rows[hidden]),
    } == {right(screen.add_folder)}
    assert not isinstance(screen.hidden_rows[hidden].parentWidget(), widgets.PillRow)
    assert not isinstance(
        screen.preparation_rows[repository][1].parentWidget(), widgets.PillRow
    )


@pytest.mark.parametrize("width", [576, 1400])
def test_empty_read_paths_keep_the_full_width(
    env: dict[str, str], qtbot: QtBot, width: int
) -> None:
    _context, screen = _screen(env, qtbot)
    screen.resize(width, 900)
    screen.show()
    qtbot.wait(30)
    assert screen.path_rows == {}
    assert screen.paths_box.findChildren(QPushButton) == []
    texts = screen.paths_box.findChildren(QLabel)
    assert [text.text() for text in texts] == ["No folders added."]
    assert screen.paths_layout.contentsMargins().right() == 0
    assert texts[0].width() == screen.paths_box.width()


@pytest.mark.parametrize(
    ("name", "key", "value"),
    [
        ("Start at login", "app.autostart", "off"),
        ("Worker network", "worker.network", "on"),
        ("Theme", "app.theme", "dark"),
        ("Terminal", "terminal", "custom-terminal --"),
    ],
)
def test_changed_rows_follow_snapshots_and_reset(
    stub_env: dict[str, str], qtbot: QtBot, name: str, key: str, value: str
) -> None:
    """Every Settings control marks external changes and clears its saved key."""
    from ember.app import widgets

    context, screen = _screen(stub_env, qtbot)
    screen.show()
    row = screen.tip_rows[name]
    reset = row.reset
    assert reset is not None
    assert reset.isHidden()
    default = context.settings()["defaults"][key]
    choices.set_settings({key: default}, env=stub_env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    assert reset.isHidden()
    choices.set_settings({key: value}, env=stub_env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    assert not reset.isHidden()
    if isinstance(row.control, widgets.Switch):
        assert row.control.property("changed") is None
    else:
        assert row.control.property("changed") is True
    if key == "terminal":
        screen._other_open = True
        assert screen.other.isVisible()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        reset.click()
    assert key not in choices.snapshot(env=stub_env)["settings"]
    assert context.setting(key) == default
    assert reset.isHidden()
    if isinstance(row.control, widgets.Picker | widgets.Segmented):
        assert row.control.property("changed") is False
    if key == "terminal":
        assert not screen.other.isVisible()
        assert not screen._other_open
    # A subsequent external clear also removes the marker without a click.
    choices.set_settings({key: value}, env=stub_env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    assert not reset.isHidden()
    choices.clear_settings([key], env=stub_env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    assert reset.isHidden()
    assert screen.tip_rows["Sandbox read paths"].reset is None


def test_reset_theme_applies_the_default(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication
) -> None:
    """Reset applies System after clearing the theme setting."""
    choices.set_settings({"app.theme": "dark"}, env=env)
    context, screen = _screen(env, qtbot)
    theme.apply(qapp, "dark")
    reset = screen.tip_rows["Theme"].reset
    assert reset is not None
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        reset.click()
    assert theme.current_mode() == theme.resolve_mode("system", qapp)


@pytest.mark.parametrize("mode", theme.MODES)
def test_changed_settings_fit_at_minimum_width(
    stub_env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str
) -> None:
    """All Settings Reset buttons fit at the minimum window width in both themes."""
    from .test_common import assert_screen_fits  # pyrefly: ignore[missing-import]

    theme.apply(qapp, mode)
    choices.set_settings(
        {
            "app.theme": "dark",
            "app.autostart": "off",
            "worker.network": "on",
            "terminal": "custom-terminal --",
        },
        env=stub_env,
    )
    _context, screen = _screen(stub_env, qtbot)
    screen.resize(640, 480)
    screen.show()
    qtbot.waitUntil(lambda: assert_screen_fits(screen))
