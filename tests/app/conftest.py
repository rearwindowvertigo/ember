import gc
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QThreadPool

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
