"""The `emb` command.

Exit codes follow BSD `sysexits.h`: 0 on success, 64 for a usage error or an
invalid value, 74 when the settings database cannot be opened or read, 75 for
a stale generation or a busy database, 77 for an owner option without a
terminal and 78 for an unsupported database schema.
"""

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

from ember import __version__, choices, db

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
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
