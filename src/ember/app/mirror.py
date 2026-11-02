"""The read-only screen mirror of a live run (design J2, J9).

`MirrorClient` attaches a tmux control-mode client to the run's session on
Ember's tmux server with the `read-only` and `ignore-size` flags, so it can
neither type into the pane nor change its size. tmux is the terminal
emulator: after the pane writes output, the client asks the same tmux client
for `capture-pane -p -e` and the pane's size and cursor, and the view redraws
from that text. At most one capture is in flight; output that arrives during
a capture causes exactly one more. Nothing runs on a timer.

`MirrorView` paints the captured screen in the terminal font with the
mirror's colours (`resolve`).
"""

import contextlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from PySide6.QtCore import (
    QByteArray,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from ember import paths, tmux
from ember.app import sgr, theme

STOP_GRACE_MS = 1000
"""How long `stop` waits after closing standard input before killing tmux."""

PADDING = 12
"""The space between the mirror's edge and its text, in pixels."""

ENDED_REASON = "The session ended."
"""The reason given when tmux exits without one."""

SIZE_FORMAT = (
    "#{pane_width} #{pane_height} #{cursor_x} #{cursor_y} "
    "#{cursor_flag} #{alternate_on}"
)
"""The `display-message` format for the pane's size and cursor."""

_CUBE = (0, 95, 135, 175, 215, 255)


@dataclass(frozen=True)
class Screen:
    """One capture of a pane."""

    lines: tuple[str, ...]
    """The captured lines, with their SGR sequences."""

    width: int
    """The pane's columns."""

    height: int
    """The pane's rows."""

    cursor_x: int
    """The cursor's column."""

    cursor_y: int
    """The cursor's row."""

    cursor_on: bool
    """Whether the cursor is shown."""


def _size(lines: list[str]) -> tuple[int, int, int, int, bool] | None:
    if not lines:
        return None
    parts = lines[0].split()
    if len(parts) < 5 or not all(part.isdigit() for part in parts[:5]):
        return None
    width, height, cursor_x, cursor_y, flag = (int(part) for part in parts[:5])
    return width, height, cursor_x, cursor_y, flag == 1


_STOPPING: set[QProcess] = set()
"""Processes stopped but not yet exited; held so they are not destroyed
while running."""


def _release(process: QProcess) -> None:
    _STOPPING.discard(process)
    process.deleteLater()


class MirrorClient(QObject):
    """A tmux control-mode client that reports the run's screen when it changes.

    Connect `updated` and `ended` before calling `start`.
    """

    updated = Signal(object)
    """A new `Screen` was captured."""

    ended = Signal(str)
    """The client ended, with the reason: the session ended, tmux is missing
    or a command failed. Nothing more is captured."""

    def __init__(
        self, run_id: str, env: Mapping[str, str], parent: QObject | None = None
    ) -> None:
        """Create the client; nothing starts until `start`.

        Args:
            run_id: The run, which is also its tmux session's name.
            env: The environment that locates tmux and Ember's tmux server.
            parent: The parent object.
        """
        super().__init__(parent)
        self.run_id = run_id
        self._env = env
        self._process: QProcess | None = None
        self._buffer = b""
        self._attached = False
        self._pane: str | None = None
        self._dirty = False
        self._pending: deque[str] = deque()
        self._block: list[str] | None = None
        self._block_key: tuple[str, str] = ("", "")
        self._block_ours = False
        self._lines: list[str] = []
        self._size: tuple[int, int, int, int, bool] | None = None
        self._done = False
        self.captures = 0
        """How many captures were asked for."""

    def pane(self) -> str | None:
        """Return the pane id (`%N`) once known."""
        return self._pane

    def start(self) -> None:
        """Start the tmux client; `ended` is emitted at once when it cannot start."""
        try:
            argv = tmux.control_argv(self.run_id, env=self._env)
        except (tmux.TmuxError, paths.RuntimeDirError, OSError) as error:
            self._end(f"The screen cannot be shown: {error}")
            return
        process = QProcess()
        process.setProcessEnvironment(QProcessEnvironment())
        process.setProgram(argv[0])
        process.setArguments(argv[1:])
        process.readyReadStandardOutput.connect(self._read)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._error)
        self._process = process
        process.start()

    def stop(self) -> None:
        """Close tmux's standard input, and kill it if it runs a second later."""
        self._done = True
        process, self._process = self._process, None
        if process is None:
            return
        process.readyReadStandardOutput.disconnect(self._read)
        process.finished.disconnect(self._finished)
        process.errorOccurred.disconnect(self._error)
        if process.state() == QProcess.ProcessState.NotRunning:
            process.deleteLater()
            return
        _STOPPING.add(process)
        process.finished.connect(lambda *_args: _release(process))
        process.closeWriteChannel()
        QTimer.singleShot(STOP_GRACE_MS, process, process.kill)

    def running(self) -> bool:
        """Return whether the tmux client was started and has not ended."""
        return self._process is not None and not self._done

    # Reading

    def _read(self) -> None:
        process = self._process
        if process is None:
            return
        self._buffer += bytes(process.readAllStandardOutput().data())
        *lines, self._buffer = self._buffer.split(b"\n")
        for raw in lines:
            if self._done:
                return
            self._line(raw.decode("utf-8", errors="replace").rstrip("\r"))

    def _line(self, line: str) -> None:
        if self._block is not None:
            word, _, rest = line.partition(" ")
            if word in ("%end", "%error") and tuple(rest.split(" ")[:2]) == (
                self._block_key
            ):
                self._finish_block(error=word == "%error")
            else:
                self._block.append(line)
            return
        word, _, rest = line.partition(" ")
        if word == "%begin":
            parts = rest.split(" ")
            self._block_key = (parts[0], parts[1] if len(parts) > 1 else "")
            # Commands this client wrote carry the flag 1; the attach that
            # started it carries 0.
            self._block_ours = (
                len(parts) > 2 and parts[2] == "1" and bool(self._pending)
            )
            self._block = []
        elif word == "%output":
            pane = rest.partition(" ")[0]
            if self._pane is None:
                self._pane = pane
            if pane == self._pane:
                self._mark_dirty()
        elif word in ("%layout-change", "%session-changed"):
            self._attached = True
            self._mark_dirty()
        elif word == "%exit":
            self._end(
                f"The session ended: {rest.strip()}" if rest.strip() else ENDED_REASON
            )

    def _finish_block(self, *, error: bool) -> None:
        lines = self._block or []
        self._block = None
        if not self._block_ours:
            if error:
                self._end(self._failure(lines))
                return
            if not self._attached:
                self._attached = True
                self._dirty = True
            self._capture()
            return
        kind = self._pending.popleft()
        if error:
            self._pending.clear()
            self._end(self._failure(lines))
            return
        if kind == "pane":
            if lines and lines[0].startswith("%"):
                self._pane = lines[0].strip()
        elif kind == "capture":
            self._lines = lines
        elif kind == "size":
            self._size = _size(lines)
        if not self._pending:
            self._deliver()
            self._capture()

    @staticmethod
    def _failure(lines: list[str]) -> str:
        text = " ".join(line.strip() for line in lines if line.strip())
        return f"tmux refused a command: {text}" if text else "tmux refused a command."

    def _deliver(self) -> None:
        size = self._size or (0, len(self._lines), 0, 0, False)
        width, height, cursor_x, cursor_y, cursor_on = size
        self.updated.emit(
            Screen(tuple(self._lines), width, height, cursor_x, cursor_y, cursor_on)
        )

    # Capturing

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._capture()

    def _capture(self) -> None:
        process = self._process
        if (
            process is None
            or self._done
            or not self._attached
            or not self._dirty
            or self._pending
        ):
            return
        self._dirty = False
        self.captures += 1
        session = f"={self.run_id}:"
        target = self._pane or session
        commands: list[tuple[str, str]] = []
        if self._pane is None:
            commands.append(("pane", f"display-message -p -t {session} '#{{pane_id}}'"))
        commands.append(("capture", f"capture-pane -p -e -t {target}"))
        commands.append(("size", f"display-message -p -t {target} '{SIZE_FORMAT}'"))
        for kind, command in commands:
            self._pending.append(kind)
            process.write(QByteArray((command + "\n").encode()))

    # Ending

    def _finished(self, *_args: object) -> None:
        if self._done:
            return
        process = self._process
        text = ""
        if process is not None:
            with contextlib.suppress(RuntimeError):
                text = bytes(process.readAllStandardError().data()).decode(
                    "utf-8", errors="replace"
                )
        first = text.strip().splitlines()[0] if text.strip() else ""
        self._end(f"The session ended: {first}" if first else ENDED_REASON)

    def _error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._end("The screen cannot be shown: tmux could not start.")

    def _end(self, reason: str) -> None:
        if self._done:
            return
        self._done = True
        self._pending.clear()
        self.ended.emit(reason)


def _rgb(index: int) -> QColor:
    if index < 232:
        value = index - 16
        return QColor(_CUBE[value // 36], _CUBE[value // 6 % 6], _CUBE[value % 6])
    level = 8 + 10 * (index - 232)
    return QColor(level, level, level)


def _color(value: sgr.Color | None, palette: theme.MirrorPalette) -> QColor:
    if value is None:
        return palette.fg
    if value[0] == "index":
        index = value[1]
        if index < 16:
            return palette.colors[index]
        return theme.readable(_rgb(index), palette.bg, 4.5)
    _kind, red, green, blue = value  # type: ignore[misc]
    return theme.readable(QColor(red, green, blue), palette.bg, 4.5)


def resolve(cell: sgr.Cell, palette: theme.MirrorPalette) -> tuple[str, str | None]:
    """Return the colours a cell is drawn in (design J9).

    Indexes 0 to 15 use the sixteen terminal colours. Other indexes and RGB
    colours are made readable on `term-bg` at 4.5:1. Background colours are
    dropped. Reverse video draws the text in `term-bg` on the foreground.

    Args:
        cell: A parsed cell.
        palette: The mirror's colours for the current mode.

    Returns:
        The text colour as `#rrggbb`, and the cell's background as `#rrggbb`
        or `None` for the mirror's own.
    """
    fg = _color(cell.fg, palette)
    if cell.reverse:
        return palette.bg.name(), fg.name()
    return fg.name(), None


class _Canvas(QWidget):
    """Paints one captured screen."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[list[sgr.Cell]] = []
        self.capture: Screen | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._font = theme.font("term")
        self._metrics = QFontMetrics(self._font)

    def cell_size(self) -> QSize:
        return QSize(
            max(1, self._metrics.horizontalAdvance("M")), max(1, self._metrics.height())
        )

    def show_screen(self, screen: Screen) -> None:
        self.capture = screen
        self.rows = sgr.parse("\n".join(screen.lines))
        columns = max(
            [screen.width]
            + [sum(sgr.width(cell.char) for cell in row) for row in self.rows]
        )
        lines = max(screen.height, len(self.rows))
        cell = self.cell_size()
        self.setFixedSize(
            columns * cell.width() + 2 * PADDING, lines * cell.height() + 2 * PADDING
        )
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002, D102
        palette = theme.mirror_palette(theme.current_mode())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.bg)
        radius = theme.RADIUS["sm"]
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        cell = self.cell_size()
        ascent = self._metrics.ascent()
        for row_index, row in enumerate(self.rows):
            top = PADDING + row_index * cell.height()
            column = 0
            for item in row:
                span = sgr.width(item.char)
                left = PADDING + column * cell.width()
                column += span
                fg, bg = resolve(item, palette)
                if bg is not None:
                    painter.fillRect(
                        left, top, span * cell.width(), cell.height(), QColor(bg)
                    )
                if item.char == " ":
                    continue
                font = QFont(self._font)
                font.setBold(item.bold)
                font.setItalic(item.italic)
                font.setUnderline(item.underline)
                font.setStrikeOut(item.strike)
                painter.setFont(font)
                painter.setPen(QColor(fg))
                painter.drawText(left, top + ascent, item.char)
        screen = self.capture
        if screen is not None and screen.cursor_on:
            painter.setPen(QPen(palette.fg, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(
                PADDING + screen.cursor_x * cell.width(),
                PADDING + screen.cursor_y * cell.height(),
                cell.width() - 1,
                cell.height() - 1,
            )
        painter.end()


class MirrorView(QWidget):
    """The read-only screen of a live run.

    No key or mouse input reaches tmux. When the client ends, the view shows
    "Session ended" above the last screen.
    """

    def __init__(
        self, run_id: str, env: Mapping[str, str], parent: QWidget | None = None
    ) -> None:
        """Create the view and its client; call `start` to attach.

        Args:
            run_id: The run.
            env: The environment that locates tmux.
            parent: The parent widget.
        """
        super().__init__(parent)
        self.run_id = run_id
        self.setAccessibleName(f"Screen of run {run_id}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.ended_label = QLabel("Session ended")
        self.ended_label.setProperty("role", "secondary")
        self.ended_label.hide()
        layout.addWidget(self.ended_label)
        self.canvas = _Canvas()
        self.scroller = QScrollArea()
        self.scroller.setFrameShape(QFrame.Shape.NoFrame)
        self.scroller.setWidgetResizable(False)
        self.scroller.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroller.setWidget(self.canvas)
        self.scroller.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self.scroller)
        self.canvas.setFixedSize(0, 0)
        self._fit()
        self.reason = ""
        self.client = MirrorClient(run_id, env, self)
        self.client.updated.connect(self._updated)
        self.client.ended.connect(self._ended)
        theme.signals.changed.connect(self.canvas.update)

    def start(self) -> None:
        """Attach to the session."""
        self.client.start()

    def stop(self) -> None:
        """Detach from the session."""
        self.client.stop()

    def last_screen(self) -> Screen | None:
        """Return the last captured screen, or `None` before the first."""
        return self.canvas.capture

    def has_ended(self) -> bool:
        """Return whether the session ended or the screen cannot be shown."""
        return self.ended_label.isVisibleTo(self)

    def _fit(self) -> None:
        bar = self.scroller.horizontalScrollBar().sizeHint().height()
        self.scroller.setFixedHeight(self.canvas.height() + bar)

    def _updated(self, screen: Screen) -> None:
        self.canvas.show_screen(screen)
        self._fit()

    def _ended(self, reason: str) -> None:
        self.reason = reason
        self.ended_label.setToolTip(reason)
        self.ended_label.setAccessibleDescription(reason)
        self.ended_label.show()
