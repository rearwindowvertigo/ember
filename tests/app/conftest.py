import os
from pathlib import Path

import pytest

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
