"""Run records: one folder per run under Ember's state folder.

Each run has a folder `runs/<run id>/` in Ember's state folder, at mode 700,
holding `run.json` (mode 600, replaced atomically), `brief.md` and one
`turn-<n>.md` per reply and `turn-<n>.outcome.json` per turn result. Paths
are absolute strings and timestamps are seconds since the epoch.

Functions that locate run folders take an optional `env` mapping, which
defaults to `os.environ`.
"""

import contextlib
import fcntl
import json
import math
import os
import re
import secrets
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from ember import engines, paths, processes

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

PARENT_COMMANDS = ("gapcheck", "panel", "flow")
"""The Ember commands that start runs of their own and record themselves as parent."""

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
    """The worker's worktree; older read-only records name the source checkout."""

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

    capacity_retries: int = 2
    """The retry limit saved at start."""

    capacity_wait: int = 30
    """Seconds before each capacity resend, saved at start."""

    base_branch: str | None = None
    """The source checkout's branch when the run started, or None if detached."""

    main: str | None = None
    """The main checkout at start; None for a bare repository or an older record."""

    common: str | None = None
    """The common git folder at start; None in records written before this field."""

    detached: bool = False
    """Whether Ember owns a detached worktree for this read-only run."""

    last_screen_change_at: float | None = None
    """When a wait loop last observed a changed screen, or None before activity."""

    sandbox_paths: dict[str, Any] = field(default_factory=dict)
    """Launch-time hidden paths, resolved names and credential inventory."""

    parent: dict[str, Any] | None = None
    """The Ember command that started the run, from `parent()`; None for `emb run`."""

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
    "capacity_retries": ((int,), False),
    "capacity_wait": ((int,), False),
    "base_branch": (_STR, True),
    "main": (_STR, True),
    "common": (_STR, True),
    "detached": ((bool,), False),
    "last_screen_change_at": ((int, float), True),
    "parent": ((dict,), True),
    "sandbox": (_STR, False),
    "sandbox_paths": ((dict,), False),
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

_OUTCOME_TYPES: dict[str, tuple[type, ...]] = {
    "exit": (int,),
    "state": (str,),
    "reason": (str,),
    "provider_error": (bool,),
    "signed_out": (bool,),
    "incomplete": (str,),
    "seconds": (int, float),
    "denied_paths": (list,),
}


def outcome_path(run: Run, turn: int) -> Path:
    """Return the file holding a turn's result.

    Args:
        run: The run.
        turn: The turn number.

    Returns:
        The outcome file's path.
    """
    return run.folder / f"turn-{turn}.outcome.json"


def _nonnegative_number(value: object) -> bool:
    """Return whether a JSON value is a finite nonnegative number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def load_outcome(run: Run, turn: int) -> dict[str, Any]:
    """Read a turn's result, with null exit and seconds for older records.

    Args:
        run: The run.
        turn: The turn number.

    Returns:
        The result's fields.

    Raises:
        RecordError: The outcome file is missing, unreadable or malformed.
    """
    path = outcome_path(run, turn)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise RecordError(run.id, f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RecordError(run.id, f"{path} is not an object")
    value = {"exit": None, "seconds": None, "denied_paths": [], **value}
    result: dict[str, Any] = {}
    for key, types in _OUTCOME_TYPES.items():
        if key not in value:
            raise RecordError(run.id, f"{path} has no {key}")
        item = value[key]
        if item is not None and (
            not isinstance(item, types)
            or (isinstance(item, bool) and bool not in types)
        ):
            raise RecordError(run.id, f"{path} has an invalid {key}")
        result[key] = item
    denials = result["denied_paths"]
    if not isinstance(denials, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and Path(item["path"]).is_absolute()
        and item.get("reason") in ("hidden", "secret location", "protected")
        and (
            item.get("allow_path") is None
            or (
                isinstance(item["allow_path"], str)
                and Path(item["allow_path"]).is_absolute()
            )
        )
        and (item["reason"] == "hidden" or item.get("allow_path") is None)
        for item in denials
    ):
        raise RecordError(run.id, f"{path} has invalid denied_paths")
    if result["state"] not in (None, engines.DONE, engines.FAILED):
        raise RecordError(run.id, f"{path} has an invalid state")
    if result["state"] is None and result["exit"] is not None:
        raise RecordError(run.id, f"{path} has no completed state")
    if result["state"] is not None:
        for key in ("provider_error", "signed_out"):
            if result[key] is None:
                raise RecordError(run.id, f"{path} has an invalid {key}")
    seconds = result["seconds"]
    if seconds is not None and not _nonnegative_number(seconds):
        raise RecordError(run.id, f"{path} has invalid seconds")
    if "provider_kind" in value:
        if value["provider_kind"] not in (None, *engines.PROVIDER_KINDS):
            raise RecordError(run.id, f"{path} has an invalid provider_kind")
        result["provider_kind"] = value["provider_kind"]
    for key in ("attempts", "retry_at", "retry_clock"):
        if key in value:
            result[key] = value[key]
    attempts = result.get("attempts", [])
    if not isinstance(attempts, list) or not all(
        isinstance(attempt, dict)
        and isinstance(attempt.get("id"), str)
        and type(attempt.get("sent")) is bool
        and type(attempt.get("sending")) is bool
        and attempt.get("provider_kind") in (None, *engines.PROVIDER_KINDS)
        and attempt.get("result") in (None, engines.DONE, engines.FAILED)
        and _nonnegative_number(attempt.get("seconds"))
        for attempt in attempts
    ):
        raise RecordError(run.id, f"{path} has invalid attempts")
    for key in ("retry_at", "retry_clock"):
        item = result.get(key)
        if item is not None and not _nonnegative_number(item):
            raise RecordError(run.id, f"{path} has invalid {key}")
    return result


def turn_results(run: Run) -> tuple[list[dict[str, Any]], list[RecordError]]:
    """Read one result per recorded turn, oldest first.

    Args:
        run: The run.

    Returns:
        The numbered results and errors. A broken outcome has null fields.
    """
    results: list[dict[str, Any]] = []
    errors: list[RecordError] = []
    for turn in range(1, run.turn + 1):
        try:
            outcome = load_outcome(run, turn)
        except RecordError as error:
            errors.append(error)
            outcome = dict.fromkeys(_OUTCOME_TYPES)
        results.append({"turn": turn, **outcome})
    return results, errors


def run_denials(run: Run) -> list[dict[str, Any]]:
    """Return denied paths with the run and turn that reported each one."""
    turns, _ = turn_results(run)
    return [
        {**item, "run": run.id, "turn": turn["turn"]}
        for turn in turns
        for item in (turn.get("denied_paths") or [])
    ]


def referenced_denials(
    run_ids: Iterable[str | None],
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read the union of denials from referenced runs, tolerating old records."""
    result: list[dict[str, Any]] = []
    for run_id in dict.fromkeys(run_ids):
        if not run_id:
            continue
        with contextlib.suppress(RecordError):
            result.extend(run_denials(load(run_id, env)))
    return result


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
    value = {
        "sandbox_paths": {},
        "capacity_retries": 2,
        "capacity_wait": 30,
        "base_branch": None,
        "main": None,
        "common": None,
        "detached": False,
        "last_screen_change_at": None,
        "parent": None,
        **value,
    }
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
    from ember import sandbox

    if not sandbox.valid_snapshot(values["sandbox_paths"]):
        raise RecordError(name, "run.json has invalid sandbox_paths")
    if not 0 <= values["capacity_retries"] <= 10:
        raise RecordError(name, "run.json has an invalid capacity_retries")
    if not 5 <= values["capacity_wait"] <= 600:
        raise RecordError(name, "run.json has an invalid capacity_wait")
    if values["id"] != name:
        raise RecordError(name, "run.json names another run")
    if values["sandbox"] not in SANDBOXES:
        raise RecordError(name, "run.json has an invalid sandbox")
    if values["status"] not in STATUSES:
        raise RecordError(name, "run.json has an invalid status")
    if values["turn_state"] is not None and values["turn_state"] not in TURN_STATES:
        raise RecordError(name, "run.json has an invalid turn_state")
    if values["parent"] is not None and not _valid_parent(values["parent"]):
        raise RecordError(name, "run.json has an invalid parent")
    for key in ("started_at", "updated_at", "ended_at", "last_screen_change_at"):
        if values[key] is not None:
            values[key] = float(values[key])
    return Run(**values, folder=folder, env=_env(env))


def _valid_parent(value: dict[str, Any]) -> bool:
    """Return whether a stored parent has the shape `parent()` writes."""
    return (
        set(value) == {"command", "id", "pid", "start"}
        and value["command"] in PARENT_COMMANDS
        and (value["id"] is None or isinstance(value["id"], str))
        and all(
            isinstance(value[key], int) and not isinstance(value[key], bool)
            for key in ("pid", "start")
        )
    )


def parent(command: str, parent_id: str | None = None) -> dict[str, Any] | None:
    """Return the parent entry for a run that this process starts.

    Args:
        command: The starting command, one of `PARENT_COMMANDS`.
        parent_id: The flow or panel id; `None` for a gap check.

    Returns:
        The command, the id, and this process's id and start time in clock
        ticks since boot; `None` when `/proc` cannot identify this process.
    """
    found = processes.current()
    if found is None:
        return None
    return {"command": command, "id": parent_id, "pid": found[0], "start": found[1]}


def orphaned(run: Run) -> bool:
    """Return whether a run's parent command ended and left the run uncollected.

    A run started by `emb run` has no parent and is never orphaned. A process
    id reused by another process does not count as the parent, because the
    start time is compared too.

    Args:
        run: The run.

    Returns:
        True when the run is starting, running or waiting, it records a
        parent, and that parent process is no longer running.
    """
    if run.parent is None or run.status not in (STARTING, RUNNING, WAITING):
        return False
    return not processes.running((run.parent["pid"], run.parent["start"]))


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


def is_run_folder(name: str) -> bool:
    """Return whether a folder in the runs folder holds a run, not a flow or panel.

    Args:
        name: The folder's name.

    Returns:
        False for a flow or panel folder, True otherwise.
    """
    return not (_FLOW_ID.fullmatch(name) or _PANEL_ID.fullmatch(name))


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
        if not is_run_folder(name):
            continue
        try:
            runs.append(load(name, env))
        except RecordError as error:
            errors.append(error)
    runs.sort(key=lambda run: (run.started_at, run.id))
    return runs, errors
