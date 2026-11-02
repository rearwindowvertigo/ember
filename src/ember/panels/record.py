"""Private panel records, lifetime locks, member logs and answer summaries."""

import contextlib
import copy
import fcntl
import json
import math
import os
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ember import paths
from ember.runs import record

PANEL_ID = re.compile(r"panel-\d{8}-\d{6}-[0-9a-f]{6}")
PANEL_NAME, QUESTION_NAME, BRIEF_NAME, SUMMARY_NAME, MEMBERS_DIR = (
    "panel.json",
    "question.md",
    "brief.md",
    "summary.md",
    "members",
)
PANEL_STATUSES = ("running", "done", "incomplete")
MEMBER_STATUSES = (
    "pending",
    "starting",
    "running",
    "answered",
    "failed",
    "stalled",
    "timed out",
    "interrupted",
    "not started",
)
UNFINISHED = ("starting", "running", "stalled", "timed out", "interrupted")
_PANEL_FIELDS = {
    "schema_version": "int",
    "id": "str",
    "status": "str",
    "dir": "str",
    "question_file": "str",
    "read": "[str]",
    "claim": "str?",
    "stall_minutes": "number",
    "timeout_minutes": "number",
    "started_at": "number",
    "updated_at": "number",
    "ended_at": "number?",
    "exit": "int?",
    "members": "[dict]",
    "left_out": "[dict]",
}
_MEMBER_FIELDS = {
    "n": "int",
    "engine": "str",
    "model": "str",
    "effort": "str?",
    "source": "str",
    "status": "str",
    "run": "str?",
    "exit": "int?",
    "reason": "str?",
    "reply": "str?",
    "bytes": "int",
    "changed": "[str]",
    "seconds": "number?",
    "closed": "bool",
    "close_error": "str?",
    "attempts": "[dict]",
}


class PanelError(Exception):
    """A panel id or record cannot be used."""

    def __init__(self, panel_id: str, message: str) -> None:
        """Keep the affected panel id with its diagnostic."""
        self.panel_id = panel_id
        super().__init__(f"{panel_id}: {message}")


class PanelMissingError(PanelError):
    """The panel folder or record does not exist."""


class Busy(Exception):  # noqa: N818 - the fixed panel interface names this exception.
    """Another open file description holds the panel lock."""


class LockError(Exception):
    """The runtime folder or panel lock cannot be used."""


def new_id(now: float | None = None) -> str:
    """Return a panel id with a UTC timestamp and six random hex digits."""
    return record.new_id("panel", now)


def is_panel_id(name: object) -> bool:
    """Return whether the entire value matches the panel namespace."""
    return isinstance(name, str) and PANEL_ID.fullmatch(name) is not None


def folder(panel_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Return a panel folder, rejecting ids outside the panel namespace."""
    if not is_panel_id(panel_id):
        raise PanelError(panel_id, "not a valid panel id")
    return record.run_dir(panel_id, env)


def new_panel(
    panel_id: str,
    *,
    dir: str,
    question_file: str,
    read: Sequence[str],
    claim: str | None,
    stall_minutes: float,
    timeout_minutes: float,
    members: Sequence[Mapping[str, Any]],
    left_out: Sequence[Mapping[str, str]],
    now: float | None = None,
) -> dict[str, Any]:
    """Build a panel record with independent nested member and attempt lists."""
    at = time.time() if now is None else now
    panel = {
        "schema_version": 1,
        "id": panel_id,
        "status": "running",
        "dir": dir,
        "question_file": question_file,
        "read": list(read),
        "claim": claim,
        "stall_minutes": stall_minutes,
        "timeout_minutes": timeout_minutes,
        "started_at": at,
        "updated_at": at,
        "ended_at": None,
        "exit": None,
        "members": copy.deepcopy([dict(member) for member in members]),
        "left_out": [dict(member) for member in left_out],
    }
    _validate(panel_id, panel)
    return panel


def _matches(value: object, kind: str) -> bool:
    """Check JSON types, rejecting booleans and nonfinite numbers as times."""
    if kind.endswith("?"):
        return value is None or _matches(value, kind[:-1])
    if kind.startswith("["):
        return isinstance(value, list) and all(
            _matches(item, kind[1:-1]) for item in value
        )
    if kind == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    return type(value) is {"str": str, "int": int, "bool": bool, "dict": dict}[kind]


def _fields(
    panel_id: str, value: object, fields: Mapping[str, str], label: str
) -> None:
    """Require exactly the declared fields with their JSON types."""
    if not isinstance(value, dict):
        raise PanelError(panel_id, f"{label} is not an object")
    if set(value) != set(fields):
        raise PanelError(panel_id, f"{label} has missing or unknown fields")
    for key, kind in fields.items():
        if not _matches(value[key], kind):
            raise PanelError(panel_id, f"{label} has an invalid {key}")


def _validate(panel_id: str, panel: object) -> None:
    """Validate the record and each nested member, attempt and exclusion."""
    _fields(panel_id, panel, _PANEL_FIELDS, PANEL_NAME)
    assert isinstance(panel, dict)
    if not is_panel_id(panel_id) or panel["id"] != panel_id:
        raise PanelError(panel_id, "panel.json names an invalid or different panel")
    if panel["schema_version"] != 1 or panel["status"] not in PANEL_STATUSES:
        raise PanelError(panel_id, "panel.json has an invalid version or status")
    for key in ("stall_minutes", "timeout_minutes"):
        if panel[key] <= 0:
            raise PanelError(panel_id, f"panel.json has an invalid {key}")
    for n, member in enumerate(panel["members"], 1):
        _fields(panel_id, member, _MEMBER_FIELDS, f"member {n}")
        if member["n"] != n or member["status"] not in MEMBER_STATUSES:
            raise PanelError(panel_id, f"member {n} has an invalid number or status")
        if member["bytes"] < 0 or (
            member["seconds"] is not None and member["seconds"] < 0
        ):
            raise PanelError(panel_id, f"member {n} has a negative count")
        for attempt in member["attempts"]:
            _fields(
                panel_id,
                attempt,
                {
                    "kind": "str",
                    "at": "number",
                    "exit": "int?",
                    **({"run": "str?"} if "run" in attempt else {}),
                },
                f"member {n} attempt",
            )
            if attempt["kind"] not in ("start", "stall", "recover", "retry"):
                raise PanelError(panel_id, f"member {n} has an invalid attempt kind")
    for item in panel["left_out"]:
        _fields(panel_id, item, {"value": "str", "reason": "str"}, "left-out member")


def create(
    panel: Mapping[str, Any],
    question: str,
    brief: str,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Create the private panel artifacts while the caller holds its lock."""
    panel = dict(panel)
    panel_id = panel["id"]
    directory = folder(panel_id, env)
    _validate(panel_id, panel)
    paths.ensure_private_dir(directory.parent, root=paths.state_dir(env))
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise PanelError(panel_id, "the panel folder already exists") from error
    record.make_run_dir(panel_id, env)
    paths.ensure_private_dir(directory / MEMBERS_DIR, root=directory)
    record.write_private(directory / QUESTION_NAME, question)
    record.write_private(directory / BRIEF_NAME, brief)
    record.write_private(directory / PANEL_NAME, json.dumps(panel, indent=2) + "\n")
    write_summary(panel, env)
    return directory


def load(panel_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read a panel, distinguishing missing records from broken records."""
    path = folder(panel_id, env) / PANEL_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PanelMissingError(panel_id, "no such panel") from error
    except (OSError, ValueError) as error:
        raise PanelError(panel_id, f"cannot read panel.json: {error}") from error
    _validate(panel_id, value)
    return value


def save(panel: dict[str, Any], env: Mapping[str, str] | None = None) -> None:
    """Validate and atomically replace the record with an updated timestamp."""
    _validate(panel["id"], panel)
    panel["updated_at"] = time.time()
    record.write_private(
        folder(panel["id"], env) / PANEL_NAME, json.dumps(panel, indent=2) + "\n"
    )


def list_panels(
    env: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[PanelError]]:
    """List panels oldest first and report each unreadable panel separately."""
    try:
        names = sorted(
            item.name
            for item in paths.runs_dir(env).iterdir()
            if item.is_dir() and is_panel_id(item.name)
        )
    except FileNotFoundError:
        return [], []
    panels: list[dict[str, Any]] = []
    errors: list[PanelError] = []
    for name in names:
        try:
            panels.append(load(name, env))
        except PanelError as error:
            errors.append(error)
    panels.sort(key=lambda panel: (panel["started_at"], panel["id"]))
    return panels, errors


def lock_path(panel_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Return the lifetime lock path for a valid panel id."""
    if not is_panel_id(panel_id):
        raise LockError(f"invalid panel id: {panel_id!r}")
    try:
        return paths.runtime_dir(env) / "locks" / f"{panel_id}.lock"
    except (paths.RuntimeDirError, OSError) as error:
        raise LockError(f"cannot find the lock of {panel_id}: {error}") from error


@contextlib.contextmanager
def hold(panel_id: str, env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Hold a nonblocking exclusive lock and release it on every exit."""
    path = lock_path(panel_id, env)
    try:
        paths.ensure_private_dir(path.parent, root=path.parent.parent)
        fd = paths.open_private_file(path)
    except (paths.RuntimeDirError, OSError) as error:
        raise LockError(f"cannot open the lock of {panel_id}: {error}") from error
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy(f"another process holds panel {panel_id}") from error
        except OSError as error:
            raise LockError(f"cannot take the lock of {panel_id}: {error}") from error
        yield
    finally:
        os.close(fd)


def live(panel_id: str, env: Mapping[str, str] | None = None) -> bool:
    """Probe the lifetime lock using a fresh open file description."""
    try:
        with hold(panel_id, env):
            return False
    except Busy:
        return True


def summary_text(
    panel: Mapping[str, Any],
    question: str,
    replies: Mapping[int, str],
) -> str:
    """Render the question, member outcomes and every available reply in order."""
    members = panel["members"]
    answered = sum(member["status"] == "answered" for member in members)
    started = (
        datetime.fromtimestamp(panel["started_at"])
        .astimezone()
        .isoformat(timespec="seconds")
    )
    sections = [
        f"# Panel {panel['id']}\n\nCheckout: {panel['dir']}\nStarted: {started}\n"
        f"Members: {len(members)}; answered: {answered}; status: {panel['status']}",
        f"## Question\n\n{question.rstrip(chr(10))}",
    ]
    for member in members:
        seconds = "-" if member["seconds"] is None else f"{round(member['seconds'])}s"
        code = "-" if member["exit"] is None else str(member["exit"])
        body = (
            f"## Member {member['n']}: {member['engine']}:{member['model']} · "
            f"{member['effort'] if member['effort'] is not None else 'default'}\n\n"
            f"Status: {member['status']} · exit {code} · {seconds} · "
            f"run {member['run'] or '-'} · {member['bytes']} bytes"
        )
        if member["changed"]:
            body += "\nChanged paths: " + ", ".join(member["changed"])
        if member["reason"] is not None:
            body += f"\nReason: {member['reason']}"
        reply = replies.get(member["n"], "")
        body += "\n\n" + (reply.rstrip("\n") if reply else "(no answer)")
        sections.append(body)
    if panel["left_out"]:
        sections.append(
            "## Left out\n\n"
            + "\n".join(
                f"- {item['value']}: {item['reason']}" for item in panel["left_out"]
            )
        )
    return "\n\n".join(sections) + "\n"


def write_summary(
    panel: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> Path:
    """Atomically write a summary, retaining readable replies of failed members."""
    directory = folder(panel["id"], env)
    question = (directory / QUESTION_NAME).read_text(encoding="utf-8")
    replies: dict[int, str] = {}
    for member in panel["members"]:
        if member["reply"]:
            try:
                replies[member["n"]] = Path(member["reply"]).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                replies[member["n"]] = f"(the reply file cannot be read: {error})"
    path = directory / SUMMARY_NAME
    record.write_private(path, summary_text(panel, question, replies))
    return path


def append_log(
    panel_id: str,
    n: int,
    text: str,
    env: Mapping[str, str] | None = None,
) -> None:
    """Append captured member diagnostics to its private log."""
    if type(n) is not int or n < 1:
        raise PanelError(panel_id, "invalid member number")
    path = folder(panel_id, env) / MEMBERS_DIR / f"{n}.log"
    fd = paths.open_private_file(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
