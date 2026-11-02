"""Hold and probe the kernel lock of a flow's runner."""

import contextlib
import fcntl
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from ember import paths


class Busy(Exception):  # noqa: N818 - the shared flow interface names this exception.
    """Another open file description holds the runner lock."""


class LockError(Exception):
    """The runtime folder or runner lock file cannot be used."""


def path(flow_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Return the lock path, rejecting names that are not flow ids."""
    if not re.fullmatch(r"flow-\d{8}-\d{6}-[0-9a-f]{6}", flow_id):
        raise LockError(f"invalid flow id: {flow_id!r}")
    try:
        return paths.runtime_dir(env) / "locks" / f"{flow_id}.lock"
    except (paths.RuntimeDirError, OSError) as error:
        raise LockError(f"cannot find the lock of {flow_id}: {error}") from error


@contextlib.contextmanager
def hold(flow_id: str, env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Take the lifetime lock without waiting and close it on every exit.

    The lock file stays in place after release. Errors from the protected
    operation propagate unchanged.
    """
    lock_path = path(flow_id, env)
    try:
        paths.ensure_private_dir(lock_path.parent, root=lock_path.parent.parent)
        fd = paths.open_private_file(lock_path)
    except (paths.RuntimeDirError, OSError) as error:
        raise LockError(f"cannot open the lock of {flow_id}: {error}") from error
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy(f"another runner works on {flow_id}") from error
        except OSError as error:
            raise LockError(f"cannot take the lock of {flow_id}: {error}") from error
        yield
    finally:
        os.close(fd)


def live(flow_id: str, env: Mapping[str, str] | None = None) -> bool:
    """Probe the lock on a new descriptor, including within its owning process."""
    try:
        with hold(flow_id, env):
            return False
    except Busy:
        return True
