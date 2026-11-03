import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from PySide6 import QtGui
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import (
    QClipboard,
    QContextMenuEvent,
    QFocusEvent,
    QGuiApplication,
    QHelpEvent,
    QIcon,
    QPaintEvent,
    QTextLayout,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from pytestqt.qtbot import QtBot

from ember.app import theme, widgets


@pytest.mark.parametrize(
    ("shown", "start", "length", "expected"),
    [
        ("abcdef…", 1, 3, [(1, 3)]),
        ("abcdef…", 2, 4, [(2, 4)]),
        ("abcdef…", 2, 5, [(2, 5)]),
        ("abcdef…", 2, 12, [(2, 5)]),
        ("abcdef…", 6, 2, [(6, 1)]),
        ("abcdef…", 10, 3, [(6, 1)]),
        ("abcdef…", 0, 16, [(0, 7)]),
        ("abcdef…", 2, 0, []),
        ("…", 10, 3, [(0, 1)]),
        ("", 2, 3, []),
    ],
)
def test_elided_label_maps_selection_to_shown_text(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    shown: str,
    start: int,
    length: int,
    expected: list[tuple[int, int]],
) -> None:
    """Map full-text selections onto the displayed prefix and ellipsis."""
    label = widgets.ElidedLabel("abcdefghijklmnop")
    qtbot.addWidget(label)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSelection(start, length)
    label.resize(220, 36)
    monkeypatch.setattr(label, "display_text", lambda: shown)
    ranges: list[tuple[int, int]] = []

    class RecordingLayout(QTextLayout):
        def setFormats(self, formats: Sequence[QTextLayout.FormatRange]) -> None:  # noqa: N802
            """Record the selection ranges passed to the text layout."""
            ranges.extend(
                (cast(int, item.start), cast(int, item.length)) for item in formats
            )
            super().setFormats(formats)

    monkeypatch.setattr(widgets, "QTextLayout", RecordingLayout)
    assert not label.grab().isNull()
    assert ranges == expected
    assert label.selectedText() == label.text()[start : start + length]


def test_elided_label_delegates_painting_without_elision(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use QLabel's paint method when the full text fits."""
    label = widgets.ElidedLabel("short")
    qtbot.addWidget(label)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSelection(1, 3)
    label.resize(label.fontMetrics().horizontalAdvance(label.text()) + 20, 36)
    calls: list[QLabel] = []
    paint = QLabel.paintEvent

    def record_paint(widget: QLabel, event: QPaintEvent) -> None:
        """Record calls to QLabel's paint method."""
        calls.append(widget)
        paint(widget, event)

    monkeypatch.setattr(QLabel, "paintEvent", record_paint)
    assert label.display_text() == label.text()
    assert not label.grab().isNull()
    assert calls == [label]
    assert label.selectedText() == "hor"


@pytest.mark.parametrize("mode", theme.MODES)
def test_elided_label_keeps_full_text_for_copy(
    qapp: QApplication, qtbot: QtBot, mode: str
) -> None:
    previous = theme.current_mode()
    sheet = qapp.styleSheet()
    try:
        theme.apply(qapp, mode)
        text = "provider/" + "long-model-name-" * 20
        name = widgets.ElidedLabel(text)
        qtbot.addWidget(name)
        name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        name.resize(220, 36)
        name.show()
        assert name.text() == name.toolTip() == text
        assert name.display_text().endswith("…")
        name.setFocus()
        qtbot.keyClick(name, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert name.selectedText() == text
        qtbot.keyClick(name, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert QGuiApplication.clipboard().text() == text
        assert not name.grab().isNull()
        name.resize(name.fontMetrics().horizontalAdvance(text) + 20, 36)
        assert name.display_text() == text
        name.setText("short")
        assert name.display_text() == name.text() == name.toolTip() == "short"
        name.setText("")
        assert not name.grab().isNull()
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet(sheet)


def test_switch_toggles_by_click_and_space(qtbot: QtBot) -> None:
    switch = widgets.Switch()
    switch.setAccessibleName("Worker network")
    qtbot.addWidget(switch)
    switch.show()
    assert switch.isCheckable()
    assert not switch.isChecked()
    with qtbot.waitSignal(switch.toggled) as blocker:
        qtbot.mouseClick(switch, Qt.MouseButton.LeftButton)
    assert blocker.args == [True]
    assert switch.isChecked()
    switch.setFocus()
    qtbot.keyClick(switch, Qt.Key.Key_Space)
    assert not switch.isChecked()
    assert switch.sizeHint().width() >= widgets.Switch.WIDTH
    assert switch.focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_switch_paints_in_both_modes(qapp: QApplication, qtbot: QtBot) -> None:
    switch = widgets.Switch()
    qtbot.addWidget(switch)
    try:
        for mode in theme.MODES:
            theme.apply(qapp, mode)
            switch.setChecked(not switch.isChecked())
            assert not switch.grab().isNull()
    finally:
        theme.apply(qapp, "light")
        qapp.setStyleSheet("")


def test_segmented_arrow_keys_move_the_choice(qtbot: QtBot) -> None:
    control = widgets.Segmented(
        [("system", "System"), ("light", "Light"), ("dark", "Dark")]
    )
    qtbot.addWidget(control)
    control.show()
    assert control.value() == "system"
    first = control.buttons()[0]
    first.setFocus()
    with qtbot.waitSignal(control.changed) as blocker:
        qtbot.keyClick(first, Qt.Key.Key_Right)
    assert blocker.args == ["light"]
    assert control.value() == "light"
    qtbot.keyClick(control.buttons()[1], Qt.Key.Key_Down)
    assert control.value() == "dark"
    with qtbot.assertNotEmitted(control.changed):
        qtbot.keyClick(control.buttons()[2], Qt.Key.Key_Right)
    assert control.value() == "dark"
    qtbot.keyClick(control.buttons()[2], Qt.Key.Key_Left)
    assert control.value() == "light"


def test_segmented_click_and_set_value(qtbot: QtBot) -> None:
    control = widgets.Segmented([("cli", "CLI"), ("subagent", "Subagent")])
    qtbot.addWidget(control)
    with qtbot.assertNotEmitted(control.changed):
        control.set_value("subagent")
    assert control.value() == "subagent"
    with qtbot.waitSignal(control.changed) as blocker:
        qtbot.mouseClick(control.buttons()[0], Qt.MouseButton.LeftButton)
    assert blocker.args == ["cli"]
    with pytest.raises(ValueError):
        control.set_value("other")


@pytest.mark.parametrize(
    "options",
    [
        [("a", "A")],
        [(str(i), str(i)) for i in range(6)],
        [("a", "A"), ("a", "B")],
    ],
)
def test_segmented_refuses_bad_options(
    qtbot: QtBot, options: list[tuple[str, str]]
) -> None:
    with pytest.raises(ValueError):
        widgets.Segmented(options)


def test_picker_headers_are_not_selectable(qtbot: QtBot) -> None:
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    picker.add_option("Ask each time", "ask")
    picker.add_header("Codex")
    picker.add_option("gpt-5 · high", "codex:gpt-5@high", hint="Shortlisted")
    assert picker.value() == "ask"
    assert picker.is_header(1)
    header = picker.model().index(1, 0)
    flags = picker.model().flags(header)
    assert not flags & Qt.ItemFlag.ItemIsSelectable
    assert not flags & Qt.ItemFlag.ItemIsEnabled
    assert picker.itemData(2, widgets.HINT_ROLE) == "Shortlisted"
    assert picker.set_value("codex:gpt-5@high")
    assert picker.value() == "codex:gpt-5@high"
    assert not picker.set_value("Codex")
    assert picker.value() == "codex:gpt-5@high"


def test_picker_first_option_after_header_is_chosen(qtbot: QtBot) -> None:
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    assert picker.value() is None
    picker.add_header("Codex")
    picker.add_option("gpt-5", "gpt-5")
    assert picker.currentIndex() == 1
    assert picker.value() == "gpt-5"


def test_picker_dropdown_paints(qtbot: QtBot) -> None:
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    picker.add_header("Codex")
    picker.add_option("gpt-5", "gpt-5", hint="a hint")
    picker.show()
    picker.showPopup()
    view = picker.view()
    assert not view.grab().isNull()
    hinted = view.sizeHintForRow(1)
    assert hinted > view.sizeHintForRow(0)
    picker.hidePopup()


def test_badge_tones(qtbot: QtBot) -> None:
    badge = widgets.Badge("Current", "success")
    qtbot.addWidget(badge)
    assert badge.text() == "Current"
    assert badge.tone() == "success"
    badge.set_tone("danger", "Check failed")
    assert (badge.tone(), badge.text()) == ("danger", "Check failed")
    with pytest.raises(ValueError):
        widgets.Badge("x", "purple")


def test_card_and_alert(qtbot: QtBot) -> None:
    card = widgets.Card()
    qtbot.addWidget(card)
    assert card.property("role") == "card"
    clicked: list[bool] = []
    alert = widgets.Alert(
        "warning",
        "Codex runs without a sandbox.",
        "Turn its sandbox on in Engines.",
        actions=[("Open Engines", lambda: clicked.append(True))],
    )
    qtbot.addWidget(alert)
    assert alert.tone() == "warning"
    assert alert.accessibleName() == "Codex runs without a sandbox."
    assert len(alert.buttons) == 1
    qtbot.mouseClick(alert.buttons[0], Qt.MouseButton.LeftButton)
    assert clicked == [True]


def test_alert_without_text_hides_the_line(qtbot: QtBot) -> None:
    alert = widgets.Alert("info", "Settings changed elsewhere.")
    qtbot.addWidget(alert)
    alert.show()
    assert not alert.text_label.isVisible()


def test_spinner_turns_only_while_visible(qtbot: QtBot) -> None:
    spinner = widgets.Spinner()
    qtbot.addWidget(spinner)
    assert not spinner.is_turning()
    spinner.show()
    assert spinner.is_turning()
    spinner.hide()
    assert not spinner.is_turning()


def test_state_view_loading_appears_after_300_ms(qtbot: QtBot) -> None:
    view = widgets.StateView()
    qtbot.addWidget(view)
    view.show()
    view.show_loading("Reading engine status")
    assert not view.loading.isVisible()
    assert not view.spinner.isVisible()
    qtbot.wait(150)
    assert not view.spinner.isVisible()
    qtbot.waitUntil(view.spinner.isVisible, timeout=1000)
    assert view.loading_text.text() == "Reading engine status"
    assert view.spinner.accessibleName() == "Reading engine status"
    assert view.spinner.is_turning()


def test_state_view_clear_cancels_loading(qtbot: QtBot) -> None:
    view = widgets.StateView()
    qtbot.addWidget(view)
    view.show()
    view.show_loading("Reading")
    view.clear()
    qtbot.wait(400)
    assert not view.loading.isVisible()


def test_state_view_empty(qtbot: QtBot) -> None:
    view = widgets.StateView()
    qtbot.addWidget(view)
    view.show()
    view.show_empty("inbox", "No runs yet", "Runs appear here.")
    assert view.empty.isVisible()
    assert view.empty_title.text() == "No runs yet"
    assert view.empty_icon.pixmap() is not None
    assert not view.empty_icon.pixmap().isNull()


def test_state_view_error_with_and_without_retry(qtbot: QtBot) -> None:
    view = widgets.StateView()
    qtbot.addWidget(view)
    view.show()
    calls: list[int] = []
    view.show_error(
        "The settings database is busy. Try again.", lambda: calls.append(1)
    )
    assert view.retry_button.isVisible()
    qtbot.mouseClick(view.retry_button, Qt.MouseButton.LeftButton)
    assert calls == [1]
    view.show_error("Reinstall Ember.", None)
    assert view.error.isVisible()
    assert not view.retry_button.isVisible()


def test_command_line_copy_and_run(qtbot: QtBot) -> None:
    line = widgets.CommandLine("sudo apt install tmux")
    qtbot.addWidget(line)
    line.show()
    qtbot.mouseClick(line.copy_button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == "sudo apt install tmux"
    assert line.copy_button.text().strip() == "Copied"
    line.set_command("sudo apt install tmux")
    assert line.copy_button.text().strip() == "Copied"
    line.set_command("sudo apt install git")
    assert line.copy_button.text().strip() == "Copy"
    with qtbot.waitSignal(line.run_requested) as blocker:
        qtbot.mouseClick(line.run_button, Qt.MouseButton.LeftButton)
    assert blocker.args == ["sudo apt install git"]


def _dialog(qtbot: QtBot, command: str = "codex login") -> widgets.RunCommandDialog:
    dialog = widgets.RunCommandDialog(command)
    qtbot.addWidget(dialog)
    with qtbot.waitExposed(dialog):
        dialog.show()
    return dialog


def test_run_command_dialog_text(qtbot: QtBot) -> None:
    dialog = _dialog(qtbot, "claude auth login")
    assert dialog.windowTitle() == "Run this command?"
    assert dialog.command.text() == "claude auth login"
    assert dialog.cancel_button.isDefault()
    assert not dialog.run_button.isDefault()
    assert dialog.focusWidget() is dialog.cancel_button
    dialog.reject()


def test_run_command_dialog_escape_rejects(qtbot: QtBot) -> None:
    dialog = _dialog(qtbot)
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_run_command_dialog_enter_rejects(qtbot: QtBot) -> None:
    dialog = _dialog(qtbot)
    qtbot.keyClick(dialog.cancel_button, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_run_command_dialog_enter_on_focused_run_accepts(qtbot: QtBot) -> None:
    dialog = _dialog(qtbot)
    dialog.show()
    dialog.run_button.setFocus()
    qtbot.keyClick(dialog.run_button, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_run_command_dialog_cancel_rejects(qtbot: QtBot) -> None:
    dialog = _dialog(qtbot)
    qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_run_command_dialog_only_run_accepts(qtbot: QtBot) -> None:
    dialog = _dialog(qtbot)
    qtbot.mouseClick(dialog.run_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_run_command_dialog_copies_original(qtbot: QtBot) -> None:
    command = "/opt/tools/bin/tool --path /home/owner/project"
    dialog = _dialog(qtbot, command)
    label = dialog.command
    assert label.text() == command.replace("/", "/\u200b")
    assert label.accessibleName() == label.accessibleDescription() == command
    label.setFocus()
    label.setSelection(0, 6)
    qtbot.keyClick(label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == command
    qtbot.mouseClick(dialog.copy_button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == command
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.isVisible()

    def copy_selection() -> None:
        menu = label.findChild(QMenu)
        assert menu is not None
        menu.actions()[0].trigger()
        menu.close()

    QTimer.singleShot(0, copy_selection)
    label.setSelection(0, 6)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(), QPoint())
    label.contextMenuEvent(event)
    assert QGuiApplication.clipboard().text() == "/opt/"


@pytest.mark.parametrize("selection", ("mouse", "keyboard"))
@pytest.mark.parametrize("backend", ("native", "double"))
def test_run_command_dialog_primary_selection(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, selection: str, backend: str
) -> None:
    clipboard = QGuiApplication.clipboard()
    if backend == "double":
        contents: dict[QClipboard.Mode, str] = {}
        clipboard = Mock(spec=QClipboard)
        clipboard.supportsSelection.return_value = True
        clipboard.setText.side_effect = lambda text, mode=QClipboard.Mode.Clipboard: (
            contents.__setitem__(mode, text)
        )
        clipboard.text.side_effect = lambda mode=QClipboard.Mode.Clipboard: (
            contents.get(mode, "")
        )
        monkeypatch.setattr(QGuiApplication, "clipboard", lambda: clipboard)
    if not clipboard.supportsSelection():
        pytest.skip("The platform does not support the primary selection")
    command = "sudo npm install -g --prefix /usr/local @openai/codex@latest"
    dialog = _dialog(qtbot, command)
    label = dialog.command
    clipboard.setText("ordinary clipboard")
    label.setFocus()
    if selection == "keyboard":
        qtbot.keyClick(label, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    else:
        text_height = label.heightForWidth(label.width()) - 2 * label.margin()
        text_top = (label.height() - text_height) // 2
        half_line = label.fontMetrics().height() // 2
        start = QPoint(label.margin(), text_top + half_line)
        end = QPoint(
            label.width() - label.margin() - 1,
            text_top + text_height - half_line,
        )
        qtbot.mousePress(label, Qt.MouseButton.LeftButton, pos=start)
        qtbot.mouseMove(label, pos=end)
        qtbot.mouseRelease(label, Qt.MouseButton.LeftButton, pos=end)
    assert label.selectedText() == command.replace("/", "/\u200b")
    qtbot.waitUntil(lambda: clipboard.text(QClipboard.Mode.Selection) == command)
    assert clipboard.text() == "ordinary clipboard"


def _check_run_dialog_geometry(mode: str) -> None:
    app = QApplication([])
    theme.apply(app, mode)
    for command in (
        "codex login",
        "tool " + "argument " * 50,
        "/" + "/".join(["long-directory"] * 40) + "/tool",
        "tool " + "a" * 300,
        "tool\n" + "argument\n" * 150,
    ):
        dialog = widgets.RunCommandDialog(command)
        hint = dialog.sizeHint()
        dialog.show()
        app.processEvents()
        assert dialog.devicePixelRatioF() == 1.25
        available = dialog.screen().availableGeometry()
        assert hint.width() <= available.width() - 96
        assert hint.height() <= available.height() - 96
        assert dialog.width() <= available.width() - 96
        assert dialog.height() <= available.height() - 96
        assert hint.width() >= dialog.fontMetrics().horizontalAdvance(dialog.TITLE)
        assert dialog.title.width() >= dialog.title.sizeHint().width()
        assert dialog.title.height() >= dialog.title.sizeHint().height()
        assert dialog.cancel_button.isVisible()
        assert dialog.run_button.isVisible()
        assert dialog.copy_button.isVisible()
        for label in (dialog.text, dialog.command):
            assert label.height() >= label.heightForWidth(label.width())
        bar = dialog.scroll_area.verticalScrollBar()
        if command.count("\n") > 100:
            assert bar.maximum() > 0
            bar.setValue(bar.maximum())
            app.processEvents()
            bottom = dialog.command.mapTo(
                dialog.scroll_area.viewport(), dialog.command.rect().bottomLeft()
            )
            assert bottom.y() < dialog.scroll_area.viewport().height()
        else:
            assert bar.maximum() == 0
            layout = dialog.layout()
            assert layout is not None
            margins = layout.contentsMargins()
            required = (
                margins.top()
                + margins.bottom()
                + dialog.title.sizeHint().height()
                + dialog.text.heightForWidth(dialog.text.width())
                + dialog.command.heightForWidth(dialog.command.width())
                + dialog.copy_button.sizeHint().height()
                + 3 * 16
            )
            assert hint.height() >= required
        assert dialog.command.frameShape() == QFrame.Shape.NoFrame
        if command != "codex login":
            dialog.close()
            continue
        flags = dialog.command.textInteractionFlags()
        for interaction in (flags, Qt.TextInteractionFlag.NoTextInteraction, flags):
            dialog.command.setTextInteractionFlags(interaction)
            app.processEvents()
            image = dialog.grab().toImage()
            origin = dialog.command.mapTo(dialog, QPoint())
            ratio = image.devicePixelRatio()
            left, top = round(origin.x() * ratio), round(origin.y() * ratio)
            width = round(dialog.command.width() * ratio)
            height = round(dialog.command.height() * ratio)
            background = theme.color("bg-page")
            for y in range(-3, height + 3):
                for x in range(-3, width + 3):
                    if 3 <= x < width - 3 and 3 <= y < height - 3:
                        continue
                    assert image.pixelColor(left + x, top + y) == background
        dialog.close()


@pytest.mark.parametrize("mode", ("light", "dark"))
def test_run_command_dialog_fractional_scale(env: dict[str, str], mode: str) -> None:
    test_file = Path(__file__).resolve()
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; "
            "runpy.run_path(sys.argv[1])['_check_run_dialog_geometry'](sys.argv[2])",
            str(test_file),
            mode,
        ],
        env={
            **os.environ,
            **env,
            "PYTHONPATH": str(test_file.parents[2] / "src"),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_SCALE_FACTOR": "1.25",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr


def test_switch_hit_area_is_the_whole_widget(qtbot: QtBot) -> None:
    switch = widgets.Switch()
    qtbot.addWidget(switch)
    switch.show()
    qtbot.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
    assert switch.isChecked()


@pytest.mark.parametrize(
    ("size", "height"), [(None, theme.CONTROL_MD), ("sm", theme.CONTROL_SM)]
)
def test_button_height_follows_scale(
    qapp: QApplication, qtbot: QtBot, size: str | None, height: int
) -> None:
    previous = theme.current_mode()
    try:
        theme.apply(qapp, "light")
        result = widgets.button("Go", "secondary", size=size)
        qtbot.addWidget(result)
        result.show()
        assert result.height() == height
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


def test_small_field_height(qapp: QApplication, qtbot: QtBot) -> None:
    previous = theme.current_mode()
    try:
        theme.apply(qapp, "light")
        field = QLineEdit()
        field.setProperty("scale", "sm")
        qtbot.addWidget(field)
        field.show()
        assert field.height() == theme.CONTROL_SM
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


def test_set_icon_keeps_a_gap_and_the_accessible_name(qtbot: QtBot) -> None:
    target = widgets.button("Check now", "secondary")
    qtbot.addWidget(target)
    widgets.set_icon(target, QIcon())
    widgets.set_icon(target, QIcon())
    assert target.text() == widgets.ICON_GAP + "Check now"
    assert target.accessibleName() == "Check now"


def test_incomplete_badge(qtbot: QtBot) -> None:
    badge = widgets.Badge("Incomplete", "incomplete")
    qtbot.addWidget(badge)
    assert badge.text() == "Incomplete"
    assert badge.tone() == "incomplete"


@pytest.fixture
def focus_host(
    qapp: QApplication, qtbot: QtBot
) -> Iterator[tuple[QWidget, QPushButton, QComboBox]]:
    """A shown window with a button and a picker, keyboard focus tracked."""
    tracker = widgets.install_focus_tracking(qapp)
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    first = widgets.button("First")
    combo = QComboBox()
    combo.addItems(["all", "Codex"])
    layout.addWidget(first)
    layout.addWidget(combo)
    host.show()
    qtbot.waitExposed(host)
    host.activateWindow()
    qtbot.waitActive(host)
    yield host, first, combo
    qapp.removeEventFilter(tracker)
    tracker.setParent(None)
    tracker.deleteLater()


def test_click_focus_has_no_ring_and_tab_focus_has_one(
    qtbot: QtBot, focus_host: tuple[QWidget, QPushButton, QComboBox]
) -> None:
    _host, first, combo = focus_host
    qtbot.mouseClick(first, Qt.MouseButton.LeftButton)
    assert first.hasFocus()
    assert not widgets.focus_visible(first)
    qtbot.keyClick(first, Qt.Key.Key_Tab)
    assert combo.hasFocus()
    assert widgets.focus_visible(combo)
    assert first.property("keyfocus") is False
    qtbot.mouseClick(first, Qt.MouseButton.LeftButton)
    assert not widgets.focus_visible(first)
    assert combo.property("keyfocus") is False


def test_app_moved_focus_follows_the_last_input(
    qtbot: QtBot, focus_host: tuple[QWidget, QPushButton, QComboBox]
) -> None:
    _host, first, combo = focus_host
    qtbot.mouseClick(first, Qt.MouseButton.LeftButton)
    combo.setFocus(Qt.FocusReason.OtherFocusReason)
    assert not widgets.focus_visible(combo)
    qtbot.keyClick(combo, Qt.Key.Key_Down)
    first.setFocus(Qt.FocusReason.OtherFocusReason)
    assert widgets.focus_visible(first)


@pytest.mark.parametrize("mode", theme.MODES)
def test_ring_is_drawn_only_for_keyboard_focus(
    qapp: QApplication,
    qtbot: QtBot,
    focus_host: tuple[QWidget, QPushButton, QComboBox],
    mode: str,
) -> None:
    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        _host, first, combo = focus_host
        ring = theme.color("focus-ring").name()
        combo.setFocus(Qt.FocusReason.MouseFocusReason)
        qapp.processEvents()
        clicked = combo.grab().toImage()
        assert clicked.pixelColor(clicked.width() // 2, 0).name() != ring
        first.setFocus(Qt.FocusReason.MouseFocusReason)
        qtbot.keyClick(first, Qt.Key.Key_Tab)
        qapp.processEvents()
        tabbed = combo.grab().toImage()
        assert tabbed.pixelColor(tabbed.width() // 2, 0).name() == ring
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


LONG_TIP = (
    "When Claude has a plan checked for gaps before a flow or Claude Workflow "
    "starts. A plan the setting skips starts without a check and without a "
    "question."
)


def test_tip_text_keeps_short_text() -> None:
    assert widgets.tip_text("Remove shown records") == "Remove shown records"
    assert widgets.tip_text("gpt-6-astra\nGPT 6 Astra") == "gpt-6-astra\nGPT 6 Astra"
    assert widgets.tip_text("") == ""


def test_tip_text_wraps_long_text_at_the_tip_width() -> None:
    shown = widgets.tip_text(LONG_TIP + " <b>")
    assert shown.startswith(f'<table width="{widgets.TIP_WIDTH}"')
    assert "When Claude has a plan" in shown
    assert "&lt;b&gt;" in shown
    assert QtGui.Qt.mightBeRichText(shown)


def test_tip_text_breaks_long_paths_after_slashes() -> None:
    path = "/home/owner/.local/state/ember/runs/codex-20261103-120000-abcdef/reply.md"
    shown = widgets.tip_text(path + "\n" + path)
    assert f"/{widgets.PATH_BREAK}runs/{widgets.PATH_BREAK}" in shown
    assert "<br>" in shown


def test_tip_wrapper_shows_long_tooltips_wrapped(
    qapp: QApplication, qtbot: QtBot
) -> None:
    widgets.install_tooltips(qapp)
    assert widgets.install_tooltips(qapp) is qapp.findChild(widgets.TipWrapper)
    label = QLabel("x")
    qtbot.addWidget(label)
    label.setToolTip(LONG_TIP)
    label.show()
    point = QPoint(2, 2)
    event = QHelpEvent(QEvent.Type.ToolTip, point, label.mapToGlobal(point))
    try:
        QApplication.sendEvent(label, event)
        assert QToolTip.text() == widgets.tip_text(LONG_TIP)
        assert label.toolTip() == LONG_TIP
    finally:
        QToolTip.hideText()


def _focus(widget: QWidget, kind: QEvent.Type, reason: Qt.FocusReason) -> None:
    QApplication.sendEvent(widget, QFocusEvent(kind, reason))


def test_info_tip_shows_on_click_and_keyboard_focus(qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    info = widgets.InfoTip("Run the gap check", LONG_TIP)
    layout.addWidget(info)
    host.show()
    assert info.accessibleName() == "About Run the gap check"
    assert info.toolTip() == LONG_TIP
    assert info.focusPolicy() == Qt.FocusPolicy.StrongFocus
    try:
        info.click()
        assert QToolTip.isVisible()
        assert QToolTip.text() == widgets.tip_text(LONG_TIP)
        QToolTip.hideText()
        qtbot.waitUntil(lambda: not QToolTip.isVisible(), timeout=2000)
        _focus(info, QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason)
        assert not QToolTip.isVisible()
        _focus(info, QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason)
        assert QToolTip.isVisible()
        assert QToolTip.text() == widgets.tip_text(LONG_TIP)
        _focus(info, QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(lambda: not QToolTip.isVisible(), timeout=2000)
    finally:
        QToolTip.hideText()
