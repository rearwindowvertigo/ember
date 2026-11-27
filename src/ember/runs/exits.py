"""The exit codes of `emb run` and the next step printed with each.

Each non-zero exit prints its message on standard error, then one line that
starts `next: ` (an action for the lead) or `owner: ` (something only the
owner does). A line names its placeholders in braces; a placeholder with no
value prints as `<name>`.

Exit 84 is the command's exit for a pending owner wait after an otherwise
successful start, send or recovery. Its output is the run's pending owner
lines; the recorded turn exit stays unchanged.
"""

import string
import sys
from collections.abc import Mapping
from typing import TextIO

OK = 0
UNEXPECTED = 1
USAGE = 64
NOT_A_CHECKOUT = 65
NO_INPUT = 66
UNAVAILABLE = 69
WORKTREE_FAILED = 70
ENGINE_FAILED = 71
SESSION_GONE = 72
PROVIDER_ERROR = 73
INTERRUPTED = 74
REFUSED = 75
MOVED = 76
HOOK_REFUSED = 77
PREPARATION_FAILED = 79
NO_REPLY = 80
OWNER_WAIT = 84
STALLED = 124

START = "emb run --engine <engine> --dir <git checkout> --task-file <file>"
SEND = "emb run --send {run} --message-file <file>"
RECOVER = "next: emb run --recover {run}"

LINES: dict[int, str | Mapping[str, str]] = {
    UNEXPECTED: {
        "*": "next: emb run --status",
        "database": "owner: repair or remove the settings database the message names",
    },
    USAGE: {
        "*": "next: emb run --help",
        "menu": "owner: set {row} with emb choices --set; "
        "or the lead passes --engine and --model",
        "auto": "next: choose a model for {row} and pass --engine and --model",
        "engine_off": "owner: switch {engine} on: emb choices --set engine.{engine}=on",
        "subagent": "next: use a subagent in Claude Code if available; "
        "otherwise ask the owner before choosing a CLI worker",
        "claim": "next: emb choices --claim --id <id>, then pass that id to --claim",
        "no_run": "next: emb run --status",
    },
    NOT_A_CHECKOUT: "next: " + START + " (--dir inside a git checkout)",
    NO_INPUT: "next: " + START + " (--dir and every file and path must exist)",
    UNAVAILABLE: {
        "*": "owner: install or connect {engine}; emb engines shows what is missing",
        "fix": "owner: {fix}",
        "trust": "owner: run codex in {dir} once and choose to trust the folder",
        "sandbox": "owner: remove the sandbox_mode key from {files}, "
        "or set sandbox.codex to off",
        "bwrap": "owner: install bubblewrap (the bwrap command) or make it work, "
        "or set sandbox.{engine} to off",
        "tmux": "owner: install tmux",
        "git": "owner: install git",
        "driver": "owner: this Ember build has no driver for {engine}; update Ember",
        "runtime": "owner: set XDG_RUNTIME_DIR to a private folder of this user "
        "(the message names the problem)",
    },
    WORKTREE_FAILED: "next: git -C {dir} worktree prune, then " + START,
    ENGINE_FAILED: {
        "*": "next: emb run --status {run}",
        "starting": "next: emb run --close {run}, then " + START,
        "send": "next: " + SEND + " (the message was not sent)",
    },
    SESSION_GONE: {
        "*": "next: emb run --status {run}",
        "send": "next: " + START + " (a new run; this run's session is gone)",
    },
    PROVIDER_ERROR: {
        "*": "next: " + SEND + " (after the provider's outage clears)",
        "starting": "next: emb run --close {run}, then "
        + START
        + " (after the provider's outage clears)",
        "fallback": "next: emb run --close {run}, then {fallback}",
        "ended": "next: emb run --close {run}, then "
        + START
        + " (a new run after the provider's outage clears; "
        "the engine of this run ended)",
        "capacity": "next: " + SEND + " (after provider capacity is available)",
        "usage_limit": "next: " + SEND + " (after the provider usage_limit resets)",
        "uncertain": "next: inspect the session of {run}; retry delivery is uncertain; "
        "ask the lead before sending again (capacity)",
    },
    INTERRUPTED: {
        "*": RECOVER + " (the turn keeps running in its session)",
        "starting": "next: emb run --close {run}, then " + START,
        "send": "next: " + SEND + " (the message was not sent)",
        "uncertain": "next: inspect the session of {run}; retry delivery is uncertain; "
        "ask the lead before sending again (capacity)",
        "retry": RECOVER + " (capacity resend cancelled; collects the saved turn)",
        "complete": RECOVER + " (finishes the saved turn)",
    },
    REFUSED: {
        "*": "next: emb run --status {run} (another emb run command is waiting on it)",
        "announcement": "next: report that no worker started; use --dry-run --announce "
        "in the lead's conversation before starting",
        "routing": "next: read Team's current choices; choose an eligible worker "
        "or ask the owner",
        "busy": RECOVER + " (waits for the turn, then collects it)",
        "close": RECOVER + ", then emb run --close {run}; "
        "or emb run --close {run} --force",
        "collected": RECOVER + " (commits the collected turn), "
        "then emb run --close {run}",
        "foreign": "next: ask the owner about the messages in the session of {run}; "
        "with their go-ahead, repeat the command with --force",
    },
    MOVED: "next: git -C {worktree} log --oneline -3 "
    "(the branch or HEAD moved; nothing was committed)",
    HOOK_REFUSED: {
        "*": "next: " + SEND + " (git commit was refused; sends the hook output "
        "with the correction; the work is uncommitted)",
        "add": RECOVER + " (git add failed; repair it before recovery)",
    },
    PREPARATION_FAILED: "next: inspect {log}, then {retry}",
    NO_REPLY: "next: emb run --screen {run}, then " + SEND,
    OWNER_WAIT: "owner: answer the pending owner actions for {run}",
    STALLED: RECOVER + " (waits for the turn still running in its session, "
    "then collects it)",
}
"""Each exit's line, or a mapping from a variant to its line with `*` as the
default."""


class _Blanks(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return f"<{key}>"


def line(code: int, variant: str | None = None, **context: object) -> str | None:
    """Return the `next:` or `owner:` line for an exit.

    Args:
        code: The exit code.
        variant: Which of the exit's lines; `None` or an unknown variant
            gives the default.
        **context: Values for the placeholders; `None` and empty values
            print as `<name>`.

    Returns:
        The line, or `None` for exit 0. An exit missing from the table gives
        the line of exit 1.
    """
    if not code:
        return None
    entry = LINES.get(code, LINES[UNEXPECTED])
    if not isinstance(entry, str):
        entry = entry.get(variant or "*") or entry["*"]
    values = _Blanks(
        {key: str(value) for key, value in context.items() if value not in (None, "")}
    )
    return string.Formatter().vformat(entry, (), values)


def emit(
    code: int,
    variant: str | None = None,
    *,
    stream: TextIO | None = None,
    **context: object,
) -> str | None:
    """Print an exit's line on standard error.

    Args:
        code: The exit code.
        variant: Which of the exit's lines.
        stream: Where to print; `None` for standard error.
        **context: Values for the placeholders.

    Returns:
        The line printed, or `None` for exit 0.
    """
    text = line(code, variant, **context)
    if text:
        print(text, file=stream or sys.stderr)
    return text


def report(code: int, variant: str | None = None, **context: object) -> str | None:
    """Return the settled report line for a run exit or merge conflict.

    Args:
        code: The exit code.
        variant: The recorded exit variant.
        **context: Placeholder values, plus turn and read_only when known.

    Returns:
        The report line, or None when this exit carries no report.
    """
    text = None
    if code == UNAVAILABLE and (
        context.get("turn", 0) == 0 or context.get("last_start_failure", False)
    ):
        text = (
            "no worker started; do not retry the start or start another engine "
            "before the owner answers"
        )
    elif code in (PROVIDER_ERROR, INTERRUPTED) and variant == "uncertain":
        text = (
            "retry delivery is uncertain; do not send again before inspecting the "
            "session; then offer only a retry with the same model and effort; offer "
            "no other model, engine, subagent or work by the lead"
        )
    elif code == PROVIDER_ERROR:
        text = {
            "capacity": "capacity; retries exhausted; offer only a retry with the "
            "same model and effort once capacity returns; offer no other model, "
            "engine, subagent or work by the lead",
            "outage": "outage; offer only a retry with the same model and effort "
            "once it clears; offer no other model, engine, subagent or work "
            "by the lead",
            "usage_limit": "usage_limit; a retry on the same worker needs no "
            "question; another worker or model needs the owner's answer; "
            "OpenCode may be offered",
            "fallback": "fallback; the owner's fallback list names the worker "
            "of the next: line; run that line without a question; tell the "
            "owner which model failed, with which error, and which model runs "
            "now",
            **dict.fromkeys(
                ("starting", "ended"),
                "outage; the engine ended; offer only a new run with the same "
                "model and effort once it clears; offer no other model, engine, "
                "subagent or work by the lead",
            ),
        }.get(variant or "outage")
    elif (
        code == INTERRUPTED
        and variant in (None, "*", "retry", "complete")
        and not context.get("read_only", False)
    ):
        text = "interrupted; partial edits may be committed on {branch}"
    elif code == REFUSED and variant == "conflict":
        text = (
            "merge conflict; do not retry or force it; ask the owner: a new run "
            "cut from the updated target, or the owner resolves it"
        )
    elif code == MOVED:
        text = (
            "branch or HEAD moved; nothing committed; recover only when the "
            "branch and HEAD are as recorded, otherwise leave the work to the owner"
        )
    elif code == HOOK_REFUSED and variant in (None, "*"):
        text = "the commit hook refused; the work is uncommitted in {worktree}"
    elif code == NO_REPLY:
        text = "the turn ended with no reply"
    if text is None:
        return None
    values = _Blanks(
        {key: str(value) for key, value in context.items() if value not in (None, "")}
    )
    return "report: " + string.Formatter().vformat(text, (), values)
