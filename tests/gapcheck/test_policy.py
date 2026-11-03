from typing import Any

import pytest

from ember.gapcheck import policy


def answers(when: str = "always", ask: str = "high") -> dict[str, Any]:
    return {
        "gapcheck.when": {"value": when, "layer": "setting"},
        "gapcheck.ask": {"value": ask, "layer": "setting"},
    }


@pytest.mark.parametrize(
    ("when", "steps", "decision"),
    [
        ("off", 9, policy.SKIPPED),
        ("always", 1, policy.RUN),
        ("ask", 4, policy.ASK),
        ("3", 2, policy.SKIPPED),
        ("3", 3, policy.RUN),
        ("10", 9, policy.SKIPPED),
        ("10", 10, policy.RUN),
    ],
)
def test_decide(when: str, steps: int, decision: str) -> None:
    result, why = policy.decide(answers(when), steps)
    assert result == decision
    assert why


def test_decide_names_the_counts() -> None:
    assert policy.decide(answers("5"), 1)[1] == (
        "the plan has 1 step; the gap check runs on plans of 5 or more "
        "(Team, Run the gap check)"
    )


@pytest.mark.parametrize(
    ("ask", "asked"),
    [
        ("high", ["high"]),
        ("medium", ["high", "medium"]),
        ("all", ["high", "medium", "low"]),
    ],
)
def test_asks_owner(ask: str, asked: list[str]) -> None:
    for harm in ("high", "medium", "low"):
        assert policy.asks_owner(answers(ask=ask), harm) == (harm in asked)
