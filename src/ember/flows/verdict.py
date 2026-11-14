"""Read flow review replies and decide whether a task needs another turn."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from importlib import resources
from typing import Any

from ember.gapcheck.reply import extract_json

SEVERITIES = ("must", "should", "lead")
VERDICTS = ("pass", "fix")
SCHEMA_NAME = "review-reply.schema.json"


def budget_text(budget: Mapping[str, Any], *, fix_rounds: int | None = None) -> str:
    """Describe a budget or completed work in the budget's original unit."""
    value = budget["value"]
    reviews = budget["unit"] == "reviews"
    count = value if fix_rounds is None else fix_rounds + int(reviews)
    unit = "review" if reviews else "fix round"
    amount = f"{count} {unit}{'' if count == 1 else 's'}"
    budget_unit = f"fix round{'' if value == 1 else 's'}"
    source = budget["source"]
    if source == "flow.review_rounds":
        allowed = value - 1
        fixes = (
            "no fix round"
            if allowed == 0
            else f"{allowed} fix round{'' if allowed == 1 else 's'}"
        )
        detail = f"flow.review_rounds {value} allows {fixes}"
    elif source == "plan.rounds":
        detail = f"plan rounds {value}"
    elif source == "default":
        detail = f"default {value} {budget_unit}"
    else:
        detail = f"budget {value} {budget_unit}; source unknown"
    return f"{amount} ({detail})"


@dataclass(frozen=True)
class Finding:
    """One normalized finding, including any demotion made while reading."""

    severity: str
    file: str | None
    line: int | None
    problem: str
    fix: str
    scenario: str
    earlier: int | None
    note: str
    round: int | None = None
    run: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the finding's fields as a JSON-ready object."""
        return asdict(self)


@dataclass(frozen=True)
class OutsideFile:
    """The reviewer's account of one file outside the task's declared files."""

    file: str
    needed: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        """Return the outside-file entry as a JSON-ready object."""
        return asdict(self)


@dataclass(frozen=True)
class Review:
    """A review's verdict and findings, or the reason it cannot be read."""

    verdict: str | None
    findings: tuple[Finding, ...]
    problem: str | None
    outside_files: tuple[OutsideFile, ...] = ()

    def counts(self) -> dict[str, int]:
        """Count each severity, including those with no findings."""
        return {
            severity: sum(item.severity == severity for item in self.findings)
            for severity in SEVERITIES
        }

    def decision(
        self, rounds_left: int, *, exhausted_reason: str = "review budget exhausted"
    ) -> tuple[str, str | None]:
        """Return fix, accept or stop, using the caller's budget stop reason."""
        if self.verdict is None:
            return "stop", f"the review is unreadable: {self.problem}"
        counts = self.counts()
        if counts["must"]:
            if rounds_left > 0:
                return "fix", None
            return "stop", exhausted_reason
        if counts["lead"]:
            return "stop", "needs the lead"
        if self.verdict == "pass":
            return "accept", None
        return "stop", "the review says fix but names no must or lead finding"


def schema_text() -> str:
    """Read the packaged review reply schema as UTF-8 text."""
    return resources.files("ember").joinpath("data", SCHEMA_NAME).read_text("utf-8")


def _text(value: object) -> str:
    """Keep strings, map null to empty text and serialize other JSON values."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def _finding(value: object, earlier_count: int) -> Finding:
    """Normalize a finding without rejecting the review that contains it."""
    item = value if isinstance(value, dict) else {"problem": json.dumps(value)}
    severity = item.get("severity")
    if severity not in SEVERITIES:
        severity = "must"
    file = item.get("file")
    file = file if isinstance(file, str) and file else None
    line = item.get("line")
    line = line if type(line) is int else None
    earlier = item.get("earlier")
    if type(earlier) is not int or not 1 <= earlier <= earlier_count:
        earlier = None
    problem = _text(item.get("problem"))
    scenario = _text(item.get("scenario"))
    note = ""
    if severity == "must" and not scenario.strip():
        severity, note = "should", "demoted: no scenario"
    if severity == "lead" and not problem.strip():
        severity, note = "should", "demoted: no decision named"
    return Finding(
        severity, file, line, problem, _text(item.get("fix")), scenario, earlier, note
    )


def _outside_files(value: object, outside: Sequence[str]) -> tuple[OutsideFile, ...]:
    """Validate listed paths and ignore entries for every other path."""
    entries: dict[str, list[dict[str, Any]]] = {path: [] for path in outside}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            path = item.get("file")
            if isinstance(path, str) and path in entries:
                entries[path].append(item)
    result = []
    problems = []
    for path, items in entries.items():
        if not items:
            problems.append(f"{path} (missing)")
        elif len(items) != 1:
            problems.append(f"{path} (repeated)")
        else:
            needed, reason = items[0].get("needed"), items[0].get("reason")
            if (
                type(needed) is not bool
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                problems.append(f"{path} (invalid needed or reason)")
            else:
                result.append(OutsideFile(path, needed, reason))
    if problems:
        raise ValueError("the reply's outside_files: " + ", ".join(problems))
    return tuple(result)


def read(
    text: str,
    earlier_count: int = 0,
    *,
    outside: Sequence[str] = (),
    round: int | None = None,
    run: str | None = None,
) -> Review:
    """Normalize findings and attach the caller's review round and run."""
    document = extract_json(text)
    if document is None:
        return Review(None, (), "the reply is not a JSON object")
    verdict = document.get("verdict")
    if verdict not in VERDICTS:
        return Review(None, (), "the reply has no verdict pass or fix")
    findings = document.get("findings")
    if not isinstance(findings, list):
        return Review(None, (), "the reply's findings is not a list")
    try:
        outside_files = _outside_files(document.get("outside_files"), outside)
    except ValueError as error:
        return Review(None, (), str(error))
    return Review(
        verdict,
        tuple(
            replace(_finding(item, earlier_count), round=round, run=run)
            for item in findings
        ),
        None,
        outside_files,
    )
