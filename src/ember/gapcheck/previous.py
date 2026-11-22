"""Validate saved results and carry active, settled and closed gaps forward."""

import copy
import difflib
import fcntl
import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any

from ember import paths
from ember.flows import plan as flow_plan
from ember.gapcheck import reply
from ember.gapcheck.plan import Plan

SCHEMA_NAME = "gapcheck-result.schema.json"


class PreviousError(Exception):
    """An earlier result that cannot be read or is not a valid result."""

    def __init__(self, message: str, *, unreadable: bool) -> None:
        """Store the problem and whether the file could not be read."""
        super().__init__(message)
        self.unreadable = unreadable


@dataclass(frozen=True)
class EarlierGap:
    """One earlier gap, with its original step and answer."""

    id: str
    step: int
    title: str
    gap: str
    harm: str
    why: str
    default: str
    answer: str | None

    def settled(self) -> dict[str, Any]:
        """Return the gap as a history entry of a new result."""
        return {
            "id": self.id,
            "step": self.step,
            "title": self.title,
            "gap": self.gap,
            "harm": self.harm,
            "why": self.why,
            "default": self.default,
            "answer": self.answer,
        }


@dataclass(frozen=True)
class Previous:
    """An earlier result, including gaps needing review and closed history."""

    path: Path
    gaps: tuple[EarlierGap, ...]
    next_id: int = 1
    plan: str = ""
    closed: tuple[dict[str, Any], ...] = ()
    document: dict[str, Any] = field(default_factory=dict)

    def ids(self) -> frozenset[str]:
        """Return the ids of active and settled earlier gaps."""
        return frozenset(gap.id for gap in self.gaps)


def snapshot(document: Plan) -> dict[str, Any]:
    """Save a versioned flow baseline or the original non-flow plan text."""
    if document.flow is None:
        return {"version": 1, "kind": "document", "text": document.text}
    flow = document.flow
    return {
        "version": 1,
        "kind": "flow",
        "context": flow.context,
        "failure_model": flow.failure_model,
        "tasks": [
            {
                "id": task.id,
                "subject": task.subject,
                "task": task.task,
                "review": task.review,
                "files": list(task.files),
                "after": list(task.after),
            }
            for task in flow.tasks
        ],
    }


def comparison(document: flow_plan.Plan, earlier: Previous) -> dict[str, Any]:
    """Report structural changes and text changes for the lead to judge."""
    reasons: list[str] = []
    judgement: list[str] = []
    made = earlier.document
    if incomplete(made):
        reasons.append("the earlier check is incomplete")
    saved = made.get("checked_plan")
    if saved is None:
        reasons.append("the earlier result has no saved plan")
    elif saved["kind"] != "flow":
        reasons.append("the earlier saved plan is not a flow structural baseline")
    else:
        before = {task["id"]: task for task in saved["tasks"]}
        now = {task.id: task for task in document.tasks}
        reasons += [f"task added: {ident}" for ident in now if ident not in before]
        reasons += [f"task removed: {ident}" for ident in before if ident not in now]
        for task in document.tasks:
            old = before.get(task.id)
            if old is None:
                continue
            for field_name in ("files", "after"):
                if set(old[field_name]) != set(getattr(task, field_name)):
                    reasons.append(f"{task.id}: changed {field_name}")
            if any(
                old[key] != getattr(task, key) for key in ("task", "subject", "review")
            ):
                judgement.append(f"{task.id}: changed task text")
        for field_name in ("context", "failure_model"):
            if saved[field_name] != getattr(document, field_name):
                judgement.append(f"changed {field_name} for tasks: " + ", ".join(now))
        old_plan = replace(
            document,
            tasks=tuple(
                flow_plan.Task(
                    task["id"],
                    task["subject"],
                    task["task"],
                    tuple(task["files"]),
                    tuple(task["after"]),
                    task["review"],
                )
                for task in saved["tasks"]
            ),
        )
        if [set(task.id for task in wave) for wave in flow_plan.waves(old_plan)] != [
            set(task.id for task in wave) for wave in flow_plan.waves(document)
        ]:
            reasons.append("changed waves")
    return {
        "previous": str(earlier.path),
        "repeat_needed": True if reasons else None,
        "reasons": reasons,
        "judge": judgement,
        "recovery": (
            "complete the earlier check, or run a whole-plan gap check without "
            "passing the incomplete result as --previous"
            if incomplete(made)
            else None
        ),
    }


def _line_changes(label: str, old: str, new: str) -> list[str]:
    """List the lines a text gained and lost, indented under their labels."""
    delta = list(difflib.ndiff(old.splitlines(), new.splitlines()))
    lines: list[str] = []
    for mark, title in (("+ ", "added or altered"), ("- ", "removed or replaced")):
        found = [line[2:] for line in delta if line.startswith(mark)]
        if found:
            lines.append(f"  {label}, {title} lines:")
            lines.extend(f"      {line}" for line in found)
    return lines


def changes(document: Plan, earlier: Previous) -> list[str] | None:
    """Describe what the plan gained and lost since the earlier check.

    Args:
        document: The plan under review.
        earlier: The earlier result.

    Returns:
        The lines of the change list, one block per changed part, empty when
        nothing changed. None when the earlier result is incomplete, saved no
        plan, or saved a plan of another kind: the repeat then covers the
        whole plan.
    """
    saved = earlier.document.get("checked_plan")
    now = snapshot(document)
    if saved is None or saved["kind"] != now["kind"] or incomplete(earlier.document):
        return None
    if now["kind"] == "document":
        found = _line_changes("text", saved["text"], now["text"])
        return ["The plan:", *found, ""] if found else []
    lines: list[str] = []
    for name in ("context", "failure_model"):
        found = _line_changes(name, saved[name], now[name])
        if found:
            lines += ["The plan:", *found, ""]
    before = {task["id"]: task for task in saved["tasks"]}
    for task in now["tasks"]:
        old = before.pop(task["id"], None)
        if old is None:
            lines += [f"Task {task['id']}: a new task.", ""]
            continue
        found = []
        for name in ("subject", "task", "review"):
            found += _line_changes(name, old[name], task[name])
        for name in ("files", "after"):
            gained = sorted(set(task[name]) - set(old[name]))
            lost = sorted(set(old[name]) - set(task[name]))
            if gained:
                found.append(f"  {name} added: {', '.join(gained)}")
            if lost:
                found.append(f"  {name} removed: {', '.join(lost)}")
        if found:
            lines += [f"Task {task['id']}:", *found, ""]
    lines += [line for ident in before for line in (f"Task {ident}: removed.", "")]
    return lines


def incomplete(document: Mapping[str, Any]) -> bool:
    """Return whether the result lacks complete parsed step coverage."""
    return bool(
        document["parse_error"]
        or document["uncovered_steps"]
        or document["unknown_steps"]
    )


def schema_text() -> str:
    """Read the packaged result schema as UTF-8 text."""
    return resources.files("ember").joinpath("data", SCHEMA_NAME).read_text("utf-8")


def _schema_error(
    value: object, schema: Mapping[str, Any], root: Mapping[str, Any], path: str
) -> str | None:
    """Apply the JSON Schema keywords used by the packaged result schema."""
    if "anyOf" in schema:
        errors = [_schema_error(value, child, root, path) for child in schema["anyOf"]]
        return None if None in errors else f"{path}: no matching saved plan schema"
    if "$ref" in schema:
        return _schema_error(
            value, root["$defs"][schema["$ref"].split("/")[-1]], root, path
        )
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    integer = type(value) is int or (
        type(value) is float and math.isfinite(value) and value.is_integer()
    )
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": integer,
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if kinds and not any(matches[kind] for kind in kinds):
        return f"{path}: expected {' or '.join(kinds)}"
    if "const" in schema and value != schema["const"]:
        return f"{path}: expected {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        options = (
            item if isinstance(item, str) else json.dumps(item)
            for item in schema["enum"]
        )
        return f"{path}: expected one of {', '.join(options)}"
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}: missing required key {key!r}"
        if schema.get("additionalProperties") is False:
            for key in sorted(value.keys() - properties.keys()):
                return f"{path}: unexpected key {key!r}"
        for key, child in properties.items():
            if key in value:
                error = _schema_error(value[key], child, root, f"{path}.{key}")
                if error:
                    return error
    elif isinstance(value, list):
        for index, item in enumerate(value):
            error = _schema_error(
                item, schema.get("items", {}), root, f"{path}[{index}]"
            )
            if error:
                return error
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return f"{path}: expected a non-empty string"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return f"{path}: expected a gap id g<n>"
    elif integer and "minimum" in schema and value < schema["minimum"]:
        return f"{path}: expected an integer at least {schema['minimum']}"
    return None


def _legacy(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize results without gap ids before applying the result schema.

    Legacy results must still hold the result metadata. Reviewer replies and
    partial objects are not results. A document with any id uses the current
    schema, including its required next_id.
    """
    steps = document.get("steps")
    if not isinstance(steps, list) or any(
        key in document
        for key in (
            "next_id",
            "previous_plan",
            "settled",
            "closed",
            "dispositions",
            "schema_version",
            "checked_plan",
        )
    ):
        return document
    if any(
        "id" in gap
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("gaps"), list)
        for gap in step["gaps"]
        if isinstance(gap, dict)
    ):
        return document
    if document.get("previous") is not None:
        raise PreviousError(
            "$.previous: legacy results have no previous result", unreadable=False
        )
    made = copy.deepcopy(document)
    number = 1
    for step in made["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("gaps"), list):
            return document
        for gap in step["gaps"]:
            if not isinstance(gap, dict):
                return document
            gap["id"] = f"g{number}"
            gap.setdefault("earlier", None)
            gap.setdefault("answer", None)
            gap.setdefault("ask_owner", True)
            number += 1
    # Check the old ranked copies before replacing them with id references.
    old = made.get("ranked")
    schema = json.loads(schema_text())
    error = _schema_error(
        old,
        {"type": "array", "items": {"$ref": "#/$defs/legacyRanked"}},
        schema,
        "$.ranked",
    )
    if error:
        raise PreviousError(error, unreadable=False)
    assert isinstance(old, list)
    for index, step in enumerate(document["steps"]):
        for position, gap in enumerate(step["gaps"]):
            error = _schema_error(
                {**gap, "step": step.get("step")},
                {
                    **schema["$defs"]["legacyRanked"],
                    "properties": {
                        **schema["$defs"]["legacyRanked"]["properties"],
                        "answer": schema["$defs"]["gap"]["properties"]["answer"],
                        "earlier": schema["$defs"]["gap"]["properties"]["earlier"],
                    },
                },
                schema,
                f"$.steps[{index}].gaps[{position}]",
            )
            if error:
                raise PreviousError(error, unreadable=False)
    keys = ("step", *reply.GAP_KEYS)
    expected = [
        {**gap, "step": step.get("step")}
        for step in made["steps"]
        for gap in step["gaps"]
    ]
    if Counter(tuple(entry.get(key) for key in keys) for entry in old) != Counter(
        tuple(entry.get(key) for key in keys) for entry in expected
    ):
        raise PreviousError("$.ranked: must match the gaps in steps", unreadable=False)
    ranked = [
        {"id": gap["id"], "step": step["step"], "harm": gap.get("harm")}
        for step in made["steps"]
        if "step" in step
        for gap in step["gaps"]
    ]
    made["ranked"] = sorted(
        ranked,
        key=lambda entry: (
            reply.HARMS.index(entry["harm"])
            if entry["harm"] in reply.HARMS
            else len(reply.HARMS)
        ),
    )
    made.setdefault("status", "run")
    made.setdefault("ask", "all")
    made.update(
        next_id=number,
        previous=None,
        previous_plan=None,
        settled=[],
        closed=[],
        dispositions=[],
    )
    return made


def _integrity(document: dict[str, Any]) -> str | None:
    """Check identities and derived result fields across schema-valid entries."""
    if ("schema_version" in document) != ("checked_plan" in document):
        return "$: schema_version and checked_plan must occur together"
    saved = document.get("checked_plan")
    if saved is not None and saved["kind"] == "flow":
        try:
            flow_plan.from_json({"dir": "/", "tasks": saved["tasks"]}, Path("/"))
        except flow_plan.PlanError as error:
            return "$.checked_plan: " + "; ".join(error.problems)
    active = [
        {**gap, "step": step["step"]}
        for step in document["steps"]
        for gap in step["gaps"]
    ]
    all_gaps = [*active, *document["settled"], *document["closed"]]
    ids = [gap["id"] for gap in all_gaps]
    if len(set(ids)) != len(ids):
        return "$: gap ids must be unique across steps, settled and closed"
    referenced = [entry["id"] for entry in document["dispositions"]]
    referenced += [gap["earlier"] for gap in active if gap["earlier"]]
    if any(
        int(identity[1:]) >= document["next_id"] for identity in [*ids, *referenced]
    ):
        return "$.next_id: must exceed every gap id"
    numbers = [step["step"] for step in document["steps"]]
    if len(set(numbers)) != len(numbers):
        return "$.steps: duplicate step number"
    expected = {gap["id"]: (gap["step"], gap["harm"]) for gap in active}
    seen = set()
    for entry in document["ranked"]:
        identity = entry["id"]
        if identity in seen or expected.get(identity) != (entry["step"], entry["harm"]):
            return "$.ranked: must refer to each active gap once with its step and harm"
        seen.add(identity)
    if seen != expected.keys():
        return "$.ranked: must refer to each active gap once with its step and harm"
    if document["counts"] != {
        harm: sum(gap["harm"] == harm for gap in active) for harm in reply.HARMS
    }:
        return "$.counts: must count active gaps by harm"
    return None


def validate(document: object) -> dict[str, Any]:
    """Validate a saved result, normalizing results without ids or answers.

    Raises:
        PreviousError: The document is not a result or has inconsistent ids.

    Returns:
        The validated result, with legacy fields normalized.
    """
    schema = json.loads(schema_text())
    made = _legacy(document) if isinstance(document, dict) else document
    error = _schema_error(made, schema, schema, "$")
    if not error:
        assert isinstance(made, dict)
        error = _integrity(made)
    if error:
        raise PreviousError(error, unreadable=False)
    assert isinstance(made, dict)
    return made


def parse(document: object) -> list[EarlierGap]:
    """Validate a result and return its active and settled gaps in file order."""
    made = validate(document)
    gaps = [
        {**gap, "step": step["step"], "title": step["title"]}
        for step in made["steps"]
        for gap in step["gaps"]
    ]
    return [
        EarlierGap(
            gap["id"],
            int(gap["step"]),
            gap["title"],
            gap["gap"],
            gap["harm"],
            gap["why"],
            gap["default"],
            gap.get("answer"),
        )
        for gap in [*gaps, *made["settled"]]
    ]


def load(path: str, *, allow_incomplete: bool = False) -> Previous:
    """Read a validated result, requiring completion except for comparison.

    Raises:
        PreviousError: The file cannot be read, or is not a valid result.
    """
    location = Path(path).expanduser().absolute()
    try:
        location = location.resolve()
        text = location.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PreviousError(
            f"cannot read the previous result {location}: {error}", unreadable=True
        ) from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise PreviousError(
            f"the previous result {location} is not JSON: {error}", unreadable=False
        ) from error
    try:
        made = validate(document)
        if incomplete(made) and not allow_incomplete:
            raise PreviousError(
                "$: the check is incomplete; finish that check before using its result",
                unreadable=False,
            )
        gaps = parse(made)
    except PreviousError as error:
        raise PreviousError(
            f"the previous result {location} is not a gap check result at {error}",
            unreadable=False,
        ) from error
    return Previous(
        location,
        tuple(gaps),
        int(made["next_id"]),
        made["plan"],
        tuple(made["closed"]),
        made,
    )


def history(
    previous: Previous, dispositions: Sequence[Mapping[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Carry settled answers and closed gaps according to explicit dispositions."""
    by_id = {entry["id"]: entry for entry in dispositions}
    settled = []
    closed = list(previous.closed)
    for gap in previous.gaps:
        entry = by_id.get(gap.id)
        if entry is None:
            continue
        if entry["status"] == "settled" and gap.answer is not None:
            settled.append(gap.settled())
        elif entry["status"] == "closed":
            closed.append({**gap.settled(), "reason": entry["reason"]})
    return settled, closed


def answer(result: Previous, identity: str, text: str) -> dict[str, Any]:
    """Reload and write an answer while holding a stable per-result lock.

    The adjacent `<result>.lock` file remains in place across replacements
    and calls. Only the path of the supplied result is used; its document
    may be an older snapshot.

    Raises:
        ValueError: The answer is empty or the id is not active or settled.
        PreviousError: The current result cannot be read or fails validation.
        OSError: The lock cannot be used or the result cannot be written.

    Returns:
        The updated result document.
    """
    if not text.strip():
        raise ValueError("--text must contain an answer")
    return _record_answers(result, [identity], text)


def take_defaults(result: Previous, identities: Sequence[str]) -> dict[str, Any]:
    """Record each listed active gap's default under the result lock.

    Raises:
        ValueError: The list is empty or contains an unknown, duplicate,
            settled or already answered id.
        PreviousError: The current result cannot be read or fails validation.
        OSError: The lock cannot be used or the result cannot be written.

    Returns:
        The updated result document.
    """
    return _record_answers(result, identities, None)


def _record_answers(
    result: Previous, identities: Sequence[str], text: str | None
) -> dict[str, Any]:
    """Reload, validate and record answers under a stable per-result lock."""
    lock = result.path.with_name(f"{result.path.name}.lock")
    fd = paths.open_private_file(lock)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = load(str(result.path))
        made = copy.deepcopy(current.document)
        gaps = {gap["id"]: gap for step in made["steps"] for gap in step["gaps"]}
        if text is not None:
            gaps.update((gap["id"], gap) for gap in made["settled"])
        if not identities:
            raise ValueError("--take-defaults needs at least one gap id")
        seen = set()
        for identity in identities:
            if identity in seen:
                raise ValueError(f"duplicate gap id {identity!r}")
            seen.add(identity)
            if identity not in gaps:
                kind = "active" if text is None else "active or settled"
                raise ValueError(f"no {kind} gap with id {identity!r}")
            if text is None and gaps[identity].get("answer") is not None:
                raise ValueError(f"gap {identity!r} is already answered")
        for identity in identities:
            gap = gaps[identity]
            gap["answer"] = f"Default taken: {gap['default']}" if text is None else text
        return _write_result(current.path, made)
    finally:
        os.close(fd)


def _write_result(path: Path, made: dict[str, Any]) -> dict[str, Any]:
    """Validate and replace a result while its caller holds the result lock."""
    validate(made)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(made, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return made
