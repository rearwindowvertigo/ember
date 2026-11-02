"""Extract, validate and rank a gap check reply using the standard library."""

import json
import math
import re
from collections.abc import Sequence
from importlib import resources
from typing import Any

from ember.gapcheck.plan import Step

HARMS = ("high", "medium", "low")
SCHEMA_NAME = "gapcheck-reply.schema.json"
REPLY_KEYS = ("steps",)
STEP_KEYS = ("step", "gaps")
GAP_KEYS = ("gap", "harm", "why", "default")
STEP_MINIMUM = 1
STRING_MIN_LENGTH = 1


def schema_text() -> str:
    """Read the packaged JSON Schema as UTF-8 text."""
    return resources.files("ember").joinpath("data", SCHEMA_NAME).read_text("utf-8")


def extract_json(text: str) -> dict[str, Any] | None:
    """Try the whole reply, fenced JSON from last to first, then its brace span."""
    candidates = [text.strip()]
    candidates.extend(
        reversed(re.findall(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL))
    )
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{") : text.rindex("}") + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _object_error(value: object, keys: tuple[str, ...], path: str) -> str | None:
    """Check an object's type, required keys and absence of extra keys."""
    if not isinstance(value, dict):
        return f"{path}: expected an object"
    for key in keys:
        if key not in value:
            return f"{path}: missing required key {key!r}"
    extra = sorted(set(value) - set(keys))
    if extra:
        return f"{path}: unexpected key {extra[0]!r}"
    return None


def _gap_error(value: object, path: str) -> str | None:
    """Check the gap's four non-empty strings and harm value."""
    error = _object_error(value, GAP_KEYS, path)
    if error:
        return error
    assert isinstance(value, dict)
    for key in GAP_KEYS:
        if not isinstance(value[key], str):
            return f"{path}.{key}: expected a string"
        if len(value[key]) < STRING_MIN_LENGTH:
            return f"{path}.{key}: expected a non-empty string"
        if key == "harm" and value[key] not in HARMS:
            return f"{path}.harm: expected one of high, medium, low"
    return None


def _step_error(value: object, path: str) -> str | None:
    """Check a step entry, including every gap, against the reply schema."""
    error = _object_error(value, STEP_KEYS, path)
    if error:
        return error
    assert isinstance(value, dict)
    number = value["step"]
    integer = type(number) is int or (
        type(number) is float and math.isfinite(number) and number.is_integer()
    )
    if not integer:
        return f"{path}.step: expected an integer"
    if number < STEP_MINIMUM:
        return f"{path}.step: expected an integer at least {STEP_MINIMUM}"
    if not isinstance(value["gaps"], list):
        return f"{path}.gaps: expected an array"
    for index, gap in enumerate(value["gaps"]):
        error = _gap_error(gap, f"{path}.gaps[{index}]")
        if error:
            return error
    return None


def read(text: str, steps: Sequence[Step]) -> dict[str, Any]:
    """Read valid step entries and report schema and coverage problems.

    Duplicate entries are joined. Covered steps follow plan order; their
    gaps and the combined ranking sort by harm, preserving reply order for
    ties. Unknown steps are reported but excluded from gaps and counts.
    """
    expected = [step.number for step in steps]
    titles = {step.number: step.title for step in steps}
    result: dict[str, Any] = {
        "steps": [],
        "ranked": [],
        "counts": dict.fromkeys(HARMS, 0),
        "uncovered_steps": list(expected),
        "unknown_steps": [],
        "parse_error": None,
    }
    document = extract_json(text)
    if document is None:
        result["parse_error"] = "the reply is not a JSON object"
        return result
    error = _object_error(document, REPLY_KEYS, "$")
    entries = document.get("steps")
    if not isinstance(entries, list):
        error = error or "$.steps: expected an array"
        entries = []
    gaps: dict[int, list[dict[str, Any]]] = {}
    flat: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        entry_error = _step_error(entry, f"$.steps[{index}]")
        error = error or entry_error
        if entry_error:
            continue
        number = int(entry["step"])
        gaps.setdefault(number, []).extend(entry["gaps"])
        if number in titles:
            flat.extend({"step": number, **gap} for gap in entry["gaps"])
    rank = {harm: index for index, harm in enumerate(HARMS)}
    result["steps"] = [
        {
            "step": number,
            "title": titles[number],
            "gaps": sorted(gaps[number], key=lambda gap: rank[gap["harm"]]),
        }
        for number in expected
        if number in gaps
    ]
    result["ranked"] = sorted(flat, key=lambda gap: rank[gap["harm"]])
    for gap in flat:
        result["counts"][gap["harm"]] += 1
    result["uncovered_steps"] = [number for number in expected if number not in gaps]
    result["unknown_steps"] = sorted(number for number in gaps if number not in titles)
    problems = []
    if error:
        problems.append(f"the reply does not match the schema at {error}")
    if result["uncovered_steps"]:
        problems.append(
            "the reply misses step " + ", ".join(map(str, result["uncovered_steps"]))
        )
    if result["unknown_steps"]:
        problems.append(
            "the reply names step "
            + ", ".join(map(str, result["unknown_steps"]))
            + ", which the plan does not have"
        )
    result["parse_error"] = "; ".join(problems) or None
    return result
