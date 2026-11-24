"""Provide private test homes and generated eval fixtures."""

import gc
import os
import pwd
import subprocess
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from ember import paths, scopes


@pytest.fixture
def scope_probe() -> Callable[[Mapping[str, str] | None], bool]:
    """Expose the real availability probe to tests that select a scope mode."""
    return scopes.available


@pytest.fixture(autouse=True)
def no_user_scopes(
    monkeypatch: pytest.MonkeyPatch,
    scope_probe: Callable[[Mapping[str, str] | None], bool],
) -> None:
    """Keep tests in tree-only mode unless they explicitly enable scopes."""
    monkeypatch.setattr(scopes, "available", lambda env=None: False)


@pytest.fixture
def collect_on_gui_thread() -> Iterator[None]:
    """Turn off automatic collection, then collect on the GUI thread after.

    Qt objects left in reference cycles must be freed on the GUI thread, not
    by a garbage collection that happens to run on a pool thread. The app's
    `Collector` turns automatic collection off for the same reason; tests
    that build widgets without it request this fixture. After the test, it
    waits for pool tasks and delivers their events until no task runs, since
    a delivered result can start another task, then collects. Collection is
    turned off again first, in case the test turned it on. The fixture
    restores the earlier state only once the pool is idle; when a task
    still runs after the waits, it leaves collection off and fails the test.
    """
    from PySide6.QtCore import QCoreApplication, QThreadPool

    enabled = gc.isenabled()
    gc.disable()
    idle = False
    try:
        yield
    finally:
        gc.disable()
        try:
            pool = QThreadPool.globalInstance()
            for _ in range(10):
                pool.waitForDone(10_000)
                QCoreApplication.processEvents()
                if pool.activeThreadCount() == 0:
                    idle = True
                    break
            gc.collect()
        finally:
            if idle and enabled:
                gc.enable()
            elif idle:
                gc.disable()
    if not idle:
        pytest.fail("pool tasks still ran after the teardown waits")


@pytest.fixture
def run_tmp_volume_reader() -> Callable[..., paths.RunTmpVolume | None]:
    """Expose the real volume reader to tests that set a reading of their own."""
    return paths.run_tmp_volume


@pytest.fixture(autouse=True)
def healthy_run_tmp_volume(
    monkeypatch: pytest.MonkeyPatch,
    run_tmp_volume_reader: Callable[..., paths.RunTmpVolume | None],
) -> None:
    """Supply a fixed reading above both minimums unless a test sets its own."""
    monkeypatch.setattr(
        paths,
        "run_tmp_volume",
        lambda base=None: paths.RunTmpVolume(100_000, 1024**3, False),
    )


@pytest.fixture(autouse=True)
def private_run_tmp_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep run temporary folders inside each test's private state."""
    folder = tmp_path_factory.mktemp("run-tmp")
    monkeypatch.setattr(paths, "RUN_TMP_ROOT", folder)


@pytest.fixture(autouse=True)
def private_password_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give the current user a private fallback home when HOME is omitted."""
    lookup: Callable[[int], pwd.struct_passwd] = pwd.getpwuid
    owner = os.getuid()
    folder = tmp_path_factory.mktemp("password-home")

    def getpwuid(uid: int) -> pwd.struct_passwd:
        entry = lookup(uid)
        if uid != owner:
            return entry
        values = list(entry)
        values[5] = str(folder)
        return pwd.struct_passwd(values)

    monkeypatch.setattr(pwd, "getpwuid", getpwuid)


@pytest.fixture(scope="session")
def generated_eval_fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate fixtures once in each pytest worker's private temporary folder."""
    top = Path(__file__).resolve().parents[1]
    folder = tmp_path_factory.mktemp("eval-fixtures")
    result = subprocess.run(
        ["bash", "skill/evals/_stubs/generate.sh", "all", str(folder)],
        cwd=top,
        # A fixed fixture clock keeps the generated ids and times the same.
        env={**os.environ, "EMBER_EVAL_BEGAN": "1793620800"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return folder
