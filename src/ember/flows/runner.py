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
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, TextIO

from ember import branches, paths, tmux
from ember.engines import Driver
from ember.flows import briefs, lock, plan, record, verdict
from ember.runs import record as runs
from ember.runs.core import Core, StartOptions

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
        )
        self.factory = core_factory
        self.poll = poll_seconds
        self.shutdown = shutdown
        self.mutex = threading.Lock()
        self.finished = False
        self.failure: str | None = None

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

    def change(self, task: dict[str, Any], **fields: Any) -> None:  # noqa: ANN401
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
                    self._lead(task, task["reason"])

    def _lead(self, task: dict[str, Any] | None, reason: str) -> None:
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
        )

    def finish(self, status: str, reason: str | None = None) -> None:
        """Record the terminal outcome outside signal handlers."""
        with self.mutex:
            self.flow.update(status=status, reason=reason)
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
                    exit=0 if status == "done" and complete else 82,
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
        ) -> tuple[Driver, str, str | None]:
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
        if write:
            self.change(task, status="pending", entry="write")
        else:
            self.change(task, status="stopped", reason=f"new runs halted: {halted}")
        return False

    def call(
        self,
        task: dict[str, Any],
        role: str,
        action: Callable[[Core], int],
        *,
        review_failed_turn: bool = False,
    ) -> bool:
        """Run one Core action and preserve its diagnostics on a failure."""

        def capture(run_id: str) -> None:
            """Persist the run before its launch finishes."""
            self.change(task, **{f"{role}_run": run_id})

        out, err = _RunIdStream(capture), io.StringIO()
        core = self.core(out, err)
        code = core.execute(lambda: action(core))
        if code == 0:
            return not self.shutdown.is_set()
        run_id = task[f"{role}_run"]
        if review_failed_turn and run_id:
            recovered = runs.load(run_id, self.env)
            if recovered.turn_state == runs.COMMITTED:
                return not self.shutdown.is_set()
        reason = "; ".join(line for line in err.getvalue().splitlines() if line.strip())
        if code == 124:
            reason = f"stalled; the turn keeps running in {run_id}"
        if code == 73:
            with self.mutex:
                engine = self.worker(task, role)["engine"]
                self.flow["halted"] = f"provider error on {engine}; new runs halted"
                self._save()
        if role == "writer" and run_id:
            self.metadata(task)
        self.change(task, status="stopped", reason=reason or f"the run exited {code}")
        return False

    def metadata(self, task: dict[str, Any]) -> runs.Run:
        """Restore writer metadata from its durable run record."""
        run = runs.load(task["writer_run"], self.env)
        self.change(task, branch=run.branch, worktree=run.worktree, task_base=run.base)
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
        """Start an editing writer or a fresh read-only reviewer."""
        if not self.allowed(task, write=role == "writer"):
            return False
        worker = self.worker(task, role)
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
        )
        return self.call(task, role, lambda core: core.start(options))

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
                self.plan,
                self.plan.task(task["id"]),
                round=task["round"] + 1,
                base=task["task_base"],
                head=task["head"],
                changed=task["changed_files"],
                outside=task["outside_files"],
                wave_files=self.other_files(task),
                writer_report=report,
                earlier=earlier,
                fix_base=task.get("fix_base"),
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
            result = verdict.read(text, len(earlier))
        except (OSError, UnicodeError) as error:
            result = verdict.Review(None, (), f"cannot read the reply: {error}")
        warning = self.close(review_run.id)
        action, reason = result.decision(self.flow["rounds"] - task["round"])
        if reason:
            reason = reason.replace("<n>", str(task["round"]))
        with self.mutex:
            if self.finished or self.shutdown.is_set():
                return "stop"
            task["reviews"].append(
                {
                    "round": task["round"],
                    "run": review_run.id,
                    "verdict": result.verdict or "unreadable",
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
                self.id, self.plan, self.plan.task(task["id"]), self.other_files(task)
            )
            path = self.artifact(task, "write", text)
            self.change(
                task, status="writing", entry=None, reason=None, reviewer_run=None
            )
            if not self.start(task, "writer", path) or not self.post_turn(task, True):
                return
        elif entry == "send":
            text = Path(task["answer_file"]).read_text(encoding="utf-8")
            path = self.artifact(
                task,
                "fix",
                briefs.send_message(self.id, self.plan.task(task["id"]), text),
            )
            if not self.send(task, path, fresh=True):
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
            path = self.artifact(
                task,
                "fix",
                briefs.fix_message(
                    self.id,
                    self.plan.task(task["id"]),
                    task["round"] + 1,
                    self.earlier(task),
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
        """Find the first-parent merge that names the task tip as a second parent."""
        lines = self.git(
            "rev-list", "--first-parent", "--parents", self.flow["branch"]
        ).stdout.splitlines()
        return next(
            (
                parts[0]
                for line in reversed(lines)
                if len(parts := line.split()) > 2 and tip in parts[2:]
            ),
            None,
        )

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
            self.flow.update(tip=tip, check_pending=True)
            self._save()
            self._event(
                "task",
                task=task["id"],
                status="merged",
                reason=None,
                cleanup=task["cleanup"],
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
        self.change(task, status="conflict" if code == 75 else "stopped", reason=reason)
        if code == 75:
            return True
        self.failure = f"merge of {task['id']} failed: {reason}"
        return False

    def recover(self) -> None:
        """Reconcile processes, merges and captured run operations before scheduling."""
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
        for task in self.flow["tasks"]:
            try:
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
        process = None
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            try:
                process = subprocess.Popen(
                    self.flow["check"],
                    cwd=self.flow["worktree"],
                    env=self.env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                with self.mutex:
                    self.flow["active_check"] = {
                        "pid": process.pid,
                        "start": _process_start(process.pid),
                    }
                    self._save()
                deadline = time.monotonic() + self.flow["check_timeout"]
                while process.poll() is None:
                    if self.shutdown.is_set() or time.monotonic() >= deadline:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        code = 124
                        break
                    self.shutdown.wait(
                        min(self.poll, max(0, deadline - time.monotonic()))
                    )
                else:
                    code = process.returncode
            except OSError as error:
                output.write(str(error))
        reason = None
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
            self.failure = reason or (
                f"the check failed after wave {self.flow['wave']}: "
                f"exit {code}, see {log}"
            )
            return False
        return True

    def answer_effect(self, task: dict[str, Any]) -> None:
        """Complete a durably scheduled close for a drop or replacement."""
        effect = task.get("answer_effect")
        if not effect:
            return
        warning = self.close(effect["run"])
        with self.mutex:
            if warning:
                task.setdefault("warnings", []).append(warning)
                self._event(
                    "task", task=task["id"], status=task["status"], reason=warning
                )
            task["answer_effect"] = None
            self._save()

    def answers(self) -> None:
        """Apply each complete answer and its consumption marker in one save."""
        lines = record.read_answers(self.id, self.env)
        for index in range(self.flow["answers_seen"], len(lines)):
            answer = lines[index]
            task = None
            problem = None
            fields: dict[str, Any] = {}
            try:
                if answer is None:
                    raise RunnerError(f"unreadable answer line {index + 1}")
                task = record.task_entry(self.flow, answer["task"])
                kind = answer["kind"]
                if kind not in record.allowed_answers(task):
                    raise RunnerError(f"task {task['id']} is {task['status']}")
                fields = {
                    "answer": answer["n"],
                    "reason": None,
                    "entry": None,
                    "explicit_review": kind == "review",
                }
                if kind == "accept":
                    tip = self.tip(task["branch"])
                    if tip != answer["commit"]:
                        raise RunnerError(
                            f"the branch moved from {answer['commit']} to {tip}"
                        )
                    fields.update(status="accepted", head=tip)
                elif kind in ("send", "review", "worker"):
                    fields.update(
                        status="pending",
                        entry="write" if kind == "worker" else kind,
                        round=0,
                        review_action=None,
                        recover_review=False,
                    )
                    if kind == "send":
                        fields["answer_file"] = answer["file"]
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
                    self.flow["halted"] = None
                self.flow["answers_seen"] = index + 1
                self._save()
                if not problem and task is not None:
                    self._event(
                        "task",
                        task=task["id"],
                        status=task["status"],
                        reason=task["reason"],
                    )
            if task is not None and not problem:
                self.answer_effect(task)

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
        futures = [executor.submit(self.pipeline, task) for task in wave]
        try:
            pending = set(futures)
            while pending and not self.shutdown.is_set():
                _, pending = wait(pending, timeout=self.poll)
            if not self.shutdown.is_set():
                for future in futures:
                    future.result()
        finally:
            executor.shutdown(wait=not self.shutdown.is_set(), cancel_futures=True)

    def begin(self) -> None:
        """Initialize one runner attempt before recovery and scheduling."""
        with self.mutex:
            self.flow.update(
                runner={
                    "pid": os.getpid(),
                    "started_at": time.time(),
                    "exiting": False,
                },
                status="running",
                reason=None,
                ended_at=None,
                halted=None,
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
        if self.flow["check_pending"] and not self.check():
            return "failed", self.failure
        while not self.shutdown.is_set():
            self.answers()
            self.dependencies()
            for task in self.flow["tasks"]:
                if task["status"] == "accepted" and not self.merge(task):
                    return "failed", self.failure
            if self.flow["check_pending"] and not self.check():
                return "failed", self.failure
            self.dependencies()
            if self.flow["halted"]:
                return "stopped", self.flow["halted"]
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
                    self.begin()
                if continuing:
                    self.answers()
                status, reason = self.drive()
            except Exception as error:
                status, reason, code = "failed", f"{type(error).__name__}: {error}", 1
                print(
                    f"emb flow: {error}\nnext: emb flow --status {self.id}",
                    file=sys.stderr,
                )
            if self.shutdown.is_set():
                _kill_check(self.flow.get("active_check"))
                status = "failed"
                reason = (
                    "the runner was stopped by a signal; "
                    f"emb flow --resume {self.id} goes on"
                )
            with contextlib.ExitStack() as lifecycle:
                lifecycle.enter_context(_lifecycle_guard(self.folder))
                if (
                    len(record.read_answers(self.id, self.env))
                    > self.flow["answers_seen"]
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
        sig: signal.signal(sig, stop) for sig in (signal.SIGHUP, signal.SIGTERM)
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
        os._exit(1)
    return code


if __name__ == "__main__":
    sys.exit(main())
