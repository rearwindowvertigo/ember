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
    .files/      a tree committed in a run or flow worktree
    .untracked/  a tree left uncommitted in the run's worktree
    .reply       the turn's reply text, a template
    .summary     a panel's summary text, a template

Templates replace `{{ID}}`, `{{RUN}}`, `{{BRANCH}}`, `{{WORKTREE}}`,
`{{WORKTREES}}`, `{{TIP}}`, `{{HEAD}}`, `{{SHORT_HEAD}}`,
`{{TARGET}}`, `{{EVAL}}`, `{{FLOW}}`, `{{PANEL}}`, `{{PLAN}}`, `{{DIR}}`
`{{SUMMARY}}` and `{{SKILL_VERSION}}` (the claim's `--skill` value). A folder
named `pycache` in a tree is placed as
`__pycache__` and a name ending in `.placeholder` loses that ending, which the
repository's ignore rules would otherwise leave out of the kit.

`run-start`, `run-send`, `run-recover`, `run-status`, `run-close`,
`run-answered`, `merge`,
`cleanup` and `ground` are built from the call and the repository unless the
reply gives `.out` or a non-zero `.code`. `flow-start` also builds a branch
when its reply has neither override. `panel-start` writes a summary whenever
its reply has `.summary`, including with a non-zero `.code`.
`help` prints the matching section
of `replies/help.out`.

The built routes print the `next:` line the real command prints, derived
from the stub's own run state and git as `ember.runs.steps.derive` derives
it, never from a template. `run-start`, `run-send`, `run-recover` and
`run-close` print it on standard error at exit 0 (a dry run prints none);
`run-status RUN` prints `step:` on standard output, in JSON `step` and
`next`, and the line on standard error; the `run-status` list gives each
record `step` and `next` and prints no next line. `merge` prints the newest
matching run's line on standard error and gives it as `next` in its JSON;
a kept branch, a branch with no run record and a flow branch give none.
`cleanup --claim <id>` prints `next: emb choices --finish <id>` once on
standard error unless a kept result holds that claim or `--dry-run` is set.
The `verify_branch.py` path comes from `ember.skill.link_path(os.environ)`.
A run records `base_branch`, the branch checked out in its `--dir` checkout
at start (null when detached). An exit-0 `choices --finish` records its
claim as `gone` in `state.json`'s `claims`, and a run whose claim is `gone`
derives no finish line. Stored `step` and `next` keys in a state document are
ignored. Canned starts with a status JSON retain that record with the call's
claim, subject and trailers; later status calls derive their output from it.
Owner actions suppress carry-on next lines and leave the run on the `owner`
step. Status repeats the pending owner lines. `run-answered` clears the wait;
a later turn or all suggested sandbox read paths also end it.
Run, merge, cleanup and flow replies append the production `report:` lines
after next or owner lines, including canned replies. Flow watch JSON also
carries the report list. No `.reply` file holds a next line.
With `EMBER_EVAL_SETUP` set, a call is neither logged
nor counted, so a case can start a run while it is set up.
`run-dry-run` checks the brief and repository and resolves the worker
without launch artifacts; a non-zero `.code` overrides it. Its default
`.out` and `.json` fixtures describe the default writer.
With `<eval>/real-flow-planner` present, `flow-dry-run` uses the production
planner and formatter and saves the submitted plan at `<eval>/flow-plan.json`.

No stub reaches the network, starts a model or opens a window. The only
program it runs is git, on the case's own repository.
"""

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

# The installer copies these production modules beside the isolated stub.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ember import branches, skill  # noqa: E402
from ember.flows import plan as flow_plan  # noqa: E402
from ember.gapcheck import brief as gap_brief  # noqa: E402
from ember.gapcheck import plan as gap_plan  # noqa: E402
from ember.gapcheck import previous as gap_previous  # noqa: E402
from ember.gapcheck import reply as gap_reply  # noqa: E402
from ember.runs import exits  # noqa: E402
from ember.runs.core import WorkerSelection  # noqa: E402

EVAL = Path(os.environ.get("EMBER_EVAL_DIR") or Path(__file__).resolve().parent.parent)
REPLIES = EVAL / "replies"
STATE = EVAL / "state.json"
LOG = EVAL / "calls.log"
PARTS = (".out", ".json", ".err", ".code", ".files", ".untracked", ".reply", ".summary")
RUN_MODES = (
    "send",
    "screen",
    "interrupt",
    "attach",
    "close",
    "recover",
    "status",
    "answered",
)
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
        if word == name:
            found = (
                args[index + 1]
                if index + 1 < len(args) and not args[index + 1].startswith("--")
                else None
            )
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
        return "run-dry-run" if has(rest, "--dry-run") else "run-start"
    if command in ("ground", "merge", "cleanup", "deps", "skill"):
        return command
    modes = {
        "gapcheck": ("answer", "brief", "reply"),
        "flow": ("dry-run", "start", "watch", "status", "lead", "resume"),
        "panel": ("dry-run", "status", "recover"),
    }
    if command in modes:
        for mode in modes[command]:
            if has(rest, f"--{mode}"):
                return f"{command}-{mode}"
        return "panel-start" if command == "panel" else command
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
        `counters` by route and `runs`, `flows`, `panels` and `claims` by id;
        `claims` maps a claim id to `gone` once `emb choices --finish` ran.
    """
    try:
        state = json.loads(STATE.read_text())
    except OSError, ValueError:
        state = {}
    state.setdefault("counters", {})
    state.setdefault("runs", {})
    state.setdefault("flows", {})
    state.setdefault("panels", {})
    state.setdefault("claims", {})
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


def worktrees_dir() -> Path:
    """Return the resolved folder Ember uses for worktrees.

    Returns:
        `worktrees` in `$XDG_DATA_HOME/ember`, with `~/.local/share` as
        the default base.
    """
    base = os.environ.get("XDG_DATA_HOME", "")
    absolute = base and Path(base).is_absolute()
    root = Path(base) if absolute else Path.home() / ".local" / "share"
    return (root / "ember" / "worktrees").resolve()


def worktree_path(worktree_id: str) -> Path:
    """Return the path Ember gives a run's or flow's worktree.

    Args:
        worktree_id: The run id or flow id.

    Returns:
        The resolved `worktrees/<worktree_id>` in Ember's data folder,
        with its parent folders created at mode 700.
    """
    folder = worktrees_dir()
    folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder / worktree_id


def recorded_worktree(
    repo: Path, branch: str, worktree_id: str, state: dict[str, Any]
) -> Path | None:
    """Return a worker branch's worktree as `emb cleanup` finds it.

    Args:
        repo: A folder in the repository.
        branch: The worker branch.
        worktree_id: The run id or flow id in the branch's name.
        state: The stub's state.

    Returns:
        The worktree its run or flow names, or else the worktree `git
        worktree list` shows holding the branch in a folder named by the id;
        `None` when there is neither.
    """
    entry = state["runs"].get(worktree_id) or state["flows"].get(worktree_id)
    if entry and entry.get("worktree"):
        return Path(entry["worktree"])
    current: Path | None = None
    for line in out(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif (
            line == f"branch refs/heads/{branch}"
            and current is not None
            and current.name == worktree_id
        ):
            return current
    return None


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


def snapshot(folder: Path) -> dict[str, tuple[str, str | None]]:
    """Read status and file hashes for a read-only turn or start warning."""
    entries = git(
        folder, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    ).stdout.split("\0")
    result = {}
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, name = entry[:2], entry[3:]
        names = [name]
        if code[0] in "RC" and index < len(entries):
            names.append(entries[index])
            index += 1
        for name in names:
            try:
                digest = hashlib.sha256((folder / name).read_bytes()).hexdigest()
            except OSError:
                digest = None
            result[name] = (code, digest)
    return result


def remove_detached(
    run: dict[str, Any], *, dry_run: bool = False
) -> tuple[bool, str | None]:
    """Remove a clean recorded detached fixture worktree or return its warning."""
    if not run.get("detached") or not run["read_only"]:
        return False, None
    folder = Path(run["worktree"])
    listing = out(run["common"], "worktree", "list", "--porcelain", "-z")
    entries = [entry.split("\0") for entry in listing.split("\0\0") if entry]
    entry = next((item for item in entries if item[0] == f"worktree {folder}"), None)
    if entry is None:
        return False, None if not folder.exists() else f"{folder} is not registered"
    if "detached" not in entry:
        return False, f"{folder} is no longer a detached worktree"
    if folder.exists() and (
        dirty(folder) or out(folder, "rev-parse", "HEAD") != run["base"]
    ):
        command = shlex.join(["git", "worktree", "remove", "--force", str(folder)])
        return False, f"kept worktree {folder} with changes; to discard: {command}"
    if any(item.startswith("locked") for item in entry):
        return False, f"kept locked worktree {folder}"
    if not dry_run:
        git(run["common"], "worktree", "remove", "--", str(folder))
    return True, None


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


def do_turn(
    run: dict[str, Any],
    base: Path | None,
    variables: dict[str, str],
    warnings: list[str],
) -> str:
    """Play one turn: commit the reply's files, write its reply text.

    Args:
        run: The run's record, updated in place.
        base: The reply.
        variables: The template values.
        warnings: Read-only change warnings, appended for this turn.

    Returns:
        The turn's printed lines.
    """
    run.update(owner_wait=[], owner_read=[], answered_at=None, last_variant=None)
    run["turn"] += 1
    worktree = Path(run["worktree"])
    before = snapshot(worktree) if run["read_only"] else {}
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
    if run["read_only"]:
        after = snapshot(worktree)
        changed = [
            str(worktree / name)
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        ]
        warnings.extend(
            f"WARNING: read-only run {run['id']} changed {name}\n" for name in changed
        )
        run["last_reason"] = "changed " + ", ".join(changed) if changed else None
    reply.write_text(read_part(base, ".reply", variables, DEFAULT_REPLY))
    outcome = {
        "exit": 0,
        "state": "done",
        "reason": None,
        "provider_error": False,
        "signed_out": False,
        "incomplete": None,
        "seconds": SECONDS,
    }
    (folder / f"turn-{run['turn']}.outcome.json").write_text(json.dumps(outcome))
    run.setdefault("turns", []).append({"turn": run["turn"], **outcome})
    run.update(reply=str(reply), status="waiting", turn_state="committed", last_exit=0)
    return turn_block(run, files, commit)


def run_merged(run: dict[str, Any]) -> bool:
    """Return whether the run's last commit is contained in its base branch.

    Args:
        run: The run's record.

    Returns:
        The check `emb cleanup` uses; a missing base branch or a failed
        check counts as not contained.
    """
    try:
        return bool(
            run.get("commits")
            and run.get("base_branch")
            and branches.is_contained(
                run.get("main") or run["repo"],
                run["commits"][-1],
                run["base_branch"],
                os.environ,
                scratch=True,
            )
        )
    except OSError, subprocess.SubprocessError, branches.BranchError:
        return False


def run_branch_left(run: dict[str, Any]) -> bool:
    """Return whether a run's branch is still in its checkout.

    Args:
        run: The run's record.

    Returns:
        `False` for no branch, or for a branch or checkout git reports gone.
    """
    if not run.get("branch"):
        return False
    result = git(
        run.get("main") or run["repo"],
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{run['branch']}",
        check=False,
    )
    return result.returncode not in (1, 128)


def finish_line(run: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Return the claim-finish line, or `None` without a live claim.

    Args:
        run: The run's record.
        state: The stub's state, whose `claims` hold the finished ids.

    Returns:
        `next: emb choices --finish <claim>`; `None` when the run has no
        claim or its claim is `gone`.
    """
    claim = run.get("claim")
    if claim is None or state["claims"].get(claim) == "gone":
        return None
    return f"next: emb choices --finish {shlex.quote(claim)}"


def verify_line(run: dict[str, Any]) -> str:
    """Return the verify and merge line for a run with commits to land.

    Args:
        run: The run's record.

    Returns:
        `next: python3 <skill>/scripts/verify_branch.py ..., then emb merge
        ...`, with the arguments filled in from the record and git.
    """
    base_branch = (
        shlex.quote(run["base_branch"])
        if run.get("base_branch")
        else "<session branch>"
    )
    branch = shlex.quote(run["branch"]) if run.get("branch") else "<branch>"
    directory = run.get("main") or run["repo"]
    script = skill.link_path(os.environ) / "scripts" / "verify_branch.py"
    line = (
        f"next: python3 {shlex.quote(str(script))} --repo {shlex.quote(directory)} "
        f"--base {base_branch} --branch {branch} "
        f"--worktree {shlex.quote(run['worktree'])}"
    )
    if run["turn"] == 1 and run.get("base") is not None:
        result = git(
            directory,
            "diff",
            "--name-only",
            "-z",
            run["base"],
            run["commits"][-1],
            check=False,
        )
        if result.returncode == 0:
            count = len([name for name in result.stdout.split("\0") if name])
            line += f" --expect-files {count}"
    return (
        f"{line}, then emb merge {branch} --into {base_branch} "
        "--verdict keep --note '<verdict>' --subject '<subject>'"
    )


def derive_next(run: dict[str, Any], state: dict[str, Any]) -> tuple[str, str | None]:
    """Return a run's step and next line from its record and git.

    The order is the one `ember.runs.steps.derive` uses. Stub sessions are live.

    Args:
        run: The run's record; stored `step` and `next` keys are ignored.
        state: The stub's state.

    Returns:
        The step name and its line, or `None` when none applies.
    """
    if run.get("parent"):
        return "parent", None
    if run.get("owner_wait"):
        reads = run.get("owner_read", [])
        if reads and len(reads) == len(run["owner_wait"]) and all(reads):
            settings = json.loads(read_part(reply_base("choices-read", 1), ".out", {}))
            allowed = (
                settings.get("answers", {}).get("sandbox.read", {}).get("value", [])
            )
            if all(path in allowed for path in reads):
                run.update(owner_wait=[], owner_read=[])
        if run.get("owner_wait"):
            return "owner", None
    if run["status"] == "closed":
        if (
            run["read_only"]
            or not run.get("commits")
            or run_merged(run)
            or not run_branch_left(run)
        ):
            return "finish", finish_line(run, state)
        return "merge", verify_line(run)
    if run["status"] == "failed" or (
        run["turn_state"] != "sent" and run.get("last_exit") not in (None, 0)
    ):
        line = exits.line(
            run.get("last_exit") or 0, run_variant(run), **{**run, "run": run["id"]}
        )
        if run.get("answered_at") is not None and line and line.startswith("owner:"):
            line = "next: " + (
                exits.SEND.format(run=run["id"])
                if run["turn"] and not run.get("last_start_failure")
                else exits.START
            )
        return "failed", line
    if run["status"] == "starting" or run["turn"] == 0:
        return "starting", f"next: emb run --status {run['id']}"
    if run["turn_state"] == "sent":
        return "running", f"next: emb run --status {run['id']}"
    if run["turn_state"] == "collected" and not run["read_only"]:
        return "recover", f"next: emb run --recover {run['id']}"
    if not run["read_only"] and run.get("commits") and not run_merged(run):
        return "merge", verify_line(run)
    return "close", f"next: emb run --close {run['id']}"


def run_variant(run: dict[str, Any]) -> str | None:
    """Read the stored variant or an older record's final provider kind."""
    if run.get("last_variant") is not None:
        return run["last_variant"]
    turns = run.get("turns", [])
    if run.get("last_exit") == 73 and turns:
        return turns[-1].get("provider_kind")
    return None


def status_errors(
    run: dict[str, Any], state: dict[str, Any], *, listing: bool = False
) -> str:
    """Print the status owner's actions or next line, then its report line."""
    step, line = derive_next(run, state)
    lines = list(run.get("owner_wait", [])) if step == "owner" else []
    if line and not listing:
        lines.append(line)
    if step in ("owner", "failed"):
        report = exits.report(run.get("last_exit") or 0, run_variant(run), **run)
        if report:
            lines.append(report)
    return "".join(f"{text}\n" for text in lines)


def with_step(run: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Return a run's status document with `step` and `next` derived fresh.

    Args:
        run: The run's record.
        state: The stub's state.

    Returns:
        The record with `session_state`, `step` and `next`; any stored `step`
        or `next` is replaced.
    """
    step, line = derive_next(run, state)
    return {**run, "session_state": "live", "step": step, "next": line}


def add_next(reply: Reply, run: dict[str, Any], state: dict[str, Any]) -> Reply:
    """Append the run's next line to a reply's standard error.

    Args:
        reply: The exit-0 reply of `emb run`.
        run: The run's record.
        state: The stub's state.

    Returns:
        The reply, with the line last on standard error when there is one.
    """
    line = derive_next(run, state)[1]
    if line is not None:
        reply.err += line + "\n"
    return reply


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
    model_source = row_name if model is None else "--model"
    effort_source = "--effort" if effort is not None else model_source
    row = menu_row(row_name)
    if model is not None and row and row.get("run_as") == "subagent":
        row = None
    if model is None:
        if row is None or row.get("state") != "set" or row.get("engine") != engine:
            state_text = row.get("state") if row else "ask"
            return refuse(
                64,
                f"the menu row {row_name} is {state_text} for {engine}; give --model",
                "next: emb run --help",
            )
        model = row["model"]
        if engine == "claude" and row.get("run_as") == "subagent":
            return refuse(64, "the row runs as a subagent", "next: emb run --help")
        if effort is None:
            effort = row.get("effort")
            if effort is not None:
                effort_source = row_name
    elif effort is None:
        if (
            row
            and row.get("state") == "set"
            and row.get("engine") == engine
            and row.get("model") == model
        ):
            effort = row.get("effort")
            if effort is not None:
                effort_source = row_name
    worker = WorkerSelection(
        engine=engine,
        model=model,
        effort=effort,
        model_source=model_source,
        effort_source=effort_source,
        row="Single tasks Reviewer" if read_only else "Single tasks Writer",
    )
    head = out(directory, "rev-parse", "HEAD")
    if has(args, "--dry-run"):
        output = json.dumps(asdict(worker)) if has(args, "--json") else worker.line()
        return Reply(0, output + "\n")
    run_id = new_run_id(engine)
    warnings = [worker.line() + "\n"]
    worktree = worktree_path(run_id)
    if read_only:
        branch = None
        short_head = out(directory, "rev-parse", "--short", head)
        warnings.extend(
            f"WARNING: read-only run {run_id} does not see uncommitted {name} "
            f"(reads {short_head})\n"
            for name in snapshot(repo)
        )
        git(repo, "worktree", "add", "-q", "--detach", str(worktree), head)
    else:
        branch = f"ember/{engine}/{run_id}"
        git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), head)
    run = {
        "id": run_id,
        "engine": engine,
        "model": model,
        "effort": effort,
        "read_only": read_only,
        "detached": read_only,
        "repo": str(repo),
        "main": str(main_checkout(repo)),
        "common": str(
            Path(
                out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
        ),
        "worktree": str(worktree),
        "branch": branch,
        "base": head,
        "base_branch": out(directory, "branch", "--show-current") or None,
        "trailers": options(args, "--trailer"),
        "claim": option(args, "--claim"),
        "status": "running",
        "turn": 0,
        "turns": [],
        "last_exit": None,
        "last_reason": None,
        "turn_state": "sent",
        "reply": None,
        "commits": [],
        "subject": subject,
        "started_at": datetime.datetime.now(datetime.UTC).timestamp(),
    }
    variables.update(RUN=run_id, BRANCH=branch or "-", WORKTREE=str(worktree))
    block = do_turn(run, base, variables, warnings)
    state["runs"][run_id] = run
    return add_next(
        Reply(
            0,
            f"run {run_id}\n{block}",
            "".join(warnings) + read_part(base, ".err", variables),
        ),
        run,
        state,
    )


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
        Reply: The run is not known (64).
    """
    run = state["runs"].get(run_id or "")
    if run is None:
        raise refuse(64, f"no run {run_id}", "next: emb run --status")
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
        mode: `send`, `recover`, `close`, `status` or `answered`.
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
        error_lines = [
            line
            for run in runs
            for line in status_errors(run, state, listing=True).splitlines()
        ]
        errors = "".join(
            line + "\n"
            for prefix in ("owner:", "report:")
            for line in error_lines
            if line.startswith(prefix)
        )
        if has(args, "--json"):
            listing = [with_step(run, state) for run in runs]
            return Reply(0, json.dumps(listing, indent=2) + "\n", errors)
        lines = [
            f"{run['id']}  {run['status']}  live  turn {run['turn']}  {run['subject']}"
            for run in runs
        ]
        return Reply(0, "".join(line + "\n" for line in lines), errors)
    if canned(base) and not (mode == "status" and run_id in state["runs"]):
        if run_id in state["runs"]:
            known_run(state, run_id, variables)
        return canned_reply(base, variables, has(args, "--json"))
    if mode == "answered" and run_id not in state["runs"]:
        return refuse(64, f"no run {run_id}", "next: emb run --status")
    run = known_run(state, run_id, variables)
    if mode == "answered":
        if not run.get("parent") and run.get("owner_wait"):
            run.update(
                owner_wait=[],
                owner_read=[],
                answered_at=datetime.datetime.now(datetime.UTC).timestamp(),
            )
        return add_next(Reply(0), run, state)
    if mode == "status":
        document = with_step(run, state)
        errors = status_errors(run, state)
        if has(args, "--json"):
            return Reply(0, json.dumps(document, indent=2) + "\n", errors)
        shown = (
            (key, value if isinstance(value, str) else json.dumps(value))
            for key, value in document.items()
            if key != "next"
        )
        return Reply(0, "".join(f"{key}: {value}\n" for key, value in shown), errors)
    if mode == "close":
        run["status"] = "closed"
        _, warning = remove_detached(run)
        errors = f"WARNING: read-only run {run['id']}: {warning}\n" if warning else ""
        return add_next(Reply(0, f"closed {run['id']}\n", errors), run, state)
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
        warnings = []
        block = do_turn(run, base, variables, warnings)
        return add_next(
            Reply(0, block, "".join(warnings) + read_part(base, ".err", variables)),
            run,
            state,
        )
    commits = run["commits"]
    return add_next(
        Reply(0, turn_block(run, 0, commits[-1] if commits else None)), run, state
    )


# emb flow and emb panel ----------------------------------------------------


def flow_start(
    args: list[str], state: dict[str, Any], base: Path | None, variables: dict[str, str]
) -> Reply:
    """Build a flow branch and commit the reply tree with the plan's subject."""
    plan_path = variables["PLAN"]
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise ValueError("not an object")
    except OSError, ValueError:
        return Reply(
            66, err=f"emb flow: cannot read {plan_path}\nnext: ls -l {plan_path}\n"
        )
    try:
        document = flow_plan.from_json(plan, Path(plan_path))
    except flow_plan.PlanError as error:
        return Reply(error.code, err=f"emb flow: {error}\nnext: emb flow --help\n")
    directory = Path(plan.get("dir") or variables["DIR"]).resolve()
    repo = main_checkout(directory)
    flow_id = new_run_id("flow")
    branch = f"ember/flow/{flow_id}"
    worktree = worktree_path(flow_id)
    head = out(directory, "rev-parse", "HEAD")
    git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), head)
    tree = part(base, ".files")
    if tree is not None:
        copy_tree(tree, worktree)
        git(worktree, "add", "-A")
        if git(worktree, "diff", "--cached", "--quiet", check=False).returncode:
            subject = document.tasks[0].subject
            trailers = plan.get("trailers") or []
            message = subject + ("\n\n" + "\n".join(trailers) if trailers else "")
            git(worktree, "commit", "-q", "--no-verify", "-m", message)
    state["flows"][flow_id] = {
        "dir": str(directory),
        "main": str(repo),
        "branch": branch,
        "worktree": str(worktree),
    }
    return Reply(
        0,
        f"flow {flow_id}\nbranch {branch}\nworktree {worktree}\n",
        f"next: emb flow --watch {flow_id} --json\n",
    )


def panel_start(
    state: dict[str, Any], base: Path | None, variables: dict[str, str], as_json: bool
) -> Reply:
    """Write a panel's summary even when its canned result reports failure."""
    panel_id = new_run_id("panel")
    summary = EVAL / "runs" / panel_id / "summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    variables.update(PANEL=panel_id, SUMMARY=str(summary))
    with summary.open("w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(read_part(base, ".summary", variables))
    state["panels"][panel_id] = {"summary": str(summary), "base": variables["HEAD"]}
    return canned_reply(base, variables, as_json)


def _cwd() -> str:
    """Return the working directory, or `PWD` when it was removed."""
    try:
        return os.getcwd()
    except FileNotFoundError:
        return os.environ.get("PWD", "/")


def record_variables(args: list[str], state: dict[str, Any]) -> dict[str, str]:
    """Resolve plan, flow and panel template values for the current call."""
    plan_path = option(args, "--plan")
    plan: dict[str, Any] = {}
    if plan_path:
        try:
            value = json.loads(Path(plan_path).expanduser().read_text(encoding="utf-8"))
            if isinstance(value, dict):
                plan = value
        except OSError, ValueError:
            pass
    directory = plan.get("dir") or option(args, "--dir") or _cwd()
    flow_id = next(reversed(state["flows"]), "")
    panel_id = next(reversed(state["panels"]), "")
    if args and args[0] == "flow":
        flow_id = next(
            (
                option(args, flag)
                for flag in ("--watch", "--status", "--lead", "--resume")
                if option(args, flag)
            ),
            flow_id,
        )
    if args and args[0] == "panel":
        panel_id = option(args, "--status") or option(args, "--recover") or panel_id
    variables = {
        "FLOW": flow_id,
        "PANEL": panel_id,
        "PLAN": str(Path(plan_path).expanduser().resolve()) if plan_path else "",
        "DIR": str(Path(directory).expanduser().resolve()),
        "WORKTREES": str(worktrees_dir()),
        "SUMMARY": str(EVAL / "runs" / panel_id / "summary.md") if panel_id else "",
    }
    with contextlib.suppress(Exception):
        variables["HEAD"] = out(variables["DIR"], "rev-parse", "HEAD")
        variables["SHORT_HEAD"] = out(
            variables["DIR"], "rev-parse", "--short", variables["HEAD"]
        )
    flow = state["flows"].get(flow_id)
    if flow:
        variables.update(BRANCH=flow["branch"], WORKTREE=flow["worktree"])
        if branch_exists(Path(flow["dir"]), flow["branch"]):
            variables["TIP"] = out(flow["dir"], "rev-parse", flow["branch"])
    return variables


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
    flow_match = re.fullmatch(
        r"ember/flow/(flow-[0-9]{8}-[0-9]{6}-[0-9a-f]{6})", branch
    )
    if match is None and flow_match is None:
        return f"kept {branch}: not an Ember run branch; next: git branch -d {branch}"
    run_id = match.group(2) if match else flow_match.group(1)
    main = main_checkout(repo)
    worktree = recorded_worktree(repo, branch, run_id, state)
    present = worktree is not None and worktree.exists()
    parts = []
    if worktree is not None and present and dirty(worktree):
        return (
            f"kept {branch}: the worktree {worktree} has changes; "
            f"next: git -C {worktree} status --short"
        )
    run = state["runs"].get(run_id)
    if run is not None and run["status"] != "closed":
        run["status"] = "closed"
        parts.append(f"closed {run_id}")
    tip = out(repo, "rev-parse", f"refs/heads/{branch}")
    if worktree is not None and present:
        git(main, "worktree", "remove", "--", str(worktree))
        parts.append(f"removed worktree {worktree}")
    git(main, "worktree", "prune")
    git(repo, "update-ref", "-d", f"refs/heads/{branch}", tip)
    parts.append(f"deleted at {tip}")
    return f"removed {branch}: {'; '.join(parts)}"


def branch_claim(branch: str, state: dict[str, Any]) -> str | None:
    """Return the claim a run branch's record holds.

    Args:
        branch: The branch.
        state: The stub's state.

    Returns:
        The recorded claim id, or `None` for a branch with no run record or
        no claim.
    """
    match = branches.WORKER_BRANCH.fullmatch(branch)
    run = state["runs"].get(match["run"]) if match else None
    return run.get("claim") if run else None


def contained(repo: Path, branch: str, target: str) -> bool:
    """Return whether Ember considers the branch contained in the target.

    Args:
        repo: A folder in the repository.
        branch: The branch.
        target: The target.

    Returns:
        Whether the target contains the branch's tip.
    """
    return branches.is_contained(
        repo, f"refs/heads/{branch}", f"refs/heads/{target}", os.environ
    )


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
        and (
            index == 0
            or args[index - 1]
            not in ("--into", "--verdict", "--note", "--subject", "--dir")
        )
    ]
    branch = words[0] if words else None
    target = option(args, "--into")
    usage = "next: emb merge --help"
    if branch is None or target is None:
        return merge_failure(64, "give a branch and --into TARGET", usage)
    note = (option(args, "--note") or "").strip()
    if not note or "\n" in note:
        return merge_failure(64, "the following arguments are required: --note", usage)
    if option(args, "--verdict") != "keep":
        return merge_failure(
            64, "only a keep verdict merges; nothing was changed", usage
        )
    directory = Path(option(args, "--dir") or ".").expanduser()
    top = top_of(directory)
    if top is None:
        return merge_failure(65, f"{directory} is not in a git checkout", usage)
    # The main checkout outlives the run worktree that cleanup removes.
    repo = main_checkout(top)
    for name in (branch, target):
        if not branch_exists(repo, name):
            return merge_failure(
                65, f"no local branch {name}", f"next: git -C {repo} branch --list"
            )
    if branch == target:
        return merge_failure(64, "the branch and the target are the same", usage)
    squash = branches.WORKER_BRANCH.fullmatch(branch) and not (
        branches.FLOW_BRANCH.fullmatch(branch)
    )
    subject = option(args, "--subject")
    if squash and (
        subject is None or not subject.strip() or any(c in subject for c in "\r\n\0")
    ):
        return merge_failure(
            64,
            "an Ember run branch requires --subject with one nonblank line",
            f"next: emb merge {branch} --into {target} --verdict keep "
            "--note '<verdict line>' --subject '<commit subject>'",
        )
    warnings: list[str] = []
    try:
        opened = branches.open_repository(repo, os.environ, "merge", from_main=True)
        worktree = branches._target_worktree(opened, target)
        tip = out(repo, "rev-parse", f"refs/heads/{branch}")
        variables.update(BRANCH=branch, TARGET=target, TIP=tip)
        commit, first = branches._merge(
            opened,
            branch,
            target,
            worktree,
            note,
            subject.strip() if squash and subject is not None else None,
            warnings=warnings,
        )
    except branches.BranchError as error:
        response = merge_failure(error.code, str(error), error.line)
        if error.code == 75 and error.variant == "conflict":
            response.err += f"{exits.report(75, 'conflict')}\n"
        return response
    cleaned = cleanup_branch(repo, branch, state)
    run_match = branches.WORKER_BRANCH.fullmatch(branch)
    run = None
    if run_match and not branches.FLOW_BRANCH.fullmatch(branch):
        run = state["runs"].get(run_match["run"])
    if cleaned.startswith("kept"):
        run = None
    next_line = derive_next(run, state)[1] if run is not None else None
    err = "".join(f"{warning}\n" for warning in warnings)
    if has(args, "--json"):
        action, _, reason = cleaned.partition(": ")
        reason, separator, next_action = reason.partition("; next: ")
        result = {
            "branch": branch,
            "target": target,
            "note": note,
            "warnings": warnings,
            "merge_commit": commit,
            "contained_before": commit is None,
            "cleanup": [
                {
                    "branch": branch,
                    "run": run_match["run"] if run_match else None,
                    "action": action.split()[0],
                    "reason": reason,
                    "next": f"next: {next_action}" if separator else None,
                }
            ],
            "next": next_line,
        }
        err += f"{next_line}\n" if next_line else ""
        return Reply(0, json.dumps(result, indent=2) + "\n", err)
    lines = [f"Verdict: {note}", first, cleaned]
    err += f"{next_line}\n" if next_line else ""
    return Reply(0, "\n".join(lines) + "\n", err)


def cleanup(args: list[str], state: dict[str, Any], variables: dict[str, str]) -> Reply:
    """Clean up contained run and flow branches as `emb cleanup` does.

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
    top = top_of(directory)
    repo = main_checkout(top) if top is not None else None
    if repo is None or not branch_exists(repo, target):
        return Reply(
            65, err=f"emb cleanup: no local branch {target}\nnext: git branch --list\n"
        )
    only = option(args, "--branch")
    claim = option(args, "--claim")
    names = (
        [only]
        if only
        else out(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/ember/"
        ).splitlines()
    )
    lines = []
    kept_claims: set[str | None] = set()
    for name in names:
        if not name or not branch_exists(repo, name):
            continue
        if claim is not None and branch_claim(name, state) != claim:
            continue
        before = len(lines)
        if not contained(repo, name, target):
            lines.append(
                f"kept {name}: not contained in {target}; next: emb merge {name} "
                f"--into {target} --verdict keep --note '<verdict line>'"
                " --subject '<commit subject>' (after a Keep verdict)"
            )
        elif has(args, "--dry-run"):
            lines.append(f"would remove {name}: deletes {name}")
        else:
            lines.append(cleanup_branch(repo, name, state))
        if lines[-1].startswith("kept") and len(lines) > before:
            kept_claims.add(branch_claim(name, state))
    if only is None:
        common = Path(
            out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        for run in state["runs"].values():
            if (
                run["status"] != "closed"
                or not run.get("common")
                or Path(run["common"]).resolve() != common.resolve()
            ):
                continue
            if claim is not None and run.get("claim") != claim:
                continue
            removed, warning = remove_detached(run, dry_run=has(args, "--dry-run"))
            if warning:
                kept_claims.add(run.get("claim"))
                lines.append(
                    f"kept {run['id']}: {warning}; next: emb cleanup "
                    f"--merged-into {target} --dir {repo}"
                )
            elif removed:
                action = "would remove" if has(args, "--dry-run") else "removed"
                verb = "removes" if has(args, "--dry-run") else "removed"
                lines.append(f"{action} {run['id']}: {verb} worktree {run['worktree']}")
    err = "" if lines else f"emb cleanup: no Ember run branch against {target}\n"
    # The claim-finish line: after exit 0 with --claim, unless a kept result
    # holds that claim; never with --dry-run.
    if claim is not None and not has(args, "--dry-run") and claim not in kept_claims:
        err += f"next: emb choices --finish {shlex.quote(claim)}\n"
    return Reply(0, "".join(line + "\n" for line in lines), err)


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


# Gap check -----------------------------------------------------------------


def gapcheck_answer(args: list[str]) -> Reply:
    """Record an answer with Ember's result validator and atomic writer."""
    location = option(args, "--answer")
    identity, text = option(args, "--id"), option(args, "--text")
    if location is None or identity is None or text is None:
        return Reply(
            64,
            err="emb gapcheck: --answer needs --id and --text\n"
            "next: emb gapcheck --help\n",
        )
    try:
        saved = gap_previous.load(location)
        made = gap_previous.answer(saved, identity, text)
    except gap_previous.PreviousError as error:
        return Reply(
            66 if error.unreadable else 65,
            err=f"emb gapcheck: {error}\nnext: emb gapcheck --help\n",
        )
    except ValueError as error:
        return Reply(64, err=f"emb gapcheck: {error}\nnext: emb gapcheck --help\n")
    except OSError as error:
        return Reply(74, err=f"emb gapcheck: {error}\nnext: ls -l {location}\n")
    if has(args, "--json"):
        return Reply(0, json.dumps(made, indent=2) + "\n")
    return Reply(0, err=f"recorded answer for {identity} in {saved.path}\n")


def gapcheck_repeat(
    args: list[str], base: Path | None, variables: dict[str, str]
) -> Reply:
    """Parse a repeat review, carrying answers and history through real validators.

    A case can supply the review as gapcheck-repeat.reply. Without that
    fixture, answered concerns remain settled and unanswered concerns reopen.
    """
    try:
        earlier = gap_previous.load(option(args, "--previous") or "")
        document = gap_plan.load(option(args, "--plan") or variables["PLAN"])
    except gap_previous.PreviousError as error:
        return Reply(
            66 if error.unreadable else 65,
            err=f"emb gapcheck: {error}\nnext: emb gapcheck --help\n",
        )
    except gap_plan.PlanError as error:
        return Reply(66, err=f"emb gapcheck: {error}\nnext: emb gapcheck --help\n")
    if has(args, "--brief"):
        return Reply(0, gap_brief.text(document, variables["DIR"], earlier))
    fixture = REPLIES / "gapcheck-repeat.reply"
    supplied_reply = option(args, "--reply")
    if supplied_reply is not None:
        text = Path(supplied_reply).read_text()
    elif fixture.exists():
        text = fixture.read_text()
    else:
        review: dict[str, Any] = {
            "steps": [{"step": step.number, "gaps": []} for step in document.steps],
            "dispositions": [],
        }
        for gap in earlier.gaps:
            settled = gap.answer is not None
            review["dispositions"].append(
                dict(
                    id=gap.id,
                    status="settled" if settled else "reopened",
                    reason="The answer remains valid."
                    if settled
                    else "The concern remains unanswered.",
                )
            )
            if not settled:
                step = next(
                    (step for step in review["steps"] if step["step"] == gap.step),
                    review["steps"][0],
                )
                step["gaps"].append(
                    dict(
                        gap=gap.gap,
                        harm=gap.harm,
                        why=gap.why,
                        default=gap.default,
                        earlier=gap.id,
                    )
                )
        text = json.dumps(review)
    fields = gap_reply.read(
        text,
        document.steps,
        earlier_ids=earlier.ids(),
        first_number=earlier.next_id,
        answered_ids={gap.id for gap in earlier.gaps if gap.answer is not None},
    )
    settled, closed = gap_previous.history(earlier, fields["dispositions"])
    made = json.loads(canned_reply(base, variables, True).out)
    made.update(
        fields,
        plan=str(document.path),
        plan_steps=document.kind,
        failure_model=document.failure_model,
        previous=str(earlier.path),
        previous_plan=earlier.plan,
        settled=settled,
        closed=closed,
    )
    for step in made["steps"]:
        for gap in step["gaps"]:
            gap["ask_owner"] = (
                made["ask"] == "all"
                or gap["harm"] == "high"
                or (made["ask"] == "medium" and gap["harm"] == "medium")
            )
    gap_previous.validate(made)
    warning = (
        ""
        if earlier.plan == str(document.path)
        else f"WARNING: the previous result's plan {earlier.plan} differs "
        f"from --plan {document.path}\n"
    )
    code = 81 if made["parse_error"] else 0
    if code:
        warning += f"emb gapcheck: {made['parse_error']}\nnext: emb gapcheck --help\n"
    return Reply(code, json.dumps(made, indent=2) + "\n", warning)


# Main ----------------------------------------------------------------------


def flow_dry_run(args: list[str], variables: dict[str, str]) -> Reply:
    """Compute a planning case's dry run with Ember's planner and formatter."""
    from ember.flows import command as flows
    from ember.flows import plan as flow_plan

    settings = json.loads(
        canned_reply(reply_base("choices-read-id", 1), variables, True).out
    )
    try:
        document = flow_plan.load(option(args, "--plan") or "")
        document = flows._parallel(flows._review_rounds(document, settings), settings)
        writer, reviewer = (
            flow_plan.resolve(role, option(args, f"--{role}"), document, settings)
            for role in flow_plan.ROLES
        )
        output, errors = io.StringIO(), io.StringIO()
        flows._dry_run(
            argparse.Namespace(
                json=has(args, "--json"),
                previous=option(args, "--previous"),
                claim=option(args, "--claim"),
                stall=None,
            ),
            document,
            Path(document.dir),
            out(document.dir, "rev-parse", document.base),
            writer,
            reviewer,
            settings,
            dict(os.environ),
            output,
            errors,
        )
    except flow_plan.PlanError as error:
        return Reply(error.code, err=f"emb flow: {error}\nnext: emb flow --help\n")
    except flow_plan.WorkerError as error:
        return Reply(error.code, err=f"emb flow: {error}\n{error.line}\n")
    # Keep the submitted plan even when the session wrote it in its scratchpad.
    (EVAL / "flow-plan.json").write_text(document.text)
    return Reply(0, output.getvalue(), errors.getvalue())


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
    if name == "flow-dry-run" and (EVAL / "real-flow-planner").is_file():
        return flow_dry_run(rest, variables)
    if name == "gapcheck-answer":
        return gapcheck_answer(rest)
    if name.startswith("gapcheck") and has(rest, "--previous"):
        # Repeat reviews use the case's base result for reviewer metadata.
        return gapcheck_repeat(rest, reply_base("gapcheck", 1), variables)
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
        if mode == "dry-run":
            return (
                canned_reply(base, variables, as_json)
                if code_of(base) != 0
                else run_start(rest, state, base, variables)
            )
        if mode == "start":
            if canned(base):
                response = canned_reply(base, variables, as_json)
                status = reply_base("run-status", 1)
                if part(status, ".json") and response.out.startswith("run "):
                    run = json.loads(read_part(status, ".json", variables))
                    brief = option(rest, "--task-file")
                    if brief:
                        run["subject"] = next(
                            line.strip()
                            for line in Path(brief).read_text().splitlines()
                            if line.strip()
                        )
                    run.update(
                        claim=option(rest, "--claim"),
                        trailers=options(rest, "--trailer"),
                    )
                    state["runs"][run["id"]] = run
                return response
            return run_start(rest, state, base, variables)
        if mode in ("send", "recover", "close", "status", "answered"):
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
    if name == "flow-start" and not canned(base):
        return flow_start(rest, state, base, variables)
    if name == "panel-start" and part(base, ".summary") is not None:
        return panel_start(state, base, variables, as_json)
    response = canned_reply(base, variables, as_json)
    if name == "choices-finish" and response.code == 0:
        # A finished claim reads `gone`, so a status derives no finish line.
        state["claims"][option(rest, "--finish") or ""] = "gone"
    if name == "choices-claim" and response.code == 0 and not has(rest, "--skill"):
        document = json.loads(response.out)
        for key in ("skill", "loaded_skill_version", "installed_skill_version"):
            document.pop(key, None)
        response.out = json.dumps(document, indent=2) + "\n"
        response.err = ""
    if name == "flow-dry-run" and has(rest, "--previous"):
        extra = " --previous " + shlex.quote(option(rest, "--previous") or "")
        if as_json:
            document = json.loads(response.out)
            if document["gapcheck"] is not None:
                document["gapcheck"] += extra
            response.out = json.dumps(document, indent=2) + "\n"
        else:
            response.out = response.out.rstrip("\n") + extra + "\n"
    return response


def command_reports(
    name: str,
    args: list[str],
    reply: Reply,
    base: Path | None,
    variables: dict[str, str],
    state: dict[str, Any],
) -> None:
    """Append command reports and retain owner waits for canned run results."""
    if name.startswith("flow-"):
        from ember.flows import command as flows

        reports = flows._exit_reports(reply.code)
        if name == "flow-watch" and part(base, ".json"):
            document = json.loads(read_part(base, ".json", variables))
            stops = document.get("stops", [])
            saved = state.setdefault("flow_stops", {}).setdefault(
                document.get("flow", ""), {}
            )
            for stop in stops:
                saved[stop.get("task") or ""] = stop
            if stops or document.get("event") == "lead_stop":
                stops = stops or [
                    saved.get(task["id"], {"task": task["id"]})
                    for task in document["tasks"]
                    if task["status"] in ("stopped", "conflict")
                ]
                flow = {**document, "id": document["flow"]}
                live = document["status"] == "running"
                # The fixture contains every stop; no live event files are read.
                if stops:
                    reports = flows._stop_reports(flow, stops, live, {}) + reports
                next_line = flows._next(flow, "lead_stop", reply.code, live)
                reply.err = reply.err.replace(document["next"], next_line)
                document["next"] = next_line
            if "event" in document:
                document["report"] = reports
                if has(args, "--json"):
                    reply.out = json.dumps(document, indent=2) + "\n"
        reply.err = "".join(
            line + "\n"
            for line in reply.err.splitlines()
            if not line.startswith("report:")
        )
        reply.err += "".join(report + "\n" for report in reports)
        return
    if name in ("merge", "cleanup") and reply.code == 75:
        conflict = re.search(
            r"^(?:next: resolve the conflicts|owner: resolve the conflict) "
            r"on (.+?)(?:,.*)?$",
            reply.err,
            re.MULTILINE,
        )
        if conflict:
            owner = (
                f"owner: resolve the conflict on {conflict[1]}, "
                "or ask for a new run cut from the updated target"
            )
            reply.err = reply.err.replace(conflict[0], owner)
            report = exits.report(75, "conflict")
            if report and report not in reply.err.splitlines():
                reply.err += report + "\n"
        return
    if not name.startswith("run-") or name == "run-status":
        return
    run_id = next(
        (option(args, f"--{mode}") for mode in RUN_MODES if option(args, f"--{mode}")),
        None,
    )
    if name == "run-start" and reply.out.startswith("run "):
        run_id = reply.out.splitlines()[0].removeprefix("run ")
    run = state["runs"].get(run_id, {})
    if name in ("run-start", "run-send", "run-recover") and run:
        owners = [line for line in reply.err.splitlines() if line.startswith("owner:")]
        if owners and not run.get("parent"):
            run["owner_wait"] = owners
            run["answered_at"] = None
            reply.err = "".join(
                line + "\n"
                for line in reply.err.splitlines()
                if not line.startswith("next:")
            )
    if reply.code:
        context = {**run, "run": run_id}
        variant = run_variant(run)
        entry = exits.LINES.get(reply.code)
        if isinstance(entry, dict):
            variant = next(
                (
                    key if key != "*" else variant
                    for key in entry
                    if exits.line(reply.code, key, **context) in reply.err.splitlines()
                ),
                variant,
            )
        if run and (run.get("last_exit") == reply.code or run.get("owner_wait")):
            run.update(last_exit=reply.code, last_variant=variant, status="failed")
        report = exits.report(reply.code, variant, **context)
        if report and report not in reply.err.splitlines():
            reply.err += report + "\n"


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
        **record_variables(argv, state),
        "EVAL": str(EVAL),
        "SKILL_VERSION": option(argv, "--skill") or "",
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
    command_reports(name, argv, reply, base, variables, state)
    save_state(state)
    if not setup:
        entry = {
            "seq": sum(state["counters"].values()),
            "at": stamp.replace("+00:00", "Z"),
            "argv": argv,
            "cwd": _cwd(),
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
