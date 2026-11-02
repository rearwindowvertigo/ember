"""The exit codes of `emb run` and the next step printed with each.

Each non-zero exit prints its message on standard error, then one line that
starts `next: ` (a command Claude runs) or `owner: ` (something only the
owner does). A line names its placeholders in braces; a placeholder with no
value prints as `<name>`.
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
NO_REPLY = 80
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
        "engine_off": "owner: switch {engine} on: emb choices --set engine.{engine}=on",
        "subagent": "next: run this task as a Claude subagent inside Claude Code",
        "claim": "next: emb choices --claim --id <id>, then pass that id to --claim",
        "no_run": "next: emb run --status",
    },
    NOT_A_CHECKOUT: "next: " + START + " (--dir inside a git checkout)",
    NO_INPUT: "next: " + START + " (--dir and every file and path must exist)",
    UNAVAILABLE: {
        "*": "owner: install or connect {engine}; emb engines shows what is missing",
        "fix": "owner: {fix}",
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
    PROVIDER_ERROR: "next: " + SEND + " (after the provider's limit or outage clears)",
    INTERRUPTED: {
        "*": RECOVER + " (the turn keeps running in its session)",
        "starting": "next: emb run --close {run}, then " + START,
        "send": "next: " + SEND + " (the message was not sent)",
        "complete": RECOVER + " (finishes the saved turn)",
    },
    REFUSED: {
        "*": "next: emb run --status {run} (another emb run command is waiting on it)",
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
    NO_REPLY: "next: emb run --screen {run}, then " + SEND,
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
