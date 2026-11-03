"""The tray icon and its menu (design J3).

The icon shows whether any run, flow or panel is live, always in the
drawings for a dark top bar. The menu lists live items with workers nested
under their parents. A change updates the icon and tooltip at once and
rebuilds the menu when it next opens.
"""

import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ember.app import icons
from ember.app.context import AppContext
from ember.app.flows import ENDED_FLOW, ENDED_PANEL, FlowItem, PanelItem
from ember.runs.record import Run

ENGINE_LABELS = {"codex": "Codex", "opencode": "OpenCode", "claude": "Claude"}
"""Each engine's shown name."""


type Item = Run | FlowItem | PanelItem


def status_line(live: int, flows: int = 0, panels: int = 0) -> str:
    """Return the non-zero top-level counts in run, flow, panel order."""
    parts = [
        f"{count} {kind}{'s' if count != 1 else ''}"
        for count, kind in ((live, "run"), (flows, "flow"), (panels, "panel"))
        if count
    ]
    return f"{', '.join(parts)} live" if parts else "No runs live"


def tooltip(live: int, flows: int = 0, panels: int = 0) -> str:
    """Return the icon's tooltip with the menu's top-level counts."""
    return f"Ember · {status_line(live, flows, panels)}"


def _started(item: Item) -> tuple[float, str]:
    """Return start time and id for Live's oldest-first ordering."""
    if isinstance(item, Run):
        return item.started_at, item.id
    return item.record["started_at"], item.record["id"]


def live_items(
    runs: list[Run], parents: list[FlowItem | PanelItem]
) -> tuple[list[Item], dict[str, list[Run]]]:
    """Group live runs under live parents, using the oldest parent's claim."""
    owners: dict[str, str] = {}
    children: dict[str, list[Run]] = {}
    top: list[Item] = []
    for parent in sorted(parents, key=_started):
        ident = parent.record["id"]
        for run_id in parent.run_ids:
            owners.setdefault(run_id, ident)
        ended = ENDED_FLOW if isinstance(parent, FlowItem) else ENDED_PANEL
        if parent.record["status"] not in ended:
            top.append(parent)
            children[ident] = []
    for run in sorted(runs, key=_started):
        owner = owners.get(run.id)
        if owner in children:
            children[owner].append(run)
        else:
            top.append(run)
    return sorted(top, key=_started), children


def counts(items: list[Item]) -> tuple[int, int, int]:
    """Count top-level runs, flows and panels."""
    return (
        sum(isinstance(item, Run) for item in items),
        sum(isinstance(item, FlowItem) for item in items),
        sum(isinstance(item, PanelItem) for item in items),
    )


def parent_line(item: FlowItem | PanelItem) -> str:
    """Return a flow or panel's type, record id and local start time."""
    kind = "Flow" if isinstance(item, FlowItem) else "Panel"
    ident = item.record["id"]
    if len(ident) > 40:
        ident = ident[:39] + "…"
    started = time.strftime("%H:%M", time.localtime(item.record["started_at"]))
    return f"{kind} · {ident} · {started}"


def run_line(run: Run) -> str:
    """Return a live run's menu line: `<Engine> · <repository> · <HH:MM>`."""
    engine = ENGINE_LABELS.get(run.engine, run.engine)
    started = time.strftime("%H:%M", time.localtime(run.started_at))
    return f"{engine} · {Path(run.repo).name} · {started}"


def available() -> bool:
    """Return whether the desktop shows tray icons."""
    return QSystemTrayIcon.isSystemTrayAvailable()


class Tray(QObject):
    """The tray icon, kept current from the run, flow and panel records."""

    def __init__(
        self,
        context: AppContext,
        open_window: Callable[[], None],
        open_overview: Callable[[], None],
        quit_app: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        """Create the icon and its menu; `show()` puts it in the tray.

        Args:
            context: The app's shared run, flow and panel state.
            open_window: Shows and raises the window.
            open_overview: Shows the window and selects Overview.
            quit_app: Quits Ember.
            parent: The parent object.
        """
        super().__init__(parent)
        self._context = context
        self._open_window = open_window
        self._open_overview = open_overview
        self._quit = quit_app
        self.menu = QMenu()
        self.icon = QSystemTrayIcon(self)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self._live: bool | None = None
        self._stale = False
        self.menu.aboutToShow.connect(self._menu_shown)
        context.runs.changed.connect(self.update)
        context.flows.changed.connect(self.update)
        self.update()
        self._build_menu()
        self._stale = False

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
        """Update the icon and tooltip; rebuild the menu when it next opens."""
        items, _children = self._items()
        live = bool(items)
        if live != self._live:
            self._live = live
            self.icon.setIcon(icons.tray_icon(live))
        self.icon.setToolTip(tooltip(*counts(items)))
        self._stale = True

    def _items(self) -> tuple[list[Item], dict[str, list[Run]]]:
        return live_items(
            self._context.runs.live(),
            [*self._context.flows.flows(), *self._context.flows.panels()],
        )

    def _build_menu(self) -> None:
        items, children = self._items()
        self.menu.clear()
        status = QAction(status_line(*counts(items)), self.menu)
        status.setEnabled(False)
        self.menu.addAction(status)
        for item in items:
            if isinstance(item, Run):
                self._add_run(item)
            else:
                action = QAction(parent_line(item), self.menu)
                action.triggered.connect(self._open_overview)
                self.menu.addAction(action)
                for run in children[item.record["id"]]:
                    self._add_run(run, nested=True)
        self.menu.addSeparator()
        open_action = QAction("Open Ember", self.menu)
        open_action.triggered.connect(self._open_window)
        self.menu.addAction(open_action)
        quit_action = QAction("Quit Ember", self.menu)
        quit_action.triggered.connect(self._quit)
        self.menu.addAction(quit_action)

    def _add_run(self, run: Run, *, nested: bool = False) -> None:
        action = QAction(("    " if nested else "") + run_line(run), self.menu)
        action.triggered.connect(
            # A run closed while the menu was open cannot be attached; its
            # error shows in the window, so the window opens with it.
            lambda _checked=False, run_id=run.id: self._context.open_run_terminal(
                run_id, self._open_window
            )
        )
        self.menu.addAction(action)

    def _menu_shown(self) -> None:
        if self._stale:
            # GNOME can deliver a click after the menu closes. Keep its actions
            # in the menu until the next open, including their D-Bus menu items.
            self._build_menu()
            self._stale = False

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._open_window()
