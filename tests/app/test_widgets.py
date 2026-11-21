import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from PySide6 import QtGui
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import (
    QClipboard,
    QContextMenuEvent,
    QEnterEvent,
    QFocusEvent,
    QGuiApplication,
    QHelpEvent,
    QIcon,
    QImage,
    QPaintEvent,
    QTextLayout,
    QWindow,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from pytestqt.qtbot import QtBot

from ember.app import theme, widgets
from ember.app.screens import menu


@pytest.mark.parametrize("height", [40, 64])
def test_pill_row_paints_half_height_radius_and_follows_theme(
    qtbot: QtBot, qapp: QApplication, height: int
) -> None:
    card = widgets.Card()
    qtbot.addWidget(card)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(20, 20, 20, 20)
    row = widgets.PillRow()
    row.setFixedHeight(height)
    layout.addWidget(row)
    card.resize(360, height + 40)
    card.show()
    for mode in ("light", "dark", "light"):
        theme.apply(qapp, mode)
        qtbot.wait(10)
        origin = row.mapTo(card, QPoint())
        image = (
            card.grab()
            .toImage()
            .copy(origin.x(), origin.y(), row.width(), row.height())
        )
        fill = theme.color("bg-subtle", mode)
        assert image.pixelColor(row.width() // 2, 1) == fill
        assert image.pixelColor(1, height // 2) == fill
        assert image.pixelColor(row.width() - 2, height // 2) == fill
        assert image.pixelColor(height // 10, height // 10) == theme.color(
            "bg-card", mode
        )
        assert image.pixelColor(height // 4, height // 10) == fill
    assert row.focusPolicy() == Qt.FocusPolicy.NoFocus


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
        assert name.text() == text
        assert name.toolTip() == ""
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
        assert name.display_text() == name.text() == "short"
        assert name.toolTip() == ""
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


@pytest.mark.parametrize("focused_segment", [1, 2, None])
@pytest.mark.parametrize(
    "reason", [Qt.FocusReason.TabFocusReason, Qt.FocusReason.MouseFocusReason]
)
def test_segmented_value_change_follows_focus_within_control(
    qtbot: QtBot,
    focus_host: tuple[QWidget, QPushButton, QComboBox],
    focused_segment: int | None,
    reason: Qt.FocusReason,
) -> None:
    host, other, combo = focus_host
    control = widgets.Segmented((("a", "A"), ("b", "B"), ("c", "C")))
    layout = host.layout()
    assert layout is not None
    layout.addWidget(control)
    control.show()
    control.set_value("b")
    # TabFocusReason can show a ring while the last input was the mouse,
    # as it does when Reset returns focus to the selected segment.
    qtbot.mouseClick(other, Qt.MouseButton.LeftButton)
    combo.setFocus(Qt.FocusReason.MouseFocusReason)
    focused = other if focused_segment is None else control.buttons()[focused_segment]
    focused.setFocus(reason)
    assert focused.hasFocus()
    with qtbot.assertNotEmitted(control.changed):
        control.set_value("a")
    assert control.value() == "a"
    target = other if focused_segment is None else control.buttons()[0]
    assert target.hasFocus()
    assert widgets.focus_visible(target) == (reason == Qt.FocusReason.TabFocusReason)
    if focused_segment is not None:
        assert not widgets.focus_visible(focused)


def test_segmented_unchanged_value_keeps_focus_on_unselected_segment(
    qtbot: QtBot, focus_host: tuple[QWidget, QPushButton, QComboBox]
) -> None:
    host, _other, _combo = focus_host
    control = widgets.Segmented((("a", "A"), ("b", "B")))
    layout = host.layout()
    assert layout is not None
    layout.addWidget(control)
    control.show()
    control.buttons()[1].setFocus(Qt.FocusReason.TabFocusReason)
    with qtbot.assertNotEmitted(control.changed):
        control.set_value("a")
    assert widgets.focus_visible(control.buttons()[1])


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


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("delta", [-120, 120])
def test_picker_wheel_scrolls_parent_without_changing_value(
    qtbot: QtBot, focused: bool, delta: int
) -> None:
    area = QScrollArea()
    qtbot.addWidget(area)
    area.setWidgetResizable(True)
    content = QWidget()
    layout = QVBoxLayout(content)
    other = QPushButton("Other control")
    layout.addWidget(other)
    layout.addSpacing(300)
    picker = widgets.Picker()
    for value in ("First", "Second", "Third"):
        picker.add_option(value, value)
    picker.setCurrentIndex(1)
    layout.addWidget(picker)
    layout.addSpacing(600)
    area.setWidget(content)
    area.resize(400, 250)
    area.show()
    qtbot.waitExposed(area)
    area.activateWindow()
    qtbot.waitActive(area)
    area.ensureWidgetVisible(picker)
    QApplication.processEvents()
    (picker if focused else other).setFocus()
    assert picker.hasFocus() == focused
    bar = area.verticalScrollBar()
    before = bar.value()
    assert 0 < before < bar.maximum()
    assert not picker.view().isVisible()
    window = area.windowHandle()
    assert window is not None
    position = picker.mapTo(area, picker.rect().center())
    with qtbot.assertNotEmitted(picker.currentIndexChanged):
        QTest.wheelEvent(window, position, QPoint(0, delta))
    assert picker.currentIndex() == 1
    qtbot.waitUntil(lambda: bar.value() != before)
    assert (bar.value() > before) == (delta < 0)


def test_picker_keyboard_and_popup_selection(qtbot: QtBot) -> None:
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    for value in ("First", "Second", "Third"):
        picker.add_option(value, value)
    picker.show()
    picker.setFocus()
    qtbot.keyClick(picker, Qt.Key.Key_Down)
    assert picker.currentIndex() == 1
    qtbot.mouseClick(picker, Qt.MouseButton.LeftButton)
    view = picker.view()
    assert view.isVisible()
    qtbot.keyClick(view, Qt.Key.Key_Down)
    qtbot.keyClick(view, Qt.Key.Key_Return)
    assert picker.currentIndex() == 2
    assert not view.isVisible()


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


def _assert_rounded_popup(window: QWidget) -> None:
    assert window.isVisible()
    image = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    window.render(image)
    bounds = image.rect()
    for corner in (
        bounds.topLeft(),
        bounds.topRight(),
        bounds.bottomLeft(),
        bounds.bottomRight(),
    ):
        assert image.pixelColor(corner).alpha() == 0, corner
    assert image.pixelColor(bounds.center()).alpha() == 255
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.windowFlags() & Qt.WindowType.NoDropShadowWindowHint


@pytest.mark.parametrize("mode", theme.MODES)
@pytest.mark.parametrize("count", [0, 1, 20])
def test_picker_popup_has_transparent_corners(
    qapp: QApplication, qtbot: QtBot, mode: str, count: int
) -> None:
    theme.apply(qapp, mode)
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    for row in range(count):
        picker.add_option(str(row), row)
    picker.setCurrentIndex(count - 1)
    picker.show()
    picker.showPopup()
    view = picker.view()
    qtbot.waitExposed(view.window())
    assert view.verticalScrollBar().isVisible() == (count > picker.maxVisibleItems())
    _assert_rounded_popup(view.window())


@pytest.mark.parametrize("count", [0, 1, 5, 10, 596])
def test_picker_popup_fits_up_to_ten_rows(
    qapp: QApplication, qtbot: QtBot, count: int
) -> None:
    theme.apply(qapp, "light")
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    for row in range(count):
        picker.add_option(str(row), row)
    picker.setCurrentIndex(count - 1)
    picker.show()
    picker.showPopup()
    view = picker.view()
    popup = view.window()
    qtbot.waitExposed(popup)
    height = sum(view.sizeHintForRow(i) for i in range(min(10, count)))
    assert view.viewport().height() == height
    assert popup.height() == height + 18  # 8 px padding and 1 px border per edge.
    assert view.verticalScrollBar().isVisible() == (count > 10)
    assert popup.height() < picker.screen().availableGeometry().height() // 2
    if count:
        selected = picker.model().index(count - 1, 0)
        assert view.viewport().rect().contains(view.visualRect(selected))
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert not popup.isVisible()


def test_picker_popup_counts_headers_and_resizes_for_visible_hints(
    qapp: QApplication, qtbot: QtBot
) -> None:
    theme.apply(qapp, "light")
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    picker.add_header("Workers")
    for row in range(1, 26):
        picker.add_option(str(row), row, hint="Shortlisted" if row < 13 else None)
    picker.show()
    picker.showPopup()
    view = picker.view()
    qtbot.waitExposed(view.window())
    viewport = view.viewport()

    def check_page(first: int) -> None:
        assert view.indexAt(QPoint()).row() == first
        assert view.indexAt(viewport.rect().bottomLeft()).row() == first + 9
        assert viewport.height() == sum(
            view.sizeHintForRow(row) for row in range(first, first + 10)
        )

    check_page(0)
    assert view.sizeHintForRow(1) > view.sizeHintForRow(0)
    assert view.verticalScrollBar().isVisible()
    qtbot.keyClick(view, Qt.Key.Key_End)
    check_page(16)
    qtbot.keyClick(view, Qt.Key.Key_Return)
    assert picker.value() == 25
    picker.showPopup()
    check_page(16)
    bar = view.verticalScrollBar()
    popup = view.window()
    window = popup.windowHandle()
    assert window is not None
    QTest.wheelEvent(
        window, viewport.mapTo(popup, viewport.rect().center()), QPoint(0, 120)
    )
    assert bar.value() < 16
    check_page(bar.value())
    bar.setValue(6)
    check_page(6)
    option = picker.model().index(8, 0)
    qtbot.mouseClick(
        viewport, Qt.MouseButton.LeftButton, pos=view.visualRect(option).center()
    )
    assert picker.value() == 8


def test_open_picker_refits_after_blocked_model_rebuild(
    qapp: QApplication, qtbot: QtBot
) -> None:
    theme.apply(qapp, "light")
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    picker.add_option("All providers", None)
    picker.show()
    picker.showPopup()
    view = picker.view()
    qtbot.waitExposed(view.window())
    assert view.viewport().height() == view.sizeHintForRow(0)

    with QSignalBlocker(picker):
        picker.clear()
        picker.add_option("All providers", None, "4 models")
        for row in range(4):
            picker.add_option(str(row), row, "1 model")
        picker.setCurrentIndex(0)

    qtbot.waitUntil(
        lambda: (
            view.viewport().height() == sum(view.sizeHintForRow(i) for i in range(5))
        )
    )
    assert view.isVisible()
    assert view.verticalScrollBar().value() == 0
    assert not view.verticalScrollBar().isVisible()


@pytest.mark.parametrize("count", [10, 20])
def test_picker_fits_complete_hinted_rows_on_short_screen(
    qapp: QApplication, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    theme.apply(qapp, "light")
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    for row in range(count):
        picker.add_option(str(row), row, "Hint line")
    picker.setCurrentIndex(count - 1)
    picker.show()
    view = picker.view()
    normal_height = 10 * view.sizeHintForRow(0) + 2 * view.frameWidth()
    area = QRect(0, 0, 853, normal_height - 25)
    screen = picker.screen()
    monkeypatch.setattr(screen, "availableGeometry", lambda: area)
    picker.showPopup()
    qtbot.waitExposed(view.window())
    assert area.contains(view.window().geometry())
    assert view.verticalScrollBar().isVisible() == (count > 10)
    viewport = view.viewport().rect()
    first = view.indexAt(viewport.topLeft()).row()
    last = view.indexAt(viewport.bottomLeft()).row()
    assert last == count - 1
    if count == 10:
        assert first == 0
    assert view.visualRect(picker.model().index(first, 0)).top() == viewport.top()
    assert view.visualRect(picker.model().index(last, 0)).bottom() == viewport.bottom()
    body = QtGui.QFontMetrics(theme.font("body"))
    caption = QtGui.QFontMetrics(theme.font("caption"))
    assert view.sizeHintForRow(last) >= body.height() + caption.height()


def _check_gap_check_popup() -> None:
    app = QApplication([])
    theme.apply(app, "light")
    picker = widgets.Picker()
    # DejaVu Sans at 16 px gives the combo box the reported 19 px line height.
    picker.setStyleSheet("QComboBox { font-size: 16px; }")
    for value, label in menu.WHEN:
        picker.add_option(label, value, menu.when_hint(value))
    assert picker.set_value("ask")
    picker.show()
    picker.showPopup()
    app.processEvents()

    view = picker.view()
    area = picker.screen().availableGeometry()
    assert area.height() == 384
    assert area.contains(view.window().geometry())
    assert not view.verticalScrollBar().isVisible()
    assert picker.count() == 10
    for row in range(picker.count()):
        assert (
            view.viewport()
            .rect()
            .contains(view.visualRect(picker.model().index(row, 0)))
        )


def test_gap_check_popup_fits_at_double_scale(
    env: dict[str, str], tmp_path: Path
) -> None:
    config = tmp_path / "screen.json"
    config.write_text('{"screens": [{"width": 1366, "height": 768}]}')
    test_file = Path(__file__).resolve()
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; "
            "runpy.run_path(sys.argv[1])['_check_gap_check_popup']()",
            str(test_file),
        ],
        env={
            **os.environ,
            **env,
            "PYTHONPATH": str(test_file.parents[2] / "src"),
            "QT_QPA_PLATFORM": f"offscreen:configfile={config}",
            "QT_SCALE_FACTOR": "2",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr


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


@pytest.mark.parametrize("mode", theme.MODES)
def test_copy_menu_has_transparent_corners_and_copies_selection(
    qapp: QApplication, qtbot: QtBot, mode: str
) -> None:
    theme.apply(qapp, mode)
    dialog = _dialog(qtbot, "/opt/tools/bin/tool")
    label = dialog.command
    label.setSelection(0, 6)
    QGuiApplication.clipboard().clear()

    def copy_selection() -> None:
        menu = label.findChild(QMenu)
        assert menu is not None
        try:
            copy = menu.actions()[0]
            menu.setActiveAction(copy)
            _assert_rounded_popup(menu)
            copy.trigger()
        finally:
            menu.close()

    QTimer.singleShot(0, copy_selection)
    point = label.rect().center()
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, label.mapToGlobal(point)
    )
    QApplication.sendEvent(label, event)
    assert QGuiApplication.clipboard().text() == "/opt/"


@pytest.mark.parametrize("selection", ("mouse", "keyboard"))
def test_run_command_dialog_primary_selection(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    # Qt's offscreen platform has no primary selection, so use a clipboard
    # double that has one.
    contents: dict[QClipboard.Mode, str] = {}
    clipboard = Mock(spec=QClipboard)
    clipboard.supportsSelection.return_value = True
    clipboard.setText.side_effect = lambda text, mode=QClipboard.Mode.Clipboard: (
        contents.__setitem__(mode, text)
    )
    clipboard.text.side_effect = lambda mode=QClipboard.Mode.Clipboard: contents.get(
        mode, ""
    )
    monkeypatch.setattr(QGuiApplication, "clipboard", lambda: clipboard)
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
    assert shown.startswith("<p>")
    assert "When Claude has a plan" in shown
    assert "&lt;b&gt;" in shown
    assert QtGui.Qt.mightBeRichText(shown)


def test_tip_text_breaks_long_paths_after_slashes() -> None:
    path = "/home/owner/.local/state/ember/runs/codex-20261103-120000-abcdef/reply.md"
    shown = widgets.tip_text(path + "\n" + path)
    assert f"/{widgets.PATH_BREAK}runs/{widgets.PATH_BREAK}" in shown
    assert "<br>" in shown


def _popup_shows(owner: QWidget) -> bool:
    popup = widgets.tip_popup()
    return popup.isVisible() and popup.owner is owner


def test_tip_wrapper_suppresses_qt_tooltips_and_info_uses_hover(
    qapp: QApplication, qtbot: QtBot
) -> None:
    widgets.install_tooltips(qapp)
    assert widgets.install_tooltips(qapp) is qapp.findChild(widgets.TipWrapper)
    label = QLabel("x")
    info = widgets.InfoTip("Run the gap check", LONG_TIP)
    for widget in (label, info):
        qtbot.addWidget(widget)
        widget.show()
    label.setToolTip("Not shown")
    point = QPoint(2, 2)
    try:
        event = QHelpEvent(QEvent.Type.ToolTip, point, label.mapToGlobal(point))
        QApplication.sendEvent(label, event)
        assert not QToolTip.isVisible()
        assert not widgets.tip_popup().isVisible()
        event = QHelpEvent(QEvent.Type.ToolTip, point, info.mapToGlobal(point))
        QApplication.sendEvent(info, event)
        assert not widgets.tip_popup().isVisible()
        QApplication.sendEvent(info, QEnterEvent(QPointF(), QPointF(), QPointF()))
        assert _popup_shows(info)
        assert widgets.tip_popup().text() == widgets.tip_text(LONG_TIP)
        assert not QToolTip.isVisible()
        QApplication.sendEvent(info, QEvent(QEvent.Type.Leave))
        assert not widgets.tip_popup().isVisible()
    finally:
        info.hide_tip()


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
        assert _popup_shows(info)
        info.hide_tip()
        _focus(info, QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason)
        assert not widgets.tip_popup().isVisible()
        _focus(info, QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason)
        assert _popup_shows(info)
        qtbot.keyClick(info, Qt.Key.Key_Escape)
        assert not widgets.tip_popup().isVisible()
        _focus(info, QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason)
        _focus(info, QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason)
        assert not widgets.tip_popup().isVisible()
        info.set_text("Short.")
        info.show_tip()
        assert widgets.tip_popup().text() == "Short."
        assert info.accessibleDescription() == "Short."
    finally:
        info.hide_tip()


def test_info_tip_stays_open_after_tab_key_release(qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    other = QPushButton("Other")
    other.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    info = widgets.InfoTip("Run the gap check", LONG_TIP)
    layout.addWidget(other)
    layout.addWidget(info)
    host.show()
    qtbot.waitExposed(host)
    host.activateWindow()
    qtbot.waitUntil(host.isActiveWindow, timeout=2000)
    other.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(other.hasFocus, timeout=2000)
    try:
        qtbot.keyPress(other, Qt.Key.Key_Tab)
        qtbot.waitUntil(info.hasFocus, timeout=2000)
        qtbot.keyRelease(info, Qt.Key.Key_Tab)
        qtbot.wait(50)
        assert _popup_shows(info)
    finally:
        info.hide_tip()


def test_tip_popup_follows_owners_between_windows(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = QWidget()
    dialog = QDialog(host)
    for window in (host, dialog):
        qtbot.addWidget(window)
    main_info = widgets.InfoTip("Command", "Main command")
    dialog_info = widgets.InfoTip("Command", "Dialog command")
    QVBoxLayout(host).addWidget(main_info)
    QVBoxLayout(dialog).addWidget(dialog_info)
    host.show()
    dialog.show()
    popup = widgets.TipPopup()
    qtbot.addWidget(popup)
    monkeypatch.setattr(widgets, "_popup", popup)
    assert popup.windowHandle() is None
    main_info.show_tip()
    handle = popup.windowHandle()
    assert handle is not None
    assert host.windowHandle() is not None
    assert dialog.windowHandle() is not None
    assert handle.transientParent() == host.windowHandle()
    assert _popup_shows(main_info)
    set_parent = handle.setTransientParent
    parents: list[QWindow] = []

    def set_parent_while_hidden(parent: QWindow) -> None:
        assert not popup.isVisible()
        parents.append(parent)
        set_parent(parent)

    monkeypatch.setattr(handle, "setTransientParent", set_parent_while_hidden)
    for info, window in ((dialog_info, dialog), (main_info, host)):
        assert popup.isVisible()
        info.show_tip()
        assert _popup_shows(info)
        assert handle.transientParent() == window.windowHandle()
    assert parents == [dialog.windowHandle(), host.windowHandle()]


def test_tip_popup_shows_for_owner_without_window_handle(qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    info = widgets.InfoTip("Command", "Command details")
    QVBoxLayout(host).addWidget(info)
    popup = widgets.TipPopup()
    qtbot.addWidget(popup)
    assert host.windowHandle() is None
    popup.show_for(info, info.toolTip())
    assert popup.isVisible()
    assert popup.owner is info
    assert popup.text() == info.toolTip()


def test_tip_popup_is_rounded_and_wraps_at_the_tip_width(
    qapp: QApplication, qtbot: QtBot
) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    popup = widgets.tip_popup()
    try:
        popup.show_for(host, LONG_TIP)
        assert popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert popup.width() <= widgets.TIP_WIDTH + 2 * widgets.TipPopup.PADDING[0] + 8
        image = popup.grab().toImage()
        assert image.pixelColor(0, 0).alpha() < 255
        middle = image.pixelColor(popup.width() // 2, 2)
        assert middle.name() == theme.color("fill-primary").name()
        popup.show_for(host, "Short.")
        assert popup.text() == "Short."
    finally:
        popup.hide_for(host)


def test_escape_closes_a_tooltip_wherever_focus_is(
    qapp: QApplication, qtbot: QtBot
) -> None:
    widgets.install_tooltips(qapp)
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    field = QLineEdit()
    info = widgets.InfoTip("Terminal", LONG_TIP)
    layout.addWidget(field)
    layout.addWidget(info)
    host.show()
    try:
        info.show_tip()
        assert widgets.tip_popup().isVisible()
        qtbot.keyClick(field, Qt.Key.Key_Escape)
        assert not widgets.tip_popup().isVisible()
    finally:
        info.hide_tip()


def test_tip_popup_stays_on_the_screen(qapp: QApplication, qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(40, 20)
    host.show()
    screen = host.screen()
    assert screen is not None
    area = screen.availableGeometry()
    host.move(area.right() - 40, area.bottom() - 20)
    popup = widgets.tip_popup()
    try:
        popup.show_for(host, LONG_TIP)
        assert area.contains(popup.geometry())
        assert popup.geometry().bottom() < host.mapToGlobal(QPoint(0, 0)).y()
    finally:
        popup.hide_for(host)


def test_picker_elides_closed_value_without_tooltip(qtbot: QtBot) -> None:
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    closed = "Same as Reviewer · Codex · a-model-with-a-long-name · high"
    picker.add_option("A model", "long", closed=closed)
    picker.add_option("Short", "short")
    picker.resize(theme.PICKER_MIN_WIDTH, 36)
    picker.show()
    picker.repaint()
    assert picker.toolTip() == ""
    assert picker.elided_text().endswith("…")
    assert picker.elided_text() != closed
    picker.set_value("short")
    picker.repaint()
    assert picker.toolTip() == ""
    picker.set_value("long")
    picker.repaint()
    assert picker.toolTip() == ""
    picker.resize(picker.sizeHint())
    picker.repaint()
    assert picker.toolTip() == ""
    assert picker.elided_text() == closed


def test_standalone_picker_shrinks_to_floor_and_stops_at_hint(qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    picker = widgets.Picker()
    picker.add_option("A long model name " * 5, "long")
    layout.addWidget(picker)
    host.resize(250, 100)
    host.show()
    qtbot.wait(20)
    assert theme.PICKER_MIN_WIDTH <= picker.width() < picker.sizeHint().width()
    host.resize(1400, 100)
    qtbot.wait(20)
    assert picker.width() == picker.sizeHint().width()


def test_icon_button_has_accessible_tooltip_and_follows_theme(
    qapp: QApplication, qtbot: QtBot
) -> None:
    button = widgets.IconButton("panel-left", "Show sidebar")
    qtbot.addWidget(button)
    assert button.accessibleName() == button.toolTip() == "Show sidebar"
    assert button.property("kind") == "ghost"
    assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert button.width() == button.height() == theme.CONTROL_SM
    before = button.icon().cacheKey()
    theme.apply(qapp, "dark" if theme.current_mode() == "light" else "light")
    assert button.icon().cacheKey() != before


def test_tooltip_filter_drops_even_opted_in_qt_tooltip_events(qtbot: QtBot) -> None:
    label = QLabel("Mark")
    qtbot.addWidget(label)
    wrapper = widgets.TipWrapper()
    event = QHelpEvent(QEvent.Type.ToolTip, QPoint(0, 0), QPoint(0, 0))
    assert wrapper.eventFilter(label, event)
    label.setProperty("show-tooltip", True)
    assert wrapper.eventFilter(label, event)
    button = widgets.IconButton("panel-left", "Hide sidebar")
    qtbot.addWidget(button)
    assert wrapper.eventFilter(button, event)


@pytest.mark.parametrize("kind", ["icon", "row", "mark"])
def test_opted_in_control_uses_ember_tip_on_hover_and_focus(
    qapp: QApplication, qtbot: QtBot, kind: str
) -> None:
    widgets.install_tooltips(qapp)
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    other = QPushButton("Other")
    if kind == "icon":
        control = widgets.IconButton("panel-left", "Show sidebar")
    else:
        control = QLabel() if kind == "mark" else QPushButton()
        control.setProperty("show-tooltip", True)
        control.setToolTip("Ember 1.0.0" if kind == "mark" else "Overview · 2 live")
        control.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    layout.addWidget(other)
    layout.addWidget(control)
    host.show()
    qtbot.waitExposed(host)
    with qtbot.waitActive(host):
        host.activateWindow()
    other.setFocus()
    popup = widgets.tip_popup()
    popup.hide()
    try:
        event = QHelpEvent(QEvent.Type.ToolTip, QPoint(), QPoint())
        QApplication.sendEvent(control, event)
        assert not QToolTip.isVisible()
        assert not popup.isVisible()
        QApplication.sendEvent(control, QEnterEvent(QPointF(), QPointF(), QPointF()))
        assert _popup_shows(control)
        assert popup.text() == control.toolTip()
        assert not QToolTip.isVisible()
        QApplication.sendEvent(control, QEvent(QEvent.Type.Leave))
        assert not popup.isVisible()
        control.setFocus(Qt.FocusReason.MouseFocusReason)
        assert not popup.isVisible()
        other.setFocus()
        qtbot.keyClick(other, Qt.Key.Key_Tab)
        assert control.hasFocus()
        assert _popup_shows(control)
        QApplication.sendEvent(control, QEvent(QEvent.Type.Leave))
        assert _popup_shows(control)
        qtbot.keyClick(control, Qt.Key.Key_Tab)
        assert not popup.isVisible()
        QApplication.sendEvent(control, QEnterEvent(QPointF(), QPointF(), QPointF()))
        assert _popup_shows(control)
        qtbot.keyClick(other, Qt.Key.Key_Escape)
        assert not popup.isVisible()
        QApplication.sendEvent(control, QEnterEvent(QPointF(), QPointF(), QPointF()))
        control.hide()
        assert not popup.isVisible()
    finally:
        popup.hide_for(control)


def test_ember_tip_updates_and_hides_when_control_text_clears(
    qapp: QApplication, qtbot: QtBot
) -> None:
    widgets.install_tooltips(qapp)
    control = widgets.IconButton("panel-left", "Show sidebar")
    qtbot.addWidget(control)
    control.show()
    try:
        QApplication.sendEvent(control, QEnterEvent(QPointF(), QPointF(), QPointF()))
        control.setToolTip("Hide sidebar")
        assert widgets.tip_popup().text() == "Hide sidebar"
        control.setToolTip("")
        assert not widgets.tip_popup().isVisible()
        QApplication.sendEvent(control, QEnterEvent(QPointF(), QPointF(), QPointF()))
        assert not widgets.tip_popup().isVisible()
    finally:
        widgets.tip_popup().hide_for(control)


@pytest.fixture(params=["info", "icon", "nav"])
def tip_control(
    request: pytest.FixtureRequest,
    qapp: QApplication,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[QPushButton, QPushButton, widgets.TipPopup]:
    from ember.app.window import NavButton

    widgets.install_tooltips(qapp)
    popup = widgets.TipPopup()
    qtbot.addWidget(popup)
    monkeypatch.setattr(widgets, "_popup", popup)
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    other = QPushButton("Other")
    control: QPushButton
    if request.param == "info":
        control = widgets.InfoTip("Overview", "Overview · 1 live")
    elif request.param == "icon":
        control = widgets.IconButton("panel-left", "Show sidebar")
    else:
        nav = NavButton("overview", "Overview", "panel-left")
        nav.set_collapsed(True)
        nav.set_count(1, "1 live")
        control = nav
    layout.addWidget(other)
    layout.addWidget(control)
    host.show()
    qtbot.waitExposed(host)
    with qtbot.waitActive(host):
        host.activateWindow()
    other.setFocus(Qt.FocusReason.MouseFocusReason)
    popup.hide()
    return control, other, popup


@pytest.mark.parametrize("hover_first", [True, False])
def test_hover_tip_leaves_a_mouse_focused_control(
    qtbot: QtBot,
    tip_control: tuple[QPushButton, QPushButton, widgets.TipPopup],
    hover_first: bool,
) -> None:
    control, _, popup = tip_control
    enter = QEnterEvent(QPointF(), QPointF(), QPointF())
    if hover_first:
        QApplication.sendEvent(control, enter)
    control.setFocus(Qt.FocusReason.MouseFocusReason)
    qtbot.mouseClick(control, Qt.MouseButton.LeftButton)
    if not hover_first:
        QApplication.sendEvent(control, enter)
    assert control.hasFocus()
    assert _popup_shows(control)
    control.setToolTip("Updated count · 2 live")
    assert popup.text() == control.toolTip()
    QApplication.sendEvent(control, QEvent(QEvent.Type.Leave))
    assert control.hasFocus()
    assert not popup.isVisible()
    control.setToolTip("Updated count · 3 live")
    assert not popup.isVisible()


@pytest.mark.parametrize("hover_first", [True, False])
def test_keyboard_tip_survives_hover_and_text_updates_until_focus_leaves(
    tip_control: tuple[QPushButton, QPushButton, widgets.TipPopup],
    hover_first: bool,
) -> None:
    control, other, popup = tip_control
    enter = QEnterEvent(QPointF(), QPointF(), QPointF())
    if hover_first:
        QApplication.sendEvent(control, enter)
    control.setFocus(Qt.FocusReason.TabFocusReason)
    if not hover_first:
        QApplication.sendEvent(control, enter)
    assert control.hasFocus()
    assert _popup_shows(control)
    if isinstance(control, widgets.InfoTip):
        control.set_text("Updated details")
    else:
        control.setToolTip("Updated details")
    assert popup.text() == "Updated details"
    QApplication.sendEvent(control, QEvent(QEvent.Type.Leave))
    assert _popup_shows(control)
    other.setFocus(Qt.FocusReason.TabFocusReason)
    assert not popup.isVisible()


@pytest.mark.parametrize("keyboard", [True, False])
def test_tip_text_updates_only_its_visible_owner_and_escape_closes_it(
    qtbot: QtBot,
    tip_control: tuple[QPushButton, QPushButton, widgets.TipPopup],
    keyboard: bool,
) -> None:
    control, other, popup = tip_control
    other.setProperty("show-tooltip", True)
    other.setToolTip("Other details")
    if keyboard:
        control.setFocus(Qt.FocusReason.TabFocusReason)
    else:
        QApplication.sendEvent(control, QEnterEvent(QPointF(), QPointF(), QPointF()))
    assert _popup_shows(control)
    text = popup.text()
    other.setToolTip("Changed other details")
    QApplication.sendEvent(other, QEvent(QEvent.Type.Leave))
    assert _popup_shows(control)
    assert popup.text() == text
    qtbot.keyClick(other, Qt.Key.Key_Escape)
    assert not popup.isVisible()
    control.setToolTip("Changed after Escape")
    assert not popup.isVisible()
    QApplication.sendEvent(other, QEnterEvent(QPointF(), QPointF(), QPointF()))
    assert _popup_shows(other)
    control.setToolTip("Changed previous owner")
    assert _popup_shows(other)
    assert popup.text() == other.toolTip()
    QApplication.sendEvent(other, QEvent(QEvent.Type.Leave))
    assert not popup.isVisible()


@pytest.mark.parametrize("keyboard", [True, False])
def test_info_tip_activation_and_text_update_keep_opening_mode(
    qapp: QApplication,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    keyboard: bool,
) -> None:
    wrapper = widgets.install_tooltips(qapp)
    qapp.removeEventFilter(wrapper)
    popup = widgets.TipPopup()
    qtbot.addWidget(popup)
    monkeypatch.setattr(widgets, "_popup", popup)
    host = QWidget()
    qtbot.addWidget(host)
    info = widgets.InfoTip("Command", "Command details")
    other = QPushButton("Other")
    layout = QVBoxLayout(host)
    layout.addWidget(other)
    layout.addWidget(info)
    host.show()
    qtbot.waitExposed(host)
    with qtbot.waitActive(host):
        host.activateWindow()
    other.setFocus(Qt.FocusReason.MouseFocusReason)
    try:
        if keyboard:
            info.setFocus(Qt.FocusReason.TabFocusReason)
            assert _popup_shows(info)
            info.hide_tip()
            qtbot.keyClick(info, Qt.Key.Key_Space)
        else:
            info.setFocus(Qt.FocusReason.MouseFocusReason)
            qtbot.mouseClick(info, Qt.MouseButton.LeftButton)
        assert info.hasFocus()
        assert _popup_shows(info)
        info.set_text("Updated command details")
        assert popup.text() == "Updated command details"
        QApplication.sendEvent(info, QEvent(QEvent.Type.Leave))
        assert popup.isVisible() is keyboard
        qtbot.keyClick(info, Qt.Key.Key_Escape)
        assert not popup.isVisible()
    finally:
        qapp.installEventFilter(wrapper)


@pytest.mark.parametrize("kind", ["picker", "segmented"])
@pytest.mark.parametrize("mode", theme.MODES)
def test_changed_outline_yields_to_keyboard_focus(
    qapp: QApplication, qtbot: QtBot, kind: str, mode: str
) -> None:
    """Changed values have one accent pixel; keyboard focus uses two."""
    theme.apply(qapp, mode)
    widgets.install_focus_tracking(qapp)
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    other = QPushButton("Other")
    layout.addWidget(other)
    control: widgets.Picker | widgets.Segmented
    if kind == "picker":
        control = widgets.Picker()
        control.add_option("One", "one")
        target: QWidget = control
    else:
        control = widgets.Segmented((("one", "One"), ("two", "Two")))
        target = control.buttons()[0]
    layout.addWidget(control)
    host.show()
    qtbot.waitExposed(host)
    with qtbot.waitActive(host):
        host.activateWindow()
    other.setFocus(Qt.FocusReason.MouseFocusReason)
    accent = theme.color("accent").name()

    def edge(y: int) -> str:
        """Read a pixel in the middle of the chosen value's top border."""
        qapp.processEvents()
        image = target.grab().toImage()
        return image.pixelColor(image.width() // 2, y).name()

    control.set_changed(False)
    assert edge(0) != accent
    control.set_changed(True)
    assert edge(0) == accent
    assert edge(1) != accent
    target.setFocus(Qt.FocusReason.TabFocusReason)
    assert widgets.focus_visible(target)
    assert edge(0) == edge(1) == accent
    if isinstance(control, widgets.Segmented):
        # Tab can focus an unselected segment; it suppresses the chosen outline.
        control.buttons()[1].setFocus(Qt.FocusReason.TabFocusReason)
        assert control.value() == "one"
        assert edge(0) != accent
    other.setFocus(Qt.FocusReason.MouseFocusReason)
    assert edge(0) == accent
    assert edge(1) != accent
    control.set_changed(False)
    assert edge(0) != accent
