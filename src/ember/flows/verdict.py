"""Read flow review replies and decide whether a task needs another turn."""

import json
from dataclasses import asdict, dataclass
from importlib import resources
from typing import Any

from ember.gapcheck.reply import extract_json

SEVERITIES = ("must", "should", "lead")
VERDICTS = ("pass", "fix")
SCHEMA_NAME = "review-reply.schema.json"


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

    def to_json(self) -> dict[str, Any]:
        """Return the finding's fields as a JSON-ready object."""
        return asdict(self)


@dataclass(frozen=True)
class Review:
    """A review's verdict and findings, or the reason it cannot be read."""

    verdict: str | None
    findings: tuple[Finding, ...]
    problem: str | None

    def counts(self) -> dict[str, int]:
        """Count each severity, including those with no findings."""
        return {
            severity: sum(item.severity == severity for item in self.findings)
            for severity in SEVERITIES
        }

    def decision(self, rounds_left: int) -> tuple[str, str | None]:
        """Return fix, accept or stop, with a reason for a stop.

        The caller replaces `<n>` in the exhausted-budget reason with the
        number of fix rounds completed in its current budget.
        """
        if self.verdict is None:
            return "stop", f"the review is unreadable: {self.problem}"
        counts = self.counts()
        if counts["must"]:
            if rounds_left > 0:
                return "fix", None
            return "stop", "unresolved after <n> fix rounds"
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


def read(text: str, earlier_count: int = 0) -> Review:
    """Extract a review object and normalize each of its findings."""
    document = extract_json(text)
    if document is None:
        return Review(None, (), "the reply is not a JSON object")
    verdict = document.get("verdict")
    if verdict not in VERDICTS:
        return Review(None, (), "the reply has no verdict pass or fix")
    findings = document.get("findings")
    if not isinstance(findings, list):
        return Review(None, (), "the reply's findings is not a list")
    return Review(
        verdict, tuple(_finding(item, earlier_count) for item in findings), None
    )
