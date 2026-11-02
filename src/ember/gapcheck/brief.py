"""Build a read-only gap check brief with the entire plan and reply schema."""

from ember.gapcheck.plan import Plan
from ember.gapcheck.reply import schema_text

FIRST_LINE = "Gap check of the plan {name}: a read-only review; change no file."


def text(plan: Plan, checkout: str) -> str:
    """Describe the review, list every step and paste the numbered plan."""
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
            FIRST_LINE.format(name=" ".join(plan.path.name.split())),
            "",
            f"Read the plan below and the repository at {' '.join(checkout.split())}. "
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
            f"The plan ({' '.join(str(plan.path).split())}), with line numbers:",
            numbered,
        ]
    )
    return brief + "\n"
