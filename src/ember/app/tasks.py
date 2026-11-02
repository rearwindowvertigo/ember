"""Work that runs off the GUI thread.

Every call that runs a subprocess, reads the network or opens the settings
database goes through `TaskRunner.run`, so the window never waits on it.
"""

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Relay(QObject):
    """Carries one task's outcome to the GUI thread.

    The relay lives on the GUI thread; the worker thread emits its signals,
    so Qt queues the call to `_deliver` onto the GUI thread's event loop.
    """

    finished = Signal(bool, object)

    def __init__(
        self,
        done: Callable[[Any], None],
        failed: Callable[[Exception], None],
        release: Callable[[_Relay], None],
    ) -> None:
        super().__init__()
        self._done = done
        self._failed = failed
        self._release = release
        self.finished.connect(self._deliver)

    @Slot(bool, object)
    def _deliver(self, ok: bool, value: object) -> None:
        self._release(self)
        if ok:
            self._done(value)
        else:
            assert isinstance(value, Exception)
            self._failed(value)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any], relay: _Relay) -> None:
        super().__init__()
        self._fn = fn
        self._relay: _Relay | None = relay

    def run(self) -> None:
        relay, self._relay = self._relay, None
        if relay is None:
            return
        try:
            result = self._fn()
        except Exception as error:
            relay.finished.emit(False, error)
        else:
            relay.finished.emit(True, result)
        # The runner keeps the relay until delivery, so the last reference
        # is dropped on the GUI thread, never here.
        del relay


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
        self._relays: set[_Relay] = set()

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
        relay = _Relay(done, failed, self._relays.discard)
        self._relays.add(relay)
        self._pool.start(_Job(fn, relay))

    def pending(self) -> int:
        """Return how many tasks have not delivered their outcome yet."""
        return len(self._relays)

    def wait(self, msecs: int = -1) -> bool:
        """Wait for the pool's running tasks to end, without delivering them.

        Args:
            msecs: The longest wait in milliseconds; -1 waits without limit.

        Returns:
            True when every task ended in time.
        """
        return self._pool.waitForDone(msecs)
