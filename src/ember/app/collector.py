"""Run cyclic garbage collection on the GUI thread."""

import gc

from PySide6.QtCore import QObject, QTimer, Slot


class Collector(QObject):
    """Replace automatic collection with a timer owned by the GUI thread.

    Create and stop this object on the GUI thread. Stop background workers
    before stopping the collector.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        """Disable automatic collection and check allocation counts every 500 ms."""
        super().__init__(parent)
        self._was_enabled = gc.isenabled()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._collect)
        gc.disable()
        self.timer.start()

    @Slot()
    def _collect(self) -> None:
        """Collect all generations when the first allocation threshold is passed."""
        if gc.get_count()[0] > gc.get_threshold()[0]:
            gc.collect()

    def stop(self) -> None:
        """Stop the timer and restore the previous automatic collection state."""
        if not self.timer.isActive():
            return
        self.timer.stop()
        if self._was_enabled:
            gc.enable()
