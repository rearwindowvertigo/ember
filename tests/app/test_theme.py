import re
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import Qt
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
