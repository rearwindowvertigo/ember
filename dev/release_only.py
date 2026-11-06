"""Tell whether a pull request changes only what a Release Please release changes.

A release pull request adds a `CHANGELOG.md` section, sets the version in
`.release-please-manifest.json`, and sets the project version in
`pyproject.toml`, in the `ember-app` package entry of `uv.lock` and on the
`Skill version:` line of `skill/SKILL.md`. When a pull request changes
nothing else, the check workflow skips the full check for it.

Usage: `python3 dev/release_only.py <base> <head> [--github-output <file>]`.
It prints `release_only=true` or `release_only=false` and one line saying
why, and appends the first line to the `--github-output` file when given.
It exits 0 in both cases, and 1 when git fails.
"""

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

FREE_FILES = frozenset({"CHANGELOG.md", ".release-please-manifest.json"})
"""Files a release changes without limit."""

VERSION_FILES = frozenset({"pyproject.toml", "uv.lock", "skill/SKILL.md"})
"""Files a release changes only in their version."""

PACKAGE = "ember-app"
"""The project's package name in `pyproject.toml` and `uv.lock`."""

SKILL_LINE = re.compile(r"^(Skill version: )(\S+)(.*)$", re.MULTILINE)
"""The skill's version line, with its annotation after the version."""


class GitError(Exception):
    """A git command failed."""


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _show(ref: str, path: str) -> str:
    return _git("show", f"{ref}:{path}")


def _project_version(text: str) -> str:
    return str(tomllib.loads(text)["project"]["version"])


def _with_version(path: str, text: str, old: str, new: str) -> object:
    """Return the file at the base with only its version set to `new`.

    Args:
        path: The file's path in the repository.
        text: The file's text at the base.
        old: The version at the base.
        new: The version at the head.

    Returns:
        A value to compare with the same file at the head through
        `_comparable`.
    """
    if path == "pyproject.toml":
        data = tomllib.loads(text)
        data["project"]["version"] = new
        return data
    if path == "uv.lock":
        data = tomllib.loads(text)
        for package in data.get("package", []):
            if package.get("name") == PACKAGE and package.get("version") == old:
                package["version"] = new
        return data
    return SKILL_LINE.sub(
        lambda match: (
            match.group(1) + new + match.group(3)
            if match.group(2) == old
            else match.group(0)
        ),
        text,
        count=1,
    )


def _comparable(path: str, text: str) -> object:
    if path in ("pyproject.toml", "uv.lock"):
        return tomllib.loads(text)
    return text


def decide(changed: list[str], read: Callable[[str, str], str]) -> tuple[bool, str]:
    """Decide whether a change set is release-only.

    Args:
        changed: The paths the pull request changes.
        read: Called as `read(side, path)` with side `"base"` or `"head"`;
            returns the file's text on that side.

    Returns:
        Whether the change is release-only, and why.
    """
    if not changed:
        return False, "no changed files"
    other = sorted(set(changed) - FREE_FILES - VERSION_FILES)
    if other:
        return False, f"changes {', '.join(other)}"
    try:
        old = _project_version(read("base", "pyproject.toml"))
        new = _project_version(read("head", "pyproject.toml"))
    except (KeyError, tomllib.TOMLDecodeError) as error:
        return False, f"cannot read the project version: {error}"
    if old == new:
        return False, "the project version is unchanged"
    for path in sorted(set(changed) & VERSION_FILES):
        try:
            expected = _with_version(path, read("base", path), old, new)
            actual = _comparable(path, read("head", path))
        except tomllib.TOMLDecodeError as error:
            return False, f"cannot read {path}: {error}"
        if expected != actual:
            return False, f"{path} changes more than the version"
    return True, f"only the release files change, {old} to {new}"


def main(argv: list[str]) -> int:
    """Print the decision for `base..head` and write the workflow output.

    Args:
        argv: The command-line arguments after the program name.

    Returns:
        The exit code: 0 after a decision, 1 when git fails.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", help="the base commit, such as origin/main")
    parser.add_argument("head", help="the pull request's commit")
    parser.add_argument("--github-output", type=Path, help="the step output file")
    args = parser.parse_args(argv)
    try:
        changed = _git("diff", "--name-only", args.base, args.head).split()
        refs = {"base": args.base, "head": args.head}
        release, reason = decide(changed, lambda side, path: _show(refs[side], path))
    except GitError as error:
        print(error, file=sys.stderr)
        return 1
    line = f"release_only={'true' if release else 'false'}"
    print(line)
    print(reason)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
