from collections.abc import Sequence
from typing import cast

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPaintEvent, QTextLayout
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit
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
    dialog.show()
    qtbot.waitExposed(dialog)
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
