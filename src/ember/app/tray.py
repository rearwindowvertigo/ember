"""The tray icon and its menu (design J3).

The icon shows whether any run is live, always in the drawings for a dark
top bar. The menu holds a status line, one line per live run, which opens a
terminal on that run, Open Ember and Quit Ember. The menu is not rebuilt
while it is open; a change that comes in meanwhile is drawn after it closes.
"""

import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ember.app import icons
from ember.app.context import AppContext
from ember.runs.record import Run

ENGINE_LABELS = {"codex": "Codex", "opencode": "OpenCode", "claude": "Claude"}
"""Each engine's shown name."""


def status_line(live: int) -> str:
    """Return the menu's status line, such as `2 runs live`."""
    if live == 0:
        return "No runs live"
    return f"{live} run live" if live == 1 else f"{live} runs live"


def tooltip(live: int) -> str:
    """Return the icon's tooltip, such as `Ember · 2 runs live`."""
    return f"Ember · {status_line(live)}"


def run_line(run: Run) -> str:
    """Return a live run's menu line: `<Engine> · <repository> · <HH:MM>`."""
    engine = ENGINE_LABELS.get(run.engine, run.engine)
    started = time.strftime("%H:%M", time.localtime(run.started_at))
    return f"{engine} · {Path(run.repo).name} · {started}"


def available() -> bool:
    """Return whether the desktop shows tray icons."""
    return QSystemTrayIcon.isSystemTrayAvailable()


class Tray(QObject):
    """The tray icon, kept current from the run records."""

    def __init__(
        self,
        context: AppContext,
        open_window: Callable[[], None],
        quit_app: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        """Create the icon and its menu; `show()` puts it in the tray.

        Args:
            context: The app's shared state; its runs drive the icon.
            open_window: Shows and raises the window.
            quit_app: Quits Ember.
            parent: The parent object.
        """
        super().__init__(parent)
        self._context = context
        self._open_window = open_window
        self._quit = quit_app
        self.menu = QMenu()
        self.icon = QSystemTrayIcon(self)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self._live: bool | None = None
        self._menu_open = False
        self._stale = False
        self.menu.aboutToShow.connect(self._menu_shown)
        self.menu.aboutToHide.connect(self._menu_hidden)
        context.runs.changed.connect(self.update)
        self.update()

    def show(self) -> None:
        """Put the icon in the tray."""
        self.icon.show()

    def hide(self) -> None:
        """Take the icon out of the tray."""
        self.icon.hide()

    def lines(self) -> list[str]:
        """Return the menu's lines, with `-` for the separator."""
        return [
            "-" if action.isSeparator() else action.text()
            for action in self.menu.actions()
        ]

    def update(self) -> None:
        """Redraw the icon and tooltip from the live runs, and the menu.

        While the menu is open it is marked stale instead, and rebuilt once
        it has closed.
        """
        runs = self._context.runs.live()
        live = bool(runs)
        if live != self._live:
            self._live = live
            self.icon.setIcon(icons.tray_icon(live))
        self.icon.setToolTip(tooltip(len(runs)))
        if self._menu_open:
            self._stale = True
            return
        self._stale = False
        self._build_menu(runs)

    def _build_menu(self, runs: list[Run]) -> None:
        self.menu.clear()
        status = QAction(status_line(len(runs)), self.menu)
        status.setEnabled(False)
        self.menu.addAction(status)
        for run in runs:
            action = QAction(run_line(run), self.menu)
            action.triggered.connect(
                lambda _checked=False, run_id=run.id: self._context.open_run_terminal(
                    run_id
                )
            )
            self.menu.addAction(action)
        self.menu.addSeparator()
        open_action = QAction("Open Ember", self.menu)
        open_action.triggered.connect(self._open_window)
        self.menu.addAction(open_action)
        quit_action = QAction("Quit Ember", self.menu)
        quit_action.triggered.connect(self._quit)
        self.menu.addAction(quit_action)

    def _menu_shown(self) -> None:
        self._menu_open = True

    def _menu_hidden(self) -> None:
        self._menu_open = False
        if self._stale:
            # The chosen action is triggered after aboutToHide, so the rebuild,
            # which deletes the old actions, waits for the event loop.
            QTimer.singleShot(0, self, self._rebuild_if_stale)

    def _rebuild_if_stale(self) -> None:
        if self._stale and not self._menu_open:
            self.update()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._open_window()
