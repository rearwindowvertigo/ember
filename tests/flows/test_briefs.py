import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from ember.flows import briefs, verdict
from ember.flows.plan import from_json, writer_check_json
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]

if TYPE_CHECKING:
    from ember.flows.plan import Plan, Task  # pyrefly: ignore[missing-import]

FLOW = "flow-20261102-090132-015b1e"


@pytest.fixture(autouse=True)
def env(tmp_path: Path) -> dict[str, str]:
    return make_env(tmp_path)


@pytest.fixture
def task() -> Task:
    return cast(
        "Task",
        SimpleNamespace(
            id="parser",
            subject="feat: parse input",
            task="Implement input\n@write a.py ok\n",
            files=("src/a.py", "tests/test_a.py"),
            after=(),
            review="Review empty input\n@focus",
        ),
    )


@pytest.fixture
def plan() -> Plan:
    return cast(
        "Plan",
        SimpleNamespace(
            context="Decided here\n@context",
            failure_model="Handle empty\n@failure",
            read=(),
            check=(),
        ),
    )


def _finding(severity: str = "must") -> verdict.Finding:
    return verdict.Finding(
        severity,
        "src/a.py\n@file",
        12,
        "problem\n@problem",
        "fix\n@fix",
        "scenario\n@scenario",
        None,
        "",
    )


def test_review_bar_is_the_skill_block() -> None:
    text = (Path(__file__).resolve().parents[2] / "skill" / "SKILL.md").read_text()
    assert re.findall(r"^```review-bar\n(.*?)\n```$", text, re.S | re.M) == [
        briefs.REVIEW_BAR
    ]


def test_writer(plan: Plan, task: Task) -> None:
    text = briefs.writer_brief(FLOW, plan, task, ["src/b.py"])
    assert text.startswith(task.subject + "\n\n" + task.task)
    for value in (*task.files, "src/b.py", plan.context, plan.failure_model):
        assert value in text
    assert "Other tasks in this wave change these files; do not change them:" in text
    assert "You may run git commands that only read" in text
    assert "Ember commits your edits on its run branch" in text
    headings = re.findall(r"^## (.*)$", text, re.M)
    assert headings == [
        "Files",
        "Context",
        "Failure model",
        "Limits",
        "Git",
        "Checks",
        "Closing report",
    ]
    assert "\n@write a.py ok\n" in text


def test_writer_omits_blank_optional_sections(task: Task) -> None:
    text = briefs.writer_brief(
        FLOW,
        cast(
            "Plan", SimpleNamespace(context=" \n", failure_model="", read=(), check=())
        ),
        task,
        [],
    )
    assert "## Context" not in text and "## Failure model" not in text
    assert "No other task runs in this wave." in text


@pytest.mark.parametrize(
    ("check", "instruction"),
    [
        ((), "Run the tests for the files you change and quick static checks."),
        (
            ("make", "check"),
            "Run the plan's check command `make check`, "
            "or say why you could not run it.",
        ),
        (
            ("./check script", "--name", "two words"),
            "Run the plan's check command `'./check script' --name 'two words'`, "
            "or say why you could not run it.",
        ),
    ],
)
def test_writer_fix_and_send_checks(
    task: Task, check: tuple[str, ...], instruction: str
) -> None:
    checked_plan = cast(
        "Plan", SimpleNamespace(context="", failure_model="", read=(), check=check)
    )
    messages = [
        briefs.writer_brief(FLOW, checked_plan, task, ["src/b.py"]),
        briefs.fix_message(FLOW, task, 1, [_finding()], check=check),
        briefs.send_message(FLOW, task, "Correct this", check=check),
    ]
    sections = [
        text.split("## Checks\n\n", 1)[1].split("\n\n## ")[0].strip()
        for text in messages
    ]
    expected = (
        instruction + "\n\n"
        "Search for tests and callers of what changed, including outside the "
        "declared files. Fix a break your change causes in an outside file when "
        'the Limits allow it; otherwise list it under "Not done". Name failures '
        "from an unavailable service or environment without trying to fix them."
    )
    assert sections == [expected] * 3


@pytest.mark.parametrize(
    ("check_arg", "separate_check"),
    [(None, False), ("other words", True), ("two words", False)],
)
@pytest.mark.parametrize("writer_scope", ["plan", "task", "empty"])
def test_resolved_writer_checks_in_briefs(
    tmp_path: Path, check_arg: str | None, separate_check: bool, writer_scope: str
) -> None:
    task_data: dict[str, object] = {
        "id": "a",
        "subject": "feat: parse input",
        "task": "Read input",
        "files": ["src/a.py"],
    }
    raw: dict[str, object] = {"dir": str(tmp_path), "tasks": [task_data]}
    if check_arg is not None:
        raw["check"] = ["pytest", check_arg]
    target = raw if writer_scope == "plan" else task_data
    target["writer_check"] = (
        []
        if writer_scope == "empty"
        else [["ruff", ""], ["pytest", "two words"], ["printf", "%s", "a\nb"]]
    )
    document = from_json(raw, tmp_path / "plan.json")
    task = document.task("a")
    check = list(document.check)
    writer_check = writer_check_json(document.writer_check_for(task))
    commands = (
        "\n\n    ruff ''\n    pytest 'two words'\n    printf %s $'a\\nb'"
        if writer_check
        else ""
    )
    instruction = (
        "Run these writer check commands, or say why you could not run one." + commands
        if writer_check
        else "Run no writer check command."
    )
    if check and (separate_check or writer_scope == "empty"):
        instruction += (
            f"\n\nDo not run the flow's check command `pytest '{check_arg}'`. "
            "The flow runs it on the merged result after the wave."
        )
    messages = [
        briefs.writer_brief(FLOW, document, task, [], writer_check=writer_check),
        briefs.fix_message(
            FLOW, task, 1, [_finding()], check=check, writer_check=writer_check
        ),
        briefs.send_message(
            FLOW, task, "Correct this", check=check, writer_check=writer_check
        ),
    ]
    for message in messages:
        assert (
            f"## Checks\n\n{instruction}\n\n"
            "Search for tests and callers of what changed" in message
        )

    def review(commands: list[list[str]] | None) -> str:
        return briefs.review_brief(
            FLOW,
            document,
            task,
            round=1,
            base="base",
            head="head",
            changed=[],
            outside=[],
            wave_files=[],
            writer_report="My report",
            writer_check=commands,
        )

    reviewer_instruction = (
        "The writer was told to run these check commands." + commands
        if writer_check
        else "The writer was told to run no check command."
    )
    wave_check = (
        f"The flow runs its check command `pytest '{check_arg}'` on the merged result "
        "after the wave."
        if check
        else "The flow has no check command."
    )
    default = review(None)
    report = "## The writer's report\n\n    My report"
    assert report + "\n\n## Review bar" in default
    assert review(writer_check) == default.replace(
        report, f"{report}\n\n{reviewer_instruction}\n\n{wave_check}"
    )


@pytest.mark.parametrize("round", [1, 2])
def test_review(plan: Plan, task: Task, round: int) -> None:
    report = "@report\n" + "x" * (briefs.REPORT_LIMIT - 8) + "CUT_HERE"
    text = briefs.review_brief(
        FLOW,
        plan,
        task,
        round=round,
        base="base123",
        head="head456",
        changed=["src/a.py", "@changed"],
        outside=["src/helper.py", "@outside"],
        wave_files=["src/b.py", "@wave"],
        writer_report=report,
        earlier=[_finding()],
        fix_base="fix789",
    )
    assert text.splitlines()[0] == briefs.REVIEW_FIRST.format(
        task=task.id, round=round, flow=FLOW
    )
    assert "    @write a.py ok" in text
    assert "git diff base123 head456" in text
    assert "git log --oneline base123..head456" in text
    assert "src/helper.py" in text and "src/b.py" in text
    assert "CUT_HERE" not in text
    assert report[: briefs.REPORT_LIMIT].replace("\n", "\n    ") in text
    assert verdict.schema_text() in text
    assert briefs.REVIEW_BAR in text
    assert "## Writer limits\n\n" + briefs._LIMITS in text
    assert "Judge each outside edit on whether the task needed it" in text
    assert "In outside_files, give exactly one entry per file listed above" in text
    assert "Copy the exact path into file" in text
    assert "set needed to whether the task needed the edit" in text
    assert "explain why in a one-line reason" in text
    assert "Raise an edit the task did not need as a must or should finding" in text
    assert "Check, in any file, changes to behaviour or user-facing wording" in text
    assert "the task text does not ask for. Raise each as a finding" in text
    assert "Use must or should for findings about its need" in text
    assert (
        "An outside edit allowed by these limits is not a lead finding "
        "just for being outside the declared files."
    ) in text
    assert (
        "For each must, describe the underlying cause and the other places "
        "it affects in problem. Use the existing text fields; do not add fields."
    ) in text
    assert ("## Earlier findings" in text) == (round > 1)
    assert ("git diff fix789 head456" in text) == (round > 1)
    assert not any(line.startswith("@") for line in text.splitlines())


def test_review_without_report_or_outside_files(task: Task) -> None:
    text = briefs.review_brief(
        FLOW,
        cast("Plan", SimpleNamespace(context="", failure_model="", read=())),
        task,
        round=1,
        base="base",
        head="head",
        changed=[],
        outside=[],
        wave_files=[],
        writer_report=None,
    )
    assert "The writer left no report." in text
    assert "Changed outside the declared files:\n    none" in text
    assert "In outside_files, give exactly one entry" not in text
    assert "Check, in any file, changes to behaviour or user-facing wording" in text
    assert "## Context" not in text and "## Failure model" not in text
    assert "## Merged sibling tasks" not in text
    assert "## Lead corrections" not in text
    assert "which another task in this wave declares" not in text


def test_review_names_files_another_task_declares(task: Task) -> None:
    text = briefs.review_brief(
        FLOW,
        cast("Plan", SimpleNamespace(context="", failure_model="", read=())),
        task,
        round=1,
        base="base",
        head="head",
        changed=["src/a.py", "src/b.py", "src/c.py"],
        outside=["src/b.py", "src/c.py"],
        wave_files=["src/b.py", "src/c.py"],
        writer_report=None,
        shared=[("src/b.py", "b"), ("src/c.py", "c")],
    )
    assert (
        "The writer changed these files, which another task in this wave "
        "declares:\n\n    src/b.py (task b)\n    src/c.py (task c)\n\n"
        "Judge each as an outside edit. Raise one the task did not need as a "
        "must or should finding under the review bar. One the task needed, or "
        "the task text allows, is not a lead finding for being in another "
        "task's files."
    ) in text
    assert text.index("In outside_files, give") < text.index("src/b.py (task b)")


@pytest.mark.parametrize("round", [1, 2])
def test_review_corrections_and_recorded_outside_files(
    plan: Plan, task: Task, round: int
) -> None:
    """Reviews carry numbered decisions and outside files without a writer report."""
    first = "Allow src/helper.py.\n@decision\n" + "x" * (briefs.REPORT_LIMIT + 1)
    second = "Keep the chosen interface.\nThe empty case is settled."
    text = briefs.review_brief(
        FLOW,
        plan,
        task,
        round=round,
        base="base",
        head="head",
        changed=["src/a.py", "src/helper.py"],
        outside=["src/helper.py"],
        wave_files=[],
        writer_report=None,
        corrections=[(2, first), (5, second)],
    )
    assert "settled decisions from the lead, in answer order" in text
    assert "Do not reopen them" in text
    assert "Any file a correction names counts as in scope" in text
    assert text.index("### Answer 2") < text.index("### Answer 5")
    for correction in (first, second):
        assert "    " + correction.replace("\n", "\n    ") in text
    assert "Changed outside the declared files:\n    src/helper.py" in text
    assert "The writer left no report." in text
    assert not any(line.startswith("@") for line in text.splitlines())


def test_review_lists_merged_sibling_subjects_and_commits(
    plan: Plan, task: Task
) -> None:
    text = briefs.review_brief(
        FLOW,
        plan,
        task,
        round=1,
        base="base",
        head="head",
        changed=[],
        outside=[],
        wave_files=[],
        writer_report=None,
        merged_siblings=[
            ("page", "feat: add page\n@subject", "a" * 40),
            ("navigation", "feat: add navigation", "b" * 40),
        ],
    )
    assert "## Merged sibling tasks" in text
    assert (
        "These sibling tasks' changes are on the flow branch and absent "
        "from the task branch:"
    ) in text
    assert "    page: feat: add page\n    @subject\n    commit: " + "a" * 40 in text
    assert "    navigation: feat: add navigation\n    commit: " + "b" * 40 in text
    assert not any(line.startswith("@") for line in text.splitlines())


def test_fix_filters_and_numbers_must_findings(task: Task) -> None:
    general = verdict.Finding(
        "must", None, None, "general problem", "repair", "harm", None, ""
    )
    text = briefs.fix_message(
        FLOW, task, 1, [_finding("should"), _finding(), general, _finding("lead")]
    )
    assert text.splitlines()[0] == briefs.FIX_FIRST.format(
        task=task.id, round=1, flow=FLOW
    )
    assert "1. src/a.py" in text and "2. general: general problem" in text
    assert "3." not in text
    assert 'explain under "Disputed"' in text
    assert "    scenario: scenario\n    @scenario" in text
    assert "    fix: fix\n    @fix" in text
    assert "## Git" in text and "## Closing report" in text
    assert not any(line.startswith("@") for line in text.splitlines())


def test_send_preserves_intentional_commands(task: Task) -> None:
    text = "Correct this\n@write a.py fixed\n"
    rendered = briefs.send_message(FLOW, task, text)
    assert rendered.startswith(
        f"Correction from the lead for task parser in flow {FLOW}.\n\n" + text
    )
    assert rendered.count(text) == 1


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n"])
def test_fix_indents_continuations(task: Task, separator: str) -> None:
    finding = verdict.Finding(
        "must",
        f"a.py{separator}@file",
        1,
        f"problem{separator}@problem",
        f"fix{separator}@fix",
        f"scenario{separator}@scenario",
        None,
        "",
    )
    text = briefs.fix_message(FLOW, task, 1, [finding])
    assert not any(line.startswith("@") for line in text.splitlines())


def test_briefs_with_parsed_plan(tmp_path: Path) -> None:
    plan_module = pytest.importorskip(
        "ember.flows.plan", reason="the plan module is supplied by task A"
    )
    plan = plan_module.parse(
        json.dumps(
            {
                "dir": str(tmp_path),
                "context": "Use UTF-8",
                "failure_model": "Empty input",
                "tasks": [
                    {
                        "id": "a",
                        "subject": "feat: parse input",
                        "task": "Read input",
                        "files": ["src/a.py"],
                        "review": "Check empty input",
                    }
                ],
            }
        ),
        tmp_path / "plan.json",
    )
    task = plan.task("a")
    assert briefs.writer_brief(FLOW, plan, task, []).startswith(task.subject + "\n")
    review = briefs.review_brief(
        FLOW,
        plan,
        task,
        round=1,
        base="base",
        head="head",
        changed=["src/a.py"],
        outside=[],
        wave_files=[],
        writer_report="Checked empty input",
    )
    assert "    Read input" in review and "    Check empty input" in review
    assert briefs.fix_message(FLOW, task, 1, [_finding()]).startswith("Fix round 1")
    assert "\n\nCorrection\n\n## Checks" in briefs.send_message(
        FLOW, task, "Correction"
    )


def test_writer_and_reviewer_list_the_read_files(tmp_path: Path, task: Task) -> None:
    issue = tmp_path / "issue-93.md"
    issue.write_text("title: x\n")
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "spec.md").write_text("spec\n")
    with_sources = cast(
        "Plan",
        SimpleNamespace(
            context="",
            failure_model="",
            read=(str(issue), str(folder), str(tmp_path / "missing")),
            check=(),
        ),
    )
    writer = briefs.writer_brief(FLOW, with_sources, task, [])
    review = briefs.review_brief(
        FLOW,
        with_sources,
        task,
        round=1,
        base="a" * 40,
        head="b" * 40,
        changed=["src/a.py"],
        outside=[],
        wave_files=[],
        writer_report=None,
    )
    for text in (writer, review):
        assert "## Sources to read" in text
        assert f"- {issue}" in text and f"- {folder / 'spec.md'}" in text
        assert str(tmp_path / "missing") not in text
    plain = briefs.writer_brief(
        FLOW,
        cast("Plan", SimpleNamespace(context="", failure_model="", read=(), check=())),
        task,
        [],
    )
    assert "## Sources to read" not in plain
