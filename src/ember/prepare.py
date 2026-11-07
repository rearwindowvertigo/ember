"""Look up and run the owner's per-repository worktree preparation."""

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from ember import choices, db, paths

TIMEOUT = 1800.0
"""The preparation time limit, in seconds."""

GIT_TIMEOUT = 30.0

_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
}

State = Literal["ok", "failed", "timeout", "interrupted", "not-started"]


class PreparationError(RuntimeError):
    """The preparation setting or a checkout's repository cannot be read."""


@dataclass(frozen=True)
class Result:
    """The outcome of one preparation attempt.

    Attributes:
        state: Whether preparation succeeded, failed, or did not run.
        exit: The shell's exit code, or `None` when no shell started.
        seconds: Elapsed time, including process cleanup and status inspection.
        log: The output log path.
        left_behind: Paths reported by Git after the command.
        message: A failure description, or `None` on success.
    """

    state: State
    exit: int | None
    seconds: float
    log: str
    left_behind: list[str]
    message: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the result as a JSON-ready object."""
        return asdict(self)


def _git(directory: Path, *args: str, env: Mapping[str, str] | None) -> str:
    """Run a Git query, raising on failure instead of returning an empty value."""
    clean = {
        key: value
        for key, value in (os.environ if env is None else env).items()
        if key not in _GIT_ENV
    }
    clean["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        reply = subprocess.run(
            ["git", "-C", str(directory), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            env=clean,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreparationError(f"git {args[0]} in {directory}: {error}") from error
    if reply.returncode:
        message = reply.stderr.strip() or f"exit {reply.returncode}"
        raise PreparationError(f"git {args[0]} in {directory}: {message}")
    return reply.stdout


def lookup(common: str | Path, *, env: Mapping[str, str] | None = None) -> str | None:
    """Find a preparation command by the repository's common Git folder.

    Args:
        common: The repository's common Git folder, including for linked worktrees.
        env: The settings and Git environment; `None` inherits the environment.

    Returns:
        The saved command (including an empty string), or `None` without an entry.
        Entries whose checkout no longer exists are skipped.

    Raises:
        PreparationError: The settings database cannot be read, worktree.prepare
            cannot be read or is invalid, an existing configured checkout cannot
            be inspected, or several entries match the repository.
    """
    try:
        reply = choices.read(env=env)
    except db.DatabaseError as error:
        raise PreparationError(f"cannot read worktree.prepare: {error}") from error
    errors = [
        error
        for error in reply["errors"]
        if error.startswith("setting worktree.prepare:")
    ]
    if errors:
        raise PreparationError("cannot read worktree.prepare: " + "; ".join(errors))
    entries = reply["answers"]["worktree.prepare"]["value"]
    command: str | None = None
    try:
        target = Path(common).resolve()
        for checkout, value in entries.items():
            folder = Path(checkout)
            try:
                folder.stat()
            except FileNotFoundError:
                continue
            found = _git(
                folder,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                env=env,
            ).strip()
            if not found:
                raise PreparationError(f"no common Git folder for {checkout}")
            if Path(found).resolve() == target:
                if command is not None:
                    raise PreparationError(
                        f"several worktree.prepare entries match {common}"
                    )
                command = value
    except (OSError, ValueError) as error:
        raise PreparationError(f"cannot resolve worktree.prepare: {error}") from error
    return command


def start_ticks(pid: int) -> int | None:
    """Return a process's Linux start ticks, including while it is a zombie."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except OSError, IndexError, ValueError:
        return None


def end_group(pid: int, start: int) -> bool:
    """Kill a recorded process group only while its leader's start ticks match.

    The leader need not still be running: an unreaped leader also identifies
    the group of children left behind by a shell that has exited.

    Args:
        pid: The group leader's pid, recorded before waiting for it.
        start: The recorded start ticks.

    Returns:
        Whether SIGKILL was sent to the matching process group.
    """
    if pid <= 1 or start_ticks(pid) != start:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


def _left_behind(worktree: Path, env: Mapping[str, str] | None) -> list[str]:
    """Read changed paths with NUL delimiters so quotes and newlines survive."""
    text = _git(
        worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all", env=env
    )
    entries = iter(text.split("\0"))
    found: list[str] = []
    for entry in entries:
        if not entry:
            continue
        found.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            original = next(entries, "")
            if original:
                found.append(original)
    return found


def run(
    command: str,
    worktree: str | Path,
    log: str | Path,
    *,
    started: Callable[[int, int], None],
    stop: threading.Event | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = TIMEOUT,
) -> Result:
    """Run preparation in its own process group and inspect the resulting tree.

    Args:
        command: The owner's shell command, run through `/bin/sh -c`.
        worktree: The new worktree to prepare.
        log: The output log, opened at mode 600. Its parent must exist.
        started: Persist the pid and start ticks before the first wait. An
            exception ends the group and is propagated to the caller.
        stop: A set event interrupts preparation, including before startup.
        env: The command and Git environment; `None` inherits the environment.
        timeout: Maximum command seconds, capped at 1800; defaults to 1800.

    Returns:
        The state, shell exit, elapsed seconds, log, changed paths and message.
        A nonzero exit or a dirty worktree fails preparation. Timeout and stop
        end the whole group before the shell is reaped. KeyboardInterrupt is
        reported as interrupted after ending the group.
    """
    began = time.monotonic()
    limit = min(TIMEOUT, timeout)
    folder = Path(worktree)
    log_path = Path(log)
    state: State = "not-started"
    exit_code: int | None = None
    message: str | None = None

    def result(left: list[str]) -> Result:
        return Result(
            state, exit_code, time.monotonic() - began, str(log_path), left, message
        )

    if stop is not None and stop.is_set():
        message = "preparation stopped before starting"
        return result([])
    try:
        output = os.fdopen(
            paths.open_private_file(log_path, os.O_WRONLY | os.O_TRUNC), "wb"
        )
    except OSError as error:
        message = f"cannot open preparation log: {error}"
        return result([])
    with output:
        try:
            process = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=folder,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            message = f"cannot start preparation: {error}"
            return result([])
        try:
            start = start_ticks(process.pid)
            if start is None:
                state = "failed"
                message = "cannot read preparation process start ticks"
            else:
                started(process.pid, start)
                deadline = began + limit
                while True:
                    if stop is not None and stop.is_set():
                        state = "interrupted"
                        message = "preparation interrupted"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state = "timeout"
                        message = f"preparation timed out after {limit:g} seconds"
                        break
                    # Observe exit without reaping: the leader's pid must
                    # remain reserved until its whole group has been ended.
                    status = os.waitid(
                        os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                    )
                    if status is not None:
                        state = "ok"
                        break
                    delay = min(0.05, remaining)
                    if stop is None:
                        time.sleep(delay)
                    else:
                        stop.wait(delay)
        except KeyboardInterrupt:
            state = "interrupted"
            message = "preparation interrupted"
        finally:
            # This Popen owns the pid until wait reaps it, so cleanup can also
            # run when start ticks could not be read or persisted.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            exit_code = process.wait()
        if state == "ok" and exit_code:
            state = "failed"
            message = f"preparation exited {exit_code}"
    try:
        left = _left_behind(folder, env)
    except PreparationError as error:
        state = "failed" if state == "ok" else state
        message = f"{message}; {error}" if message else str(error)
        return result([])
    if left:
        state = "failed" if state == "ok" else state
        dirty = "preparation left changed paths: " + ", ".join(left)
        message = f"{message}; {dirty}" if message else dirty
    return result(left)
