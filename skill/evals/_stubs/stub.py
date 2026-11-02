"""Eval stub for `emb`: log the call and print a canned or built reply.

`install.sh` copies this file and `bin/emb` into `<work>/.ember-eval/bin/`,
so the eval folder is the parent of this file's folder; `EMBER_EVAL_DIR`
overrides it.

Each call appends one JSON line to `<eval>/calls.log` (`seq`, `at`, `argv`,
`cwd`, `route`, `reply`, `exit`) and picks a reply by its route (`route()`)
and the number of earlier calls on that route: the first of
`replies/<route>.<n>.*` and `replies/<route>.*` that has any part. The parts:

    .out         standard output, a template
    .json        standard output when the call has --json, a template
    .err         standard error, a template
    .code        the exit code (default 0)
    .files/      a tree committed in the run's worktree (run-start, run-send)
    .untracked/  a tree left uncommitted in the run's worktree
    .reply       the turn's reply text, a template

Templates replace `{{ID}}`, `{{RUN}}`, `{{BRANCH}}`, `{{WORKTREE}}`, `{{TIP}}`,
`{{TARGET}}` and `{{EVAL}}`. A folder named `pycache` in a tree is placed as
`__pycache__` and a name ending in `.placeholder` loses that ending, which the
repository's ignore rules would otherwise leave out of the kit.

`run-start`, `run-send`, `run-recover`, `run-status`, `run-close`, `merge`,
`cleanup` and `ground` are built from the call and the repository unless the
reply gives `.out` or a non-zero `.code`. `help` prints the matching section
of `replies/help.out`. With `EMBER_EVAL_SETUP` set, a call is neither logged
nor counted, so a case can start a run while it is set up.

No stub reaches the network, starts a model or opens a window. The only
program it runs is git, on the case's own repository.
"""

import datetime
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

EVAL = Path(os.environ.get("EMBER_EVAL_DIR") or Path(__file__).resolve().parent.parent)
REPLIES = EVAL / "replies"
STATE = EVAL / "state.json"
LOG = EVAL / "calls.log"
PARTS = (".out", ".json", ".err", ".code", ".files", ".untracked", ".reply")
RUN_MODES = ("send", "screen", "interrupt", "attach", "close", "recover", "status")
OWNER_OPTIONS = ("--set", "--clear", "--once", "--clear-once")
REPLY_MARK = "--- reply ---"
SECONDS = 240
DEFAULT_REPLY = "Done.\n\nNot covered: nothing.\n"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Ember run",
    "GIT_AUTHOR_EMAIL": "run@ember.invalid",
    "GIT_COMMITTER_NAME": "Ember run",
    "GIT_COMMITTER_EMAIL": "run@ember.invalid",
}


class Reply(Exception):  # noqa: N818 - a reply, not an error
    """The reply a call ends with.

    Attributes:
        code: The exit code.
        out: Standard output.
        err: Standard error.
    """

    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        """Keep the reply.

        Args:
            code: The exit code.
            out: Standard output.
            err: Standard error.
        """
        super().__init__(code)
        self.code = code
        self.out = out
        self.err = err


def refuse(code: int, message: str, line: str) -> Reply:
    """Return an `emb run`-style failure.

    Args:
        code: The exit code.
        message: The message after `emb run: `.
        line: The `next:` or `owner:` line.

    Returns:
        The reply.
    """
    return Reply(code, err=f"emb run: {message}\n{line}\n")


# Arguments -----------------------------------------------------------------


def option(args: list[str], name: str) -> str | None:
    """Return the last value of `--name VALUE` or `--name=VALUE`.

    Args:
        args: The arguments.
        name: The option.

    Returns:
        The value, or `None`.
    """
    found = None
    for index, word in enumerate(args):
        if word == name and index + 1 < len(args):
            found = args[index + 1]
        elif word.startswith(name + "="):
            found = word[len(name) + 1 :]
    return found


def options(args: list[str], name: str) -> list[str]:
    """Return every value of a repeatable option.

    Args:
        args: The arguments.
        name: The option.

    Returns:
        The values in order.
    """
    found = []
    for index, word in enumerate(args):
        if word == name and index + 1 < len(args):
            found.append(args[index + 1])
        elif word.startswith(name + "="):
            found.append(word[len(name) + 1 :])
    return found


def has(args: list[str], name: str) -> bool:
    """Return whether an option is given.

    Args:
        args: The arguments.
        name: The option.

    Returns:
        Whether `name` or `name=...` is among them.
    """
    return any(word == name or word.startswith(name + "=") for word in args)


def route(args: list[str]) -> str:
    """Return the route of an `emb` call.

    Args:
        args: The arguments after `emb`.

    Returns:
        The route name.
    """
    if not args or has(args, "--help") or "-h" in args:
        return "help"
    command, rest = args[0], args[1:]
    if command == "choices":
        if any(has(rest, name) for name in OWNER_OPTIONS):
            return "choices-owner"
        if has(rest, "--claim"):
            return "choices-claim"
        if has(rest, "--finish"):
            return "choices-finish"
        return "choices-read-id" if has(rest, "--id") else "choices-read"
    if command == "engines":
        return "engines-models" if has(rest, "--models") else "engines"
    if command == "run":
        for mode in RUN_MODES:
            if has(rest, f"--{mode}"):
                return f"run-{mode}"
        return "run-start"
    if command in ("ground", "merge", "cleanup", "deps", "skill"):
        return command
    return "unknown"


# Replies -------------------------------------------------------------------


def reply_base(name: str, count: int) -> Path | None:
    """Return the reply for the nth call on a route.

    Args:
        name: The route.
        count: Which call on the route this is, from 1.

    Returns:
        `replies/<name>.<count>` or `replies/<name>` without a part suffix,
        the first that has a part, or `None`.
    """
    for base in (f"{name}.{count}", name):
        if any((REPLIES / (base + suffix)).exists() for suffix in PARTS):
            return REPLIES / base
    return None


def part(base: Path | None, suffix: str) -> Path | None:
    """Return a reply's part when it exists.

    Args:
        base: The reply, or `None`.
        suffix: The part's suffix.

    Returns:
        The part's path, or `None`.
    """
    if base is None:
        return None
    path = Path(str(base) + suffix)
    return path if path.exists() else None


def render(text: str, variables: dict[str, str]) -> str:
    """Fill a template.

    Args:
        text: The template.
        variables: The values by name.

    Returns:
        The text with each `{{NAME}}` replaced; an unknown name becomes
        `<name>`.
    """
    return re.sub(
        r"\{\{([A-Z_]+)\}\}",
        lambda match: variables.get(match.group(1), f"<{match.group(1).lower()}>"),
        text,
    )


def read_part(
    base: Path | None, suffix: str, variables: dict[str, str], default: str = ""
) -> str:
    """Return a template part, filled.

    Args:
        base: The reply, or `None`.
        suffix: The part's suffix.
        variables: The template values.
        default: The text when the part does not exist.

    Returns:
        The filled text.
    """
    path = part(base, suffix)
    return render(path.read_text(encoding="utf-8"), variables) if path else default


def code_of(base: Path | None) -> int:
    """Return a reply's exit code.

    Args:
        base: The reply, or `None`.

    Returns:
        The `.code` part as a number, 0 without one.
    """
    path = part(base, ".code")
    return int(path.read_text().strip() or 0) if path else 0


def canned(base: Path | None) -> bool:
    """Return whether a reply replaces a built route.

    Args:
        base: The reply, or `None`.

    Returns:
        Whether it has `.out` or a non-zero `.code`.
    """
    return part(base, ".out") is not None or code_of(base) != 0


def canned_reply(base: Path | None, variables: dict[str, str], as_json: bool) -> Reply:
    """Return a canned reply.

    Args:
        base: The reply.
        variables: The template values.
        as_json: Whether the call has `--json`.

    Returns:
        The reply.
    """
    out = read_part(base, ".out", variables)
    if as_json and part(base, ".json"):
        out = read_part(base, ".json", variables)
    return Reply(code_of(base), out, read_part(base, ".err", variables))


# State and git -------------------------------------------------------------


def load_state() -> dict[str, Any]:
    """Read the stub's state.

    Returns:
        `counters` by route and `runs` by id.
    """
    try:
        state = json.loads(STATE.read_text())
    except OSError, ValueError:
        state = {}
    state.setdefault("counters", {})
    state.setdefault("runs", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    """Write the stub's state.

    Args:
        state: The state.
    """
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def git(
    folder: Path | str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run git in a folder.

    Args:
        folder: Where git runs.
        *args: The git command.
        check: Turn a failure into exit 70.

    Returns:
        The finished process.

    Raises:
        Reply: `check` is set and git failed.
    """
    environment = {**os.environ, **IDENTITY}
    result = subprocess.run(
        ["git", "-C", str(folder), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if check and result.returncode:
        raise Reply(70, err=f"git {' '.join(args)}: {result.stderr.strip()}\n")
    return result


def out(folder: Path | str, *args: str) -> str:
    """Return git's standard output, stripped.

    Args:
        folder: Where git runs.
        *args: The git command.

    Returns:
        The output.
    """
    return git(folder, *args).stdout.strip()


def top_of(folder: Path) -> Path | None:
    """Return the top of the checkout holding a folder.

    Args:
        folder: The folder.

    Returns:
        The top, or `None` outside a checkout.
    """
    if not folder.is_dir():
        return None
    result = git(folder, "rev-parse", "--show-toplevel", check=False)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def main_checkout(repo: Path) -> Path:
    """Return the repository's main checkout.

    Args:
        repo: A folder in the repository.

    Returns:
        The first entry of `git worktree list`.
    """
    first = out(repo, "worktree", "list", "--porcelain").splitlines()[0]
    return Path(first.split(" ", 1)[1])


def branch_exists(repo: Path, branch: str) -> bool:
    """Return whether a local branch exists.

    Args:
        repo: A folder in the repository.
        branch: The branch's short name.

    Returns:
        Whether `refs/heads/<branch>` exists.
    """
    result = git(
        repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False
    )
    return result.returncode == 0


def dirty(folder: Path) -> bool:
    """Return whether a worktree has changes or untracked files.

    Args:
        folder: The worktree.

    Returns:
        Whether `git status --porcelain` lists anything.
    """
    return bool(out(folder, "status", "--porcelain", "--untracked-files=all"))


def copy_tree(source: Path, target: Path) -> None:
    """Copy a reply tree into a folder, with the kit's renames.

    A folder `pycache` is placed as `__pycache__`, and a file name ending in
    `.placeholder` loses that ending, since the repository's ignore rules
    would leave both out of the kit.

    Args:
        source: The tree.
        target: The folder.
    """
    for path in sorted(source.rglob("*")):
        if path.is_file():
            relative = Path(
                *(
                    "__pycache__" if piece == "pycache" else piece
                    for piece in path.relative_to(source).parts
                )
            )
            relative = relative.with_name(relative.name.removesuffix(".placeholder"))
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


def menu_row(name: str) -> dict[str, Any] | None:
    """Return a menu row from `replies/menu.json`.

    Args:
        name: `single.writer` or `single.reviewer`.

    Returns:
        The row, or `None`.
    """
    try:
        rows = json.loads((REPLIES / "menu.json").read_text())
    except OSError, ValueError:
        return None
    row = rows.get(name)
    return row if isinstance(row, dict) else None


# emb run -------------------------------------------------------------------


def new_run_id(engine: str) -> str:
    """Return a run id as `emb run` makes one.

    Args:
        engine: The engine.

    Returns:
        `<engine>-<yyyymmdd>-<hhmmss>-<6 hex digits>`, in UTC.
    """
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    return f"{engine}-{stamp}-{secrets.token_hex(3)}"


def turn_block(run: dict[str, Any], files: int, commit: str | None) -> str:
    """Return the lines a turn prints.

    Args:
        run: The run's record.
        files: How many files the turn's commit changed.
        commit: The turn's commit, or `None`.

    Returns:
        The summary line, the run's lines and the reply.
    """
    reply = Path(run["reply"]).read_text(encoding="utf-8")
    lines = [
        f"{run['engine']} {run['model']} · {run['effort'] or 'default'} · "
        f"{SECONDS}s · exit 0 · {files} files · turn {run['turn']}",
        f"run {run['id']}",
        f"worktree {run['worktree']}",
        f"branch {run['branch'] or '-'}",
        f"commit {commit or '-'}",
        "window -",
        f"reply {run['reply']}",
        REPLY_MARK,
        reply.rstrip("\n"),
    ]
    return "\n".join(lines) + "\n"


def do_turn(run: dict[str, Any], base: Path | None, variables: dict[str, str]) -> str:
    """Play one turn: commit the reply's files, write its reply text.

    Args:
        run: The run's record, updated in place.
        base: The reply.
        variables: The template values.

    Returns:
        The turn's printed lines.
    """
    run["turn"] += 1
    worktree = Path(run["worktree"])
    commit = None
    files = 0
    tree = part(base, ".files")
    if tree is not None and not run["read_only"]:
        copy_tree(tree, worktree)
        git(worktree, "add", "-A")
        if git(worktree, "diff", "--cached", "--quiet", check=False).returncode:
            subject = run["subject"][:72]
            if run["turn"] > 1:
                subject += f" (turn {run['turn']})"
            message = subject
            if run["trailers"]:
                message += "\n\n" + "\n".join(run["trailers"])
            names = out(worktree, "diff", "--cached", "--name-only")
            files = len(names.splitlines())
            git(worktree, "commit", "-q", "--no-verify", "-m", message)
            commit = out(worktree, "rev-parse", "HEAD")
            run["commits"].append(commit)
    untracked = part(base, ".untracked")
    if untracked is not None:
        copy_tree(untracked, worktree)
    folder = EVAL / "runs" / run["id"]
    folder.mkdir(parents=True, exist_ok=True)
    reply = folder / f"turn-{run['turn']}.md"
    variables.update(TIP=commit or variables.get("TIP", ""))
    reply.write_text(read_part(base, ".reply", variables, DEFAULT_REPLY))
    run.update(reply=str(reply), status="waiting", turn_state="committed")
    return turn_block(run, files, commit)


def run_start(
    args: list[str], state: dict[str, Any], base: Path | None, variables: dict[str, str]
) -> Reply:
    """Start a run as `emb run --engine ... --dir ... --task-file ...` does.

    Args:
        args: The arguments after `run`.
        state: The stub's state.
        base: The reply.
        variables: The template values.

    Returns:
        The reply.
    """
    engine, folder, task = (
        option(args, name) for name in ("--engine", "--dir", "--task-file")
    )
    if engine is None or folder is None or task is None:
        missing = [
            name
            for name, value in (
                ("--engine", engine),
                ("--dir", folder),
                ("--task-file", task),
            )
            if value is None
        ]
        return refuse(
            64, f"a new run needs {', '.join(missing)}", "next: emb run --help"
        )
    if engine not in ("codex", "opencode", "claude"):
        return refuse(64, f"unknown engine {engine}", "next: emb run --help")
    brief_path = Path(task).expanduser()
    directory = Path(folder).expanduser()
    if not brief_path.is_file() or not directory.is_dir():
        gone = task if not brief_path.is_file() else folder
        return refuse(66, f"{gone} does not exist", "next: a start with existing paths")
    lines = [line.strip() for line in brief_path.read_text().splitlines()]
    subject = next((line for line in lines if line), None)
    if subject is None:
        return refuse(64, f"{task} has no non-empty line", "next: emb run --help")
    repo = top_of(directory)
    if repo is None:
        return refuse(
            65, f"{folder} is not in a git checkout", "next: a start inside a checkout"
        )
    read_only = has(args, "--read-only")
    row_name = "single.reviewer" if read_only else "single.writer"
    model = option(args, "--model")
    effort = option(args, "--effort")
    if model is None:
        row = menu_row(row_name)
        if row is None or row.get("state") != "set" or row.get("engine") != engine:
            state_text = row.get("state") if row else "ask"
            return refuse(
                64,
                f"the menu row {row_name} is {state_text} for {engine}; give --model",
                "next: emb run --help",
            )
        model = row["model"]
        if effort is None:
            effort = row.get("effort")
    elif effort is None:
        row = menu_row(row_name)
        if row and row.get("engine") == engine and row.get("model") == model:
            effort = row.get("effort")
    run_id = new_run_id(engine)
    head = out(directory, "rev-parse", "HEAD")
    if read_only:
        worktree, branch = repo, None
    else:
        main = main_checkout(repo)
        common = Path(
            out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        if ".ember/" not in current.splitlines():
            exclude.write_text(
                current
                + ("" if current.endswith("\n") or not current else "\n")
                + ".ember/\n"
            )
        worktree = main / ".ember" / "worktrees" / run_id
        branch = f"ember/{engine}/{run_id}"
        git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), head)
    run = {
        "id": run_id,
        "engine": engine,
        "model": model,
        "effort": effort,
        "read_only": read_only,
        "repo": str(repo),
        "worktree": str(worktree),
        "branch": branch,
        "base": head,
        "trailers": options(args, "--trailer"),
        "claim": option(args, "--claim"),
        "status": "running",
        "turn": 0,
        "turn_state": "sent",
        "reply": None,
        "commits": [],
        "subject": subject,
        "started_at": datetime.datetime.now(datetime.UTC).timestamp(),
    }
    variables.update(RUN=run_id, BRANCH=branch or "-", WORKTREE=str(worktree))
    block = do_turn(run, base, variables)
    state["runs"][run_id] = run
    return Reply(0, f"run {run_id}\n{block}", read_part(base, ".err", variables))


def known_run(
    state: dict[str, Any], run_id: str | None, variables: dict[str, str]
) -> dict[str, Any]:
    """Return a run's record and set its template values.

    Args:
        state: The stub's state.
        run_id: The run id from the call.
        variables: The template values, updated.

    Returns:
        The record.

    Raises:
        Reply: The run is not known (66).
    """
    run = state["runs"].get(run_id or "")
    if run is None:
        raise refuse(66, f"no run {run_id}", "next: emb run --status")
    commits = run["commits"]
    variables.update(
        RUN=run["id"],
        BRANCH=run["branch"] or "-",
        WORKTREE=run["worktree"],
        TIP=commits[-1] if commits else run["base"],
    )
    return run


def run_other(
    mode: str,
    args: list[str],
    state: dict[str, Any],
    base: Path | None,
    variables: dict[str, str],
) -> Reply:
    """Answer `emb run --<mode> RUN` for a mode the stub builds.

    Args:
        mode: `send`, `recover`, `close` or `status`.
        args: The arguments after `run`.
        state: The stub's state.
        base: The reply.
        variables: The template values.

    Returns:
        The reply.
    """
    run_id = option(args, f"--{mode}")
    if mode == "status" and not run_id:
        runs = sorted(state["runs"].values(), key=lambda item: item["started_at"])
        if has(args, "--json"):
            listing = [{**run, "session_state": "live"} for run in runs]
            return Reply(0, json.dumps(listing, indent=2) + "\n")
        lines = [
            f"{run['id']}  {run['status']}  live  turn {run['turn']}  {run['subject']}"
            for run in runs
        ]
        return Reply(0, "".join(line + "\n" for line in lines))
    if canned(base):
        if run_id in state["runs"]:
            known_run(state, run_id, variables)
        return canned_reply(base, variables, has(args, "--json"))
    run = known_run(state, run_id, variables)
    if mode == "status":
        document = {**run, "session_state": "live"}
        if has(args, "--json"):
            return Reply(0, json.dumps(document, indent=2) + "\n")
        return Reply(0, "".join(f"{key}: {value}\n" for key, value in document.items()))
    if mode == "close":
        run["status"] = "closed"
        return Reply(0, f"closed {run['id']}\n")
    if run["status"] == "closed":
        return refuse(72, f"the run {run['id']} is closed", "next: a new start")
    if mode == "send":
        message = option(args, "--message-file")
        if message is None:
            return refuse(
                64, "--send and --message-file go together", "next: emb run --help"
            )
        if not Path(message).expanduser().is_file():
            return refuse(
                66, f"{message} does not exist", "next: a start with existing paths"
            )
        return Reply(
            0, do_turn(run, base, variables), read_part(base, ".err", variables)
        )
    commits = run["commits"]
    return Reply(0, turn_block(run, 0, commits[-1] if commits else None))


# emb merge and emb cleanup -------------------------------------------------


def cleanup_branch(repo: Path, branch: str, state: dict[str, Any]) -> str:
    """Clean up one contained branch as `emb cleanup` does.

    Args:
        repo: A folder in the repository.
        branch: The branch, contained in its target.
        state: The stub's state.

    Returns:
        The `removed` or `kept` line.
    """
    match = re.fullmatch(r"ember/(codex|opencode|claude)/(.+)", branch)
    if match is None:
        return f"kept {branch}: not an Ember run branch; next: git branch -d {branch}"
    run_id = match.group(2)
    main = main_checkout(repo)
    worktree = main / ".ember" / "worktrees" / run_id
    parts = []
    if worktree.exists() and dirty(worktree):
        return (
            f"kept {branch}: the worktree {worktree} has changes; "
            f"next: git -C {worktree} status --short"
        )
    run = state["runs"].get(run_id)
    if run is not None and run["status"] != "closed":
        run["status"] = "closed"
        parts.append(f"closed {run_id}")
    tip = out(repo, "rev-parse", f"refs/heads/{branch}")
    if worktree.exists():
        git(main, "worktree", "remove", "--", str(worktree))
        parts.append(f"removed worktree {worktree}")
    git(main, "worktree", "prune")
    git(repo, "update-ref", "-d", f"refs/heads/{branch}", tip)
    parts.append(f"deleted at {tip}")
    return f"removed {branch}: {'; '.join(parts)}"


def contained(repo: Path, branch: str, target: str) -> bool:
    """Return whether a branch is an ancestor of the target.

    Args:
        repo: A folder in the repository.
        branch: The branch.
        target: The target.

    Returns:
        Whether the target contains the branch's tip.
    """
    result = git(
        repo,
        "merge-base",
        "--is-ancestor",
        f"refs/heads/{branch}",
        f"refs/heads/{target}",
        check=False,
    )
    return result.returncode == 0


def merge_failure(code: int, message: str, line: str) -> Reply:
    """Return an `emb merge` failure.

    Args:
        code: The exit code.
        message: The message.
        line: The `next:` or `owner:` line.

    Returns:
        The reply.
    """
    return Reply(code, err=f"emb merge: {message}\n{line}\n")


def merge(args: list[str], state: dict[str, Any], variables: dict[str, str]) -> Reply:
    """Merge a reviewed branch and clean it up as `emb merge` does.

    Args:
        args: The arguments after `merge`.
        state: The stub's state.
        variables: The template values.

    Returns:
        The reply.
    """
    words = [
        word
        for index, word in enumerate(args)
        if not word.startswith("-")
        and (index == 0 or args[index - 1] not in ("--into", "--verdict", "--dir"))
    ]
    branch = words[0] if words else None
    target = option(args, "--into")
    usage = "next: emb merge --help"
    if branch is None or target is None:
        return merge_failure(64, "give a branch and --into TARGET", usage)
    if option(args, "--verdict") != "keep":
        return merge_failure(
            64, "only a keep verdict merges; nothing was changed", usage
        )
    directory = Path(option(args, "--dir") or ".").expanduser()
    repo = top_of(directory)
    if repo is None:
        return merge_failure(65, f"{directory} is not in a git checkout", usage)
    for name in (branch, target):
        if not branch_exists(repo, name):
            return merge_failure(
                65, f"no local branch {name}", f"next: git -C {repo} branch --list"
            )
    if branch == target:
        return merge_failure(64, "the branch and the target are the same", usage)
    worktree = None
    current = None
    for line in out(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line.split(" ", 1)[1]
        elif line == f"branch refs/heads/{target}":
            worktree = current
    if worktree is None:
        return merge_failure(
            75,
            f"{target} is checked out in no worktree",
            f"next: git -C {repo} switch {target}",
        )
    tip = out(repo, "rev-parse", f"refs/heads/{branch}")
    variables.update(BRANCH=branch, TARGET=target, TIP=tip)
    if contained(repo, branch, target):
        head = out(repo, "rev-parse", f"refs/heads/{target}")
        first = f"already contained: {branch} at {tip} in {target} at {head}"
    else:
        if dirty(Path(worktree)):
            return merge_failure(
                75,
                f"the tree of {worktree} is not clean",
                f"next: git -C {worktree} status --short",
            )
        message = f"Merge branch '{branch}'"
        if target not in ("main", "master"):
            message += f" into {target}"
        result = git(
            worktree,
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            message,
            f"refs/heads/{branch}",
            check=False,
        )
        if result.returncode:
            git(worktree, "merge", "--abort", check=False)
            return merge_failure(
                75,
                f"merging {branch} into {target} conflicted; the merge was aborted",
                f"next: git -C {worktree} merge --no-ff {branch} (resolve by hand)",
            )
        commit = out(worktree, "rev-parse", "HEAD")
        first = f"merged {branch} at {tip} into {target}: {commit}"
    lines = [first, cleanup_branch(repo, branch, state)]
    return Reply(0, "\n".join(lines) + "\n")


def cleanup(args: list[str], state: dict[str, Any], variables: dict[str, str]) -> Reply:
    """Clean up contained run branches as `emb cleanup` does.

    Args:
        args: The arguments after `cleanup`.
        state: The stub's state.
        variables: The template values.

    Returns:
        The reply.
    """
    target = option(args, "--merged-into")
    if target is None:
        return Reply(
            64, err="emb cleanup: --merged-into is required\nnext: emb cleanup --help\n"
        )
    variables["TARGET"] = target
    directory = Path(option(args, "--dir") or ".").expanduser()
    repo = top_of(directory)
    if repo is None or not branch_exists(repo, target):
        return Reply(
            65, err=f"emb cleanup: no local branch {target}\nnext: git branch --list\n"
        )
    only = option(args, "--branch")
    names = (
        [only]
        if only
        else out(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/ember/"
        ).splitlines()
    )
    lines = []
    for name in names:
        if not name or not branch_exists(repo, name):
            continue
        if not contained(repo, name, target):
            lines.append(
                f"kept {name}: not contained in {target}; next: emb merge {name} "
                f"--into {target} --verdict keep (after a Keep verdict)"
            )
        elif has(args, "--dry-run"):
            lines.append(f"would remove {name}: deletes {name}")
        else:
            lines.append(cleanup_branch(repo, name, state))
    if not lines:
        return Reply(0, err=f"emb cleanup: no Ember run branch against {target}\n")
    return Reply(0, "\n".join(lines) + "\n")


# emb ground ----------------------------------------------------------------


def worktree_base(top: Path | None) -> tuple[dict[str, str] | None, str | None]:
    """Read Claude Code's `worktree.baseRef` as `emb ground` does.

    Args:
        top: The top of the checkout, or `None`.

    Returns:
        The value and its file, or `None` and the reason.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    files = [Path(config) / "settings.json"]
    if top is not None:
        files += [
            top / ".claude" / "settings.json",
            top / ".claude" / "settings.local.json",
        ]
    found = None
    for path in files:
        try:
            data = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        value = (
            data.get("worktree", {}).get("baseRef") if isinstance(data, dict) else None
        )
        if isinstance(value, str) and value:
            found = {"value": value, "file": str(path)}
    return found, None if found else "no settings file sets worktree.baseRef"


def ground(args: list[str]) -> Reply:
    """Print what `emb ground` prints, read from the working directory.

    Args:
        args: The arguments after `ground`.

    Returns:
        The reply.
    """
    directory = Path(option(args, "--dir") or ".").expanduser()
    if not directory.is_dir():
        return Reply(
            66,
            err=f"emb ground: {directory} is not a folder\n"
            "next: emb ground --dir <an existing folder>\n",
        )
    directory = directory.resolve()
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT") or None
    top = top_of(directory)
    base, base_reason = worktree_base(top)
    data: dict[str, Any] = {
        "machine": socket.gethostname(),
        "surface": {"entrypoint": entry, "desktop": entry == "claude-desktop"},
        "worktree_base": base,
    }
    if base is None:
        data["worktree_base_reason"] = base_reason
    info: dict[str, Any] = {"path": str(directory), "git": top is not None}
    if top is None:
        for key in ("top", "branch", "dirty", "remote", "files"):
            info[key] = None
            info[f"{key}_reason"] = "not a git checkout"
    else:
        name = git(
            top, "symbolic-ref", "-q", "--short", "HEAD", check=False
        ).stdout.strip()
        commit = git(
            top, "rev-parse", "-q", "--verify", "HEAD", check=False
        ).stdout.strip()
        listed = out(top, "status", "--porcelain").splitlines()
        url = git(top, "remote", "get-url", "origin", check=False).stdout.strip()
        info.update(
            top=str(top),
            branch={"name": name or None, "commit": commit or None},
            dirty={"value": bool(listed), "paths": len(listed)},
        )
        if url:
            kind = "github" if re.search(r"github\.com[:/]", url) else "other"
            info["remote"] = {"name": "origin", "url": url, "kind": kind}
        else:
            info["remote"] = None
            info["remote_reason"] = "the repository has no remote"
        info["files"] = {
            name: (top / name).is_file() for name in ("CLAUDE.md", "AGENTS.md")
        }
    data["directory"] = info
    if has(args, "--json"):
        return Reply(0, json.dumps(data, indent=2) + "\n")
    return Reply(0, "".join(line + "\n" for line in ground_lines(data)))


def dirty_text(dirty: dict[str, Any]) -> str:
    """Return the `dirty` item's text.

    Args:
        dirty: `value` and `paths`.

    Returns:
        `no`, or `yes, <n> changed path(s)`.
    """
    if not dirty["value"]:
        return "no"
    count = dirty["paths"]
    return f"yes, {count} changed {'path' if count == 1 else 'paths'}"


def ground_lines(data: dict[str, Any]) -> list[str]:
    """Return `emb ground`'s text lines.

    Args:
        data: The JSON object.

    Returns:
        One line per item.
    """
    surface = data["surface"]
    entry = surface["entrypoint"] or "null (CLAUDE_CODE_ENTRYPOINT is unset)"
    if surface["desktop"]:
        entry += " (desktop)"
    base = data["worktree_base"]
    based = (
        f"{base['value']} from {base['file']}"
        if base
        else f"null ({data.get('worktree_base_reason')})"
    )
    info = data["directory"]
    lines = [
        f"machine: {data['machine']}",
        f"surface: {entry}",
        f"worktree_base: {based}",
        f"directory: {info['path']}",
        f"git: {'yes' if info['git'] else 'no'}",
    ]
    if info["top"] is None:
        for key in ("top", "branch", "dirty", "remote", "files"):
            lines.append(f"{key}: null (not a git checkout)")
        return lines
    branch = info["branch"]
    remote = info["remote"]
    files = info["files"]
    lines += [
        f"top: {info['top']}",
        f"branch: {branch['name']} at {branch['commit']}",
        f"dirty: {dirty_text(info['dirty'])}",
        f"remote: {remote['name']} {remote['url']} ({remote['kind']})"
        if remote
        else f"remote: null ({info.get('remote_reason')})",
        "files: "
        + ", ".join(
            f"{name} {'yes' if present else 'no'}" for name, present in files.items()
        ),
    ]
    return lines


# Help ----------------------------------------------------------------------


def help_reply(args: list[str]) -> Reply:
    """Print the captured help of the command the call names.

    Args:
        args: The arguments after `emb`.

    Returns:
        The section of `replies/help.out` for the command, or `emb --help`'s.
    """
    words = [word for word in args if not word.startswith("-")]
    try:
        text = (REPLIES / "help.out").read_text(encoding="utf-8")
    except OSError:
        return Reply(70, err="emb eval stub: replies/help.out is missing\n")
    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines(keepends=True):
        found = re.fullmatch(r"==> (emb(?: [a-z]+)*) --help <==\n?", line)
        if found:
            current = found.group(1)
            sections[current] = ""
        elif current is not None:
            sections[current] += line
    while words and " ".join(["emb", *words]) not in sections:
        words.pop()
    key = " ".join(["emb", *words])
    if not args:
        return Reply(64, err=sections.get("emb", ""))
    return Reply(0, sections.get(key, ""))


# Main ----------------------------------------------------------------------


def dispatch(
    name: str,
    args: list[str],
    state: dict[str, Any],
    base: Path | None,
    variables: dict[str, str],
) -> Reply:
    """Build the reply for a call.

    Args:
        name: The route.
        args: The arguments after `emb`.
        state: The stub's state.
        base: The reply files for this call, or `None`.
        variables: The template values.

    Returns:
        The reply.
    """
    rest = args[1:]
    as_json = has(rest, "--json")
    if name == "help":
        return help_reply(args)
    if name == "choices-claim" and option(rest, "--id") is None:
        return Reply(64, err="emb choices: --claim needs --id\n")
    if name == "unknown":
        return Reply(
            64,
            err=f"usage: emb [-h] [--version] <command> ...\n"
            f"emb: argument <command>: invalid choice: {args[0]!r}\n",
        )
    if name.startswith("run-"):
        mode = name.removeprefix("run-")
        if mode == "start":
            return (
                canned_reply(base, variables, as_json)
                if canned(base)
                else run_start(rest, state, base, variables)
            )
        if mode in ("send", "recover", "close", "status"):
            return run_other(mode, rest, state, base, variables)
        run_id = option(rest, f"--{mode}")
        if run_id in state["runs"]:
            known_run(state, run_id, variables)
    if not canned(base):
        if name == "merge":
            return merge(rest, state, variables)
        if name == "cleanup":
            return cleanup(rest, state, variables)
        if name == "ground":
            return ground(rest)
    if base is None:
        return Reply(
            69, err=f"emb eval stub: no reply for {name} (emb {' '.join(args)})\n"
        )
    return canned_reply(base, variables, as_json)


def main(argv: list[str]) -> int:
    """Answer one `emb` call.

    Args:
        argv: The arguments after `emb`.

    Returns:
        The exit code.
    """
    setup = bool(os.environ.get("EMBER_EVAL_SETUP"))
    name = route(argv)
    state = load_state()
    count = state["counters"].get(name, 0) + (0 if setup else 1)
    if not setup:
        state["counters"][name] = count
    base = reply_base(name, max(count, 1))
    stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    variables = {
        "EVAL": str(EVAL),
        "ID": option(argv, "--id")
        or option(argv, "--finish")
        or option(argv, "--claim")
        or "",
        "TARGET": option(argv, "--into") or option(argv, "--merged-into") or "",
    }
    try:
        reply = dispatch(name, argv, state, base, variables)
    except Reply as caught:
        reply = caught
    save_state(state)
    if not setup:
        entry = {
            "seq": sum(state["counters"].values()),
            "at": stamp.replace("+00:00", "Z"),
            "argv": argv,
            "cwd": os.getcwd(),
            "route": name,
            "reply": base.name if base else None,
            "exit": reply.code,
        }
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")
    sys.stdout.write(reply.out)
    sys.stderr.write(reply.err)
    return reply.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
