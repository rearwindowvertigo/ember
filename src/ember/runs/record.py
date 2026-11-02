"""Run records: one folder per run under Ember's state folder.

Each run has a folder `runs/<run id>/` in Ember's state folder, at mode 700,
holding `run.json` (mode 600, replaced atomically), `brief.md` and one
`turn-<n>.md` per reply. Paths are absolute strings and times are seconds
since the epoch.

Every function takes an optional `env` mapping, which defaults to
`os.environ`.
"""

import contextlib
import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from ember import paths

RECORD_NAME = "run.json"
BRIEF_NAME = "brief.md"
LOCK_NAME = "run.lock"

STARTING = "starting"
RUNNING = "running"
WAITING = "waiting"
FAILED = "failed"
CLOSED = "closed"
STATUSES = (STARTING, RUNNING, WAITING, FAILED, CLOSED)
"""The values of `status`."""

SENT = "sent"
COLLECTED = "collected"
COMMITTED = "committed"
TURN_STATES = (SENT, COLLECTED, COMMITTED)
"""The values of `turn_state`."""

SANDBOXES = ("codex", "bubblewrap", "none")
"""The values of `sandbox`: who provides the run's sandbox."""

_PANEL_ID = re.compile(r"panel-\d{8}-\d{6}-[0-9a-f]{6}")
_FLOW_ID = re.compile(r"flow-\d{8}-\d{6}-[0-9a-f]{6}")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{5,63}")


class RecordError(Exception):
    """A run record is missing or not valid."""

    def __init__(self, run_id: str, message: str) -> None:
        """Keep the run id with the message.

        Args:
            run_id: The run id.
            message: What is wrong.
        """
        super().__init__(f"{run_id}: {message}")
        self.run_id = run_id


class RecordMissingError(RecordError):
    """No run has this id."""


@dataclass(frozen=True)
class Run:
    """A run's record, with its folder and environment.

    Every field except `folder` and `env` is stored in `run.json`.
    """

    id: str
    """The run id, `<engine>-<yyyymmdd>-<hhmmss>-<6 hex>`; also the tmux session."""

    engine: str
    """`codex`, `opencode` or `claude`."""

    model: str
    """The model."""

    effort: str | None
    """The effort, or `None` when the engine's own default applies."""

    read_only: bool
    """Whether the run edits nothing."""

    repo: str
    """The top of the checkout Claude named."""

    worktree: str
    """The worker's working folder: its worktree, or the checkout itself for a
    read-only run."""

    branch: str | None
    """`ember/<engine>/<id>`, or `None` for a read-only run."""

    base: str | None
    """The commit the run started from; `None` in a checkout with no commit."""

    sandbox: str
    """Who provides the sandbox: `codex` (Codex's own), `bubblewrap` (Ember's)
    or `none`."""

    network: bool
    """Whether `worker.network` was `on` when the run started; every turn
    uses this value."""

    read: tuple[str, ...]
    """The run's own read-only paths, from `--read`."""

    read_effective: tuple[str, ...]
    """Every path the run may read: the `sandbox.read` setting when the run
    started, then the `--read` paths; every turn uses this list."""

    trailers: tuple[str, ...]
    """The trailer lines of each commit."""

    claim: str | None
    """The claim id the run read its menu with, or `None`."""

    status: str
    """`starting`, `running`, `waiting`, `failed` or `closed`."""

    turn: int
    """The current turn's number; 0 before the first message."""

    turn_state: str | None
    """`sent`, `collected` or `committed` for the current turn; `None` before
    the first message."""

    reply: str | None
    """The current turn's reply file, once collected."""

    expected_head: str | None
    """The branch tip before the current turn; `None` for a read-only run."""

    started_at: float
    """When the run started."""

    updated_at: float
    """When the record last changed."""

    ended_at: float | None
    """When the session ended, or `None` while it is open."""

    last_exit: int | None
    """The exit code of the last command that finished a turn or failed it."""

    last_reason: str | None
    """The message of that exit, or the files a read-only turn changed."""

    commits: tuple[str, ...]
    """The commits the run made, oldest first."""

    subject: str
    """The brief's first non-empty line."""

    folder: Path = field(default=Path(), compare=False)
    """The run's folder."""

    env: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)
    """The environment the run's commands use."""

    def to_json(self) -> dict[str, Any]:
        """Return the stored fields as a JSON-ready dict.

        Returns:
            Every field except `folder` and `env`, with tuples as lists.
        """
        result: dict[str, Any] = {}
        for item in fields(self):
            if item.name in ("folder", "env"):
                continue
            value = getattr(self, item.name)
            result[item.name] = list(value) if isinstance(value, tuple) else value
        return result


_STR = (str,)
_TYPES: dict[str, tuple[tuple[type, ...], bool]] = {
    "id": (_STR, False),
    "engine": (_STR, False),
    "model": (_STR, False),
    "effort": (_STR, True),
    "read_only": ((bool,), False),
    "repo": (_STR, False),
    "worktree": (_STR, False),
    "branch": (_STR, True),
    "base": (_STR, True),
    "sandbox": (_STR, False),
    "network": ((bool,), False),
    "read": ((list,), False),
    "read_effective": ((list,), False),
    "trailers": ((list,), False),
    "claim": (_STR, True),
    "status": (_STR, False),
    "turn": ((int,), False),
    "turn_state": (_STR, True),
    "reply": (_STR, True),
    "expected_head": (_STR, True),
    "started_at": ((int, float), False),
    "updated_at": ((int, float), False),
    "ended_at": ((int, float), True),
    "last_exit": ((int,), True),
    "last_reason": (_STR, True),
    "commits": ((list,), False),
    "subject": (_STR, False),
}
"""Each stored field's JSON types and whether it may be null."""


def check_id(run_id: object) -> str:
    """Check a run id.

    Args:
        run_id: The id.

    Returns:
        The id.

    Raises:
        RecordError: The id does not match `[a-z0-9][a-z0-9-]{5,63}`.
    """
    if not isinstance(run_id, str) or _ID.fullmatch(run_id) is None:
        raise RecordError(str(run_id), "not a valid run id")
    return run_id


def new_id(engine: str, now: float | None = None) -> str:
    """Return a new run id.

    Args:
        engine: The engine.
        now: The time; `None` for the current time.

    Returns:
        `<engine>-<yyyymmdd>-<hhmmss>-<6 hex>`, in UTC.
    """
    stamp = time.strftime(
        "%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now)
    )
    return check_id(f"{engine}-{stamp}-{secrets.token_hex(3)}")


def run_dir(run_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Return a run's folder without creating it.

    Args:
        run_id: The run id.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `runs/<run id>` in Ember's state folder.

    Raises:
        RecordError: The id is not valid.
    """
    return paths.runs_dir(env) / check_id(run_id)


def make_run_dir(run_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Create a run's folder at mode 700.

    Args:
        run_id: The run id.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The folder.
    """
    folder = run_dir(run_id, env)
    return paths.ensure_private_dir(folder, root=paths.state_dir(env))


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def from_json(value: object, folder: Path, env: Mapping[str, str] | None = None) -> Run:
    """Build a run from the content of `run.json`.

    Args:
        value: The parsed JSON.
        folder: The run's folder; its name must be the run id.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The run.

    Raises:
        RecordError: A field is missing, has the wrong type or an unknown
            value, or the id does not match the folder.
    """
    name = folder.name
    if not isinstance(value, dict):
        raise RecordError(name, "run.json is not an object")
    values: dict[str, Any] = {}
    for key, (types, nullable) in _TYPES.items():
        if key not in value:
            raise RecordError(name, f"run.json has no {key}")
        item = value[key]
        if item is None and nullable:
            values[key] = None
            continue
        if isinstance(item, bool) and bool not in types:
            raise RecordError(name, f"run.json has an invalid {key}")
        if not isinstance(item, types):
            raise RecordError(name, f"run.json has an invalid {key}")
        if isinstance(item, list):
            if not all(isinstance(entry, str) for entry in item):
                raise RecordError(name, f"run.json has an invalid {key}")
            item = tuple(item)
        values[key] = item
    if values["id"] != name:
        raise RecordError(name, "run.json names another run")
    if values["sandbox"] not in SANDBOXES:
        raise RecordError(name, "run.json has an invalid sandbox")
    if values["status"] not in STATUSES:
        raise RecordError(name, "run.json has an invalid status")
    if values["turn_state"] is not None and values["turn_state"] not in TURN_STATES:
        raise RecordError(name, "run.json has an invalid turn_state")
    for key in ("started_at", "updated_at", "ended_at"):
        if values[key] is not None:
            values[key] = float(values[key])
    return Run(**values, folder=folder, env=_env(env))


def load(run_id: str, env: Mapping[str, str] | None = None) -> Run:
    """Read a run's record.

    Args:
        run_id: The run id.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The run.

    Raises:
        RecordMissingError: No run has this id.
        RecordError: The id or the record is not valid.
    """
    folder = run_dir(run_id, env)
    try:
        text = (folder / RECORD_NAME).read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise RecordMissingError(run_id, "no such run") from error
    except OSError as error:
        raise RecordError(run_id, f"cannot read run.json: {error}") from error
    try:
        value = json.loads(text)
    except ValueError as error:
        raise RecordError(run_id, "run.json is not valid JSON") from error
    return from_json(value, folder, env)


def write_private(path: Path, text: str) -> None:
    """Replace a file atomically with private content.

    The text goes to a mode 600 temporary file in the same folder, which is
    then renamed over `path`.

    Args:
        path: The file.
        text: The content.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}")
    try:
        fd = paths.open_private_file(temporary, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_once(path: Path, text: str) -> bool:
    """Write a private file unless it exists.

    Args:
        path: The file.
        text: The content.

    Returns:
        `True` if the file was written, `False` if it existed.
    """
    if path.exists():
        return False
    write_private(path, text)
    return True


def save(run: Run) -> None:
    """Write a run's record atomically.

    Args:
        run: The run; its folder must exist.
    """
    text = json.dumps(run.to_json(), indent=2) + "\n"
    write_private(run.folder / RECORD_NAME, text)


@contextlib.contextmanager
def record_lock(run: Run) -> Iterator[None]:
    """Hold the run's record lock, which serializes changes to `run.json`.

    Yields:
        Nothing, while the lock is held.
    """
    fd = paths.open_private_file(run.folder / LOCK_NAME)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class RunClosedError(Exception):
    """The run was closed while a command was working on it."""


def update(
    run: Run, changes: Mapping[str, object], *, allow_closed: bool = False
) -> Run:
    """Change fields of a run's stored record under its record lock.

    The record is read again under the lock, so a change made by another
    command, such as a close, is kept.

    Args:
        run: The run.
        changes: The fields to change, by name.
        allow_closed: Change a closed record too.

    Returns:
        The run as saved, with `updated_at` set to now.

    Raises:
        RunClosedError: The stored record is closed and `allow_closed` is
            false.
        RecordError: The stored record is missing or not valid.
    """
    with record_lock(run):
        current = load(run.id, run.env)
        if current.status == CLOSED and not allow_closed:
            raise RunClosedError(f"{run.id} was closed")
        values: dict[str, Any] = {**changes, "updated_at": time.time()}
        changed = replace(current, **values)
        save(changed)
    return changed


def list_runs(
    env: Mapping[str, str] | None = None,
) -> tuple[list[Run], list[RecordError]]:
    """Read every run's record, skipping flow and panel folders.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        The runs, oldest first, and an error for each folder whose record is
        missing or not valid.
    """
    root = paths.runs_dir(env)
    try:
        names = sorted(entry.name for entry in root.iterdir() if entry.is_dir())
    except FileNotFoundError:
        return [], []
    runs: list[Run] = []
    errors: list[RecordError] = []
    for name in names:
        if _FLOW_ID.fullmatch(name) or _PANEL_ID.fullmatch(name):
            continue
        try:
            runs.append(load(name, env))
        except RecordError as error:
            errors.append(error)
    runs.sort(key=lambda run: (run.started_at, run.id))
    return runs, errors
