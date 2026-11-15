import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from ember.flows import verdict
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]


@pytest.fixture(autouse=True)
def env(tmp_path: Path) -> dict[str, str]:
    return make_env(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict":"pass","findings":[]}',
        'Some prose\n```json\n{"verdict":"pass","findings":[]}\n```',
        'Some prose {"verdict":"pass","findings":[]} followed by prose',
        '```json\n{"verdict":"fix","findings":[]}\n```\n'
        '```\n{"verdict":"pass","findings":[]}\n```',
    ],
)
def test_pass_and_extraction(text: str) -> None:
    review = verdict.read(text)
    assert review == verdict.Review("pass", (), None)
    assert review.counts() == {"must": 0, "should": 0, "lead": 0}
    assert review.decision(0) == ("accept", None)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("prose only", "the reply is not a JSON object"),
        ("[]", "the reply is not a JSON object"),
        ('{"verdict":"maybe"}', "the reply has no verdict pass or fix"),
        ("{}", "the reply has no verdict pass or fix"),
        ('{"verdict":[],"findings":[]}', "the reply has no verdict pass or fix"),
        ('{"verdict":"pass"}', "the reply's findings is not a list"),
        ('{"verdict":"pass","findings":{}}', "the reply's findings is not a list"),
    ],
)
def test_unreadable(text: str, problem: str) -> None:
    review = verdict.read(text)
    assert review.problem == problem
    for rounds_left in (0, 1):
        assert review.decision(rounds_left) == (
            "stop",
            f"the review is unreadable: {problem}",
        )


def test_findings_are_normalized_independently() -> None:
    review = verdict.read(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [
                    "a malformed finding",
                    {
                        "severity": "unknown",
                        "scenario": "a caller loses work",
                        "problem": 12,
                        "fix": ["restore"],
                        "file": "src/a.py",
                        "line": 12,
                        "earlier": 1,
                    },
                    {"severity": "must", "scenario": " \n", "file": "", "line": True},
                    {"severity": "lead", "problem": " \n", "line": "12"},
                    {
                        "severity": "should",
                        "problem": None,
                        "file": 123,
                        "line": 12.0,
                        "scenario": {"input": "empty"},
                        "earlier": True,
                    },
                ],
            }
        ),
        earlier_count=1,
    )
    assert review.problem is None
    a, b, c, d, e = review.findings
    assert a.problem == '"a malformed finding"'
    assert a.severity == "should" and a.note == "demoted: no scenario"
    assert b.to_json() == {
        "severity": "must",
        "file": "src/a.py",
        "line": 12,
        "problem": "12",
        "fix": '["restore"]',
        "scenario": "a caller loses work",
        "earlier": 1,
        "note": "",
        "round": None,
        "run": None,
    }
    assert c.severity == "should" and c.note == "demoted: no scenario"
    assert c.line is None and c.file is None
    assert d.severity == "should" and d.note == "demoted: no decision named"
    assert d.line is None
    assert e.problem == "" and e.file is None and e.line is None
    assert e.scenario == '{"input": "empty"}' and e.earlier is None
    assert review.counts() == {"must": 1, "should": 4, "lead": 0}


@pytest.mark.parametrize("earlier", [None, 0, -1, 2, True, "1", 1.0, [], {}])
def test_out_of_range_earlier(earlier: object) -> None:
    review = verdict.read(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [{"earlier": earlier}],
            }
        ),
        earlier_count=1,
    )
    assert review.findings[0].earlier is None


@pytest.mark.parametrize(
    ("name", "severities", "left", "expected"),
    [
        ("pass", [], 0, ("accept", None)),
        ("pass", ["should"], 1, ("accept", None)),
        (
            "fix",
            [],
            0,
            ("stop", "the review says fix but names no must or lead finding"),
        ),
        (
            "fix",
            ["should"],
            1,
            ("stop", "the review says fix but names no must or lead finding"),
        ),
        ("pass", ["lead"], 0, ("stop", "needs the lead")),
        ("fix", ["lead"], 1, ("stop", "needs the lead")),
        ("pass", ["must", "lead"], 1, ("fix", None)),
        ("fix", ["must"], 1, ("fix", None)),
        ("pass", ["must"], 0, ("stop", "review budget exhausted")),
        ("fix", ["must", "lead"], 0, ("stop", "review budget exhausted")),
    ],
)
def test_decision(
    name: str, severities: list[str], left: int, expected: tuple[str, str | None]
) -> None:
    review = verdict.read(
        json.dumps(
            {
                "verdict": name,
                "findings": [
                    {"severity": s, "problem": "problem", "scenario": "harm"}
                    for s in severities
                ],
            }
        )
    )
    assert review.decision(left) == expected


@pytest.mark.parametrize(
    ("value", "unit", "source", "rounds", "expected"),
    [
        (
            1,
            "reviews",
            "flow.review_rounds",
            0,
            "1 review (flow.review_rounds 1 allows no fix round)",
        ),
        (
            2,
            "reviews",
            "flow.review_rounds",
            1,
            "2 reviews (flow.review_rounds 2 allows 1 fix round)",
        ),
        (
            4,
            "reviews",
            "flow.review_rounds",
            3,
            "4 reviews (flow.review_rounds 4 allows 3 fix rounds)",
        ),
        (0, "fix_rounds", "plan.rounds", 0, "0 fix rounds (plan rounds 0)"),
        (1, "fix_rounds", "plan.rounds", 1, "1 fix round (plan rounds 1)"),
        (2, "fix_rounds", "default", 2, "2 fix rounds (default 2 fix rounds)"),
        (
            0,
            "fix_rounds",
            "unknown",
            0,
            "0 fix rounds (budget 0 fix rounds; source unknown)",
        ),
    ],
)
def test_budget_stop_reason(
    value: int, unit: str, source: str, rounds: int, expected: str
) -> None:
    budget = {"value": value, "unit": unit, "source": source}
    assert verdict.budget_text(budget) == expected
    reason = "unresolved after " + verdict.budget_text(budget, fix_rounds=rounds)
    review = verdict.read(
        json.dumps(
            {
                "verdict": "fix",
                "findings": [{"severity": "must", "scenario": "harm"}],
            }
        )
    )
    assert review.decision(0, exhausted_reason=reason) == ("stop", reason)
    assert review.decision(1, exhausted_reason=reason) == ("fix", None)


def test_finding_provenance_comes_from_the_caller() -> None:
    review = verdict.read(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [{"severity": "should", "round": 99, "run": "invented"}],
            }
        ),
        round=2,
        run="reviewer-run",
    )
    finding = review.findings[0]
    assert finding.round == 2 and finding.run == "reviewer-run"
    assert verdict.Finding(**finding.to_json()) == finding
    old = finding.to_json()
    del old["round"], old["run"]
    restored = verdict.Finding(**old)
    assert restored.round is None and restored.run is None


def test_schema_agrees_with_module() -> None:
    schema = json.loads(verdict.schema_text())
    assert schema["$id"] == "ember/review-reply/1"
    assert schema["required"] == ["verdict", "findings"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["verdict"]["enum"] == list(verdict.VERDICTS)
    finding = schema["$defs"]["finding"]
    assert finding["properties"]["severity"]["enum"] == list(verdict.SEVERITIES)
    assert set(finding["properties"]) == {
        item.name for item in fields(verdict.Finding)
    } - {"round", "run"}
    assert set(finding["required"]) == set(finding["properties"]) - {"note"}
    assert finding["additionalProperties"] is False
    outside = schema["properties"]["outside_files"]
    assert outside == {"type": "array", "items": {"$ref": "#/$defs/outside_file"}}
    entry = schema["$defs"]["outside_file"]
    assert entry["type"] == "object" and entry["additionalProperties"] is False
    assert set(entry["required"]) == {item.name for item in fields(verdict.OutsideFile)}
    assert entry["properties"]["file"] == {"type": "string"}
    assert entry["properties"]["needed"] == {"type": "boolean"}
    reason = entry["properties"]["reason"]
    assert reason["type"] == "string"
    assert re.search(reason["pattern"], "Needed by the caller")
    assert not re.search(reason["pattern"], " \n\t")
    assert not re.search(reason["pattern"], "")


def test_outside_files_keep_task_order_and_normalize_findings() -> None:
    review = verdict.read(
        json.dumps(
            {
                "verdict": "pass",
                "findings": ["malformed"],
                "outside_files": [
                    {"file": "b", "needed": False, "reason": "Unrelated wording"},
                    {"file": "unlisted", "needed": [], "reason": None},
                    {"file": "unlisted"},
                    {"file": []},
                    None,
                    {"file": "a", "needed": True, "reason": "Caller needs the change"},
                ],
            }
        ),
        outside=["a", "b"],
    )
    assert review.problem is None
    assert review.outside_files == (
        verdict.OutsideFile("a", True, "Caller needs the change"),
        verdict.OutsideFile("b", False, "Unrelated wording"),
    )
    assert review.outside_files[0].to_json() == {
        "file": "a",
        "needed": True,
        "reason": "Caller needs the change",
    }
    assert review.findings[0].severity == "should"
    assert review.decision(0) == ("accept", None)


@pytest.mark.parametrize(
    "entries",
    [
        None,
        {},
        [],
        [{"file": "./a", "needed": True, "reason": "Not an exact path"}],
        [{"file": "a", "needed": True, "reason": "Needed"}] * 2,
        [{"file": "a", "needed": True, "reason": "Needed"}, {"file": "a"}],
        [{"file": "a", "reason": "Needed"}],
        [{"file": "a", "needed": 1, "reason": "Needed"}],
        [{"file": "a", "needed": "true", "reason": "Needed"}],
        [{"file": "a", "needed": None, "reason": "Needed"}],
        [{"file": "a", "needed": True}],
        [{"file": "a", "needed": True, "reason": None}],
        [{"file": "a", "needed": True, "reason": 1}],
        [{"file": "a", "needed": True, "reason": ""}],
        [{"file": "a", "needed": True, "reason": " \n\t"}],
    ],
)
def test_invalid_outside_files_are_unreadable(entries: object) -> None:
    review = verdict.read(
        json.dumps({"verdict": "pass", "findings": [], "outside_files": entries}),
        outside=["a", "b"],
    )
    assert review.verdict is None and review.outside_files == ()
    assert review.problem is not None
    assert "a (" in review.problem and "b (missing)" in review.problem
    assert review.decision(1) == ("stop", f"the review is unreadable: {review.problem}")


def test_outside_files_property_is_required_only_for_listed_paths() -> None:
    text = json.dumps({"verdict": "pass", "findings": []})
    assert verdict.read(text).outside_files == ()
    missing = verdict.read(text, outside=["src/helper.py"])
    assert missing.problem == "the reply's outside_files: src/helper.py (missing)"
    unrelated = json.dumps(
        {
            "verdict": "pass",
            "findings": [],
            "outside_files": [{"file": "unknown"}],
        }
    )
    assert verdict.read(unrelated) == verdict.read(text)
