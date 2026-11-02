"""The Overview screen: live runs with their screen, and the last finished runs.

A live row expands to show the run's screen mirror; one row is expanded at a
time, and the mirror closes when the row collapses, the run leaves the live
list, the screen is left or the window hides. A finished row expands to the
run's record. While the screen is hidden it does not redraw; it redraws the
next time it is shown.
"""

import time
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ember import sandbox
from ember.app import icons, theme, tray
from ember.app.mirror import MirrorView
from ember.app.screens.common import (
    chain_tab_order,
    discard,
    duration,
    engine_name,
    plural,
    screen_header,
    scrolled,
    titled_card,
    when,
)
from ember.app.widgets import Alert, Badge, Card, StateView, button
from ember.runs import record
from ember.runs.record import Run

if TYPE_CHECKING:
    from ember.app.context import AppContext

ELAPSED_MS = 30_000
"""How often the elapsed times of live runs are redrawn while shown."""

BADGES = {
    record.STARTING: ("Starting", "neutral"),
    record.RUNNING: ("Running", "info"),
    record.WAITING: ("Waiting for Claude", "warning"),
    record.CLOSED: ("Closed", "success"),
    record.FAILED: ("Failed", "danger"),
}
"""The badge word and tone of each run status."""

MIRROR_DESCRIPTION = "Screen of run {id}, read-only. Updates when the worker writes."


def worker_line(run: Run) -> str:
    """Return "<Engine> · <model> · <effort>", leaving out a missing effort."""
    parts = [engine_name(run.engine), run.model]
    if run.effort:
        parts.append(run.effort)
    return " · ".join(parts)


def branch_text(run: Run) -> str:
    """Return the run's branch, or "read-only"."""
    return "read-only" if run.read_only or not run.branch else run.branch


def live_detail(run: Run) -> str:
    """Return "<repository folder> · <branch or read-only> · turn <n>"."""
    folder = PurePosixPath(run.repo).name or run.repo
    return f"{folder} · {branch_text(run)} · turn {run.turn}"


def finished_detail(run: Run) -> str:
    """Return "<subject> · <branch or read-only>"."""
    return f"{run.subject} · {branch_text(run)}"


def _ended_at(run: Run) -> float:
    return run.updated_at if run.ended_at is None else run.ended_at


def finished_time(run: Run, now: float | None = None) -> str:
    """Return a finished run's end time and duration."""
    end = _ended_at(run)
    return f"{when(end, now)} · {duration(end - run.started_at)}"


def record_rows(run: Run, now: float | None = None) -> list[tuple[str, str]]:
    """Return a finished run's record as label and value rows."""
    return [
        ("Run", run.id),
        ("Subject", run.subject),
        ("Engine", worker_line(run)),
        ("Repository", run.repo),
        ("Branch", branch_text(run)),
        ("Sandbox", run.sandbox),
        (
            "Turns and commits",
            f"{plural(run.turn, 'turn')}, {plural(len(run.commits), 'commit')}",
        ),
        ("Started", when(run.started_at, now)),
        ("Ended", "Not recorded" if run.ended_at is None else when(run.ended_at, now)),
        ("Last exit", "None" if run.last_exit is None else str(run.last_exit)),
        ("Last reason", run.last_reason or "None"),
    ]


def unreadable_line(count: int) -> str:
    """Return the line about run records that could not be read."""
    if count == 1:
        return "1 run record could not be read."
    return f"{count} run records could not be read."


def sandbox_warning(engine: str) -> str:
    """Return the alert text for an engine that runs without a sandbox."""
    return (
        f"{engine_name(engine)} runs without a sandbox. Its workers can read "
        "every file you can, including logins and keys. Turn its sandbox on "
        "in Engines."
    )


class _Row(QWidget):
    """A run's row: an expand arrow, a badge, two lines, a time and actions."""

    def __init__(self, run: Run, what: str) -> None:
        super().__init__()
        self.run = run
        self._what = what
        self.expanded = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        header = QHBoxLayout()
        header.setSpacing(12)
        self.expand_button = button("", "ghost", size="sm")
        self.expand_button.setFixedWidth(theme.CONTROL_SM)
        header.addWidget(self.expand_button, 0, Qt.AlignmentFlag.AlignTop)
        self.badge = Badge("", "neutral")
        header.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        lines = QVBoxLayout()
        lines.setSpacing(2)
        self.title_label = QLabel()
        self.title_label.setFont(theme.font("label"))
        self.detail_label = QLabel()
        self.detail_label.setProperty("role", "secondary")
        self.detail_label.setWordWrap(True)
        lines.addWidget(self.title_label)
        lines.addWidget(self.detail_label)
        header.addLayout(lines, 1)
        self.time_label = QLabel()
        self.time_label.setProperty("role", "secondary")
        header.addWidget(self.time_label, 0, Qt.AlignmentFlag.AlignTop)
        self.action_box = QHBoxLayout()
        header.addLayout(self.action_box)
        outer.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(theme.CONTROL_SM + 12, 0, 0, 0)
        outer.addLayout(self.body)
        self._draw_arrow()
        theme.signals.changed.connect(self._draw_arrow)

    def set_run(self, run: Run) -> None:
        self.run = run
        text, tone = BADGES.get(run.status, (run.status, "neutral"))
        self.badge.set_tone(tone, text)

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self._draw_arrow()

    def _draw_arrow(self, *_args: object) -> None:
        name = "chevron-down" if self.expanded else "chevron-right"
        self.expand_button.setIcon(icons.icon(name, theme.color("text-secondary"), 16))
        verb = "Hide" if self.expanded else "Show"
        self.expand_button.setAccessibleName(
            f"{verb} {self._what} of run {self.run.id}"
        )
        self.expand_button.setToolTip(f"{verb} {self._what}")


class _LiveRow(_Row):
    """A live run's row; expanded, it holds the run's screen mirror."""

    def __init__(self, run: Run, open_terminal: Callable[[str], None]) -> None:
        super().__init__(run, "screen")
        self.mirror: MirrorView | None = None
        self.open_button = button("Open terminal", "secondary", size="sm")
        self.open_button.setIcon(
            icons.icon("terminal", theme.color("text-primary"), 16)
        )
        self.open_button.clicked.connect(lambda: open_terminal(self.run.id))
        self.action_box.addWidget(self.open_button)
        self.set_run(run)

    def set_run(self, run: Run) -> None:
        super().set_run(run)
        self.title_label.setText(worker_line(run))
        self.detail_label.setText(live_detail(run))
        self.update_elapsed()
        self.open_button.setAccessibleDescription(f"Open run {run.id} in a terminal")

    def update_elapsed(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.time_label.setText(duration(now - self.run.started_at))

    def open_mirror(self, env: Mapping[str, str]) -> None:
        if self.mirror is not None:
            return
        mirror = MirrorView(self.run.id, env)
        mirror.setAccessibleDescription(MIRROR_DESCRIPTION.format(id=self.run.id))
        self.body.addWidget(mirror)
        self.mirror = mirror
        self.set_expanded(True)
        mirror.start()

    def close_mirror(self) -> None:
        mirror, self.mirror = self.mirror, None
        if mirror is not None:
            mirror.stop()
            discard(mirror, self.body)
        self.set_expanded(False)


class _FinishedRow(_Row):
    """A finished run's row; expanded, it shows the run's record."""

    def __init__(self, run: Run) -> None:
        super().__init__(run, "details")
        self.details: QWidget | None = None
        self.set_run(run)
        self.title_label.setText(worker_line(run))
        self.detail_label.setText(finished_detail(run))
        self.time_label.setText(finished_time(run))

    def show_details(self, shown: bool) -> None:
        if shown and self.details is None:
            details = QWidget()
            grid = QGridLayout(details)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(4)
            for row, (label, value) in enumerate(record_rows(self.run)):
                name = QLabel(label)
                name.setProperty("role", "secondary")
                text = QLabel(value)
                text.setWordWrap(True)
                text.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(text, row, 1)
            grid.setColumnStretch(1, 1)
            self.body.addWidget(details)
            self.details = details
        elif not shown and self.details is not None:
            discard(self.details, self.body)
            self.details = None
        self.set_expanded(shown)


def _card(title: str) -> tuple[Card, QVBoxLayout, QLabel]:
    card, layout = titled_card(title)
    empty = QLabel()
    empty.setProperty("role", "muted")
    layout.addWidget(empty)
    rows = QVBoxLayout()
    rows.setSpacing(16)
    layout.addLayout(rows)
    return card, rows, empty


class OverviewScreen(QWidget):
    """The Overview screen: live runs and the last finished runs."""

    TITLE = "Overview"

    def __init__(self, context: AppContext) -> None:
        """Create the screen.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        layout = scrolled(self)
        self.subtitle = screen_header(layout, self.TITLE)

        self.alerts = QVBoxLayout()
        self.alerts.setSpacing(8)
        layout.addLayout(self.alerts)
        self.sandbox_alerts: dict[str, Alert] = {}

        self.unreadable_alert = Alert("info", unreadable_line(0))
        self.unreadable_alert.hide()
        layout.addWidget(self.unreadable_alert)

        self.state_view = StateView()
        layout.addWidget(self.state_view)

        self.live_card, self.live_rows_layout, self.live_empty = _card("Live")
        self.live_empty.setText("No runs live.")
        layout.addWidget(self.live_card)
        self.finished_card, self.finished_rows_layout, self.finished_empty = _card(
            "Finished"
        )
        self.finished_empty.setText("No finished runs yet.")
        layout.addWidget(self.finished_card)
        layout.addStretch(1)

        self.live_rows: dict[str, _LiveRow] = {}
        self.finished_rows: dict[str, _FinishedRow] = {}
        self.expanded_live: str | None = None
        self.expanded_finished: str | None = None
        self._stale = True
        self._sandbox_busy = False
        self._sandbox_queued = False

        self._elapsed = QTimer(self)
        self._elapsed.setInterval(ELAPSED_MS)
        self._elapsed.timeout.connect(self._update_elapsed)

        context.runs.changed.connect(self._runs_changed)
        context.settings_changed.connect(self._settings_changed)
        context.visible_changed.connect(self._visible_changed)
        self.refresh()

    # Showing

    def shown(self) -> None:
        """Redraw what changed while hidden and check the sandbox settings."""
        self._elapsed.start()
        if self._stale:
            self.refresh()
        self.check_sandboxes()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: D102
        super().hideEvent(event)
        self._elapsed.stop()
        self.collapse_live()

    def _visible_changed(self, visible: bool) -> None:
        if not visible:
            self._elapsed.stop()
            self.collapse_live()

    def _runs_changed(self) -> None:
        if self.isVisible():
            self.refresh()
        else:
            self._stale = True
            # A run that left the live list closes its mirror even while
            # hidden; the screen redraws when shown.
            if self.expanded_live is not None and self.expanded_live not in {
                run.id for run in self.context.runs.live()
            }:
                self.collapse_live()

    def _settings_changed(self, _snapshot: dict) -> None:
        if self.isVisible():
            self.check_sandboxes()

    # Drawing

    def refresh(self) -> None:
        """Redraw the subtitle, the live rows and the finished rows."""
        self._stale = False
        runs = self.context.runs
        live = runs.live()
        finished = runs.finished()
        self.subtitle.setText(tray.status_line(len(live)))
        unreadable = runs.unreadable()
        self.unreadable_alert.title_label.setText(unreadable_line(unreadable))
        self.unreadable_alert.setAccessibleName(unreadable_line(unreadable))
        self.unreadable_alert.setVisible(unreadable > 0)
        if not runs.loaded():
            self.live_card.hide()
            self.finished_card.hide()
            self.state_view.show_loading("Reading runs")
            return
        if not live and not finished:
            self._draw_live([])
            self._draw_finished([])
            self.live_card.hide()
            self.finished_card.hide()
            self.state_view.show_empty(
                "inbox",
                "No runs yet",
                "Runs appear here when Claude starts a worker with `emb run`.",
            )
            return
        self.state_view.clear()
        self.state_view.hide()
        self.live_card.show()
        self.finished_card.show()
        self._draw_live(live)
        self._draw_finished(finished)

    def _draw_live(self, live: list[Run]) -> None:
        wanted = {run.id for run in live}
        if self.expanded_live is not None and self.expanded_live not in wanted:
            self.collapse_live()
        for run_id in [key for key in self.live_rows if key not in wanted]:
            row = self.live_rows.pop(run_id)
            row.close_mirror()
            discard(row, self.live_rows_layout)
        for run in live:
            row = self.live_rows.get(run.id)
            if row is None:
                row = _LiveRow(run, self.context.open_run_terminal)
                row.expand_button.clicked.connect(
                    lambda _checked=False, key=run.id: self.toggle_live(key)
                )
                self.live_rows[run.id] = row
            else:
                row.set_run(run)
            self.live_rows_layout.removeWidget(row)
        for run in live:
            row = self.live_rows[run.id]
            self.live_rows_layout.addWidget(row)
            row.show()
        self.live_empty.setVisible(not live)
        self._chain_focus()

    def _draw_finished(self, finished: list[Run]) -> None:
        for row in self.finished_rows.values():
            discard(row, self.finished_rows_layout)
        self.finished_rows = {}
        ids = {run.id for run in finished}
        if self.expanded_finished not in ids:
            self.expanded_finished = None
        for run in finished:
            row = _FinishedRow(run)
            row.expand_button.clicked.connect(
                lambda _checked=False, key=run.id: self.toggle_finished(key)
            )
            self.finished_rows[run.id] = row
            self.finished_rows_layout.addWidget(row)
            if run.id == self.expanded_finished:
                row.show_details(True)
        self.finished_empty.setVisible(not finished)
        self._chain_focus()

    def _chain_focus(self) -> None:
        """Keep Tab in reading order: alerts, then live rows, then finished rows."""
        chain: list[QWidget] = []
        for alert in self.sandbox_alerts.values():
            chain.extend(alert.buttons)
        for row in self.live_rows.values():
            chain.extend((row.expand_button, row.open_button))
        chain.extend(row.expand_button for row in self.finished_rows.values())
        chain_tab_order(chain)

    def _update_elapsed(self) -> None:
        now = time.time()
        for row in self.live_rows.values():
            row.update_elapsed(now)

    # Expanding

    def toggle_live(self, run_id: str) -> None:
        """Expand a live row with its mirror, or collapse it; one at a time."""
        if self.expanded_live == run_id:
            self.collapse_live()
            return
        self.collapse_live()
        row = self.live_rows.get(run_id)
        if row is None:
            return
        self.expanded_live = run_id
        row.open_mirror(self.context.env)

    def collapse_live(self) -> None:
        """Collapse the expanded live row and close its mirror."""
        run_id, self.expanded_live = self.expanded_live, None
        if run_id is not None and run_id in self.live_rows:
            self.live_rows[run_id].close_mirror()

    def toggle_finished(self, run_id: str) -> None:
        """Expand a finished row with its record, or collapse it; one at a time."""
        previous = self.expanded_finished
        if previous is not None and previous in self.finished_rows:
            self.finished_rows[previous].show_details(False)
        if previous == run_id:
            self.expanded_finished = None
            return
        row = self.finished_rows.get(run_id)
        if row is None:
            self.expanded_finished = None
            return
        self.expanded_finished = run_id
        row.show_details(True)

    def mirror(self) -> MirrorView | None:
        """Return the open mirror, or `None`."""
        if self.expanded_live is None or self.expanded_live not in self.live_rows:
            return None
        return self.live_rows[self.expanded_live].mirror

    # Sandbox alerts

    def check_sandboxes(self) -> None:
        """Read each engine's sandbox method in a task and redraw the alerts.

        Only engines that are on are checked. A check asked for while one
        runs follows it once.
        """
        if self._sandbox_busy:
            self._sandbox_queued = True
            return
        engines = [
            engine
            for engine in sandbox.ENGINES
            if self.context.setting(f"engine.{engine}") == "on"
        ]
        self._sandbox_busy = True
        env = self.context.env

        def read() -> list[str]:
            return [
                engine
                for engine in engines
                if sandbox.method(engine, env=env) == sandbox.NONE
            ]

        self.context.tasks.run(read, self._sandboxes_read, self._sandboxes_failed)

    def _sandboxes_read(self, engines: list[str]) -> None:
        for engine in [key for key in self.sandbox_alerts if key not in engines]:
            alert = self.sandbox_alerts.pop(engine)
            discard(alert, self.alerts)
        for engine in engines:
            if engine in self.sandbox_alerts:
                continue
            alert = Alert(
                "warning",
                sandbox_warning(engine),
                actions=[("Open Engines", lambda: self.context.show_screen("engines"))],
            )
            self.sandbox_alerts[engine] = alert
        for engine in sandbox.ENGINES:
            if engine in self.sandbox_alerts:
                self.alerts.removeWidget(self.sandbox_alerts[engine])
                self.alerts.addWidget(self.sandbox_alerts[engine])
        self._chain_focus()
        self._sandbox_done()

    def _sandboxes_failed(self, _error: Exception) -> None:
        # The window shows settings read failures; the alerts stay as they were.
        self._sandbox_done()

    def _sandbox_done(self) -> None:
        self._sandbox_busy = False
        if self._sandbox_queued:
            self._sandbox_queued = False
            self.check_sandboxes()
