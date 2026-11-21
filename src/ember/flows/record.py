"""Private flow records, numbered event and answer logs, and watch cursors."""

import contextlib
import fcntl
import json
import math
import os
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ember import choices, paths
from ember.flows.plan import Plan, Worker, waves
from ember.runs import record

FLOW_NAME, PLAN_NAME, EVENTS_NAME, ANSWERS_NAME, WATCH_NAME = (
    "flow.json",
    "plan.json",
    "events.jsonl",
    "answers.jsonl",
    "watch.json",
)
FLOW_ID = re.compile(r"flow-\d{8}-\d{6}-[0-9a-f]{6}")
TASK_STATUSES = (
    "pending",
    "writing",
    "reviewing",
    "fixing",
    "accepted",
    "merging",
    "merged",
    "stopped",
    "conflict",
    "blocked",
    "dropped",
)
FLOW_STATUSES = (
    "starting",
    "running",
    "checking",
    "stopped",
    "failed",
    "done",
    "cancelled",
)
ENDED = ("done", "failed", "cancelled")
CANCEL_NAME = "cancel.json"
ANSWER_KINDS = ("send", "accept", "review", "worker", "drop")
ANSWERS_LOCK_NAME = "answers.lock"
QUEUE_KINDS = ("withdraw", "reviewer")
"""Answer log lines that change no task: `withdraw` removes a task's unapplied
answer, and `reviewer`, which has no task, changes the flow's saved reviewer."""

# A trailing question mark permits null; brackets require a list of that type.
_FLOW_FIELDS = {
    "id": "str",
    "plan": "str",
    "plan_waves": "[[str]]",
    "dir": "str",
    "main": "str",
    "common": "str?",
    "base": "str",
    "base_branch": "str?",
    "branch": "str",
    "worktree": "str",
    "tip": "str",
    "writer": "worker",
    "reviewer": "worker",
    "rounds": "int",
    "review_budget": "dict",
    "parallel": "int",
    "check_timeout": "int",
    "check": "[str]",
    "check_changes": "[dict]",
    "trailers": "[str]",
    "read": "[str]",
    "stall_minutes": "number",
    "claim": "str?",
    "status": "str",
    "reason": "str?",
    "wave": "int",  # The flow's runner pass count.
    "check_pending": "bool",
    "check_base": "bool",
    "checks": "[dict]",
    "answers_seen": "int",
    "halted": "str?",
    "abandoned": "[str]",
    "runner": "dict?",
    "started_at": "number",
    "updated_at": "number",
    "ended_at": "number?",
    "tasks": "[dict]",
}
_TASK_FIELDS = {
    "id": "str",
    "subject": "str",
    "status": "str",
    "reason": "str?",
    "wave": "int?",  # The runner pass assigned to the task.
    "writer": "worker?",
    "reviewer": "worker?",
    "writer_run": "str?",
    "branch": "str?",
    "worktree": "str?",
    "task_base": "str?",
    "head": "str?",
    "round": "int",
    "reviews": "[dict]",
    "reviewer_run": "str?",
    "findings": "[dict]",
    "changed_files": "[str]",
    "outside_files": "[str]",
    "corrections": "[str]",
    "entry": "str?",
    "answer": "int?",
    "merge_tip": "str?",
    "merge_commit": "str?",
    "cleanup": "[dict]",
}
_WORKER_FIELDS = {"engine": "str", "model": "str", "effort": "str?", "source": "str"}


class FlowError(Exception):
    """A flow id or stored flow record is not usable."""

    def __init__(self, flow_id: str, message: str) -> None:
        """Keep the flow id with its diagnostic."""
        super().__init__(f"{flow_id}: {message}")
        self.flow_id = flow_id


class FlowMissingError(FlowError):
    """The flow folder or flow.json does not exist."""


def new_id(now: float | None = None) -> str:
    """Return a flow id with a UTC timestamp and six random hex digits."""
    return record.new_id("flow", now)


def is_flow_id(name: object) -> bool:
    """Return whether a value has the complete flow id format."""
    return isinstance(name, str) and FLOW_ID.fullmatch(name) is not None


def folder(flow_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Return the flow folder, refusing ids outside the flow namespace."""
    if not is_flow_id(flow_id):
        raise FlowError(flow_id, "not a valid flow id")
    return record.run_dir(flow_id, env)


def _new_task(task_id: str, subject: str) -> dict[str, Any]:
    """Initialize every task field with independent lists."""
    task: dict[str, Any] = dict.fromkeys(_TASK_FIELDS)
    task.update(id=task_id, subject=subject, status="pending", round=0)
    for key in (
        "reviews",
        "findings",
        "changed_files",
        "outside_files",
        "corrections",
        "cleanup",
    ):
        task[key] = []
    return task


def new_flow(
    flow_id: str,
    plan: Plan,
    *,
    top: str,
    main: str,
    worktree: str,
    base: str,
    writer: Worker,
    reviewer: Worker,
    stall_minutes: float,
    claim: str | None,
    now: float | None = None,
    base_branch: str | None = None,
    whole_review: bool = True,
    check_base: bool = False,
    common: str | None = None,
) -> dict[str, Any]:
    """Build the initial flow record without creating any files.

    `top` is the checkout the plan names, `main` its main checkout and
    `worktree` the flow worktree's resolved path, from
    `ember.paths.worktree_path`. `common` is the resolved common git folder;
    the record keeps these paths for merge, cleanup and prune.
    """
    if not is_flow_id(flow_id):
        raise FlowError(flow_id, "not a valid flow id")
    at = time.time() if now is None else now
    return {
        "id": flow_id,
        choices.OPENCODE_ID_FORMAT: 1,
        "plan": str(plan.path),
        "plan_waves": [[task.id for task in wave] for wave in waves(plan)],
        "dir": top,
        "main": main,
        "common": common,
        "base": base,
        "base_branch": base_branch,
        "branch": f"ember/flow/{flow_id}",
        "worktree": worktree,
        "tip": base,
        "writer": writer.to_json(),
        "reviewer": reviewer.to_json(),
        "rounds": plan.rounds,
        "review_budget": {
            "value": plan.rounds + int(plan.rounds_source == "setting"),
            "unit": "reviews" if plan.rounds_source == "setting" else "fix_rounds",
            "source": {
                "setting": "flow.review_rounds",
                "plan": "plan.rounds",
                "default": "default",
            }[plan.rounds_source],
        },
        "parallel": plan.parallel,
        "check_timeout": plan.check_timeout,
        "check": list(plan.check),
        "check_changes": [],
        "trailers": list(plan.trailers),
        "read": list(plan.read),
        "stall_minutes": stall_minutes,
        "whole_review": whole_review,
        "claim": claim,
        "status": "starting",
        "reason": None,
        "wave": 0,
        "check_pending": check_base,
        "check_base": check_base,
        "checks": [],
        "answers_seen": 0,
        "halted": None,
        "abandoned": [],
        "runner": None,
        "started_at": at,
        "updated_at": at,
        "ended_at": None,
        "tasks": [_new_task(task.id, task.subject) for task in plan.tasks],
    }


def _matches(value: object, kind: str) -> bool:
    """Check one stored field's JSON type, excluding booleans from numbers."""
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
    if kind == "worker":
        return isinstance(value, dict) and all(
            key in value and _matches(value[key], field)
            for key, field in _WORKER_FIELDS.items()
        )
    return type(value) is {"str": str, "int": int, "bool": bool, "dict": dict}[kind]


def _fields(flow_id: str, value: object, fields: Mapping[str, str], label: str) -> None:
    """Require every declared field while permitting future recovery metadata."""
    if not isinstance(value, dict):
        raise FlowError(flow_id, f"{label} is not an object")
    for key, kind in fields.items():
        if key not in value:
            raise FlowError(flow_id, f"{label} has no {key}")
        if not _matches(value[key], kind):
            raise FlowError(flow_id, f"{label} has an invalid {key}")


def _validate(flow_id: str, flow: object) -> None:
    """Validate the flow and nested task records before returning or saving."""
    _fields(flow_id, flow, _FLOW_FIELDS, FLOW_NAME)
    assert isinstance(flow, dict)
    if flow["id"] != flow_id:
        raise FlowError(flow_id, "flow.json names another flow")
    if flow["status"] not in FLOW_STATUSES:
        raise FlowError(flow_id, "flow.json has an invalid status")
    if "preparation_history" in flow and not isinstance(
        flow["preparation_history"], list
    ):
        raise FlowError(flow_id, "flow.json has an invalid preparation_history")
    for attempt in [
        *flow.get("preparation_history", []),
        *([flow["preparation"]] if "preparation" in flow else []),
    ]:
        if not isinstance(attempt, dict) or not record._valid_preparation(attempt):
            raise FlowError(flow_id, "flow.json has an invalid preparation")
    if "preparation_halt" in flow and not _matches(flow["preparation_halt"], "str?"):
        raise FlowError(flow_id, "flow.json has an invalid preparation_halt")
    if "answers_taken" in flow and not _matches(flow["answers_taken"], "[int]"):
        raise FlowError(flow_id, "flow.json has an invalid answers_taken")
    for key, low, high in (
        ("rounds", 0, 3),
        ("parallel", 1, 8),
        ("check_timeout", 1, None),
        ("wave", 0, None),
        ("answers_seen", 0, None),
    ):
        if flow[key] < low or (high is not None and flow[key] > high):
            raise FlowError(flow_id, f"flow.json has an invalid {key}")
    if flow["stall_minutes"] <= 0:
        raise FlowError(flow_id, "flow.json has an invalid stall_minutes")
    budget = flow["review_budget"]
    _fields(
        flow_id,
        budget,
        {"value": "int", "unit": "str", "source": "str"},
        "review_budget",
    )
    reviews = budget["source"] == "flow.review_rounds"
    if (
        budget["source"]
        not in ("flow.review_rounds", "plan.rounds", "default", "unknown")
        or budget["unit"] != ("reviews" if reviews else "fix_rounds")
        or budget["value"] != flow["rounds"] + int(reviews)
    ):
        raise FlowError(flow_id, "flow.json has an invalid review_budget")
    if flow["runner"] is not None:
        _fields(
            flow_id, flow["runner"], {"pid": "int", "started_at": "number"}, "runner"
        )
    for check in flow["checks"]:
        _fields(
            flow_id,
            check,
            {"wave": "int", "exit": "int", "log": "str", "at": "number"},
            "check",
        )
    for change in flow["check_changes"]:
        _fields(
            flow_id,
            change,
            {"at": "number", "old": "[str]", "new": "[str]", "reason": "str"},
            "check change",
        )
    seen: set[str] = set()
    for task in flow["tasks"]:
        _fields(flow_id, task, _TASK_FIELDS, "task")
        for review in task["reviews"]:
            if "outside_files" in review:
                _fields(flow_id, review, {"outside_files": "[dict]"}, "review")
                for entry in review["outside_files"]:
                    _fields(
                        flow_id,
                        entry,
                        {"file": "str", "needed": "bool", "reason": "str"},
                        "review outside file",
                    )
                    if not entry["reason"].strip():
                        raise FlowError(
                            flow_id, "review outside file has a blank reason"
                        )
        if (
            task["id"] in seen
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", task["id"]) is None
        ):
            raise FlowError(flow_id, "flow.json has an invalid task id")
        seen.add(task["id"])
        if task["status"] not in TASK_STATUSES:
            raise FlowError(flow_id, f"task {task['id']} has an invalid status")
        if task["entry"] not in (None, "write", "send", "review"):
            raise FlowError(flow_id, f"task {task['id']} has an invalid entry")
        if task["round"] < 0:
            raise FlowError(flow_id, f"task {task['id']} has an invalid round")
        if task.get("preparation_blocked") not in (
            None,
            "writer",
            "reviewer",
            "continue",
            "send",
        ):
            raise FlowError(
                flow_id, f"task {task['id']} has an invalid preparation_blocked"
            )


def create(
    flow: Mapping[str, Any], plan_text: str, env: Mapping[str, str] | None = None
) -> Path:
    """Publish a private flow folder with its plan, record and first event."""
    flow = dict(flow)
    flow_id = flow["id"]
    directory = folder(flow_id, env)
    _validate(flow_id, flow)
    try:
        with paths.create_record_dir(directory, root=paths.state_dir(env)) as temporary:
            record.write_private(temporary / PLAN_NAME, plan_text)
            record.write_private(
                temporary / FLOW_NAME, json.dumps(dict(flow), indent=2) + "\n"
            )
            _append(
                flow_id,
                EVENTS_NAME,
                "seq",
                {"kind": "flow", "status": "starting", "reason": None},
                env,
                directory=temporary,
            )
    except FileExistsError as error:
        raise FlowError(flow_id, "the flow folder already exists") from error
    return directory


def load(flow_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read and validate flow.json, distinguishing missing from broken records."""
    path = folder(flow_id, env) / FLOW_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FlowMissingError(flow_id, "no such flow") from error
    except (OSError, ValueError) as error:
        raise FlowError(flow_id, f"cannot read flow.json: {error}") from error
    if isinstance(value, dict):
        value.setdefault("plan_waves", [])
        value.setdefault("base_branch", None)
        value.setdefault("common", None)
        value.setdefault("check_base", False)
        value.setdefault("check_changes", [])
        if "review_budget" not in value:
            value["review_budget"] = _saved_budget(path.parent, value.get("rounds"))
        if isinstance(value.get("tasks"), list):
            for task in value["tasks"]:
                if isinstance(task, dict):
                    task.setdefault("corrections", [])
    _validate(flow_id, value)
    if choices.OPENCODE_ID_FORMAT not in value:
        for row in (value, *value["tasks"]):
            for name in ("writer", "reviewer"):
                worker = row.get(name)
                if worker is not None and worker["engine"] == "opencode":
                    worker["model"] = choices.full_opencode_id(worker["model"])
        value[choices.OPENCODE_ID_FORMAT] = 1
    return value


def _saved_budget(directory: Path, rounds: object) -> dict[str, Any]:
    """Recover only the source named explicitly in a saved original plan."""
    try:
        original = json.loads((directory / PLAN_NAME).read_text(encoding="utf-8"))
    except OSError, ValueError:
        original = None
    explicit = (
        isinstance(original, dict)
        and type(original.get("rounds")) is int
        and original["rounds"] == rounds
    )
    return {
        "value": rounds,
        "unit": "fix_rounds",
        "source": "plan.rounds" if explicit else "unknown",
    }


def save(flow: dict[str, Any], env: Mapping[str, str] | None = None) -> None:
    """Atomically replace a validated record and update its timestamp."""
    path = folder(flow["id"], env) / FLOW_NAME
    _validate(flow["id"], flow)
    flow[choices.OPENCODE_ID_FORMAT] = 1
    flow["updated_at"] = time.time()
    record.write_private(path, json.dumps(flow, indent=2) + "\n")


def cancel_request(
    flow_id: str, env: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """Read a pending cancellation written under lifecycle.lock."""
    try:
        value = json.loads((folder(flow_id, env) / CANCEL_NAME).read_text())
    except FileNotFoundError:
        return None
    _fields(flow_id, value, {"at": "number", "reason": "str?"}, CANCEL_NAME)
    return value


def preparation_failed(flow: Mapping[str, Any]) -> bool:
    """Return whether flow or child preparation stopped this attempt."""
    return bool(flow.get("preparation_halt")) or (flow.get("preparation") or {}).get(
        "state"
    ) in ("failed", "timeout", "interrupted", "not-started")


def finish_cancel(
    flow: dict[str, Any], request: Mapping[str, Any], env: Mapping[str, str]
) -> None:
    """Save cancellation and its end event while lifecycle.lock is held."""
    flow.update(status="cancelled", reason=request["reason"], ended_at=request["at"])
    save(flow, env)
    append_event(
        flow["id"],
        {
            "kind": "end",
            "attempt": flow.get("attempt", 0),
            "status": "cancelled",
            "reason": request["reason"],
            "exit": 79 if preparation_failed(flow) else 82,
        },
        env,
    )


def _lines(data: bytes) -> list[dict[str, Any] | None]:
    """Decode complete object lines, keeping unreadable lines as placeholders."""
    result: list[dict[str, Any] | None] = []
    for line in data.split(b"\n")[:-1]:
        try:
            value = json.loads(line)
        except ValueError, UnicodeError:
            value = None
        result.append(value if isinstance(value, dict) else None)
    return result


def _read_lines(path: Path) -> list[dict[str, Any] | None]:
    """Read complete JSON lines, treating a missing log as empty."""
    try:
        return _lines(path.read_bytes())
    except FileNotFoundError:
        return []


def _repair_tail(path: Path, fd: int, data: bytes) -> None:
    """Preserve an incomplete tail privately, then truncate it under the lock."""
    end = data.rfind(b"\n") + 1
    if end == len(data):
        return
    backup = path.with_name(f"{path.name}.incomplete-{time.time_ns()}")
    backup_fd = paths.open_private_file(backup, os.O_WRONLY | os.O_EXCL)
    with os.fdopen(backup_fd, "wb") as stream:
        stream.write(data[end:])
        stream.flush()
        os.fsync(stream.fileno())
    os.ftruncate(fd, end)
    os.fsync(fd)


def _append(
    flow_id: str,
    name: str,
    field: str,
    value: Mapping[str, Any],
    env: Mapping[str, str] | None,
    *,
    directory: Path | None = None,
) -> dict[str, Any]:
    """Allocate a number, repair the tail and append under the log's lock."""
    directory = folder(flow_id, env) if directory is None else directory
    path = directory / name
    fd = paths.open_private_file(path, os.O_RDWR | os.O_APPEND)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read()
        lines = _lines(data)
        numbers = [
            item[field]
            for item in lines
            if item is not None and type(item.get(field)) is int
        ]
        if name == ANSWERS_NAME:
            numbers.extend(
                int(entry.stem)
                for entry in (directory / "answers").glob("*.md")
                if re.fullmatch(r"[0-9]+", entry.stem)
            )
        number = max([len(lines), *numbers], default=0) + 1
        item = {**value, field: number, "at": time.time()}
        if name == EVENTS_NAME and "at" in value:
            item["at"] = value["at"]
        if name == ANSWERS_NAME:
            item = {
                "task": None,
                "kind": None,
                "file": None,
                "commit": None,
                "worker": None,
                **item,
                choices.OPENCODE_ID_FORMAT: 1,
            }
            if item["kind"] == "reviewer":
                if item["task"] is not None or not isinstance(item["worker"], str):
                    raise FlowError(flow_id, "reviewer answer needs a worker only")
            elif item["kind"] not in (
                *ANSWER_KINDS,
                *QUEUE_KINDS,
            ) or not isinstance(item["task"], str):
                raise FlowError(flow_id, "answer needs a task and a valid kind")
            if item["kind"] == "send":
                source = item["file"]
                if not isinstance(source, str):
                    raise FlowError(flow_id, "send answer needs a file")
                text = Path(source).read_text(encoding="utf-8")
                if not text.strip():
                    raise FlowError(flow_id, "send answer file is empty")
                answers = paths.ensure_private_dir(
                    directory / "answers", root=directory
                )
                target = answers / f"{number}.md"
                correction_fd = paths.open_private_file(target, os.O_WRONLY | os.O_EXCL)
                with os.fdopen(correction_fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                item["file"] = str(target)
        encoded = (json.dumps(item) + "\n").encode("utf-8")
        _repair_tail(path, fd, data)
        if os.write(fd, encoded) != len(encoded):
            raise OSError(f"short write to {path}")
        os.fsync(fd)
        return item
    finally:
        os.close(fd)


def append_event(
    flow_id: str,
    event: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Append an event with a consecutive seq and a supplied or current time."""
    return _append(flow_id, EVENTS_NAME, "seq", event, env)


def read_events(
    flow_id: str,
    env: Mapping[str, str] | None = None,
    *,
    after: int = 0,
) -> list[dict[str, Any]]:
    """Read valid complete events after a sequence number, skipping bad lines."""
    return [
        item
        for item in _read_lines(folder(flow_id, env) / EVENTS_NAME)
        if item is not None and type(item.get("seq")) is int and item["seq"] > after
    ]


def append_answer(
    flow_id: str,
    answer: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Allocate and append an answer; copy its send file while holding the lock.

    For a send answer, `file` is the source filename on input and the private
    numbered copy on output. Numbers exceed complete log lines, stored
    numbers and existing correction filenames. Existing corrections are never
    replaced. Callers must not allocate the copy's number.
    """
    return _append(flow_id, ANSWERS_NAME, "n", answer, env)


def read_answers(
    flow_id: str, env: Mapping[str, str] | None = None
) -> list[dict[str, Any] | None]:
    """Read answers and convert unmarked worker ids without changing the log.

    Each unreadable line retains its place as None.
    """
    answers = _read_lines(folder(flow_id, env) / ANSWERS_NAME)
    for answer in answers:
        if answer is None or choices.OPENCODE_ID_FORMAT in answer:
            continue
        worker = answer.get("worker")
        if isinstance(worker, str) and worker.startswith("opencode:"):
            answer["worker"] = "opencode:" + choices.full_opencode_id(
                worker.removeprefix("opencode:")
            )
            answer[choices.OPENCODE_ID_FORMAT] = 1
    return answers


@contextlib.contextmanager
def answers_lock(flow_id: str, env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Hold the lock that serializes queueing an answer with applying one.

    `emb flow --lead` holds it from reading the queue through its append,
    and the runner from checking that an answer is still in effect through
    saving its marker. An answer the command reports as replaced or
    withdrawn is therefore never applied afterwards.

    Yields:
        Nothing, while the lock is held.
    """
    fd = paths.open_private_file(folder(flow_id, env) / ANSWERS_LOCK_NAME)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def answer_queue(
    flow: Mapping[str, Any], lines: Sequence[Mapping[str, Any] | None]
) -> list[tuple[int, Mapping[str, Any], list[int]]]:
    """Return the unapplied answers that are in effect, oldest first.

    A line is unapplied from the flow's cursor (`answers_seen`) on, unless the
    runner took it ahead of the cursor (`answers_taken`). A later line for the
    same task replaces the earlier unapplied ones. A `withdraw` line removes
    them and is not in effect itself. `reviewer` lines have no task; the last
    one is in effect. Unreadable lines are left to the runner.

    Args:
        flow: The flow record.
        lines: The answer log, as `read_answers` returns it.

    Returns:
        For each answer in effect, its line index, the line, and the numbers
        of the answers it replaces.
    """
    taken = set(flow.get("answers_taken", []))
    queued: dict[object, list[tuple[int, Mapping[str, Any]]]] = {}
    for index in range(flow["answers_seen"], len(lines)):
        answer = lines[index]
        if answer is None:
            continue
        waiting = queued.setdefault(answer.get("task"), [])
        if index in taken or answer.get("kind") == "withdraw":
            waiting.clear()
        else:
            waiting.append((index, answer))
    result = [
        (
            waiting[-1][0],
            waiting[-1][1],
            [answer.get("n", index + 1) for index, answer in waiting[:-1]],
        )
        for waiting in queued.values()
        if waiting
    ]
    return sorted(result, key=lambda item: item[0])


def read_cursor(flow_id: str, env: Mapping[str, str] | None = None) -> int:
    """Read the watch cursor, returning zero only when no cursor exists."""
    try:
        value = json.loads(
            (folder(flow_id, env) / WATCH_NAME).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as error:
        raise FlowError(flow_id, f"cannot read watch.json: {error}") from error
    if (
        not isinstance(value, dict)
        or type(value.get("seq")) is not int
        or value["seq"] < 0
    ):
        raise FlowError(flow_id, "watch.json has an invalid seq")
    return value["seq"]


def write_cursor(flow_id: str, seq: int, env: Mapping[str, str] | None = None) -> None:
    """Advance the cursor atomically without regression under a cursor lock.

    Watch callers also hold a separate watch.lock across selecting events,
    printing them and calling this function.
    """
    if type(seq) is not int or seq < 0:
        raise FlowError(flow_id, "cursor must be a non-negative integer")
    directory = folder(flow_id, env)
    fd = paths.open_private_file(directory / "cursor.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        seq = max(seq, read_cursor(flow_id, env))
        record.write_private(directory / WATCH_NAME, json.dumps({"seq": seq}) + "\n")
    finally:
        os.close(fd)


def list_flows(
    env: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[FlowError]]:
    """List flows oldest first and report broken flow folders separately."""
    try:
        names = sorted(
            item.name
            for item in paths.runs_dir(env).iterdir()
            if item.is_dir() and is_flow_id(item.name)
        )
    except FileNotFoundError:
        return [], []
    flows: list[dict[str, Any]] = []
    errors: list[FlowError] = []
    for name in names:
        try:
            flows.append(load(name, env))
        except FlowError as error:
            errors.append(error)
    flows.sort(key=lambda flow: (flow["started_at"], flow["id"]))
    return flows, errors


def allowed_answers(task: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the answers accepted by the task's current state."""
    match task.get("status"):
        case "stopped":
            return ANSWER_KINDS if task.get("branch") else ("worker", "drop")
        case "conflict":
            return ("accept", "worker", "drop")
        case "pending" | "blocked":
            return ("drop",)
        case _:
            return ()


def task_entry(flow: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    """Find a mutable task record, raising KeyError for an unknown task."""
    for task in flow["tasks"]:
        if task["id"] == task_id:
            return task
    raise KeyError(task_id)
