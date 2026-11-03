"""Read flow and panel records while the app runs, without taking locks."""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from ember import paths, prune
from ember.app.tasks import TaskRunner
from ember.flows import record as flow_record
from ember.panels import record as panel_record

BACKSTOP_MS = 60_000
GONE_AFTER = 60.0
ACTIVE_FLOW = ("starting", "running", "checking", "stopped")
ACTIVE_TASK = (
    "writing",
    "reviewing",
    "fixing",
    "accepted",
    "merging",
    "stopped",
    "conflict",
    "blocked",
)
RUNNER_FLOW = ("starting", "running", "checking")
ENDED_FLOW = flow_record.ENDED
ENDED_PANEL = ("done", "incomplete")
PROC_LOCKS = Path("/proc/locks")


@dataclass(frozen=True)
class FlowItem:
    """A stored flow and the observed state of its runner lock."""

    record: dict[str, Any]
    held: bool | None
    gone: bool
    run_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class PanelItem:
    """A stored panel, its question line and its process lock state."""

    record: dict[str, Any]
    question: str
    held: bool | None
    gone: bool
    run_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Snapshot:
    """The visible records, unreadable counts and paths to watch."""

    flows: list[FlowItem]
    panels: list[PanelItem]
    unreadable_flows: int
    unreadable_panels: int
    watch: list[str]


def plan_wave(flow: Mapping[str, Any]) -> tuple[int, int] | None:
    """Return the highest active plan wave's position and the stored total."""
    waves = flow.get("plan_waves", [])
    active = {task["id"] for task in flow["tasks"] if task["status"] in ACTIVE_TASK}
    for index in range(len(waves) - 1, -1, -1):
        if active.intersection(waves[index]):
            return index + 1, len(waves)
    return None


def held_locks(text: str) -> set[tuple[int, int, int]]:
    """Parse device and inode numbers from granted FLOCK entries."""
    held = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major, minor, inode = fields[5].split(":")
            held.add((int(major, 16), int(minor, 16), int(inode)))
        except ValueError:
            continue
    return held


def lock_held(path: Path, held: set[tuple[int, int, int]] | None) -> bool | None:
    """Read a lock file's identity without opening or creating it."""
    if held is None:
        return None
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino) in held


def _selected(
    records: list[dict[str, Any]], active: tuple[str, ...], ended: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Order active records first, followed by every ended record."""
    live = sorted(
        (item for item in records if item["status"] in active),
        key=lambda item: (item["started_at"], item["id"]),
    )
    done = sorted(
        (item for item in records if item["status"] in ended),
        key=lambda item: (
            item["updated_at"] if item["ended_at"] is None else item["ended_at"],
            item["updated_at"],
            item["id"],
        ),
        reverse=True,
    )
    return live + done


def _question(path: Path) -> str:
    """Read the first non-empty question line, or an empty string on error."""
    try:
        with path.open(encoding="utf-8") as source:
            return next((line.strip() for line in source if line.strip()), "")
    except OSError, UnicodeError:
        return ""


def read(
    env: Mapping[str, str], *, proc_locks: Path = PROC_LOCKS, now: float | None = None
) -> Snapshot:
    """Read records and lock identities without writing or acquiring a lock."""
    now = time.time() if now is None else now
    flows, flow_errors = flow_record.list_flows(env)
    panels, panel_errors = panel_record.list_panels(env)
    try:
        held = held_locks(proc_locks.read_text(encoding="utf-8"))
    except OSError, UnicodeError:
        held = None
    try:
        runtime = paths.runtime_dir(env)
    except paths.RuntimeDirError, OSError:
        runtime = None

    def state(item: dict[str, Any], needs_runner: bool) -> tuple[bool | None, bool]:
        """Return the observed lock state and whether the process is gone."""
        locked = (
            None
            if runtime is None
            else lock_held(runtime / "locks" / f"{item['id']}.lock", held)
        )
        gone = (
            needs_runner and locked is False and now - item["updated_at"] > GONE_AFTER
        )
        return locked, gone

    flow_items = [
        FlowItem(
            item,
            *state(item, item["status"] in RUNNER_FLOW),
            frozenset(
                prune._run_ids(item)
                | prune._run_ids(flow_record.read_events(item["id"], env))
            ),
        )
        for item in _selected(flows, ACTIVE_FLOW, ENDED_FLOW)
    ]
    panel_items = [
        PanelItem(
            item,
            _question(
                panel_record.folder(item["id"], env) / panel_record.QUESTION_NAME
            ),
            *state(item, item["status"] == "running"),
            frozenset(prune._run_ids(item)),
        )
        for item in _selected(panels, ("running",), ENDED_PANEL)
    ]
    root = paths.runs_dir(env)
    watch = [str(root)]
    try:
        folders = list(root.iterdir())
    except FileNotFoundError:
        folders = []
    for folder in sorted(folders):
        if not folder.is_dir():
            continue
        if flow_record.is_flow_id(folder.name):
            names = (flow_record.FLOW_NAME, flow_record.EVENTS_NAME)
        elif panel_record.is_panel_id(folder.name):
            names = (panel_record.PANEL_NAME,)
        else:
            continue
        watch.append(str(folder))
        for name in names:
            if (folder / name).exists():
                watch.append(str(folder / name))
    return Snapshot(flow_items, panel_items, len(flow_errors), len(panel_errors), watch)


class FlowsModel(QObject):
    """Keep flow and panel records current through coalesced background reads."""

    changed = Signal()
    reloaded = Signal()
    failed = Signal(str)

    def __init__(
        self,
        env: Mapping[str, str],
        tasks: TaskRunner,
        *,
        backstop_ms: int = BACKSTOP_MS,
        proc_locks: Path = PROC_LOCKS,
        parent: QObject | None = None,
    ) -> None:
        """Create an inactive model; activation starts its first read."""
        super().__init__(parent)
        self._env = env
        self._tasks = tasks
        self._proc_locks = proc_locks
        self._snapshot = Snapshot([], [], 0, 0, [])
        self._keys: list[tuple[object, ...]] = []
        self._loaded = False
        self._error: str | None = None
        self._active = False
        self._in_flight = False
        self._queued = False
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._path_changed)
        self._watcher.fileChanged.connect(self._path_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(backstop_ms)
        self._timer.timeout.connect(self.reload)

    def set_active(self, active: bool) -> None:
        """Start reads and watches when activated, removing them when stopped."""
        if active == self._active:
            return
        self._active = active
        if active:
            self._timer.start()
            self.reload()
        else:
            self._timer.stop()
            self._queued = False
            watched = self.watched()
            if watched:
                self._watcher.removePaths(watched)

    def active(self) -> bool:
        """Return whether the model is reading and watching records."""
        return self._active

    def loaded(self) -> bool:
        """Return whether at least one snapshot was read."""
        return self._loaded

    def error(self) -> str | None:
        """Return the last read's error, or None."""
        return self._error

    def flows(self) -> list[FlowItem]:
        """Return active flows followed by every ended flow."""
        return list(self._snapshot.flows)

    def panels(self) -> list[PanelItem]:
        """Return running panels followed by every ended panel."""
        return list(self._snapshot.panels)

    def unreadable_flows(self) -> int:
        """Return the number of unreadable flow records."""
        return self._snapshot.unreadable_flows

    def unreadable_panels(self) -> int:
        """Return the number of unreadable panel records."""
        return self._snapshot.unreadable_panels

    def watched(self) -> list[str]:
        """Return the watched record files and folders."""
        return self._watcher.files() + self._watcher.directories()

    def reload(self) -> None:
        """Read again while active, coalescing requests during a pending read."""
        if not self._active:
            return
        if self._in_flight:
            self._queued = True
            return
        self._in_flight = True
        self._tasks.run(
            lambda: read(self._env, proc_locks=self._proc_locks),
            self._loaded_records,
            self._read_failed,
        )

    def _path_changed(self, path: str) -> None:
        """Drop an old file watch after atomic replacement and reload."""
        if not self._active:
            return
        if path in self._watcher.files():
            self._watcher.removePath(path)
        self.reload()

    def _finish_read(self) -> None:
        """Release the pending read and start one queued reload."""
        self._in_flight = False
        if self._queued:
            self._queued = False
            self.reload()

    def _read_failed(self, error: Exception) -> None:
        """Keep the previous snapshot and report why the read failed."""
        self._error = str(error) or type(error).__name__
        self._finish_read()
        self.failed.emit(self._error)

    def _loaded_records(self, snapshot: Snapshot) -> None:
        """Store the snapshot and signal reloads and changed shown values."""
        keys: list[tuple[object, ...]] = [
            (
                item.record["id"],
                item.record["status"],
                item.record["updated_at"],
                item.held,
                item.gone,
                item.run_ids,
                item.question if isinstance(item, PanelItem) else None,
            )
            for item in [*snapshot.flows, *snapshot.panels]
        ]
        differs = (
            not self._loaded
            or self._error is not None
            or keys != self._keys
            or snapshot.unreadable_flows != self.unreadable_flows()
            or snapshot.unreadable_panels != self.unreadable_panels()
        )
        self._snapshot = snapshot
        self._keys = keys
        self._loaded = True
        self._error = None
        if self._active:
            self._watch(snapshot.watch)
        self._finish_read()
        if differs:
            self.changed.emit()
        self.reloaded.emit()

    def _watch(self, wanted: list[str]) -> None:
        """Remove obsolete watches and re-add existing paths after a read."""
        current = set(self.watched())
        gone = [item for item in current if item not in wanted]
        if gone:
            self._watcher.removePaths(gone)
        new = [item for item in wanted if item not in current and Path(item).exists()]
        if new:
            self._watcher.addPaths(new)
