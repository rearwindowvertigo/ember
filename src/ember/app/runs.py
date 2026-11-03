"""The run records the app shows, kept current by watching their files (J6).

`RunsModel` reads every run record in a task. A `QFileSystemWatcher` watches
the runs folder, each run's folder and each run's `run.json`; a record
replaced atomically is a new file, so the paths are added again after every
read. A sixty-second timer reloads as a backstop, also while the window is
hidden, because the tray shows the live runs. A read that cannot list the
runs folder emits `failed` with the reason and keeps the last records; a
record that cannot be read is only counted.
"""

import contextlib
from collections.abc import Mapping
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from ember import paths
from ember.app.tasks import TaskRunner
from ember.runs import record
from ember.runs.record import Run

BACKSTOP_MS = 60_000
"""The reload interval when no file change was seen."""

FINISHED_LIMIT = 20
"""How many finished runs `finished()` returns."""

LIVE = (record.STARTING, record.RUNNING, record.WAITING)
"""The statuses of a live run."""

DONE = (record.CLOSED, record.FAILED)
"""The statuses of a finished run."""

_Key = tuple[str, int, str | None, float]


def _key(run: Run) -> _Key:
    return (run.status, run.turn, run.turn_state, run.updated_at)


def _read(env: Mapping[str, str]) -> tuple[list[Run], int, list[str]]:
    """Read every record, and list the paths to watch."""
    root = paths.runs_dir(env)
    with contextlib.suppress(OSError, ValueError):
        paths.ensure_private_dir(root, root=paths.state_dir(env))
    runs, errors = record.list_runs(env)
    watch = [str(root)]
    try:
        folders = sorted(
            entry
            for entry in root.iterdir()
            if entry.is_dir() and record.is_run_folder(entry.name)
        )
    except OSError:
        folders = []
    for folder in folders:
        watch.append(str(folder))
        file = folder / record.RECORD_NAME
        if file.exists():
            watch.append(str(file))
    return runs, len(errors), watch


class RunsModel(QObject):
    """Every run record, read off the GUI thread.

    Reloads are coalesced: at most one read is in flight, and a reload asked
    for meanwhile runs once after it.
    """

    changed = Signal()
    """A record's status, turn, turn state or update time changed, a run
    appeared or went away, the number of unreadable records changed, or a
    read succeeded after a failed one."""

    reloaded = Signal()
    """Emitted after every successful read, including unchanged records."""

    turn_failed = Signal(str)
    """A run's turn failed since the last read, with the run's engine."""

    failed = Signal(str)
    """The runs folder could not be read, with the reason."""

    def __init__(
        self,
        env: Mapping[str, str],
        tasks: TaskRunner,
        *,
        backstop_ms: int = BACKSTOP_MS,
        parent: QObject | None = None,
    ) -> None:
        """Create the model; nothing is read until `start` or `reload`.

        Args:
            env: The environment.
            tasks: Runs the reads.
            backstop_ms: The reload interval when no change was seen.
            parent: The parent object.
        """
        super().__init__(parent)
        self._env = env
        self._tasks = tasks
        self._runs: list[Run] = []
        self._keys: dict[str, _Key] = {}
        self._unreadable = 0
        self._loaded = False
        self._error: str | None = None
        self._failed_turns: set[tuple[str, int]] = set()
        self._in_flight = False
        self._queued = False
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._path_changed)
        self._watcher.fileChanged.connect(self._path_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(backstop_ms)
        self._timer.timeout.connect(self.reload)

    def start(self) -> None:
        """Read the records now and start the backstop timer."""
        self._timer.start()
        self.reload()

    def stop(self) -> None:
        """Stop the backstop timer and watching files."""
        self._timer.stop()
        watched = self._watcher.files() + self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)

    def loaded(self) -> bool:
        """Return whether the records were read at least once."""
        return self._loaded

    def error(self) -> str | None:
        """Return why the last read could not list the runs folder, or `None`."""
        return self._error

    def runs(self) -> list[Run]:
        """Return every readable run, oldest first."""
        return list(self._runs)

    def live(self) -> list[Run]:
        """Return the runs that are starting, running or waiting, oldest first."""
        return [run for run in self._runs if run.status in LIVE]

    def finished(self) -> list[Run]:
        """Return the last twenty closed or failed runs, newest first.

        Runs are ordered by `ended_at`, `updated_at` and id; a run without
        `ended_at` counts its `updated_at`.
        """
        done = [run for run in self._runs if run.status in DONE]
        done.sort(
            key=lambda run: (
                run.updated_at if run.ended_at is None else run.ended_at,
                run.updated_at,
                run.id,
            ),
            reverse=True,
        )
        return done[:FINISHED_LIMIT]

    def unreadable(self) -> int:
        """Return how many run folders hold no readable record."""
        return self._unreadable

    def watched(self) -> list[str]:
        """Return the watched files and folders."""
        return self._watcher.files() + self._watcher.directories()

    def reload(self) -> None:
        """Read the records again, or once more after the read in flight."""
        if self._in_flight:
            self._queued = True
            return
        self._in_flight = True
        env = self._env
        self._tasks.run(lambda: _read(env), self._loaded_records, self._read_failed)

    def _path_changed(self, path: str) -> None:
        # A record replaced by a rename is a new file: drop the old watch so
        # the read below adds the new file.
        if path in self._watcher.files():
            self._watcher.removePath(path)
        self.reload()

    def _read_failed(self, error: Exception) -> None:
        self._error = str(error) or type(error).__name__
        self._finish_read()
        self.failed.emit(self._error)

    def _finish_read(self) -> None:
        self._in_flight = False
        if self._queued:
            self._queued = False
            self.reload()

    def _loaded_records(self, result: tuple[list[Run], int, list[str]]) -> None:
        runs, unreadable, watch = result
        self._watch(watch)
        keys = {run.id: _key(run) for run in runs}
        differs = (
            not self._loaded
            or self._error is not None
            or keys != self._keys
            or unreadable != self._unreadable
        )
        failed = [
            run
            for run in runs
            if run.status == record.FAILED
            and (run.id, run.turn) not in self._failed_turns
        ]
        first = not self._loaded
        self._failed_turns.update((run.id, run.turn) for run in failed)
        self._runs = runs
        self._keys = keys
        self._unreadable = unreadable
        self._loaded = True
        self._error = None
        self._finish_read()
        if differs:
            self.changed.emit()
        self.reloaded.emit()
        if not first:
            for run in failed:
                self.turn_failed.emit(run.engine)

    def _watch(self, wanted: list[str]) -> None:
        current = set(self._watcher.files()) | set(self._watcher.directories())
        gone = [item for item in current if item not in wanted]
        if gone:
            self._watcher.removePaths(gone)
        new = [item for item in wanted if item not in current and Path(item).exists()]
        if new:
            self._watcher.addPaths(new)
