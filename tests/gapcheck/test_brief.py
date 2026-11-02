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


def test_metadata_cannot_start_command_lines(tmp_path: Path) -> None:
    path = tmp_path / "plan\n@command.md"
    path.write_text("@command\n", encoding="utf-8")
    rendered = brief.text(plan.load(path), str(tmp_path / "repo\n@command"))
    assert len(rendered.splitlines()[0]) > 0
    assert not any(line.startswith("@") for line in rendered.splitlines())
