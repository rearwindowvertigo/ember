"""What the screens share: text formats and the widgets every screen builds.

Times read "Today 10:42", "Yesterday 17:03" or "30 Oct 14:05", in local
time with English month abbreviations whatever the locale. Durations read
"18 min", "1 h 5 min" or "2 h".
"""

import datetime
import time
from collections.abc import Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ember.app import theme, tray, widgets

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
"""The month abbreviations times use."""

SCREEN_MARGIN = 24
"""The padding around a screen's content, in pixels."""

SCREEN_SPACING = 16
"""The gap between a screen's sections, in pixels."""

CARD_MARGIN = 20
"""The padding inside a card, in pixels."""


# Text


def when(stamp: float, now: float | None = None) -> str:
    """Return a local time as the screens show it.

    Args:
        stamp: Seconds since the epoch.
        now: The current time in seconds since the epoch; `None` reads the
            clock.

    Returns:
        "Today 10:42", "Yesterday 17:03" or "30 Oct 14:05".
    """
    moment = datetime.datetime.fromtimestamp(stamp)
    today = datetime.datetime.fromtimestamp(time.time() if now is None else now).date()
    clock = f"{moment.hour:02d}:{moment.minute:02d}"
    if moment.date() == today:
        return f"Today {clock}"
    if moment.date() == today - datetime.timedelta(days=1):
        return f"Yesterday {clock}"
    return f"{moment.day} {MONTHS[moment.month - 1]} {clock}"


def inline(text: str) -> str:
    """Return a `when` result for use inside a sentence: "today 10:42"."""
    if text.startswith(("Today", "Yesterday")):
        return text[0].lower() + text[1:]
    return text


def sentence(text: str) -> str:
    """Return text with its first letter in upper case."""
    return text[:1].upper() + text[1:]


def duration(seconds: float) -> str:
    """Return a duration in whole minutes: "18 min", "1 h 5 min", "2 h".

    Args:
        seconds: The duration; anything under two minutes counts as one.

    Returns:
        At least "1 min".
    """
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def plural(count: int, one: str, many: str | None = None) -> str:
    """Return a count with its noun: "1 turn", "2 turns".

    Args:
        count: The number.
        one: The noun for one.
        many: The noun for any other number; `None` adds "s" to `one`.

    Returns:
        "<count> <noun>".
    """
    if count == 1:
        return f"{count} {one}"
    return f"{count} {one + 's' if many is None else many}"


def engine_name(engine: str) -> str:
    """Return an engine's shown name, or its key when it has none."""
    return tray.ENGINE_LABELS.get(engine, engine)


# Widgets


def label(text: str = "", role: str | None = None, *, wrap: bool = True) -> QLabel:
    """Return a label, word-wrapped by default.

    Args:
        text: Its text.
        role: The style role, such as `secondary` or `muted`; `None` for
            body text.
        wrap: Wrap long lines.

    Returns:
        The label, without a parent.
    """
    result = QLabel(text)
    result.setWordWrap(wrap)
    if role is not None:
        result.setProperty("role", role)
    return result


class ToneLine(QLabel):
    """One line of text in a tone's colour, following the theme."""

    def __init__(self, tone: str = "warning", parent: QWidget | None = None) -> None:
        """Create the line, empty and hidden.

        Args:
            tone: One of `widgets.TONES`.
            parent: The parent widget.
        """
        super().__init__(parent)
        self._tone = tone
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setVisible(False)
        self._draw()
        theme.signals.changed.connect(self._draw)

    def _draw(self, *_args: object) -> None:
        self.setStyleSheet(f"color: {widgets.tone_color(self._tone).name()};")

    def show_text(self, text: str | None) -> None:
        """Show a line, or hide the line for `None` or empty text."""
        self.setText(text or "")
        self.setAccessibleName(text or "")
        self.setVisible(bool(text))


def fitting_columns(
    tile: int, area: QScrollArea, most: int, gap: int, margins: int = 0
) -> int:
    """Return the most columns, up to `most`, whose tiles fit beside the scroll bar.

    Args:
        tile: The narrowest a tile can be, such as its `minimumSizeHint`.
        area: The scroll area holding the tiles.
        most: The most columns.
        gap: The space between columns.
        margins: The left and right margins, together, around the tiles
            inside the scroll area.

    Returns:
        A column count from 1 to `most`.
    """
    width = (
        area.contentsRect().width()
        - area.verticalScrollBar().sizeHint().width()
        - margins
    )
    for count in range(most, 1, -1):
        if count * tile + (count - 1) * gap <= width:
            return count
    return 1


def scroll_area() -> QScrollArea:
    """Return a frameless scroll area that resizes its widget to fit."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    return area


def scrolled(screen: QWidget) -> QVBoxLayout:
    """Give a screen a scrolling content area with the screen padding.

    Args:
        screen: The screen widget.

    Returns:
        The content layout, with 24 px padding and 16 px gaps.
    """
    outer = QVBoxLayout(screen)
    outer.setContentsMargins(0, 0, 0, 0)
    area = scroll_area()
    content = QWidget()
    area.setWidget(content)
    outer.addWidget(area)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(
        SCREEN_MARGIN, SCREEN_MARGIN, SCREEN_MARGIN, SCREEN_MARGIN
    )
    layout.setSpacing(SCREEN_SPACING)
    return layout


def screen_header(
    layout: QBoxLayout,
    title: str,
    subtitle: str = "",
    trailing: Iterable[QWidget] = (),
) -> QLabel:
    """Add a screen's header: its title, a subtitle, and widgets on the right.

    Args:
        layout: The screen's content layout.
        title: The screen's name, in the display type.
        subtitle: The line under it.
        trailing: Widgets right of the titles, such as a Check now button,
            centred on the titles.

    Returns:
        The subtitle label, for screens that change it.
    """
    row = QHBoxLayout()
    row.setSpacing(12)
    titles = QVBoxLayout()
    titles.setSpacing(4)
    heading = QLabel(title)
    heading.setFont(theme.font("display"))
    titles.addWidget(heading)
    line = label(subtitle, "secondary")
    titles.addWidget(line)
    row.addLayout(titles, 1)
    for widget in trailing:
        row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addLayout(row)
    return line


CONTROL_COLUMN = 280
"""The narrowest control column; a wider control widens it."""


class TooltipRow(QWidget):
    """A setting's name with an info icon, and its control on the right.

    The name stands alone; the icon's tooltip holds the description, or the
    description and what to do. The control sits in `slot`, which
    `align_controls` sizes so a screen's controls share one column.
    """

    def __init__(self, name: str, tooltip: str, control: QWidget) -> None:
        """Lay out the row.

        Args:
            name: The setting's name.
            tooltip: What the setting does, shown by the info icon.
            control: The picker, segmented control, switch or button.
        """
        super().__init__()
        self.control = control
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        self.heading = QLabel(name)
        self.heading.setFont(theme.font("label"))
        self.info = widgets.InfoTip(name, tooltip)
        row.addWidget(self.heading)
        row.addWidget(self.info, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        self.slot = QWidget()
        slot = QHBoxLayout(self.slot)
        slot.setContentsMargins(0, 0, 0, 0)
        if not isinstance(control, QComboBox):
            slot.addStretch(1)
        slot.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.slot, 0, Qt.AlignmentFlag.AlignVCenter)
        if control.focusPolicy() != Qt.FocusPolicy.NoFocus:
            # The control was made first; Tab reaches the icon before it.
            QWidget.setTabOrder(self.info, control)


def align_controls(rows: Iterable[TooltipRow]) -> int:
    """Give every row's control the same column, as wide as the widest control.

    Pickers fill the column; segmented controls, switches and buttons sit at
    its right edge.

    Returns:
        The column's width.
    """
    rows = list(rows)
    width = max([CONTROL_COLUMN, *(row.control.sizeHint().width() for row in rows)])
    for row in rows:
        row.slot.setFixedWidth(width)
    return width


def titled_card(
    title: str, tooltip: str | None = None
) -> tuple[widgets.Card, QVBoxLayout]:
    """Return a card with a heading, and the layout under the heading.

    Args:
        title: The heading.
        tooltip: What the card holds, shown by an info icon after the
            heading, or `None` for no icon.

    Returns:
        `(card, layout)`; the layout has 20 px padding and 16 px gaps.
    """
    card = widgets.Card()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(CARD_MARGIN, CARD_MARGIN, CARD_MARGIN, CARD_MARGIN)
    layout.setSpacing(SCREEN_SPACING)
    heading = QLabel(title)
    heading.setFont(theme.font("heading"))
    if tooltip is None:
        layout.addWidget(heading)
        return card, layout
    line = QHBoxLayout()
    line.setSpacing(4)
    line.addWidget(heading)
    info = widgets.InfoTip(title, tooltip)
    line.addWidget(info, 0, Qt.AlignmentFlag.AlignVCenter)
    line.addStretch(1)
    layout.addLayout(line)
    return card, layout


def discard(widget: QWidget | None, layout: QLayout | None = None) -> None:
    """Take a widget out of its layout, hide it and delete it later.

    Args:
        widget: The widget; `None` does nothing.
        layout: The layout that holds it, or `None`.
    """
    if widget is None:
        return
    if layout is not None:
        layout.removeWidget(widget)
    widget.hide()
    widget.deleteLater()


def clear_layout(layout: QLayout) -> None:
    """Remove every item of a layout, deleting its widgets and inner layouts."""
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            break
        discard(item.widget())
        child = item.layout()
        if child is not None:
            clear_layout(child)


def chain_tab_order(order: Sequence[QWidget]) -> None:
    """Make Tab move through widgets in the order given."""
    for first, second in zip(order, order[1:], strict=False):
        QWidget.setTabOrder(first, second)
