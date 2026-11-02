"""Work that runs off the GUI thread.

Every call that runs a subprocess, reads the network or opens the settings
database goes through `TaskRunner.run`, so the window never waits on it.
"""

import contextlib
import itertools
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Relay(QObject):
    """Carries finished job ids from pool threads to the GUI thread.

    The runner owns one relay, which lives on the GUI thread. A worker emits
    only a job id, so Qt queues the call to the runner on the GUI thread's
    event loop and the worker never holds a callback or an outcome.
    """

    finished = Signal(int)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._outcomes: dict[int, tuple[bool, object, object]] = {}

    def put(self, job_id: int, ok: bool, value: object, fn: object) -> None:
        """Keep a job's outcome and function until the GUI thread takes them."""
        with self._lock:
            self._outcomes[job_id] = (ok, value, fn)

    def take(self, job_id: int) -> tuple[bool, object, object] | None:
        """Remove and return a job's entry, or `None` when there is none."""
        with self._lock:
            return self._outcomes.pop(job_id, None)


class _Job(QRunnable):
    def __init__(
        self,
        job_id: int,
        fn: Callable[[], Any],
        relay: _Relay,
    ) -> None:
        super().__init__()
        self._job_id = job_id
        self._fn: Callable[[], Any] | None = fn
        self._relay = relay

    def run(self) -> None:
        fn, self._fn = self._fn, None
        if fn is None:
            return
        try:
            self._relay.put(self._job_id, True, fn(), fn)
        except Exception as error:
            self._relay.put(self._job_id, False, error, fn)
        # The relay now holds `fn` and the outcome, and the runner holds the
        # callbacks; the worker drops its own references before it signals, so
        # the last reference to each is dropped on the GUI thread.
        del fn
        # A RuntimeError means the runner and its relay were deleted, so
        # nothing waits for this job.
        with contextlib.suppress(RuntimeError):
            self._relay.finished.emit(self._job_id)


class TaskRunner(QObject):
    """Runs functions on a thread pool and delivers results on the GUI thread."""

    def __init__(
        self, pool: QThreadPool | None = None, parent: QObject | None = None
    ) -> None:
        """Create the runner.

        Args:
            pool: The thread pool; `None` uses `QThreadPool.globalInstance()`.
            parent: The parent object.
        """
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance() if pool is None else pool
        self._ids = itertools.count(1)
        self._callbacks: dict[
            int, tuple[Callable[[Any], None], Callable[[Exception], None]]
        ] = {}
        self._relay = _Relay(self)
        self._relay.finished.connect(self._deliver)

    def run(
        self,
        fn: Callable[[], Any],
        done: Callable[[Any], None],
        failed: Callable[[Exception], None],
    ) -> None:
        """Call `fn()` on the pool and deliver its outcome on the GUI thread.

        Must be called from the GUI thread.

        Args:
            fn: The work; it must not touch widgets.
            done: Called with `fn`'s return value.
            failed: Called with the exception `fn` raised.
        """
        job_id = next(self._ids)
        self._callbacks[job_id] = (done, failed)
        self._pool.start(_Job(job_id, fn, self._relay))

    @Slot(int)
    def _deliver(self, job_id: int) -> None:
        outcome = self._relay.take(job_id)
        callbacks = self._callbacks.pop(job_id, None)
        if outcome is None or callbacks is None:
            return
        ok, value, _fn = outcome
        done, failed = callbacks
        if ok:
            done(value)
        else:
            assert isinstance(value, Exception)
            failed(value)

    def pending(self) -> int:
        """Return how many tasks have not delivered their outcome yet."""
        return len(self._callbacks)

    def wait(self, msecs: int = -1) -> bool:
        """Wait for the pool's running tasks to end, without delivering them.

        Args:
            msecs: The longest wait in milliseconds; -1 waits without limit.

        Returns:
            True when every task ended in time.
        """
        return self._pool.waitForDone(msecs)
