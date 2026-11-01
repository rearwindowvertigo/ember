"""The `emb` command."""

import argparse
import sys
from collections.abc import Sequence

from ember import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for `emb` and its subcommands.

    Returns:
        The top-level parser.
    """
    parser = argparse.ArgumentParser(
        prog="emb",
        description="Let Claude lead AI coding workers on this machine.",
    )
    parser.add_argument("--version", action="version", version=f"emb {__version__}")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run `emb` with the given arguments.

    Args:
        argv: Arguments after the program name; `None` reads `sys.argv`.

    Returns:
        The exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
