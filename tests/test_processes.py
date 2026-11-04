import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ember import processes

# A parent that starts a child in its own session, as codex-linux-sandbox
# does, then waits.
SCRIPT = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         start_new_session=True)
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""


@pytest.fixture
def parent(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    marker = tmp_path / "child.pid"
    process = subprocess.Popen([sys.executable, "-c", SCRIPT, str(marker)])
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    child = int(marker.read_text())
    yield process, child
    for pid in (process.pid, child):
        with contextlib.suppress(OSError):
            os.kill(pid, 9)
    process.wait()


def test_tree_finds_a_child_in_its_own_session(
    parent: tuple[subprocess.Popen[bytes], int],
) -> None:
    process, child = parent
    found = processes.tree(process.pid)
    assert [pid for pid, _start in found] == [process.pid, child]


def test_tree_of_a_missing_process_is_empty() -> None:
    assert processes.tree(2**22 + 1) == []


def test_end_stops_a_child_that_outlived_its_parent(
    parent: tuple[subprocess.Popen[bytes], int],
) -> None:
    process, child = parent
    found = processes.tree(process.pid)
    process.kill()
    process.wait()
    assert processes.running(found[1])
    assert processes.end(found, wait=1.0) == []
    assert not processes.running(found[1])


def test_end_skips_a_reused_process_id(
    parent: tuple[subprocess.Popen[bytes], int],
) -> None:
    process, child = parent
    found = processes.tree(process.pid)
    reused = (child, found[1][1] + 1)
    assert not processes.running(reused)
    assert processes.end([reused]) == []
    assert processes.running(found[1])


def test_current_is_this_process() -> None:
    found = processes.current()
    assert found is not None and found[0] == os.getpid()
    assert processes.running(found)
    assert not processes.running((found[0], found[1] + 1))
