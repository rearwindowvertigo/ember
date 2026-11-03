"""Find a process's descendants and end the ones that outlive their session.

A worker's tools can start processes in a session of their own, such as
Codex's `codex-linux-sandbox`. Ending a tmux session signals only the
pane's process group, so those processes survive it. `tree` records the
pane's descendants before the session ends; `end` signals the ones still
running afterwards.

Each process is kept with its start time from `/proc/<pid>/stat`, so a
process id that was reused by an unrelated process is never signalled.
"""

import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path

Process = tuple[int, int]
"""A process id and its start time in clock ticks since boot."""

PROC = Path("/proc")


def _stat(pid: int) -> tuple[int, int] | None:
    """Return a process's parent id and start time; `None` if gone or a zombie."""
    try:
        text = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name is in parentheses and may hold spaces or `)`.
    fields = text[text.rfind(")") + 2 :].split()
    try:
        if fields[0] in ("Z", "X"):
            return None
        return int(fields[1]), int(fields[19])
    except IndexError, ValueError:
        return None


def tree(pid: int) -> list[Process]:
    """Return a process and every descendant that is running now.

    Args:
        pid: The root process.

    Returns:
        The root first, then its descendants; empty when the root is gone.
    """
    children: dict[int, list[Process]] = {}
    root: Process | None = None
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        found = _stat(int(entry.name))
        if found is None:
            continue
        parent, start = found
        process = (int(entry.name), start)
        if process[0] == pid:
            root = process
        children.setdefault(parent, []).append(process)
    if root is None:
        return []
    result = [root]
    index = 0
    while index < len(result):
        result.extend(children.get(result[index][0], []))
        index += 1
    return result


def running(process: Process) -> bool:
    """Return whether the same process is still running."""
    found = _stat(process[0])
    return found is not None and found[1] == process[1]


def end(processes: Sequence[Process], *, wait: float = 2.0) -> list[int]:
    """Send SIGTERM to each process still running, then SIGKILL after `wait`.

    Args:
        processes: Processes from `tree`.
        wait: Seconds to wait after SIGTERM before SIGKILL.

    Returns:
        The ids of processes still running after SIGKILL was sent.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = [process for process in processes if running(process)]
        if not alive:
            return []
        for pid, _start in alive:
            try:
                os.kill(pid, sig)
            except OSError:
                continue
        deadline = time.monotonic() + (wait if sig == signal.SIGTERM else 0.5)
        while time.monotonic() < deadline and any(running(p) for p in alive):
            time.sleep(0.05)
    return [pid for pid, start in processes if running((pid, start))]
