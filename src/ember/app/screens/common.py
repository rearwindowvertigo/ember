"""What the screens share: text formats and the widgets every screen builds.

Times read "Today 10:42", "Yesterday 17:03" or "30 Oct 14:05", in local
time with English month abbreviations whatever the locale. Durations read
"18 min", "1 h 5 min" or "2 h".
"""

import datetime
import time
from collections.abc import Iterable, Sequence

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QSizePolicy,
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
    width = min(width, theme.CONTENT_MAX_WIDTH)
    for count in range(most, 1, -1):
        if count * tile + (count - 1) * gap <= width:
            return count
    return 1


def scroll_area() -> QScrollArea:
    """Return a frameless scroll area that resizes its widget to fit."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
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
    outer.addWidget(area)
    return scroll_column(area)


def scroll_column(area: QScrollArea) -> QVBoxLayout:
    """Install a centred content column, capped at the content width plus padding."""
    content = QWidget()
    content.setMaximumWidth(theme.CONTENT_MAX_WIDTH + 2 * SCREEN_MARGIN)
    area.setWidget(content)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(
        SCREEN_MARGIN, SCREEN_MARGIN, SCREEN_MARGIN, SCREEN_MARGIN
    )
    layout.setSpacing(SCREEN_SPACING)
    return layout


class FittingRow(QWidget):
    """A row of controls that moves into a column when their minimums do not fit."""

    def __init__(self, controls: Iterable[QWidget]) -> None:
        """Keep each control whole, with screen spacing between them."""
        super().__init__()
        self.controls = list(controls)
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(SCREEN_SPACING)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for control in self.controls:
            self.box.addWidget(control)

    def _fit(self) -> None:
        controls = [control for control in self.controls if not control.isHidden()]
        needed = sum(
            max(control.minimumWidth(), control.minimumSizeHint().width())
            for control in controls
        ) + self.box.spacing() * max(0, len(controls) - 1)
        self.box.setDirection(
            QBoxLayout.Direction.TopToBottom
            if needed > self.width()
            else QBoxLayout.Direction.LeftToRight
        )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        """Fit the controls to the row's available width."""
        self._fit()
        super().resizeEvent(event)

    def event(self, event: QEvent) -> bool:
        """Refit when a control changes its minimum size or visibility."""
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit()
        return super().event(event)


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
"""The preferred control column width; a wider control widens it."""


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
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.stacked = False
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(16)
        self.name = QWidget()
        names = QHBoxLayout(self.name)
        names.setContentsMargins(0, 0, 0, 0)
        names.setSpacing(16)
        self.heading = QLabel(name)
        self.heading.setFont(theme.font("label"))
        self.info = widgets.InfoTip(name, tooltip)
        names.addWidget(self.heading)
        names.addWidget(self.info, 0, Qt.AlignmentFlag.AlignVCenter)
        names.addStretch(1)
        self.box.addWidget(self.name, 1)
        self.slot = QWidget()
        slot = QHBoxLayout(self.slot)
        slot.setContentsMargins(0, 0, 0, 0)
        slot.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        if isinstance(control, QComboBox):
            control.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
        else:
            control.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            slot.setAlignment(control, Qt.AlignmentFlag.AlignRight)
        self.box.addWidget(self.slot, 0, Qt.AlignmentFlag.AlignVCenter)
        if control.focusPolicy() != Qt.FocusPolicy.NoFocus:
            QWidget.setTabOrder(self.info, control)

    def fit(self, width: int, stacked: bool) -> None:
        """Set the common column width and put the control beside or below its name."""
        self.stacked = stacked
        self.box.setDirection(
            QBoxLayout.Direction.TopToBottom
            if stacked
            else QBoxLayout.Direction.LeftToRight
        )
        self.box.setSpacing(theme.SPACE[1] if stacked else 16)
        self.box.setStretch(0, 0 if stacked else 1)
        self.box.setAlignment(
            self.slot,
            Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignVCenter,
        )
        slot = self.slot.layout()
        assert slot is not None
        if not isinstance(self.control, QComboBox):
            slot.setAlignment(
                self.control,
                Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignRight,
            )
        self.slot.setFixedWidth(width)


class _ControlFit(QObject):
    """Keep one screen's visible setting rows in a shared responsive layout."""

    def __init__(self, parent: QWidget, area: QScrollArea | None) -> None:
        super().__init__(parent)
        self.area = area
        self.rows: list[TooltipRow] = []
        self.last_width = CONTROL_COLUMN
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fit)
        parent.installEventFilter(self)

    def replace(self, rows: list[TooltipRow]) -> int:
        for row in self.rows:
            row.removeEventFilter(self)
        self.rows = rows
        for row in rows:
            row.installEventFilter(self)
        width = max([CONTROL_COLUMN, *(r.control.sizeHint().width() for r in rows)])
        if not any(r.isVisible() for r in rows):
            self.last_width = width
            for row in rows:
                row.fit(width, False)
        self.timer.start(0)
        return width

    def fit(self) -> None:
        rows = [row for row in self.rows if row.isVisible()]
        if not rows:
            return
        preferred = max(
            CONTROL_COLUMN, *(row.control.sizeHint().width() for row in rows)
        )
        floor = max(
            [
                theme.PICKER_MIN_WIDTH,
                *(
                    row.control.sizeHint().width()
                    for row in rows
                    if not isinstance(row.control, QComboBox)
                ),
            ]
        )
        correction = 0
        if self.area is not None:
            column = self.area.widget()
            assert column is not None
            target = min(
                theme.CONTENT_MAX_WIDTH + 2 * SCREEN_MARGIN,
                self.area.contentsRect().width()
                - self.area.verticalScrollBar().sizeHint().width(),
            )
            correction = max(0, column.width() - target)
        available = min(
            row.width() - correction - row.name.minimumSizeHint().width() - 16
            for row in rows
        )
        stacked = available < floor
        if not stacked:
            self.last_width = min(preferred, available)
        for row in self.rows:
            width = (
                min(
                    max(theme.PICKER_MIN_WIDTH, self.last_width),
                    row.width() - correction,
                )
                if stacked and row.isVisible()
                else self.last_width
            )
            row.fit(width, stacked)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Refit after viewport resizing or a row's visibility changes."""
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
            self.timer.start(0)
        return super().eventFilter(watched, event)


def align_controls(rows: Iterable[TooltipRow]) -> int:
    """Replace a screen's aligned rows and fit them whenever its viewport resizes.

    Returns:
        The preferred column width, before fitting the visible rows.
    """
    rows = list(rows)
    if not rows:
        return CONTROL_COLUMN
    parent = rows[0].parentWidget()
    area = None
    while parent is not None:
        if isinstance(parent, QScrollArea):
            area = parent
            break
        parent = parent.parentWidget()
    host = area.viewport() if area is not None else rows[0].window()
    controller = host.findChild(
        _ControlFit, options=Qt.FindChildOption.FindDirectChildrenOnly
    )
    if controller is None:
        controller = _ControlFit(host, area)
    return controller.replace(rows)


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
