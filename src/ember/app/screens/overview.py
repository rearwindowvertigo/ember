"""Live and Finished runs, flows and panels with nested worker rows.

Models stay active while the app runs. Hidden screens defer redraws until
shown.
"""

import dataclasses
import datetime
import json
import math
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSize, QSizeF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QFontMetrics,
    QHideEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QTextCursor,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ember import paths, prune, sandbox
from ember.app import icons, state, theme
from ember.app.flows import (
    FlowItem,
    FlowRun,
    PanelItem,
    plan_wave,
    review_number,
    review_run_id,
)
from ember.app.runs import DONE, FINISHED_LIMIT, LIVE
from ember.app.screens.common import (
    FittingRow,
    ToneLine,
    chain_tab_order,
    discard,
    engine_name,
    plural,
    screen_header,
    scrolled,
    sentence,
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
    record.WAITING: ("Waiting for the lead", "warning"),
    record.CLOSED: ("Done", "success"),
    record.FAILED: ("Failed", "danger"),
    ORPHANED: ("Orphaned", "warning"),
}
"""The badge word and tone of each run status, and of an orphaned run."""

WAIT_BADGES = {
    "capacity": ("At capacity", "warning"),
    "usage_limit": ("At usage limit", "warning"),
    "outage": ("Waiting for provider", "warning"),
}
"""The badge of a running run whose open turn waits for the provider, by the
wait's kind."""


def run_badge(run: Run) -> tuple[str, str]:
    """Return a run's badge, including parent, provider and orphaned states."""
    state = record.parent_state(run).state
    if state in (record.PARENT_LEAD, record.PARENT_RESUME):
        return BADGES[record.WAITING]
    key = ORPHANED if state == record.PARENT_ORPHANED else run.status
    if key == record.RUNNING:
        wait = record.provider_wait(run)
        if wait is not None:
            return WAIT_BADGES[wait["kind"]]
    return BADGES.get(key, (run.status, "neutral"))


FLOW_BADGES = {
    "starting": ("Starting", "neutral"),
    "running": ("Running", "info"),
    "checking": ("Running", "info"),
    "stopped": ("Waiting for the lead", "warning"),
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
REVIEW_ACTION_STATUSES = {
    "accept": ("accepted", "merging", "merged"),
    "fix": ("reviewing", "pending"),
    "stop": ("stopped",),
}
PANEL_BADGES = {
    "running": ("Running", "info"),
    "done": ("Done", "success"),
    "incomplete": ("Incomplete", "incomplete"),
    "gone": ("Failed", "danger"),
}


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
    speed = f" · {member['speed']}" if member.get("speed") else ""
    return (
        f"{engine_name(member['engine'])} · {member['model']} · "
        f"{member['effort'] or 'default'}{speed} · "
        f"{member['status'].capitalize()} · {seconds}s"
    )


def elapsed(seconds: float) -> str:
    """Return a live duration using the same format as a finished duration."""
    return took(seconds)


def took(seconds: float) -> str:
    """Return a duration with seconds below an hour and minutes from an hour."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def worker_line(run: Run, *, show_default_effort: bool = False) -> str:
    """Return the engine, model, effort if recorded or requested, and speed."""
    parts = [engine_name(run.engine), run.model]
    if run.effort or show_default_effort:
        parts.append(run.effort or "default")
    if run.speed:
        parts.append(run.speed)
    return " · ".join(parts)


def branch_text(run: Run) -> str:
    """Return the run's branch, or "read-only"."""
    return "read-only" if run.read_only or not run.branch else run.branch


def run_ended(run: Run) -> float | None:
    """Return when an ended run ended, or `None` while it is live."""
    if run.status not in DONE:
        return None
    return run.updated_at if run.ended_at is None else run.ended_at


def parent_label(parent: FlowItem | PanelItem) -> str:
    """Describe a parent in a worker's details and denial reports."""
    if isinstance(parent, PanelItem):
        return f"Panel · {plural(len(parent.record['members']), 'member')}"
    directory = parent.record["dir"]
    return f"Flow · {PurePosixPath(directory).name or directory}"


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
    owners = run_parents(parents)
    children: dict[str, list[Run]] = {item_id(parent): [] for parent in parents}
    top: list[Item] = list(parents)
    for run in sorted(runs, key=item_started):
        owner = owners.get(run.id)
        if owner is None or (item_ended(owner) and not item_ended(run)):
            top.append(run)
        else:
            children[item_id(owner)].append(run)
    live = sorted((item for item in top if not item_ended(item)), key=item_started)
    finished = sorted(
        (item for item in top if item_ended(item)), key=item_end_key, reverse=True
    )[:FINISHED_LIMIT]
    return live, finished, children


def matches_filters(
    item: Item, children: list[Run], kind: str, status: str, engine: str
) -> bool:
    """Match a top-level row's type, chip and readable workers' engines."""
    workers = [item] if isinstance(item, Run) else children
    return (
        (kind == "all" or kind == _kind(item))
        and (status == "all" or status == _badge(item)[0])
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
    return max(
        Badge(text, tone).sizeHint().width()
        for table in (BADGES, FLOW_BADGES, PANEL_BADGES, WAIT_BADGES)
        for text, tone in table.values()
    )


COLUMN_NAMES = ("", "", "Status", "Name", "Progress", "Worker", "Effort", "Time", "")
COLUMN_GAP = 14
NAME_MIN_WIDTH = 160
NAME_PREFERRED_WIDTH = NAME_MIN_WIDTH
DETAIL_INDENT = theme.CONTROL_SM + COLUMN_GAP
TYPE_ICONS = {"Run": "user", "Flow": "users", "Panel": "users-three"}
TYPE_ICON_SIZE = 20
ROW_BUTTON_ICON_SIZE = 16
CODE_ROWS = ("Id", "Repository", "Branch")


def _expand_control() -> QPushButton:
    """Make the round chevron button."""
    control = button("", "secondary", size="sm")
    control.setIconSize(QSize(ROW_BUTTON_ICON_SIZE, ROW_BUTTON_ICON_SIZE))
    control.setIcon(
        icons.icon("chevron-right", theme.color("text-secondary"), ROW_BUTTON_ICON_SIZE)
    )
    control.setFixedSize(theme.CONTROL_SM, theme.CONTROL_SM)
    return control


def _terminal_control() -> QPushButton:
    """Make the Terminal button with its icon and secondary button style."""
    control = button("Terminal", "secondary", size="sm")
    control.setIconSize(QSize(ROW_BUTTON_ICON_SIZE, ROW_BUTTON_ICON_SIZE))
    control.setIcon(
        icons.icon("terminal", theme.color("text-primary"), ROW_BUTTON_ICON_SIZE)
    )
    control.setFixedSize(control.sizeHint())
    return control


@dataclasses.dataclass(frozen=True)
class _ColumnWidths:
    """Minimum and preferred widths for the aligned columns."""

    minimum: tuple[int, ...]
    preferred: tuple[int, ...]

    def fit(self, available_width: int) -> tuple[int, ...]:
        """Fit the optional columns and give Name the remaining width."""
        widths = list(self.preferred)
        name_reserve = NAME_PREFERRED_WIDTH - NAME_MIN_WIDTH
        needed = sum(widths) + COLUMN_GAP * (len(widths) - 2) + name_reserve
        excess = needed - available_width
        for heading in ("Worker", "Progress", "Effort", "Time"):
            if excess <= 0:
                break
            index = COLUMN_NAMES.index(heading)
            reduction = min(excess, widths[index] - self.minimum[index])
            widths[index] -= reduction
            excess -= reduction
        excess -= name_reserve
        for heading in ("Effort", "Worker", "Progress"):
            if excess <= 0:
                break
            index = COLUMN_NAMES.index(heading)
            excess -= widths[index] + COLUMN_GAP
            widths[index] = 0
        widths[COLUMN_NAMES.index("Name")] = max(0, NAME_MIN_WIDTH - excess)
        return tuple(widths)


def _column_widths() -> _ColumnWidths:
    """Measure headings, controls and representative values at the row fonts."""
    caption = QFontMetrics(theme.font("caption"))
    body = QFontMetrics(theme.font("body"))
    fixed = (
        theme.CONTROL_SM,
        max(TYPE_ICON_SIZE, *(caption.horizontalAdvance(kind) for kind in TYPE_ICONS)),
        _chip_width(),
        NAME_MIN_WIDTH,
        0,
        0,
        0,
        0,
        _terminal_control().sizeHint().width(),
    )
    minimum = tuple(
        max(width, caption.horizontalAdvance(heading))
        for heading, width in zip(COLUMN_NAMES, fixed, strict=True)
    )
    samples = {
        "Progress": ("6/6 merged", "8/8 answered", "read-only", "turn 99"),
        "Worker": (
            "gpt-5",
            "opus",
            "openrouter/x",
            "test-model",
            "gpt-6.2-astra",
            "8 members",
        ),
        "Effort": (
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "default",
            "mixed",
        ),
        "Time": ("1 h 30 min", "59 min 59 s"),
    }
    sample_widths = {
        heading: max(body.horizontalAdvance(text) for text in values)
        for heading, values in samples.items()
    }
    preferred = tuple(
        max(floor, sample_widths.get(heading, 0))
        for heading, floor in zip(COLUMN_NAMES, minimum, strict=True)
    )
    return _ColumnWidths(minimum, preferred)


class _RowColumns(QHBoxLayout):
    """Lay out the headings or a record using the table's current widths."""

    def __init__(self, cells: Sequence[QWidget], widths: tuple[int, ...]) -> None:
        super().__init__()
        self.cells = tuple(cells)
        self._widths: tuple[int, ...] = ()
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(COLUMN_GAP)
        leading = QHBoxLayout()
        leading.setSpacing(0)
        for cell in self.cells[:2]:
            leading.addWidget(cell, 0, Qt.AlignmentFlag.AlignVCenter)
        self.addLayout(leading)
        for cell in self.cells[2:]:
            self.addWidget(cell, 0, Qt.AlignmentFlag.AlignVCenter)
        self.set_widths(widths)

    def set_widths(self, widths: tuple[int, ...]) -> None:
        """Apply the table's exact widths to every cell."""
        if self._widths == widths:
            return
        for index, (heading, cell, width) in enumerate(
            zip(COLUMN_NAMES, self.cells, widths, strict=True)
        ):
            cell.setFixedWidth(width)
            if heading != "Name" and (
                width == 0 or (self._widths and self._widths[index] == 0)
            ):
                cell.setVisible(width > 0)
        self._widths = widths
        # Child geometry changes can leave this nested layout's widths cached.
        self.invalidate()


def _column_header(widths: tuple[int, ...]) -> QWidget:
    header = QWidget()
    labels = [ElidedLabel(name) for name in COLUMN_NAMES]
    for label in labels:
        label.setFont(theme.font("caption"))
        label.setProperty("role", "secondary")
    header.setLayout(_RowColumns(labels, widths))
    return header


def _name(item: Item, worker: FlowRun | None) -> str:
    if isinstance(item, Run):
        subject = " ".join(item.subject.split())
        if worker is not None:
            if worker.role == "reviewer" and worker.round is None and subject:
                return subject
            action = "Review" if worker.role == "reviewer" else "Writer"
            suffix = "" if worker.round is None else f", round {worker.round}"
            return f"{action}: {worker.task}{suffix}"
        return subject or worker_line(item)
    if isinstance(item, PanelItem):
        return next(
            (line.strip() for line in item.question.splitlines() if line.strip()),
            "Panel question unavailable",
        )
    if title := item.record.get("title"):
        return title
    tasks = item.record["tasks"]
    if not tasks:
        return item.record["id"]
    return tasks[0]["subject"] + (
        f" and {len(tasks) - 1} more" if len(tasks) > 1 else ""
    )


def _repository_name(item: Item) -> str:
    if isinstance(item, Run):
        directory = item.repo if item.main is None else item.main
    elif isinstance(item, FlowItem):
        directory = item.record["main"]
    else:
        directory = item.main or item.record["dir"]
    return PurePosixPath(directory).name or directory


def _kind(item: Item) -> str:
    if isinstance(item, Run):
        return "Run"
    return "Flow" if isinstance(item, FlowItem) else "Panel"


def _badge(item: Item) -> tuple[str, str]:
    if isinstance(item, Run):
        return run_badge(item)
    table = FLOW_BADGES if isinstance(item, FlowItem) else PANEL_BADGES
    status = item.record["status"]
    return table.get("gone" if item.gone else status, (status, "neutral"))


def _progress_worker_effort(item: Item) -> tuple[str, str, str]:
    if isinstance(item, Run):
        progress = "read-only" if item.read_only else f"turn {item.turn}"
        return progress, item.model, item.effort or "default"
    if isinstance(item, FlowItem):
        tasks = item.record["tasks"]
        merged = sum(task["status"] == "merged" for task in tasks)
        writer = item.record["writer"]
        return (
            f"{merged}/{len(tasks)} merged",
            writer["model"],
            writer["effort"] or "default",
        )
    members = item.record["members"]
    answered = sum(member["status"] == "answered" for member in members)
    efforts = {member["effort"] for member in members}
    effort = "mixed" if len(efforts) > 1 else next(iter(efforts), None) or "default"
    return f"{answered}/{len(members)} answered", plural(len(members), "member"), effort


def _finished_at(item: Item) -> float | None:
    if isinstance(item, Run):
        return run_ended(item)
    data = item.record
    if item_ended(item) and data["ended_at"] is None:
        return data["updated_at"]
    return data["ended_at"]


def _review_result(review: Mapping[str, Any]) -> str:
    return (
        f"{review.get('must', 0)} must, {review.get('should', 0)} should, "
        f"{review.get('lead', 0)} lead"
    )


def _task_worker(task: Mapping[str, Any]) -> str | None:
    """Return the run that owns the task's current status and reason."""
    if task["reviewer_run"]:
        return task["reviewer_run"]
    # Lead answers clear the review action when they start a new attempt.
    action = task.get("review_action")
    statuses = REVIEW_ACTION_STATUSES.get(action, ()) if isinstance(action, str) else ()
    if task["reviews"] and task["status"] in statuses:
        latest = task["reviews"][-1]
        if review_number(latest) == task["round"] + 1:
            return review_run_id(latest)
    return task["writer_run"]


def _task_status(task: Mapping[str, Any]) -> str:
    return TASK_WORDS.get(task["status"], task["status"])


def _run_rows(
    run: Run, owner: FlowItem | PanelItem | None, worker: FlowRun | None
) -> list[tuple[str, str]]:
    rows = [
        ("Branch", branch_text(run)),
        (
            "Turns and commits",
            f"{plural(run.turn, 'turn')}, {plural(len(run.commits), 'commit')}",
        ),
        ("Last exit", "None" if run.last_exit is None else str(run.last_exit)),
        ("Last reason", run.last_reason or "None"),
        ("Subject", run.subject),
        ("Engine", worker_line(run, show_default_effort=True)),
        ("Sandbox", run.sandbox),
    ]
    if owner is not None:
        rows.append(("Parent", f"{parent_label(owner)} · {item_id(owner)}"))
    state = record.parent_state(run)
    if state.state == record.PARENT_LEAD:
        rows.append(("Wait", f"Waiting for the lead's answer to flow {state.id}"))
    elif wait := record.parent_wait(state):
        rows.append(("Wait", sentence(wait)))
    if worker is not None and isinstance(owner, FlowItem):
        rows.extend([("Task", worker.task), ("Role", worker.role.capitalize())])
        if worker.round is not None:
            rows.append(("Round", str(worker.round)))
        task = next(
            (task for task in owner.record["tasks"] if task["id"] == worker.task),
            None,
        )
        if task is not None:
            if task["subject"] != run.subject:
                rows.append(("Task subject", task["subject"]))
            if _task_worker(task) == run.id:
                rows.append(("Task status", _task_status(task)))
                if task["reason"]:
                    rows.append(("Task reason", task["reason"]))
        if worker.review is not None:
            rows.extend(
                [
                    ("Verdict", str(worker.review.get("verdict") or "unreadable")),
                    ("Findings", _review_result(worker.review)),
                ]
            )
    return rows


def _flow_rows(item: FlowItem, workers: Sequence[Run]) -> list[tuple[str, str]]:
    flow = item.record
    wave = plan_wave(flow)
    writer = flow["writer"]
    rows = [
        ("Branch", f"ember/flow/{flow['id']}"),
        ("Wave", f"{wave[0]} of {wave[1]}" if wave else "None"),
        ("Passes", str(flow["wave"])),
        (
            "Writer",
            f"{engine_name(writer['engine'])} · {writer['model']} · "
            f"{writer['effort'] or 'default'}",
        ),
    ]
    if item.gone:
        rows.append(("Runner", "Runner gone"))
    if flow.get("reason"):
        rows.append(("Reason", flow["reason"]))
    readable = {run.id for run in workers}
    for task in flow["tasks"]:
        facts = []
        if _task_worker(task) not in readable:
            worker = task["writer"] or flow["writer"]
            facts.extend(
                (
                    task["subject"],
                    f"{_task_status(task)} · round {task['round']} · "
                    f"{engine_name(worker['engine'])} · {worker['model']}",
                )
            )
            if task["reason"]:
                facts.append(task["reason"])
        for review in task["reviews"]:
            run_id = review_run_id(review)
            worker = item.workers.get(run_id) if run_id is not None else None
            if run_id not in readable or (
                worker is not None and worker.review is not review
            ):
                round_number = review_number(review)
                title = (
                    "Review" if round_number is None else f"Review round {round_number}"
                )
                facts.append(
                    f"{title} · {review.get('verdict') or 'unreadable'} · "
                    f"{_review_result(review)}"
                )
        if facts:
            rows.append((f"Task {task['id']}", "\n".join(facts)))
    return rows


def _panel_rows(item: PanelItem) -> list[tuple[str, str]]:
    rows = [("Question", item.question)] if item.question else []
    if item.gone:
        rows.append(("Process", "Panel process gone"))
    for member in item.record["members"]:
        facts = [member_line(member)]
        if member["reason"]:
            facts.append(member["reason"])
        if member["changed"]:
            facts.append("Changed: " + ", ".join(member["changed"]))
        rows.append((f"Member {member['n']}", "\n".join(facts)))
    for index, omitted in enumerate(item.record["left_out"], 1):
        rows.append((f"Left out {index}", f"{omitted['value']}: {omitted['reason']}"))
    return rows


class _DetailValue(QTextEdit):
    """Selectable plain text that wraps without setting the table width."""

    def __init__(self) -> None:
        super().__init__()
        self._value = ""
        self.setReadOnly(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setFrameShape(QTextEdit.Shape.NoFrame)
        self.setStyleSheet("QTextEdit { background: transparent; }")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.fontMetrics().height())
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)

    def set_value(self, value: str) -> None:
        """Set the text and retain selected text still present in the value."""
        if value == self._value:
            return
        self._value = value
        selection = self.textCursor().selection().toPlainText()
        self.setPlainText(value)
        plain = self.toPlainText()
        if selection and (start := plain.find(selection)) >= 0:
            cursor = self.textCursor()
            cursor.setPosition(len(plain[:start].encode("utf-16-le")) // 2)
            cursor.setPosition(
                len(plain[: start + len(selection)].encode("utf-16-le")) // 2,
                QTextCursor.MoveMode.KeepAnchor,
            )
            self.setTextCursor(cursor)

    def _fit_height(self, size: QSizeF) -> None:
        self.setFixedHeight(math.ceil(size.height()))

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: D102, N802
        event.ignore()


class _OverviewRow(QWidget):
    """The columns, details, action bar and child layout of any Overview item."""

    def __init__(self, item: Item, screen: OverviewScreen) -> None:
        """Build a row whose widgets can be reused across reloads."""
        super().__init__()
        self.item = item
        self.overview = screen
        self.id = item_id(item)
        self.group: QVBoxLayout | None = None
        self.owner: FlowItem | PanelItem | None = None
        self.worker: FlowRun | None = None
        self.readable_workers: Sequence[Run] = ()
        self.child_rows: dict[str, _OverviewRow] = {}
        self.expanded = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        self.expand_button = _expand_control()
        self.expand_button.clicked.connect(lambda: screen._toggle_row(self))
        kind = _kind(item)
        self.type_icon = QLabel()
        self.type_icon.setAccessibleName(kind)
        self.type_label = ElidedLabel(kind)
        self.type_label.setFont(theme.font("caption"))
        self.type_label.setProperty("role", "secondary")
        self.type_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        type_cell = QWidget()
        type_layout = QVBoxLayout(type_cell)
        type_layout.setContentsMargins(0, 0, 0, 0)
        type_layout.setSpacing(2)
        type_layout.addWidget(self.type_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        type_layout.addWidget(self.type_label)

        self.badge = Badge("", "neutral")
        self.title_label = ElidedLabel()
        self.title_label.setFont(theme.font("label"))
        self.repo_label = ElidedLabel()
        self.repo_label.setFont(theme.font("caption"))
        self.repo_label.setProperty("role", "secondary")
        name_cell = QWidget()
        name_layout = QVBoxLayout(name_cell)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(2)
        name_layout.addWidget(self.title_label)
        name_layout.addWidget(self.repo_label)

        self.progress_label = ElidedLabel()
        self.worker_label = ElidedLabel()
        self.effort_label = ElidedLabel()
        self.time_label = ElidedLabel()
        for label in (
            self.progress_label,
            self.worker_label,
            self.effort_label,
            self.time_label,
        ):
            label.setFont(theme.font("body"))

        name = f"Open terminal for run {self.id}"
        self.open_button = _terminal_control()
        self.open_button.setAccessibleName(name)
        self.open_button.setAccessibleDescription("Open this run in a terminal.")
        self.open_button.setToolTip(name)
        self.open_button.clicked.connect(self._open_terminal)
        self.columns = _RowColumns(
            [
                self.expand_button,
                type_cell,
                self._cell(self.badge),
                name_cell,
                self.progress_label,
                self.worker_label,
                self.effort_label,
                self.time_label,
                self._cell(self.open_button),
            ],
            screen.column_widths.minimum,
        )
        outer.addLayout(self.columns)
        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        self.detail_box = QWidget()
        self.detail_box.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        detail_layout = QVBoxLayout(self.detail_box)
        detail_layout.setContentsMargins(DETAIL_INDENT, 0, 0, 0)
        self.details = QWidget()
        self.grid = QGridLayout(self.details)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(4)
        self.grid.setColumnStretch(1, 1)
        self.detail_labels: dict[str, tuple[QLabel, _DetailValue]] = {}
        detail_layout.addWidget(self.details)

        actions = QHBoxLayout()
        detail_layout.addLayout(actions)
        self.file_buttons: dict[str, QPushButton] = {}
        self.file_paths: dict[str, Path | None] = {}
        self.file_note = ToneLine("warning")
        self.file_note.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        detail_layout.addWidget(self.file_note)
        self.detail_terminal: QPushButton | None = None
        if isinstance(item, Run):
            self.detail_terminal = button("Open terminal", "secondary", size="sm")
            self.detail_terminal.setAccessibleName(name)
            self.detail_terminal.clicked.connect(self._open_terminal)
            actions.addWidget(self.detail_terminal)
        else:
            names = (
                ("Briefs", "Check logs") if isinstance(item, FlowItem) else ("Summary",)
            )
            for action in names:
                link = button(action, "secondary", size="sm")
                link.setAccessibleName(f"{action} for {_kind(item).lower()} {self.id}")
                link.clicked.connect(
                    lambda _checked=False, selected=action: self._open_file(selected)
                )
                actions.addWidget(link)
                self.file_buttons[action] = link
        actions.addStretch()

        body.addWidget(self.detail_box)
        self.nested = QVBoxLayout()
        self.nested.setSpacing(16)
        body.addLayout(self.nested)
        outer.addWidget(self.body)
        self.body.hide()
        self._draw_arrow()
        theme.signals.changed.connect(self._draw_arrow)
        self._draw_row_icons()
        theme.signals.changed.connect(self._draw_row_icons)

    @staticmethod
    def _cell(widget: QWidget) -> QWidget:
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return cell

    def set_item(
        self,
        item: Item,
        owner: FlowItem | PanelItem | None,
        workers: Sequence[Run],
    ) -> None:
        """Update the columns and any displayed details for this record."""
        self.item = item
        self.owner = owner
        self.readable_workers = workers
        self.worker = (
            owner.workers.get(self.id) if isinstance(owner, FlowItem) else None
        )
        text, tone = _badge(item)
        if (text, tone) != (self.badge.text(), self.badge.tone()):
            self.badge.set_tone(tone, text)
        progress, worker, effort = _progress_worker_effort(item)
        for label, value in (
            (self.title_label, _name(item, self.worker)),
            (self.repo_label, _repository_name(item)),
            (self.progress_label, progress),
            (self.worker_label, worker),
            (self.effort_label, effort),
        ):
            label.setText(value)
            label.setToolTip(value)
        live = isinstance(item, Run) and item.status in LIVE
        self.open_button.setVisible(live)
        if self.detail_terminal is not None:
            self.detail_terminal.setVisible(live)
        self.update_elapsed()
        if self.expanded:
            self._draw_details()

    def show_details(self, shown: bool) -> None:
        """Set the row's expansion and close its children when collapsing."""
        if shown == self.expanded:
            return
        self.expanded = shown
        if shown:
            self._draw_details()
        else:
            for child in self.child_rows.values():
                child.show_details(False)
        self.body.setVisible(shown)
        self._draw_arrow()

    def _draw_arrow(self, *_args: object) -> None:
        name = "chevron-down" if self.expanded else "chevron-right"
        self.expand_button.setIcon(
            icons.icon(name, theme.color("text-secondary"), ROW_BUTTON_ICON_SIZE)
        )
        verb = "Hide" if self.expanded else "Show"
        self.expand_button.setAccessibleName(
            f"{verb} details of {_kind(self.item).lower()} {self.id}"
        )

    def _draw_row_icons(self, *_args: object) -> None:
        self.type_icon.setPixmap(
            icons.pixmap(
                TYPE_ICONS[_kind(self.item)],
                theme.color("text-secondary"),
                TYPE_ICON_SIZE,
            )
        )
        self.open_button.setIcon(
            icons.icon("terminal", theme.color("text-primary"), ROW_BUTTON_ICON_SIZE)
        )

    def update_elapsed(self, now: float | None = None) -> None:
        """Show live elapsed time or the completed duration."""
        started = item_started(self.item)[0]
        ended = _finished_at(self.item)
        value = (
            elapsed((time.time() if now is None else now) - started)
            if ended is None
            else took(ended - started)
        )
        self.time_label.setText(value)
        self.time_label.setToolTip(value)

    def _detail_rows(self) -> list[tuple[str, str]]:
        item = self.item
        repo = item.repo if isinstance(item, Run) else item.record["dir"]
        ended = item.ended_at if isinstance(item, Run) else item.record["ended_at"]
        ended_text = (
            when(ended)
            if ended is not None
            else ("Not recorded" if item_ended(item) else "Not yet")
        )
        progress, worker, effort = _progress_worker_effort(item)
        shared = [
            ("Id", self.id),
            ("Repository", repo),
            ("Started", when(item_started(item)[0])),
            ("Ended", ended_text),
            ("Progress", progress),
        ]
        if isinstance(item, Run):
            return shared + _run_rows(item, self.owner, self.worker)
        if isinstance(item, FlowItem):
            return shared + _flow_rows(item, self.readable_workers)
        return shared + [("Worker", worker), ("Effort", effort)] + _panel_rows(item)

    def _draw_details(self) -> None:
        rows = self._detail_rows()
        previous = self.detail_labels
        current = {}
        for index, (key, value) in enumerate(rows):
            pair = previous.pop(key, None)
            if pair is None:
                name = QLabel(key)
                name.setProperty("role", "secondary")
                name.setWordWrap(True)
                text = _DetailValue()
                if key in CODE_ROWS:
                    text.setFont(theme.font("code"))
                pair = (name, text)
            name, text = pair
            text.set_value(value)
            cell = self.grid.itemAtPosition(index, 0)
            if cell is None or cell.widget() is not name:
                self.grid.addWidget(name, index, 0, Qt.AlignmentFlag.AlignTop)
                self.grid.addWidget(text, index, 1)
            current[key] = pair
        for pair in previous.values():
            for label in pair:
                discard(label, self.grid)
        self.detail_labels = current
        if self.file_buttons:
            self._refresh_files()

    def _open_terminal(self) -> None:
        self.overview.context.open_run_terminal(self.id)

    def _refresh_files(self) -> None:
        """Check the targets of the expanded row's file actions."""
        env = self.overview.context.env
        if isinstance(self.item, FlowItem):
            folder = flow_record.folder(self.id, env)
            briefs = folder / "briefs"
            self.file_paths = {
                "Briefs": briefs if briefs.is_dir() else None,
                "Check logs": newest_check_log(folder),
            }
        else:
            summary = panel_record.folder(self.id, env) / "summary.md"
            self.file_paths = {"Summary": summary if summary.is_file() else None}

    def _open_file(self, name: str) -> None:
        self._refresh_files()
        path = self.file_paths[name]
        if path is None:
            self.file_note.show_text(f"{name}: {MISSING_FILES[name]}")
        elif QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.file_note.show_text(None)
        else:
            self.file_note.show_text(f"No viewer opened {path}.")


def _place(layout: QVBoxLayout, rows: Sequence[QWidget]) -> None:
    """Put rows in a layout in order, moving them only when the order differs."""
    if [layout.indexOf(row) for row in rows] == list(range(len(rows))):
        return
    for row in rows:
        layout.removeWidget(row)
    for row in rows:
        layout.addWidget(row)
        row.show()


class _HeaderDivider(QWidget):
    """The one-pixel hairline between the table heading and its rows."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(1)
        theme.signals.changed.connect(self.update)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: D102, N802
        painter = QPainter(self)
        painter.fillRect(event.rect(), theme.color("border"))


class _RowTable(QWidget):
    """Fit the headings and rows to the available width in their card."""

    def __init__(self, widths: _ColumnWidths) -> None:
        super().__init__()
        self._widths = widths
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: D102, N802
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.fit_columns()

    def fit_columns(self) -> None:
        """Apply the table's fitted widths to every heading and row."""
        widths = self._widths.fit(self.width())
        for columns in self.findChildren(_RowColumns):
            columns.set_widths(widths)


def _card(
    title: str, widths: _ColumnWidths
) -> tuple[Card, QVBoxLayout, QLabel, QWidget, _RowTable]:
    card, layout = titled_card(title)
    margins = layout.contentsMargins()
    layout.setContentsMargins(16, margins.top(), 16, margins.bottom())
    empty = QLabel()
    empty.setProperty("role", "muted")
    layout.addWidget(empty)
    table = _RowTable(widths)
    table_layout = QVBoxLayout(table)
    table_layout.setContentsMargins(0, 0, 0, 0)
    table_layout.setSpacing(layout.spacing() // 2)
    header = _column_header(widths.minimum)
    table_layout.addWidget(header)
    table_layout.addWidget(_HeaderDivider())
    rows = QVBoxLayout()
    rows.setSpacing(16)
    table_layout.addLayout(rows)
    layout.addWidget(table)
    return card, rows, empty, header, table


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
        area = self.findChild(QScrollArea)
        assert area is not None
        bar = area.verticalScrollBar()
        # Let the bar use the screen's right padding so it cannot resize the tables.
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        area.setViewportMargins(0, 0, -bar.sizeHint().width(), 0)
        bar.setStyleSheet("QScrollBar::handle:disabled { background: transparent; }")
        bar.rangeChanged.connect(lambda start, end: bar.setEnabled(start != end))
        bar.setEnabled(bar.minimum() != bar.maximum())
        self.column_widths = _column_widths()
        self.subtitle = screen_header(layout, self.TITLE)
        self.subtitle.hide()

        self.alerts = QVBoxLayout()
        self.alerts.setSpacing(8)
        layout.addLayout(self.alerts)
        self.sandbox_alerts: dict[str, Alert] = {}
        self.tmp_volume_alert = Alert("warning", "")
        self.tmp_volume_alert.hide()
        layout.addWidget(self.tmp_volume_alert)
        self.denied_area = QVBoxLayout()
        self.denied_area.setSpacing(8)
        layout.addLayout(self.denied_area)
        self.denied_notices: dict[str, _DeniedNotice] = {}
        self._moving_hidden = False
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
        (
            self.live_card,
            self.live_rows_layout,
            self.live_empty,
            self.live_header,
            self.live_table,
        ) = _card("Live", self.column_widths)
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
        (
            self.finished_card,
            self.finished_rows_layout,
            self.finished_empty,
            self.finished_header,
            self.finished_table,
        ) = _card("Finished", self.column_widths)
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

        self.live_rows: dict[str, _OverviewRow] = {}
        self.finished_rows: dict[str, _OverviewRow] = {}
        self._rows: dict[str, _OverviewRow] = {}
        self._parents: dict[str, FlowItem | PanelItem] = {}
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
        self._draw_tmp_volume(runs.tmp_volume())
        parents: list[FlowItem | PanelItem] = [
            *self.records.flows(),
            *self.records.panels(),
        ]
        records = runs.runs()
        live, finished, children = sections(records, parents)
        self._parents = run_parents(parents)
        self.check_denials()
        workers: dict[str, list[Run]] = {item_id(parent): [] for parent in parents}
        for run in records:
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
            for row in self.live_rows.values():
                row.show_details(False)
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
        self._draw_sections(live, finished, children, workers)
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
        self,
        live: list[Item],
        finished: list[Item],
        children: Mapping[str, list[Run]],
        workers: Mapping[str, list[Run]],
    ) -> None:
        """Place top-level rows and only their assigned children."""
        shown = {item_id(item) for item in (*live, *finished)}
        wanted = shown | {run.id for key in shown for run in children.get(key, ())}
        for key in self._rows.keys() - wanted:
            row = self._rows.pop(key)
            discard(row, row.group)
        self.live_rows = self._draw_card(
            live,
            children,
            workers,
            self.live_rows_layout,
            self.live_empty,
            self.live_table,
        )
        self.finished_rows = self._draw_card(
            finished,
            children,
            workers,
            self.finished_rows_layout,
            self.finished_empty,
            self.finished_table,
        )
        error = self.records.error()
        self.records_alert.setVisible(error is not None)
        if error is not None:
            text = f"Ember could not read the flow and panel records: {error}."
            self.records_alert.title_label.setText(text)
            self.records_alert.setAccessibleName(text)

    def _draw_card(
        self,
        items: Sequence[Item],
        children: Mapping[str, list[Run]],
        workers: Mapping[str, list[Run]],
        layout: QVBoxLayout,
        empty: QLabel,
        table: _RowTable,
    ) -> dict[str, _OverviewRow]:
        has_expanded = any(
            row is not None and row.expanded and row.group is layout
            for row in (self._rows.get(item_id(item)) for item in items)
        )
        rows = {}
        for item in items:
            key = item_id(item)
            row = self._draw_row(
                item,
                layout,
                self._parents.get(key),
                workers.get(key, []),
                keep_expansion=not has_expanded and not isinstance(item, Run),
            )
            has_expanded |= row.expanded
            if not isinstance(item, Run):
                row.child_rows = {
                    run.id: self._draw_row(run, row.nested, item)
                    for run in children[key]
                }
                _place(row.nested, list(row.child_rows.values()))
            rows[key] = row
        _place(layout, list(rows.values()))
        empty.setVisible(not rows)
        table.setVisible(bool(rows))
        if rows:
            table.fit_columns()
        return rows

    def _draw_row(
        self,
        item: Item,
        group: QVBoxLayout,
        owner: FlowItem | PanelItem | None,
        workers: Sequence[Run] = (),
        *,
        keep_expansion: bool = False,
    ) -> _OverviewRow:
        key = item_id(item)
        row = self._rows.get(key)
        if row is None:
            row = _OverviewRow(item, self)
            self._rows[key] = row
        elif row.group is not group:
            if not keep_expansion:
                row.show_details(False)
            if row.group is not None:
                row.group.removeWidget(row)
        row.group = group
        row.set_item(item, owner, workers)
        return row

    def _toggle_row(self, row: _OverviewRow) -> None:
        """Expand one top-level row per card and one child under that row."""
        shown = not row.expanded
        if shown:
            for other in self._rows.values():
                if other.group is row.group and other.expanded:
                    other.show_details(False)
        row.show_details(shown)
        self._chain_focus()

    def _chain_focus(self) -> None:
        """Keep Tab in reading order through alerts, rows, and expanded bodies."""
        chain: list[QWidget] = []
        for alert in self.sandbox_alerts.values():
            chain.extend(alert.buttons)
        for notice in self.denied_notices.values():
            chain.extend((notice.runs_toggle, *notice.buttons))
        if not self.records_alert.isHidden():
            chain.extend(self.records_alert.buttons)
        for row in self.live_rows.values():
            self._row_controls(row, chain)
        chain.extend(
            control
            for control in (
                self.finished_toggle,
                self.type_filter,
                self.status_filter,
                self.engine_filter,
                self.clear_button,
            )
            if control.isVisibleTo(self)
        )
        for row in self.finished_rows.values():
            self._row_controls(row, chain)
        chain_tab_order(chain)

    @staticmethod
    def _row_controls(row: _OverviewRow, chain: list[QWidget]) -> None:
        chain.append(row.expand_button)
        if not row.open_button.isHidden():
            chain.append(row.open_button)
        if row.expanded:
            if row.detail_terminal is not None and not row.detail_terminal.isHidden():
                chain.append(row.detail_terminal)
            chain.extend(row.file_buttons.values())
            for child in row.child_rows.values():
                OverviewScreen._row_controls(child, chain)

    def _update_elapsed(self) -> None:
        now = time.time()
        for row in self._rows.values():
            if row.isVisible() and _finished_at(row.item) is None:
                row.update_elapsed(now)

    def check_denials(self) -> None:
        """Show undismissed denials except startup files from recorded turns."""
        if not self.context.settings_ready():
            return
        saved = state.load(self.context.env)
        if saved.denied_hidden:
            self._move_hidden_notices(saved.denied_hidden)
        hidden = [
            Path(path).resolve()
            for path in (
                *(self.context.setting("sandbox.hidden_notices") or []),
                *saved.denied_hidden,
            )
        ]
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
            if any(Path(key).is_relative_to(folder) for folder in hidden):
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
        """Hide every notice for this resolved folder until shown again.

        The folder is saved in `sandbox.hidden_notices`, so runs that report it
        later print no owner action and wait for no owner answer.
        """
        hidden = list(self.context.setting("sandbox.hidden_notices") or [])
        if path not in hidden:
            self.context.save_settings({"sandbox.hidden_notices": [*hidden, path]})

    def _move_hidden_notices(self, folders: list[str]) -> None:
        """Move folders hidden in the app's state file into the setting.

        The state file's list is emptied once the setting holds every folder.
        """
        if self._moving_hidden:
            return
        self._moving_hidden = True
        hidden = list(self.context.setting("sandbox.hidden_notices") or [])
        merged = [*hidden, *(path for path in folders if path not in hidden)]

        def moved(snapshot: dict[str, Any] | Exception) -> None:
            """Empty the state file's list after a successful save."""
            self._moving_hidden = False
            if isinstance(snapshot, Exception):
                return
            saved = state.load(self.context.env)
            state.save(
                dataclasses.replace(
                    saved,
                    denied_hidden=[
                        path for path in saved.denied_hidden if path not in merged
                    ],
                ),
                self.context.env,
            )

        if merged == hidden:
            moved({})
            return
        self.context.save_settings({"sandbox.hidden_notices": merged}, result=moved)

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

    def _draw_tmp_volume(self, volume: paths.RunTmpVolume | None) -> None:
        if volume is None or not volume.low:
            self.tmp_volume_alert.hide()
            return
        files = (
            "free file count unavailable"
            if volume.free_inodes is None
            else f"{volume.free_inodes:,} free files"
        )
        text = (
            f"{paths.RUN_TMP_ROOT} is nearly full: {files}, "
            f"{volume.free_bytes // 1024**2} MiB free. "
            "Ember refuses new Codex runs until files or space are freed."
        )
        self.tmp_volume_alert.title_label.setText(text)
        self.tmp_volume_alert.setAccessibleName(text)
        self.tmp_volume_alert.show()

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
