import json
from pathlib import Path
from typing import Any

import pytest

from ember.gapcheck import reply
from ember.gapcheck.plan import Step
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]

STEPS = (Step(1, "Read", 1, 1), Step(2, "Build", 2, 2))


@pytest.fixture(autouse=True)
def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in make_env(tmp_path).items():
        monkeypatch.setenv(key, value)


def gap(name: str, harm: str = "high") -> dict[str, str]:
    return {"gap": name, "harm": harm, "why": "Data can be lost", "default": "Keep it"}


@pytest.mark.parametrize("form", ["{}", "Prose\n```json\n{}\n```", "Prose {} end"])
def test_valid_reply_forms(form: str) -> None:
    document = {
        "steps": [{"step": 1, "gaps": [gap("Retention")]}, {"step": 2, "gaps": []}]
    }
    result = reply.read(form.format(json.dumps(document)), STEPS)
    assert result == {
        "steps": [
            {"step": 1, "title": "Read", "gaps": [gap("Retention")]},
            {"step": 2, "title": "Build", "gaps": []},
        ],
        "ranked": [{"step": 1, **gap("Retention")}],
        "counts": {"high": 1, "medium": 0, "low": 0},
        "uncovered_steps": [],
        "unknown_steps": [],
        "parse_error": None,
    }


def test_last_fenced_object_wins() -> None:
    assert reply.extract_json('```\n{"a": 1}\n```\n```json\n{"b": 2}\n```') == {"b": 2}


def test_invalid_last_fence_falls_through_to_brace_span() -> None:
    assert reply.extract_json('```\n{"a": 1}\n```\n```json\ninvalid\n```') == {"a": 1}


@pytest.mark.parametrize("malformed", [True, False])
def test_invalid_last_fence_does_not_recover_earlier_draft(malformed: bool) -> None:
    draft = json.dumps({"steps": [{"step": 1, "gaps": []}]})
    correction = json.dumps({"steps": [{"step": 1, "gaps": [gap("Data loss")]}]})
    final_block = correction[:-1] if malformed else f"[{correction}]"
    text = f"Draft:\n```json\n{draft}\n```\nCorrection:\n```json\n{final_block}\n```"
    result = reply.read(text, STEPS[:1])
    assert result["parse_error"] == "the reply is not a JSON object"
    assert result["uncovered_steps"] == [1]
    assert result["steps"] == []


@pytest.mark.parametrize("text", ["No gaps", "[]", "null", "{broken}"])
def test_no_json_object(text: str) -> None:
    result = reply.read(text, STEPS)
    assert result["parse_error"] == "the reply is not a JSON object"
    assert result["uncovered_steps"] == [1, 2]
    assert result["steps"] == result["ranked"] == result["unknown_steps"] == []
    assert result["counts"] == dict.fromkeys(reply.HARMS, 0)


@pytest.mark.parametrize(
    ("entry", "location", "message"),
    [
        (None, "$.steps[0]", "expected an object"),
        ({"step": 1}, "$.steps[0]", "missing required key 'gaps'"),
        ({"gaps": []}, "$.steps[0]", "missing required key 'step'"),
        ({"step": 1, "gaps": [], "extra": 1}, "$.steps[0]", "unexpected key 'extra'"),
        ({"step": True, "gaps": []}, "$.steps[0].step", "expected an integer"),
        ({"step": 1.5, "gaps": []}, "$.steps[0].step", "expected an integer"),
        ({"step": "1", "gaps": []}, "$.steps[0].step", "expected an integer"),
        ({"step": 0, "gaps": []}, "$.steps[0].step", "expected an integer at least 1"),
        ({"step": 1, "gaps": {}}, "$.steps[0].gaps", "expected an array"),
        ({"step": 1, "gaps": [None]}, "$.steps[0].gaps[0]", "expected an object"),
        (
            {"step": 1, "gaps": [gap("Bad", "critical")]},
            "$.steps[0].gaps[0].harm",
            "expected one of high, medium, low",
        ),
        (
            {"step": 1, "gaps": [{**gap("Bad"), "extra": 1}]},
            "$.steps[0].gaps[0]",
            "unexpected key 'extra'",
        ),
    ],
)
def test_invalid_entry_keeps_other_valid_entries(
    entry: object, location: str, message: str
) -> None:
    document = {"steps": [entry, {"step": 2, "gaps": []}]}
    result = reply.read(json.dumps(document), STEPS)
    assert result["steps"] == [{"step": 2, "title": "Build", "gaps": []}]
    assert result["parse_error"] == (
        f"the reply does not match the schema at {location}: {message}; "
        "the reply misses step 1"
    )


@pytest.mark.parametrize("field", reply.GAP_KEYS)
@pytest.mark.parametrize("value", [None, "", 3])
def test_gap_field_types_and_minimum_lengths(field: str, value: object) -> None:
    item: dict[str, Any] = gap("Missing behavior")
    item[field] = value
    result = reply.read(json.dumps({"steps": [{"step": 1, "gaps": [item]}]}), STEPS[:1])
    message = "expected a non-empty string" if value == "" else "expected a string"
    assert result["parse_error"] == (
        f"the reply does not match the schema at $.steps[0].gaps[0].{field}: "
        f"{message}; the reply misses step 1"
    )


@pytest.mark.parametrize("field", reply.GAP_KEYS)
def test_gap_required_keys(field: str) -> None:
    item = gap("Missing behavior")
    del item[field]
    result = reply.read(json.dumps({"steps": [{"step": 1, "gaps": [item]}]}), STEPS[:1])
    assert f"missing required key {field!r}" in result["parse_error"]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "$: missing required key 'steps'"),
        ({"steps": None}, "$.steps: expected an array"),
    ],
)
def test_invalid_top_level(document: object, message: str) -> None:
    result = reply.read(json.dumps(document), STEPS)
    assert result["parse_error"] == (
        f"the reply does not match the schema at {message}; the reply misses step 1, 2"
    )


def test_top_level_extra_key_keeps_valid_steps() -> None:
    result = reply.read('{"steps": [{"step": 1, "gaps": []}], "extra": 1}', STEPS[:1])
    assert result["uncovered_steps"] == []
    assert (
        result["parse_error"]
        == "the reply does not match the schema at $: unexpected key 'extra'"
    )


def test_missing_and_unknown_steps() -> None:
    result = reply.read(
        json.dumps({"steps": [{"step": 3, "gaps": [gap("Unknown")]}]}), STEPS
    )
    assert result["uncovered_steps"] == [1, 2]
    assert result["unknown_steps"] == [3]
    assert result["counts"] == dict.fromkeys(reply.HARMS, 0)
    assert result["ranked"] == []
    assert result["parse_error"] == (
        "the reply misses step 1, 2; "
        "the reply names step 3, which the plan does not have"
    )


def test_duplicates_rank_by_harm_preserving_reply_order() -> None:
    document = {
        "steps": [
            {"step": 2, "gaps": [gap("a", "low"), gap("b")]},
            {"step": 1, "gaps": [gap("c"), gap("d", "medium")]},
            {"step": 2, "gaps": [gap("e"), gap("f", "low")]},
        ]
    }
    result = reply.read(json.dumps(document), STEPS)
    assert [entry["step"] for entry in result["steps"]] == [1, 2]
    assert [entry["gap"] for entry in result["steps"][1]["gaps"]] == [
        "b",
        "e",
        "a",
        "f",
    ]
    assert [entry["gap"] for entry in result["ranked"]] == [
        "b",
        "c",
        "e",
        "d",
        "a",
        "f",
    ]
    assert result["counts"] == {"high": 3, "medium": 1, "low": 2}
    assert result["parse_error"] is None


def test_empty_steps_and_zero_counts() -> None:
    result = reply.read('{"steps": []}', ())
    assert result["parse_error"] is None
    assert result["steps"] == result["ranked"] == result["uncovered_steps"] == []
    assert result["counts"] == dict.fromkeys(reply.HARMS, 0)


def test_schema_integer_and_nonempty_string_semantics() -> None:
    result = reply.read(
        json.dumps({"steps": [{"step": 1.0, "gaps": [gap(" ")]}]}), STEPS[:1]
    )
    assert result["parse_error"] is None
    assert type(result["steps"][0]["step"]) is int


def test_schema_agrees_with_every_validator_rule() -> None:
    schema = json.loads(reply.schema_text())
    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.pop("$id") == "ember/gapcheck-reply/1"
    assert isinstance(schema.pop("title"), str)
    assert isinstance(schema.pop("description"), str)
    assert schema == {
        "type": "object",
        "additionalProperties": False,
        "required": list(reply.REPLY_KEYS),
        "properties": {"steps": {"type": "array", "items": {"$ref": "#/$defs/step"}}},
        "$defs": {
            "step": {
                "type": "object",
                "additionalProperties": False,
                "required": list(reply.STEP_KEYS),
                "properties": {
                    "step": {"type": "integer", "minimum": reply.STEP_MINIMUM},
                    "gaps": {"type": "array", "items": {"$ref": "#/$defs/gap"}},
                },
            },
            "gap": {
                "type": "object",
                "additionalProperties": False,
                "required": list(reply.GAP_KEYS),
                "properties": {
                    key: {
                        "type": "string",
                        "minLength": reply.STRING_MIN_LENGTH,
                        **({"enum": list(reply.HARMS)} if key == "harm" else {}),
                    }
                    for key in reply.GAP_KEYS
                },
            },
        },
    }
