"""Live and Finished runs, flows and panels with nested worker rows.

Models stay active while the app runs. Hidden screens defer redraws until
shown.
"""

import dataclasses
import datetime
import json
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QHideEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ember import prune, sandbox
from ember.app import icons, state, theme
from ember.app.flows import FlowItem, PanelItem, plan_wave
from ember.app.runs import DONE, FINISHED_LIMIT, LIVE
from ember.app.screens.common import (
    FittingRow,
    ToneLine,
    chain_tab_order,
    discard,
    engine_name,
    inline,
    plural,
    screen_header,
    scrolled,
    titled_card,
    when,
)
from ember.app.widgets import (
    Alert,
    Badge,
    Card,
    ElidedLabel,
    Picker,
    StateView,
    button,
    set_icon,
)
from ember.flows import record as flow_record
from ember.panels import record as panel_record
from ember.runs import record
from ember.runs.record import Run

if TYPE_CHECKING:
    from ember.app.context import AppContext

CLEAR_TIP = "Remove shown records"
"""Clear's tooltip and accessible description: what it does."""

FINISHED_PREVIEW = 3
"""Finished items shown while the Finished list is not open."""

ELAPSED_MS = 1_000
"""How often active run, flow and panel elapsed times are redrawn while shown."""

ORPHANED = "orphaned"
"""The badge key of a run whose starting command ended without collecting it."""

BADGES = {
    record.STARTING: ("Starting", "neutral"),
    record.RUNNING: ("Running", "info"),
    record.WAITING: ("Waiting for Claude", "warning"),
    record.CLOSED: ("Done", "success"),
    record.FAILED: ("Failed", "danger"),
    ORPHANED: ("Orphaned", "warning"),
}
"""The badge word and tone of each run status, and of an orphaned run."""


def run_badge(run: Run) -> tuple[str, str]:
    """Return a run's badge, including flow waits and orphaned parents."""
    if record.waiting_for_flow(run) is not None:
        return BADGES[record.WAITING]
    key = ORPHANED if record.orphaned(run) else run.status
    return BADGES.get(key, (run.status, "neutral"))


FLOW_BADGES = {
    "starting": ("Starting", "neutral"),
    "running": ("Running", "info"),
    "checking": ("Running", "info"),
    "stopped": ("Waiting for Claude", "warning"),
    "failed": ("Failed", "danger"),
    "done": ("Done", "success"),
    "gone": ("Failed", "danger"),
    "cancelled": ("Incomplete", "incomplete"),
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
    "incomplete": ("Incomplete", "incomplete"),
    "gone": ("Failed", "danger"),
}


def flow_title(flow: Mapping[str, Any]) -> str:
    """Return "Flow · <repository> · <tasks> · <merged>/<tasks> merged"."""
    tasks = flow["tasks"]
    folder = PurePosixPath(flow["dir"]).name or flow["dir"]
    merged = sum(task["status"] == "merged" for task in tasks)
    return (
        f"Flow · {folder} · {plural(len(tasks), 'task')} · {merged}/{len(tasks)} merged"
    )


def flow_detail(flow: Mapping[str, Any]) -> str:
    """Return the flow branch, active plan wave and any reason."""
    text = f"ember/flow/{flow['id']}"
    wave = plan_wave(flow)
    if wave is not None:
        text += f" · wave {wave[0]} of {wave[1]}"
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
    """Return the panel's repository folder."""
    folder = PurePosixPath(item.record["dir"]).name or item.record["dir"]
    return folder


def newest_check_log(folder: Path) -> Path | None:
    """Prefer the highest numeric wave and attempt, else the newest mtime."""
    candidates: list[tuple[tuple[int, int, int, int, str], Path]] = []
    for path in folder.glob("check-*.log"):
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        match = re.fullmatch(r"check-([0-9]+)-([0-9]+)\.log", path.name)
        wave, attempt = (int(part) for part in match.groups()) if match else (0, 0)
        key = (int(match is not None), wave, attempt, info.st_mtime_ns, path.name)
        candidates.append((key, path))
    return max(candidates)[1] if candidates else None


def member_line(member: Mapping[str, Any]) -> str:
    """Return a member's worker, status and rounded duration."""
    seconds = "-" if member["seconds"] is None else str(round(member["seconds"]))
    return (
        f"{member['n']}. {engine_name(member['engine'])} · {member['model']} · "
        f"{member['effort'] or 'default'} · "
        f"{member['status'].capitalize()} · {seconds}s"
    )


def elapsed(seconds: float) -> str:
    """Return a running time to the second: "45 s", "4 min 12 s", "1 h 5 min 3 s"."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min {secs} s"
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def _clock(stamp: float) -> str:
    moment = datetime.datetime.fromtimestamp(stamp)
    return f"{moment.hour:02d}:{moment.minute:02d}"


def span_text(started: float, ended: float | None, now: float | None = None) -> str:
    """Return a record's times for its detail line.

    An ended record reads "08:17 to 08:33"; its duration shows in the time
    column. A live one reads "started 08:01 · 4 min 12 s". The start names
    its day only when it falls on an earlier day than the end, or than now
    while live: "yesterday 23:50 to 00:10".
    """
    now = time.time() if now is None else now
    last = now if ended is None else ended
    start_day = datetime.datetime.fromtimestamp(started).date()
    if start_day < datetime.datetime.fromtimestamp(last).date():
        start = inline(when(started, now))
    else:
        start = _clock(started)
    if ended is None:
        return f"started {start} · {elapsed(now - started)}"
    return f"{start} to {_clock(ended)}"


def took(seconds: float) -> str:
    """Return a finished duration: "45 s", "2 min 5 s", or "1 h 12 min" from an hour."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def end_text(started: float, ended: float | None) -> str:
    """Return the right column's text: "Elapsed 2 min 5 s", or "" while live."""
    return "" if ended is None else f"Elapsed {took(ended - started)}"


def worker_line(run: Run) -> str:
    """Return "<Engine> · <model> · <effort>", leaving out a missing effort."""
    parts = [engine_name(run.engine), run.model]
    if run.effort:
        parts.append(run.effort)
    return " · ".join(parts)


def branch_text(run: Run) -> str:
    """Return the run's branch, or "read-only"."""
    return "read-only" if run.read_only or not run.branch else run.branch


def run_title(run: Run) -> str:
    """Return "Run · <subject>", or "Run · <worker>" for a run without one."""
    subject = " ".join(run.subject.split())
    return f"Run · {subject}" if subject else f"Run · {worker_line(run)}"


def run_ended(run: Run) -> float | None:
    """Return when an ended run ended, or `None` while it is live."""
    if run.status not in DONE:
        return None
    return run.updated_at if run.ended_at is None else run.ended_at


def run_result(run: Run) -> str:
    """Return what a run produced so far, or why it failed.

    Live: "turn 2 · 1 commit" or "turn 2 · read-only". Ended: "3 commits" or
    "read-only". Failed: its last reason.
    """
    if run.status == record.FAILED:
        return run.last_reason or "No reason recorded"
    work = "read-only" if run.read_only else plural(len(run.commits), "commit")
    return work if run.status in DONE else f"turn {run.turn} · {work}"


def run_detail(run: Run, now: float | None = None) -> str:
    """Return "<worker> · <repository folder> · <result> · <times>".

    Flow waits name the awaited answer first. Otherwise the worker leads
    the line when the title holds the run's subject.
    """
    folder = PurePosixPath(run.repo).name or run.repo
    times = span_text(run.started_at, run_ended(run), now)
    worker = f"{worker_line(run)} · " if run.subject.strip() else ""
    detail = f"{worker}{folder} · {run_result(run)} · {times}"
    waiting = record.waiting_for_flow(run)
    if waiting is not None:
        return f"Waiting for Claude's answer to flow {waiting} · {detail}"
    return detail


def parent_label(parent: FlowItem | PanelItem) -> str:
    """Return a parent's type and first fact: "Panel · 7 members", "Flow · ember"."""
    title = (
        flow_title(parent.record)
        if isinstance(parent, FlowItem)
        else panel_title(parent.record)
    )
    return " · ".join(title.split(" · ")[:2])


def run_parents(parents: list[FlowItem | PanelItem]) -> dict[str, FlowItem | PanelItem]:
    """Return each worker's oldest parent by run id."""
    result: dict[str, FlowItem | PanelItem] = {}
    for parent in sorted(parents, key=item_started):
        for run_id in parent.run_ids:
            result.setdefault(run_id, parent)
    return result


MISSING_FILES = {
    "Summary": "the panel writes summary.md when it ends.",
    "Briefs": "the flow has written no task briefs yet.",
    "Check logs": "the flow has run no repository check yet.",
}
"""Why each flow or panel file button has nothing to open yet."""

CODE_ROWS = ("Run", "Repository", "Branch")
"""Run record rows whose values are a run id, a path or a branch, in `type-code`."""


def record_rows(run: Run, now: float | None = None) -> list[tuple[str, str]]:
    """Return a run's record as label and value rows."""
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
        ("Ended", _ended_text(run, now)),
        ("Last exit", "None" if run.last_exit is None else str(run.last_exit)),
        ("Last reason", run.last_reason or "None"),
    ]


def _ended_text(run: Run, now: float | None) -> str:
    if run.ended_at is not None:
        return when(run.ended_at, now)
    return "Not yet" if run.status in LIVE else "Not recorded"


def unreadable_line(runs: int, flows: int = 0, panels: int = 0) -> str:
    """Name only the non-zero unreadable record counts."""
    parts = [
        plural(count, kind)
        for count, kind in ((runs, "run"), (flows, "flow"), (panels, "panel"))
        if count
    ]
    return ", ".join(parts) + " could not be read."


type Item = Run | FlowItem | PanelItem


def item_id(item: Item) -> str:
    """Return the record id of a row."""
    return item.id if isinstance(item, Run) else item.record["id"]


def item_started(item: Item) -> tuple[float, str]:
    """Return start time and id for oldest-first ordering."""
    start = item.started_at if isinstance(item, Run) else item.record["started_at"]
    return start, item_id(item)


def item_ended(item: Item) -> bool:
    """Return whether the stored status belongs in Finished."""
    if isinstance(item, Run):
        return item.status in DONE
    ended = flow_record.ENDED if isinstance(item, FlowItem) else ("done", "incomplete")
    return item.record["status"] in ended


def item_end_key(item: Item) -> tuple[float, float, str]:
    """Return end time, update time and id for newest-first ordering."""
    if isinstance(item, Run):
        end = item.updated_at if item.ended_at is None else item.ended_at
        return end, item.updated_at, item.id
    data = item.record
    end = data["updated_at"] if data["ended_at"] is None else data["ended_at"]
    return end, data["updated_at"], data["id"]


def sections(
    runs: list[Run], parents: list[FlowItem | PanelItem]
) -> tuple[list[Item], list[Item], dict[str, list[Run]]]:
    """Select top-level rows and assign workers to their oldest parent.

    A live worker whose parent has ended is a top-level row, so it shows in
    Live even when its parent is not among the shown Finished items.
    """
    owners: dict[str, str] = {}
    children: dict[str, list[Run]] = {item_id(parent): [] for parent in parents}
    ended = {item_id(parent) for parent in parents if item_ended(parent)}
    for parent in sorted(parents, key=item_started):
        for run_id in parent.run_ids:
            owners.setdefault(run_id, item_id(parent))
    top: list[Item] = list(parents)
    for run in sorted(runs, key=item_started):
        owner = owners.get(run.id)
        if owner is None or (owner in ended and not item_ended(run)):
            top.append(run)
        else:
            children[owner].append(run)
    live = sorted((item for item in top if not item_ended(item)), key=item_started)
    finished = sorted(
        (item for item in top if item_ended(item)), key=item_end_key, reverse=True
    )[:FINISHED_LIMIT]
    return live, finished, children


def matches_filters(
    item: Item, children: list[Run], kind: str, status: str, engine: str
) -> bool:
    """Match a top-level row's type, chip and readable workers' engines."""
    if isinstance(item, Run):
        item_kind = "Run"
        chip = run_badge(item)[0]
        workers = [item]
    else:
        item_kind = "Flow" if isinstance(item, FlowItem) else "Panel"
        badges = FLOW_BADGES if isinstance(item, FlowItem) else PANEL_BADGES
        chip = badges["gone" if item.gone else item.record["status"]][0]
        workers = children
    return (
        (kind == "all" or kind == item_kind)
        and (status == "all" or status == chip)
        and (engine == "all" or any(run.engine == engine for run in workers))
    )


def prune_text(result: prune.Result, *, dry_run: bool = False) -> str:
    """List records, skips, retained branches and worktrees, then errors."""
    verb = "Would remove" if dry_run else "Removed"
    lines = [f"{verb} {item['kind']} {item['id']}" for item in result.removed]
    if not lines:
        lines.append("No records removed." if not dry_run else "No records to remove.")
    lines.extend(f"Skipped {item['id']}: {item['reason']}" for item in result.skipped)
    printed: set[str] = set()
    for item in result.branches:
        lines.append(f"Branch {item['branch']}: {item['command']}")
        printed.add(item["branch"])
        for worktree in result.worktrees:
            if worktree["branch"] == item["branch"]:
                lines.append(
                    f"  Worktree {worktree['worktree']}: {worktree['command']}"
                )
    for item in result.worktrees:
        if item["branch"] not in printed:
            lines.extend(
                [
                    f"Branch {item['branch']}",
                    f"  Worktree {item['worktree']}: {item['command']}",
                ]
            )
    lines.extend(f"Error: {error}" for error in result.errors)
    return "\n".join(lines)


class _PruneDialog(QDialog):
    """A selectable report with a screen-height-capped scroll area."""

    def __init__(
        self, title: str, text: str, parent: QWidget, *, confirm: bool = False
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        layout = QVBoxLayout(self)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.report = QLabel(text)
        self.report.setTextFormat(Qt.TextFormat.PlainText)
        self.report.setWordWrap(True)
        self.report.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.report.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.report.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.area.setWidget(self.report)
        layout.addWidget(self.area)
        self.buttons = QDialogButtonBox()
        if confirm:
            remove = self.buttons.addButton(
                "Remove", QDialogButtonBox.ButtonRole.AcceptRole
            )
            remove.setAutoDefault(False)
            cancel = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
            cancel.setDefault(True)
            cancel.setFocus()
        else:
            self.buttons.addButton(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        available = self.screen().availableGeometry()
        width = min(
            max(680, self.fontMetrics().horizontalAdvance(title) + 100),
            available.width() - 80,
        )
        margins = layout.contentsMargins()
        overhead = (
            margins.top()
            + margins.bottom()
            + layout.spacing()
            + self.buttons.sizeHint().height()
        )
        content_width = width - margins.left() - margins.right() - 30
        height = min(
            self.report.heightForWidth(content_width) + overhead + 12,
            available.height() - 80,
        )
        self.setMaximumSize(available.width() - 80, available.height() - 80)
        self.resize(width, height)


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


def _chip_width() -> int:
    """Return the status chip column's width.

    It is the widest chip any row can show, so a wider word never moves
    the text column.
    """
    if not _COLUMNS:
        chips = [
            Badge(text, tone)
            for table in (BADGES, FLOW_BADGES, PANEL_BADGES)
            for text, tone in table.values()
        ]
        _COLUMNS.append(max(chip.sizeHint().width() for chip in chips))
        for chip in chips:
            chip.deleteLater()
    return _COLUMNS[0]


_COLUMNS: list[int] = []


class _ExpandableRow(QWidget):
    """An expand button, chip, text, time, actions and a body.

    The columns have fixed widths so rows line up. The title starts with the
    row's type (Run, Flow or Panel). The title and detail lines each show
    one line, cut with an ellipsis, with the whole text in their tooltips; a
    third such line shows a question. The expand button and the chip are
    centred on the text lines.
    """

    def __init__(self, item_id: str, kind: str, what: str) -> None:
        """Build the shared header and body layout."""
        super().__init__()
        self._item_id = item_id
        self._kind = kind
        self._what = what
        self.expanded = False
        outer = QVBoxLayout(self)
        self.outer = outer
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        header = QHBoxLayout()
        header.setSpacing(12)
        self.expand_button = button("", "secondary", size="sm")
        self.expand_button.setFixedWidth(theme.CONTROL_SM)
        header.addWidget(self.expand_button, 0, Qt.AlignmentFlag.AlignVCenter)
        chip_column = QWidget()
        chip_column.setFixedWidth(_chip_width())
        chip_layout = QHBoxLayout(chip_column)
        chip_layout.setContentsMargins(0, 0, 0, 0)
        self.badge = Badge("", "neutral")
        chip_layout.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignLeft)
        chip_layout.addStretch(1)
        header.addWidget(chip_column, 0, Qt.AlignmentFlag.AlignVCenter)
        lines = QVBoxLayout()
        lines.setSpacing(2)
        self.title_label = ElidedLabel()
        self.title_label.setFont(theme.font("label"))
        self.detail_label = ElidedLabel()
        self.detail_label.setProperty("role", "secondary")
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


class _Row(_ExpandableRow):
    """The shared header of a live or finished run; expanded, its record."""

    def __init__(self, run: Run) -> None:
        """Build a run row without drawing its values."""
        self.run = run
        self.nested = False
        self.parent_label = ""
        self.details: QWidget | None = None
        self.detail_values: tuple[tuple[str, str], ...] = ()
        super().__init__(run.id, "run", "details")

    def set_nested(self, nested: bool) -> None:
        """Mark the row as nested under a flow or panel; it draws the same."""
        self.nested = nested

    def draw_time(self, now: float | None = None) -> None:
        """Draw the detail line and the right column's Elapsed time."""
        ended = run_ended(self.run)
        detail = run_detail(self.run, now)
        if self.parent_label:
            detail = f"{self.parent_label} · {detail}"
        self.detail_label.setText(detail)
        self.time_label.setText(end_text(self.run.started_at, ended))

    def set_run(self, run: Run) -> None:
        """Store the run and draw its badge."""
        self.run = run
        text, tone = run_badge(run)
        self.badge.set_tone(tone, text)
        if self.details is not None and tuple(record_rows(run)) != self.detail_values:
            self._drop_details()
            self._add_details()

    def show_details(self, shown: bool) -> None:
        """Show or hide the run's record."""
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
        rows = record_rows(self.run)
        for row, (label, value) in enumerate(rows):
            name = QLabel(label)
            name.setProperty("role", "secondary")
            text = QLabel(value)
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if label in CODE_ROWS and value not in ("read-only", "None"):
                text.setFont(theme.font("code"))
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text, row, 1)
        grid.setColumnStretch(1, 1)
        self.body.addWidget(details)
        self.details = details
        self.detail_values = tuple(rows)

    def _drop_details(self) -> None:
        discard(self.details, self.body)
        self.details = None
        self.detail_values = ()


class _LiveRow(_Row):
    """A live run's row; expanded, it shows the run's record."""

    def __init__(self, run: Run, open_terminal: Callable[[str], None]) -> None:
        super().__init__(run)
        self.open_button = button("Open terminal", "secondary", size="sm")
        set_icon(
            self.open_button, icons.icon("terminal", theme.color("text-primary"), 16)
        )
        self.open_button.clicked.connect(lambda: open_terminal(self.run.id))
        self.action_box.addWidget(self.open_button)
        self.set_run(run)

    def set_run(self, run: Run) -> None:
        super().set_run(run)
        self.title_label.setText(run_title(run))
        self.update_elapsed()
        self.open_button.setAccessibleDescription(f"Open run {run.id} in a terminal")

    def update_elapsed(self, now: float | None = None) -> None:
        self.draw_time(now)


class _FinishedRow(_Row):
    """A finished run's row; expanded, it shows the run's record."""

    def __init__(self, run: Run) -> None:
        super().__init__(run)
        self.shown_values: tuple[object, ...] = ()
        self.set_run(run)

    @staticmethod
    def values(run: Run) -> tuple[object, ...]:
        """Return everything the row shows of a run, expanded or not."""
        return (
            run.status,
            run_title(run),
            run_detail(run),
            end_text(run.started_at, run_ended(run)),
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
        self.title_label.setText(run_title(run))
        self.draw_time()


class _RecordRow(_ExpandableRow):
    """A flow or panel row whose expanded body survives unrelated changes."""

    def __init__(self, item: FlowItem | PanelItem, screen: OverviewScreen) -> None:
        """Create a record row with details and nested worker rows."""
        self.item = item
        self.live_count = 0
        self.run_count = 0
        self.collapsed_runs = False
        self.overview = screen
        kind = "flow" if isinstance(item, FlowItem) else "panel"
        super().__init__(item.record["id"], kind, "details")
        self.details: QWidget | None = None
        self.file_buttons: dict[str, QPushButton] = {}
        self.file_note = ToneLine("warning")
        self.file_paths: dict[str, Path | None] = {}
        self.shown_values: tuple[object, ...] = ()
        self.base_detail = ""
        self.nested_box = QWidget()
        self.nested = QVBoxLayout(self.nested_box)
        self.nested.setContentsMargins(theme.CONTROL_SM + 12, 0, 0, 0)
        self.nested.setSpacing(16)
        self.outer.addWidget(self.nested_box)
        self.set_item(item)

    def entries(self) -> list[tuple[str, bool]]:
        """Return body lines with their secondary style flag."""
        raise NotImplementedError

    def set_item(self, item: FlowItem | PanelItem) -> None:
        """Draw changed values and refresh available file buttons."""
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
        # The chip shows the status; the detail adds only what it does not.
        parts = []
        if item.gone:
            parts.append(
                "runner gone" if isinstance(item, FlowItem) else "panel process gone"
            )
        if self.live_count:
            parts.append(plural(self.live_count, "live run"))
        if self.collapsed_runs and self.run_count:
            parts.append(plural(self.run_count, "run"))
        detail = " · ".join([*parts, detail])
        self.base_detail = detail
        entries = self.entries()
        values = (
            badge,
            title,
            detail,
            data["started_at"],
            data["ended_at"],
            tuple(entries),
        )
        self.update_elapsed()
        if values == self.shown_values:
            if self.expanded:
                self._refresh_files()
            return
        self.shown_values = values
        self.badge.set_tone(badge[1], badge[0])
        self.title_label.setText(title)
        if self.expanded:
            self.show_details(False)
            self.show_details(True)

    def update_elapsed(self, now: float | None = None) -> None:
        """Redraw the times without rebuilding the body."""
        data = self.item.record
        ended = data["ended_at"]
        if ended is None and data["status"] in (
            "done",
            "failed",
            "incomplete",
            "cancelled",
        ):
            ended = data["updated_at"]
        times = span_text(data["started_at"], ended, now)
        self.detail_label.setText(f"{self.base_detail} · {times}")
        self.time_label.setText(end_text(data["started_at"], ended))

    def show_details(self, shown: bool) -> None:
        """Expand or collapse the selectable body and its file buttons."""
        if self.details is not None:
            discard(self.details, self.body)
            self.details = None
        self.file_buttons = {}
        self.file_paths = {}
        if shown:
            details = QWidget()
            layout = QVBoxLayout(details)
            layout.setContentsMargins(0, 0, 0, 0)
            for text, secondary in self.entries():
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
                layout.addLayout(line)
            actions = QHBoxLayout()
            names = (
                ("Briefs", "Check logs")
                if isinstance(self.item, FlowItem)
                else ("Summary",)
            )
            for name in names:
                link = button(name, "secondary", size="sm")
                link.clicked.connect(
                    lambda _checked=False, selected=name: self._open_file(selected)
                )
                actions.addWidget(link)
                self.file_buttons[name] = link
            actions.addStretch()
            layout.addLayout(actions)
            self.file_note = ToneLine("warning")
            policy = self.file_note.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            policy.setHeightForWidth(True)
            self.file_note.setSizePolicy(policy)
            layout.addWidget(self.file_note)
            self._refresh_files()
            self.body.addWidget(details)
            self.details = details
        self.set_expanded(shown)
        self.show_nested()

    def show_nested(self) -> None:
        """Show nested runs, except in Finished while the row is collapsed.

        A Finished parent with a live nested run keeps its runs shown.
        """
        self.nested_box.setVisible(not self.collapsed_runs or self.expanded)

    def _refresh_files(self) -> None:
        """Check targets only while drawing expanded details."""
        env = self.overview.context.env
        if isinstance(self.item, FlowItem):
            folder = flow_record.folder(self.item.record["id"], env)
            briefs = folder / "briefs"
            self.file_paths = {
                "Briefs": briefs if briefs.is_dir() else None,
                "Check logs": newest_check_log(folder),
            }
        else:
            summary = panel_record.folder(self.item.record["id"], env) / "summary.md"
            self.file_paths = {"Summary": summary if summary.is_file() else None}

    def _open_file(self, name: str) -> None:
        """Open the selected file URL, or say under the buttons why it did not open.

        The buttons stay enabled; a file or folder that is not there yet, or a
        viewer that refused the file, is named in the line under them.
        """
        self._refresh_files()
        path = self.file_paths[name]
        if path is None:
            self.file_note.show_text(f"{name}: {MISSING_FILES[name]}")
        elif QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.file_note.show_text(None)
        else:
            self.file_note.show_text(f"No viewer opened {path}.")


class _FlowRow(_RecordRow):
    """A flow with task status and review details."""

    def entries(self) -> list[tuple[str, bool]]:
        """Return the runner pass count and each task's lines in plan order."""
        result: list[tuple[str, bool]] = [(f"passes {self.item.record['wave']}", True)]
        for task in self.item.record["tasks"]:
            result.append((task_line(self.item.record, task), False))
            if task["reason"]:
                result.append((task["reason"], True))
            review = review_line(task)
            if review:
                result.append((review, True))
        return result


class _PanelRow(_RecordRow):
    """A panel with member answers, omissions and a Summary button."""

    def entries(self) -> list[tuple[str, bool]]:
        """Return the question, then member details, then omissions."""
        result: list[tuple[str, bool]] = []
        data = self.item.record
        question = self.item.question if isinstance(self.item, PanelItem) else ""
        if question:
            result.append(("Question", False))
            result.append((question, True))
        for member in data["members"]:
            result.append((member_line(member), False))
            if member["reason"]:
                result.append((member["reason"], True))
            if member["changed"]:
                result.append(("Changed: " + ", ".join(member["changed"]), True))
        for omitted in data["left_out"]:
            result.append((f"Left out {omitted['value']}: {omitted['reason']}", True))
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


@dataclasses.dataclass(frozen=True)
class _DenialReport:
    """One resolved folder reported by a run in a turn."""

    denial: dict[str, Any]
    run: Run
    turn: int
    parent: FlowItem | PanelItem | None
    reported_at: float | None

    def sort_key(self) -> tuple[bool, float, str, int]:
        """Sort newest first, then by run and turn, with unknown times last."""
        return (
            self.reported_at is None,
            -self.reported_at if self.reported_at is not None else 0,
            self.run.id,
            self.turn,
        )

    def line(self) -> str:
        """Name the local report time, kind, claim, parent, run and turn."""
        parts = [
            datetime.datetime.fromtimestamp(self.reported_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if self.reported_at is not None
            else "time unknown",
            parent_label(self.parent) if self.parent is not None else "Run",
        ]
        if self.run.claim:
            parts.append(f"claim {self.run.claim}")
        if self.parent is not None:
            parts.append(item_id(self.parent))
        parts.extend((self.run.id, f"turn {self.turn}"))
        return " · ".join(" ".join(part.split()) for part in parts)


def _denial_summary(reports: Sequence[_DenialReport]) -> str:
    """Count distinct sources and show the first and latest local report times."""
    flows = {
        item_id(report.parent)
        for report in reports
        if isinstance(report.parent, FlowItem)
    }
    panels = {
        item_id(report.parent)
        for report in reports
        if isinstance(report.parent, PanelItem)
    }
    runs = {report.run.id for report in reports if report.parent is None}
    stamps = [
        report.reported_at for report in reports if report.reported_at is not None
    ]
    first = latest = "time unknown"
    if stamps:
        first = datetime.datetime.fromtimestamp(min(stamps)).strftime("%Y-%m-%d")
        latest = datetime.datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%d %H:%M")
    return (
        f"{plural(len(reports), 'report')} · {plural(len(flows), 'flow')}, "
        f"{plural(len(panels), 'panel')}, {plural(len(runs), 'standalone run')}"
        f" · first {first} · latest {latest} (local)"
    )


class _DeniedNotice(Alert):
    """A denial summary with a collapsible report list and actions below it."""

    def __init__(
        self,
        path: str,
        reports: Sequence[_DenialReport],
        explanation: str,
        actions: Sequence[tuple[str, Callable[[], None]]],
    ) -> None:
        super().__init__(
            "warning",
            f"A worker could not read {path}",
            _denial_summary(reports),
            actions,
        )
        outer = self.layout()
        assert isinstance(outer, QHBoxLayout)
        body_item = outer.itemAt(1)
        assert body_item is not None
        body = body_item.layout()
        assert isinstance(body, QVBoxLayout)
        self.runs_toggle = button("Show runs", "secondary", size="sm")
        self.runs_toggle.setCheckable(True)
        body.addWidget(self.runs_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.reports_label = QLabel("\n".join(report.line() for report in reports))
        self.reports_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.reports_label.hide()
        body.addWidget(self.reports_label)
        self.runs_toggle.toggled.connect(self.reports_label.setVisible)
        self.offer_label = QLabel(explanation)
        body.addWidget(self.offer_label)
        for label in (
            self.title_label,
            self.text_label,
            self.reports_label,
            self.offer_label,
        ):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            policy = label.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            label.setSizePolicy(policy)
        for action in self.buttons:
            outer.removeWidget(action)
        body.addWidget(FittingRow(self.buttons))
        self.setAccessibleDescription(f"{self.text_label.text()}\n{explanation}")


class OverviewScreen(QWidget):
    """The Overview screen: Live and Finished records and their workers."""

    TITLE = "Overview"

    def __init__(self, context: AppContext) -> None:
        """Create the screen.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        self.records = context.flows
        layout = scrolled(self)
        self.subtitle = screen_header(layout, self.TITLE)
        self.subtitle.hide()

        self.alerts = QVBoxLayout()
        self.alerts.setSpacing(8)
        layout.addLayout(self.alerts)
        self.sandbox_alerts: dict[str, Alert] = {}
        self.denied_area = QVBoxLayout()
        self.denied_area.setSpacing(8)
        layout.addLayout(self.denied_area)
        self.denied_notices: dict[str, _DeniedNotice] = {}
        self._denied_dialog: _PruneDialog | None = None

        self.unreadable_alert = Alert("info", unreadable_line(0))
        self.unreadable_alert.hide()

        self.state_view = StateView()
        layout.addWidget(self.state_view)

        self.records_alert = Alert(
            "danger", "", actions=[("Try again", self.retry_records)]
        )
        self.records_alert.hide()
        layout.addWidget(self.records_alert)
        self.flow_rows: dict[str, _RecordRow] = {}
        self.panel_rows: dict[str, _RecordRow] = {}

        self.live_card, self.live_rows_layout, self.live_empty = _card("Live")
        self.live_empty.setText("Nothing running")
        self.live_empty.setProperty("role", "secondary")
        live_layout = self.live_card.layout()
        assert isinstance(live_layout, QVBoxLayout)
        live_heading_item = live_layout.takeAt(0)
        live_heading = (
            live_heading_item.widget() if live_heading_item is not None else None
        )
        assert live_heading is not None
        live_layout.removeWidget(self.live_empty)
        live_header = QHBoxLayout()
        live_header.setSpacing(12)
        live_header.addWidget(live_heading, 0, Qt.AlignmentFlag.AlignVCenter)
        live_header.addWidget(self.live_empty, 0, Qt.AlignmentFlag.AlignVCenter)
        live_header.addStretch(1)
        live_layout.insertLayout(0, live_header)
        live_layout.addWidget(self.unreadable_alert)
        layout.addWidget(self.live_card)
        self.finished_card, self.finished_rows_layout, self.finished_empty = _card(
            "Finished"
        )
        self.finished_empty.setText("No finished runs, flows or panels yet.")
        finished_layout = self.finished_card.layout()
        assert isinstance(finished_layout, QVBoxLayout)
        heading_item = finished_layout.takeAt(0)
        heading = heading_item.widget() if heading_item is not None else None
        assert heading is not None
        header = QHBoxLayout()
        header.addWidget(heading)
        header.addStretch(1)
        self.finished_toggle = button("", "secondary", size="sm")
        self.finished_toggle.clicked.connect(self.toggle_finished_list)
        header.addWidget(self.finished_toggle)
        finished_layout.insertLayout(0, header)
        self.finished_open = state.load(context.env).finished_open
        self.type_filter = Picker()
        self.status_filter = Picker()
        self.engine_filter = Picker()
        for label, combo, every, values in (
            ("Type", self.type_filter, "All types", ("Run", "Flow", "Panel")),
            (
                "Status",
                self.status_filter,
                "All statuses",
                ("Done", "Incomplete", "Failed"),
            ),
            (
                "Engine",
                self.engine_filter,
                "All engines",
                ("Codex", "OpenCode", "Claude"),
            ),
        ):
            combo.setAccessibleName(label)
            combo.setProperty("scale", "sm")
            combo.add_option(every, "all")
            for value in values:
                combo.add_option(value, value)
            combo.currentTextChanged.connect(self.refresh)
        self.finished_cap = QLabel(
            f"Filters cover the newest {FINISHED_LIMIT} records."
        )
        self.finished_cap.setProperty("role", "secondary")
        self.finished_cap.hide()
        self.clear_button = button("Clear", "danger-outline", size="sm")
        self.clear_button.clicked.connect(self.clear_finished)
        self.clear_button.setAccessibleDescription(CLEAR_TIP)
        actions = QWidget()
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        self.finished_cap.setWordWrap(True)
        action_layout.addWidget(self.finished_cap)
        action_layout.addStretch(1)
        action_layout.addWidget(self.clear_button)
        self.filter_bar = FittingRow(
            (self.type_filter, self.status_filter, self.engine_filter, actions)
        )
        finished_layout.insertWidget(1, self.filter_bar)
        self._draw_clear_icon()
        theme.signals.changed.connect(self._draw_clear_icon)
        self._shown_finished: tuple[str, ...] = ()
        self._clearing = False
        self._prune_dialog: _PruneDialog | None = None
        layout.addWidget(self.finished_card)
        layout.addStretch(1)

        self.live_rows: dict[str, _LiveRow] = {}
        self.finished_rows: dict[str, _FinishedRow] = {}
        self._reading_order: list[_ExpandableRow] = []
        self._parents: dict[str, FlowItem | PanelItem] = {}
        self.expanded_live: str | None = None
        self.expanded_finished: str | None = None
        self._stale = True

        self._elapsed = QTimer(self)
        self._elapsed.setInterval(ELAPSED_MS)
        self._elapsed.timeout.connect(self._update_elapsed)

        self.records.reloaded.connect(self._runs_changed)
        self.records.failed.connect(self._runs_changed)
        context.runs.reloaded.connect(self._runs_changed)
        context.runs.failed.connect(self._runs_changed)
        context.settings_changed.connect(self._settings_changed)
        context.visible_changed.connect(self._visible_changed)
        self.refresh()

    # Showing

    def shown(self) -> None:
        """Redraw what changed while hidden and the sandbox alerts."""
        self._elapsed.start()
        if self._stale:
            self.refresh()
        self.check_sandboxes()
        self.check_denials()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: D102
        super().hideEvent(event)
        self._elapsed.stop()

    def _visible_changed(self, visible: bool) -> None:
        if not visible:
            self._elapsed.stop()
        elif self.isVisible():
            self.shown()

    def _runs_changed(self, *_args: object) -> None:
        if self.isVisible():
            self.refresh()
        else:
            self._stale = True

    def _settings_changed(self, _snapshot: dict) -> None:
        if self.isVisible():
            self.check_sandboxes()
            self.check_denials()

    # Drawing

    def refresh(self) -> None:
        """Redraw the records and run rows after either model changes."""
        self._stale = False
        runs = self.context.runs
        parents: list[FlowItem | PanelItem] = [
            *self.records.flows(),
            *self.records.panels(),
        ]
        live, finished, children = sections(runs.runs(), parents)
        self._parents = run_parents(parents)
        self.check_denials()
        workers: dict[str, list[Run]] = {item_id(parent): [] for parent in parents}
        for run in runs.runs():
            owner = self._parents.get(run.id)
            if owner is not None:
                workers[item_id(owner)].append(run)
        total = len(finished)
        if self.finished_open:
            finished = [
                item
                for item in finished
                if matches_filters(
                    item,
                    workers.get(item_id(item), []),
                    str(self.type_filter.value()),
                    str(self.status_filter.value()),
                    str(self.engine_filter.value()).lower(),
                )
            ]
        else:
            finished = finished[:FINISHED_PREVIEW]
        self._draw_finished_header(total)
        self._shown_finished = tuple(item_id(item) for item in finished)
        self._update_clear()
        self.finished_empty.setText(
            "No finished records match the filters."
            if self.finished_open
            and any(
                combo.value() != "all"
                for combo in (self.type_filter, self.status_filter, self.engine_filter)
            )
            else "No finished runs, flows or panels yet."
        )
        unreadable = (
            runs.unreadable()
            + self.records.unreadable_flows()
            + self.records.unreadable_panels()
        )
        line = unreadable_line(
            runs.unreadable(),
            self.records.unreadable_flows(),
            self.records.unreadable_panels(),
        )
        self.unreadable_alert.title_label.setText(line)
        self.unreadable_alert.setAccessibleName(line)
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
        self.state_view.clear()
        self.state_view.hide()
        self.live_card.show()
        self.finished_card.show()
        self._draw_sections(live, finished, children)
        self._chain_focus()

    def toggle_finished_list(self) -> None:
        """Open the full Finished list, or return to its preview, and keep it."""
        self.finished_open = not self.finished_open
        saved = state.load(self.context.env)
        state.save(
            dataclasses.replace(saved, finished_open=self.finished_open),
            self.context.env,
        )
        self.refresh()
        self.finished_toggle.setFocus(Qt.FocusReason.OtherFocusReason)

    def _draw_finished_header(self, total: int) -> None:
        """Show the pickers and Clear only while the full list is open."""
        self.filter_bar.setVisible(self.finished_open)
        if self.finished_open:
            label = "Show fewer"
        elif total >= FINISHED_LIMIT:
            label = f"Newest {FINISHED_LIMIT}"
        else:
            label = f"Show all {total}"
        self.finished_toggle.setText(label)
        self.finished_cap.setVisible(self.finished_open and total >= FINISHED_LIMIT)
        self.finished_toggle.setVisible(self.finished_open or total > FINISHED_PREVIEW)

    def _draw_clear_icon(self, *_args: object) -> None:
        set_icon(
            self.clear_button, icons.icon("trash-2", theme.color("danger-text"), 16)
        )

    def _show_state(self) -> None:
        """Hide the cards and give the state view their place."""
        self._shown_finished = ()
        self._update_clear()
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

    def _update_clear(self) -> None:
        """Show Clear only while records are shown; it waits out a pending clear.

        With nothing shown, the Finished list's empty line says why there is
        nothing to clear.
        """
        self.clear_button.setVisible(bool(self._shown_finished))
        self.clear_button.setEnabled(not self._clearing)

    def clear_finished(self) -> None:
        """Preview removal of the ids shown at the time Clear is clicked."""
        if self._clearing or not self._shown_finished:
            return
        ids = self._shown_finished
        self._clearing = True
        self._update_clear()
        env = self.context.env

        def previewed(result: prune.Result) -> None:
            if result.errors or not result.removed:
                self._show_prune_result(result, dry_run=True)
                return
            dialog = _PruneDialog(
                "Remove finished records?",
                "Remove these records and their logs? "
                "Branches and worktrees are kept.\n\n"
                + prune_text(result, dry_run=True),
                self,
                confirm=True,
            )
            self._prune_dialog = dialog

            def confirmed(answer: int) -> None:
                self._prune_dialog = None
                dialog.deleteLater()
                if answer != QDialog.DialogCode.Accepted:
                    self._clearing = False
                    self._update_clear()
                    return
                self.context.tasks.run(
                    lambda: prune.remove(older_than=0, record_ids=ids, env=env),
                    self._show_prune_result,
                    self._prune_failed,
                )

            dialog.finished.connect(confirmed)
            dialog.open()

        self.context.tasks.run(
            lambda: prune.remove(older_than=0, record_ids=ids, dry_run=True, env=env),
            previewed,
            self._prune_failed,
        )

    def _prune_failed(self, error: Exception) -> None:
        """Report a failed pruning task without starting another removal."""
        self._show_prune_result(prune.Result(errors=[str(error)]))

    def _show_prune_result(
        self, result: prune.Result, *, dry_run: bool = False
    ) -> None:
        """Show removal results or a warning, then reload both record models."""
        dialog = _PruneDialog(
            "Warning: could not clear all records"
            if result.errors
            else "Clear finished records",
            prune_text(result, dry_run=dry_run),
            self,
        )
        self._prune_dialog = dialog

        def closed(_answer: int) -> None:
            self._prune_dialog = None
            dialog.deleteLater()
            self.context.runs.reload()
            self.records.reload()

        dialog.finished.connect(closed)
        dialog.open()
        self._clearing = False
        self._update_clear()

    def _draw_sections(
        self, live: list[Item], finished: list[Item], children: dict[str, list[Run]]
    ) -> None:
        """Reuse rows and place every worker in its parent's section.

        A top-level worker of an ended parent names that parent first on its
        detail line.
        """
        shown_runs = [item for item in [*live, *finished] if isinstance(item, Run)]
        parents = [item for item in [*live, *finished] if not isinstance(item, Run)]
        for parent in parents:
            shown_runs.extend(children[item_id(parent)])
        self._draw_live([run for run in shown_runs if run.status in LIVE])
        self._draw_finished([run for run in shown_runs if run.status in DONE])
        top_ids = {item.id for item in [*live, *finished] if isinstance(item, Run)}
        for run in shown_runs:
            row = self._run_row(run)
            owner = self._parents.get(run.id) if run.id in top_ids else None
            label = parent_label(owner) if owner is not None else ""
            if label != row.parent_label:
                row.parent_label = label
                row.draw_time()
        for item in parents:
            rows = self.flow_rows if isinstance(item, FlowItem) else self.panel_rows
            key = item_id(item)
            row = rows.get(key)
            if row is None:
                row = (
                    _FlowRow(item, self)
                    if isinstance(item, FlowItem)
                    else _PanelRow(item, self)
                )
                row.expand_button.clicked.connect(
                    lambda _checked=False, selected=row, group=rows: (
                        self._toggle_record(selected, group)
                    )
                )
                rows[key] = row
            row.live_count = sum(run.status in LIVE for run in children[key])
            row.run_count = len(children[key])
            row.collapsed_runs = item_ended(item) and row.live_count == 0
            row.set_item(item)
            nested_rows = [self._run_row(run) for run in children[key]]
            for nested_row in nested_rows:
                nested_row.set_nested(True)
            _place(row.nested, nested_rows)
            row.show_nested()
        self._reading_order = []
        for items, layout, empty in (
            (live, self.live_rows_layout, self.live_empty),
            (finished, self.finished_rows_layout, self.finished_empty),
        ):
            top_rows: list[QWidget] = []
            for item in items:
                if isinstance(item, Run):
                    row = self._run_row(item)
                    row.set_nested(False)
                    self._reading_order.append(row)
                else:
                    row = (
                        self.flow_rows
                        if isinstance(item, FlowItem)
                        else self.panel_rows
                    )[item_id(item)]
                    self._reading_order.append(row)
                    self._reading_order.extend(
                        self._run_row(run) for run in children[item_id(item)]
                    )
                top_rows.append(row)
            _place(layout, top_rows)
            empty.setVisible(not items)
        # Surviving workers have moved out of parents that are no longer shown.
        parent_ids = {item_id(item) for item in parents}
        for rows in (self.flow_rows, self.panel_rows):
            for key in list(rows):
                if key not in parent_ids:
                    row = rows.pop(key)
                    row.setParent(None)
                    row.deleteLater()
        error = self.records.error()
        self.records_alert.setVisible(error is not None)
        if error is not None:
            text = f"Ember could not read the flow and panel records: {error}."
            self.records_alert.title_label.setText(text)
            self.records_alert.setAccessibleName(text)

    def _run_row(self, run: Run) -> _Row:
        """Return a shown run's row by its own status."""
        return (
            self.live_rows[run.id] if run.status in LIVE else self.finished_rows[run.id]
        )

    def _toggle_record(self, row: _RecordRow, rows: Mapping[str, _RecordRow]) -> None:
        """Expand one row in its card, or collapse the already expanded row."""
        shown = not row.expanded
        for other in rows.values():
            if other.expanded:
                other.show_details(False)
        row.show_details(shown)
        self._chain_focus()

    def _draw_live(self, live: list[Run]) -> None:
        wanted = {run.id for run in live}
        if self.expanded_live is not None and self.expanded_live not in wanted:
            self.collapse_live()
        for run_id in [key for key in self.live_rows if key not in wanted]:
            row = self.live_rows.pop(run_id)
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
        expanded = self.finished_rows.get(self.expanded_finished or "")
        if expanded is not None and expanded.details is None:
            expanded.show_details(True)

    def _chain_focus(self) -> None:
        """Keep Tab in reading order through alerts, records and runs."""
        chain: list[QWidget] = []
        for alert in self.sandbox_alerts.values():
            chain.extend(alert.buttons)
        for notice in self.denied_notices.values():
            chain.extend((notice.runs_toggle, *notice.buttons))
        if not self.records_alert.isHidden():
            chain.extend(self.records_alert.buttons)
        filters: list[QWidget] = [
            control
            for control in (
                self.finished_toggle,
                self.type_filter,
                self.status_filter,
                self.engine_filter,
                self.clear_button,
            )
            if control.isVisibleTo(self)
        ]
        for row in self._reading_order:
            if row._item_id in self._shown_finished and filters:
                chain.extend(filters)
                filters = []
            chain.append(row.expand_button)
            if isinstance(row, _LiveRow):
                chain.append(row.open_button)
            elif isinstance(row, _RecordRow):
                chain.extend(row.file_buttons.values())
        chain.extend(filters)
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
        """Expand a live row with its record, or collapse it; one at a time."""
        if self.expanded_live == run_id:
            self.collapse_live()
            return
        self.collapse_live()
        row = self.live_rows.get(run_id)
        if row is None:
            return
        self.expanded_live = run_id
        row.show_details(True)

    def collapse_live(self) -> None:
        """Collapse the expanded live row."""
        run_id, self.expanded_live = self.expanded_live, None
        if run_id is not None and run_id in self.live_rows:
            self.live_rows[run_id].show_details(False)

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

    def check_denials(self) -> None:
        """Show undismissed denials except startup files from recorded turns."""
        if not self.context.settings_ready():
            return
        saved = state.load(self.context.env)
        reads = [
            Path(path).resolve() for path in self.context.setting("sandbox.read") or []
        ]
        groups: dict[str, dict[tuple[str, int], _DenialReport]] = {}
        for run in self.context.runs.runs():
            for item in record.run_denials(run):
                if item["reason"] == "startup file":
                    continue
                if not sandbox.valid_denial_allow_path(
                    item["path"], item["allow_path"]
                ):
                    item["allow_path"] = None
                path = str(Path(item["path"]).resolve())
                target = Path(item["allow_path"] or item["path"]).resolve()
                if any(target.is_relative_to(read) for read in reads):
                    continue
                try:
                    stamp = record.outcome_path(run, item["turn"]).stat().st_mtime
                except OSError:
                    stamp = None
                reports = groups.setdefault(path, {})
                key = (run.id, item["turn"])
                previous = reports.get(key)
                reports[key] = _DenialReport(
                    sandbox.merge_denial(previous.denial, item)
                    if previous is not None
                    else item,
                    run,
                    item["turn"],
                    self._parents.get(run.id),
                    stamp,
                )
        wanted = {}
        for key, group in groups.items():
            if key in saved.denied_hidden:
                continue
            reports = sorted(group.values(), key=_DenialReport.sort_key)
            occurrences = sorted(f"{report.run.id}:{report.turn}" for report in reports)
            if set(occurrences).issubset(saved.denied_dismissals.get(key, [])):
                continue
            items = [report.denial for report in reports]
            item = items[0]
            for occurrence in items[1:]:
                item = sandbox.merge_denial(item, occurrence)
            signature = json.dumps(
                (
                    items,
                    [report.line() for report in reports],
                    _denial_summary(reports),
                ),
                sort_keys=True,
            )
            existing = self.denied_notices.get(key)
            if (
                existing is not None
                and existing.property("denied_signature") == signature
            ):
                wanted[key] = existing
                continue
            actions = []
            allow = item["allow_path"]
            if allow is not None:
                actions.append(
                    (
                        "Allow reading",
                        lambda _checked=False, p=allow: self._allow_reading(p),
                    )
                )
            actions.append(
                (
                    "Dismiss",
                    lambda _checked=False, p=key, seen=occurrences: (
                        self._dismiss_denial(p, seen)
                    ),
                )
            )
            actions.append(
                (
                    "Don't show again",
                    lambda _checked=False, p=key: self._hide_denial(p),
                )
            )
            if item["reason"] == "secret location":
                text = "This folder holds credentials."
            elif item["reason"] == "protected":
                text = "This folder is protected from workers."
            elif allow is None:
                text = "No folder can be offered for automatic read permission."
            else:
                text = f"Allow reading {allow} for future workers."
            notice = _DeniedNotice(key, reports, text, actions)
            if existing is not None:
                notice.runs_toggle.setChecked(existing.runs_toggle.isChecked())
            notice.setProperty("denied_signature", signature)
            wanted[key] = notice
        for key, notice in self.denied_notices.items():
            if wanted.get(key) is not notice:
                discard(notice, self.denied_area)
        self.denied_notices = wanted
        _place(self.denied_area, list(wanted.values()))
        self._chain_focus()

    def _dismiss_denial(self, path: str, occurrences: list[str]) -> None:
        """Keep dismissal until a later run or turn reports this folder."""
        saved = state.load(self.context.env)
        dismissals = {**saved.denied_dismissals, path: occurrences}
        state.save(
            dataclasses.replace(saved, denied_dismissals=dismissals), self.context.env
        )
        self.check_denials()

    def _hide_denial(self, path: str) -> None:
        """Hide every denial notice for this resolved folder until shown again."""
        saved = state.load(self.context.env)
        hidden = sorted({*saved.denied_hidden, path})
        state.save(dataclasses.replace(saved, denied_hidden=hidden), self.context.env)
        self.check_denials()

    def _allow_reading(self, path: str) -> None:
        """Confirm the folder's read permission for every future worker."""
        if self._denied_dialog is not None:
            return
        dialog = _PruneDialog(
            "Allow reading?",
            f"Every future worker can read {path}. No worker can change it.",
            self,
            confirm=True,
        )
        for action in dialog.buttons.buttons():
            if (
                dialog.buttons.buttonRole(action)
                == QDialogButtonBox.ButtonRole.AcceptRole
            ):
                action.setText("Allow reading")
        self._denied_dialog = dialog

        def confirmed(answer: int) -> None:
            self._denied_dialog = None
            dialog.deleteLater()
            if answer != QDialog.DialogCode.Accepted:
                return
            # Recheck aliases and credential boundaries at confirmation time.
            if not sandbox.allow_reading_path(path, self.context.env):
                self.context.error.emit(
                    f"Ember could not allow reading {path}: the folder is protected "
                    "or can hold credentials. Review Settings, Sandbox read paths."
                )
                self.context.show_screen("settings")
                return
            reads = list(self.context.setting("sandbox.read") or [])
            if path not in reads:
                self.context.save_settings({"sandbox.read": [*reads, path]})

        dialog.finished.connect(confirmed)
        dialog.open()

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
