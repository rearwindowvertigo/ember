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
"""

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

from ember import __version__, branches, choices, db, deps, ground, skill
from ember.engines import models, status
from ember.runs import core as run_core

EX_OK = 0
EX_USAGE = 64
EX_NOINPUT = 66
EX_SOFTWARE = 70
EX_NOPERM = 77


class ArgumentParser(argparse.ArgumentParser):
    """An argument parser whose usage errors exit 64."""

    def error(self, message: str) -> NoReturn:
        """Print the usage and `message` to standard error and exit 64.

        Args:
            message: What is wrong with the arguments.
        """
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"{self.prog}: {message}\n")


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


def _positive_minutes(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive number")
    return value


_RUN_MODES = ("send", "screen", "interrupt", "attach", "close", "recover", "status")
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
        "--status",
        nargs="?",
        const=_ALL_RUNS,
        metavar="RUN",
        help="print a run's record, or every run's",
    )
    existing.add_argument("--json", action="store_true", help="with --status: JSON")
    output = parser.add_argument_group("waiting and output")
    output.add_argument(
        "--quiet", action="store_true", help="print only the first line and summary"
    )
    output.add_argument(
        "--stall",
        type=_positive_minutes,
        default=run_core.STALL_MINUTES,
        metavar="MINUTES",
        help="minutes without a screen change before exit 124 (default 10)",
    )


def _add_gapcheck(parser: argparse.ArgumentParser) -> None:
    """Add the plan review, brief and reply options."""
    parser.add_argument("--plan", required=True, metavar="FILE", help="the plan")
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


def _add_flow(parser: argparse.ArgumentParser) -> None:
    """Add flow modes, worker selections and lead answers."""
    parser.add_argument("--plan", metavar="FILE", help="the JSON task plan")
    parser.add_argument(
        "--dry-run", action="store_true", help="print workers and waves"
    )
    parser.add_argument("--start", action="store_true", help="start the flow runner")
    parser.add_argument("--watch", metavar="FLOW", help="wait for a lead stop or end")
    parser.add_argument(
        "--status", nargs="?", const="", metavar="FLOW", help="show one or all flows"
    )
    parser.add_argument(
        "--lead", nargs=2, metavar=("FLOW", "TASK"), help="answer a task's lead stop"
    )
    parser.add_argument("--resume", metavar="FLOW", help="restart a flow's runner")
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
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_panel(parser: argparse.ArgumentParser) -> None:
    """Add panel start, validation, status and recovery options."""
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
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_ground(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir", metavar="DIR", help="the folder to read (default: the current one)"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_merge(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("branch", metavar="BRANCH", help="the reviewed branch")
    parser.add_argument(
        "--into", required=True, metavar="TARGET", help="the branch to merge into"
    )
    parser.add_argument(
        "--verdict", metavar="VERDICT", help="the lead's verdict; only keep merges"
    )
    parser.add_argument(
        "--dir", default=".", metavar="DIR", help="a folder in the git checkout"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_cleanup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--merged-into",
        required=True,
        metavar="TARGET",
        help="the branch the worker branches were merged into",
    )
    parser.add_argument("--branch", metavar="BRANCH", help="only this branch")
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
        help="link the skill into Claude Code's skills folder",
        description=(
            "Create the link to the skill source. A broken link is replaced; "
            "a link to another folder only with --force; a folder or file is "
            "never touched."
        ),
    )
    install.add_argument(
        "--force", action="store_true", help="replace a link to another folder"
    )
    install.add_argument("--json", action="store_true", help="print JSON")
    uninstall = actions.add_parser(
        "uninstall",
        help="remove the skill's link",
        description=(
            "Remove the link when it points to an Ember skill or to nothing. "
            "A folder or file is never touched."
        ),
    )
    uninstall.add_argument("--json", action="store_true", help="print JSON")
    show = actions.add_parser(
        "status",
        help="print the link, its state, its target and the skill source",
        description="Print the link, its state, its target and the skill source.",
    )
    show.add_argument("--json", action="store_true", help="print JSON")


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for `emb` and its subcommands.

    Returns:
        The top-level parser.
    """
    parser = ArgumentParser(
        prog="emb",
        description="Let Claude lead AI coding workers on this machine.",
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
                "Print the machine, the Claude Code surface, Claude Code's "
                "worktree.baseRef setting and the folder's checkout, branch, "
                "dirty state, remote and instruction files."
            ),
        )
    )
    _add_merge(
        subparsers.add_parser(
            "merge",
            help="merge a reviewed worker branch, then clean it up",
            description=(
                "Merge a reviewed branch into a local branch with a merge commit, "
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
            help="install or remove the ember skill for Claude Code",
            description=(
                "Install, remove or show the ember skill for Claude Code. The "
                "skill is a link at $CLAUDE_CONFIG_DIR/skills/ember, or "
                "~/.claude/skills/ember when CLAUDE_CONFIG_DIR is unset or empty, to "
                "the skill source: the source checkout's skill/ folder when "
                "emb runs from a checkout, else /usr/share/ember/skill. Each "
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
    if args.request and action not in ("read", "--claim"):
        raise UsageError(f"--request cannot be used with {action}")
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
            args.id, _pairs(args.request, "--request"), env=env
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


def _model_line(model: models.Model) -> str:
    parts = [model.id]
    if model.label != model.id:
        parts.append(model.label)
    if model.efforts:
        efforts = "efforts " + ", ".join(model.efforts)
        if model.default_effort:
            efforts += f" (default {model.default_effort})"
        parts.append(efforts)
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
        if args.json:
            print(json.dumps(listing.to_json(), indent=2))
            return EX_OK
        fetched = (
            "never read"
            if listing.fetched_at is None
            else f"read {_age(time.time() - listing.fetched_at)} ago"
        )
        print(f"{args.models} models from {listing.source}, {fetched}")
        for model in listing.models:
            print(_model_line(model))
        return EX_OK
    result = status.read(env, refresh=args.refresh)
    if args.json:
        reply = {name: item.to_json() for name, item in result.items()}
        print(json.dumps(reply, indent=2))
        return EX_OK
    for item in result.values():
        for line in _status_lines(item):
            print(line)
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
    if args.json and mode != "status":
        raise UsageError("--json needs --status")
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
            )
        ),
        "send": lambda: core.send(args.send, args.message_file, force=args.force),
        "screen": lambda: core.screen(args.screen),
        "interrupt": lambda: core.interrupt(args.interrupt),
        "attach": lambda: core.attach(args.attach),
        "close": lambda: core.close(args.close, force=args.force),
        "recover": lambda: core.recover(args.recover),
        "status": lambda: core.status(args.status or None, as_json=args.json),
    }
    return core.execute(actions[mode])


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
    if item.installed is not None:
        head += f", installed {item.installed}"
    if item.newest is not None:
        head += f", newest {item.newest}"
    if item.error:
        head += f": {item.error}"
    lines = [head]
    if item.command is not None:
        lines.append(f"  {item.command_kind}: {item.command}")
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


def _run_skill(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    candidates = skill.source_candidates()
    if args.skill_action == "status":
        current = skill.status(env, candidates)
        if args.json:
            print(json.dumps(current.to_json(), indent=2))
        else:
            for line in skill.status_lines(current):
                print(line)
        return EX_OK
    try:
        if args.skill_action == "install":
            result = skill.install(env, candidates, force=args.force)
        else:
            result = skill.uninstall(env, candidates)
    except skill.SkillError as error:
        print(f"emb skill: {error}", file=sys.stderr)
        print(error.line, file=sys.stderr)
        return error.code
    print(json.dumps(result.to_json(), indent=2) if args.json else result.line())
    return EX_OK


def main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None
) -> int:
    """Run `emb` with the given arguments.

    Args:
        argv: Arguments after the program name; `None` reads `sys.argv`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EX_USAGE
    if args.command == "choices":
        return _run_choices(args, env)
    if args.command == "engines":
        return _run_engines(args, env)
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
        return branches.merge(
            args.branch,
            args.into,
            args.verdict,
            directory=args.dir,
            as_json=args.json,
            env=env,
        )
    if args.command == "cleanup":
        return branches.cleanup(
            args.merged_into,
            branch=args.branch,
            directory=args.dir,
            dry_run=args.dry_run,
            as_json=args.json,
            env=env,
        )
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
