"""The `emb` command.

Exit codes follow BSD `sysexits.h`. For `emb choices` and `emb engines`: 0 on
success, 64 for a usage error or an invalid value, 74 when the settings
database cannot be opened or read, 75 for a stale generation or a busy
database, 77 for an owner option without a terminal and 78 for an unsupported
database schema. `emb run` uses the table in `ember.runs.exits`; `emb merge`
and `emb cleanup` use the codes in `ember.branches`. `emb ground` exits 0 once
it has printed, 64 for a usage error and 66 when `--dir` does not exist or is
not a folder, or, without `--dir`, when the current folder cannot be read.
`emb deps` exits 0 whatever the statuses, 64 for a usage error and 70 when
the packaged component list cannot be read or is not valid. `emb app` exits 0
after Quit Ember or when the running app takes over, 64 for a usage error and
69 without a graphical session. `emb skill` uses the codes in `ember.skill`.
`emb gapcheck` uses the codes in `ember.gapcheck.command`.
`emb flow` uses the codes in `ember.flows.command`.
`emb panel` uses the codes in `ember.panels.command`.
`emb prune` exits 0 on success, 1 on a failure and 64 for invalid arguments.
`emb benchmarks` exits 0 after a read or a refresh with any available source,
64 for invalid settings or arguments, 69 when every source failed a refresh,
and uses the settings database codes for local read, write and busy errors.

With `--json`, a command that exits non-zero having printed nothing on
standard output prints one error object there; see `_json_error`.
"""

import argparse
import contextlib
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn, TextIO, cast

from ember import (
    __version__,
    benchmarks,
    branches,
    choices,
    db,
    deps,
    ground,
    holds,
    skill,
)
from ember.engines import models, status
from ember.runs import core as run_core
from ember.runs import exits, record, steps

EX_OK = 0
EX_USAGE = 64
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_NOPERM = 77


class ArgumentParser(argparse.ArgumentParser):
    """An argument parser whose usage errors exit 64."""

    def error(self, message: str) -> NoReturn:
        """Print the usage, `message` and a `next:` line to standard error, exit 64.

        Args:
            message: What is wrong with the arguments.
        """
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"{self.prog}: {message}\nnext: {self.prog} --help\n")


class UsageError(Exception):
    """The arguments are not a valid combination."""


def _add_choices(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="print JSON (every reply is JSON)"
    )
    parser.add_argument("--id", metavar="ID", help="the run's claim id")
    parser.add_argument(
        "--claim", action="store_true", help="claim the pending one-time choice"
    )
    parser.add_argument(
        "--skill",
        metavar="X.Y.Z",
        help="compare the loaded skill version with the installed skill, with --claim",
    )
    parser.add_argument(
        "--skill-dir",
        metavar="DIR",
        type=Path,
        help="check the skill in this absolute directory instead of the default link",
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help="with a read or --claim, keep only the engine, shortlist and picker "
        "answers and the single, multi-task and panel menu rows",
    )
    parser.add_argument("--finish", metavar="ID", help="release the claim for ID")
    parser.add_argument(
        "--request",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a request-layer value, with a read or --claim",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="save a standing setting",
    )
    parser.add_argument(
        "--clear",
        action="append",
        default=[],
        metavar="KEY",
        help="remove a standing setting",
    )
    parser.add_argument(
        "--once",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="set the one-time choice for the next run",
    )
    parser.add_argument(
        "--clear-once", action="store_true", help="remove the pending one-time choice"
    )
    parser.add_argument(
        "--expect-generation",
        type=int,
        metavar="N",
        help="refuse the write unless the generation is N",
    )


def _add_engines(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="check the engines, or read the model list, instead of the cache",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument(
        "--models",
        choices=status.ENGINES,
        metavar="ENGINE",
        help="print ENGINE's model list (codex, opencode or claude)",
    )


def _add_benchmarks(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(
        dest="benchmark_action", metavar="<action>", required=True
    )
    for action, help_text in (
        ("refresh", "download and save the supported public benchmark results"),
        ("status", "show saved benchmark evidence for the routing pool, offline"),
    ):
        subparser = actions.add_parser(action, help=help_text, description=help_text)
        subparser.add_argument("--json", action="store_true", help="print JSON")


def _positive_minutes(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive number")
    return value


_RUN_MODES = (
    "send",
    "screen",
    "interrupt",
    "attach",
    "close",
    "recover",
    "status",
    "answered",
)
_ALL_RUNS = ""


def _add_run(parser: argparse.ArgumentParser) -> None:
    start = parser.add_argument_group("start a run")
    start.add_argument(
        "--engine",
        choices=status.ENGINES,
        metavar="ENGINE",
        help="codex, opencode or claude",
    )
    start.add_argument("--dir", metavar="DIR", help="a folder in the git checkout")
    start.add_argument("--task-file", metavar="FILE", help="the brief")
    start.add_argument(
        "--dry-run", action="store_true", help="check inputs and print the worker"
    )
    start.add_argument("--model", metavar="MODEL", help="the model (default: menu)")
    start.add_argument(
        "--effort", metavar="EFFORT", help="the effort (default: menu or engine)"
    )
    start.add_argument(
        "--read-only", action="store_true", help="work in the checkout, edit nothing"
    )
    start.add_argument(
        "--read",
        action="append",
        default=[],
        metavar="PATH",
        help="a path the run may read; repeatable",
    )
    start.add_argument(
        "--trailer",
        action="append",
        default=[],
        metavar="LINE",
        help="a trailer line for each commit; repeatable",
    )
    start.add_argument("--claim", metavar="ID", help="read the menu with this claim id")
    start.add_argument(
        "--fallback-from",
        metavar="RUN",
        help="the run that a provider error ended and that this start replaces",
    )
    start.add_argument(
        "--continue-run",
        metavar="RUN",
        help="start from the branch of this earlier editing run and tell the "
        "worker about its work",
    )
    start.add_argument(
        "--window",
        action="store_true",
        help="also open a terminal window on the session",
    )
    existing = parser.add_argument_group("work with a run")
    existing.add_argument(
        "--send", metavar="RUN", help="send a message and wait for its turn"
    )
    existing.add_argument("--message-file", metavar="FILE", help="the message")
    existing.add_argument("--screen", metavar="RUN", help="print the session's screen")
    existing.add_argument(
        "--interrupt", metavar="RUN", help="interrupt the current turn"
    )
    existing.add_argument(
        "--attach", metavar="RUN", help="open a new window on the session"
    )
    existing.add_argument(
        "--close", metavar="RUN", help="end the session; keep worktree and branch"
    )
    existing.add_argument(
        "--force",
        action="store_true",
        help="with --close: close even while busy or holding messages Ember did "
        "not send; with --send: send even to a session holding such messages",
    )
    existing.add_argument(
        "--recover", metavar="RUN", help="collect a turn that ended unwatched"
    )
    existing.add_argument(
        "--answered",
        metavar="RUN",
        help="record the owner's reply, end the owner wait and print the next step",
    )
    existing.add_argument(
        "--status",
        nargs="?",
        const=_ALL_RUNS,
        metavar="RUN",
        help="print a run's record, or every run's",
    )
    output = parser.add_argument_group("waiting and output")
    output.add_argument(
        "--json", action="store_true", help="with --status or --dry-run: JSON"
    )
    output.add_argument(
        "--quiet", action="store_true", help="print only the run id, worker and summary"
    )
    output.add_argument(
        "--stall",
        type=_positive_minutes,
        default=run_core.STALL_MINUTES,
        metavar="MINUTES",
        help="minutes without a screen change before exit 124 (default 10)",
    )


def _add_gapcheck(parser: argparse.ArgumentParser) -> None:
    """Add plan review, brief, reply and answer options."""
    parser.add_argument("--plan", metavar="FILE", help="the plan")
    parser.add_argument("--dir", metavar="DIR", help="checkout folder (default: .)")
    parser.add_argument("--engine", choices=status.ENGINES, help="reviewer engine")
    parser.add_argument("--model", metavar="MODEL", help="reviewer model")
    parser.add_argument("--effort", metavar="EFFORT", help="reviewer effort")
    parser.add_argument("--claim", metavar="ID", help="the existing claim id")
    parser.add_argument(
        "--read",
        action="append",
        default=[],
        metavar="PATH",
        help="an additional readable path; repeatable",
    )
    parser.add_argument("--window", action="store_true", help="open a run window")
    parser.add_argument(
        "--stall",
        type=_positive_minutes,
        metavar="MINUTES",
        help="minutes without a screen change before exit 124 (default 10)",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON result")
    parser.add_argument("--brief", action="store_true", help="print the brief only")
    parser.add_argument("--reply", metavar="FILE", help="read an existing reply")
    parser.add_argument(
        "--brief-file", metavar="FILE", help="with --reply: the reviewer's saved brief"
    )
    parser.add_argument(
        "--previous",
        metavar="FILE",
        help="an earlier --json result whose active and settled gaps are reviewed",
    )
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="run the check although the gap check settings skip or ask",
    )
    parser.add_argument("--answer", metavar="FILE", help="record an answer in a result")
    parser.add_argument("--id", metavar="GAP", help="the gap id to answer")
    parser.add_argument("--text", metavar="ANSWER", help="the answer to record")
    parser.add_argument(
        "--take-defaults",
        action="append",
        default=[],
        metavar="ID[,ID...]",
        help="with --answer: record each listed gap's default; repeatable",
    )


def _add_flow(parser: argparse.ArgumentParser) -> None:
    """Add flow modes, worker selections and lead answers."""
    parser.add_argument("--plan", metavar="FILE", help="the JSON task plan")
    parser.add_argument(
        "--dry-run", action="store_true", help="print workers and waves"
    )
    parser.add_argument(
        "--previous",
        metavar="FILE",
        help="an earlier gap check result to compare on --dry-run",
    )
    parser.add_argument(
        "--gapcheck-use-team",
        action="store_true",
        help="with --dry-run, keep the Team gap check reviewer with --reviewer",
    )
    parser.add_argument("--start", action="store_true", help="start the flow runner")
    parser.add_argument(
        "--check-base",
        action="store_true",
        help="with --start, run the plan check on the base before wave 1",
    )
    parser.add_argument("--watch", metavar="FLOW", help="wait for a lead stop or end")
    parser.add_argument(
        "--status", nargs="?", const="", metavar="FLOW", help="show one or all flows"
    )
    parser.add_argument(
        "--lead",
        nargs="+",
        metavar=("FLOW", "TASK"),
        help="answer a task's lead stop; with --reviewer and no task, change "
        "the flow's saved reviewer",
    )
    parser.add_argument("--resume", metavar="FLOW", help="restart a flow's runner")
    parser.add_argument(
        "--check", metavar="JSON", help="with --resume, replace a failed check argv"
    )
    parser.add_argument("--cancel", metavar="FLOW", help="cancel a flow's runner")
    parser.add_argument(
        "--reason", metavar="TEXT", help="the cancellation or check replacement reason"
    )
    parser.add_argument("--writer", metavar="W", help="writer engine:model[@effort]")
    parser.add_argument(
        "--reviewer", metavar="W", help="reviewer engine:model[@effort]"
    )
    parser.add_argument("--claim", metavar="ID", help="read choices with this claim")
    parser.add_argument(
        "--stall",
        type=_positive_minutes,
        metavar="MINUTES",
        help="minutes without a screen change before a task stops (default 10)",
    )
    parser.add_argument(
        "--send", metavar="FILE", help="send a correction to the writer"
    )
    parser.add_argument("--accept", action="store_true", help="accept the task branch")
    parser.add_argument("--commit", metavar="SHA", help="the commit to accept")
    parser.add_argument("--review", action="store_true", help="start a fresh review")
    parser.add_argument("--worker", metavar="W", help="replace the writer with W")
    parser.add_argument("--drop", action="store_true", help="drop the task")
    parser.add_argument(
        "--withdraw", action="store_true", help="withdraw the task's queued answer"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_panel(parser: argparse.ArgumentParser) -> None:
    """Add panel start, validation, status, recovery, retry and close options."""
    parser.add_argument(
        "--question-file", metavar="FILE", help="the question as UTF-8 text"
    )
    parser.add_argument(
        "--dir", metavar="DIR", help="a checkout folder (default: current folder)"
    )
    parser.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="W",
        help="engine:model[@effort]; repeatable",
    )
    parser.add_argument("--claim", metavar="ID", help="read choices with this claim")
    parser.add_argument(
        "--read",
        action="append",
        default=[],
        metavar="PATH",
        help="an additional path members may read; repeatable",
    )
    parser.add_argument(
        "--stall",
        type=_positive_minutes,
        metavar="MINUTES",
        help="minutes without a screen change (default 10)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_minutes,
        metavar="MINUTES",
        help="panel deadline in minutes (default 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print resolved members",
    )
    parser.add_argument(
        "--status", nargs="?", const="", metavar="PANEL", help="show one or all panels"
    )
    parser.add_argument(
        "--recover",
        metavar="PANEL",
        help="collect unfinished members without asking again",
    )
    parser.add_argument(
        "--retry",
        nargs=2,
        metavar=("PANEL", "MEMBER"),
        help="ask a failed or not-started member again",
    )
    parser.add_argument(
        "--close", metavar="PANEL", help="close every open member run without force"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_ground(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir", metavar="DIR", help="the folder to read (default: the current one)"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_merge(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--subject",
        type=_one_line,
        metavar="TEXT",
        help="the squash commit subject; required for an Ember run branch",
    )
    parser.add_argument("branch", metavar="BRANCH", help="the reviewed branch")
    parser.add_argument(
        "--into", required=True, metavar="TARGET", help="the branch to merge into"
    )
    parser.add_argument(
        "--verdict", metavar="VERDICT", help="the lead's verdict; only keep merges"
    )
    parser.add_argument(
        "--note",
        required=True,
        type=_one_line,
        metavar="TEXT",
        help="the verdict line, printed first and kept in the landed commit",
    )
    parser.add_argument(
        "--dir", default=".", metavar="DIR", help="a folder in the git checkout"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _one_line(text: str) -> str:
    if not text.strip() or "\n" in text or "\r" in text:
        raise argparse.ArgumentTypeError(f"{text!r} is not one non-blank line")
    return text.strip()


def _whole_days(text: str) -> int:
    if not text.isascii() or not text.isdecimal():
        raise argparse.ArgumentTypeError(f"{text!r} is not a non-negative whole number")
    return int(text)


def _add_prune(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--claim", metavar="ID", help="only records with this exact claim id"
    )
    parser.add_argument(
        "--older-than",
        type=_whole_days,
        metavar="DAYS",
        help="remove records ended at least DAYS days ago (0 includes all ended)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list without removing records"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_cleanup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--merged-into",
        metavar="TARGET",
        help="the merge target, a local branch or a remote-tracking ref such as "
        "origin/main; defaults to each record's base branch with --claim",
    )
    parser.add_argument("--branch", metavar="BRANCH", help="only this branch")
    parser.add_argument(
        "--claim", metavar="ID", help="only records with this exact claim id"
    )
    parser.add_argument(
        "--dir", default=".", metavar="DIR", help="a folder in the git checkout"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be done; change nothing",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_deps(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="read newest versions now, even when the cached ones are recent",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_app(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="start in the tray, with the window hidden",
    )


def _add_skill(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(
        dest="skill_action", metavar="<action>", required=True
    )
    install = actions.add_parser(
        "install",
        help="link the skill into a lead harness's skills folder",
        description=(
            "Create the link to the skill source. A broken link is replaced; "
            "a link to another folder only with --force; a folder or file is "
            "never touched."
        ),
    )
    install.add_argument(
        "--force", action="store_true", help="replace a link to another folder"
    )
    uninstall = actions.add_parser(
        "uninstall",
        help="remove the skill's link",
        description=(
            "Remove the link when it points to an Ember skill or to nothing. "
            "A folder or file is never touched."
        ),
    )
    show = actions.add_parser(
        "status",
        help="print the link, its state, its target and the skill source",
        description="Print the link, its state, its target and the skill source.",
    )
    for action in (install, uninstall, show):
        action.add_argument(
            "--harness",
            choices=skill.HARNESSES,
            default="claude",
            help="which link to use; defaults to the Claude Code compatible link",
        )
        action.add_argument("--json", action="store_true", help="print JSON")


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for `emb` and its subcommands.

    Returns:
        The top-level parser.
    """
    parser = ArgumentParser(
        prog="emb",
        description="Coordinate AI coding workers on this machine.",
    )
    parser.add_argument("--version", action="version", version=f"emb {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    _add_choices(
        subparsers.add_parser(
            "choices",
            help="read settings and choices, claim or set them",
            description=(
                "Read settings and choices, claim or release a one-time choice, "
                "or set settings and one-time choices at a terminal."
            ),
        )
    )
    _add_engines(
        subparsers.add_parser(
            "engines",
            help="show whether each engine is connected, or its models",
            description=(
                "Show whether Codex, OpenCode and Claude Code are installed, "
                "signed in and connected, or list one engine's models."
            ),
        )
    )
    _add_benchmarks(
        subparsers.add_parser(
            "benchmarks",
            help="refresh or read benchmark evidence for the routing pool",
        )
    )
    _add_run(
        subparsers.add_parser(
            "run",
            help="start a worker run, or work with one",
            description=(
                "Start one worker in its own worktree and tmux session and wait "
                "for its turn, or send to, read, interrupt, close or recover a "
                "run. Each non-zero exit prints a next: or owner: line."
            ),
        )
    )
    _add_gapcheck(
        subparsers.add_parser(
            "gapcheck",
            help="review a plan for undefined or contradictory behaviour",
            description="Review a plan in one read-only run, print its brief, "
            "or read a reply from another worker.",
        )
    )
    _add_flow(
        subparsers.add_parser(
            "flow",
            help="plan, start, watch and answer flows",
            description=(
                "Run a JSON task plan in waves with writers, read-only reviews "
                "and fix rounds. Accepted tasks merge into a flow branch. "
                "Each non-zero exit prints a next: or owner: line."
            ),
        )
    )
    _add_panel(
        subparsers.add_parser(
            "panel",
            help="ask several models one question and collect their answers",
            description=(
                "Ask members the same question in parallel read-only runs "
                "and write a summary of their answers. "
                "Each non-zero exit prints a next: or owner: line."
            ),
        )
    )
    _add_ground(
        subparsers.add_parser(
            "ground",
            help="print what to read before choosing a worker",
            description=(
                "Print the machine and the folder's checkout, branch, dirty "
                "state, remote, instruction files and worktree preparation. "
                "Includes surface and worktree.baseRef metadata for Claude Code."
            ),
        )
    )
    _add_merge(
        subparsers.add_parser(
            "merge",
            help="merge a reviewed worker branch, then clean it up",
            description=(
                "Squash a reviewed Ember run branch under --subject, or merge another "
                "branch with a merge commit, "
                "in the worktree that has the target checked out, then remove the "
                "branch, its run's worktree and session as emb cleanup does. "
                "Never pushes. Each non-zero exit prints a next: or owner: line."
            ),
        )
    )
    _add_cleanup(
        subparsers.add_parser(
            "cleanup",
            help="remove merged worker branches, their worktrees and sessions",
            description=(
                "Remove each Ember run branch the target contains, its run's "
                "worktree and session, and keep everything else with a reason "
                "and the command that resolves it. Each non-zero exit prints a "
                "next: or owner: line."
            ),
        )
    )
    _add_prune(
        subparsers.add_parser(
            "prune",
            help="remove old ended run, flow and panel records",
            description="Remove ended records and their logs, and list kept branches "
            "and worktrees with removal commands.",
        )
    )
    _add_deps(
        subparsers.add_parser(
            "deps",
            help="show the installed and newest version of each dependency",
            description=(
                "Show each component Ember depends on: its installed version, "
                "the newest version its source offers, its status and the "
                "command that updates or installs it. Newest versions are read "
                "when the cached ones are six hours old or more, or with "
                "--refresh."
            ),
        )
    )
    _add_app(
        subparsers.add_parser(
            "app",
            help="start the desktop app, or bring it forward",
            description=(
                "Start Ember's desktop app, or bring the running one forward. "
                "With --hidden it starts in the tray; without a tray the "
                "window shows."
            ),
        )
    )
    _add_skill(
        subparsers.add_parser(
            "skill",
            help="install, remove or show the ember skill",
            description=(
                "The default link is $CLAUDE_CONFIG_DIR/skills/ember or "
                "~/.claude/skills/ember, which OpenCode V2 also discovers. "
                "With --harness opencode on the action, the link is "
                "$XDG_CONFIG_HOME/opencode/skills/ember (~/.config by default). "
                "Both link to the source checkout's skill/ folder or "
                "/usr/share/ember/skill. Each "
                "non-zero exit prints a next: or owner: line."
            ),
        )
    )
    return parser


def _pairs(items: list[str], option: str) -> dict[str, object]:
    values: dict[str, object] = {}
    for item in items:
        name, equals, text = item.partition("=")
        if not equals:
            raise UsageError(f"{option} needs KEY=VALUE, not {item!r}")
        if name in values:
            raise UsageError(f"{option} gives {name} more than once")
        values[name] = choices.parse_value(name, text)
    return values


def _choices_action(args: argparse.Namespace) -> str:
    """Return the one action the arguments ask for, checking combinations."""
    given = [
        name
        for name, present in (
            ("--claim", args.claim),
            ("--finish", args.finish is not None),
            ("--set", bool(args.set)),
            ("--clear", bool(args.clear)),
            ("--once", bool(args.once)),
            ("--clear-once", args.clear_once),
        )
        if present
    ]
    if len(given) > 1:
        raise UsageError(f"{' and '.join(given)} cannot be combined")
    action = given[0] if given else "read"
    if args.id is not None and action not in ("read", "--claim"):
        raise UsageError(f"--id cannot be used with {action}")
    if action == "--claim" and args.id is None:
        raise UsageError("--claim needs --id")
    if args.skill is not None and action != "--claim":
        raise UsageError("--skill needs --claim")
    if args.skill_dir is not None and args.skill is None:
        raise UsageError("--skill-dir needs --skill")
    if args.request and action not in ("read", "--claim"):
        raise UsageError(f"--request cannot be used with {action}")
    if args.brief and action not in ("read", "--claim"):
        raise UsageError(f"--brief cannot be used with {action}")
    owner = ("--set", "--clear", "--once", "--clear-once")
    if args.expect_generation is not None and action not in owner:
        raise UsageError(
            "--expect-generation needs --set, --clear, --once or --clear-once"
        )
    return action


def _run_choices(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    def fail(code: int, message: str) -> int:
        print(f"emb choices: {message}", file=sys.stderr)
        return code

    try:
        action = _choices_action(args)
    except UsageError as error:
        return fail(EX_USAGE, str(error))
    if (
        action in ("--set", "--clear", "--once", "--clear-once")
        and not sys.stdin.isatty()
    ):
        return fail(EX_NOPERM, "owner options require a terminal")
    expect = args.expect_generation
    calls: dict[str, Callable[[], dict[str, Any]]] = {
        "read": lambda: choices.read(
            args.id, _pairs(args.request, "--request"), env=env
        ),
        "--claim": lambda: choices.claim(
            args.id,
            _pairs(args.request, "--request"),
            skill_version=args.skill,
            skill_dir=args.skill_dir,
            env=env,
        ),
        "--finish": lambda: choices.finish(args.finish, env=env),
        "--set": lambda: choices.set_settings(
            _pairs(args.set, "--set"), expect, env=env
        ),
        "--clear": lambda: choices.clear_settings(args.clear, expect, env=env),
        "--once": lambda: choices.set_once(
            _pairs(args.once, "--once"), expect, env=env
        ),
        "--clear-once": lambda: choices.clear_once(expect, env=env),
    }
    try:
        reply = calls[action]()
    except UsageError as error:
        return fail(EX_USAGE, str(error))
    except (choices.InvalidChoiceError, db.DatabaseError) as error:
        return fail(error.exit_code, str(error))
    if reply.get("skill") == "stale":
        print(
            f"WARNING: the loaded ember skill is {reply['loaded_skill_version']}; "
            f"the installed skill is {reply['installed_skill_version']}",
            file=sys.stderr,
        )
        print("next: reload the ember skill before the next step", file=sys.stderr)
    elif reply.get("skill") == "unknown":
        print(
            "WARNING: the installed ember skill version is missing, unreadable "
            "or malformed",
            file=sys.stderr,
        )
        print(
            "owner: repair or reinstall the ember skill, then reload it before "
            "starting a worker",
            file=sys.stderr,
        )
    if args.brief:
        reply = choices.brief(reply)
    print(json.dumps(reply, indent=2))
    return EX_OK


def _age(seconds: float) -> str:
    """Return a short age such as `5 min`."""
    seconds = max(0, int(seconds))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit}"
    return f"{seconds} s"


def _status_lines(item: status.EngineStatus) -> list[str]:
    head = f"{item.engine}: {item.state}"
    if item.version:
        head += f", {item.version}"
    if item.reason:
        head += f": {item.reason}"
    lines = [head]
    for requirement in item.requirements:
        if not requirement.met:
            fix = f": {requirement.fix}" if requirement.fix else ""
            lines.append(f"  {requirement.label}{fix}")
    return lines


def _per_million(value: float) -> str:
    return f"${value * 1_000_000:.2f}/M"


def _model_line(model: models.Model, speed: str | None = None) -> str:
    parts = [model.id]
    if model.label != model.id:
        parts.append(model.label)
    if model.efforts:
        efforts = "efforts " + ", ".join(model.efforts)
        if model.default_effort:
            efforts += f" (default {model.default_effort})"
        parts.append(efforts)
    if speed is not None:
        parts[-1] += f" · {speed}"
    if model.context_length is not None:
        parts.append(f"context {model.context_length}")
    if model.prompt_price is not None:
        parts.append(f"{_per_million(model.prompt_price)} in")
    if model.completion_price is not None:
        parts.append(f"{_per_million(model.completion_price)} out")
    if model.supports_tools:
        parts.append("tools")
    return "  ".join(parts)


def _run_engines(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    if args.models is not None:
        listing = models.list_models(args.models, env, refresh=args.refresh)
        if listing.reason:
            print(f"emb engines: {listing.reason}", file=sys.stderr)
        speeds = {}
        if args.models == "codex" and listing.models:
            try:
                answers = choices.read(env=env)["answers"]
            except db.DatabaseError as error:
                # The list does not depend on the settings; it goes out without speeds.
                print(f"emb engines: {error}", file=sys.stderr)
            else:
                speeds = {
                    m.id: choices.codex_speed(answers, m.id) for m in listing.models
                }
        if args.json:
            document = listing.to_json()
            if speeds:
                for row in document["models"]:
                    row["speed"] = speeds[row["id"]]
            print(json.dumps(document, indent=2))
            return EX_OK
        fetched = (
            "never read"
            if listing.fetched_at is None
            else f"read {_age(time.time() - listing.fetched_at)} ago"
        )
        print(f"{args.models} models from {listing.source}, {fetched}")
        for model in listing.models:
            print(_model_line(model, speeds.get(model.id)))
        return EX_OK
    result = status.read(env, refresh=args.refresh)
    held: dict[str, list[holds.Hold]] = {}
    for hold in holds.active(env):
        held.setdefault(hold.engine, []).append(hold)
    if args.json:
        reply = {
            name: {
                **item.to_json(),
                "holds": [
                    {
                        "model": hold.model,
                        "kind": hold.kind,
                        "since": hold.since,
                        "until": hold.until,
                        "run": hold.run,
                    }
                    for hold in held.get(name, [])
                ],
            }
            for name, item in result.items()
        }
        print(json.dumps(reply, indent=2))
        return EX_OK
    for name, item in result.items():
        for line in _status_lines(item):
            print(line)
        for hold in held.get(name, []):
            until = time.strftime("%H:%M", time.localtime(hold.until))
            print(f"  held: {hold.name()} after {hold.kind} until {until}")
    return EX_OK


def _run_mode(args: argparse.Namespace) -> str:
    """Return the one `emb run` mode the arguments ask for, checking options."""
    given = [mode for mode in _RUN_MODES if getattr(args, mode) is not None]
    if len(given) > 1:
        options = " and ".join(f"--{mode}" for mode in given)
        raise UsageError(f"{options} cannot be combined")
    mode = given[0] if given else "start"
    start_only = {
        "--engine": args.engine is not None,
        "--dir": args.dir is not None,
        "--task-file": args.task_file is not None,
        "--model": args.model is not None,
        "--effort": args.effort is not None,
        "--read-only": args.read_only,
        "--read": bool(args.read),
        "--trailer": bool(args.trailer),
        "--claim": args.claim is not None,
        "--window": args.window,
        "--continue-run": args.continue_run is not None,
        "--fallback-from": args.fallback_from is not None,
        "--dry-run": args.dry_run,
    }
    if mode == "start":
        missing = [
            name
            for name in ("--engine", "--dir", "--task-file")
            if not start_only[name]
        ]
        if missing:
            raise UsageError(f"a new run needs {', '.join(missing)}")
    else:
        extra = [name for name, present in start_only.items() if present]
        if extra:
            raise UsageError(f"{', '.join(extra)} cannot be used with --{mode}")
    if (args.message_file is not None) != (mode == "send"):
        raise UsageError("--send and --message-file go together")
    if args.force and mode not in ("close", "send"):
        raise UsageError("--force needs --close or --send")
    if args.json and mode != "status" and not args.dry_run:
        raise UsageError("--json needs --status or --dry-run")
    return mode


def _run_run(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    try:
        mode = _run_mode(args)
    except UsageError as error:
        print(f"emb run: {error}", file=sys.stderr)
        print("next: emb run --help", file=sys.stderr)
        return EX_USAGE
    core = run_core.Core(env, stall_minutes=args.stall, quiet=args.quiet)
    actions: dict[str, Callable[[], int]] = {
        "start": lambda: core.start(
            run_core.StartOptions(
                engine=args.engine,
                dir=args.dir,
                task_file=args.task_file,
                model=args.model,
                effort=args.effort,
                read_only=args.read_only,
                read=tuple(args.read),
                trailers=tuple(args.trailer),
                claim=args.claim,
                window=args.window,
                continue_run=args.continue_run,
                fallback_from=args.fallback_from,
                dry_run=args.dry_run,
                as_json=args.json,
            )
        ),
        "send": lambda: core.send(args.send, args.message_file, force=args.force),
        "screen": lambda: core.screen(args.screen),
        "interrupt": lambda: core.interrupt(args.interrupt),
        "attach": lambda: core.attach(args.attach),
        "close": lambda: core.close(args.close, force=args.force),
        "recover": lambda: core.recover(args.recover),
        "status": lambda: core.status(args.status or None, as_json=args.json),
        "answered": lambda: core.answered(args.answered),
    }
    code = core.execute(actions[mode])
    if code == EX_OK and not args.dry_run:
        next_line = None
        if mode == "status":
            for text in core.status_owner:
                print(text, file=sys.stderr)
            next_line = core.status_next
        elif mode in ("start", "send", "recover", "close", "answered"):
            run_id = core.last_run_id if mode == "start" else getattr(args, mode)
            if run_id is not None:
                with contextlib.suppress(record.RecordError):
                    step, next_line = core.step(record.load(run_id, env))
                    if mode in ("start", "send", "recover") and step == "owner":
                        for text in record.load(run_id, env).owner_wait:
                            if text not in core.printed_owner:
                                print(text, file=sys.stderr)
                        code = exits.OWNER_WAIT
        if next_line is not None:
            print(next_line, file=sys.stderr)
    if mode == "status" and code == EX_OK:
        runs = (
            [record.load(args.status, env)] if args.status else record.list_runs(env)[0]
        )
        for run in runs:
            step, _ = core.step(run)
            if step in ("failed", "owner"):
                text = exits.report(
                    run.last_exit or 0, steps.shown_variant(run)[0], **run.to_json()
                )
                if text:
                    print(text, file=sys.stderr)
    elif core.last_error is not None:
        error = core.last_error
        context = dict(error.context)
        run_id = context.get("run") or core.last_run_id
        if isinstance(run_id, str):
            with contextlib.suppress(record.RecordError):
                context = {**record.load(run_id, env).to_json(), **context}
        text = (
            exits.report(error.code, error.variant, **context)
            if error.code != exits.UNAVAILABLE or mode == "start"
            else None
        )
        if text:
            print(text, file=sys.stderr)
    return code


def _run_ground(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    try:
        result = ground.read(args.dir, env)
    except ground.DirectoryMissingError as error:
        print(f"emb ground: {error}", file=sys.stderr)
        print("next: emb ground --dir <an existing folder>", file=sys.stderr)
        return EX_NOINPUT
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for line in ground.lines(result):
            print(line)
    return EX_OK


def _deps_lines(item: deps.Component) -> list[str]:
    head = f"{item.label}: {item.status}"
    if item.link_state is not None and item.link_state not in (item.error, item.status):
        head += f", {item.link_state}"
    if item.installed is not None:
        head += f", installed {item.installed}"
    if item.newest is not None:
        head += f", newest {item.newest}"
    if item.error:
        head += f": {item.error}"
    lines = [head]
    if (
        item.resolved_path is not None
        and item.command is not None
        and item.command_kind == "update"
    ):
        lines.append(f"  resolved path: {item.resolved_path}")
    if item.command is not None:
        lines.append(f"  {item.command_kind}: {item.command}")
    if item.note is not None and (
        item.status == deps.UPDATE_AVAILABLE or item.installed_kind == "skill"
    ):
        lines.append(f"  {item.note}")
    return lines


def _run_deps(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    try:
        report = deps.read(env, newest="always" if args.refresh else "due")
    except deps.ComponentsError as error:
        print(f"emb deps: {error}", file=sys.stderr)
        return EX_SOFTWARE
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
        return EX_OK
    for item in report.components:
        for line in _deps_lines(item):
            print(line)
    return EX_OK


def _run_benchmarks(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    refreshing = args.benchmark_action == "refresh"
    try:
        if refreshing:
            reply = benchmarks.refresh(env)
        else:
            settings = choices.read(env=env)
            if settings["source_error"]:
                raise choices.InvalidChoiceError("; ".join(settings["errors"]))
            reply = benchmarks.cached(settings["answers"], env)
    except (choices.InvalidChoiceError, db.DatabaseError) as error:
        print(f"emb benchmarks: {error}", file=sys.stderr)
        return error.exit_code
    except benchmarks.RefreshBusyError as error:
        print(f"emb benchmarks: {error}", file=sys.stderr)
        return db.EX_TEMPFAIL
    except OSError as error:
        print(f"emb benchmarks: {error}", file=sys.stderr)
        return db.EX_IOERR
    if args.json:
        print(json.dumps(reply, indent=2))
    else:
        for line in benchmarks.lines(reply):
            print(line)
    if refreshing and reply["status"] == "unavailable":
        return EX_UNAVAILABLE
    return EX_OK


def _run_skill(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    candidates = skill.source_candidates()
    if args.skill_action == "status":
        current = skill.status(env, candidates, harness=args.harness)
        if args.json:
            print(json.dumps(current.to_json(), indent=2))
        else:
            for line in skill.status_lines(current):
                print(line)
        return EX_OK
    try:
        if args.skill_action == "install":
            result = skill.install(
                env, candidates, force=args.force, harness=args.harness
            )
        else:
            result = skill.uninstall(env, candidates, harness=args.harness)
    except skill.SkillError as error:
        print(f"emb skill: {error}", file=sys.stderr)
        print(error.line, file=sys.stderr)
        return error.code
    print(json.dumps(result.to_json(), indent=2) if args.json else result.line())
    return EX_OK


class _Watched:
    """Pass writes through to a stream, noting whether any text was written.

    While `keep()` is true, a copy of each write is kept in `parts`.
    """

    def __init__(self, stream: TextIO, keep: Callable[[], bool]) -> None:
        """Wrap `stream`, keeping copies while `keep()` is true."""
        self.stream = stream
        self.keep = keep
        self.written = False
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        """Write `text` to the wrapped stream, keeping a copy while needed."""
        if text:
            self.written = True
            if self.keep():
                self.parts.append(text)
        return self.stream.write(text)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - any stream attribute
        """Delegate every other attribute to the wrapped stream."""
        return getattr(self.stream, name)


_MESSAGE = re.compile(r"emb(?: [a-z-]+)*: (.*)")
_ACTIONS = ("next:", "owner:", "report:")


def _json_error(
    code: int, diagnostics: str, crash: BaseException | None = None
) -> dict[str, Any]:
    """Build the error object for a failed `--json` command.

    `messages` holds each `emb <command>: <message>` line without its prefix,
    `actions` each `next:`, `owner:` and `report:` line as printed, and
    `warnings` each `WARNING:` line without its prefix, all in printed
    order. Other standard error lines, such as a usage line, are left out.
    An exception that escaped the command, `crash`, adds
    `<type>: <message>` to `messages`.
    """
    messages: list[str] = []
    actions: list[str] = []
    warnings: list[str] = []
    for line in diagnostics.splitlines():
        if line.startswith(_ACTIONS):
            actions.append(line)
        elif line.startswith("WARNING: "):
            warnings.append(line.removeprefix("WARNING: "))
        elif match := _MESSAGE.fullmatch(line):
            messages.append(match.group(1))
    if crash is not None:
        messages.append(f"{type(crash).__name__}: {crash}")
    return {
        "error": {
            "exit": code,
            "messages": messages,
            "actions": actions,
            "warnings": warnings,
        }
    }


def _wants_json(argv: Sequence[str]) -> bool:
    """Report whether `--json` is among the options, before any `--`."""
    for word in argv:
        if word == "--":
            return False
        if word == "--json":
            return True
    return False


def main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None
) -> int:
    """Run `emb` with the given arguments.

    With `--json`, a non-zero exit that printed nothing on standard output
    prints `_json_error`'s object there; standard error is unchanged. This
    includes an exception that escapes the command, which is then raised
    again. A command that printed JSON before failing keeps its own output.

    Args:
        argv: Arguments after the program name; `None` reads `sys.argv`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The exit code.
    """
    words = list(sys.argv[1:] if argv is None else argv)
    if not _wants_json(words):
        return _main(words, env)
    real_out, real_err = sys.stdout, sys.stderr
    out = _Watched(real_out, lambda: False)
    # Diagnostics matter only until the command prints its own output.
    err = _Watched(real_err, lambda: not out.written)
    sys.stdout, sys.stderr = cast(TextIO, out), cast(TextIO, err)
    code: int | None = None
    crash: BaseException | None = None
    try:
        code = _main(words, env)
        return code
    except SystemExit as error:
        code = (
            error.code
            if isinstance(error.code, int)
            else 0
            if error.code is None
            else 1
        )
        raise
    except BaseException as error:
        # Python exits 130 after an interrupt and 1 after other exceptions.
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
        crash = error
        raise
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        if code and not out.written:
            print(json.dumps(_json_error(code, "".join(err.parts), crash), indent=2))


def _main(argv: Sequence[str], env: Mapping[str, str] | None) -> int:
    """Parse `argv` and run the command it names."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EX_USAGE
    if args.command == "choices":
        return _run_choices(args, env)
    if args.command == "engines":
        return _run_engines(args, env)
    if args.command == "benchmarks":
        return _run_benchmarks(args, env)
    if args.command == "run":
        return _run_run(args, env)
    if args.command == "gapcheck":
        from ember.gapcheck import command

        return command.main(args, env)
    if args.command == "flow":
        from ember.flows import command

        return command.main(args, env)
    if args.command == "panel":
        from ember.panels import command

        return command.main(args, env)
    if args.command == "ground":
        return _run_ground(args, env)
    if args.command == "deps":
        return _run_deps(args, env)
    if args.command == "app":
        from ember.app import main as app_main  # Qt is imported only for `emb app`

        return app_main.run(["--hidden"] if args.hidden else [], env)
    if args.command == "skill":
        return _run_skill(args, env)
    if args.command == "merge":
        failures: list[branches.BranchError] = []
        code = branches.merge(
            args.branch,
            args.into,
            args.verdict,
            note=args.note,
            subject=args.subject,
            directory=args.dir,
            as_json=args.json,
            env=env,
            failures=failures,
        )
        for failure in failures:
            text = (
                exits.report(failure.code, failure.variant)
                if failure.code == exits.REFUSED and failure.variant == "conflict"
                else None
            )
            if text:
                print(text, file=sys.stderr)
        return code
    if args.command == "prune":
        from ember import prune

        if args.older_than is None and args.claim is None:
            parser.error("prune requires --older-than unless --claim is given")
        return prune.main(
            args.older_than or 0,
            claim=args.claim,
            dry_run=args.dry_run,
            as_json=args.json,
            env=env,
        )
    if args.command == "cleanup":
        if args.merged_into is None and args.claim is None:
            parser.error("cleanup requires --merged-into unless --claim is given")
        failures = []
        code = branches.cleanup(
            args.merged_into,
            branch=args.branch,
            claim=args.claim,
            directory=args.dir,
            dry_run=args.dry_run,
            as_json=args.json,
            env=env,
            failures=failures,
        )
        for failure in failures:
            text = (
                exits.report(failure.code, failure.variant)
                if failure.code == exits.REFUSED and failure.variant == "conflict"
                else None
            )
            if text:
                print(text, file=sys.stderr)
        return code
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
