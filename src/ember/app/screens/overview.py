"""The Overview screen: runs, flows with tasks, and panels with members.

A live row expands to show the run's screen mirror; one row is expanded at a
time, and the mirror closes when the row collapses, the run leaves the live
list, the screen is left or the window hides. A finished row expands to the
run's record. Rows are kept per run id; a finished row is redrawn only when
a value it shows changed. The sandbox alerts come from the last settings
snapshot. While the screen is hidden it does not redraw; it redraws the
next time it is shown. When the runs folder cannot be read, the screen shows
the reason with Try again, which reads it again at once.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ember import sandbox
from ember.app import icons, theme, tray
from ember.app.flows import FlowItem, FlowsModel, PanelItem
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
from ember.app.widgets import Alert, Badge, Card, StateView, button, set_icon
from ember.panels import record as panel_record
from ember.runs import record
from ember.runs.record import Run

if TYPE_CHECKING:
    from ember.app.context import AppContext

ELAPSED_MS = 30_000
"""How often active run, flow and panel elapsed times are redrawn while shown."""

BADGES = {
    record.STARTING: ("Starting", "neutral"),
    record.RUNNING: ("Running", "info"),
    record.WAITING: ("Waiting for Claude", "warning"),
    record.CLOSED: ("Closed", "success"),
    record.FAILED: ("Failed", "danger"),
}
"""The badge word and tone of each run status."""

FLOW_BADGES = {
    "starting": ("Starting", "neutral"),
    "running": ("Running", "info"),
    "checking": ("Checking", "info"),
    "stopped": ("Waiting for Claude", "warning"),
    "failed": ("Failed", "danger"),
    "done": ("Done", "success"),
    "gone": ("Runner gone", "danger"),
}
TASK_WORDS = {
    status: status.capitalize()
    for status in (
        "pending",
        "writing",
        "reviewing",
        "fixing",
        "accepted",
        "merging",
        "merged",
        "stopped",
        "conflict",
        "blocked",
        "dropped",
    )
}
PANEL_BADGES = {
    "running": ("Running", "info"),
    "done": ("Done", "success"),
    "incomplete": ("Incomplete", "warning"),
    "gone": ("Process gone", "danger"),
}


def flow_title(flow: Mapping[str, Any]) -> str:
    """Return the repository, task count and merged count."""
    tasks = flow["tasks"]
    folder = PurePosixPath(flow["dir"]).name or flow["dir"]
    merged = sum(task["status"] == "merged" for task in tasks)
    return f"{folder} · {plural(len(tasks), 'task')} · {merged}/{len(tasks)} merged"


def flow_detail(flow: Mapping[str, Any]) -> str:
    """Return the flow branch, wave and any reason."""
    text = f"ember/flow/{flow['id']} · wave {flow['wave']}"
    return text + (f" · {flow['reason']}" if flow.get("reason") else "")


def task_line(flow: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    """Return the task's status, round and selected writer."""
    worker = task["writer"] or flow["writer"]
    word = TASK_WORDS.get(task["status"], task["status"])
    return (
        f"{task['id']} · {word} · round {task['round']} · "
        f"{engine_name(worker['engine'])} · {worker['model']}"
    )


def review_line(task: Mapping[str, Any]) -> str | None:
    """Return the last review's verdict and finding counts, if reviewed."""
    reviews = task["reviews"]
    if not reviews:
        return None
    last = reviews[-1]
    verdict = last.get("verdict", "unreadable")
    return (
        f"{plural(len(reviews), 'review')} · last {verdict} · "
        f"{last.get('must', 0)} must, {last.get('should', 0)} should, "
        f"{last.get('lead', 0)} lead"
    )


def panel_title(panel: Mapping[str, Any]) -> str:
    """Return the panel member and answer counts."""
    members = panel["members"]
    answered = sum(member["status"] == "answered" for member in members)
    return (
        f"Panel · {plural(len(members), 'member')} · {answered}/{len(members)} answered"
    )


def panel_detail(item: PanelItem) -> str:
    """Return the repository and the first eighty question characters."""
    folder = PurePosixPath(item.record["dir"]).name or item.record["dir"]
    return folder + (f" · {item.question[:80]}" if item.question else "")


def member_line(member: Mapping[str, Any]) -> str:
    """Return a member's worker, status and rounded duration."""
    seconds = "-" if member["seconds"] is None else str(round(member["seconds"]))
    return (
        f"{member['n']}. {engine_name(member['engine'])} · {member['model']} · "
        f"{member['effort'] or 'default'} · "
        f"{member['status'].capitalize()} · {seconds}s"
    )


def item_time(started: float, ended: float | None, now: float | None = None) -> str:
    """Return elapsed time, or end time and duration for an ended record."""
    if ended is None:
        return duration((time.time() if now is None else now) - started)
    return f"{when(ended, now)} · {duration(ended - started)}"


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


def read_failure(reason: str) -> str:
    """Return the error text for a runs folder that could not be read."""
    return (
        f"Ember could not read the run records: {reason}. Try again; if this "
        "keeps happening, check the runs folder's permissions."
    )


def sandbox_warning(engine: str) -> str:
    """Return the alert text for an engine that runs without a sandbox."""
    return (
        f"{engine_name(engine)} runs without a sandbox. Its workers can read "
        "every file you can, including logins and keys. Turn its sandbox on "
        "in Engines."
    )


class _ExpandableRow(QWidget):
    """An expand arrow, badge, two lines, time, actions and optional body."""

    def __init__(self, item_id: str, kind: str, what: str) -> None:
        """Build the shared header and body layout."""
        super().__init__()
        self._item_id = item_id
        self._kind = kind
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

    def set_expanded(self, expanded: bool) -> None:
        """Store the expansion state and redraw the arrow."""
        self.expanded = expanded
        self._draw_arrow()

    def _draw_arrow(self, *_args: object) -> None:
        """Draw the expansion icon and its accessible description."""
        name = "chevron-down" if self.expanded else "chevron-right"
        self.expand_button.setIcon(icons.icon(name, theme.color("text-secondary"), 16))
        verb = "Hide" if self.expanded else "Show"
        self.expand_button.setAccessibleName(
            f"{verb} {self._what} of {self._kind} {self._item_id}"
        )
        self.expand_button.setToolTip(f"{verb} {self._what}")


class _Row(_ExpandableRow):
    """The shared header of a live or finished run."""

    def __init__(self, run: Run, what: str) -> None:
        """Build a run row without drawing its values."""
        self.run = run
        super().__init__(run.id, "run", what)

    def set_run(self, run: Run) -> None:
        """Store the run and draw its badge."""
        self.run = run
        text, tone = BADGES.get(run.status, (run.status, "neutral"))
        self.badge.set_tone(tone, text)


class _LiveRow(_Row):
    """A live run's row; expanded, it holds the run's screen mirror."""

    def __init__(self, run: Run, open_terminal: Callable[[str], None]) -> None:
        super().__init__(run, "screen")
        self.mirror: MirrorView | None = None
        self.open_button = button("Open terminal", "secondary", size="sm")
        set_icon(
            self.open_button, icons.icon("terminal", theme.color("text-primary"), 16)
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
        self.shown_values: tuple[object, ...] = ()
        self.set_run(run)

    @staticmethod
    def values(run: Run) -> tuple[object, ...]:
        """Return everything the row shows of a run, expanded or not."""
        return (
            run.status,
            worker_line(run),
            finished_detail(run),
            finished_time(run),
            tuple(record_rows(run)),
        )

    def set_run(self, run: Run) -> None:
        """Show a run's record; nothing is redrawn when no shown value changed."""
        values = self.values(run)
        self.run = run
        if values == self.shown_values:
            return
        self.shown_values = values
        super().set_run(run)
        self.title_label.setText(worker_line(run))
        self.detail_label.setText(finished_detail(run))
        self.time_label.setText(finished_time(run))
        if self.details is not None:
            self._drop_details()
            self._add_details()

    def show_details(self, shown: bool) -> None:
        if shown and self.details is None:
            self._add_details()
        elif not shown and self.details is not None:
            self._drop_details()
        self.set_expanded(shown)

    def _add_details(self) -> None:
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
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text, row, 1)
        grid.setColumnStretch(1, 1)
        self.body.addWidget(details)
        self.details = details

    def _drop_details(self) -> None:
        discard(self.details, self.body)
        self.details = None


class _RecordRow(_ExpandableRow):
    """A flow or panel row whose expanded body survives unrelated changes."""

    def __init__(self, item: FlowItem | PanelItem, screen: OverviewScreen) -> None:
        """Create a record row with read-only links to shown runs."""
        self.item = item
        self.overview = screen
        kind = "flow" if isinstance(item, FlowItem) else "panel"
        super().__init__(item.record["id"], kind, "details")
        self.title_label.setWordWrap(True)
        for label in (self.title_label, self.detail_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            policy = label.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            label.setSizePolicy(policy)
        self.details: QWidget | None = None
        self.links: list[QPushButton] = []
        self.shown_values: tuple[object, ...] = ()
        self.set_item(item)

    def entries(self) -> list[tuple[str, bool, str | None]]:
        """Return body lines with secondary style and optional run id."""
        raise NotImplementedError

    def set_item(self, item: FlowItem | PanelItem) -> None:
        """Draw changed values and refresh available run links."""
        self.item = item
        data = item.record
        if isinstance(item, FlowItem):
            badges = FLOW_BADGES
            title, detail = flow_title(data), flow_detail(data)
        else:
            badges = PANEL_BADGES
            title, detail = panel_title(data), panel_detail(item)
        badge = badges.get(
            "gone" if item.gone else data["status"], (data["status"], "neutral")
        )
        entries = self.entries()
        links = tuple(
            run for _, _, run in entries if run and self.overview.has_run(run)
        )
        values = (
            badge,
            title,
            detail,
            data["started_at"],
            data["ended_at"],
            tuple(entries),
            links,
        )
        self.update_elapsed()
        if values == self.shown_values:
            return
        self.shown_values = values
        self.badge.set_tone(badge[1], badge[0])
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        if self.expanded:
            self.show_details(False)
            self.show_details(True)

    def update_elapsed(self, now: float | None = None) -> None:
        """Update the time label without rebuilding the body."""
        data = self.item.record
        ended = data["ended_at"]
        if ended is None and data["status"] in ("done", "failed", "incomplete"):
            ended = data["updated_at"]
        self.time_label.setText(item_time(data["started_at"], ended, now))

    def show_details(self, shown: bool) -> None:
        """Expand or collapse the selectable body and its Show run buttons."""
        if self.details is not None:
            discard(self.details, self.body)
            self.details = None
        self.links = []
        if shown:
            details = QWidget()
            layout = QVBoxLayout(details)
            layout.setContentsMargins(0, 0, 0, 0)
            for text, secondary, run_id in self.entries():
                line = QHBoxLayout()
                label = QLabel(text)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                policy = label.sizePolicy()
                policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
                label.setSizePolicy(policy)
                label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                if secondary:
                    label.setProperty("role", "secondary")
                line.addWidget(label, 1)
                if run_id and self.overview.has_run(run_id):
                    link = button("Show run", "secondary", size="sm")
                    link.setAccessibleName(f"Show run {run_id}")
                    link.clicked.connect(
                        lambda _checked=False, key=run_id: self.overview.show_run(key)
                    )
                    line.addWidget(link)
                    self.links.append(link)
                layout.addLayout(line)
            self.body.addWidget(details)
            self.details = details
        self.set_expanded(shown)


class _FlowRow(_RecordRow):
    """A flow with task status, reviews and current worker runs."""

    def entries(self) -> list[tuple[str, bool, str | None]]:
        """Return each task's lines in plan order."""
        result: list[tuple[str, bool, str | None]] = []
        for task in self.item.record["tasks"]:
            result.append((task_line(self.item.record, task), False, None))
            if task["reason"]:
                result.append((task["reason"], True, None))
            review = review_line(task)
            if review:
                result.append((review, True, None))
            for label, field in (("Writer", "writer_run"), ("Review", "reviewer_run")):
                if task[field]:
                    result.append((f"{label} run {task[field]}", True, task[field]))
        return result


class _PanelRow(_RecordRow):
    """A panel with member answers, omissions and its summary path."""

    def entries(self) -> list[tuple[str, bool, str | None]]:
        """Return member details followed by omissions and the summary path."""
        result: list[tuple[str, bool, str | None]] = []
        data = self.item.record
        for member in data["members"]:
            result.append((member_line(member), False, None))
            if member["reason"]:
                result.append((member["reason"], True, None))
            if member["changed"]:
                result.append(("Changed: " + ", ".join(member["changed"]), True, None))
            if member["run"]:
                result.append((f"Run {member['run']}", True, member["run"]))
        for omitted in data["left_out"]:
            result.append(
                (f"Left out {omitted['value']}: {omitted['reason']}", True, None)
            )
        folder = panel_record.folder(data["id"], self.overview.context.env)
        result.append((f"Summary: {folder / 'summary.md'}", True, None))
        return result


def _place(layout: QVBoxLayout, rows: Sequence[QWidget]) -> None:
    """Put rows in a layout in order, moving them only when the order differs."""
    if [layout.indexOf(row) for row in rows] == list(range(len(rows))):
        return
    for row in rows:
        layout.removeWidget(row)
    for row in rows:
        layout.addWidget(row)
        row.show()


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
    """The Overview screen: flows, panels, live runs and finished runs."""

    TITLE = "Overview"

    def __init__(self, context: AppContext) -> None:
        """Create the screen.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        self.records = FlowsModel(context.env, context.tasks, parent=self)
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

        self.records_alert = Alert(
            "danger", "", actions=[("Try again", self.retry_records)]
        )
        self.records_alert.hide()
        layout.addWidget(self.records_alert)
        self.flows_card, self.flow_rows_layout, self.flows_unreadable = _card("Flows")
        self.panels_card, self.panel_rows_layout, self.panels_unreadable = _card(
            "Panels"
        )
        layout.addWidget(self.flows_card)
        layout.addWidget(self.panels_card)
        self.flow_rows: dict[str, _RecordRow] = {}
        self.panel_rows: dict[str, _RecordRow] = {}

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

        self._elapsed = QTimer(self)
        self._elapsed.setInterval(ELAPSED_MS)
        self._elapsed.timeout.connect(self._update_elapsed)

        self.records.changed.connect(self._runs_changed)
        self.records.failed.connect(self._runs_changed)
        context.runs.changed.connect(self._runs_changed)
        context.runs.failed.connect(self._runs_changed)
        context.settings_changed.connect(self._settings_changed)
        context.visible_changed.connect(self._visible_changed)
        self.refresh()

    # Showing

    def shown(self) -> None:
        """Redraw what changed while hidden and the sandbox alerts."""
        self.records.set_active(True)
        self._elapsed.start()
        if self._stale:
            self.refresh()
        self.check_sandboxes()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: D102
        super().hideEvent(event)
        self.records.set_active(False)
        self._elapsed.stop()
        self.collapse_live()

    def _visible_changed(self, visible: bool) -> None:
        if not visible:
            self.records.set_active(False)
            self._elapsed.stop()
            self.collapse_live()
        elif self.isVisible():
            self.shown()

    def _runs_changed(self, *_args: object) -> None:
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
        """Redraw the subtitle, records and run rows after either model changes."""
        self._stale = False
        runs = self.context.runs
        live = runs.live()
        finished = runs.finished()
        self.subtitle.setText(tray.status_line(len(live)))
        unreadable = runs.unreadable()
        self.unreadable_alert.title_label.setText(unreadable_line(unreadable))
        self.unreadable_alert.setAccessibleName(unreadable_line(unreadable))
        error = runs.error()
        self.unreadable_alert.setVisible(unreadable > 0 and error is None)
        if error is not None:
            self.collapse_live()
            self._show_state()
            self.state_view.show_error(read_failure(error), self.retry_runs)
            return
        if not runs.loaded() or (
            not self.records.loaded() and self.records.error() is None
        ):
            self._show_state()
            self.state_view.show_loading("Reading runs, flows and panels")
            return
        if (
            not live
            and not finished
            and not self.records.flows()
            and not self.records.panels()
            and not self.records.unreadable_flows()
            and not self.records.unreadable_panels()
            and self.records.error() is None
        ):
            self._draw_live([])
            self._draw_finished([])
            self._draw_records()
            self._show_state()
            self.state_view.show_empty(
                "inbox",
                "No runs yet",
                "Runs, flows and panels appear here when Claude starts them with "
                "`emb run`, `emb flow` or `emb panel`.",
            )
            return
        self.state_view.clear()
        self.state_view.hide()
        self.live_card.show()
        self.finished_card.show()
        self._draw_live(live)
        self._draw_finished(finished)
        self._draw_records()
        self._chain_focus()

    def _show_state(self) -> None:
        """Hide the cards and give the state view their place."""
        self.flows_card.hide()
        self.panels_card.hide()
        self.records_alert.hide()
        self.live_card.hide()
        self.finished_card.hide()
        self.state_view.show()

    def retry_runs(self) -> None:
        """Read the runs folder again now, showing that it is being read."""
        self.state_view.show_loading("Reading runs")
        self.context.runs.reload()

    def retry_records(self) -> None:
        """Read the flow and panel records again now."""
        self.records.reload()

    def _draw_records(self) -> None:
        """Reuse rows by id and refresh links after the run rows are drawn."""
        error = self.records.error()
        self.records_alert.setVisible(error is not None)
        if error is not None:
            text = f"Ember could not read the flow and panel records: {error}."
            self.records_alert.title_label.setText(text)
            self.records_alert.setAccessibleName(text)
            self.flows_card.hide()
            self.panels_card.hide()
            return
        for items, rows, layout, card, unreadable, count, kind, row_type in (
            (
                self.records.flows(),
                self.flow_rows,
                self.flow_rows_layout,
                self.flows_card,
                self.flows_unreadable,
                self.records.unreadable_flows(),
                "flow",
                _FlowRow,
            ),
            (
                self.records.panels(),
                self.panel_rows,
                self.panel_rows_layout,
                self.panels_card,
                self.panels_unreadable,
                self.records.unreadable_panels(),
                "panel",
                _PanelRow,
            ),
        ):
            wanted = {item.record["id"] for item in items}
            for key in list(rows):
                if key not in wanted:
                    discard(rows.pop(key), layout)
            for item in items:
                key = item.record["id"]
                if key not in rows:
                    row = row_type(item, self)
                    row.expand_button.clicked.connect(
                        lambda _checked=False, selected=row, group=rows: (
                            self._toggle_record(selected, group)
                        )
                    )
                    rows[key] = row
                else:
                    rows[key].set_item(item)
            ordered = [(item.record["id"], rows[item.record["id"]]) for item in items]
            rows.clear()
            rows.update(ordered)
            _place(layout, list(rows.values()))
            unreadable.setText(f"{plural(count, kind + ' record')} could not be read.")
            unreadable.setVisible(count > 0)
            card.setVisible(bool(items) or count > 0)

    def _toggle_record(self, row: _RecordRow, rows: Mapping[str, _RecordRow]) -> None:
        """Expand one row in its card, or collapse the already expanded row."""
        shown = not row.expanded
        for other in rows.values():
            if other.expanded:
                other.show_details(False)
        row.show_details(shown)
        self._chain_focus()

    def has_run(self, run_id: str) -> bool:
        """Return whether a live or finished row for this run is shown."""
        return run_id in self.live_rows or run_id in self.finished_rows

    def show_run(self, run_id: str) -> None:
        """Expand a shown run's mirror or record and scroll to its row."""
        row: QWidget | None = None
        if run_id in self.live_rows:
            if self.expanded_live != run_id:
                self.toggle_live(run_id)
            row = self.live_rows[run_id]
        elif run_id in self.finished_rows:
            if self.expanded_finished != run_id:
                self.toggle_finished(run_id)
            row = self.finished_rows[run_id]
        area = self.findChild(QScrollArea)
        if row is not None and area is not None:
            area.ensureWidgetVisible(row)

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
        _place(self.live_rows_layout, [self.live_rows[run.id] for run in live])
        self.live_empty.setVisible(not live)
        self._chain_focus()

    def _draw_finished(self, finished: list[Run]) -> None:
        """Draw the finished rows, reusing each run's row.

        A row is redrawn only when a value it shows changed, so focus, an
        expanded record and selected text survive a reload.
        """
        wanted = {run.id for run in finished}
        if self.expanded_finished not in wanted:
            self.expanded_finished = None
        for run_id in [key for key in self.finished_rows if key not in wanted]:
            discard(self.finished_rows.pop(run_id), self.finished_rows_layout)
        for run in finished:
            row = self.finished_rows.get(run.id)
            if row is None:
                row = _FinishedRow(run)
                row.expand_button.clicked.connect(
                    lambda _checked=False, key=run.id: self.toggle_finished(key)
                )
                self.finished_rows[run.id] = row
            else:
                row.set_run(run)
        self.finished_rows = {run.id: self.finished_rows[run.id] for run in finished}
        _place(self.finished_rows_layout, list(self.finished_rows.values()))
        expanded = self.finished_rows.get(self.expanded_finished or "")
        if expanded is not None and expanded.details is None:
            expanded.show_details(True)
        self.finished_empty.setVisible(not finished)
        self._chain_focus()

    def _chain_focus(self) -> None:
        """Keep Tab in reading order through alerts, records and runs."""
        chain: list[QWidget] = []
        for alert in self.sandbox_alerts.values():
            chain.extend(alert.buttons)
        if not self.records_alert.isHidden():
            chain.extend(self.records_alert.buttons)
        for item in (*self.flow_rows.values(), *self.panel_rows.values()):
            chain.append(item.expand_button)
            chain.extend(item.links)
        for row in self.live_rows.values():
            chain.extend((row.expand_button, row.open_button))
        chain.extend(row.expand_button for row in self.finished_rows.values())
        chain_tab_order(chain)

    def _update_elapsed(self) -> None:
        now = time.time()
        for row in self.live_rows.values():
            row.update_elapsed(now)
        for item in (*self.flow_rows.values(), *self.panel_rows.values()):
            if item.item.record["ended_at"] is None:
                item.update_elapsed(now)

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
        """Redraw the alerts from the last settings snapshot.

        An engine that is on and whose `sandbox.<engine>` setting gives no
        sandbox has an alert. Nothing is shown before the first snapshot.
        """
        engines = []
        if self.context.settings_ready():
            engines = [
                engine
                for engine in sandbox.ENGINES
                if self.context.setting(f"engine.{engine}") == "on"
                and sandbox.provider_of(
                    engine, str(self.context.setting(f"sandbox.{engine}"))
                )
                == sandbox.NONE
            ]
        for engine in [key for key in self.sandbox_alerts if key not in engines]:
            discard(self.sandbox_alerts.pop(engine), self.alerts)
        for engine in engines:
            if engine in self.sandbox_alerts:
                continue
            alert = Alert(
                "warning",
                sandbox_warning(engine),
                actions=[("Open Engines", lambda: self.context.show_screen("engines"))],
            )
            self.sandbox_alerts[engine] = alert
        _place(self.alerts, [self.sandbox_alerts[engine] for engine in engines])
        self._chain_focus()
