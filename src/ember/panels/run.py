"""Run panel members concurrently and collect unfinished turns without resending."""

import contextlib
import copy
import io
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ember import paths
from ember.engines import Driver
from ember.panels import record
from ember.runs import record as runs
from ember.runs import worktrees
from ember.runs.core import (
    Core,
    RunError,
    StartOptions,
    WorkerSelection,
    worktree_removal_reason,
)

START_SPACING = 1.0
POLL_SECONDS = 1.0
CoreFactory = Callable[..., Core]


class NothingToRecover(Exception):  # noqa: N818 - fixed public interface.
    """The panel has no member whose turn can be collected."""


class RetryRefused(Exception):  # noqa: N818 - command diagnostic.
    """The member or its old run cannot be retried."""


class MemberMissingError(Exception):
    """The panel has no member with the requested number."""


@dataclass
class Outcome:
    """The final panel snapshot and all threads created for this invocation."""

    panel: dict[str, Any]
    stopped: bool
    threads: list[threading.Thread]


class _RunIdStream(io.StringIO):
    """Capture the first complete run announcement before the turn finishes."""

    def __init__(self, capture: Callable[[str], None]) -> None:
        """Keep the callback and the incomplete output line."""
        super().__init__()
        self.capture = capture
        self.pending = ""
        self.captured = False

    def write(self, text: str) -> int:
        """Save an announced run id as soon as its newline arrives."""
        count = super().write(text)
        if not self.captured:
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line.startswith("run "):
                    self.capture(runs.check_id(line[4:]))
                    self.captured = True
                    break
        return count


@dataclass
class _Job:
    """One member's private output and progress within the current invocation."""

    member: dict[str, Any]
    out: _RunIdStream
    err: io.StringIO = field(default_factory=io.StringIO)
    out_offset: int = 0
    err_offset: int = 0
    warning_offset: int = 0
    began: float | None = None
    elapsed: float = 0.0


def _reason(diagnostics: str, default: str) -> str:
    """Read the first failure message, excluding recovery progress messages."""
    return next(
        (
            line.removeprefix("emb run: ")
            for line in diagnostics.splitlines()
            if line.startswith("emb run: ")
            and not line.startswith(
                ("emb run: waiting for turn ", "emb run: nothing to recover in ")
            )
        ),
        default,
    )


def _changes(run_id: str, diagnostics: str) -> list[str]:
    """Extract changed paths in diagnostic order without duplicates."""
    prefix = f"WARNING: read-only run {run_id} changed "
    return list(
        dict.fromkeys(
            line[len(prefix) :]
            for line in diagnostics.splitlines()
            if line.startswith(prefix)
        )
    )


def _no_collection_exit(run: runs.Run) -> int:
    """Settle a collected answer or retain the failure of a no-op recovery."""
    if run.turn_state == runs.COLLECTED:
        candidate = Path(run.reply) if run.reply else run.folder / "turn-1.md"
        with contextlib.suppress(OSError, UnicodeError):
            if candidate.read_text(encoding="utf-8").strip():
                return 0
    return run.last_exit if run.last_exit is not None else 0


def exit_code(panel: Mapping[str, Any], stopped: bool) -> int:
    """Return interrupted, clean completion or partial-result status."""
    if stopped:
        return 74
    return (
        0
        if all(
            member["status"] == "answered" and not member["changed"]
            for member in panel["members"]
        )
        else 83
    )


def member_line(member: Mapping[str, Any]) -> str:
    """Format one member, preserving zero exits and unknown elapsed time."""
    seconds = "-" if member["seconds"] is None else f"{member['seconds']:.0f}s"
    code = "-" if member["exit"] is None else str(member["exit"])
    return (
        f"member {member['n']} {member['engine']}:{member['model']} · "
        f"{member['effort'] or 'default'} · {member['status']} · {seconds} · "
        f"exit {code} · {member['bytes']} bytes · run {member['run'] or '-'}"
    )


class _Coordinator:
    """Serialize panel writes and freeze all member effects at finalization."""

    def __init__(
        self,
        panel_id: str,
        env: Mapping[str, str],
        factory: CoreFactory,
        stop: threading.Event | None,
        poll: float,
        err: TextIO | None,
    ) -> None:
        """Load the panel and allocate per-invocation state."""
        self.env = env
        self.panel = record.load(panel_id, env)
        self.factory = factory
        self.stop = stop if stop is not None else threading.Event()
        self.poll = poll
        self.err = err if err is not None else sys.stderr
        self.mutex = threading.Lock()
        self.ended = False
        self.failure: record.PanelError | None = None
        self.failed = threading.Event()
        self.deadline = float("inf")
        self.threads: list[threading.Thread] = []
        self.jobs: list[_Job] = []
        self.active: set[int] = set()
        self.retrying = False

    def persist(self, summary: bool = False) -> None:
        """Save under the mutex and surface storage failures to the coordinator."""
        try:
            record.save(self.panel, self.env)
            if summary:
                record.write_summary(self.panel, self.env)
        except Exception as error:
            self.storage_failed(error)
            assert self.failure is not None
            raise self.failure from error

    def storage_failed(self, error: Exception) -> None:
        """Stop scheduling after a persistence failure, retaining prior artifacts."""
        if self.failure is None:
            self.failure = record.PanelError(
                self.panel["id"],
                f"cannot persist panel results: {error}",
            )
        self.failed.set()

    def cutoff(self) -> bool:
        """Return whether no new member operation may begin."""
        return (
            self.ended
            or self.failed.is_set()
            or self.stop.is_set()
            or time.monotonic() >= self.deadline
        )

    def flush(self, job: _Job) -> None:
        """Append captured output and emit complete warnings under the mutex."""
        output, errors = job.out.getvalue(), job.err.getvalue()
        text = output[job.out_offset :] + errors[job.err_offset :]
        try:
            if text:
                record.append_log(self.panel["id"], job.member["n"], text, self.env)
            job.out_offset, job.err_offset = len(output), len(errors)
            end = errors.rfind("\n") + 1
            for line in errors[job.warning_offset : end].splitlines():
                if line.startswith("WARNING: "):
                    member = job.member
                    print(
                        f"WARNING: member {member['n']} "
                        f"({member['engine']}:{member['model']}): "
                        + line.removeprefix("WARNING: "),
                        file=self.err,
                    )
            job.warning_offset = end
        except Exception as error:
            self.storage_failed(error)
            assert self.failure is not None
            raise self.failure from error

    def core(self, job: _Job) -> Core:
        """Build a private Core whose explicit effort cannot fall back to the menu."""
        core = self.factory(
            self.env,
            stall_minutes=self.panel["stall_minutes"],
            quiet=False,
            out=job.out,
            err=job.err,
        )
        select = core._select

        def frozen_select(
            options: StartOptions,
            settings: Mapping[str, Any],
        ) -> tuple[Driver, WorkerSelection]:
            """Keep engine gates while excluding the single-run menu rows."""
            return select(options, {**settings, "menu": None})

        core._select = frozen_select
        return core

    def launch(self, member: dict[str, Any], recovering: bool) -> None:
        """Register a job before its daemon thread can begin or be delayed."""

        def capture(run_id: str) -> None:
            """Persist a run id before its launch returns, unless finalized."""
            with self.mutex:
                if self.ended or self.failed.is_set():
                    return
                member.update(run=run_id, status="running")
                self.persist()

        job = _Job(member, _RunIdStream(capture))
        with self.mutex:
            if self.cutoff():
                return
            self.jobs.append(job)
            self.active.add(member["n"])
            thread = threading.Thread(
                target=self.job, args=(job, recovering), daemon=True
            )
            self.threads.append(thread)
            thread.start()

    def attempt(self, job: _Job, kind: str) -> bool:
        """Persist the next attempt before entering Core, or stop at the cutoff."""
        with self.mutex:
            if self.cutoff():
                return False
            if kind == "start":
                job.member["status"] = "starting"
            else:
                job.member["status"] = "running"
            if job.began is None:
                job.began = time.monotonic()
            if kind != "start" or not self.retrying:
                job.member["attempts"].append(
                    {"kind": kind, "at": time.time(), "exit": None}
                )
            else:
                job.member["attempts"][-1]["at"] = time.time()
            self.persist()
            return True

    def completed_attempt(self, job: _Job, code: int) -> bool:
        """Persist the completed exit before another attempt can start."""
        with self.mutex:
            if self.ended or self.failed.is_set():
                return False
            job.member["attempts"][-1]["exit"] = code
            job.member["exit"] = code
            self.flush(job)
            self.persist()
            return True

    def collect(self, job: _Job, code: int, diagnostics: str) -> dict[str, Any]:
        """Read any available reply and restore previous change evidence."""
        member = job.member
        ids = [member.get("run"), *(a.get("run") for a in member["attempts"])]
        denials = list(member.get("denied_paths", []))
        for item in runs.referenced_denials(ids, self.env):
            if item not in denials:
                denials.append(item)
        result: dict[str, Any] = {
            "exit": code,
            "reason": None,
            "denied_paths": denials,
        }
        run_id = member["run"]
        run = None
        reply = ""
        problem = None
        evidence = job.err.getvalue()
        if run_id:
            try:
                run = runs.load(run_id, self.env)
            except runs.RecordError as error:
                problem = str(error)
            candidate = (
                Path(run.reply)
                if run and run.reply
                else (runs.run_dir(run_id, self.env) / "turn-1.md")
            )
            try:
                reply = candidate.read_text(encoding="utf-8")
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError) as error:
                problem = f"cannot read the reply {candidate}: {error}"
                result["reply"] = str(candidate)
            else:
                result.update(reply=str(candidate), bytes=len(reply.encode("utf-8")))
            log = record.folder(self.panel["id"], self.env) / record.MEMBERS_DIR
            with contextlib.suppress(FileNotFoundError):
                evidence += "\n" + (log / f"{member['n']}.log").read_text(
                    encoding="utf-8"
                )
            changed = [*member["changed"], *_changes(run_id, evidence)]
            if run and run.last_reason and run.last_reason.startswith("changed "):
                changed.extend(run.last_reason.removeprefix("changed ").split(", "))
            result["changed"] = list(dict.fromkeys(changed))
            result["closed"] = run is not None and run.status == runs.CLOSED
        if code == 124 and not result.get("closed"):
            result.update(
                status="stalled", reason=f"stalled; the turn keeps running in {run_id}"
            )
        elif code == 74 and not result.get("closed"):
            result.update(
                status="interrupted",
                reason=f"interrupted; the turn keeps running in {run_id}",
            )
        elif code:
            fallback = run.last_reason if run and run.last_exit == code else None
            result.update(
                status="failed",
                reason=_reason(diagnostics, fallback or f"the run exited {code}"),
            )
        elif problem:
            result.update(status="failed", reason=problem)
        elif reply.strip():
            result["status"] = "answered"
        else:
            result.update(
                status="failed",
                reason="empty answer" if "reply" in result else "no reply file",
            )
        return result

    def close(self, core: Core, job: _Job, result: dict[str, Any]) -> None:
        """Admit a non-forced close before cutoff, allowing it to finish afterward."""
        if result["status"] not in ("answered", "failed") or not job.member["run"]:
            return
        if result.get("closed"):
            result["close_error"] = None
            return
        with self.mutex:
            if self.cutoff():
                return
            # Admission is serialized with finalization; the call can outlive it.
            offset = len(job.err.getvalue())
        code = core.execute(lambda: core.close(job.member["run"]))
        result["closed"] = code == 0
        result["close_error"] = (
            None
            if code == 0
            else _reason(job.err.getvalue()[offset:], f"close exited {code}")
        )
        if code:
            job.err.write(
                f"WARNING: the run {job.member['run']} stays open: "
                f"{result['close_error']}; close it with "
                f"emb run --close {job.member['run']}\n"
            )

    def job(self, job: _Job, recovering: bool) -> None:
        """Collect one member, turning operation exceptions into member failures."""
        try:
            if not self.attempt(job, "recover" if recovering else "start"):
                return
            core = self.core(job)
            member = job.member
            if recovering:
                existing = runs.load(member["run"], self.env)
                if existing.status == runs.CLOSED:
                    code = existing.last_exit
                    if code is None:
                        code = (
                            member["exit"]
                            if member["exit"] not in (None, 74, 124)
                            else 0
                        )
                else:
                    code = core.execute(lambda: core.recover(member["run"]))
                    # A no-op recovery may follow collection or a launch failure.
                    if code == 0 and existing.turn_state != runs.SENT:
                        code = _no_collection_exit(existing)
            else:
                code = core.execute(
                    lambda: core.start(
                        StartOptions(
                            member["engine"],
                            dir=self.panel["dir"],
                            task_file=str(
                                record.folder(self.panel["id"], self.env)
                                / record.BRIEF_NAME
                            ),
                            model=member["model"],
                            effort=member["effort"],
                            read_only=True,
                            base=self.panel["base"],
                            parent=runs.parent("panel", self.panel["id"]),
                            read=tuple(self.panel["read"]),
                            trailers=(),
                            claim=None,
                            window=False,
                        )
                    )
                )
            diagnostics = job.err.getvalue()
            if not self.completed_attempt(job, code):
                return
            if code == 124 and member["run"] and not recovering:
                if not self.attempt(job, "stall"):
                    return
                offset = len(job.err.getvalue())
                code = core.execute(lambda: core.recover(member["run"]))
                diagnostics = job.err.getvalue()[offset:]
                if not self.completed_attempt(job, code):
                    return
            result = self.collect(job, code, diagnostics)
            self.close(core, job, result)
            with self.mutex:
                if self.ended or self.failed.is_set():
                    return
                member.update(result)
                self.finish_job(job)
        except Exception as error:
            with self.mutex:
                if self.ended or self.failed.is_set():
                    return
                job.member.update(
                    status="failed",
                    reason=(f"unexpected error: {type(error).__name__}: {error}"),
                    exit=1,
                )
                if job.member["attempts"]:
                    job.member["attempts"][-1]["exit"] = 1
                try:
                    self.finish_job(job)
                except Exception as failure:
                    self.storage_failed(failure)

    def finish_job(self, job: _Job) -> None:
        """Persist a terminal member and remove it from the active set."""
        if job.began is not None:
            job.elapsed = time.monotonic() - job.began
        job.member["seconds"] = (job.member["seconds"] or 0.0) + job.elapsed
        self.flush(job)
        self.persist(summary=True)
        self.active.discard(job.member["n"])
        print(member_line(job.member), file=self.err, flush=True)

    def pause(self, seconds: float) -> None:
        """Wait in bounded ticks while observing stop, failure and deadline."""
        until = min(time.monotonic() + seconds, self.deadline)
        while not self.cutoff() and time.monotonic() < until:
            self.failed.wait(min(self.poll, max(0.0, until - time.monotonic())))

    def drive(
        self, members: Sequence[dict[str, Any]], recovering: bool, spacing: float
    ) -> Outcome:
        """Start jobs in order, wait for their completion and finalize once."""
        self.deadline = time.monotonic() + self.panel["timeout_minutes"] * 60
        for index, member in enumerate(members):
            if self.cutoff():
                break
            self.launch(member, recovering)
            if index < len(members) - 1:
                self.pause(spacing)
        while not self.cutoff():
            with self.mutex:
                if not self.active:
                    break
            self.pause(self.poll)
        return self.finalize()

    def finalize(self) -> Outcome:
        """Freeze member effects, flush diagnostics and save the final snapshot."""
        with self.mutex:
            self.ended = True
            if self.failure is not None:
                raise self.failure
            stopped = self.stop.is_set()
            for job in self.jobs:
                self.flush(job)
                if job.member["n"] in self.active:
                    available = self.collect(job, job.member["exit"] or 0, "")
                    for key in ("reply", "bytes", "changed", "closed"):
                        if key in available:
                            job.member[key] = available[key]
                if job.member["n"] in self.active and job.began is not None:
                    job.member["seconds"] = (job.member["seconds"] or 0.0) + (
                        time.monotonic() - job.began
                    )
            for member in self.panel["members"]:
                if member["status"] == "pending":
                    member.update(
                        status="not started",
                        reason=(
                            "the panel stopped before this member started"
                            if stopped
                            else (
                                "the panel's deadline passed before this member started"
                            )
                        ),
                    )
                    print(member_line(member), file=self.err, flush=True)
                elif member["n"] in self.active:
                    status = "interrupted" if stopped else "timed out"
                    prefix = (
                        "the panel stopped"
                        if stopped
                        else (
                            f"the panel's {self.panel['timeout_minutes']:g} minute "
                            "deadline passed"
                        )
                    )
                    suffix = (
                        f"; the turn keeps running in {member['run']}"
                        if member["run"]
                        else " before its run started"
                    )
                    member.update(status=status, reason=prefix + suffix)
                    print(member_line(member), file=self.err, flush=True)
            self.panel.update(
                status="incomplete"
                if any(
                    member["status"] in record.UNFINISHED
                    for member in self.panel["members"]
                )
                else "done",
                ended_at=time.time(),
                exit=exit_code(self.panel, stopped),
            )
            self.persist(summary=True)
            return Outcome(copy.deepcopy(self.panel), stopped, self.threads)


def start(
    panel_id: str,
    env: Mapping[str, str],
    *,
    core_factory: CoreFactory = Core,
    stop: threading.Event | None = None,
    spacing: float = START_SPACING,
    poll_seconds: float = POLL_SECONDS,
    err: TextIO | None = None,
) -> Outcome:
    """Run pending members while the caller holds the panel's lifetime lock."""
    coordinator = _Coordinator(panel_id, env, core_factory, stop, poll_seconds, err)
    if any(member["status"] != "pending" for member in coordinator.panel["members"]):
        raise record.PanelError(panel_id, "a start requires every member to be pending")
    _pin_base(coordinator)
    return coordinator.drive(coordinator.panel["members"], False, spacing)


def _pin_base(coordinator: _Coordinator) -> None:
    """Save one commit before any member starts, including legacy retries."""
    panel = coordinator.panel
    if panel["base"] is not None:
        return
    for member in panel["members"]:
        ids = [member["run"], *(item.get("run") for item in member["attempts"])]
        for run_id in ids:
            if run_id:
                saved = runs.load(run_id, coordinator.env)
                if saved.base:
                    panel["base"] = saved.base
                    coordinator.persist()
                    return
    core = Core(coordinator.env)
    base = core._git_out(panel["dir"], "rev-parse", "--verify", "HEAD^{commit}")
    if not base:
        raise record.PanelError(panel["id"], "the checkout has no commit to read")
    panel["base"] = base
    coordinator.persist()


def recover(
    panel_id: str,
    env: Mapping[str, str],
    *,
    stall_minutes: float,
    timeout_minutes: float,
    core_factory: CoreFactory = Core,
    stop: threading.Event | None = None,
    poll_seconds: float = POLL_SECONDS,
    err: TextIO | None = None,
) -> Outcome:
    """Collect unfinished members without starting runs or sending new questions."""
    coordinator = _Coordinator(panel_id, env, core_factory, stop, poll_seconds, err)
    panel = coordinator.panel
    members = []
    for member in panel["members"]:
        if member["status"] in (*record.UNFINISHED, "pending"):
            if member["run"]:
                members.append(member)
            else:
                member.update(
                    status="failed",
                    reason=("the panel ended before this member's run started"),
                )
    panel.update(
        status="running",
        stall_minutes=stall_minutes,
        timeout_minutes=timeout_minutes,
        ended_at=None,
        exit=None,
    )
    if not members:
        coordinator.finalize()
        raise NothingToRecover(f"the panel {panel_id} has no unfinished member")
    coordinator.persist()
    return coordinator.drive(members, True, 0.0)


def _close_run(
    panel: dict[str, Any],
    member: dict[str, Any],
    run_id: str,
    env: Mapping[str, str],
    core_factory: CoreFactory,
    err: TextIO | None = None,
) -> dict[str, Any]:
    """Close one run without force and append its diagnostics to the member log."""
    output, errors = io.StringIO(), io.StringIO()
    core = core_factory(env, quiet=True, out=output, err=errors)
    code = core.execute(lambda: core.close(run_id))
    reason = None if code == 0 else _reason(errors.getvalue(), f"close exited {code}")
    return _close_result(
        panel,
        member,
        run_id,
        env,
        output.getvalue() + errors.getvalue(),
        errors.getvalue(),
        code == 0,
        reason,
        err,
    )


def _cleanup_run(
    panel: dict[str, Any],
    member: dict[str, Any],
    existing: runs.Run,
    env: Mapping[str, str],
    err: TextIO | None,
) -> dict[str, Any] | None:
    """Check an ended run under its locks and clean its detached worktree."""
    diagnostics = ""
    reason = None
    try:
        with Core(env)._turn_lock(existing.id, "close"), runs.record_lock(existing):
            existing = runs.load(existing.id, env)
            reason = worktree_removal_reason(existing)
            if reason is None:
                with paths.worktree_lock(Path(existing.common or existing.repo), env):
                    try:
                        if not worktrees.remove_detached(existing):
                            return None
                    except OSError as error:
                        diagnostics = f"WARNING: read-only run {existing.id}: {error}\n"
    except (RunError, runs.RecordError, paths.RuntimeDirError, OSError) as error:
        reason = str(error)
    if reason:
        diagnostics = f"WARNING: read-only run {existing.id}: {reason}\n"
        return _close_result(
            panel,
            member,
            existing.id,
            env,
            diagnostics,
            diagnostics,
            False,
            reason,
            err,
        )
    return _close_result(
        panel, member, existing.id, env, diagnostics, diagnostics, True, None, err
    )


def _close_result(
    panel: dict[str, Any],
    member: dict[str, Any],
    run_id: str,
    env: Mapping[str, str],
    log: str,
    diagnostics: str,
    closed: bool,
    reason: str | None,
    err: TextIO | None,
) -> dict[str, Any]:
    """Log a close or cleanup and forward its warnings to the caller."""
    record.append_log(panel["id"], member["n"], log, env)
    if err is not None:
        for line in diagnostics.splitlines():
            if line.startswith("WARNING: "):
                print(
                    f"WARNING: member {member['n']} "
                    f"({member['engine']}:{member['model']}): "
                    + line.removeprefix("WARNING: "),
                    file=err,
                )
    return {"n": member["n"], "run": run_id, "closed": closed, "close_error": reason}


def close(
    panel_id: str,
    env: Mapping[str, str],
    *,
    core_factory: CoreFactory = Core,
    err: TextIO | None = None,
) -> list[dict[str, Any]]:
    """Close runs and clean terminal runs' worktrees under the panel lock."""
    panel = record.load(panel_id, env)
    results = []
    for member in panel["members"]:
        run_ids = dict.fromkeys(
            [member["run"]]
            + [a.get("run") for a in member["attempts"] if a["kind"] == "retry"]
        )
        targets: dict[str, str | runs.Run | None] = {}
        for run_id in run_ids:
            if not run_id:
                continue
            try:
                existing = runs.load(run_id, env)
            except runs.RecordError as error:
                targets[run_id] = str(error)
            else:
                if existing.status not in (runs.CLOSED, runs.FAILED) or (
                    existing.status == runs.FAILED and existing.turn_state == runs.SENT
                ):
                    targets[run_id] = None
                elif existing.read_only and existing.detached:
                    targets[run_id] = existing
        remaining = set(targets)
        refused = []
        for run_id, error in targets.items():
            if isinstance(error, runs.Run):
                result = _cleanup_run(panel, member, error, env, err)
            elif error is not None:
                result = {
                    "n": member["n"],
                    "run": run_id,
                    "closed": False,
                    "close_error": error,
                }
            else:
                result = _close_run(panel, member, run_id, env, core_factory, err)
            if result is not None:
                results.append(result)
            if result is not None and not result["closed"]:
                refused.append(f"{run_id}: {result['close_error']}")
            else:
                remaining.remove(run_id)
            member.update(closed=not remaining, close_error="; ".join(refused) or None)
            record.save(panel, env)
    return results


def retry(
    panel_id: str,
    number: str,
    env: Mapping[str, str],
    *,
    stall_minutes: float,
    timeout_minutes: float,
    core_factory: CoreFactory = Core,
    stop: threading.Event | None = None,
    poll_seconds: float = POLL_SECONDS,
    err: TextIO | None = None,
) -> Outcome:
    """Replace one failed answer while the caller holds the panel's lifetime lock."""
    coordinator = _Coordinator(panel_id, env, core_factory, stop, poll_seconds, err)
    panel = coordinator.panel
    try:
        n = int(number)
    except ValueError:
        n = 0
    member = next((m for m in panel["members"] if m["n"] == n), None)
    if member is None:
        raise MemberMissingError(f"panel {panel_id} has no member {number}")
    if member["status"] not in (
        "failed",
        "stalled",
        "timed out",
        "interrupted",
        "not started",
    ):
        raise RetryRefused(f"member {n} is {member['status']}")
    _pin_base(coordinator)
    old_run = member["run"]
    if old_run:
        result = _close_run(panel, member, old_run, env, core_factory, coordinator.err)
        member.update(closed=result["closed"], close_error=result["close_error"])
        record.save(panel, env)
        if not result["closed"]:
            raise RetryRefused(
                f"cannot close old run {old_run}: {result['close_error']}"
            )
    member["attempts"].append(
        {"kind": "retry", "at": time.time(), "exit": None, "run": old_run}
    )
    coordinator.retrying = True
    member.update(
        status="pending",
        run=None,
        exit=None,
        reason=None,
        reply=None,
        bytes=0,
        changed=[],
        seconds=None,
        closed=False,
        close_error=None,
    )
    panel.update(
        status="running",
        ended_at=None,
        exit=None,
        stall_minutes=stall_minutes,
        timeout_minutes=timeout_minutes,
    )
    coordinator.persist(summary=True)
    outcome = coordinator.drive([member], False, 0.0)
    outcome.panel.update(
        status="done"
        if all(m["status"] == "answered" for m in outcome.panel["members"])
        else "incomplete",
        ended_at=time.time(),
        exit=exit_code(outcome.panel, outcome.stopped),
    )
    record.save(outcome.panel, env)
    record.write_summary(outcome.panel, env)
    return outcome
