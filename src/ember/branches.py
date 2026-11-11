"""Merging reviewed run branches and cleaning up merged ones.

A worker branch is a run branch, `ember/<engine>/<run id>`, or a flow
branch, `ember/flow/<flow id>`. Its worktree is the path its run or flow
record names: `worktrees/<id>` in Ember's data folder, or
`.ember/worktrees/<id>` in the main checkout for a record made before that
folder. A branch with no record has the worktree that `git worktree list`
shows holding it in a folder named by its id, other than the main checkout.
Cleanup keeps a flow branch while its runner holds the lifetime lock.
Cleanup removes worker branches, their worktrees and their sessions, and
clean detached worktrees of closed read-only runs. A session is closed
through the run core, never through tmux directly.

A branch is contained in a target when it is an ancestor of the target, or
when a reachable commit names it in an Ember-Task-Tip trailer, or when
merging it into the target cleanly gives the target's own tree. The
check changes no index, worktree or ref.

Each non-zero exit prints its message on standard error, then one `next:` or
`owner:` line, as `emb run` does.
"""

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from ember import paths
from ember.flows import lock as flow_lock
from ember.flows import record as flow_record
from ember.runs import exits, record, worktrees
from ember.runs.core import Core, RunError, worktree_removal_reason
from ember.runs.record import Run

GIT_TIMEOUT = 120
"""Seconds each git command may take, except repository changes and hooks.

Writing or signing a commit, running a hook, merging, aborting, removing a
worktree, pruning and deleting a branch run without a time limit.
"""

IN_PROGRESS = (
    ("MERGE_HEAD", "merge"),
    ("REBASE_HEAD", "rebase"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)
"""The files in a worktree's git folder that show an unfinished operation."""

WORKER_BRANCH = re.compile(
    r"ember/(?P<engine>[a-z0-9]+)/(?P<run>[a-z0-9][a-z0-9-]{5,63})"
)
"""The name of a worker branch."""

FLOW_BRANCH = re.compile(r"ember/flow/(?P<run>flow-\d{8}-\d{6}-[0-9a-f]{6})")
"""The name of a flow branch."""

REMOVED = "removed"
KEPT = "kept"
WOULD_REMOVE = "would remove"
ACTIONS = (REMOVED, KEPT, WOULD_REMOVE)
"""The values of a cleanup result's `action`."""

NOT_WORKER = "not an Ember run branch"
"""The reason a branch that is not a worker branch is kept."""

VERDICT_TRAILER = "Ember-Verdict"
"""The commit trailer that holds the lead's verdict line."""

TASK_TIP_TRAILER = "Ember-Task-Tip"
"""The squash commit trailer that identifies the worker tip."""


class BranchError(Exception):
    """A branch operation's exit, with its message and follow-up line."""

    def __init__(
        self, code: int, message: str, line: str, variant: str | None = None
    ) -> None:
        """Keep the exit code and the `next:` or `owner:` line.

        Args:
            code: The exit code.
            message: What happened.
            line: The line printed after the message.
            variant: The failure variant for CLI reporting.
        """
        super().__init__(message)
        self.code = code
        self.line = line
        self.variant = variant


class GitFailedError(BranchError):
    """A git command that changes the repository failed; exit 70."""


class _KeepError(Exception):
    """A reason to keep a branch, with the line that resolves it."""

    def __init__(self, reason: str, line: str | None) -> None:
        super().__init__(reason)
        self.line = line


@dataclass(frozen=True)
class Result:
    """One act or keep of a cleanup."""

    branch: str | None
    """The branch, or `None` for a detached read-only run."""

    run: str | None
    """The run id the branch name holds, or `None`."""

    action: str
    """`removed`, `kept` or `would remove`."""

    reason: str
    """Why."""

    next: str | None
    """For a keep: the line that resolves it, `next: <command>` or
    `owner: <step>`; `None` otherwise."""

    claim: str | None = None
    """The recorded claim id, or `None` when none is recorded."""

    def to_json(self) -> dict[str, Any]:
        """Return the result as a JSON-ready dict.

        Returns:
            `branch`, `run`, `action`, `reason`, `next` and `claim`.
        """
        return {
            "branch": self.branch,
            "run": self.run,
            "action": self.action,
            "reason": self.reason,
            "next": self.next,
            "claim": self.claim,
        }

    def text(self) -> str:
        """Return the result's line.

        Returns:
            The action, branch and reason, with the claim and next step for a keep.
        """
        line = f"{self.action} {self.branch or self.run}: {self.reason}"
        if self.action == KEPT:
            line += (
                f"; claim {self.claim}"
                if self.claim is not None
                else "; no claim is recorded"
            )
        return line if self.next is None else f"{line}; {self.next}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _git(
    cwd: str | Path,
    *args: str,
    env: Mapping[str, str],
    timeout: float | None = GIT_TIMEOUT,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git in `cwd`; a git that cannot run or times out returns 1.

    `timeout` is `None` for a command that changes the repository, which
    runs to its end.
    """
    command = ["git", "-C", str(cwd), *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            env=dict(env),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        message = f"git {args[0]} timed out after {timeout}s\n"
        return subprocess.CompletedProcess(command, 1, "", message)
    except OSError as error:
        return subprocess.CompletedProcess(command, 1, "", f"git cannot run: {error}\n")


def _git_message(result: subprocess.CompletedProcess[str]) -> str:
    return _one_line(result.stderr or result.stdout) or f"exit {result.returncode}"


def _breaking_subject(subject: str) -> bool:
    """Return whether the subject has a breaking marker before its colon."""
    prefix, colon, _ = subject.partition(":")
    return bool(colon) and prefix.endswith("!")


def _breaking_change(message: str) -> bool:
    """Detect a breaking subject or Conventional Commits breaking footer."""
    subject, _, body = message.strip("\r\n").partition("\n")
    return _breaking_subject(subject) or bool(
        re.search(r"(?m)^BREAKING(?: CHANGE|-CHANGE):", body)
    )


def _trailer_separators(directory: str | Path, env: Mapping[str, str]) -> str:
    """Read Git's configured trailer separators, defaulting to a colon."""
    result = _git(directory, "config", "--null", "--get", "trailer.separators", env=env)
    if result.returncode == 1:
        return ":"
    if result.returncode:
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"reading trailer.separators failed: {_git_message(result)}",
            f"next: git -C {directory} config --get trailer.separators",
        )
    return result.stdout.removesuffix("\0")


def landed_commit(
    directory: str | Path, tip: str, target: str, env: Mapping[str, str]
) -> str | None:
    """Find a reachable squash commit whose task-tip trailer equals `tip`."""
    resolved = _git(
        directory,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{tip}^{{commit}}",
        env=env,
    )
    if resolved.returncode:
        return None
    tip = resolved.stdout.strip()
    try:
        separators = _trailer_separators(directory, env)
    except GitFailedError:
        return None
    # Ember writes colon-separated metadata even when worker trailers use
    # another separator. Keep Git's worker grammar and add Ember's separator.
    if ":" not in separators:
        separators += ":"
    result = _git(
        directory,
        "-c",
        f"trailer.separators={separators}",
        "log",
        "--format=%H%x00%(trailers:key=Ember-Task-Tip,valueonly,unfold)%x00",
        target,
        "--",
        env=env,
    )
    if result.returncode:
        return None
    fields = result.stdout.split("\0")
    for index in range(0, len(fields) - 1, 2):
        if tip in (line.strip() for line in fields[index + 1].splitlines()):
            return fields[index].strip()
    return None


def is_contained(
    directory: str | Path,
    tip: str,
    target: str,
    env: Mapping[str, str],
    *,
    scratch: bool = False,
) -> bool:
    """Return whether `target` holds `tip`'s history or all of its changes.

    Args:
        directory: A folder of the repository.
        tip: The commit to look for.
        target: The commit or ref to look in.
        env: The environment of the git commands.
        scratch: Write the objects of the trial merge to a temporary folder,
            so the repository's object store is left as it was.

    Returns:
        `True` when `tip` is an ancestor or a recorded landed task of `target`,
        or when merging `tip` into `target` gives no conflict and `target`'s
        own tree. A conflict,
        a missing commit or a failed git command gives `False`.
    """
    ancestor = _git(directory, "merge-base", "--is-ancestor", tip, target, env=env)
    if ancestor.returncode == 0:
        return True
    if ancestor.returncode != 1:
        return False
    if landed_commit(directory, tip, target, env) is not None:
        return True
    tree = _git(directory, "rev-parse", "--verify", f"{target}^{{tree}}", env=env)
    if tree.returncode or not tree.stdout.strip():
        return False
    if not scratch:
        return _merge_tree(directory, tip, target, env) == tree.stdout.strip()
    objects = _git(
        directory,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "objects",
        env=env,
    )
    if objects.returncode:
        return False
    alternates = objects.stdout.strip()
    if env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        alternates += os.pathsep + env["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
    with tempfile.TemporaryDirectory(prefix="ember-contained-") as folder:
        trial = {
            **env,
            "GIT_OBJECT_DIRECTORY": folder,
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternates,
        }
        return _merge_tree(directory, tip, target, trial) == tree.stdout.strip()


def _merge_tree(
    directory: str | Path, tip: str, target: str, env: Mapping[str, str]
) -> str | None:
    result = _git(directory, "merge-tree", "--write-tree", target, tip, env=env)
    if result.returncode:
        return None
    lines = result.stdout.splitlines()
    return lines[0].strip() if lines else None


@dataclass(frozen=True)
class Worktree:
    """One entry of `git worktree list --porcelain`."""

    path: Path
    """The worktree's folder."""

    branch: str | None
    """The branch checked out, without `refs/heads/`; `None` when detached."""

    locked: bool
    """Whether the worktree is locked."""

    prunable: bool
    """Whether git reports the worktree as prunable."""


@dataclass(frozen=True)
class Repository:
    """The repository a command works on."""

    top: Path
    """The top of the checkout the command was given, or the main checkout."""

    common: Path
    """The repository's common git folder."""

    main: Path
    """The main checkout, the first entry of `git worktree list`."""

    env: Mapping[str, str]
    """The environment of the git commands."""

    def git(
        self,
        *args: str,
        cwd: Path | None = None,
        changes: bool = False,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run git.

        Args:
            *args: The git arguments.
            cwd: The folder; `None` for `top`.
            changes: The command changes the repository, so it runs without
                a time limit; otherwise it may take `GIT_TIMEOUT` seconds.
            stdin: Text for the command's standard input.

        Returns:
            The finished command.
        """
        timeout = None if changes else GIT_TIMEOUT
        lock = (
            paths.worktree_lock(self.common, self.env)
            if args[:1] == ("worktree",)
            else contextlib.nullcontext()
        )
        try:
            with lock:
                return _git(
                    cwd or self.top, *args, env=self.env, timeout=timeout, stdin=stdin
                )
        except (OSError, paths.RuntimeDirError) as error:
            raise GitFailedError(
                exits.UNAVAILABLE,
                f"cannot use the worktree lock of {self.common}: {error}",
                exits.line(exits.UNAVAILABLE, "runtime") or "",
            ) from error

    def tip(self, branch: str) -> str | None:
        """Return a local branch's commit.

        Args:
            branch: The branch name, without `refs/heads/`.

        Returns:
            The commit, or `None` when there is no such branch.
        """
        result = self.git(
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"refs/heads/{branch}^{{commit}}",
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def branch_exists(self, branch: str) -> bool:
        """Return whether `branch` is a valid name of an existing local branch.

        Args:
            branch: The branch name.

        Returns:
            Whether it exists.
        """
        valid = self.git("check-ref-format", f"refs/heads/{branch}").returncode == 0
        return valid and self.tip(branch) is not None

    def worktrees(self) -> list[Worktree]:
        """List the repository's worktrees.

        Returns:
            Every worktree, the main checkout first.

        Raises:
            GitFailedError: `git worktree list` failed.
        """
        result = self.git("worktree", "list", "--porcelain", "-z")
        if result.returncode:
            raise GitFailedError(
                exits.WORKTREE_FAILED,
                f"git worktree list failed: {_git_message(result)}",
                f"next: git -C {self.top} worktree list",
            )
        found: list[Worktree] = []
        for block in result.stdout.split("\0\0"):
            fields: dict[str, str] = {}
            for item in block.split("\0"):
                if item:
                    key, _, value = item.partition(" ")
                    fields[key] = value
            if not fields.get("worktree"):
                continue
            branch = fields.get("branch")
            found.append(
                Worktree(
                    path=Path(fields["worktree"]),
                    branch=branch.removeprefix("refs/heads/") if branch else None,
                    locked="locked" in fields,
                    prunable="prunable" in fields,
                )
            )
        return found

    def unrecorded_worktree(self, branch: str, worktree_id: str) -> Path | None:
        """Return the worktree of a worker branch that has no record.

        Args:
            branch: The worker branch.
            worktree_id: The run id or flow id in the branch's name.

        Returns:
            The worktree `git worktree list` shows with `branch` checked out
            in a folder named `worktree_id`, other than the main checkout, or
            `None` when there is none.

        Raises:
            GitFailedError: `git worktree list` failed.
        """
        main = _resolved(self.main)
        for item in self.worktrees():
            if (
                item.branch == branch
                and item.path.name == worktree_id
                and _resolved(item.path) != main
            ):
                return item.path
        return None

    def changes(self, worktree: Path, *, skip_nested: bool = False) -> list[str]:
        """Return the changed paths of a worktree.

        Tracked changes and untracked files that are not ignored count;
        ignored files do not.

        Args:
            worktree: The worktree.
            skip_nested: Leave out other worktrees of the repository that sit
                inside this one and show as untracked.

        Returns:
            The paths, as `git status` names them.

        Raises:
            GitFailedError: `git status` failed.
        """
        result = self.git(
            "status", "--porcelain", "-z", "--untracked-files=all", cwd=worktree
        )
        if result.returncode:
            raise GitFailedError(
                exits.WORKTREE_FAILED,
                f"git status failed in {worktree}: {_git_message(result)}",
                f"next: git -C {worktree} status",
            )
        nested = (
            {_resolved(item.path) for item in self.worktrees()}
            if skip_nested
            else set()
        )
        found: list[str] = []
        entries = iter(result.stdout.split("\0"))
        for entry in entries:
            if not entry:
                continue
            name = entry[3:]
            if entry.startswith("?? ") and _resolved(worktree / name) in nested:
                continue
            found.append(name)
            if entry[0] in "RC":
                next(entries, None)
        return found


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _common(directory: str | Path, env: Mapping[str, str]) -> Path | None:
    if not Path(directory).is_dir():
        return None
    result = _git(
        directory, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env
    )
    return _resolved(Path(result.stdout.strip())) if result.returncode == 0 else None


def open_repository(
    directory: str | Path,
    env: Mapping[str, str],
    command: str,
    *,
    from_main: bool = False,
) -> Repository:
    """Find the repository of a folder and its main checkout.

    The main checkout is the first entry of `git worktree list`, as the run
    core finds it, also when `directory` is in a linked worktree.

    Args:
        directory: A folder in the git checkout.
        env: The environment of the git commands.
        command: `merge`, `cleanup` or `flow`, for the lines printed.
        from_main: Use the main checkout as `top`, so git commands keep a
            folder when cleanup removes the worktree `directory` is in.

    Returns:
        The repository.

    Raises:
        BranchError: The folder is not in a git checkout (65), or the main
            checkout cannot be found (70).
    """
    folder = _resolved(Path(directory))
    top = _git(folder, "rev-parse", "--show-toplevel", env=env)
    common = _common(folder, env) if top.returncode == 0 else None
    if top.returncode or not top.stdout.strip() or common is None:
        raise BranchError(
            exits.NOT_A_CHECKOUT,
            f"{folder} is not in a git checkout",
            f"next: emb {command} ... --dir <a folder in the git checkout>",
        )
    top_path = Path(top.stdout.strip())
    partial = Repository(top=top_path, common=common, main=top_path, env=env)
    entries = partial.worktrees()
    if not entries or not entries[0].path.is_dir() or entries[0].path == common:
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"the repository of {top_path} has no main checkout",
            f"next: git -C {top_path} worktree list",
        )
    main = entries[0].path
    return Repository(
        top=main if from_main else top_path, common=common, main=main, env=env
    )


@dataclass(frozen=True)
class FlowBranch:
    """A flow branch and its worktree, base commit and checkout locations."""

    branch: str
    worktree: Path
    base: str
    top: Path
    main: Path


def resolve_base(
    directory: str | Path, base: str, env: Mapping[str, str]
) -> tuple[Path, Path, str]:
    """Resolve a flow's checkout, main checkout and base commit.

    Raises:
        BranchError: No checkout (65), no main checkout (70), or a base
            that does not resolve to a commit (64).
    """
    repo = open_repository(directory, env, "flow")
    result = repo.git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    if result.returncode or not result.stdout.strip():
        raise BranchError(
            exits.USAGE,
            f"base {base} is not a commit",
            "next: emb flow --plan <plan> --dry-run (with base naming a commit)",
        )
    return repo.top, repo.main, result.stdout.strip()


def create_flow_branch(
    directory: str | Path,
    flow_id: str,
    base: str,
    env: Mapping[str, str],
    *,
    worktree: Path | None = None,
) -> FlowBranch:
    """Create a flow branch at its base, in a worktree outside the repository.

    Args:
        directory: A folder in the git checkout.
        flow_id: The flow id.
        base: The commit or branch the flow branch starts from.
        env: The environment of the git commands.
        worktree: The worktree path the flow record names; `None` for
            `ember.paths.worktree_path(flow_id, env)`.

    Returns:
        The branch, its worktree, base commit and checkout locations.

    Raises:
        BranchError: An invalid flow id (64), a worktree folder inside a
            folder of the repository (69), an unresolved base or checkout,
            or a failure to create the branch or worktree (70).
    """
    branch = f"ember/flow/{flow_id}"
    if FLOW_BRANCH.fullmatch(branch) is None:
        raise BranchError(
            exits.USAGE, f"invalid flow id: {flow_id!r}", "next: emb flow --help"
        )
    top, main, commit = resolve_base(directory, base, env)
    if worktree is None:
        worktree = paths.worktree_path(flow_id, env)
    line = f"next: git -C {main} worktree prune, then emb flow --plan <plan> --start"
    repo = open_repository(top, env, "flow")
    folders = [item.path for item in repo.worktrees()]
    try:
        paths.check_worktree_path(worktree, [*folders, repo.common], env)
    except paths.WorktreeDirError as error:
        raise BranchError(
            exits.UNAVAILABLE,
            str(error),
            f"owner: set {paths.worktree_setting(env)} so that "
            f"{paths.worktrees_dir(env)} is outside every git checkout",
        ) from error
    try:
        paths.ensure_worktrees_dir(env)
    except (OSError, ValueError) as error:
        raise BranchError(
            exits.WORKTREE_FAILED,
            f"cannot create the worktree folder {paths.worktrees_dir(env)}: {error}",
            line,
        ) from error
    result = repo.git(
        "worktree",
        "add",
        "-q",
        "-b",
        branch,
        str(worktree),
        commit,
        cwd=main,
        changes=True,
    )
    if result.returncode:
        raise BranchError(
            exits.WORKTREE_FAILED,
            f"git worktree add failed for {worktree}: {_git_message(result)}",
            line,
        )
    return FlowBranch(branch, worktree, commit, top, main)


CoreFactory = Callable[[TextIO], Core]
"""Returns the run core that closes sessions, printing on the given stream."""


class Cleanup:
    """Removes merged worker branches, their worktrees and sessions."""

    def __init__(
        self,
        repo: Repository,
        target: str | None,
        *,
        again: str,
        claim: str | None = None,
        dry_run: bool = False,
        core: CoreFactory | None = None,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        """Set up a cleanup.

        Args:
            repo: The repository.
            target: The merge target, or `None` for each record's base branch.
            again: The command that repeats this cleanup.
            claim: Handle only records with this exact claim id.
            dry_run: Report what would be done and change nothing.
            core: Returns the run core; `None` for `Core` with the
                repository's environment.
            out: Where each result's line goes; `None` prints nothing.
            err: Standard error; `None` for `sys.stderr`.
        """
        self.repo = repo
        self.target = target
        self.again = again
        self.claim = claim
        self.dry_run = dry_run
        self.err = err or sys.stderr
        self.out = out
        self.make_core: CoreFactory = core or (
            lambda stream: Core(repo.env, out=stream, err=stream)
        )
        self._core: Core | None = None
        self.results: list[Result] = []

    def worker_branches(self) -> list[str]:
        """List the repository's worker branches.

        Returns:
            Every local branch named `ember/<engine>/<run id>`, sorted.

        Raises:
            GitFailedError: `git for-each-ref` failed.
        """
        result = self.repo.git(
            "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads/ember/"
        )
        if result.returncode:
            raise GitFailedError(
                exits.WORKTREE_FAILED,
                f"git for-each-ref failed: {_git_message(result)}",
                f"next: {self.again}",
            )
        names = result.stdout.splitlines()
        return sorted(name for name in names if WORKER_BRANCH.fullmatch(name))

    def run(self, branches: list[str]) -> list[Result]:
        """Clean up each branch in turn.

        Args:
            branches: The branches.

        Returns:
            One result per branch matching the claim filter, if given.

        Raises:
            GitFailedError: A git command failed; `results` holds every result
                before it, then the branch it failed on, kept, with the steps
                done to it and the failed step.
        """
        if self.target is None:
            return self._own_targets(branches)
        for branch in branches:
            claim = self._branch_claim(branch)
            if self.claim is None or claim == self.claim:
                self._report(self._one(branch, claim))
        return self.results

    def _recorded_base(self, branch: str) -> str | None:
        """Read the base after checking the record belongs to this branch."""
        match = WORKER_BRANCH.fullmatch(branch)
        if match is None:
            raise _KeepError(NOT_WORKER, f"next: git branch -d {branch}")
        if FLOW_BRANCH.fullmatch(branch):
            try:
                flow = _matching_flow(self.repo, branch)
            except BranchError as error:
                raise _KeepError(str(error), error.line) from error
            return flow.get("base_branch") if flow is not None else None
        run = self._record(match["run"], match["engine"], branch)
        return run.base_branch if run is not None else None

    def _base_target(self, base: str | None) -> str:
        """Follow deleted flow branches until a surviving base is found."""
        target = base
        seen: set[str] = set()
        while target and target not in seen:
            seen.add(target)
            if self.repo.branch_exists(target):
                return target
            try:
                flow = _matching_flow(self.repo, target)
            except BranchError as error:
                raise _KeepError(str(error), error.line) from error
            target = flow.get("base_branch") if flow is not None else None
        reason = (
            f"no surviving base branch found from {base}"
            if base
            else "no base branch is recorded"
        )
        raise _KeepError(reason, f"next: {self.again}")

    def _own_targets(self, branches: list[str]) -> list[Result]:
        """Clean up claimed records before the flow branches they were cut from."""
        targets: dict[str, str | None] = {}
        keeps: dict[str, _KeepError] = {}
        for branch in branches:
            if self._branch_claim(branch) != self.claim:
                continue
            try:
                targets[branch] = self._base_target(self._recorded_base(branch))
            except _KeepError as error:
                targets[branch] = None
                keeps[branch] = error
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(branch: str) -> None:
            if branch in seen:
                return
            seen.add(branch)
            for child, target in targets.items():
                if target == branch:
                    visit(child)
            ordered.append(branch)

        for branch in targets:
            visit(branch)
        for branch in ordered:
            if branch in keeps:
                error = keeps[branch]
                match = WORKER_BRANCH.fullmatch(branch)
                self._report(
                    Result(
                        branch,
                        match["run"] if match else None,
                        KEPT,
                        str(error),
                        error.line,
                        self.claim,
                    )
                )
                continue
            target = targets[branch]
            assert target is not None
            job = Cleanup(
                self.repo,
                target,
                again=_again(target, branch, self.repo.top, self.claim),
                claim=self.claim,
                dry_run=self.dry_run,
                core=self.make_core,
                out=self.out,
                err=self.err,
            )
            try:
                job.run([branch])
            finally:
                self.results.extend(job.results)
        return self.results

    def _branch_claim(self, branch: str) -> str | None:
        """Read the recorded claim, without consulting live choices."""
        match = WORKER_BRANCH.fullmatch(branch)
        if match is None:
            return None
        try:
            if FLOW_BRANCH.fullmatch(branch):
                return flow_record.load(match["run"], self.repo.env).get("claim")
            return record.load(match["run"], self.repo.env).claim
        except record.RecordError, flow_record.FlowError, OSError, ValueError:
            return None

    def _report(self, result: Result) -> None:
        self.results.append(result)
        if self.out is not None:
            print(result.text(), file=self.out, flush=True)

    def detached(self) -> None:
        """Remove safe detached worktrees of this repository's ended runs."""
        runs, errors = record.list_runs(self.repo.env)
        for error in errors:
            print(f"emb cleanup: skipped {error}", file=self.err)
        for run in runs:
            if (
                not run.read_only
                or (self.claim is not None and run.claim != self.claim)
                or not run.detached
                or run.status not in (record.CLOSED, record.FAILED)
                or not run.common
                or _resolved(Path(run.common)) != _resolved(self.repo.common)
            ):
                continue
            try:
                with self._removal_lock(run):
                    run = record.load(run.id, self.repo.env)
                    with paths.worktree_lock(self.repo.common, self.repo.env):
                        present = worktrees.remove_detached(run, dry_run=self.dry_run)
                if present:
                    self._report(
                        Result(
                            None,
                            run.id,
                            WOULD_REMOVE if self.dry_run else REMOVED,
                            f"{'removes' if self.dry_run else 'removed'} "
                            f"worktree {run.worktree}",
                            None,
                            run.claim,
                        )
                    )
            except (OSError, record.RecordError, _KeepError) as error:
                self._report(
                    Result(
                        None, run.id, KEPT, str(error), f"next: {self.again}", run.claim
                    )
                )

    def _one(self, branch: str, claim: str | None) -> Result:
        """Clean up one branch.

        Raises:
            GitFailedError: A git command failed; `results` ends with the
                branch kept, what was done to it and the failed step.
        """
        match = WORKER_BRANCH.fullmatch(branch)
        run_id = match["run"] if match else None
        done: list[str] = []
        try:
            return replace(self._clean(branch, match, done), claim=claim)
        except _KeepError as keep:
            return Result(branch, run_id, KEPT, str(keep), keep.line, claim)
        except GitFailedError as error:
            reason = "; ".join([*done, str(error)])
            self._report(Result(branch, run_id, KEPT, reason, error.line, claim))
            raise

    def _contained(self, tip: str, target_tip: str) -> bool:
        return is_contained(
            self.repo.top, tip, target_tip, self.repo.env, scratch=self.dry_run
        )

    def _tips(self, branch: str) -> tuple[str, str]:
        assert self.target is not None
        tip = self.repo.tip(branch)
        if tip is None:
            raise _KeepError(f"{branch} no longer exists", f"next: {self.again}")
        target_tip = self.repo.tip(self.target)
        if target_tip is None:
            raise _KeepError(f"{self.target} no longer exists", f"next: {self.again}")
        if not self._contained(tip, target_tip):
            raise _KeepError(
                f"not contained in {self.target}",
                f"next: emb merge {branch} --into {self.target} --verdict keep "
                "--note '<verdict line>' --subject '<commit subject>' "
                "(after a Keep verdict)"
                if self.claim is not None
                else None,
            )
        return tip, target_tip

    def _clean(
        self, branch: str, match: re.Match[str] | None, parts: list[str]
    ) -> Result:
        """Clean up one branch, adding each step done to `parts`."""
        if branch == self.target:
            raise _KeepError(
                "the merge target",
                "next: emb cleanup --merged-into <the branch it was merged into>",
            )
        if match is None:
            raise _KeepError(NOT_WORKER, f"next: git branch -d {branch}")
        engine, run_id = match["engine"], match["run"]
        if FLOW_BRANCH.fullmatch(branch):
            line = f"next: emb flow --status {run_id}"
            try:
                live = flow_lock.live(run_id, self.repo.env)
            except flow_lock.LockError as error:
                raise _KeepError(str(error), line) from error
            if live:
                raise _KeepError(f"the flow {run_id} is running", line)
        tip, target_tip = self._tips(branch)
        run: Run | None = None
        if FLOW_BRANCH.fullmatch(branch):
            folder = self._flow_worktree(run_id, branch)
        else:
            run = self._record(run_id, engine, branch)
            folder = Path(run.worktree) if run is not None else None
        if folder is None:
            folder = self.repo.unrecorded_worktree(branch, run_id)
        present = self._check_worktree(branch, folder)
        parts.append(f"contained in {self.target}")
        if self.dry_run:
            if run is not None and run.status == record.CLOSED:
                with self._removal_lock(run):
                    pass
            if run is not None and run.status != record.CLOSED:
                _check_closable(run)
                parts.append(f"closes {run_id}")
            if present:
                parts.append(f"removes worktree {folder}")
            parts.append(f"deletes {branch} at {tip}")
            return Result(branch, run_id, WOULD_REMOVE, "; ".join(parts), None)
        if run is not None and run.status != record.CLOSED:
            self._close(run)
            parts.append(f"closed {run_id}")
        with self._removal_lock(run):
            return self._remove_branch(
                branch, run_id, folder, present, tip, target_tip, parts
            )

    @contextlib.contextmanager
    def _removal_lock(self, run: Run | None) -> Iterator[None]:
        """Hold run locks through the removal check and worktree removal."""
        if run is None:
            yield
            return
        try:
            with (
                Core(self.repo.env)._turn_lock(run.id, "close"),
                record.record_lock(run),
            ):
                current = record.load(run.id, self.repo.env)
                reason = worktree_removal_reason(current)
                if reason:
                    raise _KeepError(reason, f"next: emb run --close {run.id}")
                yield
        except (RunError, OSError, record.RecordError) as error:
            raise _KeepError(str(error), f"next: emb run --close {run.id}") from error

    def _remove_branch(
        self,
        branch: str,
        run_id: str,
        folder: Path | None,
        present: bool,
        tip: str,
        target_tip: str,
        parts: list[str],
    ) -> Result:
        """Remove a branch and its worktree while any run locks are held."""
        assert self.target is not None
        if (self.repo.tip(branch), self.repo.tip(self.target)) != (tip, target_tip):
            moved = branch if self.repo.tip(branch) != tip else self.target
            raise _KeepError(f"{moved} moved", f"next: {self.again}")
        tip, _ = self._tips(branch)
        if present and folder is not None:
            self._check_worktree(branch, folder)
            self._git_step(
                ["worktree", "remove", "--", str(folder)],
                f"removing the worktree {folder}",
                f"git -C {self.repo.main} worktree remove {folder}",
            )
            parts.append(f"removed worktree {folder}")
        self._git_step(
            ["worktree", "prune"], "git worktree prune", "git worktree prune"
        )
        deleted = self.repo.git(
            "update-ref", "-d", f"refs/heads/{branch}", tip, changes=True
        )
        if deleted.returncode:
            if self.repo.tip(branch) != tip:
                raise _KeepError(f"{branch} moved", f"next: {self.again}")
            raise GitFailedError(
                exits.WORKTREE_FAILED,
                f"deleting the branch {branch} failed: {_git_message(deleted)}",
                f"next: git -C {self.repo.top} branch -d {branch}, then {self.again}",
            )
        parts.append(f"deleted at {tip}")
        return Result(branch, run_id, REMOVED, "; ".join(parts), None)

    def _git_step(self, args: list[str], step: str, retry: str) -> None:
        result = self.repo.git(*args, cwd=self.repo.main, changes=True)
        if result.returncode:
            raise GitFailedError(
                exits.WORKTREE_FAILED,
                f"{step} failed: {_git_message(result)}",
                f"next: {retry}, then {self.again}",
            )

    def _record(self, run_id: str, engine: str, branch: str) -> Run | None:
        """Read the branch's run record and check that it is this branch's.

        Returns:
            The run, or `None` when there is no record.
        """
        try:
            run = record.load(run_id, self.repo.env)
        except record.RecordMissingError:
            return None
        except record.RecordError as error:
            folder = record.run_dir(run_id, self.repo.env)
            raise _KeepError(
                f"cannot read the run record: {error}",
                f"owner: repair or remove {folder / record.RECORD_NAME}",
            ) from error
        mismatch = None
        if run.engine != engine:
            mismatch = f"its engine is {run.engine}"
        elif run.branch != branch:
            mismatch = f"its branch is {run.branch}"
        else:
            mismatch = self._mismatch(
                run_id, run.worktree, run.main or run.repo, run.common
            )
        if mismatch:
            raise _KeepError(
                f"the run record {run_id} is not this branch's: {mismatch}",
                f"next: emb run --status {run_id}",
            )
        return run

    def _flow_worktree(self, flow_id: str, branch: str) -> Path | None:
        """Read the flow branch's record and return the worktree it names.

        Returns:
            The worktree, or `None` when there is no record.
        """
        try:
            flow = flow_record.load(flow_id, self.repo.env)
        except flow_record.FlowMissingError:
            return None
        except (flow_record.FlowError, OSError, ValueError) as error:
            folder = flow_record.folder(flow_id, self.repo.env)
            raise _KeepError(
                f"cannot read the flow record: {error}",
                f"owner: repair or remove {folder / flow_record.FLOW_NAME}",
            ) from error
        mismatch = None
        if flow["branch"] != branch:
            mismatch = f"its branch is {flow['branch']}"
        else:
            mismatch = self._mismatch(
                flow_id, flow["worktree"], flow["main"], flow.get("common")
            )
        if mismatch:
            raise _KeepError(
                f"the flow record {flow_id} is not this branch's: {mismatch}",
                f"next: emb flow --status {flow_id}",
            )
        return Path(flow["worktree"])

    def _mismatch(
        self, worktree_id: str, worktree: str, checkout: str, common: str | None
    ) -> str | None:
        """Return why a record's worktree and checkout are not this branch's.

        A record's worktree is a folder named by its id, other than the main
        checkout, in this repository. The recorded common git folder
        identifies the repository; older records query their checkout.
        """
        folder = Path(worktree)
        if folder.name != worktree_id or _resolved(folder) == _resolved(self.repo.main):
            return f"its worktree {worktree} is not a worktree of {worktree_id}"
        recorded_common = (
            _resolved(Path(common))
            if common is not None
            else _common(checkout, self.repo.env) or _common(worktree, self.repo.env)
        )
        if recorded_common is not None and recorded_common != self.repo.common:
            return f"its checkout {checkout} is in another repository"
        return None

    def _check_worktree(self, branch: str, folder: Path | None) -> bool:
        """Check where the branch is checked out and the branch's worktree.

        Args:
            branch: The worker branch.
            folder: The branch's worktree, from its record or `git worktree
                list`; `None` when it has none.

        Returns:
            Whether the branch's worktree is there to remove.
        """
        expected = _resolved(folder) if folder is not None else None
        own = None
        for item in self.repo.worktrees():
            path = _resolved(item.path)
            if path == expected:
                own = item
            elif item.branch == branch:
                raise _KeepError(
                    f"checked out in {item.path}",
                    f"next: git -C {item.path} switch --detach, then {self.again}",
                )
        if folder is None or own is None or own.prunable or not folder.is_dir():
            return False
        if own.branch != branch:
            held = own.branch or "a detached HEAD"
            raise _KeepError(
                f"the run's worktree {folder} has {held} checked out",
                f"next: git -C {folder} status",
            )
        if own.locked:
            raise _KeepError(
                f"the worktree {folder} is locked",
                f"next: git worktree unlock {folder}, then {self.again}",
            )
        if self.repo.changes(folder):
            raise _KeepError(
                f"the worktree {folder} has changes",
                f"next: git -C {folder} status --short",
            )
        return True

    def _close(self, run: Run) -> None:
        if self._core is None:
            self._core = self.make_core(self.err)
        try:
            self._core.close(run.id)
        except RunError as error:
            line = exits.line(error.code, error.variant, **error.context) or ""
            if error.code == exits.REFUSED:
                raise _KeepError(str(error), line) from error
            if not line.startswith("owner: "):
                line = (
                    f"owner: repair what the message names (emb run --status "
                    f"{run.id}), then {self.again}"
                )
            raise _KeepError(str(error), line) from error
        except Exception as error:  # noqa: BLE001
            raise _KeepError(
                f"closing {run.id} failed: {type(error).__name__}: {error}",
                f"owner: repair what the message names (emb run --status "
                f"{run.id}), then {self.again}",
            ) from error


def _check_closable(run: Run) -> None:
    """Keep a run whose record shows that its close would be refused.

    A turn recorded as sent counts as running, and a collected turn of a
    writing run as not committed, with the reason and line the close gives.
    """
    if run.turn_state == record.COLLECTED and not run.read_only:
        reason = f"turn {run.turn} of {run.id} is collected but not committed"
        variant = "collected"
    elif run.turn_state == record.SENT:
        reason = f"turn {run.turn} of {run.id} is running"
        variant = "close"
    else:
        return
    raise _KeepError(reason, exits.line(exits.REFUSED, variant, run=run.id) or "")


def _print_failure(
    command: str,
    error: BranchError,
    err: TextIO,
    failures: list[BranchError] | None = None,
) -> int:
    """Print a branch failure and expose its variant to the CLI."""
    if failures is not None:
        failures.append(error)
    print(f"emb {command}: {error}", file=err)
    print(error.line, file=err)
    return error.code


def _again(
    target: str | None, branch: str | None, top: Path, claim: str | None = None
) -> str:
    scope = f" --branch {branch}" if branch else ""
    if claim is not None:
        scope += f" --claim {shlex.quote(claim)}"
    into = f" --merged-into {target}" if target is not None else ""
    return f"emb cleanup{into}{scope} --dir {top}"


def cleanup(
    target: str | None = None,
    *,
    branch: str | None = None,
    claim: str | None = None,
    directory: str | Path = ".",
    dry_run: bool = False,
    as_json: bool = False,
    env: Mapping[str, str] | None = None,
    core: CoreFactory | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    failures: list[BranchError] | None = None,
) -> int:
    """Run `emb cleanup`.

    Args:
        target: The merge target, or `None` for each claimed record's base branch.
        branch: Clean up only this branch.
        claim: Handle only records with this exact claim id; no live claim needed.
        directory: A folder in the git checkout.
        dry_run: Report what would be done and change nothing.
        as_json: Print a JSON list of results.
        env: The environment; `None` reads `os.environ`.
        core: Returns the run core that closes sessions.
        out: Standard output; `None` for `sys.stdout`.
        err: Standard error; `None` for `sys.stderr`.
        failures: Receives failures for CLI report lines.

    Returns:
        The exit code.
    """
    env = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    if target is None and claim is None:
        return _print_failure(
            "cleanup",
            BranchError(
                exits.USAGE,
                "--merged-into is required without --claim",
                "next: emb cleanup --help",
            ),
            err,
            failures,
        )
    try:
        repo = open_repository(directory, env, "cleanup", from_main=True)
    except BranchError as error:
        return _print_failure("cleanup", error, err, failures)
    again = _again(target, branch, repo.top, claim)
    for name in (target, branch):
        if name is not None and not repo.branch_exists(name):
            return _print_failure(
                "cleanup",
                BranchError(
                    exits.NOT_A_CHECKOUT,
                    f"no local branch {name}",
                    f"next: git -C {repo.top} branch --list",
                ),
                err,
            )
    job = Cleanup(
        repo,
        target,
        again=again,
        claim=claim,
        dry_run=dry_run,
        core=core,
        out=None if as_json else out,
        err=err,
    )
    failure: GitFailedError | None = None
    try:
        job.run([branch] if branch else job.worker_branches())
        if branch is None:
            job.detached()
    except GitFailedError as error:
        failure = error
    if as_json:
        print(json.dumps([item.to_json() for item in job.results], indent=2), file=out)
    elif not job.results and failure is None:
        scope = f" for claim {claim}" if claim is not None else ""
        against = target if target is not None else "each record's base branch"
        print(
            f"emb cleanup: no Ember run or flow branch left against {against}{scope}",
            file=err,
        )
    if failure is not None:
        return _print_failure("cleanup", failure, err, failures)
    if (
        claim is not None
        and not dry_run
        and not any(item.action == KEPT and item.claim == claim for item in job.results)
    ):
        print(f"next: emb choices --finish {shlex.quote(claim)}", file=err)
    return exits.OK


def _target_worktree(repo: Repository, target: str) -> Path:
    for item in repo.worktrees():
        if item.branch == target and not item.prunable and item.path.is_dir():
            return item.path
    raise BranchError(
        exits.REFUSED,
        f"{target} is not checked out in any worktree of this repository",
        f"next: git -C {repo.top} switch {target}, then repeat the merge",
    )


def _check_no_operation(
    repo: Repository, branch: str, target: str, worktree: Path
) -> None:
    """Refuse a target worktree with an unfinished merge, rebase or the like.

    Raises:
        BranchError: The worktree's git folder shows one (75), or cannot be
            found (70).
    """
    found = repo.git("rev-parse", "--path-format=absolute", "--git-dir", cwd=worktree)
    if found.returncode or not found.stdout.strip():
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"git rev-parse failed in {worktree}: {_git_message(found)}",
            f"next: git -C {worktree} status",
        )
    folder = Path(found.stdout.strip())
    for name, operation in IN_PROGRESS:
        if (folder / name).exists():
            raise BranchError(
                exits.REFUSED,
                f"the {target} tree at {worktree} has an unfinished {operation}",
                f"next: git -C {worktree} status; finish or abort the "
                f"{operation}, then emb merge {branch} --into {target} "
                "--verdict keep --note '<verdict line>'",
            )


def _merge_title(
    repo: Repository, branch: str, target: str, tip: str, worktree: Path
) -> str:
    """Return git's default merge message for merging the branch by name.

    The merge itself names `refs/heads/<branch>`, so that a tag of the same
    name is never merged; git would put that full name in its message, so the
    message is made by `git fmt-merge-msg` for the short name.

    Raises:
        GitFailedError: `git fmt-merge-msg` failed (70).
    """
    made = repo.git(
        "fmt-merge-msg",
        "--no-log",
        cwd=worktree,
        stdin=f"{tip}\t\tbranch '{branch}' of .\n",
    )
    title = made.stdout.strip()
    if made.returncode or not title:
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"git fmt-merge-msg failed in {worktree}: {_git_message(made)}",
            f"next: git -C {worktree} status, then emb merge {branch} --into "
            f"{target} --verdict keep --note '<verdict line>'",
        )
    return title


def _matching_flow(repo: Repository, branch: str) -> dict[str, Any] | None:
    """Read a flow record matching the branch and repository identity."""
    match = FLOW_BRANCH.fullmatch(branch)
    if match is None:
        return None
    flow_id = match["run"]
    line = f"next: emb flow --status {flow_id}"
    try:
        flow = flow_record.load(flow_id, repo.env)
    except flow_record.FlowMissingError:
        return None
    except (flow_record.FlowError, OSError, ValueError) as error:
        raise BranchError(
            exits.REFUSED, f"cannot read the flow record: {error}", line
        ) from error
    folder = Path(flow["worktree"])
    common = (
        _resolved(Path(flow["common"]))
        if flow.get("common") is not None
        else _common(flow["main"], repo.env) or _common(folder, repo.env)
    )
    actual_common = _common(folder, repo.env)
    if (
        flow["branch"] != branch
        or common != repo.common
        or folder.name != flow_id
        or _resolved(folder) == _resolved(repo.main)
        or (actual_common is not None and actual_common != repo.common)
    ):
        raise BranchError(
            exits.REFUSED,
            f"the flow record {flow_id} is not this branch's in {repo.main}",
            line,
        )
    return flow


def _completed_flow(repo: Repository, branch: str) -> dict[str, Any] | None:
    """Read a matching flow record, returning only completed flows."""
    flow = _matching_flow(repo, branch)
    return flow if flow is not None and flow["status"] == "done" else None


def _unfinished_flow_line(repo: Repository, target: str) -> str | None:
    """Return an unfinished target flow's next step when its record matches."""
    try:
        flow = _matching_flow(repo, target)
    except BranchError:
        return None
    if flow is None or flow["status"] == "done":
        return None
    action = (
        "resume"
        if flow["status"] == "failed" and flow.get("check_pending")
        else "status"
    )
    return f"next: emb flow --{action} {flow['id']}"


def _check_flow_base(
    repo: Repository,
    flow: Mapping[str, Any],
    branch: str,
    target: str,
    head: str,
    tip: str,
) -> None:
    """Require the completed flow tip to contain the selected target tip."""
    checked = repo.git("merge-base", "--is-ancestor", head, tip)
    if checked.returncode == 0:
        return
    folder = shlex.quote(flow["worktree"])
    check = (
        f"run the plan check again there: {shlex.join(flow['check'])}"
        if flow["check"]
        else "no plan check is configured"
    )
    raise BranchError(
        exits.REFUSED,
        f"flow {branch} at {tip} does not contain target {target} at {head}"
        + (f": {_git_message(checked)}" if checked.returncode != 1 else ""),
        f"next: merge {target} into {branch} in the flow worktree: "
        f"git -C {folder} switch {shlex.quote(branch)} && "
        f"git -C {folder} merge {shlex.quote('refs/heads/' + target)}; "
        f"{check}; then {shlex.join(['emb', 'merge', branch, '--into', target])} "
        "--verdict keep --note '<verdict line>'",
    )


def _squash_message(
    repo: Repository,
    head: str,
    tip: str,
    subject: str,
    note: str | None,
    worktree: Path,
) -> tuple[str, bool]:
    """Collect Git trailers and report breaking signals in the worker history."""
    separators = _trailer_separators(worktree, repo.env)
    separator = separators[0] if separators else ":"
    base = repo.git("merge-base", head, tip, cwd=worktree)
    if base.returncode:
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"git merge-base failed: {_git_message(base)}",
            "next: emb merge --help",
        )
    result = repo.git(
        "log",
        "--reverse",
        "--format=%H%x00%B%x00",
        f"{base.stdout.strip()}..{tip}",
        "--",
        cwd=worktree,
    )
    if result.returncode:
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"reading worker trailers failed: {_git_message(result)}",
            "next: emb merge --help",
        )
    trailers: list[str] = []
    seen: set[tuple[str, str]] = set()
    fields = result.stdout.split("\0")
    breaking = False
    for index in range(0, len(fields) - 1, 2):
        breaking = breaking or _breaking_change(fields[index + 1])
        parsed = repo.git(
            "show",
            "-s",
            "--format=%(trailers:unfold,only,key_value_separator=%x00,separator=%x00)",
            fields[index].strip(),
            "--",
            cwd=worktree,
        )
        if parsed.returncode:
            raise GitFailedError(
                exits.WORKTREE_FAILED,
                f"reading worker trailers failed: {_git_message(parsed)}",
                "next: emb merge --help",
            )
        pairs = parsed.stdout.rstrip("\n").split("\0")
        for offset in range(0, len(pairs) - 1, 2):
            key, value = pairs[offset].strip(), pairs[offset + 1].strip()
            pair = (key.lower(), value)
            if pair[0].startswith("ember-") or pair in seen:
                continue
            seen.add(pair)
            trailers.append(f"{key}{separator} {value}")
    if note is not None:
        trailers.append(f"{VERDICT_TRAILER}: {note}")
    trailers.append(f"{TASK_TIP_TRAILER}: {tip}")
    return subject + "\n\n" + "\n".join(trailers) + "\n", breaking


def _commit_tree(
    repo: Repository,
    worktree: Path,
    tree: str,
    parents: tuple[str, ...],
    message: str,
    operation: str,
) -> str:
    """Run the message hook and create a commit with the configured signing."""
    with tempfile.TemporaryDirectory(prefix="ember-merge-") as folder:
        message_file = Path(folder) / "COMMIT_EDITMSG"
        message_file.write_text(message, encoding="utf-8")
        hooks = (
            ("prepare-commit-msg", "commit-msg")
            if operation == "merge"
            else ("commit-msg",)
        )
        for name in hooks:
            hook = repo.git(
                "hook",
                "run",
                "--ignore-missing",
                name,
                "--",
                str(message_file),
                *(("merge",) if name == "prepare-commit-msg" else ()),
                cwd=worktree,
                changes=True,
            )
            if hook.returncode:
                raise BranchError(
                    exits.HOOK_REFUSED,
                    f"{name} hook refused the {operation}: {_git_message(hook)}",
                    "next: fix the commit message or hook, then repeat emb merge",
                )
        message = message_file.read_text(encoding="utf-8")
    signing = repo.git("config", "--type=bool", "--get", "commit.gpgsign", cwd=worktree)
    if signing.returncode not in (0, 1):
        raise BranchError(
            exits.HOOK_REFUSED,
            f"cannot read commit.gpgsign: {_git_message(signing)}",
            "next: fix commit.gpgsign, then repeat emb merge",
        )
    commit = repo.git(
        "commit-tree",
        *(("-S",) if signing.stdout.strip() == "true" else ()),
        tree,
        *(arg for parent in parents for arg in ("-p", parent)),
        cwd=worktree,
        stdin=message,
        changes=True,
    )
    if commit.returncode:
        raise BranchError(
            exits.HOOK_REFUSED,
            f"git commit-tree refused the {operation}: {_git_message(commit)}",
            "next: fix the commit identity or signing configuration, "
            "then repeat emb merge",
        )
    return commit.stdout.strip()


def _squash(
    repo: Repository,
    branch: str,
    target: str,
    worktree: Path,
    head: str,
    tip: str,
    subject: str,
    note: str | None,
    warnings: list[str] | None = None,
) -> tuple[str, str]:
    """Build a reviewed commit before fast-forwarding the target worktree."""
    tree = repo.git("merge-tree", "--write-tree", "--name-only", "-z", head, tip)
    if tree.returncode:
        if tree.returncode == 1:
            conflicts: list[str] = []
            for path in tree.stdout.split("\0")[1:]:
                if not path:
                    break
                conflicts.append(path)
            raise BranchError(
                exits.REFUSED,
                f"conflict in {', '.join(conflicts) or _git_message(tree)}; "
                f"{target} unchanged at {head}",
                f"owner: resolve the conflict on {branch}, or ask for a new run "
                "cut from the updated target",
                "conflict",
            )
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"git merge-tree failed: {_git_message(tree)}",
            f"next: git -C {worktree} status",
        )
    message, breaking = _squash_message(repo, head, tip, subject, note, worktree)
    made = _commit_tree(
        repo, worktree, tree.stdout.split("\0", 1)[0], (head,), message, "squash"
    )
    checked_out = repo.git("symbolic-ref", "-q", "HEAD", cwd=worktree)
    if (
        repo.tip(target) != head
        or repo.tip(branch) != tip
        or checked_out.stdout.strip() != f"refs/heads/{target}"
    ):
        raise BranchError(
            exits.MOVED,
            "the target or worker branch moved while building the squash",
            "next: review the branch tips, then repeat emb merge",
        )
    advanced = repo.git(
        "merge",
        "--ff-only",
        "--no-edit",
        "--no-verify",
        made,
        cwd=worktree,
        changes=True,
    )
    if advanced.returncode:
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"fast-forward of {target} failed: {_git_message(advanced)}",
            f"next: git -C {worktree} status, then repeat emb merge",
        )
    if breaking and not _breaking_subject(subject) and warnings is not None:
        warnings.append(
            f"WARNING: {branch} holds a breaking change; the subject has no !"
        )
    return made, f"merged {branch} at {tip} into {target}: {made}"


def _merge_completed_flow(
    repo: Repository,
    branch: str,
    target: str,
    worktree: Path,
    head: str,
    tip: str,
    note: str | None,
) -> tuple[str, str]:
    """Build the merge commit before fast-forwarding the target worktree.

    The merge tree and commit leave the target's index and files untouched.
    Both branch tips and the checked-out target are checked before the
    fast-forward. A refused fast-forward leaves files as Git left them.
    """
    tree = repo.git("merge-tree", "--write-tree", "--name-only", "-z", head, tip)
    if tree.returncode:
        if tree.returncode == 1:
            conflicts: list[str] = []
            for path in tree.stdout.split("\0")[1:]:
                if not path:
                    break
                conflicts.append(path)
            raise BranchError(
                exits.REFUSED,
                f"conflict in {', '.join(conflicts) or _git_message(tree)}; "
                f"{target} unchanged at {head}",
                f"owner: resolve the conflict on {branch}, or ask for a new run "
                "cut from the updated target",
                "conflict",
            )
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"git merge-tree failed: {_git_message(tree)}",
            f"next: git -C {shlex.quote(str(worktree))} status",
        )
    message = _merge_title(repo, branch, target, tip, worktree)
    if note is not None:
        message += f"\n\n{VERDICT_TRAILER}: {note}"
    hook = repo.git(
        "hook",
        "run",
        "--ignore-missing",
        "pre-merge-commit",
        cwd=worktree,
        changes=True,
    )
    if hook.returncode:
        raise BranchError(
            exits.HOOK_REFUSED,
            f"pre-merge-commit hook refused the merge: {_git_message(hook)}",
            "next: fix the hook, then repeat emb merge",
        )
    made = _commit_tree(
        repo, worktree, tree.stdout.split("\0", 1)[0], (head, tip), message, "merge"
    )
    checked_out = repo.git("symbolic-ref", "-q", "HEAD", cwd=worktree)
    if (
        repo.tip(target) != head
        or repo.tip(branch) != tip
        or checked_out.stdout.strip() != f"refs/heads/{target}"
    ):
        raise BranchError(
            exits.MOVED,
            f"target {target} at {head} or flow {branch} at {tip} moved before "
            "the merge update; nothing merged",
            "next: review the branch tips, then repeat emb merge",
        )
    advanced = repo.git(
        "merge",
        "--ff-only",
        "--no-edit",
        "--no-verify",
        made,
        cwd=worktree,
        changes=True,
    )
    if advanced.returncode:
        raise GitFailedError(
            exits.WORKTREE_FAILED,
            f"fast-forward of {target} failed: {_git_message(advanced)}",
            f"next: git -C {shlex.quote(str(worktree))} status, then repeat emb merge",
        )
    return made, f"merged {branch} at {tip} into {target}: {made}"


def _merge(
    repo: Repository,
    branch: str,
    target: str,
    worktree: Path,
    note: str | None,
    subject: str | None = None,
    *,
    warnings: list[str] | None = None,
) -> tuple[str | None, str]:
    """Merge `branch` into `target` in `worktree` unless it is contained.

    A `note` is added as an `Ember-Verdict:` trailer. A `subject` selects
    a squash commit; otherwise git creates a merge commit.

    Returns:
        The landed commit, or `None` when the branch was contained, and the
        line printed.
    """
    head = repo.tip(target)
    branch_tip = repo.tip(branch)
    if head is None or branch_tip is None:
        raise BranchError(
            exits.NOT_A_CHECKOUT,
            f"no local branch {branch if branch_tip is None else target}",
            f"next: git -C {repo.top} branch --list",
        )
    flow = _completed_flow(repo, branch)
    if flow is not None:
        _check_flow_base(repo, flow, branch, target, head, branch_tip)
    if is_contained(repo.top, branch_tip, head, repo.env):
        return (
            None,
            f"already contained: {branch} at {branch_tip} in {target} at {head}",
        )
    _check_no_operation(repo, branch, target, worktree)
    changed = repo.changes(worktree, skip_nested=True)
    if changed:
        raise BranchError(
            exits.REFUSED,
            f"the {target} tree at {worktree} is not clean: {', '.join(changed)}",
            f"next: git -C {worktree} status --short; commit or set aside those "
            f"changes, then emb merge {branch} --into {target} --verdict keep "
            "--note '<verdict line>'",
        )
    if subject is not None:
        return _squash(
            repo, branch, target, worktree, head, branch_tip, subject, note, warnings
        )
    if flow is not None:
        return _merge_completed_flow(
            repo, branch, target, worktree, head, branch_tip, note
        )
    merged = repo.git(
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        _merge_title(repo, branch, target, branch_tip, worktree),
        *(("-m", f"{VERDICT_TRAILER}: {note}") if note is not None else ()),
        "--end-of-options",
        f"refs/heads/{branch}",
        cwd=worktree,
        changes=True,
    )
    if merged.returncode == 0:
        commit = repo.git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        return commit, f"merged {branch} at {branch_tip} into {target}: {commit}"
    conflicts = repo.git(
        "diff", "--name-only", "--diff-filter=U", "-z", cwd=worktree
    ).stdout.split("\0")
    conflicts = [path for path in conflicts if path]
    # The worktree had no merge in progress before this one, so the abort
    # undoes only this merge.
    if (
        repo.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=worktree).returncode
        == 0
    ):
        repo.git("merge", "--abort", cwd=worktree, changes=True)
    if conflicts:
        raise BranchError(
            exits.REFUSED,
            f"conflict in {', '.join(conflicts)}; merge aborted, {target} "
            f"unchanged at {head}",
            f"owner: resolve the conflict on {branch}, or ask for a new run "
            "cut from the updated target",
            "conflict",
        )
    raise GitFailedError(
        exits.WORKTREE_FAILED,
        f"git merge failed, {target} at {repo.tip(target)}: {_git_message(merged)}",
        f"next: git -C {worktree} status, then emb merge {branch} --into {target} "
        "--verdict keep --note '<verdict line>'",
    )


def _merged_run_lines(job: Cleanup, branch: str) -> tuple[str | None, tuple[str, ...]]:
    """Derive the newest unparented run's next and owner lines after cleanup."""
    results = [item for item in job.results if item.branch == branch]
    kept = any(item.action == KEPT for item in results)
    runs = []
    for item in results:
        if item.run is not None:
            with contextlib.suppress(record.RecordError):
                runs.append(record.load(item.run, job.repo.env))
    for run in sorted(runs, key=lambda run: run.started_at, reverse=True):
        if run.parent is None:
            core = job._core or job.make_core(job.err)
            step, next_line = core.step(run)
            return (
                None if kept else next_line,
                run.owner_wait if step == "owner" else (),
            )
    return None, ()


def merge(
    branch: str,
    target: str,
    verdict: str | None,
    *,
    note: str | None = None,
    subject: str | None = None,
    directory: str | Path = ".",
    as_json: bool = False,
    env: Mapping[str, str] | None = None,
    core: CoreFactory | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    failures: list[BranchError] | None = None,
) -> int:
    """Run `emb merge`: merge a reviewed branch, then clean it up.

    Args:
        branch: The branch to merge.
        target: The branch to merge into.
        verdict: The lead's verdict; only `keep` merges.
        note: The lead's verdict line, printed first and kept as a trailer
            on the landed commit; `None` prints and records none.
        subject: The squash subject, required for an Ember run branch.
        directory: A folder in the git checkout.
        as_json: Print one JSON object.
        env: The environment; `None` reads `os.environ`.
        core: Returns the run core that closes sessions.
        out: Standard output; `None` for `sys.stdout`.
        err: Standard error; `None` for `sys.stderr`.
        failures: Receives failures for CLI report lines.

    Returns:
        The exit code.
    """
    env = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    if verdict != "keep":
        given = "no verdict" if verdict is None else f"the verdict {verdict!r}"
        return _print_failure(
            "merge",
            BranchError(
                exits.USAGE,
                f"{given} does not merge; only --verdict keep merges",
                f"next: review {branch}, then emb merge {branch} --into {target} "
                "--verdict keep --note '<verdict line>'",
            ),
            err,
        )
    if branch == target:
        return _print_failure(
            "merge",
            BranchError(
                exits.USAGE,
                "the branch and the target are the same",
                "next: emb merge --help",
            ),
            err,
        )
    try:
        repo = open_repository(directory, env, "merge", from_main=True)
        for name in (branch, target):
            if not repo.branch_exists(name):
                raise BranchError(
                    exits.NOT_A_CHECKOUT,
                    f"no local branch {name}",
                    f"next: git -C {repo.top} branch --list",
                )
        squash = WORKER_BRANCH.fullmatch(branch) and not FLOW_BRANCH.fullmatch(branch)
        if squash and (
            subject is None
            or not subject.strip()
            or any(c in subject for c in "\r\n\0")
        ):
            raise BranchError(
                exits.USAGE,
                "an Ember run branch requires --subject with one nonblank line",
                f"next: emb merge {branch} --into {target} --verdict keep "
                "--note '<verdict line>' --subject '<commit subject>'",
            )
        worktree = _target_worktree(repo, target)
        warnings: list[str] = []
        commit, line = _merge(
            repo,
            branch,
            target,
            worktree,
            note,
            subject.strip() if squash and subject is not None else None,
            warnings=warnings,
        )
    except BranchError as error:
        return _print_failure("merge", error, err, failures)
    for warning in warnings:
        print(warning, file=err)
    contained_before = commit is None
    if not as_json:
        if note is not None:
            print(f"Verdict: {note}", file=out)
        print(line, file=out, flush=True)
    job = Cleanup(
        repo,
        target,
        again=_again(target, branch, repo.top),
        core=core,
        out=None if as_json else out,
        err=err,
    )
    failure: GitFailedError | None = None
    try:
        job.run([branch])
    except GitFailedError as error:
        failure = error
    next_line, owner_wait = _merged_run_lines(job, branch)
    if failure is not None:
        next_line = None
    elif not any(item.action == KEPT for item in job.results if item.branch == branch):
        next_line = _unfinished_flow_line(repo, target) or next_line
    if as_json:
        document = {
            "branch": branch,
            "target": target,
            "note": note,
            "warnings": warnings,
            "merge_commit": commit,
            "contained_before": contained_before,
            "cleanup": [item.to_json() for item in job.results],
            "next": next_line,
        }
        if owner_wait:
            document["owner_wait"] = list(owner_wait)
        print(json.dumps(document, indent=2), file=out)
    for owner_line in owner_wait:
        print(owner_line, file=err)
    if failure is not None:
        return _print_failure("merge", failure, err, failures)
    if next_line is not None:
        print(next_line, file=err)
    return exits.OK
