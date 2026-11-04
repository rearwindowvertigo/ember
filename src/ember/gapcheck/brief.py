"""Build a read-only gap check brief with the entire plan and reply schema."""

from ember.gapcheck.plan import Plan
from ember.gapcheck.previous import Previous
from ember.gapcheck.reply import schema_text

FIRST_LINE = "Gap check of the plan {name}: a read-only review; change no file."
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


def text(plan: Plan, checkout: str, previous: Previous | None = None) -> str:
    """Describe the review, list every step and paste the numbered plan.

    Args:
        plan: The plan under review.
        checkout: The source checkout, omitted from the review instructions.
        previous: An earlier result whose gaps and answers the brief lists.

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
            "",
            "Read the plan below and the repository in your own working folder. "
            "For every step, list the behaviours the plan leaves undefined or "
            "contradictory: what a writer following the plan would have to invent, "
            "where two parts disagree, files shared by tasks that can run at the "
            "same time, a missing order between dependent tasks, or a missing check. "
            "A case the failure model settles is not a gap. When the plan has no "
            "failure model, say so in the first gap of step 1 with harm high.",
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
            *_earlier(previous),
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
