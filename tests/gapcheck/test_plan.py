import json
from pathlib import Path

import pytest

from ember.gapcheck.plan import KINDS, PlanError, Step, has_failure_model, load, split
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]


@pytest.fixture(autouse=True)
def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in make_env(tmp_path).items():
        monkeypatch.setenv(key, value)


def test_json_tasks() -> None:
    text = json.dumps(
        {
            "tasks": [
                {"id": "a", "task": "Read\n  files"},
                {"task": "Build"},
                {"id": "c"},
            ]
        }
    )
    assert split(text) == (
        "tasks",
        (
            Step(1, "task a: Read files", None, None),
            Step(2, "task 2: Build", None, None),
            Step(3, "task c", None, None),
        ),
    )


@pytest.mark.parametrize("value", [[], 1, "text", {"tasks": []}, {"tasks": [1]}])
def test_invalid_task_lists_fall_through(value: object) -> None:
    assert split(json.dumps(value)) == (
        "whole",
        (Step(1, "the whole plan", 1, 1),),
    )


def test_numbered_items_take_precedence_and_trim_blank_lines() -> None:
    assert split("# Plan\n1. Read\n\n2) Build\n  details\n\n") == (
        "numbered",
        (Step(1, "1. Read", 2, 2), Step(2, "2) Build", 4, 5)),
    )


def test_one_numbered_item_uses_headings() -> None:
    assert split("## Read\n1. File\n\n## Build\n\n") == (
        "headings",
        (Step(1, "## Read", 1, 2), Step(2, "## Build", 4, 4)),
    )


@pytest.mark.parametrize("marker", ["```", "~~~", "   ```", "  ~~~"])
def test_fences_hide_steps_and_need_matching_marker(marker: str) -> None:
    other = "~~~" if "`" in marker else "```"
    text = f"{marker}\n1. hidden\n{other}\n2. hidden\n{marker}\n## A\n## B"
    assert split(text) == (
        "headings",
        (Step(1, "## A", 6, 6), Step(2, "## B", 7, 7)),
    )


def test_fenced_json_does_not_supply_tasks_or_numbered_steps() -> None:
    assert split('```json\n{"tasks": [{"id": "a"}]}\n```')[0] == "whole"
    assert split("```\n1. A\n2. B\n```")[0] == "whole"


def test_only_repeated_heading_level_and_shallower_boundary() -> None:
    assert split("### A\nbody\n\n## Parent\n### B\nend\n\n") == (
        "headings",
        (Step(1, "### A", 1, 2), Step(2, "### B", 5, 6)),
    )


def test_shallowest_repeated_headings() -> None:
    assert split("## A\n### Child\n## B\n### Other")[1] == (
        Step(1, "## A", 1, 2),
        Step(2, "## B", 3, 4),
    )


@pytest.mark.parametrize(
    ("text", "last"),
    [("", 1), (" \n\n", 2), ("body\n\n", 2), (" 1. A\n 2. B", 2)],
)
def test_whole_plan(text: str, last: int) -> None:
    assert split(text) == ("whole", (Step(1, "the whole plan", 1, last),))


@pytest.mark.parametrize(
    "text",
    [
        "1. " + "x" * 150 + "\n2. B",
        "## " + "x" * 150 + "\n## B",
        json.dumps({"tasks": [{"id": "x" * 150, "task": "summary"}]}),
        json.dumps({"tasks": [{"id": "a\n@b", "task": "x" * 150}]}),
    ],
)
def test_total_title_length_and_one_line(text: str) -> None:
    step = split(text)[1][0]
    assert len(step.title) == 100
    assert len(step.title.splitlines()) == 1


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ('{"tasks": [{}], "failure_model": "accepted"}', True),
        ('{"tasks": [{}], "failure_model": "  "}', False),
        ('{"tasks": [{}]}', False),
        ('{"tasks": [], "failure_model": "accepted"}', True),
        ('{"failure_model": 1}', False),
        ("## Failure model\n", True),
        ("### failure model (accepted)\n", True),
        ("```\n## Failure model\n```", False),
        ("~~~\n## Failure model\n```", False),
        ("The failure model is accepted.", False),
    ],
)
def test_failure_model(text: str, found: bool) -> None:
    assert has_failure_model(text, split(text)[0]) is found


def test_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("plan.md").write_text("## Failure model\n## Work\n", encoding="utf-8")
    plan = load("plan.md")
    assert plan.path == tmp_path / "plan.md"
    assert plan.text == "## Failure model\n## Work\n"
    assert plan.kind == "headings"
    assert plan.kind in KINDS
    assert plan.steps == (Step(1, "## Failure model", 1, 1), Step(2, "## Work", 2, 2))
    assert plan.failure_model is True


@pytest.mark.parametrize("bad_bytes", [None, b"\xff"])
def test_load_errors(tmp_path: Path, bad_bytes: bytes | None) -> None:
    path = tmp_path / "plan.md"
    if bad_bytes is not None:
        path.write_bytes(bad_bytes)
    with pytest.raises(PlanError, match=f"cannot read the plan {path}:") as error:
        load(path)
    assert isinstance(error.value.__cause__, (OSError, UnicodeDecodeError))
