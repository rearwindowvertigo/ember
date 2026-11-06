"""Build writer briefs, read-only review briefs and correction messages."""

from collections.abc import Sequence
from textwrap import indent
from typing import TYPE_CHECKING

from ember import citations
from ember.flows import verdict
from ember.flows.verdict import Finding

if TYPE_CHECKING:
    from ember.flows.plan import Plan, Task  # pyrefly: ignore[missing-import]

REVIEW_BAR = (
    "Review the work against its task and its goals, as a careful second engineer "
    "would: is it correct for the cases the task and its failure model cover, is "
    "anything it needs missing, is there a clearly simpler way, and is anything "
    "important overlooked. Where there is no failure model, judge by the task "
    "text. Do not search every combination of inputs and timing; report a case "
    "only when it matters by the rules below.\n\n"
    "Give each finding a severity:\n"
    "- must: blocks the work. Write a scenario: a concrete case that can happen "
    "in normal use, or any case that can lose work or data, and the harm it "
    "causes, such as a wrong result, lost work, a crash, a broken caller or test, "
    "a security hole, or a contradiction of what was decided. A must without "
    "a scenario is read as should.\n"
    "- should: an improvement, or a case outside normal use. It does not block; "
    "the lead reads it.\n"
    "- lead: something only the lead or the owner can settle: behaviour the "
    "task and the decisions given do not define, or a change outside what the "
    "writer may touch. Name the decision in the problem. It does not go to "
    "the writer.\n\n"
    "Do not ask for behaviour the failure model or the decisions given already "
    "settle."
)
REPORT_LIMIT = 20000
REVIEW_FIRST = (
    "Review of task {task}, round {round}, in flow {flow}: "
    "a read-only review; change no file."
)
FIX_FIRST = "Fix round {round} of task {task} in flow {flow}."

_GIT = (
    "## Git\n\nYou may run git commands that only read, such as `git log`, "
    "`git show`, `git diff`, `git status` and `git blame`. Run no git command "
    "that changes the index, a tracked file's state, a branch or a ref: Ember "
    "commits your edits on its run branch at the end of each turn."
)
_CLOSING = (
    "## Closing report\n\nState what you changed and how you checked it. "
    'List "Outside files", "Not done" and "Not covered".'
)
_LIMITS = (
    "Work only inside your working folder. If the task needs a change to a "
    "file outside your list that no other task in this wave changes, make it, "
    'check the code that uses it, and list it under "Outside files" in your '
    "final message with one line on why. Never change a file another task in "
    'this wave changes; if the task needs that, list it under "Not done".'
)


def _files(files: Sequence[str], wave_files: Sequence[str]) -> str:
    """List this task's files followed by the other tasks' files."""
    other = (
        "Other tasks in this wave change these files; do not change them:\n"
        + "\n".join(wave_files)
        if wave_files
        else "No other task runs in this wave."
    )
    return "\n".join(files) + "\n\n" + other


def writer_brief(
    flow_id: str,
    plan: Plan,
    task: Task,
    wave_files: Sequence[str],
    standing: Sequence[str] = (),
) -> str:
    """Build an editing brief, keeping the task text and commit subject intact.

    `standing` holds the `sandbox.read` paths, listed as sources before the
    plan's `read`.
    """
    parts = [task.subject, task.task, "## Files\n\n" + _files(task.files, wave_files)]
    sources = citations.sources_section(citations.source_files([*standing, *plan.read]))
    if sources:
        parts.append(sources)
    if plan.context.strip():
        parts.append("## Context\n\n" + plan.context)
    if plan.failure_model.strip():
        parts.append("## Failure model\n\n" + plan.failure_model)
    parts.extend(
        [
            "## Limits\n\n" + _LIMITS,
            _GIT,
            "## Checks\n\nRun the tests for the files you change "
            "and quick static checks.",
            _CLOSING,
        ]
    )
    return "\n\n".join(parts) + "\n"


def _must_list(findings: Sequence[Finding]) -> str:
    """Number must findings and indent every continuation of pasted text."""
    parts = []
    must = [item for item in findings if item.severity == "must"]
    for number, item in enumerate(must, 1):
        location = item.file or "general"
        if item.line is not None:
            location += f":{item.line}"
        problem = indent(f"{location}: {item.problem}", "    ").removeprefix("    ")
        parts.append(
            f"{number}. {problem}\n"
            + indent(f"scenario: {item.scenario}\nfix: {item.fix}", "    ")
        )
    return "\n".join(parts)


def review_brief(
    flow_id: str,
    plan: Plan,
    task: Task,
    *,
    round: int,
    base: str,
    head: str,
    changed: Sequence[str],
    outside: Sequence[str],
    wave_files: Sequence[str],
    writer_report: str | None,
    earlier: Sequence[Finding] = (),
    fix_base: str | None = None,
    standing: Sequence[str] = (),
    merged_siblings: Sequence[tuple[str, str, str]] = (),
    corrections: Sequence[tuple[int, str]] = (),
) -> str:
    """Build a review brief with indented evidence and the packaged reply schema.

    `standing` holds the `sandbox.read` paths, listed as sources before the
    plan's `read`.
    `merged_siblings` holds task ids, subjects and merge commits absent from
    the reviewed head.
    `corrections` holds answer numbers and the lead's correction texts in order.
    """
    parts = [
        REVIEW_FIRST.format(task=task.id, round=round, flow=flow_id),
        f"You review task {task.id} of a flow. The writer's work is in your "
        "working folder, on its branch.",
        "## Task\n\n" + indent(task.subject + "\n\n" + task.task, "    "),
        "## Files\n\n" + indent(_files(task.files, wave_files), "    "),
    ]
    if corrections:
        parts.append(
            "## Lead corrections\n\n"
            "These are settled decisions from the lead, in answer order. "
            "Do not reopen them. Any file a correction names counts as in scope "
            "for this review, even if listed below as outside the declared files.\n\n"
            + "\n\n".join(
                f"### Answer {number}\n\n" + indent(text, "    ")
                for number, text in corrections
            )
        )
    if merged_siblings:
        parts.append(
            "## Merged sibling tasks\n\n"
            "These sibling tasks' changes are on the flow branch and absent "
            "from the task branch:\n\n"
            + "\n".join(
                indent(f"{task_id}: {subject}\ncommit: {commit}", "    ")
                for task_id, subject, commit in merged_siblings
            )
        )
    sources = citations.sources_section(citations.source_files([*standing, *plan.read]))
    if sources:
        parts.append(sources)
    for heading, text in (
        ("Review focus", task.review),
        ("Context", plan.context),
        ("Failure model", plan.failure_model),
    ):
        if text.strip():
            parts.append(f"## {heading}\n\n" + indent(text, "    "))
    parts.append(
        f"## The change\n\nRead it with `git diff {base} {head}` and "
        f"`git log --oneline {base}..{head}`.\n\n"
        + indent("\n".join(changed) or "none", "    ")
        + "\n\nChanged outside the declared files:\n"
        + indent("\n".join(outside) or "none", "    ")
    )
    parts.append(
        "## The writer's report\n\n"
        + (
            indent(writer_report[:REPORT_LIMIT], "    ")
            if writer_report
            else "The writer left no report."
        )
    )
    if round > 1:
        parts.append(
            "## Earlier findings\n\n"
            + indent(_must_list(earlier) or "none", "    ")
            + "\n\nCheck only whether those findings are fixed and the fix diff "
            f"`git diff {fix_base or base} {head}`; a new must or lead finding "
            "must be caused by the fix."
        )
    parts.extend(
        [
            "## Review bar\n\n" + REVIEW_BAR + "\n\n"
            "For each must, describe the underlying cause and the other places "
            "it affects in problem. Use the existing text fields; do not add fields.",
            "## Reply\n\nChange, stage, commit or stash nothing. Reply with only "
            "one JSON object matching this JSON Schema, with no other text and "
            "no code fence:\n\n"
            + verdict.schema_text()
            + "\nThe verdict is fix when any finding is must or lead.",
        ]
    )
    return "\n\n".join(parts) + "\n"


def fix_message(
    flow_id: str, task: Task, round: int, findings: Sequence[Finding]
) -> str:
    """Send the numbered must findings to the existing writer."""
    return (
        "\n\n".join(
            [
                FIX_FIRST.format(task=task.id, round=round, flow=flow_id),
                "The review found these must findings. Fix each one, or, if you "
                'dispute one, change nothing for it and explain under "Disputed" '
                "in your final message.",
                _must_list(findings),
                _GIT,
                _CLOSING,
            ]
        )
        + "\n"
    )


def send_message(flow_id: str, task: Task, text: str) -> str:
    """Prefix a lead correction while preserving its text as written."""
    return f"Correction from the lead for task {task.id} in flow {flow_id}.\n\n{text}"
