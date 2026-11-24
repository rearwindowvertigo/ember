"""Advance and remove owned detached worktrees without discarding changes."""

import re
import shlex
import subprocess
from pathlib import Path

from ember import paths
from ember.runs.record import Run


def _git(run: Run, directory: str, *args: str) -> str:
    """Run a repository operation, reporting failures as filesystem errors."""
    try:
        result = subprocess.run(
            ["git", "-C", directory, *args],
            env=dict(run.env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=120,
            check=False,
        )
    except subprocess.SubprocessError as error:
        raise OSError(str(error)) from error
    if result.returncode:
        raise OSError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout


def _detached_entry(run: Run) -> list[str] | None:
    """Find the registered, unlocked worktree at the recorded path."""
    folder = Path(run.worktree)
    common = run.common
    if (
        not common
        or folder.name != run.id
        or folder.resolve() != folder
        or folder.resolve() == Path(run.repo).resolve()
        or (run.main and folder.resolve() == Path(run.main).resolve())
    ):
        raise OSError(f"cannot verify ownership of detached worktree {folder}")
    listing = _git(run, common, "worktree", "list", "--porcelain", "-z")
    entries = [entry.split("\0") for entry in listing.split("\0\0") if entry]
    entry = next((item for item in entries if item[0] == f"worktree {folder}"), None)
    if entry is None:
        if not folder.exists():
            return None
        raise OSError(f"{folder} is not a registered worktree of {common}")
    if "detached" not in entry or run.branch is not None:
        raise OSError(f"{folder} is no longer a detached worktree")
    if any(item.startswith("locked") for item in entry):
        raise OSError(f"kept locked worktree {folder}")
    return entry


def _detached_head(run: Run, entry: list[str]) -> str:
    """Check that Git in the worktree and its registration agree on identity."""
    folder = run.worktree
    identity = (
        _git(
            run,
            folder,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
            "--git-dir",
            "HEAD",
        )
        .removesuffix("\n")
        .rsplit("\n", 3)
    )
    if len(identity) != 4:
        raise OSError(f"cannot verify ownership of detached worktree {folder}")
    top, common, admin, actual = identity
    linked = Path(admin)
    gitfile = Path(folder) / ".git"
    if (
        top != folder
        or common != run.common
        or linked.parent != Path(common) / "worktrees"
        or linked.resolve() != linked
        or not gitfile.is_file()
        or gitfile.is_symlink()
        or (linked / "HEAD").read_text(errors="surrogateescape").strip() != actual
        or (
            linked
            / (linked / "gitdir").read_text(errors="surrogateescape").removesuffix("\n")
        ).resolve()
        != gitfile
        or f"HEAD {actual}" not in entry
    ):
        raise OSError(f"cannot verify ownership of detached worktree {folder}")
    if any(
        (linked / name).exists()
        for name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
            "BISECT_START",
            "index.lock",
            "HEAD.lock",
        )
    ):
        raise OSError(f"{folder} has a Git operation in progress")
    return actual


def _status(run: Run, *, ignored: bool = False) -> str:
    """Read tracked, untracked and optionally ignored worktree changes."""
    return _git(
        run,
        run.worktree,
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        *(("--ignored",) if ignored else ()),
    )


def _hidden_index_changes(run: Run) -> bool:
    """Report index flags that can hide changes from Git status."""
    entries = _git(run, run.worktree, "ls-files", "-v", "-z").split("\0")
    return any(item and (item[0] == "S" or item[0].islower()) for item in entries)


def verify_detached(run: Run, head: str) -> None:
    """Require the owned detached checkout at head with no pending changes."""
    entry = _detached_entry(run) if run.read_only and run.detached else None
    if entry is None:
        raise OSError(f"cannot verify ownership of detached worktree {run.worktree}")
    actual = _detached_head(run, entry)
    if actual != head:
        raise OSError(f"{run.worktree} is at {actual}, expected {head}")
    if _status(run) or _hidden_index_changes(run):
        raise OSError(f"{run.worktree} has worktree or index changes")


def advance_detached(run: Run, previous: str, head: str) -> None:
    """Check out the fixed commit while protecting ignored and untracked files."""
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head) is None:
        raise OSError(f"invalid commit for detached worktree {run.worktree}")
    verify_detached(run, previous)
    if previous != head:
        _git(
            run,
            run.worktree,
            "-c",
            "core.hooksPath=/dev/null",
            "checkout",
            "--detach",
            "--no-overwrite-ignore",
            "--no-recurse-submodules",
            head,
        )
        verify_detached(run, head)


def remove_detached(run: Run, *, dry_run: bool = False) -> bool:
    """Remove a clean detached worktree owned by a run.

    Use the expected tip after a review advance and the original base before
    it. Ignored files count as changes when the run's record protects them.

    Args:
        run: The recorded run, including its command environment.
        dry_run: Check the worktree without removing it.

    Returns:
        Whether the worktree exists and is removable. Legacy read-only
        checkouts and editing worktrees return false.

    Raises:
        OSError: Ownership cannot be checked, the worktree has changes or
            git refuses removal. The caller must retain the recorded path.
    """
    if not run.read_only or not run.detached:
        return False
    folder = Path(run.worktree)
    entry = _detached_entry(run)
    if entry is None:
        if not dry_run:
            paths.remove_run_tmp_dir(run.id, run.env)
        return False
    if folder.exists():
        actual = _detached_head(run, entry)
        flow = (run.parent or {}).get("command") == "flow"
        if (
            actual != (run.expected_head or run.base)
            or _status(run, ignored=run.preserve_ignored)
            or (flow and _hidden_index_changes(run))
        ):
            command = shlex.join(["git", "worktree", "remove", "--force", str(folder)])
            raise OSError(f"kept worktree {folder} with changes; to discard: {command}")
    if not dry_run:
        _git(run, run.common or run.repo, "worktree", "remove", "--", str(folder))
        paths.remove_run_tmp_dir(run.id, run.env)
    return True
