"""Select and remove ended records while retaining their branches and worktrees."""

import contextlib
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ember import paths
from ember.flows import lock as flow_lock
from ember.flows import plan
from ember.flows import record as flows
from ember.panels import record as panels
from ember.runs import record as runs

_RECORD_ERRORS = (
    runs.RecordError,
    flows.FlowError,
    panels.PanelError,
    OSError,
    ValueError,
)
_LOCK_ERRORS = (flow_lock.LockError, panels.LockError)
_BUSY = (BlockingIOError, flow_lock.Busy, panels.Busy)


@dataclass
class Entry:
    """A loaded record and the run ids it names."""

    id: str
    kind: str
    data: dict[str, Any]
    children: set[str] = field(default_factory=set)


@dataclass
class Selection:
    """Selected records, ownership, skips and read failures."""

    records: dict[str, Entry] = field(default_factory=dict)
    owners: dict[str, set[str]] = field(default_factory=dict)
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

    def to_json(self) -> dict[str, list[dict[str, str]]]:
        """Return the command's three JSON fields."""
        return dict(removed=self.removed, skipped=self.skipped, branches=self.branches)


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


def _ended(entry: Entry, env: Mapping[str, str]) -> str | None:
    """Return a refusal reason, or None when the record has ended."""
    statuses = {
        "run": (runs.CLOSED, runs.FAILED),
        "flow": flows.ENDED,
        "panel": ("done", "incomplete"),
    }
    if entry.data["status"] not in statuses[entry.kind]:
        return "not ended"
    if entry.kind == "panel":
        for ident in sorted(entry.children):
            try:
                member = runs.load(ident, env)
            except runs.RecordMissingError:
                continue
            if member.status not in (runs.CLOSED, runs.FAILED):
                return f"member run {ident} is still open"
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
    """Select ended records by age or explicit ids, without changing files.

    Explicit ids ignore age. A selected parent includes its ended runs of
    any age. A run named by parents requires every parent to be selected.
    Missing, unreadable and unfinished explicit ids are returned as skips.
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
            if item.is_dir()
            and re.fullmatch(r"[a-z0-9-]+-\d{8}-\d{6}-[0-9a-f]{6}", item.name)
        )
    except FileNotFoundError:
        names = []
    except OSError as error:
        result.errors.append(str(error))
        return result
    loaded: dict[str, Entry] = {}
    broken_parents = False
    requested = set(record_ids) if record_ids is not None else None
    for ident in sorted(set(names) | (requested or set())):
        try:
            runs.check_id(ident)
        except runs.RecordError:
            if requested is not None and ident in requested:
                result.skipped.append(dict(id=ident, reason="not a valid record id"))
            continue
        try:
            entry = _load(ident, env)
            loaded[ident] = entry
            for child in entry.children:
                result.owners.setdefault(child, set()).add(ident)
        except _RECORD_ERRORS as error:
            result.errors.append(str(error))
            result.skipped.append(dict(id=ident, reason=str(error)))
            broken_parents |= flows.is_flow_id(ident) or panels.is_panel_id(ident)
    for ident, entry in loaded.items():
        if requested is not None and ident not in requested:
            continue
        try:
            reason = _ended(entry, env)
            if reason is None and requested is None and not _old(entry, older_than, at):
                reason = "younger than the requested age"
            if reason is None:
                result.records[ident] = entry
            elif requested is not None:
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
            reason = _ended(child_entry, env)
            if reason is None:
                result.records[child] = child_entry
            else:
                result.skipped.append(dict(id=child, reason=reason))
    for ident, entry in list(result.records.items()):
        if entry.kind != "run":
            continue
        parents = result.owners.get(ident, set())
        reason = None
        if broken_parents:
            reason = "cannot check ownership while a parent record does not load"
        elif not parents <= result.records.keys():
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


def _artifacts(entry: Entry, env: Mapping[str, str], result: Result) -> None:
    """List unmerged branches and existing worktrees without changing them."""
    data = entry.data
    if entry.kind == "panel":
        return
    directory = data["repo"] if entry.kind == "run" else data["dir"]
    base = data.get("base_branch") or data.get("base")
    if entry.kind == "flow" and not data.get("base_branch"):
        try:
            planned = plan.load(flows.folder(entry.id, env) / flows.PLAN_NAME).base
            if planned != "HEAD":
                base = planned
        except (plan.PlanError, OSError, ValueError) as error:
            result.errors.append(f"{entry.id}: cannot read branch base: {error}")
    candidates = [(data.get("branch"), data.get("worktree"), base)]
    if entry.kind == "flow":
        candidates += [
            (task["branch"], task["worktree"], data["branch"]) for task in data["tasks"]
        ]
    for branch, worktree, target in candidates:
        if not branch:
            continue
        worktree_command = None
        if worktree and Path(worktree).exists():
            worktree_command = shlex.join(
                ["git", "-C", directory, "worktree", "remove", worktree]
            )
            item = dict(branch=branch, worktree=worktree, command=worktree_command)
            if item not in result.worktrees:
                result.worktrees.append(item)
        try:
            exists = _git(
                directory,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                env=env,
            )
            if exists.returncode == 1:
                if worktree_command:
                    result.branches.append(
                        dict(branch=branch, command=worktree_command)
                    )
                continue
            if exists.returncode:
                raise OSError(exists.stderr.strip() or "cannot inspect branch")
            merged = _git(
                directory,
                "merge-base",
                "--is-ancestor",
                f"refs/heads/{branch}",
                target or "HEAD",
                env=env,
            )
            if merged.returncode == 0:
                if worktree_command:
                    result.branches.append(
                        dict(branch=branch, command=worktree_command)
                    )
                continue
            if merged.returncode != 1:
                raise OSError(merged.stderr.strip() or "cannot check branch ancestry")
        except (OSError, subprocess.SubprocessError) as error:
            result.errors.append(f"{entry.id}: {branch}: {error}")
        if re.fullmatch(
            r"ember/(?:codex|opencode|claude)/[a-z0-9][a-z0-9-]{5,63}", branch
        ):
            command = shlex.join(
                [
                    "emb",
                    "cleanup",
                    "--merged-into",
                    target or data.get("base") or "HEAD",
                    "--branch",
                    branch,
                    "--dir",
                    directory,
                ]
            )
        else:
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

    Dry runs return the same removal candidates without deleting files.
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
        for ident, entry in current.records.items():
            if entry.kind != "run":
                continue
            try:
                _hold(entry, locks, env)
            except _BUSY:
                unavailable.add(ident)
                result.skipped.append(dict(id=ident, reason="busy"))
            except (*_RECORD_ERRORS, *_LOCK_ERRORS) as error:
                unavailable.add(ident)
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
                entry = _load(ident, env)
                reason = _ended(entry, env)
                if entry.kind == "panel" and entry.children & unavailable:
                    reason = "member run is busy or unreadable"
                parents = current.owners.get(ident, set())
                if entry.kind == "run" and not parents <= removed:
                    reason = "parent was not removed"
                if (
                    reason is None
                    and not parents
                    and record_ids is None
                    and not _old(entry, older_than, at)
                ):
                    reason = "younger than the requested age"
                if reason:
                    result.skipped.append(dict(id=ident, reason=reason))
                    continue
                artifacts = Result()
                _artifacts(entry, env, artifacts)
                if not dry_run:
                    shutil.rmtree(runs.run_dir(ident, env))
                result.removed.append(dict(id=ident, kind=entry.kind))
                removed.add(ident)
                result.errors.extend(artifacts.errors)
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
