#!/usr/bin/env python3
"""Print a worker branch's tip, its numstat and its worktree's status.

Usage:
    python3 scripts/verify_branch.py --repo REPO --base BASE --branch BRANCH
        [--worktree WORKTREE] [--expect-files N]

It decides no verdict; the lead reads what it prints. Exit 0 once it has
printed, 2 when a git command fails or git cannot run, 64 for a usage error.
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

EX_OK = 0
EX_GIT = 2
EX_USAGE = 64

GIT_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
"""Variables that would point git at another repository; git runs without them."""


class GitError(Exception):
    """A git command failed or git cannot run."""


class ArgumentParser(argparse.ArgumentParser):
    """An argument parser whose usage errors exit 64."""

    def error(self, message: str) -> NoReturn:
        """Print the usage and `message` to standard error and exit 64.

        Args:
            message: What is wrong with the arguments.
        """
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"{self.prog}: {message}\n")


def git_read(folder: Path, *args: str) -> str:
    """Run a git command that reads `folder`'s repository.

    Args:
        folder: The folder git runs in.
        *args: The git command and its arguments.

    Returns:
        Its standard output.

    Raises:
        GitError: Git cannot run or exits non-zero.
    """
    env = {
        name: value for name, value in os.environ.items() if name not in GIT_VARIABLES
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.quotePath=true",
        "-C",
        str(folder),
        *args,
    ]
    try:
        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            check=False,
        )
    except OSError as error:
        raise GitError(f"cannot run git: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise GitError(f"git {args[0]} failed: {detail}")
    return result.stdout


def nonnegative(text: str) -> int:
    """Parse a whole number that is zero or greater.

    Args:
        text: The argument.

    Returns:
        The number.

    Raises:
        ArgumentTypeError: It is not a whole number of zero or more.
    """
    try:
        number = int(text)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number >= 0")
    return number


def build_parser() -> ArgumentParser:
    """Build the parser.

    Returns:
        The parser.
    """
    parser = ArgumentParser(
        prog="verify_branch.py",
        description=(
            "Print a branch's tip, the numstat of BASE...BRANCH and the "
            "worktree's git status --short. It decides no verdict."
        ),
    )
    parser.add_argument("--repo", required=True, type=Path, help="the repository")
    parser.add_argument("--base", required=True, help="the session branch")
    parser.add_argument("--branch", required=True, help="the worker branch or a commit")
    parser.add_argument(
        "--worktree", type=Path, help="the worker's worktree (default: --repo)"
    )
    parser.add_argument(
        "--expect-files",
        type=nonnegative,
        metavar="N",
        help="print a MISMATCH line when the numstat lists another number of files",
    )
    return parser


def _print_block(text: str) -> None:
    if text:
        print(text, end="" if text.endswith("\n") else "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Print the branch's scope and the worktree's status.

    Args:
        argv: The arguments; `None` reads `sys.argv`.

    Returns:
        The exit code.
    """
    args = build_parser().parse_args(argv)
    repo = args.repo.expanduser()
    worktree = (args.worktree or args.repo).expanduser()
    try:
        base = git_read(
            repo, "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}"
        ).strip()
        tip = git_read(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{args.branch}^{{commit}}",
        ).strip()
        numstat = git_read(
            repo,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--numstat",
            f"{base}...{tip}",
            "--",
        )
        status = git_read(worktree, "status", "--short")
    except GitError as error:
        print(f"verify_branch.py: {error}", file=sys.stderr)
        return EX_GIT
    # Without -z, git quotes a path holding a tab, a newline or a quote, so
    # each numstat line is one file.
    count = len(numstat.splitlines())
    print(f"tip: {tip}")
    print(f"numstat: {args.base}...{args.branch}")
    _print_block(numstat)
    print(f"files: {count}")
    if args.expect_files is not None and count != args.expect_files:
        print(f"MISMATCH: expected {args.expect_files} files; found {count}")
    print(f"status --short: {worktree}")
    _print_block(status)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
