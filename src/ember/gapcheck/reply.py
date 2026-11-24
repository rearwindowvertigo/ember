"""Extract, validate and rank a gap check reply using the standard library."""

import json
import math
import re
from collections.abc import Collection, Sequence
from importlib import resources
from typing import Any

from ember.gapcheck.plan import Step

HARMS = ("high", "medium", "low")
SCHEMA_NAME = "gapcheck-reply.schema.json"
REPLY_KEYS = ("steps",)
STEP_KEYS = ("step", "gaps")
GAP_KEYS = ("gap", "harm", "why", "default")
OPTIONAL_GAP_KEYS = ("earlier",)
STEP_MINIMUM = 1
STRING_MIN_LENGTH = 1


def schema_text() -> str:
    """Read the packaged JSON Schema as UTF-8 text."""
    return resources.files("ember").joinpath("data", SCHEMA_NAME).read_text("utf-8")


def extract_json(text: str) -> dict[str, Any] | None:
    """Try the whole reply, the last fenced block, then its brace span."""
    candidates = [text.strip()]
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if blocks:
        candidates.append(blocks[-1])
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


def _object_error(
    value: object, keys: tuple[str, ...], path: str, optional: tuple[str, ...] = ()
) -> str | None:
    """Check an object's type, required keys and absence of extra keys."""
    if not isinstance(value, dict):
        return f"{path}: expected an object"
    for key in keys:
        if key not in value:
            return f"{path}: missing required key {key!r}"
    extra = sorted(set(value) - set(keys) - set(optional))
    if extra:
        return f"{path}: unexpected key {extra[0]!r}"
    return None


def _gap_error(value: object, path: str) -> str | None:
    """Check the gap's non-empty strings, its harm value and any `earlier`."""
    error = _object_error(value, GAP_KEYS, path, OPTIONAL_GAP_KEYS)
    if error:
        return error
    assert isinstance(value, dict)
    for key in (*GAP_KEYS, *(key for key in OPTIONAL_GAP_KEYS if key in value)):
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


def _reference_problem(
    gaps: Sequence[dict[str, Any]], earlier_ids: Collection[str] | None
) -> str | None:
    """Report `earlier` ids that the previous result does not have."""
    named = list(dict.fromkeys(gap["earlier"] for gap in gaps if gap["earlier"]))
    if not named:
        return None
    if earlier_ids is None:
        return (
            "the reply names earlier gap "
            + ", ".join(named)
            + ", but the check has no previous result"
        )
    unknown = [identity for identity in named if identity not in earlier_ids]
    if not unknown:
        return None
    return (
        "the reply names earlier gap "
        + ", ".join(unknown)
        + ", which the previous result does not have as an active or settled gap"
    )


def read(
    text: str,
    steps: Sequence[Step],
    *,
    earlier_ids: Collection[str] | None = None,
    answered_ids: Collection[str] = (),
    first_number: int = 1,
) -> dict[str, Any]:
    """Read valid step entries and report schema and coverage problems.

    Duplicate entries are joined. Covered steps follow plan order; their
    gaps sort by harm, preserving reply order for ties. Each gap gets the id
    `g<n>`, numbered from `first_number` in that order, an `earlier` id or
    null, and a null `answer`. `ranked` names every gap by `id`, `step` and
    `harm`, sorted by harm in reply order. Unknown steps are reported but
    excluded from gaps and counts.

    Args:
        text: The reviewer's reply.
        steps: The plan's steps.
        earlier_ids: The previous result's gap ids, or None without one.
        answered_ids: Earlier gaps with recorded answers.
        first_number: The number of the first new gap id.

    Returns:
        The result fields `steps`, `ranked`, `counts`, `uncovered_steps`,
        `unknown_steps`, `parse_error`, `next_id` and `dispositions`.
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
        "next_id": first_number,
        "dispositions": [],
    }
    document = extract_json(text)
    if document is None:
        result["parse_error"] = "the reply is not a JSON object"
        return result
    error = _object_error(document, REPLY_KEYS, "$", ("dispositions",))
    entries = document.get("steps")
    if not isinstance(entries, list):
        error = error or "$.steps: expected an array"
        entries = []
    gaps: dict[int, list[dict[str, Any]]] = {}
    flat: list[tuple[int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        entry_error = _step_error(entry, f"$.steps[{index}]")
        error = error or entry_error
        if entry_error:
            continue
        number = int(entry["step"])
        made = [
            {
                "id": None,
                **{key: gap[key] for key in GAP_KEYS},
                "earlier": gap.get("earlier"),
                "answer": None,
            }
            for gap in entry["gaps"]
        ]
        gaps.setdefault(number, []).extend(made)
        if number in titles:
            flat.extend((number, gap) for gap in made)
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
    identity = first_number
    for step in result["steps"]:
        for gap in step["gaps"]:
            gap["id"] = f"g{identity}"
            identity += 1
    result["next_id"] = identity
    result["ranked"] = [
        {"id": gap["id"], "step": number, "harm": gap["harm"]}
        for number, gap in sorted(flat, key=lambda pair: rank[pair[1]["harm"]])
    ]
    for _, gap in flat:
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
    reference = _reference_problem([gap for _, gap in flat], earlier_ids)
    if reference:
        problems.append(reference)
    problems.extend(_dispositions(document, result, flat, earlier_ids, answered_ids))
    result["parse_error"] = "; ".join(problems) or None
    return result


def _dispositions(
    document: dict[str, Any],
    result: dict[str, Any],
    gaps: Sequence[tuple[int, dict[str, Any]]],
    earlier_ids: Collection[str] | None,
    answered_ids: Collection[str],
) -> list[str]:
    """Check coverage and one-to-one links for earlier active and settled gaps."""
    entries = document.get("dispositions", [])
    if not isinstance(entries, list):
        return ["$.dispositions: expected an array"]
    problems = []
    seen: dict[str, str] = {}
    for index, entry in enumerate(entries):
        path = f"$.dispositions[{index}]"
        error = _object_error(entry, ("id", "status", "reason"), path)
        if not error:
            for key in ("id", "status", "reason"):
                if not isinstance(entry[key], str) or not entry[key].strip():
                    error = f"{path}.{key}: expected a non-empty string"
                    break
        if not error and entry["status"] not in ("settled", "reopened", "closed"):
            error = f"{path}.status: expected one of settled, reopened, closed"
        if error:
            problems.append(error)
            continue
        identity, status = entry["id"], entry["status"]
        if earlier_ids is None or identity not in earlier_ids:
            problems.append(f"{path}: unknown earlier id {identity}")
        elif identity in seen:
            problems.append(f"{path}: duplicate disposition for {identity}")
        else:
            seen[identity] = status
            result["dispositions"].append(entry)
        if status == "settled" and identity not in answered_ids:
            problems.append(f"{path}: unanswered gap {identity} cannot be settled")
    missing = sorted(set(earlier_ids or ()) - seen.keys())
    if missing:
        problems.append("the reply misses earlier gap " + ", ".join(missing))
    links = [gap["earlier"] for _, gap in gaps if gap["earlier"]]
    for identity, status in seen.items():
        count = links.count(identity)
        if status == "reopened" and count != 1:
            problems.append(
                f"reopened gap {identity} needs exactly one new gap "
                f"with earlier set to {identity}"
            )
        elif status != "reopened" and count:
            problems.append(
                f"gap {identity} is {status} but a new gap names it as earlier"
            )
    return problems
