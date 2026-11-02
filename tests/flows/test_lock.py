import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ember import paths
from ember.flows import lock
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]

FLOW = "flow-20261102-090132-015b1e"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return make_env(tmp_path)


def test_hold_and_probe(env: dict[str, str]) -> None:
    expected = Path(env["XDG_RUNTIME_DIR"]) / "ember" / "locks" / f"{FLOW}.lock"
    assert lock.path(FLOW, env) == expected
    assert not lock.live(FLOW, env)
    with lock.hold(FLOW, env):
        assert lock.live(FLOW, env)
        with pytest.raises(lock.Busy), lock.hold(FLOW, env):
            pytest.fail("a second holder acquired the lock")
    assert not lock.live(FLOW, env)
    assert expected.is_file()
    assert expected.stat().st_mode & 0o777 == 0o600
    assert expected.parent.stat().st_mode & 0o777 == 0o700


def test_body_error_is_unchanged_and_releases_lock(env: dict[str, str]) -> None:
    with pytest.raises(OSError, match="body error"), lock.hold(FLOW, env):
        raise OSError("body error")
    assert not lock.live(FLOW, env)


def test_process_death_releases_lock(tmp_path: Path, env: dict[str, str]) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    script = (
        "import sys, time\nfrom pathlib import Path\nfrom ember.flows import lock\n"
        "with lock.hold(sys.argv[1]):\n"
        "    Path(sys.argv[2]).touch()\n"
        "    while not Path(sys.argv[3]).exists(): time.sleep(0.01)\n"
    )
    child_env = {**env, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    child = subprocess.Popen(
        [sys.executable, "-c", script, FLOW, str(ready), str(release)], env=child_env
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()
        assert lock.live(FLOW, env)
        child.kill()
        child.wait(timeout=10)
        assert not lock.live(FLOW, env)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.mark.parametrize("operation", [lock.live, lock.hold])
def test_unusable_runtime(env: dict[str, str], operation: object) -> None:
    root = Path(env["XDG_RUNTIME_DIR"])
    root.write_text("not a folder")
    with pytest.raises(lock.LockError):
        if operation is lock.live:
            lock.live(FLOW, env)
        else:
            with lock.hold(FLOW, env):
                pytest.fail("acquired an unusable lock")


def test_runtime_error_is_wrapped(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(environment: object) -> Path:
        raise paths.RuntimeDirError("no runtime folder")

    monkeypatch.setattr(paths, "runtime_dir", fail)
    with pytest.raises(lock.LockError, match="no runtime folder"):
        lock.live(FLOW, env)


def test_symlink_lock_is_refused(env: dict[str, str], tmp_path: Path) -> None:
    assert not lock.live(FLOW, env)
    target = tmp_path / "target"
    target.write_text("unchanged")
    path = lock.path(FLOW, env)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(lock.LockError):
        lock.live(FLOW, env)
    assert target.read_text() == "unchanged"


def test_invalid_id_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(lock.LockError, match="invalid flow id"):
        lock.live("../outside", env)
    assert not os.path.exists(Path(env["XDG_RUNTIME_DIR"]) / "outside.lock")
