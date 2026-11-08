"""Remove owned detached worktrees without discarding worker changes."""

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
            timeout=120,
            check=False,
        )
    except subprocess.SubprocessError as error:
        raise OSError(str(error)) from error
    if result.returncode:
        raise OSError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout


def remove_detached(run: Run, *, dry_run: bool = False) -> bool:
    """Remove a clean detached worktree owned by a run.

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
    common = run.common
    if (
        not common
        or folder.name != run.id
        or folder.resolve() == Path(run.repo).resolve()
        or (run.main and folder.resolve() == Path(run.main).resolve())
    ):
        raise OSError(f"cannot verify ownership of detached worktree {folder}")
    listing = _git(run, common, "worktree", "list", "--porcelain", "-z")
    entries = [entry.split("\0") for entry in listing.split("\0\0") if entry]
    entry = next((item for item in entries if item[0] == f"worktree {folder}"), None)
    if entry is None:
        if not folder.exists():
            if not dry_run:
                paths.remove_run_tmp_dir(run.id, run.env)
            return False
        raise OSError(f"{folder} is not a registered worktree of {common}")
    if "detached" not in entry:
        raise OSError(f"{folder} is no longer a detached worktree")
    if folder.exists():
        changed = _git(
            run, str(folder), "status", "--porcelain", "-z", "--untracked-files=all"
        )
        head = _git(run, str(folder), "rev-parse", "HEAD").strip()
        if changed or head != run.base:
            command = shlex.join(["git", "worktree", "remove", "--force", str(folder)])
            raise OSError(f"kept worktree {folder} with changes; to discard: {command}")
    if any(item.startswith("locked") for item in entry):
        raise OSError(f"kept locked worktree {folder}")
    if not dry_run:
        _git(run, common, "worktree", "remove", "--", str(folder))
        paths.remove_run_tmp_dir(run.id, run.env)
    return True
