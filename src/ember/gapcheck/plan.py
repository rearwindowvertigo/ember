"""Identify a plan's steps and failure model."""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

KINDS = ("tasks", "numbered", "headings", "whole")
_NUMBERED = re.compile(r"\d+[.)]\s+\S")
_HEADING = re.compile(r"(#{1,6})\s+\S")
_FENCE = re.compile(r" {0,3}(```|~~~)")


@dataclass(frozen=True)
class Step:
    """A numbered step with a title and inclusive, one-based source lines."""

    number: int
    title: str
    first_line: int | None
    last_line: int | None


@dataclass(frozen=True)
class Plan:
    """A plan's absolute path, source text, steps and failure model presence."""

    path: Path
    text: str
    kind: str
    steps: tuple[Step, ...]
    failure_model: bool


class PlanError(Exception):
    """The plan file could not be read as UTF-8."""


def _one_line(value: object) -> str:
    """Collapse whitespace to spaces for a step title."""
    return " ".join(str(value).split())


def _json_steps(text: str) -> tuple[Step, ...] | None:
    """Read steps from a non-empty JSON list of task objects."""
    try:
        document = json.loads(text)
    except ValueError:
        return None
    tasks = document.get("tasks") if isinstance(document, dict) else None
    if (
        not isinstance(tasks, list)
        or not tasks
        or not all(isinstance(task, dict) for task in tasks)
    ):
        return None
    steps = []
    for number, task in enumerate(tasks, 1):
        ident = task.get("id")
        name = f"task {ident}" if isinstance(ident, str) else f"task {number}"
        summary = _one_line(task.get("task", ""))
        title = _one_line(f"{name}: {summary}" if summary else name)[:100]
        steps.append(Step(number, title, None, None))
    return tuple(steps)


def _outside_fences(lines: Sequence[str]) -> Iterator[tuple[int, str]]:
    """Yield zero-based line positions outside matching code fences."""
    fenced = None
    for index, line in enumerate(lines):
        match = _FENCE.match(line)
        if match:
            if fenced is None:
                fenced = match.group(1)
            elif match.group(1) == fenced:
                fenced = None
            continue
        if fenced is None:
            yield index, line


def split(text: str) -> tuple[str, tuple[Step, ...]]:
    """Split JSON tasks, numbered items, repeated heading levels or a whole file.

    Titles are one line of at most 100 characters. JSON tasks have no line
    positions; other steps have inclusive, one-based positions.
    """
    tasks = _json_steps(text)
    if tasks is not None:
        return "tasks", tasks
    lines = text.splitlines()
    visible = list(_outside_fences(lines))
    starts = [index for index, line in visible if _NUMBERED.match(line)]
    kind = "numbered"
    if len(starts) < 2:
        headings = [
            (index, len(match.group(1)))
            for index, line in visible
            if (match := _HEADING.match(line))
        ]
        levels = sorted(
            level
            for level in {level for _, level in headings}
            if sum(other == level for _, other in headings) >= 2
        )
        if not levels:
            return "whole", (Step(1, "the whole plan", 1, max(len(lines), 1)),)
        level, kind = levels[0], "headings"
        starts = [index for index, other in headings if other == level]
        ends = [
            next(
                (
                    index
                    for index, other in headings
                    if index > start and other <= level
                ),
                len(lines),
            )
            - 1
            for start in starts
        ]
    else:
        ends = [following - 1 for following in starts[1:]] + [len(lines) - 1]
    steps = []
    for number, (start, end) in enumerate(zip(starts, ends, strict=True), 1):
        while end > start and not lines[end].strip():
            end -= 1
        steps.append(Step(number, _one_line(lines[start])[:100], start + 1, end + 1))
    return kind, tuple(steps)


def has_failure_model(text: str, kind: str) -> bool:
    """Find a nonblank JSON failure_model or a Markdown failure model heading."""
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict):
        model = document.get("failure_model")
        return isinstance(model, str) and bool(model.strip())
    if kind == "tasks":
        return False
    return any(
        _HEADING.match(line) and "failure model" in line.casefold()
        for _, line in _outside_fences(text.splitlines())
    )


def load(path: str | Path) -> Plan:
    """Read a UTF-8 plan and identify its steps and failure model.

    Raises:
        PlanError: The file cannot be read or is not UTF-8.
    """
    absolute = Path(path).expanduser().absolute()
    try:
        text = absolute.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PlanError(f"cannot read the plan {absolute}: {error}") from error
    kind, steps = split(text)
    return Plan(absolute, text, kind, steps, has_failure_model(text, kind))
