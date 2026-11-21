import re
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from pytestqt.qtbot import QtBot

from ember.app import theme, widgets


@pytest.mark.parametrize("mode", theme.MODES)
def test_selectable_labels_render_frameless_and_keep_selection(
    qapp: QApplication, qtbot: QtBot, mode: str
) -> None:
    previous = theme.current_mode()
    sheet = qapp.styleSheet()
    try:
        theme.apply(qapp, mode)
        host = QWidget()
        qtbot.addWidget(host)
        layout = QVBoxLayout(host)
        mouse = QLabel("Selectable with the mouse")
        mouse.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(mouse)
        keyboard = QLabel("Selectable with the keyboard")
        keyboard.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        keyboard.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(keyboard)
        host.show()
        qtbot.waitExposed(host)
        with qtbot.waitActive(host):
            host.activateWindow()
        mouse.setFocus()
        assert mouse.focusPolicy() == Qt.FocusPolicy.ClickFocus
        assert mouse.frameShape() == QFrame.Shape.NoFrame
        mouse.setSelection(0, len(mouse.text()))
        assert mouse.selectedText() == mouse.text()
        keyboard.setFocus(Qt.FocusReason.TabFocusReason)
        assert keyboard.hasFocus()
        qtbot.keyClick(keyboard, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert keyboard.selectedText() == keyboard.text()
        assert not host.grab().isNull()
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet(sheet)


OPAQUE_BACKGROUNDS = [
    name
    for name, values in theme.TOKENS.items()
    if name.startswith("bg-") and all(len(v) == 7 for v in values)
]
TEXT_TOKENS = [
    name
    for name in theme.TOKENS
    if (name.startswith("text-") or name.endswith("-text")) and name != "text-disabled"
]


def test_tokens_have_two_valid_values() -> None:
    for name, values in theme.TOKENS.items():
        assert len(values) == 2, name
        for value in values:
            assert re.fullmatch(r"#[0-9a-f]{6}([0-9a-f]{2})?", value), (name, value)


def test_token_values_match_the_plan() -> None:
    assert theme.TOKENS["bg-page"] == ("#ffffff", "#191919")
    assert theme.TOKENS["bg-hover"] == ("#0000000d", "#ffffff13")
    assert theme.TOKENS["accent"] == theme.TOKENS["focus-ring"]
    assert theme.TOKENS["on-accent"] == ("#ffffff", "#ffffff")


def test_opaque_backgrounds_found() -> None:
    assert set(OPAQUE_BACKGROUNDS) == {
        "bg-page",
        "bg-sidebar",
        "bg-card",
        "bg-raised",
        "bg-subtle",
        "bg-accent",
    }


@pytest.mark.parametrize("mode", theme.MODES)
def test_text_tokens_meet_4_5_on_every_background(mode: str) -> None:
    for text in TEXT_TOKENS:
        for background in OPAQUE_BACKGROUNDS:
            ratio = theme.contrast(
                theme.color(text, mode), theme.color(background, mode)
            )
            assert ratio >= 4.5, (mode, text, background, ratio)


def test_contrast_known_values() -> None:
    assert theme.contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert theme.contrast("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert theme.contrast(QColor("#777777"), "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize(
    ("color", "background"),
    [
        ("#333333", "#141414"),
        ("#0000ff", "#141414"),
        ("#ffff00", "#f7f7f7"),
        ("#cccccc", "#f7f7f7"),
        ("#808080", "#808080"),
    ],
)
def test_readable_reaches_minimum(color: str, background: str) -> None:
    result = theme.readable(color, background)
    assert theme.contrast(result, background) >= 4.5


def test_readable_moves_away_from_background() -> None:
    dark_bg = theme.readable("#333333", "#141414")
    assert dark_bg.lightnessF() > QColor("#333333").lightnessF()
    light_bg = theme.readable("#cccccc", "#f7f7f7")
    assert light_bg.lightnessF() < QColor("#cccccc").lightnessF()


def test_readable_leaves_passing_colour_unchanged() -> None:
    result = theme.readable("#e0e0e0", "#141414")
    assert result.name() == "#e0e0e0"


def test_readable_with_higher_minimum() -> None:
    result = theme.readable("#3366cc", "#141414", minimum=7.0)
    assert theme.contrast(result, "#141414") >= 7.0


@pytest.mark.parametrize("mode", theme.MODES)
def test_style_sheet_has_no_unresolved_tokens(mode: str) -> None:
    sheet = theme.style_sheet(mode)
    assert "@" not in sheet
    for name in theme.TOKENS:
        assert f"@{name}" not in sheet
    assert "rgba(" in sheet


def test_style_sheet_writes_alpha_as_rgba() -> None:
    sheet = theme.style_sheet("light")
    assert "rgba(0, 0, 0, 13)" in sheet  # bg-hover, #0000000d


def test_unknown_mode_refused() -> None:
    with pytest.raises(ValueError, match="mode"):
        theme.style_sheet("sepia")


def test_apply_sets_style_palette_and_emits(qapp: QApplication, qtbot: QtBot) -> None:
    previous = theme.current_mode()
    try:
        with qtbot.waitSignal(theme.signals.changed) as blocker:
            theme.apply(qapp, "dark")
        assert blocker.args == ["dark"]
        assert theme.current_mode() == "dark"
        assert qapp.styleSheet() == theme.style_sheet("dark")
        window = qapp.palette().color(qapp.palette().ColorRole.Window)
        assert window.name() == "#191919"
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


@pytest.mark.parametrize(
    ("setting", "scheme", "expected"),
    [
        ("light", Qt.ColorScheme.Dark, "light"),
        ("dark", Qt.ColorScheme.Light, "dark"),
        ("system", Qt.ColorScheme.Dark, "dark"),
        ("system", Qt.ColorScheme.Light, "light"),
        ("system", Qt.ColorScheme.Unknown, "light"),
    ],
)
def test_resolve_mode(setting: str, scheme: Qt.ColorScheme, expected: str) -> None:
    class Hints:
        def colorScheme(self) -> Qt.ColorScheme:  # noqa: N802
            return scheme

    class App:
        def styleHints(self) -> Hints:  # noqa: N802
            return Hints()

    assert theme.resolve_mode(setting, cast(QGuiApplication, App())) == expected


def test_font_steps(qapp: QApplication) -> None:  # noqa: ARG001
    display = theme.font("display")
    assert display.pixelSize() == 28
    assert display.weight() == 500


@pytest.mark.parametrize(
    ("step", "size"), [("title", 20), ("code", 14), ("display", 28)]
)
def test_font_step_survives_apply(
    qapp: QApplication, qtbot: QtBot, step: str, size: int
) -> None:
    previous = theme.current_mode()
    try:
        theme.apply(qapp, "light")
        label = QLabel("text")
        label.setFont(theme.font(step))
        qtbot.addWidget(label)
        label.show()
        assert label.fontInfo().pixelSize() == size
        plain = QLabel("text")
        qtbot.addWidget(plain)
        plain.show()
        assert plain.fontInfo().pixelSize() == theme.TYPE["body"].size
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


@pytest.mark.parametrize("mode", theme.MODES)
@pytest.mark.parametrize("tone", ["warning", "danger", "incomplete"])
def test_status_tones_meet_text_contrast(mode: str, tone: str) -> None:
    assert (
        theme.contrast(
            theme.color(f"{tone}-text", mode), theme.color(f"{tone}-bg", mode)
        )
        >= 4.5
    )


def test_border_tokens_are_solid() -> None:
    assert theme.TOKENS["border"] == ("#e6e6e6", "#414141")
    assert theme.TOKENS["border-strong"] == ("#cccccc", "#4c4c4c")
    assert theme.TOKENS["warning-border"] == ("#e6e6e6", "#f2a600")


@pytest.mark.parametrize("mode", theme.MODES)
@pytest.mark.parametrize(
    ("tone", "background"),
    [
        ("neutral", "bg-subtle"),
        ("info", "bg-accent"),
        ("success", "success-bg"),
        ("warning", "warning-bg"),
        ("danger", "danger-bg"),
        ("incomplete", "incomplete-bg"),
    ],
)
def test_alert_fill_border_and_warning_icon(
    qapp: QApplication, qtbot: QtBot, mode: str, tone: str, background: str
) -> None:
    theme.apply(qapp, mode)
    alert = widgets.Alert(tone, "Notice", "Details")
    qtbot.addWidget(alert)
    alert.resize(320, 100)
    alert.show()
    qtbot.waitExposed(alert)
    image = alert.grab().toImage()
    border = "border"
    if tone == "warning" and mode == "dark":
        background = "bg-card"
        border = "warning-text"
    x = image.width() // 2
    assert image.pixelColor(x, 0).name() == theme.hex_value(border, mode)
    assert image.pixelColor(x, 1).name() == theme.hex_value(background, mode)
    if tone == "warning":
        mark = next(
            label
            for label in alert.findChildren(QLabel)
            if label.accessibleName() == "warning"
        )
        icon = mark.pixmap().toImage()
        assert any(
            icon.pixelColor(x, y) == theme.color("warning-text", mode)
            for x in range(icon.width())
            for y in range(icon.height())
        )


@pytest.mark.parametrize("mode", theme.MODES)
@pytest.mark.parametrize(
    ("tone", "background", "text"),
    [
        ("neutral", "bg-subtle", "text-secondary"),
        ("info", "bg-accent", "text-accent"),
        ("success", "success-bg", "success-text"),
        ("warning", "warning-bg", "warning-text"),
        ("danger", "danger-bg", "danger-text"),
        ("incomplete", "incomplete-bg", "incomplete-text"),
    ],
)
def test_badge_fill_and_text(
    qapp: QApplication,
    qtbot: QtBot,
    mode: str,
    tone: str,
    background: str,
    text: str,
) -> None:
    theme.apply(qapp, mode)
    badge = widgets.Badge("Status", tone)
    qtbot.addWidget(badge)
    badge.resize(120, 22)
    badge.show()
    qtbot.waitExposed(badge)
    image = badge.grab().toImage()
    if tone == "warning" and mode == "dark":
        background = "bg-subtle"
    assert image.pixelColor(image.width() // 2, 1).name() == theme.hex_value(
        background, mode
    )
    assert badge.palette().color(badge.foregroundRole()) == theme.color(text, mode)


@pytest.mark.parametrize("mode", theme.MODES)
def test_picker_arrow_is_chevron_in_text_secondary(mode: str) -> None:
    sheet = theme.style_sheet(mode)
    found = re.search(r"QComboBox::down-arrow \{\s*image: url\(([^)]+)\);", sheet)
    assert found is not None
    svg = Path(found.group(1)).read_text()
    assert "currentColor" not in svg
    assert theme.hex_value("text-secondary", mode) in svg
    assert "m6 9 6 6 6-6" in svg


@pytest.mark.parametrize("mode", theme.MODES)
def test_picker_scroll_bar_uses_theme_inside_list_padding(
    qapp: QApplication, qtbot: QtBot, mode: str
) -> None:
    qapp.setStyleSheet("")
    qapp.setStyle("Fusion")
    picker = widgets.Picker()
    qtbot.addWidget(picker)
    theme.apply(qapp, mode)
    for row in range(20):
        picker.add_option(str(row), row)
    picker.show()
    picker.showPopup()
    view = picker.view()
    popup = view.window()
    qtbot.waitExposed(popup)
    bar = view.verticalScrollBar()
    bounds = QRect(bar.mapTo(popup, QPoint()), bar.size())
    assert bar.isVisible()
    assert popup.rect().adjusted(9, 9, -9, -9).contains(bounds)
    assert bar.width() == 10
    image = popup.grab().toImage()
    assert image.pixelColor(bounds.center().x(), bounds.top() + 8) == theme.color(
        "border-strong", mode
    )


@pytest.mark.parametrize("mode", theme.MODES)
def test_badge_is_short_with_rounded_corners(
    qapp: QApplication, qtbot: QtBot, mode: str
) -> None:
    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        host = QWidget()
        host.setObjectName("host")
        host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        host.setStyleSheet("#host { background: #808080; }")
        qtbot.addWidget(host)
        layout = QVBoxLayout(host)
        badge = widgets.Badge("Done", "success")
        layout.addWidget(badge)
        host.resize(200, 80)
        host.show()
        qtbot.waitExposed(host)
        image = host.grab().toImage()
        box = badge.geometry()
        assert box.height() == 22
        corner = image.pixelColor(box.topLeft())
        center = image.pixelColor(box.center())
        assert corner.name() == "#808080"
        assert center.name() != "#808080"
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


@pytest.mark.parametrize("mode", theme.MODES)
def test_disabled_danger_outline_looks_disabled(
    qapp: QApplication, qtbot: QtBot, mode: str
) -> None:
    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        clear = widgets.button("Clear", "danger-outline", size="sm")
        qtbot.addWidget(clear)
        clear.resize(120, 32)
        clear.show()
        qtbot.waitExposed(clear)
        enabled = clear.grab().toImage()
        top = enabled.pixelColor(enabled.width() // 2, 0).name()
        assert top == theme.hex_value("danger-text", mode)
        clear.setEnabled(False)
        disabled = clear.grab().toImage()
        assert disabled.pixelColor(disabled.width() // 2, 0).name() == (
            theme.hex_value("border", mode)
        )
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


def test_screen_size_tokens() -> None:
    assert theme.CONTENT_MAX_WIDTH == 960
    assert theme.PICKER_MIN_WIDTH == 200
    assert theme.SIDEBAR_COLLAPSED_WIDTH == 64
