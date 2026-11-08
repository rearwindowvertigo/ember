"""Read which lifetime locks are held from `/proc/locks`, without taking any.

Flow runners and panel commands hold an exclusive `flock` on
`<runtime>/locks/<id>.lock` for their lifetime. Probing that lock by taking
it would make a runner that starts at the same moment fail as busy, and
opening the lock file would create it. These functions compare the lock
file's device and inode with the granted `FLOCK` entries instead.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from ember import paths

PROC_LOCKS = Path("/proc/locks")


def held_locks(text: str) -> set[tuple[int, int, int]]:
    """Parse device and inode numbers from granted FLOCK entries."""
    held = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major, minor, inode = fields[5].split(":")
            held.add((int(major, 16), int(minor, 16), int(inode)))
        except ValueError:
            continue
    return held


def read_held(proc_locks: Path = PROC_LOCKS) -> set[tuple[int, int, int]] | None:
    """Return the held FLOCK identities, or `None` when they cannot be read."""
    try:
        return held_locks(proc_locks.read_text(encoding="utf-8"))
    except OSError, UnicodeError:
        return None


def lock_held(path: Path, held: set[tuple[int, int, int]] | None) -> bool | None:
    """Read a lock file's identity without opening or creating it."""
    if held is None:
        return None
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino) in held


def lifetime_held(
    owner_id: str,
    env: Mapping[str, str] | None = None,
    proc_locks: Path = PROC_LOCKS,
) -> bool | None:
    """Return whether a flow's or panel's lifetime lock is held.

    Args:
        owner_id: The flow or panel id.
        env: The environment; `None` reads `os.environ`.
        proc_locks: The kernel's lock table.

    Returns:
        True or False, or `None` when the lock table or the runtime folder
        cannot be read.
    """
    try:
        runtime = paths.runtime_dir(env)
    except paths.RuntimeDirError, OSError:
        return None
    return lock_held(runtime / "locks" / f"{owner_id}.lock", read_held(proc_locks))
