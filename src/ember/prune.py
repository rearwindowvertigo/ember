"""Prune ended records, orphaned worktrees and temporary folders, keeping branches."""

import contextlib
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ember import paths, prepare
from ember.flows import lock as flow_lock
from ember.flows import record as flows
from ember.panels import record as panels
from ember.runs import record as runs
from ember.runs import worktrees
from ember.runs.core import abandon_start, worktree_removal_reason

_RECORD_ERRORS = (
    runs.RecordError,
    flows.FlowError,
    panels.PanelError,
    paths.RuntimeDirError,
    OSError,
    ValueError,
)
_LOCK_ERRORS = (flow_lock.LockError, panels.LockError, paths.RuntimeDirError)
_BUSY = (BlockingIOError, flow_lock.Busy, panels.Busy)


@dataclass
class Entry:
    """A loaded record and the run ids it names."""

    id: str
    kind: str
    data: dict[str, Any]
    children: set[str] = field(default_factory=set)
    abandoned: bool = False


@dataclass
class Selection:
    """Selected records, ownership, skips and read failures."""

    records: dict[str, Entry] = field(default_factory=dict)
    owners: dict[str, set[str]] = field(default_factory=dict)
    flow_bases: dict[str, str | None] = field(default_factory=dict)
    live_worktrees: set[str] = field(default_factory=set)
    named_worktrees: set[str] = field(default_factory=set)
    named_tmp_ids: set[str] = field(default_factory=set)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class Result:
    """Removal results and diagnostics, including retained worktrees."""

    removed: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    branches: list[dict[str, str]] = field(default_factory=list)
    worktrees: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, list[dict[str, str]]]:
        """Return pruning results, including abandoned starts when found."""
        result = dict(
            removed=self.removed,
            skipped=self.skipped,
            branches=self.branches,
            worktrees=self.worktrees,
        )
        if self.failed:
            result["failed"] = self.failed
        return result


def _run_ids(value: object) -> set[str]:
    """Collect run and role_run fields throughout a record or event."""
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if (key == "run" or key.endswith("_run")) and isinstance(item, str):
                result.add(item)
            else:
                result.update(_run_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_run_ids(item))
    return result


def _worktree_paths(value: object) -> set[str]:
    """Collect every worktree path named by a record, including flow tasks."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "worktree" and isinstance(item, str) and item:
                found.add(str(Path(item).resolve()))
            else:
                found.update(_worktree_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_worktree_paths(item))
    return found


def _load(ident: str, env: Mapping[str, str]) -> Entry:
    """Read a record through its record module and gather its run references."""
    folder = runs.run_dir(ident, env)
    if folder.is_symlink():
        raise OSError(f"{ident}: record folder is a symlink")
    if flows.is_flow_id(ident):
        data = flows.load(ident, env)
        children = _run_ids(data) | _run_ids(flows.read_events(ident, env))
        return Entry(ident, "flow", data, children)
    if panels.is_panel_id(ident):
        data = panels.load(ident, env)
        return Entry(ident, "panel", data, _run_ids(data))
    return Entry(ident, "run", runs.load(ident, env).to_json())


def _folder_run_ids(ident: str, env: Mapping[str, str], errors: list[str]) -> set[str]:
    """Recover run ids from a broken parent's files without following symlinks."""
    folder = runs.run_dir(ident, env)
    if folder.is_symlink():
        return set()
    found: set[str] = set()
    pattern = re.compile(
        rb"(?<![a-z0-9-])([a-z0-9][a-z0-9-]*-\d{8}-\d{6}-[0-9a-f]{6})(?![a-z0-9-])"
    )

    def report(error: OSError) -> None:
        errors.append(f"{ident}: cannot read parent files: {error}")

    for directory, _, names in os.walk(folder, followlinks=False, onerror=report):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                found.update(
                    match.decode("ascii")
                    for match in pattern.findall(path.read_bytes())
                )
            except OSError as error:
                report(error)
    return {item for item in found if runs.is_run_folder(item)}


def _ended(
    entry: Entry,
    env: Mapping[str, str],
    overrides: Mapping[str, Entry] | None = None,
) -> str | None:
    """Return a refusal reason, or None when the record has ended."""
    statuses = {
        "run": (runs.CLOSED, runs.FAILED),
        "flow": flows.ENDED,
        "panel": ("done", "incomplete"),
    }
    if entry.data["status"] not in statuses[entry.kind]:
        return "not ended"
    if entry.kind == "run":
        return worktree_removal_reason(
            runs.from_json(entry.data, runs.run_dir(entry.id, env), env)
        )
    if entry.kind in ("flow", "panel"):
        role = "task" if entry.kind == "flow" else "member"
        for ident in sorted(entry.children):
            try:
                if overrides is not None and ident in overrides:
                    member = runs.from_json(
                        overrides[ident].data, runs.run_dir(ident, env), env
                    )
                else:
                    member = runs.load(ident, env)
            except runs.RecordMissingError:
                continue
            except _RECORD_ERRORS as error:
                raise ValueError(
                    f"{entry.id}: {role} run {ident} is unreadable: {error}"
                ) from error
            reason = worktree_removal_reason(member)
            if reason:
                if reason == "not ended":
                    return f"{role} run {ident} is still open ({member.status})"
                return f"{role} run {ident}: {reason}"
    return None


def _old(entry: Entry, days: int, now: float) -> bool:
    """Compare the end time, or last update, with an inclusive age boundary."""
    ended = entry.data["ended_at"]
    at = entry.data["updated_at"] if ended is None else ended
    if not math.isfinite(at):
        raise ValueError(f"{entry.id}: invalid record time")
    return days == 0 or now - at >= days * 86400


def select(
    older_than: int,
    record_ids: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Selection:
    """Select ended or abandoned records without changing records or processes.

    Explicit ids ignore age. A selected parent includes its ended runs of
    any age. A run named by parents requires every parent to be selected.
    Unlocked starting runs are considered failed for selection. Missing,
    unreadable and other unfinished explicit ids are returned as skips.
    """
    if type(older_than) is not int or older_than < 0:
        raise ValueError("older_than must be a non-negative whole number")
    env = os.environ if env is None else env
    at = time.time() if now is None else now
    result = Selection()
    root = paths.runs_dir(env)
    try:
        names = sorted(
            item.name
            for item in root.iterdir()
            if item.is_symlink()
            or (
                item.is_dir()
                and (
                    re.fullmatch(r"[a-z0-9-]+-\d{8}-\d{6}-[0-9a-f]{6}", item.name)
                    or any(
                        (item / name).exists()
                        for name in (
                            runs.RECORD_NAME,
                            flows.FLOW_NAME,
                            panels.PANEL_NAME,
                        )
                    )
                )
            )
        )
    except FileNotFoundError:
        names = []
    except OSError as error:
        result.errors.append(str(error))
        return result
    loaded: dict[str, Entry] = {}
    requested = set(record_ids) if record_ids is not None else None
    for ident in sorted(set(names) | (requested or set())):
        try:
            runs.check_id(ident)
        except runs.RecordError as error:
            if requested is not None and ident in requested:
                result.skipped.append(dict(id=ident, reason="not a valid record id"))
            else:
                result.errors.append(str(error))
            continue
        try:
            entry = _load(ident, env)
            if entry.kind == "run" and entry.data["status"] == runs.STARTING:
                # A live start holds this lock before writing its first record.
                # The preview changes no record and ends no preparation group.
                try:
                    with contextlib.ExitStack() as locks:
                        _hold(entry, locks, env)
                        entry = _load(ident, env)
                        if entry.data["status"] == runs.STARTING:
                            candidate = abandon_start(
                                runs.from_json(
                                    entry.data, runs.run_dir(ident, env), env
                                ),
                                dry_run=True,
                            )
                            entry = Entry(
                                ident, "run", candidate.to_json(), abandoned=True
                            )
                except _BUSY:
                    pass
            loaded[ident] = entry
            result.named_tmp_ids.update(entry.children)
            if entry.kind == "run":
                result.named_tmp_ids.add(ident)
            result.named_worktrees.update(_worktree_paths(entry.data))
            if entry.kind == "flow":
                result.flow_bases[entry.data["branch"]] = entry.data.get("base_branch")
            if entry.kind == "run" and _ended(entry, env) is not None:
                result.live_worktrees.update(_worktree_paths(entry.data))
            for child in entry.children:
                result.owners.setdefault(child, set()).add(ident)
        except _RECORD_ERRORS as error:
            result.errors.append(str(error))
            result.skipped.append(dict(id=ident, reason=str(error)))
            if flows.is_flow_id(ident) or panels.is_panel_id(ident):
                for child in _folder_run_ids(ident, env, result.errors):
                    result.owners.setdefault(child, set()).add(ident)
    for ident, entry in loaded.items():
        if requested is not None and ident not in requested:
            continue
        try:
            reason = None if entry.abandoned else _ended(entry, env, loaded)
            # Select abandoned starts using their last recorded activity.
            age_entry = _load(ident, env) if entry.abandoned else entry
            if (
                reason is None
                and requested is None
                and not _old(age_entry, older_than, at)
            ):
                reason = "younger than the requested age"
            if reason is None:
                result.records[ident] = entry
            elif requested is not None or reason != "not ended":
                result.skipped.append(dict(id=ident, reason=reason))
        except _RECORD_ERRORS as error:
            result.errors.append(str(error))
            result.skipped.append(dict(id=ident, reason=str(error)))
    for entry in list(result.records.values()):
        for child in sorted(entry.children):
            if child not in loaded:
                if not any(item["id"] == child for item in result.skipped):
                    result.skipped.append(dict(id=child, reason="missing run record"))
                continue
            child_entry = loaded[child]
            if child_entry.kind != "run":
                continue
            reason = None if child_entry.abandoned else _ended(child_entry, env, loaded)
            if reason is None:
                result.records[child] = child_entry
            else:
                result.skipped.append(dict(id=child, reason=reason))
    for ident, entry in list(result.records.items()):
        if entry.kind != "run":
            continue
        parents = result.owners.get(ident, set())
        reason = None
        if not parents <= result.records.keys():
            reason = "parent not selected: " + ", ".join(
                sorted(parents - result.records.keys())
            )
        if reason:
            del result.records[ident]
            result.skipped.append(dict(id=ident, reason=reason))
    selected = result.records.keys()
    result.skipped = [item for item in result.skipped if item["id"] not in selected]
    return result


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an existing record's lock without waiting."""
    fd = paths.open_private_file(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _hold(entry: Entry, stack: contextlib.ExitStack, env: Mapping[str, str]) -> None:
    """Acquire the record's locks, keeping them until the stack closes."""
    folder = runs.run_dir(entry.id, env)
    if entry.kind == "flow":
        stack.enter_context(_file_lock(folder / "lifecycle.lock"))
        stack.enter_context(flow_lock.hold(entry.id, env))
    elif entry.kind == "panel":
        stack.enter_context(panels.hold(entry.id, env))
    else:
        root = paths.runtime_dir(env)
        locks = paths.ensure_private_dir(root / "locks", root=root)
        stack.enter_context(_file_lock(locks / f"{entry.id}.lock"))
        stack.enter_context(_file_lock(folder / runs.LOCK_NAME))


def _git(
    directory: str, *args: str, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a read-only repository query."""
    return subprocess.run(
        ["git", "-C", directory, *args],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _branch_exists(directory: str, branch: str, env: Mapping[str, str]) -> bool:
    """Check a local ref, distinguishing a missing branch from a query failure."""
    result = _git(
        directory, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", env=env
    )
    if result.returncode not in (0, 1):
        raise OSError(result.stderr.strip() or f"cannot inspect branch {branch}")
    return result.returncode == 0


def _target_branch(
    directory: str,
    target: str | None,
    fallbacks: Mapping[str, str | None],
    env: Mapping[str, str],
) -> str | None:
    """Resolve a surviving target branch, following deleted flows to their bases."""
    seen: set[str] = set()
    while target and target not in seen:
        seen.add(target)
        if _branch_exists(directory, target, env):
            return target
        target = fallbacks.get(target)
    return None


def _main_of(worktree: str, env: Mapping[str, str]) -> str | None:
    """Return the main checkout of a worktree's repository, as git lists it."""
    try:
        common = _git(
            worktree, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env
        )
        if common.returncode or not common.stdout.strip():
            return None
        with paths.worktree_lock(Path(common.stdout.strip()), env):
            result = _git(worktree, "worktree", "list", "--porcelain", "-z", env=env)
    except OSError, subprocess.SubprocessError, paths.RuntimeDirError:
        return None
    first = result.stdout.split("\0", 1)[0] if result.returncode == 0 else ""
    if not first.startswith("worktree "):
        return None
    main = first.removeprefix("worktree ")
    return main if Path(main).is_dir() else None


def _checkout(
    data: Mapping[str, Any],
    kind: str,
    env: Mapping[str, str],
    parents: Sequence[str] = (),
) -> str:
    """Return the first folder of the record's repository that still exists.

    New run and flow records use their main checkout, or their common git folder
    when that checkout is gone. Older records try their source checkout,
    their parents and the main checkout listed from their worktree.

    Args:
        data: The record.
        kind: `run`, `flow` or `panel`.
        env: The environment of the git commands.
        parents: The checkouts the record's parent records name.

    Returns:
        A folder in the record's repository, or the recorded checkout.
    """
    if data.get("common") is not None:
        main = data.get("main")
        return main if main and Path(main).is_dir() else data["common"]
    candidates = [data.get("repo") if kind == "run" else data.get("dir")]
    candidates += [data.get("main"), *parents]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return candidate
    worktree = data.get("worktree")
    if isinstance(worktree, str) and worktree and Path(worktree).is_dir():
        main = _main_of(worktree, env)
        if main is not None:
            return main
    return str(candidates[0])


def _parent_checkouts(
    ident: str, owners: Mapping[str, set[str]], records: Mapping[str, Entry]
) -> list[str]:
    """Return the checkouts a record's parent records name, `main` first."""
    found: list[str] = []
    for parent in sorted(owners.get(ident, set())):
        entry = records.get(parent)
        if entry is None:
            continue
        for key in ("main", "dir"):
            value = entry.data.get(key)
            if isinstance(value, str) and value and value not in found:
                found.append(value)
    return found


def _orphan_reason(worktree: Path, root: Path) -> str | None:
    """Return why a folder cannot be removed as an orphan, or None if it can."""
    resolved = worktree.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return "outside the Ember worktrees folder"
    if worktree.is_symlink():
        return "worktree folder is a symlink"
    pointer = worktree / ".git"
    if pointer.is_symlink() or not pointer.is_file():
        return "no regular .git file"
    # Git removes trailing CR and LF bytes, but preserves path whitespace.
    text = pointer.read_bytes().rstrip(b"\r\n")
    if (
        not text.startswith(b"gitdir: ")
        or not (name := text.removeprefix(b"gitdir: "))
        or any(byte in name for byte in (b"\0", b"\r", b"\n"))
    ):
        return "invalid .git file"
    git_folder = Path(os.fsdecode(name))
    if not git_folder.is_absolute():
        git_folder = worktree / git_folder
    try:
        git_folder.stat()
    except FileNotFoundError:
        return None
    return f"git folder still exists: {git_folder}"


def _remove_orphan(worktree: Path, result: Result, *, dry_run: bool) -> None:
    """Remove a verified orphan and report the same candidate for a dry run."""
    item = dict(id=str(worktree), kind="worktree")
    if item in result.removed:
        return
    if not dry_run:
        shutil.rmtree(worktree)
    result.removed.append(item)


def _sweep_orphans(
    older_than: int,
    at: float,
    env: Mapping[str, str],
    named: set[str],
    result: Result,
    *,
    dry_run: bool,
    readable: bool,
) -> None:
    """Inspect unrecorded folders only when every record could be read."""
    root = paths.worktrees_dir(env).resolve()
    try:
        folders = sorted(root.iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        result.errors.append(f"{root}: {error}")
        return
    for folder in folders:
        try:
            if str(folder.resolve()) in named:
                continue
            if not (folder.is_dir() or folder.is_symlink()):
                continue
            reason = (
                _orphan_reason(folder, root)
                if readable
                else "cannot establish ownership: unreadable records"
            )
            if (
                reason is None
                and older_than
                and at - folder.stat().st_mtime < (older_than * 86400)
            ):
                reason = "younger than the requested age"
            if reason is None:
                _remove_orphan(folder, result, dry_run=dry_run)
            else:
                result.skipped.append(dict(id=str(folder), reason=reason))
        except (OSError, ValueError) as error:
            result.errors.append(f"{folder}: {error}")
            result.skipped.append(dict(id=str(folder), reason=str(error)))


def _sweep_tmp(
    older_than: int,
    at: float,
    env: Mapping[str, str],
    named: set[str],
    result: Result,
    *,
    dry_run: bool,
    readable: bool,
) -> None:
    """Remove unrecorded temporary entries only when every record was readable."""
    short_names = {f"ember-{ident.rsplit('-', 1)[-1]}" for ident in named}
    for root, reserved, short in (
        (paths.RUN_TMP_ROOT, short_names, True),
        (paths.cache_dir(env) / "tmp", named, False),
    ):
        _sweep_tmp_root(
            root,
            reserved,
            older_than,
            at,
            result,
            dry_run=dry_run,
            readable=readable,
            short=short,
        )


def _sweep_tmp_root(
    root: Path,
    named: set[str],
    older_than: int,
    at: float,
    result: Result,
    *,
    dry_run: bool,
    readable: bool,
    short: bool,
) -> None:
    """Sweep one temporary base without following its symlinks."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as error:
        result.errors.append(f"{root}: {error}")
        return
    try:
        for name in sorted(os.listdir(fd)):
            if name in named or (short and not paths.RUN_TMP_PATTERN.fullmatch(name)):
                continue
            entry = root / name
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if info.st_uid != os.getuid():
                    continue
                if short and not stat.S_ISDIR(info.st_mode):
                    continue
                if not readable:
                    result.skipped.append(
                        dict(
                            id=str(entry),
                            reason="cannot establish ownership: unreadable records",
                        )
                    )
                    continue
                if older_than and at - info.st_mtime < older_than * 86400:
                    result.skipped.append(
                        dict(id=str(entry), reason="younger than the requested age")
                    )
                    continue
                if not dry_run:
                    if stat.S_ISDIR(info.st_mode):
                        shutil.rmtree(name, dir_fd=fd)
                    else:
                        os.unlink(name, dir_fd=fd)
                result.removed.append(dict(id=str(entry), kind="tmp"))
            except OSError as error:
                result.errors.append(f"{entry}: {error}")
                result.skipped.append(dict(id=str(entry), reason=str(error)))
    except OSError as error:
        result.errors.append(f"{root}: {error}")
    finally:
        os.close(fd)


def _artifacts(
    entry: Entry,
    env: Mapping[str, str],
    result: Result,
    fallbacks: Mapping[str, str | None],
    live_worktrees: set[str],
    parents: Sequence[str] = (),
    *,
    dry_run: bool = True,
) -> None:
    """Remove orphaned worktrees and list commands for surviving repositories.

    Each worktree is the path its record names, wherever it is.
    """
    data = entry.data
    preparation = data.get("preparation") or {}
    if (
        not dry_run
        and preparation.get("pid") is not None
        and preparation.get("start") is not None
    ):
        prepare.end_group(preparation["pid"], preparation["start"])
    if entry.kind == "panel":
        return
    directory = _checkout(data, entry.kind, env, parents)
    if entry.kind == "run" and data.get("detached") and data.get("read_only"):
        worktree = data["worktree"]
        if str(Path(worktree).resolve()) in live_worktrees:
            raise OSError(f"detached worktree {worktree} is in use")
        if data.get("common") and Path(data["common"]).exists():
            run = runs.from_json(data, runs.run_dir(entry.id, env), env)
            with paths.worktree_lock(Path(run.common or run.repo), env):
                if worktrees.remove_detached(run, dry_run=dry_run):
                    result.removed.append(dict(id=worktree, kind="worktree"))
        elif Path(worktree).exists():
            reason = _orphan_reason(Path(worktree), paths.worktrees_dir(env).resolve())
            if reason:
                raise OSError(f"{worktree}: {reason}")
            _remove_orphan(Path(worktree), result, dry_run=dry_run)
        return
    candidates = [(data.get("branch"), data.get("worktree"), data.get("base_branch"))]
    if entry.kind == "flow":
        candidates += [
            (task["branch"], task["worktree"], data["branch"]) for task in data["tasks"]
        ]
    for branch, worktree, target in candidates:
        if not branch:
            continue
        worktree_command = None
        if (
            worktree
            and str(Path(worktree).resolve()) not in live_worktrees
            and Path(worktree).exists()
        ):
            reason = _orphan_reason(Path(worktree), paths.worktrees_dir(env).resolve())
            if reason is None:
                _remove_orphan(Path(worktree), result, dry_run=dry_run)
                if not Path(directory).is_dir():
                    continue
            elif data.get("common") and not Path(data["common"]).exists():
                result.skipped.append(dict(id=worktree, reason=reason))
            else:
                worktree_command = shlex.join(
                    ["git", "-C", directory, "worktree", "remove", worktree]
                )
                item = dict(branch=branch, worktree=worktree, command=worktree_command)
                if item not in result.worktrees:
                    result.worktrees.append(item)
        if data.get("common") and not Path(data["common"]).exists():
            continue
        contained = False
        try:
            if not _branch_exists(directory, branch, env):
                continue
            target = (
                _target_branch(directory, target, fallbacks, env)
                if data.get("base_branch")
                else None
            )
            while target:
                merged = _git(
                    directory,
                    "merge-base",
                    "--is-ancestor",
                    f"refs/heads/{branch}",
                    f"refs/heads/{target}",
                    env=env,
                )
                if merged.returncode in (0, 1):
                    contained = merged.returncode == 0
                    break
                replacement = _target_branch(directory, target, fallbacks, env)
                if replacement == target:
                    raise OSError(
                        merged.stderr.strip() or "cannot check branch ancestry"
                    )
                target = replacement
        except (OSError, subprocess.SubprocessError) as error:
            result.errors.append(f"{entry.id}: {branch}: {error}")
            continue
        if contained:
            continue
        command = shlex.join(["git", "-C", directory, "branch", "-D", branch])
        if worktree_command:
            command = f"{worktree_command} && {command}"
        item = dict(branch=branch, command=command)
        if item not in result.branches:
            result.branches.append(item)


def remove(
    older_than: int,
    record_ids: Sequence[str] | None = None,
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Result:
    """Prune selected records, reloading under nonblocking locks before removal.

    Unlocked starting runs are failed and their preparation groups ended.
    Dry runs list the same candidates without changing records or processes.
    Failures are collected while remaining records are processed.
    """
    env = os.environ if env is None else env
    at = time.time() if now is None else now
    selection = select(older_than, record_ids, env=env, now=at)
    result = Result(errors=list(selection.errors))
    with contextlib.ExitStack() as locks:
        unavailable: set[str] = set()
        # Hold parents throughout child selection, rechecks and removal.
        for entry in selection.records.values():
            if entry.kind == "run":
                continue
            try:
                _hold(entry, locks, env)
            except _BUSY:
                unavailable.add(entry.id)
                result.skipped.append(dict(id=entry.id, reason="busy"))
            except (*_RECORD_ERRORS, *_LOCK_ERRORS) as error:
                unavailable.add(entry.id)
                result.errors.append(str(error))
                result.skipped.append(dict(id=entry.id, reason=str(error)))
        current = select(older_than, record_ids, env=env, now=at)
        result.errors.extend(
            error for error in current.errors if error not in result.errors
        )
        result.skipped.extend(current.skipped)
        for ident in selection.records.keys() - current.records.keys() - unavailable:
            if not any(item["id"] == ident for item in result.skipped):
                result.skipped.append(dict(id=ident, reason="no longer qualifies"))
        # Newly qualifying parents wait for a later invocation.
        for ident, entry in current.records.items():
            if entry.kind != "run" and ident not in selection.records:
                unavailable.add(ident)
                result.skipped.append(dict(id=ident, reason="selection changed"))
        child_ids = {
            child for entry in current.records.values() for child in entry.children
        }
        lock_ids = child_ids | {
            ident for ident, entry in current.records.items() if entry.kind == "run"
        }
        for ident in sorted(lock_ids):
            try:
                entry = _load(ident, env)
                _hold(entry, locks, env)
                entry = _load(ident, env)
                if entry.kind == "run" and entry.data["status"] == runs.STARTING:
                    failed = abandon_start(
                        runs.from_json(entry.data, runs.run_dir(ident, env), env),
                        dry_run=dry_run,
                    )
                    entry = Entry(ident, "run", failed.to_json(), abandoned=True)
                    result.failed.append(
                        dict(id=ident, reason=failed.last_reason or "")
                    )
                if entry.kind == "run":
                    current.records[ident] = entry
                if entry.kind == "run" and _ended(entry, env) is not None:
                    current.live_worktrees.update(_worktree_paths(entry.data))
            except runs.RecordMissingError:
                continue
            except _BUSY:
                unavailable.add(ident)
                if entry.kind == "run":
                    current.live_worktrees.update(_worktree_paths(entry.data))
                result.skipped.append(dict(id=ident, reason="busy"))
            except (*_RECORD_ERRORS, *_LOCK_ERRORS) as error:
                unavailable.add(ident)
                prior = current.records.get(ident)
                if prior is not None and prior.kind == "run":
                    current.live_worktrees.update(_worktree_paths(prior.data))
                result.errors.append(str(error))
                result.skipped.append(dict(id=ident, reason=str(error)))
        removed: set[str] = set()
        ordered = sorted(
            current.records.values(), key=lambda item: (item.kind == "run", item.id)
        )
        for entry in ordered:
            ident = entry.id
            if ident in unavailable:
                continue
            try:
                abandoned = entry.abandoned
                if not (dry_run and abandoned):
                    entry = _load(ident, env)
                reason = _ended(entry, env, current.records)
                if entry.kind != "run" and entry.children & unavailable:
                    role = "task" if entry.kind == "flow" else "member"
                    child = min(entry.children & unavailable)
                    reason = f"{role} run {child} is busy or unreadable"
                parents = current.owners.get(ident, set())
                if entry.kind == "run" and not parents <= removed:
                    reason = "parent was not removed"
                if (
                    reason is None
                    and not parents
                    and record_ids is None
                    and not abandoned
                    and not _old(entry, older_than, at)
                ):
                    reason = "younger than the requested age"
                if reason:
                    result.skipped.append(dict(id=ident, reason=reason))
                    continue
                temporary = (
                    paths.run_tmp_dir(ident, env) if entry.kind == "run" else None
                )
                had_tmp = temporary is not None and os.path.lexists(temporary)
                artifacts = Result()
                _artifacts(
                    entry,
                    env,
                    artifacts,
                    current.flow_bases,
                    current.live_worktrees,
                    _parent_checkouts(ident, current.owners, current.records),
                    dry_run=dry_run,
                )
                if temporary is not None:
                    if not dry_run:
                        paths.remove_run_tmp_dir(ident, env)
                    if had_tmp:
                        artifacts.removed.append(dict(id=str(temporary), kind="tmp"))
                if not dry_run:
                    shutil.rmtree(runs.run_dir(ident, env))
                result.removed.append(dict(id=ident, kind=entry.kind))
                removed.add(ident)
                result.errors.extend(artifacts.errors)
                result.removed.extend(
                    item for item in artifacts.removed if item not in result.removed
                )
                result.skipped.extend(artifacts.skipped)
                for item in artifacts.branches:
                    if not any(
                        old["branch"] == item["branch"] for old in result.branches
                    ):
                        result.branches.append(item)
                for item in artifacts.worktrees:
                    if not any(
                        old["branch"] == item["branch"]
                        and old["worktree"] == item["worktree"]
                        for old in result.worktrees
                    ):
                        result.worktrees.append(item)
            except _RECORD_ERRORS as error:
                result.errors.append(f"{ident}: {error}")
                result.skipped.append(dict(id=ident, reason=str(error)))
    if record_ids is None:
        readable = not result.errors
        _sweep_orphans(
            older_than,
            at,
            env,
            selection.named_worktrees | current.named_worktrees,
            result,
            dry_run=dry_run,
            readable=readable,
        )
        _sweep_tmp(
            older_than,
            at,
            env,
            selection.named_tmp_ids | current.named_tmp_ids,
            result,
            dry_run=dry_run,
            readable=readable,
        )
    result.skipped = list({item["id"]: item for item in result.skipped}.values())
    return result


def main(
    older_than: int,
    *,
    dry_run: bool = False,
    as_json: bool = False,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Print pruning results and return 1 when any operation failed."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    result = remove(older_than, dry_run=dry_run, env=env)
    if as_json:
        print(json.dumps(result.to_json(), indent=2), file=out)
    else:
        verb = "would remove" if dry_run else "removed"
        for item in result.failed:
            fail_verb = "would fail" if dry_run else "failed"
            print(f"{fail_verb} {item['id']}: {item['reason']}", file=out)
        for item in result.removed:
            print(f"{verb} {item['kind']} {item['id']}", file=out)
        for item in result.skipped:
            print(f"skipped {item['id']}: {item['reason']}", file=out)
        printed: set[str] = set()
        for item in result.branches:
            print(f"branch {item['branch']}: {item['command']}", file=out)
            printed.add(item["branch"])
            for worktree in result.worktrees:
                if worktree["branch"] == item["branch"]:
                    print(
                        f"  worktree {worktree['worktree']}: {worktree['command']}",
                        file=out,
                    )
        for item in result.worktrees:
            if item["branch"] not in printed:
                print(f"branch {item['branch']}", file=out)
                print(f"  worktree {item['worktree']}: {item['command']}", file=out)
    for error in result.errors:
        print(f"emb prune: {error}", file=err)
    return 1 if result.errors else 0
