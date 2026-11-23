"""Build a read-only gap check brief with the entire plan and reply schema."""

import hashlib
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from ember import citations
from ember.flows import plan as flow_plan
from ember.gapcheck.plan import Plan
from ember.gapcheck.previous import Previous, changes
from ember.gapcheck.reply import schema_text

FIRST_LINE = "Gap check of the plan {name}: a read-only review; change no file."
PLAN_HASH = "Checked plan SHA-256: "
_LINE_BREAK_ESCAPES = str.maketrans(
    {
        "\n": r"\n",
        "\r": r"\r",
        "\v": r"\v",
        "\f": r"\f",
        "\x1c": r"\x1c",
        "\x1d": r"\x1d",
        "\x1e": r"\x1e",
        "\x85": r"\x85",
        "\u2028": r"\u2028",
        "\u2029": r"\u2029",
    }
)


def _path_text(value: str) -> str:
    """Escape line breaks in a path, preserving every other character."""
    return value.translate(_LINE_BREAK_ESCAPES)


def plan_hash(plan: Plan) -> str:
    """Hash the exact UTF-8 plan text embedded in the brief."""
    return hashlib.sha256(plan.text.encode("utf-8")).hexdigest()


def matches_plan(text: str, plan: Plan) -> bool:
    """Check that a brief carries exactly one matching plan hash line."""
    hashes = re.findall(r"^Checked plan SHA-256: ([0-9a-f]{64})$", text, re.M)
    return hashes == [plan_hash(plan)]


def _earlier(previous: Previous | None) -> list[str]:
    """List the earlier check's gaps with their ids and answers."""
    if previous is None:
        return [
            "This plan has no earlier gap check: leave out the earlier field "
            "and dispositions.",
            "",
        ]
    lines = [
        "An earlier gap check of this plan found the gaps below. Give a "
        "dispositions entry {id, status, reason} for every active and settled "
        "earlier gap. A gap with an answer is settled: keep it settled unless "
        "a change invalidates its answer. An unanswered gap must be reopened "
        "or closed, never settled. Explain each disposition in reason. For "
        "every reopened id, report exactly one new gap with earlier set to "
        "that id; explain in gap what reopens it. Closed history needs no "
        "disposition; raise a closed concern that matters again as a new gap "
        "without earlier. Earlier step numbers may differ from the steps "
        "below; report gaps under their current steps. Every step below "
        "still needs an entry.",
        "",
    ]
    for gap in previous.gaps:
        answer = (
            f"Answer: {_path_text(gap.answer)}"
            if gap.answer is not None
            else "Unanswered."
        )
        lines.append(
            f"- {_path_text(gap.id)}, earlier step {gap.step} "
            f"({_path_text(gap.title)}): {_path_text(gap.gap)} {answer}"
        )
    if not previous.gaps:
        lines.append("The earlier result has no active or settled gaps.")
    if previous.closed:
        lines.append("Closed history (no disposition required):")
        for gap in previous.closed:
            lines.append(
                f"- {_path_text(gap['id'])}: {_path_text(gap['gap'])} "
                f"Reason: {_path_text(gap['reason'])}"
            )
    return [*lines, ""]


_COMPARED = (
    "The list compares a flow plan's context, failure model, tasks and waves, "
    "or another plan's text. A flow plan's check, writer checks, read files "
    "and folder are not compared and are outside this repeat."
)
_REPEAT_LIMITS = (
    "On this repeat, report a missing failure model only when no earlier gap "
    "names it, and search for outside tests and callers only for the listed "
    "changes."
)


def _changes(plan: Plan, previous: Previous | None) -> list[str]:
    """Limit a repeat check to what the plan gained and lost since the earlier one.

    Without an earlier result, or when it holds no comparable saved plan,
    the section is left out and the check covers the whole plan.
    """
    found = None if previous is None else changes(plan, previous)
    if found is None:
        return []
    heading = ["## Changes since the earlier check", ""]
    if not found:
        return [
            *heading,
            "Nothing in the list below has changed since the earlier check. "
            "Report a gap only to reopen an earlier one; every other step list "
            f"is empty. {_COMPARED} {_REPEAT_LIMITS}",
            "",
        ]
    return [
        *heading,
        "The earlier check read this plan before the changes below. Report a "
        "new gap only when it arises from a change listed here: text that was "
        "added, altered or removed, a task that was added or removed, a file "
        "or dependency that was added or removed, or changed waves. "
        "Do not review unchanged text again. A step whose changes raise no gap "
        "gets an empty list. An earlier gap is reopened as the rule above says. "
        f"{_COMPARED} {_REPEAT_LIMITS}",
        "",
        *found,
    ]


def _sources(files: Sequence[Path]) -> list[str]:
    """Return the sources section and a blank line, or nothing without files."""
    section = citations.sources_section(files)
    return [section, ""] if section else []


def _flow(plan: flow_plan.Plan | None) -> list[str]:
    """Describe the planner's waves and the flow runner's execution rules."""
    if plan is None:
        return []
    return [
        "## How this flow runs",
        "",
        "The flow planner computed these waves:",
        *(
            f"- Wave {number}: {', '.join(task.id for task in wave)}"
            for number, wave in enumerate(flow_plan.waves(plan), 1)
        ),
        "",
        "Tasks with overlapping files, including folder-prefix overlaps, run "
        "in separate waves. Waves are the planned order assuming each wave "
        "merges fully; they are not dependencies. When a task stops, a later "
        "task sharing its file can run without its change. A task that needs "
        "another task's result still needs that task in after.",
        "Each task runs in its own isolated worktree and merges into the flow branch.",
        (
            f"The plan check runs after each wave: {_path_text(shlex.join(plan.check))}"
            if plan.check
            else "The plan has no check command; waves merge without a check."
        ),
        "Report an ordering gap only when a needed direction is missing or "
        "wrong. Check for tasks whose files overlap only on a shared file, even "
        "when they edit different sections, and raise that overlap as a gap "
        "when the shared edits can be separated from the tasks' own checks, "
        "even when their waves serialize them.",
        "",
    ]


def text(
    plan: Plan,
    checkout: str,
    previous: Previous | None = None,
    sources: Sequence[Path] = (),
) -> str:
    """Describe the review, list every step and paste the numbered plan.

    Args:
        plan: The plan under review.
        checkout: The source checkout, omitted from the review instructions.
        previous: An earlier result whose gaps and answers the brief lists.
        sources: The read files the lead supplied, listed for the reviewer.

    Returns:
        The brief, ending with a newline.
    """
    listing = []
    for step in plan.steps:
        where = (
            f"lines {step.first_line} to {step.last_line}"
            if step.first_line is not None
            else "in the tasks list"
        )
        listing.append(f"Step {step.number} ({where}): {step.title}")
    numbered = "\n".join(
        f"{number:>5}  {line}" for number, line in enumerate(plan.text.splitlines(), 1)
    )
    brief = "\n".join(
        [
            FIRST_LINE.format(name=_path_text(plan.path.name)),
            PLAN_HASH + plan_hash(plan),
            "",
            "Read the plan below and the repository in your own working folder. "
            "For every step, list the behaviours the plan leaves undefined or "
            "contradictory: what a writer following the plan would have to invent, "
            "where two parts disagree, files shared by tasks that can run at the "
            "same time, a missing order between dependent tasks, or a missing check. "
            "A case the failure model settles is not a gap. Report a gap only when "
            "you can state a case in which a writer who follows the plan as written "
            "produces a wrong result or cannot finish, two tasks change the same "
            "thing, or a test or caller outside a task's declared files breaks. "
            "State that case in why. A step with no such case gets an empty list: "
            "do not look for gaps to fill a step. One gap is reported whatever "
            "this rule says: when the plan has no failure model, say so in the "
            "first gap of step 1 with harm high.",
            "For each task, search for tests and callers outside the declared "
            "files that reference what it changes. Name one in a gap when the "
            "task's change breaks it.",
            "Change, stage, commit or stash nothing.",
            "",
            "For each gap give these four fields:",
            "- gap: the undefined or contradictory behaviour, in one line",
            "- harm: high (work or data can be lost, or a wrong result is likely "
            "in normal use), medium (a writer would likely choose differently from "
            "the plan's author and the difference needs rework), or low "
            "(easily changed later)",
            "- why: the reason for the harm, in one line",
            "- default: the behaviour you propose when no one decides otherwise",
            "",
            "For each gap with harm high, describe the underlying cause and "
            "the other places it affects in why. Use the existing text fields; "
            "do not add fields.",
            "",
            *_flow(plan.flow),
            *_sources(sources),
            *_earlier(previous),
            *_changes(plan, previous),
            "Do not redesign the plan and do not decide missing behaviour; "
            "name each gap and propose a default.",
            "",
            f"Cover all {len(plan.steps)} steps. Give every step an entry, in order; "
            'a step with no gaps gets "gaps": []. Use the step numbers below.',
            "",
            *listing,
            "",
            "Reply with only one JSON object matching this JSON Schema, "
            "with no other text and no code fence:",
            schema_text(),
            "",
            f"The plan ({_path_text(str(plan.path))}), with line numbers:",
            numbered,
        ]
    )
    return brief + "\n"
