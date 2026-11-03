"""The owner's gap check settings: whether a plan is checked, and which gaps ask.

`gapcheck.when` decides whether a plan of a given number of steps gets a gap
check: `off`, `always`, a least step count (`2` to `10`), or `ask`.
`gapcheck.ask` names the lowest harm put to the owner: `high`, `medium` (high
and medium) or `all`. Gaps below it take their proposed default.
"""

from collections.abc import Mapping
from typing import Any

from ember.gapcheck import reply

RUN = "run"
SKIPPED = "skipped"
ASK = "ask"


def decide(answers: Mapping[str, Mapping[str, Any]], steps: int) -> tuple[str, str]:
    """Return whether a plan of `steps` steps gets a gap check, and why.

    Args:
        answers: Resolved choices answers.
        steps: The plan's step count.

    Returns:
        `RUN`, `SKIPPED` or `ASK`, with a sentence naming the setting.
    """
    when = str(answers["gapcheck.when"]["value"])
    if when == "off":
        return SKIPPED, "the gap check is off (Team, Run the gap check)"
    if when == "always":
        return RUN, "the gap check runs on every plan"
    if when == "ask":
        return ASK, "the gap check asks the owner for each plan"
    least = int(when)
    noun = "step" if steps == 1 else "steps"
    if steps < least:
        return SKIPPED, (
            f"the plan has {steps} {noun}; the gap check runs on plans of "
            f"{least} or more (Team, Run the gap check)"
        )
    return RUN, f"the plan has {steps} {noun}; the gap check runs on {least} or more"


def asks_owner(answers: Mapping[str, Mapping[str, Any]], harm: str) -> bool:
    """Return whether a gap of this harm is put to the owner.

    Args:
        answers: Resolved choices answers.
        harm: `high`, `medium` or `low`.

    Returns:
        True when the harm is at or above `gapcheck.ask`.
    """
    lowest = {"high": "high", "medium": "medium", "all": "low"}[
        str(answers["gapcheck.ask"]["value"])
    ]
    return reply.HARMS.index(harm) <= reply.HARMS.index(lowest)
