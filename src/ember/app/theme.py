"""The app's colours, sizes, type, palette and style sheet.

Colours are tokens with a light and a dark value. `apply` sets the Fusion
style, a palette and one style sheet built from the tokens of a mode, and
emits `signals.changed` so painted widgets repaint. Widgets choose style
rules through dynamic properties (`kind`, `role`, `scale`, `tone`), never
through object names. The focus ring needs `keyfocus`, which
`widgets.FocusTracker` sets only for focus that came from the keyboard.
"""

import atexit
import re
import shutil
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

MODES = ("light", "dark")
"""The two modes, in the order of each token's values."""

TOKENS: dict[str, tuple[str, str]] = {
    "bg-page": ("#ffffff", "#191919"),
    "bg-sidebar": ("#f9f9f9", "#0b0b0b"),
    "bg-card": ("#ffffff", "#1f1f1f"),
    "bg-raised": ("#ffffff", "#303030"),
    "bg-subtle": ("#f3f3f3", "#2c2c2c"),
    "bg-hover": ("#0000000d", "#ffffff13"),
    "bg-selected": ("#00000014", "#ffffff26"),
    "border": ("#e6e6e6", "#414141"),
    "border-strong": ("#cccccc", "#4c4c4c"),
    "text-primary": ("#0d0d0d", "#ededed"),
    "text-secondary": ("#5d5d5d", "#b4b4b4"),
    "text-muted": ("#6b6b6b", "#999999"),
    "text-disabled": ("#b4b4b4", "#5e5e5e"),
    "fill-primary": ("#0d0d0d", "#ffffff"),
    "fill-primary-hover": ("#303030", "#e3e3e3"),
    "on-primary": ("#ffffff", "#0d0d0d"),
    "accent": ("#257de0", "#257de0"),
    "on-accent": ("#ffffff", "#ffffff"),
    "focus-ring": ("#257de0", "#257de0"),
    "accent-hover": ("#2171ca", "#338ced"),
    "text-accent": ("#1757ac", "#6aabf1"),
    "bg-accent": ("#e7f2fc", "#02244e"),
    "control-off": ("#8f8f8f", "#6b6b6b"),
    "thumb": ("#ffffff", "#ffffff"),
    "success-text": ("#006c10", "#72d071"),
    "success-bg": ("#caecc8", "#142a13"),
    "warning-text": ("#794c02", "#f2a600"),
    "warning-bg": ("#f9dca4", "#331d00"),
    "incomplete-text": ("#7135a1", "#c994f0"),
    "incomplete-bg": ("#ecdbfa", "#311544"),
    "danger-text": ("#a42528", "#f17778"),
    "danger-bg": ("#fbd4d4", "#470f10"),
    "danger-fill": ("#ba2623", "#ba2623"),
    "danger-fill-hover": ("#cf2f2b", "#cf2f2b"),
    "on-danger": ("#ffffff", "#ffffff"),
}
"""Colour tokens as (light, dark); eight hex digits are `#rrggbbaa`.

Border tokens are solid: Qt draws a rounded border as overlapping strokes,
and a colour with transparency shows a brighter dot at each overlap.
"""

SPACE = (4, 8, 12, 16, 20, 24, 32, 48)
"""The spacing steps, in pixels."""

CONTROL_MD = 36
"""The height of a control, in pixels."""

CONTROL_SM = 32
"""The height of a small control (`scale="sm"`), in pixels."""

SIDEBAR_WIDTH = 220
"""The width of the sidebar, in pixels."""

CONTENT_MAX_WIDTH = 960
"""The maximum screen content width, excluding padding, in pixels."""

PICKER_MIN_WIDTH = 200
"""The minimum picker width, in pixels."""

SIDEBAR_COLLAPSED_WIDTH = 64
"""The collapsed sidebar width, in pixels."""

RADIUS = {"xs": 6, "sm": 10, "md": 16, "lg": 24}
"""Corner radii, in pixels. A pill's radius is half its control's height."""


@dataclass(frozen=True)
class TypeStyle:
    """One step of the type scale."""

    size: int
    """The size in pixels."""
    weight: int
    """The CSS weight, 100 to 900."""
    mono: bool = False
    """Whether the step uses the desktop's monospace font."""


TYPE = {
    "display": TypeStyle(28, 500),
    "title": TypeStyle(20, 600),
    "heading": TypeStyle(16, 600),
    "body": TypeStyle(14, 400),
    "label": TypeStyle(14, 500),
    "caption": TypeStyle(12, 400),
    "code": TypeStyle(14, 400, mono=True),
}
"""The type scale, relative to the desktop's interface and monospace fonts."""


class ThemeSignals(QObject):
    """Signals about the theme."""

    changed = Signal(str)
    """Emitted with the mode each time `apply` runs."""


signals = ThemeSignals()
"""The module's signals; painted widgets repaint on `changed`."""

_current = {"mode": "light"}


def current_mode() -> str:
    """Return the mode `apply` last set, `light` before the first call."""
    return _current["mode"]


def _mode_index(mode: str) -> int:
    if mode not in MODES:
        raise ValueError(f"unknown theme mode: {mode!r}")
    return MODES.index(mode)


def hex_value(token: str, mode: str | None = None) -> str:
    """Return a token's hex value in a mode (the current mode by default)."""
    return TOKENS[token][_mode_index(mode or current_mode())]


def _qcolor(value: str) -> QColor:
    digits = value.lstrip("#")
    color = QColor("#" + digits[:6])
    if len(digits) == 8:
        color.setAlpha(int(digits[6:], 16))
    return color


def color(token: str, mode: str | None = None) -> QColor:
    """Return a token as a `QColor` in a mode (the current mode by default)."""
    return _qcolor(hex_value(token, mode))


def _css(value: str) -> str:
    digits = value.lstrip("#")
    if len(digits) == 8:
        r, g, b, a = (int(digits[i : i + 2], 16) for i in (0, 2, 4, 6))
        return f"rgba({r}, {g}, {b}, {a})"
    return "#" + digits


def font(step: str) -> QFont:
    """Return the font for a step of the type scale.

    Args:
        step: A key of `TYPE`.

    Returns:
        The desktop's interface font, or its monospace font for `code` and
        `term`, at the step's pixel size and weight.
    """
    style = TYPE[step]
    if style.mono:
        result = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    else:
        result = QFont(QGuiApplication.font())
    result.setPixelSize(style.size)
    result.setWeight(QFont.Weight(style.weight))
    return result


def resolve_mode(setting: str, app: QGuiApplication) -> str:
    """Return the mode for the `app.theme` setting.

    Args:
        setting: `system`, `light` or `dark`.
        app: The application, whose style hints give the desktop's scheme.

    Returns:
        `light` or `dark`. `system` follows the desktop; an unknown scheme
        counts as light.
    """
    if setting in MODES:
        return setting
    scheme = app.styleHints().colorScheme()
    return "dark" if scheme == Qt.ColorScheme.Dark else "light"


_STYLE = """
QWidget {
    color: @text-primary;
}
QMainWindow, QDialog {
    background: @bg-page;
}
QToolTip {
    background: @fill-primary;
    color: @on-primary;
    border: 1px solid @fill-primary;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
    font-weight: 400;
}
QFrame[role="card"] {
    background: @bg-card;
    border: 1px solid @border;
    border-radius: 16px;
}
QFrame[role="sidebar"] {
    background: @bg-sidebar;
    border: none;
    border-right: 1px solid @border;
}
QFrame[role="update"] {
    background: @bg-accent;
    border: none;
    border-radius: 10px;
}
QFrame[role="update"] QLabel {
    color: @text-accent;
}
QLabel {
    background: transparent;
    border: none;
}
QLabel[role="muted"] {
    color: @text-muted;
}
QLabel[role="secondary"] {
    color: @text-secondary;
}
QLabel[role="badge"] {
    border-radius: 6px;
    min-height: 22px;
    max-height: 22px;
    padding: 0 8px;
    font-size: 12px;
    font-weight: 500;
    background: @bg-subtle;
    color: @text-secondary;
}
QLabel[role="badge"][tone="success"] {
    background: @success-bg;
    color: @success-text;
}
QLabel[role="badge"][tone="warning"] {
    background: @warning-bg;
    color: @warning-text;
}
QLabel[role="badge"][tone="danger"] {
    background: @danger-bg;
    color: @danger-text;
}
QLabel[role="badge"][tone="incomplete"] {
    background: @incomplete-bg;
    color: @incomplete-text;
}
QLabel[role="badge"][tone="info"] {
    background: @bg-accent;
    color: @text-accent;
}
QFrame[role="alert"] {
    border-radius: 16px;
    border: 1px solid @border;
    background: @bg-subtle;
}
QFrame[role="alert"][tone="info"] {
    background: @bg-accent;
}
QFrame[role="alert"][tone="success"] {
    background: @success-bg;
}
QFrame[role="alert"][tone="warning"] {
    background: @warning-bg;
}
QFrame[role="alert"][tone="danger"] {
    background: @danger-bg;
}
QFrame[role="alert"][tone="incomplete"] {
    background: @incomplete-bg;
}
QFrame[role="command"] {
    background: @bg-subtle;
    border: none;
    border-radius: 10px;
}
QLabel[role="command"] {
    padding: 8px 12px;
}
QPushButton {
    min-height: 34px;
    max-height: 34px;
    padding: 0 16px;
    border-radius: 18px;
    border: 1px solid @border-strong;
    background: transparent;
    color: @text-primary;
    font-size: 14px;
    font-weight: 500;
}
QPushButton:hover {
    background: @bg-hover;
}
QPushButton:pressed {
    background: @bg-selected;
}
QPushButton:disabled {
    color: @text-disabled;
    border-color: @border;
}
QPushButton:focus {
    outline: none;
}
QPushButton[keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
    min-height: 32px;
    max-height: 32px;
    padding: 0 15px;
}
QPushButton[kind="primary"] {
    background: @fill-primary;
    color: @on-primary;
    border: 1px solid @fill-primary;
}
QPushButton[kind="primary"]:hover {
    background: @fill-primary-hover;
    border-color: @fill-primary-hover;
}
QPushButton[kind="primary"][keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
}
QPushButton[kind="ghost"] {
    border: 1px solid transparent;
    background: transparent;
}
QPushButton[role="reset"] {
    color: @text-secondary;
}
QPushButton[kind="ghost"]:hover {
    background: @bg-hover;
}
QPushButton[kind="ghost"][keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
}
QPushButton[kind="danger"] {
    background: @danger-fill;
    color: @on-danger;
    border: 1px solid @danger-fill;
}
QPushButton[kind="danger"]:hover {
    background: @danger-fill-hover;
    border-color: @danger-fill-hover;
}
QPushButton[kind="danger"][keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
}
QPushButton[kind="danger-outline"] {
    background: transparent;
    color: @danger-text;
    border: 1px solid @danger-text;
}
QPushButton[kind="danger-outline"]:hover {
    background: @danger-bg;
}
QPushButton[kind="danger-outline"]:disabled {
    color: @text-disabled;
    border-color: @border;
    background: transparent;
}
QPushButton[kind="danger-outline"][keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
}
QPushButton[scale="sm"] {
    min-height: 30px;
    max-height: 30px;
    padding: 0 12px;
    border-radius: 16px;
}
QPushButton[scale="sm"][keyfocus="true"]:focus {
    min-height: 28px;
    max-height: 28px;
    padding: 0 11px;
}
QPushButton[role="info"] {
    border: 1px solid transparent;
    background: transparent;
    border-radius: 6px;
    padding: 0;
    min-height: 20px;
    max-height: 20px;
    min-width: 20px;
    max-width: 20px;
}
QPushButton[role="info"]:hover {
    background: @bg-hover;
}
QPushButton[role="info"][keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
    padding: 0;
    min-height: 20px;
    max-height: 20px;
}
QWidget[role="segmented"] {
    background: @bg-subtle;
    border-radius: 18px;
}
QPushButton[role="segment"] {
    border: 1px solid transparent;
    background: transparent;
    color: @text-secondary;
}
QPushButton[role="segment"]:hover {
    background: @bg-hover;
}
QPushButton[role="segment"]:checked {
    background: @bg-card;
    color: @text-primary;
    border: 1px solid @border-strong;
}
QPushButton[role="segment"][changed="true"]:checked {
    border: 1px solid @accent;
}
QPushButton[role="segment"][keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
}
QLineEdit, QComboBox {
    min-height: 34px;
    max-height: 34px;
    padding: 0 14px;
    border-radius: 18px;
    border: 1px solid @border-strong;
    background: @bg-raised;
    color: @text-primary;
    font-size: 14px;
    selection-background-color: @accent;
    selection-color: #ffffff;
}
QComboBox[changed="true"] {
    border: 1px solid @accent;
}
QLineEdit[keyfocus="true"]:focus, QComboBox[keyfocus="true"]:focus {
    border: 2px solid @focus-ring;
    min-height: 32px;
    max-height: 32px;
    padding: 0 13px;
}
QLineEdit:disabled, QComboBox:disabled {
    color: @text-disabled;
}
QComboBox[scale="sm"], QLineEdit[scale="sm"] {
    min-height: 30px;
    max-height: 30px;
    border-radius: 16px;
}
QComboBox[scale="sm"][keyfocus="true"]:focus,
QLineEdit[scale="sm"][keyfocus="true"]:focus {
    min-height: 28px;
    max-height: 28px;
}
QComboBox::drop-down {
    border: none;
    width: 28px;
}
QComboBox::down-arrow {
    image: url(@@arrow-down);
    width: 16px;
    height: 16px;
}
QComboBox QAbstractItemView {
    background: @bg-raised;
    color: @text-primary;
    border: 1px solid @border-strong;
    border-radius: 16px;
    padding: 8px;
    outline: none;
    selection-background-color: @bg-selected;
    selection-color: @text-primary;
}
QMenu {
    background: @bg-raised;
    color: @text-primary;
    border: 1px solid @border-strong;
    border-radius: 16px;
    padding: 8px;
}
QMenu::item {
    padding: 6px 12px;
    border-radius: 10px;
}
QMenu::item:selected {
    background: @bg-hover;
}
QMenu::item:disabled {
    color: @text-muted;
}
QMenu::separator {
    height: 1px;
    background: @border;
    margin: 4px 0;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background: transparent;
    border: none;
    width: 10px;
    height: 10px;
}
QScrollBar::handle {
    background: @border-strong;
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}
"""

_TOKEN_REF = re.compile(r"@([a-z][a-z0-9-]*)")
_ICON_FILES: dict[tuple[str, str], Path] = {}


def _icon_file(name: str, value: str) -> Path:
    """Return a Lucide icon from `assets/icons/` drawn in a colour, as a file.

    Style sheet images are read from files, so each icon and colour is
    written once to a folder of the process that is removed at exit.
    """
    key = (name, value)
    if key not in _ICON_FILES:
        if not _ICON_FILES:
            folder = Path(tempfile.mkdtemp(prefix="ember-theme-"))
            atexit.register(shutil.rmtree, folder, ignore_errors=True)
        else:
            folder = next(iter(_ICON_FILES.values())).parent
        svg = files("ember.app").joinpath("assets", "icons", f"{name}.svg")
        path = folder / f"{name}-{value.lstrip('#')}.svg"
        path.write_text(svg.read_text().replace("currentColor", _css(value)))
        _ICON_FILES[key] = path
    return _ICON_FILES[key]


def style_sheet(mode: str) -> str:
    """Return the app's style sheet for a mode, every token resolved.

    The picker arrow is the `chevron-down` icon in `text-secondary`.

    Raises:
        KeyError: The template names a token that does not exist.
    """
    index = _mode_index(mode)
    arrow = _icon_file("chevron-down", TOKENS["text-secondary"][index])
    sheet = _STYLE.replace("@@arrow-down", arrow.as_posix())
    return _TOKEN_REF.sub(lambda m: _css(TOKENS[m.group(1)][index]), sheet)


def palette(mode: str) -> QPalette:
    """Return a `QPalette` built from the tokens of a mode."""
    result = QPalette()
    roles = {
        QPalette.ColorRole.Window: "bg-page",
        QPalette.ColorRole.WindowText: "text-primary",
        QPalette.ColorRole.Base: "bg-card",
        QPalette.ColorRole.AlternateBase: "bg-subtle",
        QPalette.ColorRole.ToolTipBase: "bg-raised",
        QPalette.ColorRole.ToolTipText: "text-primary",
        QPalette.ColorRole.PlaceholderText: "text-muted",
        QPalette.ColorRole.Text: "text-primary",
        QPalette.ColorRole.Button: "bg-card",
        QPalette.ColorRole.ButtonText: "text-primary",
        QPalette.ColorRole.BrightText: "on-primary",
        QPalette.ColorRole.Highlight: "accent",
        QPalette.ColorRole.Link: "text-accent",
        QPalette.ColorRole.LinkVisited: "text-accent",
    }
    for role, token in roles.items():
        result.setColor(role, color(token, mode))
    result.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    disabled = QPalette.ColorGroup.Disabled
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        result.setColor(disabled, role, color("text-disabled", mode))
    return result


def apply(app: QApplication, mode: str) -> None:
    """Apply a mode to the application and emit `signals.changed`.

    The application font takes the `body` size, so a widget without its own
    font shows body text and a widget given `font(step)` keeps that step.

    Args:
        app: The running application.
        mode: `light` or `dark`.
    """
    _mode_index(mode)
    _current["mode"] = mode
    app.setStyle("Fusion")
    app.setPalette(palette(mode))
    body = QFont(app.font())
    body.setPixelSize(TYPE["body"].size)
    app.setFont(body)
    app.setStyleSheet(style_sheet(mode))
    signals.changed.emit(mode)


def _as_color(value: QColor | str) -> QColor:
    return QColor(value) if isinstance(value, QColor) else _qcolor(value)


def _luminance(value: QColor) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(value.redF())
        + 0.7152 * channel(value.greenF())
        + 0.0722 * channel(value.blueF())
    )


def contrast(a: QColor | str, b: QColor | str) -> float:
    """Return the WCAG 2.x contrast ratio of two colours, alpha ignored.

    Returns:
        A ratio from 1.0 to 21.0.
    """
    first, second = _luminance(_as_color(a)), _luminance(_as_color(b))
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def readable(
    color: QColor | str, background: QColor | str, minimum: float = 4.5
) -> QColor:
    """Return a colour that reads on a background.

    The colour is returned unchanged when it meets `minimum`. Otherwise its
    HSL lightness moves away from the background in steps of 1 % until it
    does; when that end of the range is reached first, it moves the other way.

    Args:
        color: The colour a worker set.
        background: The background it is drawn on.
        minimum: The contrast ratio to reach.

    Returns:
        The colour, or its nearest lighter or darker version that reaches
        `minimum`.
    """
    original = _as_color(color)
    ground = _as_color(background)
    if contrast(original, ground) >= minimum:
        return QColor(original)
    hue = max(original.hslHueF(), 0.0)
    saturation = original.hslSaturationF()
    start = round(original.lightnessF() * 100)
    lighter_first = _luminance(original) >= _luminance(ground)
    candidate = QColor(original)
    for step in (1, -1) if lighter_first else (-1, 1):
        level = start
        while 0 <= level + step <= 100:
            level += step
            candidate = QColor.fromHslF(hue, saturation, level / 100)
            if contrast(candidate, ground) >= minimum:
                return candidate
    return candidate
