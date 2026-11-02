import json
from pathlib import Path

import pytest

from ember.gapcheck import brief, plan, reply
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
    assert f"repository at {tmp_path}" in rendered
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
    assert f"repository at {tmp_path}/repo{escaped}@command." in rendered
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
    assert f"repository at {checkout}." in rendered
    assert f"The plan ({path}), with line numbers:" in rendered
