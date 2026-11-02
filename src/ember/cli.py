"""The `emb` command.

Exit codes follow BSD `sysexits.h`. For `emb choices` and `emb engines`: 0 on
success, 64 for a usage error or an invalid value, 74 when the settings
database cannot be opened or read, 75 for a stale generation or a busy
database, 77 for an owner option without a terminal and 78 for an unsupported
database schema. `emb run` uses the table in `ember.runs.exits`.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

from ember import __version__, choices, db
from ember.engines import models, status
from ember.runs import core as run_core

EX_OK = 0
EX_USAGE = 64
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
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
