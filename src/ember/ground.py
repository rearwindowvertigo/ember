"""What a lead reads before choosing a worker, in one call.

`read` returns the machine and the state of one folder: whether it is in a
git checkout, the top of that checkout, the branch, whether the tree is
dirty, the remote, which of `CLAUDE.md` and `AGENTS.md` exist at the top,
and Ember's worktree preparation setting. It also reports the Claude Code
surface and `worktree.baseRef` for native Claude Code workers. Git runs
with optional locks off, so reading takes no lock and writes nothing.

A value that cannot be read is `None` with a sibling `<field>_reason` key
holding the reason.
"""

import json
import os
import re
import socket
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ember import paths, prepare

GIT_TIMEOUT = 60
"""Seconds each git command may take."""

ENTRYPOINT_VARIABLE = "CLAUDE_CODE_ENTRYPOINT"
"""The variable Claude Code sets to name its surface."""

CONFIG_DIR_VARIABLE = "CLAUDE_CONFIG_DIR"
"""The variable that moves Claude Code's user settings folder."""

DESKTOP_ENTRYPOINT = "claude-desktop"
"""The value of `CLAUDE_CODE_ENTRYPOINT` in the Claude desktop app."""

INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
"""The instruction files looked for at the top of the checkout."""

GIT_LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
"""Variables that would point git at a repository other than the folder."""

_SCP_HOST = re.compile(r"^(?:[^@/:]+@)?([^/:]+):")


class DirectoryMissingError(Exception):
    """The folder to read does not exist or is not a folder."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def _git(
    folder: Path, env: Mapping[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    """Run git in `folder` without the variables that would redirect it.

    Git's messages are in English, and optional locks are off, so `git status`
    does not take `index.lock` or rewrite the index. A git that cannot run or
    times out gives exit code 1 with the error as its standard error.
    """
    clean = {
        key: value for key, value in env.items() if key not in GIT_LOCATION_VARIABLES
    }
    clean["LC_ALL"] = "C"  # Messages in English, so they can be recognised.
    clean["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", "-C", str(folder), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        message = f"git {args[0]} timed out after {GIT_TIMEOUT}s"
        return subprocess.CompletedProcess(args, 1, "", message)
    except OSError as error:
        return subprocess.CompletedProcess(args, 1, "", f"git cannot run: {error}")


def _failure(result: subprocess.CompletedProcess[str], what: str) -> str:
    detail = _one_line(result.stderr or result.stdout)
    return f"{what} failed: {detail}" if detail else f"{what} failed"


def machine() -> dict[str, Any]:
    """Return the host name.

    Returns:
        `{"machine": <name>}`, or `None` with `machine_reason`.
    """
    try:
        name = socket.gethostname()
    except OSError as error:
        return {
            "machine": None,
            "machine_reason": f"cannot read the host name: {error}",
        }
    if not name:
        return {"machine": None, "machine_reason": "the host name is empty"}
    return {"machine": name}


def surface(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the Claude Code surface from `CLAUDE_CODE_ENTRYPOINT`.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `entrypoint` (the value, or `None` with `entrypoint_reason` when it is
        unset or empty) and `desktop` (whether it is `claude-desktop`).
    """
    entrypoint = _env(env).get(ENTRYPOINT_VARIABLE) or None
    result: dict[str, Any] = {
        "entrypoint": entrypoint,
        "desktop": entrypoint == DESKTOP_ENTRYPOINT,
    }
    if entrypoint is None:
        result["entrypoint_reason"] = f"{ENTRYPOINT_VARIABLE} is not set"
    return result


def settings_files(
    top: Path | None, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Return the Claude Code settings files in the order they are applied.

    Args:
        top: The top of the git checkout, or `None` outside one.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The user settings file, then, with a checkout, its
        `.claude/settings.json` and `.claude/settings.local.json`. The user
        settings file is `settings.json` in `CLAUDE_CONFIG_DIR` when that is
        set and not empty, otherwise `~/.claude/settings.json`.
    """
    config = _env(env).get(CONFIG_DIR_VARIABLE, "")
    user = Path(config) if config else paths.home_dir(env) / ".claude"
    files = [user / "settings.json"]
    if top is not None:
        files += [
            top / ".claude" / "settings.json",
            top / ".claude" / "settings.local.json",
        ]
    return files


def worktree_base(
    top: Path | None, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Return Claude Code's `worktree.baseRef` setting and its file.

    A missing file, or a file without the key, is skipped, and a later file
    wins. An unreadable or malformed file, or a value that is not a non-empty
    string, gives `None` with the reason.

    Args:
        top: The top of the git checkout, or `None` outside one.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `{"worktree_base": {"value": ..., "file": ...}}`, or
        `{"worktree_base": None, "worktree_base_reason": ...}`.
    """
    found: dict[str, str] | None = None
    files = settings_files(top, env)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as error:
            return _no_base(f"{path} cannot be read: {_one_line(error)}")
        try:
            settings = json.loads(text)
        except ValueError as error:
            return _no_base(f"{path} is not valid JSON: {_one_line(error)}")
        if not isinstance(settings, dict):
            return _no_base(f"{path} does not hold a JSON object")
        if "worktree" not in settings:
            continue
        worktree = settings["worktree"]
        if not isinstance(worktree, dict):
            return _no_base(f"worktree in {path} is not an object")
        if "baseRef" not in worktree:
            continue
        value = worktree["baseRef"]
        if not isinstance(value, str) or not value:
            return _no_base(f"worktree.baseRef in {path} is not a non-empty string")
        found = {"value": value, "file": str(path)}
    if found is None:
        names = ", ".join(str(path) for path in files)
        return _no_base(f"worktree.baseRef is not set in {names}")
    return {"worktree_base": found}


def _no_base(reason: str) -> dict[str, Any]:
    return {"worktree_base": None, "worktree_base_reason": reason}


def remote_kind(url: str) -> str:
    """Return `github` for a github.com URL, otherwise `other`.

    Args:
        url: A remote URL, in URL form or git's `[user@]host:path` form.

    Returns:
        `github` or `other`.
    """
    if "://" in url:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
    else:
        match = _SCP_HOST.match(url)
        host = match.group(1) if match else None
    return "github" if host is not None and host.lower() == "github.com" else "other"


def _branch(top: Path, env: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    symbolic = _git(top, env, "symbolic-ref", "--short", "-q", "HEAD")
    name = symbolic.stdout.strip()
    if symbolic.returncode == 0 and name:
        result["name"] = name
    elif symbolic.returncode == 1 and not symbolic.stderr.strip():
        result.update(name=None, name_reason="detached HEAD")
    else:
        result.update(name=None, name_reason=_failure(symbolic, "git symbolic-ref"))
    commit = _git(top, env, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    if commit.returncode == 0 and commit.stdout.strip():
        result["commit"] = commit.stdout.strip()
    elif commit.returncode == 1 and not commit.stderr.strip():
        result.update(commit=None, commit_reason="the branch has no commit")
    else:
        result.update(commit=None, commit_reason=_failure(commit, "git rev-parse"))
    return {"branch": result}


def _nested_worktrees(top: Path, env: Mapping[str, str]) -> list[str]:
    """Return the checkout's other worktrees inside `top`, relative to it.

    A `git worktree list` that fails gives an empty list.
    """
    listing = _git(top, env, "worktree", "list", "--porcelain", "-z")
    if listing.returncode != 0:
        return []
    real_top = Path(os.path.realpath(top))
    prefix = "worktree "
    nested = []
    for field in listing.stdout.split("\0"):
        if not field.startswith(prefix):
            continue
        path = Path(os.path.realpath(field[len(prefix) :]))
        if path != real_top and path.is_relative_to(real_top):
            nested.append(path.relative_to(real_top).as_posix())
    return nested


def _dirty(top: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Count the paths `git status` lists.

    The checkout's other worktrees inside `top`, such as
    `.claude/worktrees/<name>`, are left out of `git status`, so an untracked
    folder that holds only worktrees is not counted.
    """
    excluded = [f":(exclude,top,literal){path}" for path in _nested_worktrees(top, env)]
    status = _git(top, env, "status", "--porcelain", "-z", "--", ".", *excluded)
    if status.returncode != 0:
        return {"dirty": None, "dirty_reason": _failure(status, "git status")}
    count = 0
    entries = status.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        count += 1
        if entry[0] in "RC" or entry[1:2] in ("R", "C"):
            index += 1  # A rename or copy names its source in the next entry.
    return {"dirty": {"value": count > 0, "paths": count}}


def _remote(top: Path, env: Mapping[str, str]) -> dict[str, Any]:
    listing = _git(top, env, "remote")
    if listing.returncode != 0:
        return {"remote": None, "remote_reason": _failure(listing, "git remote")}
    names = listing.stdout.split()
    if not names:
        return {"remote": None, "remote_reason": "the repository has no remote"}
    name = "origin" if "origin" in names else names[0]
    result: dict[str, Any] = {"name": name}
    url = _git(top, env, "remote", "get-url", name)
    if url.returncode == 0 and url.stdout.strip():
        result["url"] = url.stdout.strip()
        result["kind"] = remote_kind(result["url"])
    else:
        reason = _failure(url, "git remote get-url")
        result.update(url=None, url_reason=reason, kind=None, kind_reason=reason)
    return {"remote": result}


def _preparation(top: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Read preparation and the resolved main checkout, also from a worktree."""
    common = _git(top, env, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0 or not common.stdout.strip():
        return {"prepare": None, "prepare_reason": _failure(common, "git rev-parse")}
    listing = _git(top, env, "worktree", "list", "--porcelain", "-z")
    if listing.returncode != 0:
        return {
            "prepare": None,
            "prepare_reason": _failure(listing, "git worktree list"),
        }
    # Git lists the main checkout first, including from a linked worktree.
    first = listing.stdout.split("\0\0", 1)[0].split("\0")
    prefix = "worktree "
    if (
        not first[0].startswith(prefix)
        or not first[0][len(prefix) :]
        or "bare" in first
    ):
        return {
            "prepare": None,
            "prepare_reason": f"the repository of {top} has no main checkout",
        }
    try:
        repository = Path(first[0][len(prefix) :]).resolve()
        if not repository.is_dir():
            return {
                "prepare": None,
                "prepare_reason": f"the main checkout {repository} does not exist",
            }
        command = prepare.lookup(common.stdout.strip(), env=env)
    except (prepare.PreparationError, OSError, ValueError) as error:
        return {"prepare": None, "prepare_reason": _one_line(error)}
    state = "unset" if command is None else "none" if command == "" else "set"
    return {
        "prepare": {"state": state, "command": command, "repository": str(repository)}
    }


def _not_checkout(reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("top", "branch", "dirty", "remote", "files", "prepare"):
        result[field] = None
        result[f"{field}_reason"] = reason
    return result


def directory(folder: Path, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the git state of `folder`.

    Args:
        folder: An existing folder.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `path`, `git`, `top`, `branch` (`name` and `commit`), `dirty`
        (`value` and `paths`), `remote` (`name`, `url` and `kind`) and
        `files` (whether each of `CLAUDE.md` and `AGENTS.md` is at the top)
        and `prepare` (`state`, `command` and the main checkout `repository`).
    """
    environment = _env(env)
    result: dict[str, Any] = {"path": str(folder)}
    top = _git(folder, environment, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.stdout.strip():
        reason = _failure(top, "git rev-parse")
        if "not a git repository" in reason:
            result["git"] = False
            result.update(_not_checkout("not a git checkout"))
        else:
            result.update(git=None, git_reason=reason)
            result.update(_not_checkout(reason))
        return result
    top_path = Path(top.stdout.strip())
    result.update(git=True, top=str(top_path))
    result.update(_branch(top_path, environment))
    result.update(_dirty(top_path, environment))
    result.update(_remote(top_path, environment))
    result["files"] = {name: (top_path / name).is_file() for name in INSTRUCTION_FILES}
    result.update(_preparation(top_path, environment))
    return result


def resolve_folder(folder: str | None) -> Path:
    """Return `folder` (default: the current folder) as an absolute path.

    Args:
        folder: The folder given with `--dir`, or `None`.

    Returns:
        The absolute path.

    Raises:
        DirectoryMissingError: The folder does not exist or is not a folder.
    """
    try:
        path = Path(folder if folder is not None else ".").absolute()
    except OSError as error:
        message = f"the current folder cannot be read: {error}"
        raise DirectoryMissingError(message) from error
    if not path.exists():
        raise DirectoryMissingError(f"{path} does not exist")
    if not path.is_dir():
        raise DirectoryMissingError(f"{path} is not a folder")
    return path


def read(folder: str | None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return everything `emb ground` prints.

    Args:
        folder: The folder to read; `None` reads the current folder.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `machine`, `surface`, `worktree_base` and `directory`, each with a
        sibling `<field>_reason` where its value is `None`.

    Raises:
        DirectoryMissingError: The folder does not exist or is not a folder.
    """
    path = resolve_folder(folder)
    folder_state = directory(path, env)
    top = folder_state.get("top")
    result: dict[str, Any] = {}
    result.update(machine())
    result["surface"] = surface(env)
    result.update(worktree_base(Path(top) if top else None, env))
    result["directory"] = folder_state
    return result


def _null(reason: object) -> str:
    return f"null ({reason})"


def _branch_text(branch: dict[str, Any]) -> str:
    commit = branch["commit"]
    at = f"at {commit}" if commit else f"no commit ({branch['commit_reason']})"
    if branch["name"] is not None:
        return f"{branch['name']} {at}"
    if branch.get("name_reason") == "detached HEAD":
        return f"detached {at}"
    return f"{_null(branch['name_reason'])} {at}"


def _remote_text(remote: dict[str, Any]) -> str:
    if remote["url"] is None:
        return f"{remote['name']} {_null(remote['url_reason'])}"
    return f"{remote['name']} {remote['url']} ({remote['kind']})"


def lines(result: dict[str, Any]) -> list[str]:
    """Return one short `<item>: <value>` line per item of a `read` result.

    Args:
        result: What `read` returned.

    Returns:
        The lines, without line ends.
    """

    def item(
        name: str, holder: dict[str, Any], render: Callable[[Any], str] = str
    ) -> str:
        value = holder[name]
        if value is None:
            return f"{name}: {_null(holder[f'{name}_reason'])}"
        return f"{name}: {render(value)}"

    found = result["surface"]
    if found["entrypoint"] is None:
        surface_text = _null(found["entrypoint_reason"])
    else:
        surface_text = found["entrypoint"] + (" (desktop)" if found["desktop"] else "")
    folder = result["directory"]
    return [
        item("machine", result),
        f"surface: {surface_text}",
        item(
            "worktree_base", result, lambda base: f"{base['value']} from {base['file']}"
        ),
        f"directory: {folder['path']}",
        item("git", folder, lambda value: "yes" if value else "no"),
        item("top", folder),
        item("branch", folder, _branch_text),
        item(
            "dirty",
            folder,
            lambda dirty: (
                f"yes, {dirty['paths']} changed "
                f"{'path' if dirty['paths'] == 1 else 'paths'}"
                if dirty["value"]
                else "no"
            ),
        ),
        item("remote", folder, _remote_text),
        item(
            "files",
            folder,
            lambda files: ", ".join(
                f"{name} {'yes' if present else 'no'}"
                for name, present in files.items()
            ),
        ),
        item(
            "prepare",
            folder,
            lambda preparation: (
                f"{preparation['state']} for {preparation['repository']}"
                + (
                    f": {_one_line(preparation['command'])}"
                    if preparation["state"] == "set"
                    else ""
                )
            ),
        ),
    ]
