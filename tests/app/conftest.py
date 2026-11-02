import gc
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThreadPool
from PySide6.QtWidgets import QApplication, QWidget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A private environment for one app test.

    XDG folders live under `tmp_path`; `PATH` holds a stub `bin/` folder
    plus `/usr/bin:/bin`.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(runtime),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }


@pytest.fixture(autouse=True)
def _collect_on_gui_thread() -> Iterator[None]:
    """Wait for pool tasks, deliver their events, then collect garbage.

    Qt objects left in reference cycles must be freed on the GUI thread, not
    by a garbage collection that happens to run on a pool thread.
    """
    yield
    QThreadPool.globalInstance().waitForDone(10_000)
    QCoreApplication.processEvents()
    gc.collect()


class _WindowShows(QObject):
    """Record every widget shown as a window of its own."""

    def __init__(self) -> None:
        super().__init__()
        self.shown: list[tuple[QWidget, str]] = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.Show
            and isinstance(watched, QWidget)
            and watched.isWindow()
        ):
            self.shown.append((watched, type(watched).__name__))
        return False


@pytest.fixture(autouse=True)
def _no_stray_windows() -> Iterator[None]:
    """Fail a test in which a widget is shown before it has a parent.

    Showing a parentless widget makes it a window, which takes activation
    from the app's window until the widget joins a layout; the window's
    shortcuts then stop working.
    """
    app = QApplication.instance() or QApplication([])
    spy = _WindowShows()
    app.installEventFilter(spy)
    yield
    app.removeEventFilter(spy)
    stray = []
    for widget, name in spy.shown:
        try:
            if not widget.isWindow():
                stray.append(name)
        except RuntimeError:
            continue
    assert not stray, f"shown as a window before getting a parent: {stray}"
