"""The app's shared widgets.

Painted widgets (`Switch`, `Spinner`) read their colours from `theme` and
repaint on `theme.signals.changed`. The other widgets are styled by the
theme's style sheet through dynamic properties.
"""

import html
import math
from collections.abc import Callable, Sequence
from typing import cast

import shiboken6
from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QClipboard,
    QColor,
    QContextMenuEvent,
    QFocusEvent,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QHideEvent,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QShowEvent,
    QStandardItem,
    QStandardItemModel,
    QTextCharFormat,
    QTextDocument,
    QTextLayout,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from ember.app import icons, theme

TONES = ("neutral", "success", "warning", "danger", "info", "incomplete")
"""The tones of badges and alerts."""

_TONE_TEXT = {
    "neutral": "text-secondary",
    "success": "success-text",
    "warning": "warning-text",
    "danger": "danger-text",
    "info": "text-accent",
    "incomplete": "incomplete-text",
}

_TONE_ICON = {
    "neutral": "info",
    "success": "circle-check",
    "warning": "triangle-alert",
    "danger": "circle-alert",
    "info": "info",
    "incomplete": "circle-alert",
}

LOADING_DELAY_MS = 300
"""How long a read runs before its loading state appears (design J10)."""


def _check_tone(tone: str) -> str:
    if tone not in TONES:
        raise ValueError(f"unknown tone: {tone!r}")
    return tone


def repolish(widget: QWidget) -> None:
    """Apply style rules again after a dynamic property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


KEYBOARD_REASONS = (
    Qt.FocusReason.TabFocusReason,
    Qt.FocusReason.BacktabFocusReason,
    Qt.FocusReason.ShortcutFocusReason,
)
"""Focus reasons that always show the focus ring."""


def focus_visible(widget: QWidget) -> bool:
    """Return whether a widget has focus that arrived from the keyboard."""
    return widget.hasFocus() and widget.property("keyfocus") is True


class FocusTracker(QObject):
    """Mark keyboard focus with the `keyfocus` property for the style sheet.

    Focus taken by Tab, Shift+Tab or a shortcut sets `keyfocus` to true;
    focus taken by a mouse click sets it to false. Focus that the app moves,
    or that returns with the window, follows the last input: a key press or a
    mouse press. Losing focus clears the property.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        """Create the tracker; the last input counts as the mouse."""
        super().__init__(parent)
        self.keyboard = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: D102
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            self.keyboard = True
        elif kind == QEvent.Type.MouseButtonPress:
            self.keyboard = False
        elif kind == QEvent.Type.FocusIn and isinstance(watched, QWidget):
            reason = cast(QFocusEvent, event).reason()
            if reason in KEYBOARD_REASONS:
                visible = True
            elif reason == Qt.FocusReason.MouseFocusReason:
                visible = False
            else:
                visible = self.keyboard
            _set_keyfocus(watched, visible)
        elif kind == QEvent.Type.FocusOut and isinstance(watched, QWidget):
            _set_keyfocus(watched, False)
        return False


def _set_keyfocus(widget: QWidget, visible: bool) -> None:
    if widget.property("keyfocus") is not visible:
        widget.setProperty("keyfocus", visible)
        repolish(widget)


def install_focus_tracking(app: QApplication) -> FocusTracker:
    """Install one `FocusTracker` on the application and return it."""
    tracker = app.findChild(FocusTracker)
    if tracker is None:
        tracker = FocusTracker(app)
        app.installEventFilter(tracker)
    return tracker


TIP_WIDTH = 320
"""The width in pixels a tooltip wraps at when its text is wider."""

PATH_BREAK = "\u200b"
"""A zero-width space put after each `/`, so a long path wraps there."""


def tip_text(text: str) -> str:
    """Return a tooltip's text, ready to wrap when it is wider than `TIP_WIDTH`.

    Text that fits in `TIP_WIDTH` on every line is returned unchanged. Wider
    text becomes rich text with its line breaks kept, a break allowed after
    each `/`, and breaks inside any word wider than `TIP_WIDTH`.

    Args:
        text: The tooltip's plain text.

    Returns:
        The text Qt shows as the tooltip.
    """
    metrics = QFontMetrics(theme.font("caption"))
    lines = text.split("\n")
    if all(metrics.horizontalAdvance(line) <= TIP_WIDTH for line in lines):
        return text
    body = "<br>".join(
        " ".join(_breakable(word, metrics) for word in line.split(" "))
        for line in lines
    )
    return f"<p>{body}</p>"


def _breakable(word: str, metrics: QFontMetrics) -> str:
    """Escape a word, allowing a break after `/`, or anywhere it is too wide."""
    parts = [part + "/" for part in word.split("/")]
    parts[-1] = parts[-1][:-1]
    pieces: list[str] = []
    for part in parts:
        while metrics.horizontalAdvance(part) > TIP_WIDTH:
            cut = len(part) - 1
            while cut > 1 and metrics.horizontalAdvance(part[:cut]) > TIP_WIDTH:
                cut -= 1
            pieces.append(part[:cut])
            part = part[cut:]
        pieces.append(part)
    return PATH_BREAK.join(html.escape(piece, quote=False) for piece in pieces if piece)


class TipPopup(QWidget):
    """Ember's tooltip window: `fill-primary` with `on-primary`, rounded corners.

    Info icons and opted-in icon-only controls use this window. Qt's own
    tooltip window is suppressed. Text wider than `TIP_WIDTH` wraps at that width.
    """

    RADIUS = 6
    PADDING = (8, 4)

    def __init__(self) -> None:
        """Create the hidden window."""
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.PADDING, *self.PADDING)
        self.label = QLabel()
        self.label.setFont(theme.font("caption"))
        layout.addWidget(self.label)
        self.owner: QWidget | None = None

    def show_for(self, owner: QWidget, text: str) -> None:
        """Show `text` under `owner`, wrapped at `TIP_WIDTH` when it is wider."""
        self.owner = owner
        shown = tip_text(text)
        wide = shown != text
        self.label.setTextFormat(
            Qt.TextFormat.RichText if wide else Qt.TextFormat.PlainText
        )
        self.label.setWordWrap(wide)
        if wide:
            self.label.setFixedWidth(TIP_WIDTH)
        else:
            self.label.setMinimumWidth(0)
            self.label.setMaximumWidth(16777215)  # QWIDGETSIZE_MAX
        self.label.setText(shown)
        color = theme.color("on-primary").name()
        self.label.setStyleSheet(f"color: {color}; background: transparent;")
        self.adjustSize()
        self.move(self._place(owner))
        self.show()
        self.raise_()

    def _place(self, owner: QWidget) -> QPoint:
        """Return a top-left under `owner`, kept on its screen.

        Near the bottom of the screen the window opens above `owner`; near
        the right edge it moves left.
        """
        below = owner.mapToGlobal(QPoint(0, owner.height() + 4))
        screen = owner.screen()
        if screen is None:
            return below
        area = screen.availableGeometry()
        x = max(area.left(), min(below.x(), area.right() - self.width()))
        y = below.y()
        if y + self.height() > area.bottom():
            y = owner.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4
        return QPoint(x, max(area.top(), y))

    def hide_for(self, owner: QWidget) -> None:
        """Hide the window when `owner` is the widget it shows for."""
        if self.owner is owner:
            self.hide()
            self.owner = None

    def text(self) -> str:
        """Return the shown text as plain text."""
        return self.label.text()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: D102, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.color("fill-primary"))
        painter.drawRoundedRect(QRectF(self.rect()), self.RADIUS, self.RADIUS)


_popup: TipPopup | None = None


def tip_popup() -> TipPopup:
    """Return the app's one tooltip window, made again after Qt deleted it."""
    global _popup  # noqa: PLW0603
    if _popup is None or not shiboken6.isValid(_popup):
        _popup = TipPopup()
    return _popup


class TipWrapper(QObject):
    """Show Ember tips for info icons and opted-in controls, suppressing Qt tips.

    Widgets with `show-tooltip` show `TipPopup` on hover or keyboard focus.
    Escape closes an open tip wherever focus is.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: D102
        kind = event.type()
        if kind == QEvent.Type.ToolTip:
            return True
        if (
            kind == QEvent.Type.KeyPress
            and cast(QKeyEvent, event).key() == Qt.Key.Key_Escape
            and _popup is not None
            and shiboken6.isValid(_popup)
            and _popup.isVisible()
        ):
            # Escape closes an open tooltip wherever focus is, then goes on.
            _popup.hide()
            return False
        if not isinstance(watched, QWidget) or not (
            isinstance(watched, InfoTip) or watched.property("show-tooltip") is True
        ):
            return False
        show = kind == QEvent.Type.Enter or (
            kind == QEvent.Type.FocusIn
            and cast(QFocusEvent, event).reason() in KEYBOARD_REASONS
        )
        if show and watched.isEnabled() and watched.toolTip():
            tip_popup().show_for(watched, watched.toolTip())
        elif _popup is not None and shiboken6.isValid(_popup):
            if kind in (QEvent.Type.FocusOut, QEvent.Type.Hide) or (
                kind == QEvent.Type.Leave and not watched.hasFocus()
            ):
                _popup.hide_for(watched)
            elif kind == QEvent.Type.ToolTipChange and _popup.owner is watched:
                if watched.toolTip() and _popup.isVisible():
                    _popup.show_for(watched, watched.toolTip())
                else:
                    _popup.hide_for(watched)
        return False


def install_tooltips(app: QApplication) -> TipWrapper:
    """Install one `TipWrapper` on the application and return it."""
    wrapper = app.findChild(TipWrapper)
    if wrapper is None:
        wrapper = TipWrapper(app)
        app.installEventFilter(wrapper)
    return wrapper


class InfoTip(QPushButton):
    """A small info icon that shows a tooltip on hover, keyboard focus or click.

    The tooltip stays while the pointer is over the icon or the icon has
    focus, and hides when the pointer leaves an unfocused icon, focus moves
    on, or Escape is pressed.
    """

    def __init__(self, about: str, text: str) -> None:
        """Create the icon.

        Args:
            about: What the tooltip explains; it names the icon for screen
                readers as "About <about>".
            text: The tooltip's plain text.
        """
        super().__init__()
        self.setProperty("role", "info")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"About {about}")
        self.setAccessibleDescription(text)
        self.setToolTip(text)
        self._paint_icon()
        theme.signals.changed.connect(self._paint_icon)
        self.clicked.connect(self.show_tip)

    def _paint_icon(self, _mode: str = "") -> None:
        self.setIcon(icons.icon("info", theme.color("text-muted"), 16))

    def set_text(self, text: str) -> None:
        """Change the tooltip's text, redrawing it when shown."""
        self.setToolTip(text)
        self.setAccessibleDescription(text)
        if self._tip_shown():
            self.show_tip()

    def _tip_shown(self) -> bool:
        popup = _popup
        return (
            popup is not None
            and shiboken6.isValid(popup)
            and popup.owner is self
            and popup.isVisible()
        )

    def show_tip(self) -> None:
        """Show the tooltip under the icon."""
        tip_popup().show_for(self, self.toolTip())

    def hide_tip(self) -> None:
        """Hide the tooltip when it is this icon's."""
        if _popup is not None and shiboken6.isValid(_popup):
            _popup.hide_for(self)

    def event(self, event: QEvent) -> bool:  # noqa: D102
        if event.type() == QEvent.Type.ToolTip:
            self.show_tip()
            return True
        return super().event(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: D102, N802
        super().leaveEvent(event)
        if not self.hasFocus():
            self.hide_tip()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: D102, N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide_tip()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: D102, N802
        super().focusInEvent(event)
        if event.reason() in KEYBOARD_REASONS:
            self.show_tip()

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: D102, N802
        super().focusOutEvent(event)
        self.hide_tip()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: D102, N802
        super().hideEvent(event)
        self.hide_tip()


def button(
    text: str, kind: str = "secondary", *, size: str | None = None
) -> QPushButton:
    """Return a push button styled by its kind.

    Args:
        text: The button's label.
        kind: The style rule the button selects: `primary`, `secondary`,
            `ghost`, `danger` or `danger-outline`.
        size: `sm` for a small button, or `None`. It sets the `scale`
            property; `size` is QWidget's own geometry property.

    Returns:
        A button that takes focus with Tab.
    """
    result = QPushButton(text)
    result.setProperty("kind", kind)
    if size is not None:
        result.setProperty("scale", size)
    result.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    return result


class IconButton(QPushButton):
    """A small ghost icon button with a tooltip and an accessible name."""

    def __init__(self, name: str, title: str, *, color: str = "text-secondary") -> None:
        """Build a button whose icon follows the theme's colour token."""
        super().__init__()
        self._icon_name = name
        self._icon_color = color
        self.setProperty("kind", "ghost")
        self.setProperty("scale", "sm")
        self.setProperty("icon-only", True)
        self.setProperty("show-tooltip", True)
        self.setStyleSheet('QPushButton[icon-only="true"] { padding: 0; }')
        self.setFixedSize(theme.CONTROL_SM, theme.CONTROL_SM)
        self.setIconSize(QSize(16, 16))
        self.setToolTip(title)
        self.setAccessibleName(title)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._draw_icon()
        theme.signals.changed.connect(self._draw_icon)

    def _draw_icon(self, *_args: object) -> None:
        self.setIcon(icons.icon(self._icon_name, theme.color(self._icon_color), 16))


ICON_GAP = "  "
"""Spaces put before an icon button's text; the style sheet draws no gap."""


def set_icon(target: QPushButton, icon: QIcon) -> None:
    """Give a button an icon with a gap before its text.

    The accessible name stays the text without the gap.

    Args:
        target: The button.
        icon: The icon.
    """
    target.setIcon(icon)
    text = target.text().lstrip(" ")
    if text:
        target.setText(ICON_GAP + text)
        if not target.accessibleName():
            target.setAccessibleName(text)


class ElidedLabel(QLabel):
    """A plain-text label that paints one elided line and retains its full text."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        """Create a label that keeps its full text for selection and copy."""
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def display_text(self) -> str:
        """Return the text that fits the label's contents rectangle."""
        return self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width()
        )

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(0, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: D102
        shown = self.display_text()
        if shown == self.text():
            super().paintEvent(event)
            return
        if not shown:
            return
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        text = QTextLayout(shown, self.font())
        if self.hasSelectedText():
            # The selection indexes the full text; the ellipsis spans its hidden suffix.
            start = self.selectionStart()
            end = min(start + len(self.selectedText()), len(shown))
            selection = QTextLayout.FormatRange()
            selection.start = min(start, len(shown) - 1)  # type: ignore[assignment]
            selection.length = end - selection.start  # type: ignore[assignment]
            selected_format = QTextCharFormat()
            selected_format.setBackground(self.palette().highlight())
            selected_format.setForeground(self.palette().highlightedText())
            selection.format = selected_format  # type: ignore[assignment]
            text.setFormats([selection])
        text.beginLayout()
        line = text.createLine()
        line.setLineWidth(self.contentsRect().width())
        text.endLayout()
        rect = self.contentsRect()
        text.draw(
            painter,
            QPointF(rect.x(), rect.y() + (rect.height() - line.height()) / 2),
        )


class Switch(QAbstractButton):
    """An on and off switch, painted, 36 by 20 pixels.

    The caller sets its accessible name from the setting's label. Click and
    Space toggle it.
    """

    WIDTH = 36
    HEIGHT = 20

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the switch, off."""
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.WIDTH + 4, self.HEIGHT + 4)
        theme.signals.changed.connect(self.update)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(self.WIDTH + 4, self.HEIGHT + 4)

    def hitButton(self, pos: QPoint) -> bool:  # noqa: D102
        return self.rect().contains(pos)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002, D102
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            track = QRectF(2, 2, self.WIDTH, self.HEIGHT)
            on = self.isChecked()
            fill = theme.color("accent" if on else "control-off")
            if not self.isEnabled():
                fill.setAlphaF(0.4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            radius = self.HEIGHT / 2
            painter.drawRoundedRect(track, radius, radius)
            margin = 2
            diameter = self.HEIGHT - 2 * margin
            left = track.right() - margin - diameter if on else track.left() + margin
            painter.setBrush(theme.color("thumb"))
            painter.drawEllipse(QRectF(left, track.top() + margin, diameter, diameter))
            if focus_visible(self):
                pen = QPen(theme.color("focus-ring"), 2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                ring = track.adjusted(-1, -1, 1, 1)
                painter.drawRoundedRect(ring, radius + 1, radius + 1)
        finally:
            painter.end()


class Spinner(QWidget):
    """A ring that turns one step every 80 ms while visible.

    Its accessible name is the text shown beside it.
    """

    STEP_MS = 80
    STEPS = 12

    def __init__(self, size: int = 20, parent: QWidget | None = None) -> None:
        """Create the spinner at `size` pixels square."""
        super().__init__(parent)
        self._step = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.setInterval(self.STEP_MS)
        self._timer.timeout.connect(self._advance)
        theme.signals.changed.connect(self.update)

    def is_turning(self) -> bool:
        """Return whether the spinner's timer runs."""
        return self._timer.isActive()

    def _advance(self) -> None:
        self._step = (self._step + 1) % self.STEPS
        self.update()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: D102
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: D102
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002, D102
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            width = 2.0
            box = QRectF(self.rect()).adjusted(width, width, -width, -width)
            pen = QPen(theme.color("border-strong"), width)
            painter.setPen(pen)
            painter.drawEllipse(box)
            pen.setColor(theme.color("text-secondary"))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            start = -self._step * (360 // self.STEPS) * 16
            painter.drawArc(box, start, 90 * 16)
        finally:
            painter.end()


class Segmented(QWidget):
    """Two to five options, one checkable button each, one chosen at a time.

    The arrow keys move the choice. `changed(str)` carries the chosen value.
    """

    changed = Signal(str)

    def __init__(
        self, options: Sequence[tuple[str, str]], parent: QWidget | None = None
    ) -> None:
        """Create the control.

        Args:
            options: `(value, label)` pairs, two to five, values unique.
            parent: The parent widget.

        Raises:
            ValueError: Fewer than two or more than five options, or a value
                repeated.
        """
        super().__init__(parent)
        values = [value for value, _ in options]
        if not 2 <= len(values) <= 5 or len(set(values)) != len(values):
            raise ValueError("Segmented takes two to five options with unique values")
        self.setProperty("role", "segmented")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._values = values
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._buttons: list[QPushButton] = []
        for index, (_, label) in enumerate(options):
            item = QPushButton(label)
            item.setProperty("role", "segment")
            item.setCheckable(True)
            item.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            item.installEventFilter(self)
            self._group.addButton(item, index)
            layout.addWidget(item)
            self._buttons.append(item)
        self._buttons[0].setChecked(True)
        self._group.idClicked.connect(self._clicked)

    def value(self) -> str:
        """Return the chosen value."""
        return self._values[max(self._group.checkedId(), 0)]

    def set_value(self, value: str) -> None:
        """Choose a value without emitting `changed`.

        Raises:
            ValueError: The value is not one of the options.
        """
        self._buttons[self._values.index(value)].setChecked(True)

    def buttons(self) -> list[QPushButton]:
        """Return the option buttons in order."""
        return list(self._buttons)

    def _clicked(self, index: int) -> None:
        self.changed.emit(self._values[index])

    def _move(self, offset: int) -> None:
        current = max(self._group.checkedId(), 0)
        target = max(0, min(len(self._buttons) - 1, current + offset))
        if target == current:
            return
        self._buttons[target].setChecked(True)
        self._buttons[target].setFocus(Qt.FocusReason.OtherFocusReason)
        self.changed.emit(self._values[target])

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: D102
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
                self._move(-1)
                return True
            if key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
                self._move(1)
                return True
        return super().eventFilter(watched, event)


HINT_ROLE = Qt.ItemDataRole.UserRole + 1
"""The item data role holding a picker item's hint line."""

CLOSED_ROLE = Qt.ItemDataRole.UserRole + 3
"""The item data role holding what a closed picker shows for the item."""

HEADER_ROLE = Qt.ItemDataRole.UserRole + 2
"""The item data role marking a picker group header."""


class _PickerDelegate(QStyledItemDelegate):
    """Draws a picker's items with their hint line and its group headers."""

    PAD = 8

    def sizeHint(  # noqa: D102
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        metrics = QFontMetrics(cast(QFont, option.font))
        lines = 2 if index.data(HINT_ROLE) else 1
        height = lines * metrics.height() + 2 * self.PAD
        return QSize(super().sizeHint(option, index).width(), height)

    def paint(  # noqa: D102
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        rect = cast(QRect, option.rect)
        state = cast(QStyle.StateFlag, option.state)
        header = bool(index.data(HEADER_ROLE))
        painter.save()
        try:
            if not header and state & QStyle.StateFlag.State_Selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.color("bg-selected"))
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                radius = theme.RADIUS["sm"]
                painter.drawRoundedRect(QRectF(rect), radius, radius)
            text_rect = rect.adjusted(12, self.PAD, -12, -self.PAD)
            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            if header:
                painter.setFont(theme.font("caption"))
                painter.setPen(theme.color("text-muted"))
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)
                return
            painter.setFont(theme.font("body"))
            painter.setPen(theme.color("text-primary"))
            flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            painter.drawText(text_rect, flags, text)
            hint = index.data(HINT_ROLE)
            if hint:
                line = QFontMetrics(theme.font("body")).height()
                painter.setFont(theme.font("caption"))
                painter.setPen(theme.color("text-muted"))
                painter.drawText(text_rect.adjusted(0, line, 0, 0), flags, str(hint))
        finally:
            painter.restore()


class Picker(QComboBox):
    """A pill picker whose items can carry a hint line, under group headers.

    Headers are not selectable. Each item's value is its user data.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create an empty picker."""
        super().__init__(parent)
        self.setMinimumWidth(theme.PICKER_MIN_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setItemDelegate(_PickerDelegate(self))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def add_header(self, text: str) -> None:
        """Add a group header, shown in the list and never selectable."""
        item = QStandardItem(text)
        item.setData(True, HEADER_ROLE)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self._model.appendRow(item)

    def add_option(
        self,
        text: str,
        value: object,
        hint: str | None = None,
        closed: str | None = None,
    ) -> None:
        """Add an item.

        Args:
            text: The shown text.
            value: The value `value()` returns while the item is chosen.
            hint: A second line shown in the dropdown list, or `None`.
            closed: The text the closed picker shows while the item is
                chosen, or `None` for `text`.
        """
        item = QStandardItem(text)
        item.setData(value, Qt.ItemDataRole.UserRole)
        if hint:
            item.setData(hint, HINT_ROLE)
        if closed:
            item.setData(closed, CLOSED_ROLE)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self._model.appendRow(item)
        if self.currentIndex() < 0 or self.is_header(self.currentIndex()):
            self.setCurrentIndex(self._model.rowCount() - 1)

    def closed_text(self) -> str:
        """Return what the closed picker shows: the chosen item's closed text."""
        closed = self.currentData(CLOSED_ROLE)
        return str(closed) if closed else self.currentText()

    def sizeHint(self) -> QSize:  # noqa: D102
        """Return a width that fits every item's text and closed text."""
        hint = super().sizeHint()
        metrics = self.fontMetrics()
        texts = [self.itemText(row) for row in range(self.count())]
        texts += [
            str(self.itemData(row, CLOSED_ROLE))
            for row in range(self.count())
            if self.itemData(row, CLOSED_ROLE)
        ]
        longest = max((metrics.horizontalAdvance(text) for text in texts), default=0)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        size = self.style().sizeFromContents(
            QStyle.ContentsType.CT_ComboBox,
            option,
            QSize(longest, metrics.height()),
            self,
        )
        return QSize(max(hint.width(), size.width()), hint.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """Allow the closed value to shrink to the picker floor."""
        return QSize(theme.PICKER_MIN_WIDTH, self.sizeHint().height())

    def elided_text(self) -> str:
        """Return the closed value fitted to the label rectangle."""
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        return self.fontMetrics().elidedText(
            self.closed_text(), Qt.TextElideMode.ElideRight, max(0, rect.width() - 4)
        )

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: D102, N802
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        text = self.elided_text()
        # PySide's stubs type this attribute wrongly; it holds the label text.
        option.currentText = text  # pyrefly: ignore[bad-assignment]
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)

    def is_header(self, row: int) -> bool:
        """Return whether a row is a group header."""
        item = self._model.item(row)
        return item is not None and bool(item.data(HEADER_ROLE))

    def value(self) -> object:
        """Return the chosen item's value, or `None` when nothing is chosen."""
        if self.currentIndex() < 0:
            return None
        return self.currentData(Qt.ItemDataRole.UserRole)

    def set_value(self, value: object) -> bool:
        """Choose the first item carrying `value`.

        Returns:
            Whether an item carries it; the choice is unchanged otherwise.
        """
        for row in range(self._model.rowCount()):
            if self.is_header(row):
                continue
            if self.itemData(row, Qt.ItemDataRole.UserRole) == value:
                self.setCurrentIndex(row)
                return True
        return False


class Badge(QLabel):
    """A short status word in a tone; the word is always shown."""

    def __init__(
        self, text: str, tone: str = "neutral", parent: QWidget | None = None
    ) -> None:
        """Create the badge.

        Raises:
            ValueError: The tone is not one of `TONES`.
        """
        super().__init__(text, parent)
        self.setProperty("role", "badge")
        self.setProperty("tone", _check_tone(tone))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def tone(self) -> str:
        """Return the badge's tone."""
        return str(self.property("tone"))

    def set_tone(self, tone: str, text: str) -> None:
        """Change the badge's tone and word.

        Raises:
            ValueError: The tone is not one of `TONES`.
        """
        self.setProperty("tone", _check_tone(tone))
        self.setText(text)
        repolish(self)


class Card(QFrame):
    """A flat card with a hairline border."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the card."""
        super().__init__(parent)
        self.setProperty("role", "card")


class _ToneIcon(QLabel):
    """An icon in a tone's text colour that follows the theme."""

    def __init__(self, name: str, token: str, size: int) -> None:
        super().__init__()
        self._name, self._token, self._size = name, token, size
        self.setFixedSize(size, size)
        self._draw()
        theme.signals.changed.connect(self._draw)

    def _draw(self) -> None:
        self.setPixmap(icons.pixmap(self._name, theme.color(self._token), self._size))


class Alert(QFrame):
    """A message in a tone, with its icon, a title, text and optional actions."""

    def __init__(
        self,
        tone: str,
        title: str,
        text: str = "",
        actions: Sequence[tuple[str, Callable[[], None]]] = (),
        parent: QWidget | None = None,
    ) -> None:
        """Create the alert.

        Args:
            tone: One of `TONES`.
            title: The first line, in the label weight.
            text: The lines under the title; empty for none.
            actions: `(label, callback)` pairs, each shown as a button.
            parent: The parent widget.

        Raises:
            ValueError: The tone is not one of `TONES`.
        """
        super().__init__(parent)
        self.setProperty("role", "alert")
        self.setProperty("tone", _check_tone(tone))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        mark = _ToneIcon(_TONE_ICON[tone], _TONE_TEXT[tone], 18)
        mark.setAccessibleName(tone)
        layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(4)
        self.title_label = QLabel(title)
        self.title_label.setFont(theme.font("label"))
        self.title_label.setWordWrap(True)
        body.addWidget(self.title_label)
        self.text_label = QLabel(text)
        self.text_label.setWordWrap(True)
        body.addWidget(self.text_label)
        layout.addLayout(body, 1)
        self.text_label.setVisible(bool(text))
        self.buttons: list[QPushButton] = []
        for label, callback in actions:
            action = button(label, "secondary", size="sm")
            action.clicked.connect(callback)
            layout.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)
            self.buttons.append(action)
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)

    def tone(self) -> str:
        """Return the alert's tone."""
        return str(self.property("tone"))


class StateView(QWidget):
    """The empty, loading and error states of a list or screen (design J10).

    Empty shows an icon, a title and one line. Loading shows a spinner and
    what is being read, only after 300 ms. Error shows what happened and what
    to do, with Try again when a retry can help.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the view, showing nothing."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.empty = QWidget()
        empty = QVBoxLayout(self.empty)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_icon = QLabel()
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel()
        self.empty_title.setFont(theme.font("heading"))
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_line = QLabel()
        self.empty_line.setProperty("role", "muted")
        self.empty_line.setWordWrap(True)
        self.empty_line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for part in (self.empty_icon, self.empty_title, self.empty_line):
            empty.addWidget(part)

        self.loading = QWidget()
        loading = QHBoxLayout(self.loading)
        loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spinner = Spinner()
        self.loading_text = QLabel()
        self.loading_text.setProperty("role", "secondary")
        loading.addWidget(self.spinner)
        loading.addWidget(self.loading_text)

        self.error = QWidget()
        error = QVBoxLayout(self.error)
        error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_text = QLabel()
        self.error_text.setWordWrap(True)
        self.error_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.retry_button = button("Try again", "secondary")
        self.retry_button.clicked.connect(self._retry_clicked)
        error.addWidget(self.error_text)
        error.addWidget(self.retry_button, 0, Qt.AlignmentFlag.AlignCenter)

        for page in (self.empty, self.loading, self.error):
            layout.addWidget(page)
            page.hide()

        self._icon_name = ""
        self._retry: Callable[[], None] | None = None
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(LOADING_DELAY_MS)
        self._delay.timeout.connect(self.loading.show)
        theme.signals.changed.connect(self._draw_icon)

    def show_empty(self, icon: str, title: str, line: str) -> None:
        """Show the empty state: an icon, a title and one line, no button."""
        self.clear()
        self._icon_name = icon
        self._draw_icon()
        self.empty_title.setText(title)
        self.empty_line.setText(line)
        self.empty.show()

    def show_loading(self, text: str) -> None:
        """Show a spinner and `text` once 300 ms have passed."""
        self.clear()
        self.loading_text.setText(text)
        self.spinner.setAccessibleName(text)
        self._delay.start()

    def show_error(self, text: str, retry: Callable[[], None] | None) -> None:
        """Show what happened and what to do, with Try again when `retry` is set."""
        self.clear()
        self.error_text.setText(text)
        self._retry = retry
        self.retry_button.setVisible(retry is not None)
        self.error.show()

    def clear(self) -> None:
        """Show nothing and cancel a pending loading state."""
        self._delay.stop()
        self._retry = None
        for page in (self.empty, self.loading, self.error):
            page.hide()

    def _retry_clicked(self) -> None:
        if self._retry is not None:
            self._retry()

    def _draw_icon(self) -> None:
        if self._icon_name:
            self.empty_icon.setPixmap(
                icons.pixmap(self._icon_name, theme.color("text-muted"), 32)
            )


class CommandLine(QFrame):
    """A command in the code font, with Copy and a primary Run button.

    Copy puts the command on the clipboard and reads "Copied" until the
    command changes. Run emits `run_requested(str)` with the command; it
    never runs anything itself.
    """

    run_requested = Signal(str)

    def __init__(self, command: str = "", parent: QWidget | None = None) -> None:
        """Create the line for a command."""
        super().__init__(parent)
        self.setProperty("role", "command")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 6, 6)
        layout.setSpacing(8)
        self.text = QLabel()
        self.text.setFont(theme.font("code"))
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setWordWrap(True)
        layout.addWidget(self.text, 1)
        self.copy_button = button("Copy", "ghost", size="sm")
        self.copy_button.clicked.connect(self.copy)
        layout.addWidget(self.copy_button)
        self.run_button = button("Run", "primary", size="sm")
        self.run_button.clicked.connect(lambda: self.run_requested.emit(self._command))
        layout.addWidget(self.run_button)
        self._command = ""
        self._draw_icons()
        theme.signals.changed.connect(self._draw_icons)
        self.set_command(command)

    def command(self) -> str:
        """Return the command shown."""
        return self._command

    def set_command(self, command: str) -> None:
        """Show a command; Copy reads "Copy" again when it changed."""
        if command != self._command:
            self.copy_button.setText(ICON_GAP + "Copy")
        self._command = command
        self.text.setText(command)
        self.text.setAccessibleName(command)
        self.copy_button.setAccessibleName(f"Copy {command}")
        self.run_button.setAccessibleName(f"Run {command}")

    def copy(self) -> None:
        """Put the command on the clipboard."""
        QGuiApplication.clipboard().setText(self._command)
        self.copy_button.setText(ICON_GAP + "Copied")

    def _draw_icons(self) -> None:
        set_icon(self.copy_button, icons.icon("copy", theme.color("text-primary"), 16))


class _CommandLabel(QLabel):
    """A selectable command with display-only break opportunities after slashes."""

    def __init__(self, command: str) -> None:
        super().__init__(command.replace("/", "/\u200b"))
        self._command = command
        self.setFont(theme.font("code"))
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMargin(8)
        self.setAccessibleName(command)
        self.setAccessibleDescription(command)

    def heightForWidth(self, width: int) -> int:  # noqa: N802, D102
        document = QTextDocument()
        document.setDefaultFont(self.font())
        document.setDocumentMargin(0)
        document.setPlainText(self.text())
        document.setTextWidth(max(1, width - 2 * self.margin()))
        return math.ceil(document.size().height()) + 2 * self.margin()

    def copy(self) -> None:
        """Put the original command on the clipboard."""
        QGuiApplication.clipboard().setText(self._command)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: D102
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            event.accept()
            return
        super().keyPressEvent(event)

    def _copy_primary_selection(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if self.hasSelectedText() and clipboard.supportsSelection():
            clipboard.setText(
                self.selectedText().replace("\u200b", ""), QClipboard.Mode.Selection
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        super().mouseReleaseEvent(event)
        self._copy_primary_selection()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: D102
        super().keyReleaseEvent(event)
        self._copy_primary_selection()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: D102
        menu = QMenu(self)
        copy = menu.addAction("Copy")
        copy.setEnabled(self.hasSelectedText())
        copy.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(
                self.selectedText().replace("\u200b", "")
            )
        )
        menu.exec(event.globalPos())
        menu.deleteLater()


class _CommandScrollArea(QScrollArea):
    """Show a scrollbar only when the content exceeds the full viewport height."""

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: D102
        content = self.widget()
        if content is not None:
            layout = content.layout()
            assert layout is not None
            height = layout.totalHeightForWidth(self.contentsRect().width())
            self.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOn
                if height > self.contentsRect().height()
                else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
        super().resizeEvent(event)


class RunCommandDialog(QDialog):
    """Asks before a command runs in the owner's terminal (design J5).

    Cancel is the default button and has focus when the dialog opens, so
    Enter and Escape cancel; Enter on a focused Run in terminal presses it.
    The dialog runs nothing; it returns `Accepted` only from Run in terminal.
    """

    TITLE = "Run this command?"
    TEXT = (
        "Ember opens it in your terminal. "
        "Its output and any password prompt stay there."
    )

    def __init__(self, command: str, parent: QWidget | None = None) -> None:
        """Create the dialog for a command."""
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.title = QLabel(self.TITLE)
        self.title.setFont(theme.font("title"))
        layout.addWidget(self.title)
        self.scroll_area = _CommandScrollArea()
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(16)
        self.text = QLabel(self.TEXT)
        self.text.setWordWrap(True)
        self.text.setProperty("role", "secondary")
        self.command = _CommandLabel(command)
        for label in (self.text, self.command):
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            self._body.addWidget(label)
        self.scroll_area.setWidget(content)
        layout.addWidget(self.scroll_area)
        self._row = QHBoxLayout()
        self.copy_button = button("Copy", "secondary")
        self.copy_button.setAutoDefault(False)
        self.copy_button.clicked.connect(self.command.copy)
        self._row.addWidget(self.copy_button)
        row = self._row
        row.addStretch(1)
        self.cancel_button = button("Cancel", "secondary")
        self.cancel_button.clicked.connect(self.reject)
        self.run_button = button("Run in terminal", "primary")
        self.run_button.setAutoDefault(True)
        self.run_button.clicked.connect(self.accept)
        row.addWidget(self.cancel_button)
        row.addWidget(self.run_button)
        layout.addLayout(row)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._limit_size()

    def _limit_size(self) -> None:
        available = self.screen().availableGeometry().size() - QSize(96, 96)
        self.setMaximumSize(available)

    def sizeHint(self) -> QSize:  # noqa: D102
        self.ensurePolished()
        layout = self.layout()
        assert layout is not None
        margins = layout.contentsMargins()
        horizontal = margins.left() + margins.right()
        vertical = margins.top() + margins.bottom()
        # Reserve title-bar controls on both sides of the window title.
        title_bar = self.fontMetrics().horizontalAdvance(self.windowTitle()) + 4 * (
            self.style().pixelMetric(QStyle.PixelMetric.PM_TitleBarHeight)
        )
        width = min(
            self.maximumWidth(),
            max(
                title_bar,
                horizontal + self.title.sizeHint().width(),
                horizontal + self._row.sizeHint().width(),
                horizontal + self.text.sizeHint().width(),
                horizontal + self.command.sizeHint().width(),
            ),
        )
        height = (
            vertical
            + self.title.sizeHint().height()
            + self._body.totalHeightForWidth(width - horizontal)
            + self._row.sizeHint().height()
            + 2 * layout.spacing()
        )
        return QSize(width, min(height, self.maximumHeight()))

    def showEvent(self, event: QShowEvent) -> None:  # noqa: D102
        self._limit_size()
        self.resize(self.sizeHint())
        super().showEvent(event)
        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)


def tone_color(tone: str) -> QColor:
    """Return the text colour of a tone in the current theme mode."""
    return theme.color(_TONE_TEXT[_check_tone(tone)])
