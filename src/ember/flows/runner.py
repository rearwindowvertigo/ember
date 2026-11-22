"""Execute flow waves and recover interrupted runs, checks and merges."""

import contextlib
import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from ember import branches, choices, citations, holds, paths, prepare, sandbox, tmux
from ember.engines import Driver
from ember.flows import briefs, lock, plan, record, verdict
from ember.runs import exits
from ember.runs import record as runs
from ember.runs.core import (
    HOOK_REFUSAL_NAME,
    Core,
    RunError,
    StartOptions,
    WorkerSelection,
)

POLL_SECONDS = 1.0
OUTPUT_LIMIT = 4000
GIT_TIMEOUT = 120
_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "EMBER_FLOW_RUNNER",
}


class RunnerError(Exception):
    """An operation cannot safely continue in the flow worktree."""


class PreparationFailedError(RunnerError):
    """A flow worktree's preparation failed before scheduling work."""


def end_preparation(flow: Mapping[str, Any]) -> None:
    """End the recorded flow preparation group while its identity matches."""
    saved = flow.get("preparation") or {}
    if saved.get("pid") is not None and saved.get("start") is not None:
        prepare.end_group(saved["pid"], saved["start"])


def preparation_known(flow: Mapping[str, Any], env: Mapping[str, str]) -> None:
    """Refuse automatic continuation when a preparation outcome is unknown."""
    end_preparation(flow)
    if (flow.get("preparation") or {}).get("state") == "running":
        raise RunnerError(
            "flow preparation is indeterminate; inspect its log and worktree"
        )
    for task in flow["tasks"]:
        for role in ("writer", "reviewer"):
            run_id = task[f"{role}_run"]
            if not run_id or task["status"] in ("merged", "dropped"):
                continue
            run = runs.load(run_id, env)
            saved = run.preparation or {}
            if saved.get("pid") is not None and saved.get("start") is not None:
                prepare.end_group(saved["pid"], saved["start"])
            if run.turn == 0 and saved.get("state") == "running":
                raise RunnerError(
                    f"{run_id}: preparation is indeterminate; "
                    "inspect its log and worktree"
                )


def prepare_worktree(
    flow: dict[str, Any],
    env: Mapping[str, str],
    stop: threading.Event | None = None,
) -> None:
    """Prepare a flow tree once, retaining each unsuccessful attempt and log."""
    previous = flow.get("preparation")
    if previous and previous["state"] == "ok":
        return
    if previous and previous["state"] == "running":
        end_preparation(flow)
        raise RunnerError(
            "flow preparation is indeterminate; inspect its log and worktree"
        )
    directory = record.folder(flow["id"], env)
    history = flow.get("preparation_history", [])
    log = directory / (f"prepare-{len(history) + 1}.log" if previous else "prepare.log")
    began = time.monotonic()
    command = ""
    current: dict[str, Any] = {}
    try:
        common = (
            flow.get("common")
            or branches.open_repository(Path(flow["worktree"]), env, "flow").common
        )
        command = prepare.lookup(common, env=env) or ""
        if not command and previous is None:
            return
        if previous:
            flow["preparation_history"] = [*history, previous]
        current = {"command": command, "state": "running", "log": str(log)}
        flow["preparation"] = current
        record.save(flow, env)

        def started(pid: int, start: int) -> None:
            flow["preparation"].update(pid=pid, start=start)
            record.save(flow, env)

        if command:
            result = prepare.run(
                command,
                flow["worktree"],
                log,
                started=started,
                stop=_PreparationStop(flow["id"], env, stop),
                env=env,
            )
        else:
            runs.write_private(log, "No preparation command is set.\n")
            result = prepare.Result("ok", None, 0, str(log), [], None)
    except (Exception, KeyboardInterrupt) as error:
        end_preparation(flow)
        if previous and flow.get("preparation") is previous:
            flow["preparation_history"] = [*history, previous]
        result = prepare.Result(
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            None,
            time.monotonic() - began,
            str(log),
            [],
            "preparation interrupted"
            if isinstance(error, KeyboardInterrupt)
            else f"preparation failed: {error}",
        )
        if not log.exists():
            with contextlib.suppress(OSError):
                runs.write_private(log, (result.message or "preparation failed") + "\n")
    flow["preparation"] = {
        **current,
        "command": command,
        **result.as_dict(),
    }
    record.save(flow, env)
    if result.state != "ok":
        raise PreparationFailedError(f"{result.message}; log: {log}")


class _PreparationStop(threading.Event):
    """Observe cancellation during preparation without holding lifecycle.lock."""

    def __init__(
        self, flow_id: str, env: Mapping[str, str], shutdown: threading.Event | None
    ) -> None:
        """Keep the cancellation record and the runner's shutdown event."""
        super().__init__()
        self.flow_id = flow_id
        self.env = env
        self.shutdown = shutdown

    def is_set(self) -> bool:
        """Return whether the command or the runner requested cancellation."""
        return bool(
            (self.shutdown and self.shutdown.is_set())
            or record.cancel_request(self.flow_id, self.env) is not None
        )

    def wait(self, timeout: float | None = None) -> bool:
        """Wait briefly, then reread the cancellation request."""
        if self.shutdown is not None:
            self.shutdown.wait(timeout)
        else:
            super().wait(timeout)
        return self.is_set()


class _RunIdStream(io.StringIO):
    """Capture the first complete run announcement before the turn finishes."""

    def __init__(self, capture: Callable[[str], None]) -> None:
        """Keep the callback and the unfinished output line."""
        super().__init__()
        self.capture = capture
        self.pending = ""
        self.captured = False

    def write(self, text: str) -> int:
        """Save an announced id as soon as its newline arrives."""
        count = super().write(text)
        if not self.captured:
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line.startswith("run "):
                    run_id = runs.check_id(line[4:])
                    self.capture(run_id)
                    self.captured = True
                    break
        return count


@contextlib.contextmanager
def _lifecycle_guard(folder: Path) -> Iterator[None]:
    """Serialize the final answer read with command startup and enqueueing."""
    fd = paths.open_private_file(folder / "lifecycle.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _process_start(pid: int) -> str | None:
    """Read a Linux process's start ticks to distinguish reused process ids."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except OSError, IndexError:
        return None


def _kill_check(identity: Mapping[str, Any] | None) -> None:
    """Kill a recorded check group only while its leader has the same identity."""
    if identity and identity.get("start") is not None:
        pid = identity["pid"]
        if _process_start(pid) == identity["start"]:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)


class _Runner:
    """Own one flow record and serialize all writes made by task threads."""

    def __init__(
        self,
        flow_id: str,
        env: Mapping[str, str],
        core_factory: Callable[..., Core],
        poll_seconds: float,
        shutdown: threading.Event,
    ) -> None:
        """Load the stored plan and prepare the per-attempt state."""
        self.env = {key: value for key, value in env.items() if key not in _GIT_ENV}
        self.id = flow_id
        self.folder = record.folder(flow_id, self.env)
        self.flow = record.load(flow_id, self.env)
        self.plan = plan.from_json(
            json.loads((self.folder / record.PLAN_NAME).read_text(encoding="utf-8")),
            Path(self.flow["plan"]),
            subjects={task["id"]: task["subject"] for task in self.flow["tasks"]},
        )
        self.factory = core_factory
        self.poll = poll_seconds
        self.shutdown = shutdown
        self.mutex = threading.Lock()
        self.preparing = threading.Condition(self.mutex)
        self.preparing_count = 0
        self.finished = False
        self.failure: str | None = None
        self.preparation_recovered = False
        self.ahead: set[str] = set()
        """Tasks whose review started during the running wave."""
        self.retry: set[tuple[str, str]] = set()
        """`(task, role)` pairs whose start is repeated on a fallback worker."""

    def _save(self) -> None:
        """Save while the caller holds the record mutex."""
        record.save(self.flow, self.env)

    def _event(self, kind: str, **fields: Any) -> None:  # noqa: ANN401
        """Append an event while the caller holds the record mutex."""
        record.append_event(
            self.id,
            {"kind": kind, "attempt": self.flow.get("attempt", 0), **fields},
            self.env,
        )

    def change(
        self,
        task: dict[str, Any],
        *,
        stop_provider_kind: str | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> None:
        """Persist task changes and emit each status transition."""
        with self.mutex:
            if self.finished:
                return
            task.update(fields)
            if fields.get("status") in ("stopped", "conflict", "accepted", "merged"):
                task["entry"] = None
            self._save()
            if "status" in fields:
                self._event(
                    "task",
                    task=task["id"],
                    status=task["status"],
                    reason=task["reason"],
                    warnings=task.get("warnings", []),
                )
                if task["status"] in ("stopped", "conflict"):
                    self._lead(task, task["reason"], stop_provider_kind)

    def _lead(
        self,
        task: dict[str, Any] | None,
        reason: str,
        provider_kind: str | None = None,
    ) -> None:
        """Emit a lead stop with commands under the record mutex."""
        suffix = {"send": " <file>", "worker": " <engine>:<model>"}
        choices = (
            []
            if task is None
            else [
                f"emb flow --lead {self.id} {task['id']} --{kind}{suffix.get(kind, '')}"
                for kind in record.allowed_answers(task)
            ]
        )
        self._event(
            "lead_stop",
            task=task["id"] if task else None,
            status=task["status"] if task else self.flow["status"],
            reason=reason,
            choices=choices,
            provider_kind=provider_kind,
        )

    def finish(self, status: str, reason: str | None = None) -> None:
        """Record the terminal outcome outside signal handlers."""
        with self.mutex:
            request = record.cancel_request(self.id, self.env)
            if request is not None:
                if self.flow["status"] != "cancelled":
                    if self.flow["runner"]:
                        self.flow["runner"]["exiting"] = True
                    record.finish_cancel(self.flow, request, self.env)
                self.finished = True
                return
            self.flow.update(status=status, reason=reason)
            if self.flow["runner"]:
                self.flow["runner"]["exiting"] = True
            if status in record.ENDED:
                self.flow["ended_at"] = time.time()
            self._save()
            if status in record.ENDED:
                complete = all(t["status"] == "merged" for t in self.flow["tasks"])
                self._event(
                    "end",
                    status=status,
                    reason=reason,
                    exit=(
                        exits.PREPARATION_FAILED
                        if record.preparation_failed(self.flow)
                        else 0
                        if status == "done" and complete
                        else 82
                    ),
                )
            else:
                self._event("flow", status=status, reason=reason)
            self.finished = True

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a bounded git command in the flow checkout with a clean environment."""
        try:
            result = subprocess.run(
                ["git", "-C", self.flow["worktree"], *args],
                env=self.env,
                capture_output=True,
                text=True,
                errors="surrogateescape",
                timeout=GIT_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RunnerError(f"git {args[0]}: {error}") from error
        if check and result.returncode:
            raise RunnerError(
                f"git {args[0]}: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def standing_read(self) -> tuple[str, ...]:
        """Return the `sandbox.read` paths every worker of this flow also reads."""
        try:
            return citations.standing(choices.read(None, env=self.env))
        except Exception:  # noqa: BLE001 - a brief is written without the listing
            return ()

    def tip(self, branch: str) -> str:
        """Resolve a commit without allowing a ref to become an option."""
        return self.git(
            "rev-parse", "--verify", "--end-of-options", f"{branch}^{{commit}}"
        ).stdout.strip()

    def core(self, out: TextIO, err: TextIO) -> Core:
        """Make a private Core and exclude later menu effort fallback."""
        core = self.factory(
            self.env,
            stall_minutes=self.flow["stall_minutes"],
            quiet=False,
            out=out,
            err=err,
        )
        select = core._select

        def frozen_select(
            options: StartOptions,
            settings: Mapping[str, Any],
        ) -> tuple[Driver, WorkerSelection]:
            """Keep engine gates while using the flow's explicit worker options."""
            return select(options, {**settings, "menu": None})

        core._select = frozen_select
        return core

    def freeze(self, worker: dict[str, Any]) -> dict[str, Any]:
        """Resolve a driver's default once, retaining explicit engine-default null."""
        if worker.get("effort_frozen"):
            return worker
        result = dict(worker)
        if result["effort"] is None:
            core = self.core(io.StringIO(), io.StringIO())
            result["effort"] = core.load_driver(result["engine"]).default_effort(
                result["model"]
            )
        result["effort_frozen"] = True
        return result

    def worker(self, task: Mapping[str, Any], role: str) -> dict[str, Any]:
        """Return the task override or the frozen flow worker."""
        return task[role] or self.flow[role]

    def artifact(self, task: dict[str, Any], kind: str, text: str) -> str:
        """Allocate a monotonically numbered private artifact for this task."""
        with self.mutex:
            if self.finished or self.shutdown.is_set():
                raise RunnerError("the runner is stopping")
            number = task.get("artifact", 0) + 1
            task["artifact"] = number
            self._save()
            directory = paths.ensure_private_dir(
                self.folder / "briefs", root=self.folder
            )
            path = directory / f"{task['id']}-{kind}-{number}.md"
            runs.write_private(path, text)
        return str(path)

    def allowed(self, task: dict[str, Any], write: bool = False) -> bool:
        """Stop new turns after shutdown or a provider error."""
        if self.shutdown.is_set():
            return False
        with self.mutex:
            halted = self.flow["halted"]
        if not halted:
            return True
        if self.flow.get("preparation_halt"):
            self.change(
                task,
                status="stopped",
                reason=halted,
                preparation_blocked=(
                    "writer"
                    if write
                    else "send"
                    if task["entry"] == "send"
                    else "continue"
                    if task.get("review_action") == "fix" or task.get("turn_pending")
                    else "reviewer"
                ),
            )
            return False
        if write:
            self.change(task, status="pending", entry="write")
        else:
            self.change(task, status="stopped", reason=f"new runs halted: {halted}")
        return False

    def halt_preparation(self, task: dict[str, Any], role: str, reason: str) -> None:
        """Persist a preparation halt before other runs reach admission."""
        with self.mutex:
            if self.finished:
                return
            if not self.flow.get("preparation_halt"):
                self.flow["preparation_halt"] = reason or "worktree preparation failed"
            self.flow["halted"] = self.flow["preparation_halt"]
            task["preparation_blocked"] = role
            self._save()

    def call(
        self,
        task: dict[str, Any],
        role: str,
        action: Callable[[Core], int],
        *,
        review_failed_turn: bool = False,
        fallback: bool = False,
    ) -> bool:
        """Run one Core action and preserve its diagnostics on a failure.

        With `fallback`, a provider error that the owner's fallback list
        covers switches the task's worker and marks the pair in `self.retry`
        in place of stopping the task.
        """

        def capture(run_id: str) -> None:
            """Persist the run before its launch finishes."""
            self.change(task, **{f"{role}_run": run_id})

        out, err = _RunIdStream(capture), io.StringIO()
        core = self.core(out, err)

        def admit(run: runs.Run) -> None:
            with self.mutex:
                halted = self.flow.get("preparation_halt")
                if halted:
                    raise RunError(
                        exits.PREPARATION_FAILED,
                        halted,
                        run=run.id,
                    )

        core.admit = admit
        core.prepare_stop = self.shutdown
        prepare_run = core._prepare

        def tracked_preparation(
            run: runs.Run, command: str, stop: threading.Event | None
        ) -> runs.Run:
            """Keep shutdown alive until preparation has ended and saved its result."""
            with self.preparing:
                if self.shutdown.is_set():
                    raise RunError(
                        exits.PREPARATION_FAILED, "the runner is stopping", run=run.id
                    )
                self.preparing_count += 1
            try:
                return prepare_run(run, command, stop)
            except RunError as error:
                if error.code == exits.PREPARATION_FAILED:
                    self.halt_preparation(task, role, str(error))
                raise
            finally:
                with self.preparing:
                    self.preparing_count -= 1
                    self.preparing.notify_all()

        core._prepare = tracked_preparation
        code = core.execute(lambda: action(core))
        reason = "; ".join(
            line
            for line in err.getvalue().splitlines()
            if line.strip() and not line.lstrip().startswith(("next:", "owner:"))
        )
        if code == exits.PREPARATION_FAILED:
            self.halt_preparation(task, role, reason)
        run_id = task[f"{role}_run"]
        denials = runs.referenced_denials([run_id], self.env)
        with self.mutex:
            old = task.get("denied_paths", [])
            fresh = [item for item in denials if item not in old]
            if fresh:
                task["denied_paths"] = [*old, *fresh]
                self._event(
                    "denied_paths", task=task["id"], role=role, denied_paths=fresh
                )
                self._save()
        if code == 0:
            return not self.shutdown.is_set()
        run_id = task[f"{role}_run"]
        if review_failed_turn and run_id:
            recovered = runs.load(run_id, self.env)
            if recovered.turn_state == runs.COMMITTED:
                return not self.shutdown.is_set()
        if code == 124:
            reason = f"stalled; the turn keeps running in {run_id}"
        provider_kind = None
        engine = self.worker(task, role)["engine"]
        if code == 73 and run_id:
            with contextlib.suppress(runs.RecordError):
                run = runs.load(run_id, self.env)
                # The saved worker can have changed since the run started.
                engine = run.engine
                if run.turn == 0 or run.last_variant == "starting":
                    # A start that could not reach its provider has no turn.
                    provider_kind = "outage"
                provider_kind = (
                    runs.load_outcome(run, run.turn).get("provider_kind")
                    or provider_kind
                )
        if (
            code == 73
            and fallback
            and run_id
            and self.fall_back(task, role, run_id, provider_kind or "outage")
        ):
            self.retry.add((task["id"], role))
            return False
        if code == 73 and provider_kind != "capacity":
            with self.mutex:
                if not self.flow.get("preparation_halt"):
                    self.flow["halted"] = f"provider error on {engine}; new runs halted"
                    self._save()
        if role == "writer" and run_id:
            self.metadata(task)
        self.change(
            task,
            status="stopped",
            reason=reason or f"the run exited {code}",
            stop_provider_kind=provider_kind,
        )
        return False

    def fallback_avoid(
        self, task: Mapping[str, Any], role: str
    ) -> tuple[tuple[str, str], ...]:
        """Return the workers a fallback for a role must not use.

        A review does not fall back to a model that wrote the work: the
        task's saved writer, its writer run, and every earlier writer whose
        branch that run continues.
        """
        if role != "reviewer":
            return ()
        writer = self.worker(task, "writer")
        avoid = [(writer["engine"], writer["model"])]
        with contextlib.suppress(runs.RecordError, TypeError):
            # A hold or a fallback can have sent the writing to other workers.
            run = runs.load(task["writer_run"], self.env)
            avoid += [
                (item.engine, item.model)
                for item in (run, *runs.linked(run, "continues"))
            ]
        return tuple(dict.fromkeys(avoid))

    def fall_back(
        self, task: dict[str, Any], role: str, run_id: str, kind: str
    ) -> bool:
        """Close a failed run and name the fallback worker that repeats its start.

        The repeat is saved on the task as `fallback_pending`, so a runner
        that stops before the repeat starts makes it after a resume. The
        task's saved worker does not change: a later start of the role uses
        the first choice again.

        Args:
            task: The task whose run a provider error ended.
            role: `writer` or `reviewer`.
            run_id: The failed run.
            kind: The failure's provider kind.

        Returns:
            Whether a repeat is pending. It is not when no entry of the
            owner's fallback list applies, when the failed run's resend is
            uncertain, or when the failed run cannot be closed.
        """
        try:
            run = runs.load(run_id, self.env)
            if run.last_variant == "uncertain":
                # The session may still hold the prompt; the lead inspects it.
                return False
            answers = choices.read(None, env=self.env)["answers"]
            history = task.get("fallback_failed", {}).get(role, [])
            row = holds.replacement(
                answers,
                [*(tuple(item) for item in history), (run.engine, run.model, kind)],
                avoid=self.fallback_avoid(task, role),
                env=self.env,
            )
        except Exception:  # noqa: BLE001 - without the settings the task stops
            return False
        if row is None or self.close(run_id):
            return False
        worker = self.freeze(
            {
                "engine": row["engine"],
                "model": row["model"],
                "effort": row["effort"],
                "source": "fallback",
            }
        )
        entry = {
            "role": role,
            "from": f"{run.engine}:{run.model}",
            "to": f"{row['engine']}:{row['model']}",
            "cause": kind,
            "run": run_id,
            "at": time.time(),
        }
        continued = role == "writer" and bool(runs.work_tip(run))
        with self.mutex:
            task.setdefault("fallback_failed", {}).setdefault(role, []).append(
                [run.engine, run.model, kind]
            )
            task.setdefault("fallbacks", []).append(entry)
            task["fallback_pending"] = {
                "role": role,
                "worker": worker,
                "failed_run": run_id,
                "continue_run": run_id if continued else None,
            }
            if (
                role == "writer"
                and not continued
                and run.branch
                and run.branch not in self.flow["abandoned"]
            ):
                # A continued branch lands with the task's branch.
                self.flow["abandoned"].append(run.branch)
            self._save()
            self._event("fallback", task=task["id"], **entry)
        return True

    def metadata(self, task: dict[str, Any]) -> runs.Run:
        """Restore writer metadata from its durable run record.

        The base of a writer that continues an earlier writer's branch is
        the base of the first writer, so a review reads all of the work.
        """
        run = runs.load(task["writer_run"], self.env)
        self.change(
            task,
            branch=run.branch,
            worktree=run.worktree,
            task_base=runs.first_base(run),
        )
        return run

    def declarations(self, task: Mapping[str, Any]) -> dict[str, list[str]]:
        """Read the saved wave declarations, including after other tasks move on."""
        if "wave_declarations" in task:
            return task["wave_declarations"]
        return {
            item["id"]: list(self.plan.task(item["id"]).files)
            for item in self.flow["tasks"]
            if item["id"] != task["id"] and item["wave"] == task["wave"]
        }

    def other_files(self, task: Mapping[str, Any]) -> list[str]:
        """Return the declarations from the task's original wave."""
        return [path for files in self.declarations(task).values() for path in files]

    def post_turn(self, task: dict[str, Any], initial: bool) -> bool:
        """Validate recovered and uninterrupted turns by the same changed-file rules."""
        run = self.metadata(task)
        if run.turn == 0 or run.turn_state is None:
            self.change(
                task,
                status="stopped",
                reason="the writer was not launched; choose --worker to replace it",
            )
            return False
        head = self.tip(task["branch"])
        previous = task["task_base"] if initial else task.get("fix_base")
        if head == previous:
            self.change(
                task,
                status="stopped",
                reason=(
                    "the writer made no change" if initial else "the fix made no change"
                ),
            )
            return False
        changed = self.git(
            "diff", "--name-only", "--no-renames", "-z", task["task_base"], head, "--"
        ).stdout.split("\0")[:-1]
        own = self.plan.task(task["id"]).files
        outside = [
            path for path in changed if not any(plan.overlaps(path, f) for f in own)
        ]
        self.change(
            task,
            head=head,
            changed_files=changed,
            outside_files=outside,
            turn_pending=False,
        )
        for other, files in self.declarations(task).items():
            for path in changed:
                if any(plan.overlaps(path, f) for f in files):
                    self.change(
                        task,
                        status="stopped",
                        reason=(
                            f"changed {path}, which task {other} in this wave declares"
                        ),
                    )
                    return False
        return True

    def start(self, task: dict[str, Any], role: str, path: str) -> bool:
        """Start an editing writer or a fresh read-only reviewer.

        A start that a provider error ends is repeated on the worker of the
        owner's fallback list while an entry applies.
        """
        while True:
            if not self.allowed(task, write=role == "writer"):
                return False
            pending = task.get("fallback_pending") or {}
            if pending.get("role") != role:
                pending = {}
            worker = pending.get("worker") or self.worker(task, role)
            options = StartOptions(
                engine=worker["engine"],
                model=worker["model"],
                effort=worker["effort"],
                dir=self.flow["worktree"] if role == "writer" else task["worktree"],
                task_file=path,
                read_only=role == "reviewer",
                read=self.plan.read,
                trailers=self.plan.trailers if role == "writer" else (),
                claim=None,
                window=False,
                parent=runs.parent("flow", self.flow["id"]),
                fallback_avoid=self.fallback_avoid(task, role),
                continue_run=pending.get("continue_run"),
            )
            started = self.call(
                task,
                role,
                lambda core, options=options: core.start(options),
                fallback=True,
            )
            repeat = (task["id"], role) in self.retry
            self.retry.discard((task["id"], role))
            if not repeat:
                with self.mutex:
                    # The repeat, if any, has run; its history ends with it.
                    if (task.get("fallback_pending") or {}).get("role") == role:
                        task["fallback_pending"] = None
                    task.get("fallback_failed", {}).pop(role, None)
                    self._save()
                if started:
                    self.note_worker(task, role, worker)
                return started

    def note_worker(
        self, task: dict[str, Any], role: str, worker: Mapping[str, Any]
    ) -> None:
        """Record a start that a provider hold sent to a fallback worker."""
        run_id = task.get(f"{role}_run")
        try:
            run = runs.load(run_id, self.env) if run_id else None
        except runs.RecordError:
            return
        if run is None or (run.engine, run.model) == (
            worker["engine"],
            worker["model"],
        ):
            return
        entry = {
            "role": role,
            "from": f"{worker['engine']}:{worker['model']}",
            "to": f"{run.engine}:{run.model}",
            "cause": "held",
            "run": run.id,
            "at": time.time(),
        }
        with self.mutex:
            task.setdefault("fallbacks", []).append(entry)
            self._save()
            self._event("fallback", task=task["id"], **entry)

    def close(self, run_id: str | None) -> str | None:
        """Close without force and return any refusal for the report."""
        if not run_id:
            return None
        run = runs.load(run_id, self.env)
        if run.status == runs.CLOSED:
            return None
        stream = io.StringIO()
        core = self.core(stream, stream)
        code = core.execute(lambda: core.close(run_id))
        return stream.getvalue().strip() if code else None

    def clean_writer_tmp(self, task: Mapping[str, Any]) -> None:
        """Remove leftovers once a task with a settled Codex writer is accepted."""
        run_id = task["writer_run"]
        if run_id is None:
            return
        try:
            run = runs.load(run_id, self.env)
            if (
                run.sandbox == sandbox.CODEX
                and run.status != runs.CLOSED
                and run.turn_state in (runs.COLLECTED, runs.COMMITTED)
            ):
                keep = [name for _, name, _ in sandbox.WORKER_HOME_FOLDERS]
                paths.clean_run_tmp_dir(run_id, keep, self.env)
        except (OSError, ValueError, runs.RecordError) as error:
            print(
                f"WARNING: cannot clean writer {run_id}'s temporary folder: {error}",
                file=sys.stderr,
            )

    def send(self, task: dict[str, Any], path: str, *, fresh: bool) -> bool:
        """Persist the actual branch tip and expected turn before a correction."""
        if not self.allowed(task):
            return False
        run = self.metadata(task)
        self.change(
            task,
            status="fixing",
            entry=None,
            fix_base=self.tip(task["branch"]),
            explicit_review=False,
            round=0 if fresh else task["round"] + 1,
            turn_pending=True,
            turn_target=run.turn + 1,
            turn_file=path,
            turn_kind="send" if fresh else "fix",
            recover_review=False,
            reviewer_run=None,
        )
        if not self.call(task, "writer", lambda core: core.send(run.id, path)):
            return False
        return self.post_turn(task, False)

    def earlier(self, task: Mapping[str, Any]) -> list[verdict.Finding]:
        """Rebuild the previous review's must findings for fix briefs."""
        return [verdict.Finding(**item) for item in task.get("earlier", [])]

    def merged_siblings(self, task: Mapping[str, Any]) -> list[tuple[str, str, str]]:
        """List merged siblings whose recorded commits are absent from this head."""
        with self.mutex:
            siblings = [
                (other["id"], other["subject"], other["merge_commit"])
                for other in self.flow["tasks"]
                if other["id"] != task["id"]
                and other["status"] == "merged"
                and other.get("merge_commit")
            ]
        absent = []
        for sibling in siblings:
            result = self.git(
                "merge-base", "--is-ancestor", sibling[2], task["head"], check=False
            )
            if result.returncode == 1:
                absent.append(sibling)
            elif result.returncode:
                raise RunnerError(f"git merge-base: {result.stderr.strip()}")
        return absent

    def review(self, task: dict[str, Any], *, recovering: bool = False) -> str:
        """Read one review, close it and durably decide the next operation."""
        earlier = self.earlier(task)
        if not recovering or not task["reviewer_run"]:
            if not self.allowed(task):
                return "stop"
            writer = self.metadata(task)
            if task["round"] > 0 and not task.get("fix_base"):
                raise RunnerError(
                    "the previous reviewed head is missing; request --review"
                )
            report = (
                Path(writer.reply).read_text(errors="replace") if writer.reply else None
            )
            text = briefs.review_brief(
                self.id,
                replace(self.plan, check=tuple(self.flow["check"])),
                self.plan.task(task["id"]),
                round=task["round"] + 1,
                base=task["task_base"],
                head=task["head"],
                changed=task["changed_files"],
                outside=task["outside_files"],
                wave_files=self.other_files(task),
                writer_report=report,
                writer_check=task["writer_check"],
                earlier=earlier,
                fix_base=task.get("fix_base"),
                standing=self.standing_read(),
                merged_siblings=self.merged_siblings(task),
                corrections=[
                    (int(path.stem), path.read_text(encoding="utf-8"))
                    for path in map(Path, task["corrections"])
                ],
            )
            path = self.artifact(task, "review", text)
            self.change(
                task,
                status="reviewing",
                entry=None,
                reviewer_run=None,
                review_action=None,
                recover_review=False,
            )
            if not self.start(task, "reviewer", path):
                return "stop"
        review_run = runs.load(task["reviewer_run"], self.env)
        if review_run.turn_state == runs.SENT and review_run.status != runs.CLOSED:
            if not self.call(
                task, "reviewer", lambda core: core.recover(review_run.id)
            ):
                return "stop"
            review_run = runs.load(review_run.id, self.env)
        if review_run.last_exit not in (None, 0):
            self.change(
                task,
                status="stopped",
                reason=review_run.last_reason or "the review run failed",
            )
            return "stop"
        try:
            text = (
                Path(review_run.reply).read_text(encoding="utf-8")
                if review_run.reply
                else ""
            )
            result = verdict.read(
                text,
                len(earlier),
                outside=task["outside_files"],
                round=task["round"] + 1,
                run=review_run.id,
            )
        except (OSError, UnicodeError) as error:
            result = verdict.Review(None, (), f"cannot read the reply: {error}")
        warning = self.close(review_run.id)
        action, reason = result.decision(
            self.flow["rounds"] - task["round"],
            exhausted_reason="unresolved after "
            + verdict.budget_text(self.flow["review_budget"], fix_rounds=task["round"]),
        )
        with self.mutex:
            if self.finished or self.shutdown.is_set():
                return "stop"
            task["reviews"].append(
                {
                    "round": task["round"],
                    "review_round": task["round"] + 1,
                    "run": review_run.id,
                    "verdict": result.verdict or "unreadable",
                    "outside_files": [item.to_json() for item in result.outside_files],
                    **result.counts(),
                    "at": time.time(),
                }
            )
            task["findings"] = [
                f for f in task["findings"] if f["severity"] in ("should", "lead")
            ]
            task["findings"].extend(f.to_json() for f in result.findings)
            task["earlier"] = [
                f.to_json() for f in result.findings if f.severity == "must"
            ]
            task["reviewer_run"] = None
            if warning:
                message = (
                    f"WARNING: the review run {review_run.id} stays open: {warning}"
                )
                task.setdefault("warnings", []).append(message)
                if action == "stop":
                    reason = f"{reason}; {message}"
            # A fix decision is a recoverable scheduled send, not another review.
            task["review_action"] = action
            task["status"] = {
                "accept": "accepted",
                "stop": "stopped",
                "fix": "reviewing",
            }[action]
            task["reason"] = reason
            task["entry"] = None
            self._save()
            self._event(
                "task",
                task=task["id"],
                status=task["status"],
                reason=reason,
                warnings=task.get("warnings", []),
            )
            if action == "stop":
                self._lead(task, reason or "the review stopped")
        if action == "accept":
            self.clean_writer_tmp(task)
        return action

    def pipeline(self, task: dict[str, Any]) -> None:
        """Execute one task's write, review and bounded fix rounds."""
        try:
            self._pipeline(task)
        except Exception as error:
            self.change(
                task, status="stopped", reason=f"{type(error).__name__}: {error}"
            )

    def _pipeline(self, task: dict[str, Any]) -> None:
        """Continue at the durable operation selected by answers or recovery."""
        entry = task["entry"] or "write"
        if entry == "write":
            if not self.allowed(task, write=True):
                return
            text = briefs.writer_brief(
                self.id,
                replace(self.plan, check=tuple(self.flow["check"])),
                self.plan.task(task["id"]),
                self.other_files(task),
                standing=self.standing_read(),
                writer_check=task["writer_check"],
            )
            path = self.artifact(task, "write", text)
            self.change(
                task, status="writing", entry=None, reason=None, reviewer_run=None
            )
            if not self.start(task, "writer", path) or not self.post_turn(task, True):
                return
        elif entry == "send":
            run = self.metadata(task)
            hook_refused = (
                run.turn_state == runs.COLLECTED
                and (run.folder / HOOK_REFUSAL_NAME).exists()
            )
            text = Path(task["answer_file"]).read_text(encoding="utf-8")
            path = self.artifact(
                task,
                "fix",
                briefs.send_message(
                    self.id,
                    self.plan.task(task["id"]),
                    text,
                    check=self.flow["check"],
                    writer_check=task["writer_check"],
                ),
            )
            if not self.send(task, path, fresh=not hook_refused):
                return
        elif task.get("explicit_review"):
            # A recovered review covers only the head it started on; a moved
            # branch gets a fresh review of its new tip instead.
            saved = task.get("head")
            if not (
                task.get("recover_review")
                and saved
                and self.tip(task["branch"]) == saved
            ):
                if task.get("recover_review"):
                    self.change(
                        task,
                        recover_review=False,
                        review_action=None,
                        reviewer_run=None,
                    )
                if not self.review_writer(task):
                    return
        elif not self.reconcile_writer(task):
            return
        recovering = bool(task.get("recover_review"))
        action = task.get("review_action") if recovering else None
        while not self.shutdown.is_set():
            if action != "fix":
                action = self.review(task, recovering=recovering)
                recovering = False
            if action != "fix":
                return
            if task["id"] in self.ahead:
                # A fix is a writer turn; one decided by a review that
                # started during a wave waits for the next wave, as a
                # recovered fix decision does.
                self.change(task, status="pending", entry="review", recover_review=True)
                return
            path = self.artifact(
                task,
                "fix",
                briefs.fix_message(
                    self.id,
                    self.plan.task(task["id"]),
                    task["round"] + 1,
                    self.earlier(task),
                    check=self.flow["check"],
                    writer_check=task["writer_check"],
                ),
            )
            if not self.send(task, path, fresh=False):
                return
            self.change(task, review_action=None)
            action = None

    def review_writer(self, task: dict[str, Any]) -> bool:
        """Review saved work at the lead's request, including failed writer turns."""
        run = self.metadata(task)
        if run.turn_state in (runs.SENT, runs.COLLECTED) and not self.call(
            task, "writer", lambda core: core.recover(run.id), review_failed_turn=True
        ):
            return False
        # A fresh review covers the current branch against the original task base.
        return self.post_turn(task, True)

    def reconcile_writer(self, task: dict[str, Any]) -> bool:
        """Reconcile a captured turn before reviewing or sending another message."""
        run = self.metadata(task)
        if run.turn == 0 or run.turn_state is None:
            self.change(
                task,
                status="stopped",
                reason="the writer was not launched; choose --worker to replace it",
            )
            return False
        pending = task.get("turn_pending", False)
        if pending and run.turn < task["turn_target"]:
            if not self.allowed(task):
                return False
            self.change(task, fix_base=self.tip(task["branch"]))
            if not self.call(
                task, "writer", lambda core: core.send(run.id, task["turn_file"])
            ):
                return False
        elif run.turn_state in (runs.SENT, runs.COLLECTED):
            if not self.call(task, "writer", lambda core: core.recover(run.id)):
                return False
        elif run.last_exit not in (None, 0):
            self.change(
                task,
                status="stopped",
                reason=run.last_reason or "the writer turn failed",
            )
            return False
        if pending or task.get("validate_turn") or not task["head"]:
            initial = not pending and not task.get("fix_base")
            if not self.post_turn(task, initial):
                return False
            self.change(task, validate_turn=False, review_action=None)
        return True

    def clean(self) -> None:
        """Restore the runner-owned flow worktree before further merges."""
        self.git("reset", "-q", "--hard")
        self.git("clean", "-q", "-f", "-d")

    def merge_commit(self, tip: str) -> str | None:
        """Find the squash commit recording the approved worker tip."""
        return branches.landed_commit(
            self.flow["worktree"], tip, self.flow["branch"], self.env
        )

    def holds(self, tip: str) -> bool:
        """Return whether the flow branch already holds an approved worker tip."""
        return branches.is_contained(
            self.flow["worktree"], tip, self.flow["branch"], self.env
        )

    def reconcile(self) -> None:
        """Record as merged each conflicted task whose tip the lead merged by hand.

        A merge conflict leaves the task `conflict`, and its `next:` line has
        the lead merge the approved tip into the flow branch. Once the flow
        branch holds that tip, the task is merged: its commit is the squash
        commit that records the tip, else the first flow commit that holds it.
        """
        for task in self.flow["tasks"]:
            tip = task.get("merge_tip")
            if task["status"] != "conflict" or not tip or not self.holds(tip):
                continue
            commit = self.merge_commit(tip) or self.holding_commit(tip)
            self.merged(task, {"merge_commit": commit, "cleanup": []})

    def holding_commit(self, tip: str) -> str:
        """Return the oldest flow branch commit that holds `tip`.

        Every commit tested is on the flow branch, so the result is reachable
        whether the hand merge kept `tip` as an ancestor or squashed it.
        """
        branch = self.flow["branch"]
        base = self.git("merge-base", tip, branch).stdout.strip()
        commits = self.git(
            "rev-list", "--reverse", f"{base}..{branch}" if base else branch
        ).stdout.split()
        for commit in commits:
            if branches.is_contained(self.flow["worktree"], tip, commit, self.env):
                return commit
        return self.tip(branch)

    def merged(self, task: dict[str, Any], result: Mapping[str, Any]) -> None:
        """Record containment and a pending check even after cleanup fails."""
        tip = self.tip(self.flow["branch"])
        with self.mutex:
            task.update(
                status="merged",
                entry=None,
                reason=None,
                merge_commit=result.get("merge_commit"),
                contained_before=result.get("contained_before", False),
                cleanup=result.get("cleanup", []),
            )
            if task["cleanup"] and all(
                r["action"] == "removed" for r in task["cleanup"]
            ):
                task.update(branch=None, worktree=None)
            if result.get("warnings"):
                task.setdefault("warnings", []).extend(result["warnings"])
            self.flow.update(tip=tip, check_pending=True)
            self._save()
            self._event(
                "task",
                task=task["id"],
                status="merged",
                reason=None,
                cleanup=task["cleanup"],
                warnings=task.get("warnings", []),
            )

    def merge(self, task: dict[str, Any]) -> bool:
        """Merge only the approved head through the shared branch implementation."""
        approved = task["head"]
        tip = self.tip(task["branch"])
        if tip != approved:
            self.change(
                task,
                status="stopped",
                reason=f"the branch moved from {approved} to {tip}",
            )
            return True
        self.change(task, status="merging", merge_tip=approved)
        out, err = io.StringIO(), io.StringIO()
        code = branches.merge(
            task["branch"],
            self.flow["branch"],
            "keep",
            subject=task["subject"],
            note=task["reviews"][-1]["verdict"] if task["reviews"] else "keep",
            directory=self.flow["worktree"],
            as_json=True,
            env=self.env,
            core=lambda stream: self.core(stream, stream),
            out=out,
            err=err,
        )
        result = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        contained = branches.is_contained(
            self.flow["worktree"], approved, self.flow["branch"], self.env
        )
        if code == 0 or contained:
            if not result:
                result = {"merge_commit": self.merge_commit(approved), "cleanup": []}
            if code:
                self.change(task, cleanup_error=err.getvalue().strip())
            self.merged(task, result)
            return True
        reason = err.getvalue().strip() or f"merge exited {code}"
        if code == 75:
            reason += (
                f"; once {approved} is in {self.flow['branch']}, the flow records "
                f"{task['id']} as merged: emb flow --resume {self.id}, or "
                f"emb flow --lead {self.id} {task['id']} --accept"
            )
        self.change(task, status="conflict" if code == 75 else "stopped", reason=reason)
        if code == 75:
            return True
        self.failure = f"merge of {task['id']} failed: {reason}"
        return False

    def recover(self) -> None:
        """Clean and prepare the flow, recover runs, then answer and retry children."""
        _kill_check(self.flow.get("active_check"))
        with self.mutex:
            self.flow["active_check"] = None
            self._save()
        merge_head = self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
        if merge_head.returncode == 0:
            if not any(
                t["status"] == "merging" and t["merge_tip"] == merge_head.stdout.strip()
                for t in self.flow["tasks"]
            ):
                raise RunnerError(
                    "the flow worktree has an unrecorded merge in progress"
                )
            self.git("merge", "--abort")
        self.clean()
        if not self.preparation_recovered and self.flow.get("preparation"):
            prepare_worktree(self.flow, self.env, self.shutdown)
        for task in self.flow["tasks"]:
            try:
                pending = task.get("fallback_pending") or {}
                role = pending.get("role")
                if role and task.get(f"{role}_run") == pending.get("failed_run"):
                    # The runner stopped between a failed run and its repeat
                    # on the fallback worker; the pipeline makes the start.
                    self.change(
                        task,
                        status="pending",
                        entry=None if role == "writer" else "review",
                        reason=None,
                        **{f"{role}_run": None},
                    )
                    continue
                if pending or task.get("fallback_failed"):
                    # The repeat started before the runner stopped.
                    self.change(task, fallback_pending=None, fallback_failed={})
                if task["status"] == "merging":
                    if branches.is_contained(
                        self.flow["worktree"],
                        task["merge_tip"],
                        self.flow["branch"],
                        self.env,
                    ):
                        commit = self.merge_commit(task["merge_tip"])
                        out, err = io.StringIO(), io.StringIO()
                        if (
                            self.git(
                                "rev-parse",
                                "--verify",
                                f"refs/heads/{task['branch']}",
                                check=False,
                            ).returncode
                            == 0
                        ):
                            code = branches.cleanup(
                                self.flow["branch"],
                                branch=task["branch"],
                                directory=self.flow["worktree"],
                                as_json=True,
                                env=self.env,
                                core=lambda stream: self.core(stream, stream),
                                out=out,
                                err=err,
                            )
                            if code:
                                self.change(task, cleanup_error=err.getvalue().strip())
                        self.merged(
                            task,
                            {
                                "merge_commit": commit,
                                "contained_before": commit is None,
                                "cleanup": json.loads(out.getvalue() or "[]"),
                            },
                        )
                    else:
                        self.change(task, status="accepted", head=task["merge_tip"])
                        self.clean_writer_tmp(task)
                elif task["status"] in ("writing", "fixing"):
                    if not task["writer_run"]:
                        self.change(task, status="pending", entry="write")
                    else:
                        self.change(
                            task, status="pending", entry="review", validate_turn=True
                        )
                elif task["status"] == "reviewing":
                    self.change(
                        task, status="pending", entry="review", recover_review=True
                    )
                self.answer_effect(task)
            except Exception as error:
                self.change(task, status="stopped", reason=f"recovery failed: {error}")
        if not self.preparation_recovered:
            answered = self.answers()
            self.retry_preparation_children(answered)
            self.preparation_recovered = True

    def retry_preparation_children(self, answered: set[str]) -> None:
        """Retry unanswered preparation children without discarding writer commits."""
        for task in self.flow["tasks"]:
            if task["id"] in answered or task["status"] in (
                "accepted",
                "merged",
                "dropped",
            ):
                continue
            role = task.get("preparation_blocked")
            if not role:
                if "preparation_blocked" in task:
                    continue
                # The runner may have died between Core's failure and call's save.
                for candidate in ("writer", "reviewer"):
                    run_id = task[f"{candidate}_run"]
                    if not run_id:
                        continue
                    run = runs.load(run_id, self.env)
                    unsuccessful = (run.preparation or {}).get("state") in (
                        "failed",
                        "timeout",
                        "interrupted",
                        "not-started",
                    )
                    if run.turn == 0 and (
                        run.last_exit == exits.PREPARATION_FAILED or unsuccessful
                    ):
                        role = candidate
                        break
            if not role:
                continue
            if role in ("writer", "reviewer"):
                run_id = task[f"{role}_run"]
                if run_id:
                    run = runs.load(run_id, self.env)
                    if run.turn != 0:
                        raise RunnerError(
                            f"{run_id}: preparation-blocked run has worker work"
                        )
                    with self.mutex:
                        previous = {"role": role, "run": run_id}
                        history = task.setdefault("preparation_runs", [])
                        if previous not in history:
                            history.append(previous)
                        self._save()
                    warning = self.close(run_id)
                    if warning:
                        raise RunnerError(warning)
            fields: dict[str, Any] = {
                "status": "pending",
                "reason": None,
                "entry": "write"
                if role == "writer"
                else "send"
                if role == "send"
                else "review",
                "preparation_blocked": None,
                "recover_review": role == "continue",
            }
            if role == "writer":
                fields.update(
                    writer_run=None,
                    branch=None,
                    worktree=None,
                    task_base=None,
                    head=None,
                )
            elif role == "reviewer":
                fields.update(reviewer_run=None, review_action=None)
            self.change(task, **fields)
        with self.mutex:
            if self.flow.get("preparation_halt"):
                if self.flow["halted"] == self.flow["preparation_halt"]:
                    self.flow["halted"] = None
                self.flow["preparation_halt"] = None
                self._save()

    @contextlib.contextmanager
    def check_tmp_dir(self) -> Iterator[Path]:
        """Give one check attempt a fresh folder for the lifetime of its process."""
        try:
            folder = paths.ensure_check_tmp_dir(self.id)
        except OSError as error:
            raise RunnerError(
                f"cannot create check folder {paths.check_tmp_dir(self.id)}: {error}"
            ) from error
        try:
            yield folder
        finally:
            try:
                paths.remove_check_tmp_dir(self.id)
            except OSError as error:
                print(
                    f"WARNING: cannot remove check folder {folder}: {error}",
                    file=sys.stderr,
                )

    def check(self) -> bool:
        """Run a check attempt, retain its log and clean the flow worktree."""
        with self.mutex:
            self.flow["status"] = "checking"
            self._save()
        if not self.flow["check"]:
            with self.mutex:
                self.flow.update(status="running", check_pending=False)
                self._save()
            return True
        number = self.flow.get("check_attempt", 0) + 1
        log = self.folder / f"check-{self.flow['wave']}-{number}.log"
        with self.mutex:
            self.flow["check_attempt"] = number
            self._save()
        fd = paths.open_private_file(log, os.O_WRONLY | os.O_EXCL)
        code = 127
        reason = None
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            try:
                with self.check_tmp_dir() as temporary:
                    process = subprocess.Popen(
                        self.flow["check"],
                        cwd=self.flow["worktree"],
                        env={**self.env, "TMPDIR": str(temporary)},
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        with self.mutex:
                            self.flow["active_check"] = {
                                "pid": process.pid,
                                "start": _process_start(process.pid),
                            }
                            self._save()
                        deadline = time.monotonic() + self.flow["check_timeout"]
                        while process.poll() is None:
                            if self.shutdown.is_set() or time.monotonic() >= deadline:
                                code = 124
                                break
                            self.shutdown.wait(
                                min(self.poll, max(0, deadline - time.monotonic()))
                            )
                        else:
                            code = process.returncode
                    finally:
                        if process.returncode is None:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
            except OSError as error:
                output.write(str(error))
            except RunnerError as error:
                reason = str(error)
                output.write(reason)
        try:
            self.clean()
        except RunnerError as error:
            reason = f"check cleanup failed: {error}"
        tail = log.read_text(errors="replace")[-OUTPUT_LIMIT:]
        with self.mutex:
            self.flow["active_check"] = None
            self.flow["checks"].append(
                {
                    "wave": self.flow["wave"],
                    "exit": code,
                    "log": str(log),
                    "at": time.time(),
                }
            )
            self._event(
                "check", wave=self.flow["wave"], exit=code, log=str(log), output=tail
            )
            if code == 0 and reason is None and not self.shutdown.is_set():
                self.flow.update(status="running", check_pending=False)
            self._save()
        if self.shutdown.is_set():
            return False
        if code or reason:
            where = (
                "on the base"
                if self.flow["wave"] == 0
                else f"after wave {self.flow['wave']}"
            )
            self.failure = reason or (
                f"the check failed {where}: exit {code}, see {log}"
            )
            return False
        return True

    def answer_effect(self, task: dict[str, Any]) -> None:
        """Complete durable closes for an answer and its blocked reviewer."""
        effect = task.get("answer_effect")
        if not effect:
            return
        warnings = [
            warning
            for run_id in (effect.get("preparation_run"), effect["run"])
            if (warning := self.close(run_id))
        ]
        with self.mutex:
            for warning in warnings:
                task.setdefault("warnings", []).append(warning)
                self._event(
                    "task", task=task["id"], status=task["status"], reason=warning
                )
            task["answer_effect"] = None
            self._save()

    def answers(self) -> set[str]:
        """Apply every unapplied answer in effect and move the cursor to the end.

        Each answer is applied with its consumption marker and preparation
        decision. A line the runner took ahead of the cursor, a line that a
        later line for the same task replaced, and a `withdraw` line are
        passed without effect.

        Returns:
            Tasks with applied answers, including those whose answers were
            refused.
        """
        answered: set[str] = set()
        lines = record.read_answers(self.id, self.env)
        queue = {index for index, _, _ in record.answer_queue(self.flow, lines)}
        for index in range(self.flow["answers_seen"], len(lines)):
            if lines[index] is None or index in queue:
                self._apply_answer(index, lines[index], answered, ahead=False)
                continue
            with self.mutex:
                self._consume(index, False)
                self._save()
        return answered

    def answers_ahead(self, running: Iterable[str], free: int) -> list[dict[str, Any]]:
        """Apply queued answers that need no wave boundary, ahead of the cursor.

        A `reviewer` answer changes the flow's saved reviewer. A `review`
        answer is applied when a job slot is free, new runs are not halted,
        the task has no running job and its declared files overlap no
        running task's files. Every other answer stays queued for the next
        pass of `answers`.

        Args:
            running: The ids of the tasks whose jobs are running.
            free: The number of free job slots.

        Returns:
            The tasks whose reviews are now scheduled, for the caller to run.
        """
        scheduled: list[dict[str, Any]] = []
        active = set(running)
        occupied = [path for name in active for path in self.plan.task(name).files]
        lines = record.read_answers(self.id, self.env)
        for index, answer, _ in record.answer_queue(self.flow, lines):
            kind = answer.get("kind")
            if kind == "reviewer":
                self._apply_answer(index, answer, set(), ahead=True)
                continue
            # An applied answer lifts a halt; one queued before a halt of
            # the running wave waits for the wave's end to do so.
            if kind != "review" or len(scheduled) >= free or self.flow["halted"]:
                continue
            try:
                task = record.task_entry(self.flow, answer["task"])
                planned = self.plan.task(task["id"])
            except KeyError, TypeError, plan.PlanError:
                continue
            if (
                task["id"] in active
                or "review" not in record.allowed_answers(task)
                or not plan.pick_wave([planned], occupied)
            ):
                continue
            self._apply_answer(index, answer, set(), ahead=True)
            if task["status"] == "pending" and task["entry"] == "review":
                scheduled.append(task)
                active.add(task["id"])
                occupied.extend(planned.files)
        return scheduled

    def unapplied(self) -> bool:
        """Return whether the answer log holds a line no pass has consumed."""
        taken = set(self.flow.get("answers_taken", []))
        count = len(record.read_answers(self.id, self.env))
        return any(
            index not in taken for index in range(self.flow["answers_seen"], count)
        )

    def _consume(self, index: int, ahead: bool) -> None:
        """Mark an answer line consumed while the caller holds the record mutex."""
        taken = [item for item in self.flow.get("answers_taken", []) if item != index]
        if ahead:
            taken.append(index)
        else:
            self.flow["answers_seen"] = index + 1
            taken = [item for item in taken if item > index]
        self.flow["answers_taken"] = taken

    def _apply_answer(
        self,
        index: int,
        answer: Mapping[str, Any] | None,
        answered: set[str],
        *,
        ahead: bool,
    ) -> None:
        """Apply one answer line with its consumption marker.

        The answers lock is held from the check that the line is still in
        effect through the save of its marker, so a command cannot replace
        or withdraw the line in between.

        Args:
            index: The line's index in the answer log.
            answer: The line, or `None` for an unreadable one.
            answered: Receives the answer's task.
            ahead: Mark the line taken ahead of the cursor, not the cursor.
        """
        with record.answers_lock(self.id, self.env):
            task = self._apply_locked(index, answer, answered, ahead=ahead)
        if task is not None:
            if task["status"] == "accepted":
                self.clean_writer_tmp(task)
            self.answer_effect(task)

    def _apply_locked(
        self,
        index: int,
        answer: Mapping[str, Any] | None,
        answered: set[str],
        *,
        ahead: bool,
    ) -> dict[str, Any] | None:
        """Apply an answer under the answers lock; return its task when applied."""
        task = None
        problem = None
        fields: dict[str, Any] = {}
        try:
            if answer is None:
                raise RunnerError(f"unreadable answer line {index + 1}")
            lines = record.read_answers(self.id, self.env)
            if all(index != item[0] for item in record.answer_queue(self.flow, lines)):
                # Replaced or withdrawn since the caller read the queue.
                if not ahead:
                    with self.mutex:
                        self._consume(index, False)
                        self._save()
                return None
            if answer["kind"] == "reviewer":
                worker = self.freeze(
                    plan.parse_worker(answer["worker"], "answer").to_json()
                )
                with self.mutex:
                    self.flow["reviewer"] = worker
                    self._consume(index, ahead)
                    self._save()
                    self._event("reviewer", answer=answer["n"], worker=worker)
                return None
            task = record.task_entry(self.flow, answer["task"])
            answered.add(task["id"])
            kind = answer["kind"]
            if (
                kind == "accept"
                and task["status"] == "merged"
                and answer.get("commit") == task.get("merge_tip")
            ):
                # Reconciliation already recorded this hand-merged tip.
                with self.mutex:
                    self._consume(index, ahead)
                    self._save()
                return None
            if kind not in record.allowed_answers(task):
                raise RunnerError(f"task {task['id']} is {task['status']}")
            fields = {
                "answer": answer["n"],
                "reason": None,
                "entry": None,
                "explicit_review": kind == "review",
            }
            if kind == "accept" and task["status"] == "conflict":
                if answer["commit"] != task.get("merge_tip"):
                    raise RunnerError(
                        f"{answer['commit']} is not the approved tip "
                        f"{task.get('merge_tip')}"
                    )
                if not self.holds(answer["commit"]):
                    raise RunnerError(
                        f"{answer['commit']} is not in {self.flow['branch']}"
                    )
                fields.update(status="conflict")
            elif kind == "accept":
                tip = self.tip(task["branch"])
                if tip != answer["commit"]:
                    raise RunnerError(
                        f"the branch moved from {answer['commit']} to {tip}"
                    )
                fields.update(status="accepted", head=tip)
            elif kind in ("send", "review", "worker"):
                # A lead answer starts a new attempt with a fresh history.
                fields.update(fallback_pending=None, fallback_failed={})
                hook_refused = False
                if kind == "send" and task["writer_run"]:
                    run = runs.load(task["writer_run"], self.env)
                    hook_refused = (
                        run.turn_state == runs.COLLECTED
                        and (run.folder / HOOK_REFUSAL_NAME).exists()
                    )
                fields.update(
                    status="pending",
                    entry="write" if kind == "worker" else kind,
                    round=task["round"] if hook_refused else 0,
                    review_action=None,
                    recover_review=False,
                )
                if kind == "send":
                    fields["answer_file"] = answer["file"]
                    fields["corrections"] = [*task["corrections"], answer["file"]]
                if kind in ("review", "worker") and answer.get("worker"):
                    role = "reviewer" if kind == "review" else "writer"
                    fields[role] = self.freeze(
                        plan.parse_worker(answer["worker"], "answer").to_json()
                    )
                if kind == "review":
                    run = runs.load(task["writer_run"], self.env)
                    active = run.turn_state in (runs.SENT, runs.COLLECTED)
                    fields.update(
                        reviewer_run=None,
                        validate_turn=active,
                        turn_pending=active and task.get("turn_pending", False),
                        earlier=[],
                    )
                    if not active:
                        fields["fix_base"] = None
            else:
                fields.update(status="dropped", reason="dropped by the lead")
            if kind in ("worker", "drop"):
                fields["answer_effect"] = {
                    "n": answer["n"],
                    "run": task["writer_run"],
                }
                if kind == "worker":
                    fields.update(
                        writer_run=None,
                        branch=None,
                        worktree=None,
                        head=None,
                        task_base=None,
                        reviewer_run=None,
                        fix_base=None,
                        turn_pending=False,
                        validate_turn=False,
                        earlier=[],
                    )
            if "preparation_blocked" in task:
                fields["preparation_blocked"] = None
                if task["preparation_blocked"] == "reviewer" and task["reviewer_run"]:
                    previous = {"role": "reviewer", "run": task["reviewer_run"]}
                    fields["preparation_runs"] = [
                        *task.get("preparation_runs", []),
                        previous,
                    ]
                    fields["reviewer_run"] = None
                    fields.setdefault("answer_effect", {"n": answer["n"], "run": None})[
                        "preparation_run"
                    ] = task["reviewer_run"]
        except (RunnerError, KeyError, TypeError, plan.PlanError) as error:
            number = answer.get("n", index + 1) if answer else index + 1
            problem = f"answer {number} refused: {error}"
        with self.mutex:
            if problem:
                self._lead(task, problem)
            elif task is not None and answer is not None:
                if (
                    answer["kind"] in ("worker", "drop")
                    and task["branch"]
                    and task["branch"] not in self.flow["abandoned"]
                ):
                    self.flow["abandoned"].append(task["branch"])
                task.update(fields)
                if not self.flow.get("preparation_halt"):
                    self.flow["halted"] = None
            self._consume(index, ahead)
            self._save()
            if not problem and task is not None:
                self._event(
                    "task",
                    task=task["id"],
                    status=task["status"],
                    reason=task["reason"],
                )
        return task if not problem else None

    def dependencies(self) -> None:
        """Propagate dependency blocking and unblocking to a fixed point."""
        changed = True
        while changed:
            changed = False
            for task in self.flow["tasks"]:
                if task["status"] not in ("pending", "blocked") or task["entry"]:
                    continue
                needs = [
                    record.task_entry(self.flow, name)
                    for name in self.plan.task(task["id"]).after
                ]
                blocker = next(
                    (
                        t
                        for t in needs
                        if t["status"] in ("stopped", "conflict", "blocked", "dropped")
                    ),
                    None,
                )
                if blocker and task["status"] == "pending":
                    self.change(
                        task,
                        status="blocked",
                        reason=f"needs {blocker['id']}, which is {blocker['status']}",
                    )
                    changed = True
                elif (
                    not blocker
                    and task["status"] == "blocked"
                    and all(t["status"] == "merged" for t in needs)
                ):
                    self.change(task, status="pending", reason=None)
                    changed = True

    def wave(self) -> list[dict[str, Any]]:
        """Select compatible pending entries in plan order."""
        candidates = [
            self.plan.task(t["id"])
            for t in self.flow["tasks"]
            if t["status"] == "pending"
            and (
                t["entry"]
                or all(
                    record.task_entry(self.flow, dep)["status"] == "merged"
                    for dep in self.plan.task(t["id"]).after
                )
            )
        ]
        return [record.task_entry(self.flow, t.id) for t in plan.pick_wave(candidates)]

    def jobs(self, wave: list[dict[str, Any]]) -> None:
        """Wait for a whole wave, allowing main to observe handled signals."""
        with self.mutex:
            # Each job batch increments the runner pass count.
            self.flow["wave"] += 1
            for task in wave:
                # Recovery retains the declarations of the interrupted wave.
                if task["entry"] != "review":
                    task["wave"] = self.flow["wave"]
                    task["wave_declarations"] = {
                        other["id"]: list(self.plan.task(other["id"]).files)
                        for other in wave
                        if other is not task
                    }
            self._save()
        executor = ThreadPoolExecutor(max_workers=self.flow["parallel"])
        running = {executor.submit(self.pipeline, task): task for task in wave}
        futures = list(running)
        self.ahead = set()
        try:
            pending = set(futures)
            while pending and not self.shutdown.is_set():
                _, pending = wait(pending, timeout=self.poll)
                if not pending or self.shutdown.is_set():
                    break
                # A review answer does not wait for the wave to end.
                for task in self.answers_ahead(
                    [running[future]["id"] for future in pending],
                    self.flow["parallel"] - len(pending),
                ):
                    self.ahead.add(task["id"])
                    future = executor.submit(self.pipeline, task)
                    running[future] = task
                    futures.append(future)
                    pending.add(future)
            if not self.shutdown.is_set():
                for future in futures:
                    future.result()
        finally:
            executor.shutdown(wait=not self.shutdown.is_set(), cancel_futures=True)
            self.ahead = set()

    def begin(self) -> None:
        """Initialize one runner attempt before recovery and scheduling."""
        with self.mutex:
            self.flow.update(
                runner={
                    "pid": os.getpid(),
                    "start": _process_start(os.getpid()),
                    "started_at": time.time(),
                    "exiting": False,
                },
                status="running",
                reason=None,
                ended_at=None,
                halted=self.flow["halted"]
                if self.flow.get("preparation_halt")
                else None,
                attempt=self.flow.get("attempt", 0) + 1,
            )
            self.flow["tip"] = self.tip(self.flow["branch"])
            for role in ("writer", "reviewer"):
                self.flow[role] = self.freeze(self.flow[role])
                for task in self.flow["tasks"]:
                    if task[role]:
                        task[role] = self.freeze(task[role])
            self._save()
            self._event("flow", status="running", reason=None)

    def drive(self) -> tuple[str, str | None]:
        """Recover and run waves until an outcome needs the final answer check."""
        if self.shutdown.is_set():
            return "failed", None
        self.recover()
        self.reconcile()
        if self.flow["check_pending"] and not self.check():
            return "failed", self.failure
        while not self.shutdown.is_set():
            self.answers()
            self.reconcile()
            self.dependencies()
            for task in self.flow["tasks"]:
                if task["status"] == "accepted" and not self.merge(task):
                    return "failed", self.failure
            if self.flow["check_pending"] and not self.check():
                return "failed", self.failure
            self.dependencies()
            if self.flow["halted"]:
                return (
                    "failed" if self.flow.get("preparation_halt") else "stopped",
                    self.flow["halted"],
                )
            wave = self.wave()
            if wave:
                self.jobs(wave)
                continue
            stopped = [
                t["id"]
                for t in self.flow["tasks"]
                if t["status"] in ("stopped", "conflict")
            ]
            if stopped:
                return "stopped", "waiting for the lead: " + ", ".join(stopped)
            merged = sum(t["status"] == "merged" for t in self.flow["tasks"])
            total = len(self.flow["tasks"])
            return (
                "done",
                None if merged == total else f"{merged} of {total} tasks merged",
            )
        return "failed", None

    def execute(self, closing: contextlib.ExitStack) -> int:
        """Guard every attempt outcome and continue when the lead queued an answer."""
        begun = False
        continuing = False
        while True:
            code = 0
            try:
                if not begun:
                    begun = True
                    with contextlib.ExitStack() as startup:
                        startup.enter_context(_lifecycle_guard(self.folder))
                        self.flow = record.load(self.id, self.env)
                        if record.cancel_request(self.id, self.env) is not None:
                            closing.enter_context(startup.pop_all())
                            self.finish("cancelled")
                            return 0
                        preparation_known(self.flow, self.env)
                        self.begin()
                if continuing:
                    self.answers()
                status, reason = self.drive()
            except PreparationFailedError as error:
                status, reason, code = "failed", str(error), exits.PREPARATION_FAILED
                print(
                    f"emb flow: {error}\nnext: emb flow --resume {self.id}",
                    file=sys.stderr,
                )
            except Exception as error:
                status, reason, code = "failed", f"{type(error).__name__}: {error}", 1
                print(
                    f"emb flow: {error}\nnext: emb flow --status {self.id}",
                    file=sys.stderr,
                )
            if self.shutdown.is_set():
                with self.preparing:
                    self.preparing.wait_for(lambda: self.preparing_count == 0)
                _kill_check(self.flow.get("active_check"))
                status = "failed"
                if self.flow.get("preparation_halt"):
                    reason = self.flow["preparation_halt"]
                elif record.preparation_failed(self.flow):
                    saved = self.flow["preparation"]
                    reason = f"{saved['message']}; log: {saved['log']}"
                else:
                    reason = (
                        "the runner was stopped by a signal; "
                        f"emb flow --resume {self.id} goes on"
                    )
            if self.flow.get("preparation_halt"):
                code = exits.PREPARATION_FAILED
            with contextlib.ExitStack() as lifecycle:
                lifecycle.enter_context(_lifecycle_guard(self.folder))
                if (
                    not self.shutdown.is_set()
                    and not self.flow.get("preparation_halt")
                    and code != exits.PREPARATION_FAILED
                    and record.cancel_request(self.id, self.env) is None
                    and self.unapplied()
                ):
                    continuing = True
                    continue
                # Keep the guard until the caller releases the lifetime lock.
                closing.enter_context(lifecycle.pop_all())
                if status == "stopped" and self.flow["halted"]:
                    with self.mutex:
                        self._lead(None, self.flow["halted"])
                self.finish(status, reason)
                return code


def _run(
    flow_id: str,
    env: Mapping[str, str],
    core_factory: Callable[..., Core],
    poll_seconds: float,
    shutdown: threading.Event,
) -> int:
    """Release the lifetime lock before closing the final lifecycle guard."""
    try:
        with contextlib.ExitStack() as closing, lock.hold(flow_id, env):
            try:
                runner = _Runner(flow_id, env, core_factory, poll_seconds, shutdown)
            except Exception as error:
                # No attempt can start without a readable flow and stored plan.
                print(
                    f"emb flow: {error}\nnext: emb flow --status {flow_id}",
                    file=sys.stderr,
                )
                return 1
            return runner.execute(closing)
    except lock.Busy:
        print(
            f"emb flow: another runner works on {flow_id}\n"
            f"next: emb flow --watch {flow_id} --json",
            file=sys.stderr,
        )
        return 75
    except lock.LockError as error:
        print(f"emb flow: {error}\nnext: emb flow --status {flow_id}", file=sys.stderr)
        return 1


def run(
    flow_id: str,
    env: Mapping[str, str] | None = None,
    *,
    core_factory: Callable[..., Core] = Core,
    poll_seconds: float | None = None,
    out: TextIO | None = None,
) -> int:
    """Run a flow in this process, returning 75 for a second runner."""
    return _run(
        flow_id,
        os.environ if env is None else env,
        core_factory,
        POLL_SECONDS if poll_seconds is None else poll_seconds,
        threading.Event(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Handle termination requests and remove this runner's session last."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not record.is_flow_id(args[0]):
        print("emb flow: expected one flow id\nnext: emb flow --help", file=sys.stderr)
        return 64
    flow_id = args[0]
    shutdown = threading.Event()

    def stop(signum: int, frame: object) -> None:
        """Request shutdown without taking a record lock or writing files."""
        shutdown.set()

    previous = {
        sig: signal.signal(sig, stop)
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    }
    try:
        code = _run(flow_id, os.environ, Core, POLL_SECONDS, shutdown)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if os.environ.get("EMBER_FLOW_RUNNER") == flow_id and code != 75:
        with contextlib.suppress(tmux.TmuxError, paths.RuntimeDirError):
            tmux.kill(flow_id, env=os.environ)
    if shutdown.is_set():
        os._exit(code if code == exits.PREPARATION_FAILED else 1)
    return code


if __name__ == "__main__":
    sys.exit(main())
