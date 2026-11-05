import json
from pathlib import Path

import pytest

from ember.gapcheck import brief, plan, previous, reply
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]


@pytest.fixture(autouse=True)
def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in make_env(tmp_path).items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    "source",
    [
        "1. Read\n@do nothing\n\n2. Build\n",
        json.dumps({"tasks": [{"id": "a", "task": "Read"}, {"task": "Build"}]}),
        "",
    ],
)
def test_brief_contains_steps_schema_and_numbered_plan(
    tmp_path: Path, source: str
) -> None:
    path = tmp_path / "plan.md"
    path.write_text(source, encoding="utf-8")
    loaded = plan.load(path)
    rendered = brief.text(loaded, str(tmp_path))
    assert rendered.splitlines()[0] == brief.FIRST_LINE.format(name="plan.md")
    assert "repository in your own working folder" in rendered
    assert "repository at " not in rendered
    assert f"Cover all {len(loaded.steps)} steps." in rendered
    for step in loaded.steps:
        where = (
            "in the tasks list"
            if step.first_line is None
            else f"lines {step.first_line} to {step.last_line}"
        )
        assert f"Step {step.number} ({where}): {step.title}" in rendered
    assert reply.schema_text() in rendered
    assert f"The plan ({path}), with line numbers:" in rendered
    for number, line in enumerate(source.splitlines(), 1):
        assert f"{number:>5}  {line}" in rendered.splitlines()
    assert not any(line.startswith("@") for line in rendered.splitlines())
    assert "Change, stage, commit or stash nothing." in rendered
    assert "first gap of step 1 with harm high" in rendered
    assert "files shared by tasks" in rendered
    assert "missing order between dependent tasks" in rendered
    assert "missing check" in rendered
    assert "Do not redesign the plan" in rendered
    assert (
        "For each gap with harm high, describe the underlying cause and "
        "the other places it affects in why. Use the existing text fields; "
        "do not add fields."
    ) in rendered


@pytest.mark.parametrize(
    ("line_break", "escaped"),
    [
        ("\n", r"\n"),
        ("\r", r"\r"),
        ("\r\n", r"\r\n"),
        ("\v", r"\v"),
        ("\f", r"\f"),
        ("\x1c", r"\x1c"),
        ("\x1d", r"\x1d"),
        ("\x1e", r"\x1e"),
        ("\x85", r"\x85"),
        ("\u2028", r"\u2028"),
        ("\u2029", r"\u2029"),
    ],
)
def test_metadata_escapes_line_breaks(
    tmp_path: Path, line_break: str, escaped: str
) -> None:
    path = tmp_path / f"plan{line_break}@command.md"
    path.write_text("@command\n", encoding="utf-8")
    rendered = brief.text(plan.load(path), str(tmp_path / f"repo{line_break}@command"))
    assert rendered.splitlines()[0] == brief.FIRST_LINE.format(
        name=f"plan{escaped}@command.md"
    )
    assert f"repo{escaped}@command" not in rendered
    assert (
        f"The plan ({tmp_path}/plan{escaped}@command.md), with line numbers:"
        in rendered
    )
    assert not any(line.startswith("@") for line in rendered.splitlines())


def test_paths_preserve_spaces_tabs_and_backslashes(tmp_path: Path) -> None:
    checkout = tmp_path / "project  copy\twith\\slashes "
    checkout.mkdir()
    path = checkout / " plan  copy\twith\\slashes.md "
    path.write_text("Read files.\n", encoding="utf-8")
    rendered = brief.text(plan.load(path), str(checkout))
    assert rendered.splitlines()[0] == brief.FIRST_LINE.format(name=path.name)
    assert "repository in your own working folder" in rendered
    assert f"repository at {checkout}." not in rendered
    assert f"The plan ({path}), with line numbers:" in rendered


def test_brief_without_previous_leaves_out_earlier(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("1. Read\n2. Build\n", encoding="utf-8")
    rendered = brief.text(plan.load(path), str(tmp_path))
    assert (
        "This plan has no earlier gap check: leave out the earlier field "
        "and dispositions."
    ) in rendered.splitlines()


def test_brief_lists_earlier_gaps_with_ids_and_answers(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("1. Read\n2. Build\n", encoding="utf-8")
    earlier = previous.Previous(
        tmp_path / "earlier.json",
        (
            previous.EarlierGap(
                "g1", 2, "Old\ntitle", "Retention", "high", "w", "d", "Keep\n30 days"
            ),
            previous.EarlierGap("g2", 1, "Read", "Order", "low", "w", "d", None),
        ),
    )
    lines = brief.text(plan.load(path), str(tmp_path), earlier).splitlines()
    assert (
        r"- g1, earlier step 2 (Old\ntitle): Retention Answer: Keep\n30 days" in lines
    )
    assert "- g2, earlier step 1 (Read): Order Unanswered." in lines
    assert any(line.startswith("An earlier gap check of this plan") for line in lines)
    assert "Cover all 2 steps." in "\n".join(lines)


def test_brief_with_an_empty_previous_result(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("1. Read\n", encoding="utf-8")
    earlier = previous.Previous(tmp_path / "earlier.json", ())
    lines = brief.text(plan.load(path), str(tmp_path), earlier).splitlines()
    assert "The earlier result has no active or settled gaps." in lines


def test_closed_history_is_context_without_a_disposition(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("1. Read\n", encoding="utf-8")
    earlier = previous.Previous(
        tmp_path / "earlier.json",
        (),
        next_id=2,
        closed=({"id": "g1", "gap": "Old\nconcern", "reason": "Removed\nfrom plan"},),
    )
    rendered = brief.text(plan.load(path), str(tmp_path), earlier)
    assert "The earlier result has no active or settled gaps." in rendered
    assert "Closed history (no disposition required):" in rendered
    assert r"- g1: Old\nconcern Reason: Removed\nfrom plan" in rendered
    assert "raise a closed concern that matters again as a new gap" in rendered
