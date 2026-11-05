import json
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
        ("pass", ["must"], 0, ("stop", "unresolved after <n> fix rounds")),
        ("fix", ["must", "lead"], 0, ("stop", "unresolved after <n> fix rounds")),
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
