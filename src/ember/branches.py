"""Merging reviewed run branches and cleaning up merged ones.

A worker branch is a run branch, `ember/<engine>/<run id>`, whose worktree is
`.ember/worktrees/<run id>` at the top of the repository's main checkout.
Flow branches use `ember/flow/<flow id>` and the same worktree layout.
Cleanup keeps a flow branch while its runner holds the lifetime lock.
Cleanup removes only worker branches, their worktrees and their sessions; a
session is closed through the run core, never through tmux directly.

A branch is contained in a target when it is an ancestor of the target, or
when merging it into the target cleanly gives the target's own tree. The
check changes no index, worktree or ref.

Each non-zero exit prints its message on standard error, then one `next:` or
`owner:` line, as `emb run` does.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from ember.flows import lock as flow_lock
from ember.runs import exits, record
from ember.runs.core import Core, RunError
from ember.runs.record import Run

GIT_TIMEOUT = 120
"""Seconds each git command may take, except one that changes the repository
(a merge, its abort, a worktree removal, a prune or a branch deletion), which
runs without a time limit."""

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


class BranchError(Exception):
    """A branch operation's exit, with its message and follow-up line."""

    def __init__(self, code: int, message: str, line: str) -> None:
        """Keep the exit code and the `next:` or `owner:` line.

        Args:
            code: The exit code.
            message: What happened.
            line: The line printed after the message.
        """
        super().__init__(message)
        self.code = code
        self.line = line


class GitFailedError(BranchError):
    """A git command that changes the repository failed; exit 70."""


class _KeepError(Exception):
    """A reason to keep a branch, with the line that resolves it."""

    def __init__(self, reason: str, line: str) -> None:
        super().__init__(reason)
        self.line = line


@dataclass(frozen=True)
class Result:
    """One act or keep of a cleanup."""

    branch: str
    """The branch."""

    run: str | None
    """The run id the branch name holds, or `None`."""

    action: str
    """`removed`, `kept` or `would remove`."""

    reason: str
    """Why."""

    next: str | None
    """For a keep: the line that resolves it, `next: <command>` or
    `owner: <step>`; `None` otherwise."""

    def to_json(self) -> dict[str, Any]:
        """Return the result as a JSON-ready dict.

        Returns:
            `branch`, `run`, `action`, `reason` and `next`.
        """
        return {
            "branch": self.branch,
            "run": self.run,
            "action": self.action,
            "reason": self.reason,
            "next": self.next,
        }

    def text(self) -> str:
        """Return the result's line.

        Returns:
            `<action> <branch>: <reason>`, and for a keep `; ` and its `next`.
        """
        line = f"{self.action} {self.branch}: {self.reason}"
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
        `True` when `tip` is an ancestor of `target`, or when merging `tip`
        into `target` gives no conflict and `target`'s own tree. A conflict,
        a missing commit or a failed git command gives `False`.
    """
    ancestor = _git(directory, "merge-base", "--is-ancestor", tip, target, env=env)
    if ancestor.returncode == 0:
        return True
    if ancestor.returncode != 1:
        return False
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
    """The top of the checkout the command was given."""

    common: Path
    """The repository's common git folder."""

    main: Path
    """The main checkout, which holds `.ember/worktrees/`."""

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
        return _git(cwd or self.top, *args, env=self.env, timeout=timeout, stdin=stdin)

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

    def run_worktree(self, run_id: str) -> Path:
        """Return the folder of a run's worktree.

        Args:
            run_id: The run id.

        Returns:
            `.ember/worktrees/<run id>` in the main checkout.
        """
        return self.main / ".ember" / "worktrees" / run_id

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
    directory: str | Path, env: Mapping[str, str], command: str
) -> Repository:
    """Find the repository of a folder and its main checkout.

    The main checkout is the first entry of `git worktree list`, as the run
    core finds it, also when `directory` is in a linked worktree.

    Args:
        directory: A folder in the git checkout.
        env: The environment of the git commands.
        command: `merge`, `cleanup` or `flow`, for the lines printed.

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
    return Repository(top=top_path, common=common, main=entries[0].path, env=env)


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
    directory: str | Path, flow_id: str, base: str, env: Mapping[str, str]
) -> FlowBranch:
    """Create a flow branch at its base in the main checkout's worktree folder.

    Raises:
        BranchError: An invalid flow id (64), an unresolved base or checkout,
            or a failure to create the branch or worktree (70).
    """
    branch = f"ember/flow/{flow_id}"
    if FLOW_BRANCH.fullmatch(branch) is None:
        raise BranchError(
            exits.USAGE, f"invalid flow id: {flow_id!r}", "next: emb flow --help"
        )
    top, main, commit = resolve_base(directory, base, env)
    worktree = main / ".ember" / "worktrees" / flow_id
    line = f"next: git -C {main} worktree prune, then emb flow --plan <plan> --start"
    common = _common(top, env)
    if common is None:
        raise BranchError(
            exits.WORKTREE_FAILED, "cannot find the common git folder", line
        )
    exclude = common / "info" / "exclude"
    try:
        try:
            text = exclude.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        if not any(
            item.strip() in (".ember/", "/.ember/", ".ember", "/.ember")
            for item in text.splitlines()
        ):
            exclude.parent.mkdir(parents=True, exist_ok=True)
            if text and not text.endswith("\n"):
                text += "\n"
            exclude.write_text(text + ".ember/\n", encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise BranchError(
            exits.WORKTREE_FAILED, f"cannot add .ember/ to {exclude}: {error}", line
        ) from error
    result = _git(
        main,
        "worktree",
        "add",
        "-q",
        "-b",
        branch,
        str(worktree),
        commit,
        env=env,
        timeout=None,
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
        target: str,
        *,
        again: str,
        dry_run: bool = False,
        core: CoreFactory | None = None,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        """Set up a cleanup.

        Args:
            repo: The repository.
            target: The branch the worker branches were merged into.
            again: The command that repeats this cleanup.
            dry_run: Report what would be done and change nothing.
            core: Returns the run core; `None` for `Core` with the
                repository's environment.
            out: Where each result's line goes; `None` prints nothing.
            err: Standard error; `None` for `sys.stderr`.
        """
        self.repo = repo
        self.target = target
        self.again = again
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
            One result per branch.

        Raises:
            GitFailedError: A git command failed; `results` holds every result
                before it, then the branch it failed on, kept, with the steps
                done to it and the failed step.
        """
        for branch in branches:
            self._report(self._one(branch))
        return self.results

    def _report(self, result: Result) -> None:
        self.results.append(result)
        if self.out is not None:
            print(result.text(), file=self.out, flush=True)

    def _one(self, branch: str) -> Result:
        """Clean up one branch.

        Raises:
            GitFailedError: A git command failed; `results` ends with the
                branch kept, what was done to it and the failed step.
        """
        match = WORKER_BRANCH.fullmatch(branch)
        run_id = match["run"] if match else None
        done: list[str] = []
        try:
            return self._clean(branch, match, done)
        except _KeepError as keep:
            return Result(branch, run_id, KEPT, str(keep), keep.line)
        except GitFailedError as error:
            reason = "; ".join([*done, str(error)])
            self._report(Result(branch, run_id, KEPT, reason, error.line))
            raise

    def _contained(self, tip: str, target_tip: str) -> bool:
        return is_contained(
            self.repo.top, tip, target_tip, self.repo.env, scratch=self.dry_run
        )

    def _tips(self, branch: str) -> tuple[str, str]:
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
                "(after a Keep verdict)",
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
        run = self._record(run_id, engine, branch)
        folder = self.repo.run_worktree(run_id)
        present = self._check_worktree(branch, folder)
        parts.append(f"contained in {self.target}")
        if self.dry_run:
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
        if (self.repo.tip(branch), self.repo.tip(self.target)) != (tip, target_tip):
            moved = branch if self.repo.tip(branch) != tip else self.target
            raise _KeepError(f"{moved} moved", f"next: {self.again}")
        tip, _ = self._tips(branch)
        if present:
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
        elif _resolved(Path(run.worktree)) != _resolved(self.repo.run_worktree(run_id)):
            mismatch = f"its worktree {run.worktree} is not this repository's"
        else:
            common = _common(run.repo, self.repo.env) or _common(
                run.worktree, self.repo.env
            )
            if common is not None and common != self.repo.common:
                mismatch = f"its checkout {run.repo} is in another repository"
        if mismatch:
            raise _KeepError(
                f"the run record {run_id} is not this branch's: {mismatch}",
                f"next: emb run --status {run_id}",
            )
        return run

    def _check_worktree(self, branch: str, folder: Path) -> bool:
        """Check where the branch is checked out and the run's worktree.

        Returns:
            Whether the run's worktree is there to remove.
        """
        expected = _resolved(folder)
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
        if own is None or own.prunable or not folder.is_dir():
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


def _print_failure(command: str, error: BranchError, err: TextIO) -> int:
    print(f"emb {command}: {error}", file=err)
    print(error.line, file=err)
    return error.code


def _again(target: str, branch: str | None, top: Path) -> str:
    scope = f" --branch {branch}" if branch else ""
    return f"emb cleanup --merged-into {target}{scope} --dir {top}"


def cleanup(
    target: str,
    *,
    branch: str | None = None,
    directory: str | Path = ".",
    dry_run: bool = False,
    as_json: bool = False,
    env: Mapping[str, str] | None = None,
    core: CoreFactory | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run `emb cleanup`.

    Args:
        target: The branch the worker branches were merged into.
        branch: Clean up only this branch.
        directory: A folder in the git checkout.
        dry_run: Report what would be done and change nothing.
        as_json: Print a JSON list of results.
        env: The environment; `None` reads `os.environ`.
        core: Returns the run core that closes sessions.
        out: Standard output; `None` for `sys.stdout`.
        err: Standard error; `None` for `sys.stderr`.

    Returns:
        The exit code.
    """
    env = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        repo = open_repository(directory, env, "cleanup")
    except BranchError as error:
        return _print_failure("cleanup", error, err)
    again = _again(target, branch, repo.top)
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
        dry_run=dry_run,
        core=core,
        out=None if as_json else out,
        err=err,
    )
    failure: GitFailedError | None = None
    try:
        job.run([branch] if branch else job.worker_branches())
    except GitFailedError as error:
        failure = error
    if as_json:
        print(json.dumps([item.to_json() for item in job.results], indent=2), file=out)
    elif not job.results and failure is None:
        print(f"emb cleanup: no Ember run branch against {target}", file=err)
    if failure is not None:
        return _print_failure("cleanup", failure, err)
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
                "--verdict keep",
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
            f"{target} --verdict keep",
        )
    return title


def _merge(
    repo: Repository, branch: str, target: str, worktree: Path
) -> tuple[str | None, str]:
    """Merge `branch` into `target` in `worktree` unless it is contained.

    Returns:
        The merge commit, or `None` when the branch was contained, and the
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
            f"changes, then emb merge {branch} --into {target} --verdict keep",
        )
    merged = repo.git(
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        _merge_title(repo, branch, target, branch_tip, worktree),
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
            f"next: git -C {worktree} merge --no-ff --no-edit {branch}, resolve "
            f"the conflicts and commit, then {_again(target, branch, repo.top)}",
        )
    raise GitFailedError(
        exits.WORKTREE_FAILED,
        f"git merge failed, {target} at {repo.tip(target)}: {_git_message(merged)}",
        f"next: git -C {worktree} status, then emb merge {branch} --into {target} "
        "--verdict keep",
    )


def merge(
    branch: str,
    target: str,
    verdict: str | None,
    *,
    directory: str | Path = ".",
    as_json: bool = False,
    env: Mapping[str, str] | None = None,
    core: CoreFactory | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run `emb merge`: merge a reviewed branch, then clean it up.

    Args:
        branch: The branch to merge.
        target: The branch to merge into.
        verdict: The lead's verdict; only `keep` merges.
        directory: A folder in the git checkout.
        as_json: Print one JSON object.
        env: The environment; `None` reads `os.environ`.
        core: Returns the run core that closes sessions.
        out: Standard output; `None` for `sys.stdout`.
        err: Standard error; `None` for `sys.stderr`.

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
                "--verdict keep",
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
        repo = open_repository(directory, env, "merge")
        for name in (branch, target):
            if not repo.branch_exists(name):
                raise BranchError(
                    exits.NOT_A_CHECKOUT,
                    f"no local branch {name}",
                    f"next: git -C {repo.top} branch --list",
                )
        worktree = _target_worktree(repo, target)
        commit, line = _merge(repo, branch, target, worktree)
    except BranchError as error:
        return _print_failure("merge", error, err)
    contained_before = commit is None
    if not as_json:
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
    if as_json:
        document = {
            "branch": branch,
            "target": target,
            "merge_commit": commit,
            "contained_before": contained_before,
            "cleanup": [item.to_json() for item in job.results],
        }
        print(json.dumps(document, indent=2), file=out)
    if failure is not None:
        return _print_failure("merge", failure, err)
    return exits.OK
