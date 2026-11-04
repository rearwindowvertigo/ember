"""Plan, start, watch and answer flows through private records and sessions.

Exit 82 reports an ended flow with unmerged tasks or a failure. Other exits
use ember.runs.exits. Commands do not apply queued answers to task records.
"""

import argparse
import contextlib
import fcntl
import io
import json
import os
import shlex
import shutil
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from ember import branches, choices, db, paths, tmux
from ember.flows import lock, plan, record
from ember.gapcheck import policy
from ember.runs import exits
from ember.runs import record as run_record
from ember.runs.core import STALL_MINUTES, Core

NOT_MERGED = 82
POLL_SECONDS = 1.0
START_SECONDS = 30.0
SESSION_EXIT_SECONDS = 10.0
CANCEL_SECONDS = 60.0
GIT_LOCATION = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "EMBER_FLOW_RUNNER",
)


class CommandError(Exception):
    """A command diagnostic with its exit code and next action."""

    def __init__(self, code: int, message: str, line: str) -> None:
        """Store the diagnostic and action."""
        super().__init__(message)
        self.code = code
        self.line = line


def _usage(args: argparse.Namespace) -> str:
    """Check every mode and dependent option before reading files."""
    modes = [
        name
        for name in ("dry_run", "start", "watch", "status", "lead", "resume", "cancel")
        if (
            getattr(args, name)
            if name in ("dry_run", "start")
            else getattr(args, name) is not None
        )
    ]
    problem = None
    if len(modes) != 1:
        problem = (
            "give exactly one of --dry-run, --start, --watch, "
            "--status, --lead, --resume or --cancel"
        )
    else:
        mode = modes[0]
        planning = mode in ("dry_run", "start")
        answers = [name for name in record.ANSWER_KINDS if getattr(args, name)]
        if planning != (args.plan is not None):
            problem = "--plan is required only with --dry-run or --start"
        elif args.previous is not None and mode != "dry_run":
            problem = "--previous needs --dry-run"
        elif (mode == "lead" and len(answers) != 1) or (mode != "lead" and answers):
            problem = (
                "--lead needs exactly one of --send, --accept, "
                "--review, --worker or --drop"
            )
        elif args.commit is not None and not (mode == "lead" and args.accept):
            problem = "--commit needs --lead with --accept"
        elif args.reviewer is not None and not (
            planning or (mode == "lead" and args.review)
        ):
            problem = "--reviewer needs --dry-run, --start or --lead with --review"
        elif not planning and (args.writer is not None or args.claim is not None):
            problem = "--writer and --claim need --dry-run or --start"
        elif args.stall is not None and mode != "start":
            problem = "--stall needs --start"
        elif args.reason is not None and mode != "cancel":
            problem = "--reason needs --cancel"
        elif args.json and mode not in ("dry_run", "watch", "status", "cancel"):
            problem = "--json needs --dry-run, --watch, --status or --cancel"
    if problem:
        raise CommandError(exits.USAGE, problem, "next: emb flow --help")
    return modes[0]


def _settings(claim: str | None, env: Mapping[str, str]) -> dict[str, Any]:
    """Read settings once and reject an unusable claim."""
    try:
        settings = choices.read(claim, env=env)
    except choices.InvalidChoiceError as error:
        raise CommandError(64, str(error), exits.line(64, "claim") or "") from error
    except db.DatabaseError as error:
        raise CommandError(1, str(error), exits.line(1, "database") or "") from error
    if claim is not None and settings["claim"]["status"] in ("gone", "unknown"):
        raise CommandError(
            64,
            f"claim {claim} is {settings['claim']['status']}",
            exits.line(64, "claim") or "",
        )
    return settings


def _worker(
    worker: plan.Worker,
    role: str,
    settings: Mapping[str, Any],
    core: Core,
) -> plan.Worker:
    """Check a worker and resolve its effective effort from this settings read."""
    plan.check_worker(worker, settings, core.env, core.load_driver)
    effort = worker.effort
    row = (settings.get("menu") or {}).get(plan.ROW[role])
    if (
        effort is None
        and row
        and row["state"] == "set"
        and row["engine"] == worker.engine
        and row["model"] == worker.model
    ):
        effort = row["effort"]
    driver = core.load_driver(worker.engine)
    if effort is None:
        effort = driver.default_effort(worker.model)
    if effort is not None and not driver.valid_effort(effort):
        raise CommandError(
            64,
            f"{worker.engine} does not take the effort {effort}",
            "next: emb flow --help",
        )
    return replace(worker, effort=effort)


def _require_tool(name: str, env: Mapping[str, str]) -> None:
    """Refuse before creating a flow when a required tool is absent."""
    if shutil.which(name, path=env.get("PATH", os.defpath)) is None:
        raise CommandError(69, f"{name} is not on PATH", f"owner: install {name}")


def runner_command(flow_id: str) -> list[str]:
    """Load the runner in isolation from this Ember installation.

    The absolute package root also supports source installations without
    trusting the flow worktree or PYTHONPATH during module loading.
    """
    root = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        f"import sys; sys.path.insert(0, {root!r}); "
        "import runpy; runpy.run_module('ember.flows.runner', run_name='__main__')"
    )
    return [sys.executable, "-I", "-c", bootstrap, flow_id]


@contextlib.contextmanager
def lifecycle(
    flow_id: str, env: Mapping[str, str], *, blocking: bool = True
) -> Iterator[None]:
    """Serialize startup, answers and cancellation with runner shutdown.

    The runner must hold this same lifecycle.lock for its final answer read
    through releasing its lifetime lock. It must not hold it for a wave.
    """
    with _file_lock(record.folder(flow_id, env) / "lifecycle.lock", blocking=blocking):
        yield


@contextlib.contextmanager
def _file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive lock on a private file until the operation ends."""
    fd = paths.open_private_file(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise lock.Busy(f"another command holds {path.name}") from error
        yield
    finally:
        os.close(fd)


def _startup(flow: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Read a startup reservation without changing the runner's flow record."""
    path = record.folder(flow["id"], env) / "startup.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("at"), (int, float)):
        raise record.FlowError(flow["id"], "invalid startup.json")
    return value


def _starting(flow: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """Return whether a runner has time left to acquire its lifetime lock."""
    reservation = _startup(flow, env)
    at = reservation.get("at", 0)
    runner = flow.get("runner") or {}
    if runner.get("started_at", 0) >= at and at:
        return False
    if not at and flow["status"] == "starting":
        at = flow["updated_at"]
    return bool(at and time.time() < at + START_SECONDS)


def _wait_session_exit(flow_id: str, env: Mapping[str, str], follow: str) -> None:
    """Wait for an unlocked runner's pane to end, then remove its dead session."""
    deadline = time.monotonic() + SESSION_EXIT_SECONDS
    while tmux.exists(flow_id, env=env):
        try:
            if tmux.pane_dead(flow_id, env=env):
                tmux.kill(flow_id, env=env)
                return
        except tmux.TmuxError:
            # The runner may remove its session between these observations.
            if not tmux.exists(flow_id, env=env):
                return
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CommandError(
                75, f"the session for {flow_id} is still running", follow
            )
        time.sleep(min(POLL_SECONDS, remaining))


def _session(
    flow: dict[str, Any],
    env: Mapping[str, str],
    argv: Callable[[str], list[str]],
    *,
    fresh: bool = False,
) -> None:
    """Reserve startup and launch one session while lifecycle.lock is held."""
    flow_id = flow["id"]
    follow = f"next: emb flow --watch {flow_id} --json"
    if lock.live(flow_id, env) or (not fresh and _starting(flow, env)):
        raise CommandError(
            75, f"the runner for {flow_id} is running or starting", follow
        )
    _require_tool("tmux", env)
    try:
        _wait_session_exit(flow_id, env, follow)
        (record.folder(flow_id, env) / record.CANCEL_NAME).unlink(missing_ok=True)
        events = record.read_events(flow_id, env)
        reservation = {"at": time.time(), "after": events[-1]["seq"] if events else 0}
        run_record.write_private(
            record.folder(flow_id, env) / "startup.json", json.dumps(reservation) + "\n"
        )
        tmux.new_session(
            flow_id,
            cwd=Path(flow["worktree"]),
            argv=argv(flow_id),
            env_vars={"EMBER_FLOW_RUNNER": flow_id},
            env=env,
        )
    except (tmux.TmuxError, paths.RuntimeDirError) as error:
        # No runner was launched, so the command may record its startup failure.
        flow.update(status="failed", reason=str(error), ended_at=time.time())
        record.save(flow, env)
        run_record.write_private(
            record.folder(flow_id, env) / "startup.json", '{"at": 0}\n'
        )
        if isinstance(error, paths.RuntimeDirError):
            raise
        raise CommandError(
            71, str(error), f"next: emb flow --resume {flow_id}"
        ) from error


def _prepare(
    args: argparse.Namespace,
    env: Mapping[str, str],
    core: Core,
) -> tuple[plan.Plan, Path, Path, str, plan.Worker, plan.Worker, dict[str, Any]]:
    """Validate a plan, checkout, claim and both workers before creating state."""
    document = plan.load(args.plan)
    if not Path(document.dir).is_dir():
        raise CommandError(
            66,
            f"not a directory: {document.dir}",
            f"next: ls -l {shlex.quote(document.dir)}",
        )
    _require_tool("git", env)
    try:
        top, main, base = branches.resolve_base(document.dir, document.base, env)
    except branches.BranchError as error:
        if error.code == 65:
            raise CommandError(
                65,
                str(error),
                f"next: emb flow --plan {shlex.quote(str(document.path))} "
                "--dry-run (with dir inside a git checkout)",
            ) from error
        raise
    settings = _settings(args.claim, env)
    document = _parallel(_review_rounds(document, settings), settings)
    workers = [
        _worker(
            plan.resolve(role, getattr(args, role), document, settings),
            role,
            settings,
            core,
        )
        for role in plan.ROLES
    ]
    return document, top, main, base, workers[0], workers[1], settings


def _review_rounds(document: plan.Plan, settings: Mapping[str, Any]) -> plan.Plan:
    """Use the owner's `flow.review_rounds` row when the plan sets no rounds.

    The row counts reviews of one task; a plan's `rounds` counts fix rounds,
    one fewer.
    """
    if document.rounds_source == "plan":
        return document
    row = (settings.get("answers") or {}).get("flow.review_rounds") or {}
    value = row.get("value")
    if value not in choices.REVIEW_ROUNDS:
        return document
    return replace(document, rounds=int(value) - 1, rounds_source="setting")


def _parallel(document: plan.Plan, settings: Mapping[str, Any]) -> plan.Plan:
    """Apply the owner's `flow.parallel` row unless the plan sets `parallel`."""
    if document.parallel_source == "plan":
        return document
    row = (settings.get("answers") or {}).get("flow.parallel") or {}
    value = row.get("value")
    if value not in choices.FLOW_PARALLEL:
        return document
    return replace(document, parallel=int(value), parallel_source="setting")


def _stall_minutes(args: argparse.Namespace, settings: Mapping[str, Any]) -> float:
    """Return `--stall`, else the owner's `flow.stall` row, else the default."""
    if args.stall is not None:
        return args.stall
    row = (settings.get("answers") or {}).get("flow.stall") or {}
    value = row.get("value")
    return int(value) if value in choices.FLOW_STALL else STALL_MINUTES


def _whole_review(settings: Mapping[str, Any]) -> bool:
    """Return whether the owner's `flow.whole_review` row is on."""
    row = (settings.get("answers") or {}).get("flow.whole_review") or {}
    return row.get("value", "on") == "on"


def _gapcheck_decision(
    document: plan.Plan, settings: Mapping[str, Any]
) -> tuple[str, str]:
    answers = settings.get("answers")
    if not answers or "gapcheck.when" not in answers:
        return policy.RUN, "the gap check settings cannot be read"
    return policy.decide(answers, len(document.tasks))


def _dry_run(
    args: argparse.Namespace,
    document: plan.Plan,
    top: Path,
    base: str,
    writer: plan.Worker,
    reviewer: plan.Worker,
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
) -> None:
    """Print resolved workers, waves, warnings and the gap check decision.

    The gap check command names the flow's reviewer when it was given with
    `--reviewer` or in the plan; otherwise `emb gapcheck` uses the owner's
    Gap check reviewer row. The command requests JSON and forwards `--previous`
    when given. A plan the settings skip gets no command; one they leave to
    the owner gets the command with `--confirmed`.
    """
    decision, why = _gapcheck_decision(document, settings)
    gapcheck = [
        "emb",
        "gapcheck",
        "--plan",
        str(document.path),
        "--dir",
        str(top),
        "--json",
    ]
    if args.previous is not None:
        gapcheck += ["--previous", args.previous]
    if reviewer.source != "menu":
        # A reviewer named for this flow, by option or in the plan, also
        # checks its plan; the Gap check reviewer row covers the rest.
        gapcheck += ["--engine", reviewer.engine, "--model", reviewer.model]
        if reviewer.effort is not None:
            gapcheck += ["--effort", reviewer.effort]
    if args.claim is not None:
        gapcheck += ["--claim", args.claim]
    if decision == policy.ASK:
        gapcheck.append("--confirmed")
    command = None if decision == policy.SKIPPED else shlex.join(gapcheck)
    whole = _whole_review(settings)
    stall = _stall_minutes(args, settings)
    waves = plan.waves(document)
    warnings = plan.warnings(document, str(top), env)
    if args.json:
        print(
            json.dumps(
                {
                    "plan": str(document.path),
                    "dir": str(top),
                    "base": base,
                    "writer": writer.to_json(),
                    "reviewer": reviewer.to_json(),
                    "check": list(document.check),
                    "rounds": document.rounds,
                    "rounds_source": document.rounds_source,
                    "parallel": document.parallel,
                    "parallel_source": document.parallel_source,
                    "stall_minutes": stall,
                    "whole_review": whole,
                    "waves": [[task.id for task in wave] for wave in waves],
                    "warnings": warnings,
                    "gapcheck": command,
                    "gapcheck_status": decision,
                    "gapcheck_reason": why,
                },
                indent=2,
            ),
            file=out,
        )
    else:
        print(f"writer {writer.text()} ({writer.source})", file=out)
        print(f"reviewer {reviewer.text()} ({reviewer.source})", file=out)
        print(f"base {base}", file=out)
        print(
            f"rounds {document.rounds} fix rounds, at most {document.rounds + 1} "
            f"reviews per task ({document.rounds_source})",
            file=out,
        )
        print(
            f"check {shlex.join(document.check) if document.check else 'none'}",
            file=out,
        )
        for number, wave in enumerate(waves, 1):
            print(f"wave {number}:", file=out)
            for task in wave:
                print(f"  {task.id}: {', '.join(task.files)}", file=out)
        print(
            f"parallel {document.parallel} ({document.parallel_source}), "
            f"stall {stall:g} min, whole-flow review {'on' if whole else 'off'}",
            file=out,
        )
        if command is None:
            print(f"gap check: skipped, {why}", file=out)
        elif decision == policy.ASK:
            print(f"gap check: ask the owner, then {command}", file=out)
        else:
            print(f"gap check: {command}", file=out)
    for warning in warnings:
        print(f"WARNING: {warning}", file=err)


def _start(
    args: argparse.Namespace,
    env: Mapping[str, str],
    core: Core,
    argv: Callable[[str], list[str]],
    out: TextIO,
    err: TextIO,
) -> int:
    """Validate, create the branch and record, then launch the runner."""
    document, top, main, base, writer, reviewer, settings = _prepare(args, env, core)
    if args.dry_run:
        _dry_run(args, document, top, base, writer, reviewer, settings, env, out, err)
        return 0
    _require_tool("tmux", env)
    # Validate the runtime folder before leaving a record or worktree behind.
    try:
        tmux.socket_path(env)
    except OSError as error:
        raise CommandError(69, str(error), exits.line(69, "runtime") or "") from error
    repository = branches.open_repository(top, env, "flow")
    base_ref = repository.git(
        "rev-parse",
        "--symbolic-full-name",
        "--verify",
        "--end-of-options",
        document.base,
    )
    base_name = base_ref.stdout.strip() if base_ref.returncode == 0 else ""
    base_branch = (
        base_name.removeprefix("refs/heads/")
        if base_name.startswith("refs/heads/")
        else None
    )
    flow_id = record.new_id()
    # The record and the branch name the same path, resolved once.
    worktree = paths.worktree_path(flow_id, env)
    flow = record.new_flow(
        flow_id,
        document,
        top=str(top),
        main=str(main),
        common=str(repository.common),
        worktree=str(worktree),
        base=base,
        base_branch=base_branch,
        writer=writer,
        reviewer=reviewer,
        stall_minutes=_stall_minutes(args, settings),
        claim=args.claim,
        whole_review=_whole_review(settings),
    )
    # None denotes a frozen engine default, not permission to read a later menu.
    for role in plan.ROLES:
        flow[role]["effort_frozen"] = True
    record.create(flow, document.text, env)
    with lifecycle(flow_id, env):
        try:
            branches.create_flow_branch(
                document.dir, flow_id, base, env, worktree=worktree
            )
        except branches.BranchError as error:
            flow.update(status="failed", reason=str(error), ended_at=time.time())
            record.save(flow, env)
            raise
        _session(flow, env, argv, fresh=True)
    print(
        f"flow {flow_id}\nbranch {flow['branch']}\nworktree {flow['worktree']}",
        file=out,
    )
    print(f"next: emb flow --watch {flow_id} --json", file=err)
    return 0


def _load(
    flow_id: str, env: Mapping[str, str], *, missing_code: int = 64
) -> dict[str, Any]:
    """Read a flow with the mode's missing-record exit code."""
    if not record.is_flow_id(flow_id):
        raise CommandError(64, f"invalid flow id: {flow_id}", "next: emb flow --status")
    try:
        return record.load(flow_id, env)
    except record.FlowMissingError as error:
        raise CommandError(
            missing_code, str(error), "next: emb flow --status"
        ) from error


def _not_done(flow: Mapping[str, Any]) -> None:
    """Refuse any attempt to restart an already completed flow."""
    if flow["status"] == "done":
        raise CommandError(
            75,
            f"the flow {flow['id']} is done",
            f"next: emb flow --status {flow['id']}",
        )


def _resume(
    flow_id: str, env: Mapping[str, str], argv: Callable[[str], list[str]], out: TextIO
) -> None:
    """Start an idle flow under the startup reservation lock."""
    _load(flow_id, env)
    with lifecycle(flow_id, env):
        flow = _load(flow_id, env)
        _not_done(flow)
        if flow["status"] != "cancelled" and record.cancel_request(flow_id, env):
            raise CommandError(
                75,
                f"cancellation of {flow_id} is pending",
                f"next: emb flow --status {flow_id}",
            )
        _session(flow, env, argv)
    print(f"flow {flow_id}", file=out)


def _cancel_report(
    flow: Mapping[str, Any], env: Mapping[str, str], err: TextIO
) -> dict[str, Any]:
    """List recorded task branches and runs whose stored status is open.

    A run record that does not load is left out with a warning on `err`.
    """
    task_branches: list[dict[str, Any]] = []
    open_runs: list[dict[str, str]] = []
    seen_runs: set[str] = set()
    seen_branches: set[str] = set()
    for task in flow["tasks"]:
        branch = task["branch"]
        removed = any(
            item.get("action") == "removed" and item.get("branch") == branch
            for item in task["cleanup"]
        )
        if branch and not removed:
            task_branches.append({"task": task["id"], "branch": branch})
            seen_branches.add(branch)
        for item in task["cleanup"]:
            branch = item.get("branch")
            if (
                branch
                and item.get("action") != "removed"
                and branch not in seen_branches
            ):
                task_branches.append({"task": task["id"], "branch": branch})
                seen_branches.add(branch)
        references = [
            ("writer", task["writer_run"]),
            ("reviewer", task["reviewer_run"]),
            *[("reviewer", item.get("run")) for item in task["reviews"]],
            *[("writer", item.get("run")) for item in task["cleanup"]],
            ("writer", (task.get("answer_effect") or {}).get("run")),
        ]
        for role, run_id in references:
            if not run_id or run_id in seen_runs:
                continue
            seen_runs.add(run_id)
            try:
                run = run_record.load(run_id, env)
            except run_record.RecordError as error:
                print(
                    f"WARNING: task {task['id']} {role} run {run_id} "
                    f"is not listed: {error}",
                    file=err,
                )
                continue
            if run.status in (
                run_record.STARTING,
                run_record.RUNNING,
                run_record.WAITING,
            ):
                open_runs.append({"task": task["id"], "role": role, "run": run_id})
            if (
                run.branch is not None
                and run.branch in flow["abandoned"]
                and run.branch not in seen_branches
            ):
                task_branches.append({"task": task["id"], "branch": run.branch})
                seen_branches.add(run.branch)
    for branch in flow["abandoned"]:
        if branch not in seen_branches:
            task_branches.append({"task": None, "branch": branch})
            seen_branches.add(branch)
    return {
        "flow": flow["id"],
        "status": flow["status"],
        "reason": flow["reason"],
        "branch": flow["branch"],
        "task_branches": task_branches,
        "open_runs": open_runs,
    }


def _signal_cancel(flow: Mapping[str, Any], env: Mapping[str, str]) -> None:
    """Signal the recorded runner while its process identity still matches."""
    from ember.flows.runner import _process_start

    identity = flow["runner"]
    if not identity or identity.get("exiting") or not lock.live(flow["id"], env):
        return
    pid = identity["pid"]
    with contextlib.suppress(ProcessLookupError):
        fd = os.pidfd_open(pid)
        try:
            matches = (
                _process_start(pid) == identity["start"]
                if identity.get("start") is not None
                else tmux.pane_pid(flow["id"], env=env) == pid
            )
            if matches:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)


def _cancel(
    flow_id: str,
    reason: str | None,
    as_json: bool,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
) -> None:
    """Request cancellation, stop the runner and report the retained work."""
    _load(flow_id, env, missing_code=65)
    with lifecycle(flow_id, env):
        flow = _load(flow_id, env, missing_code=65)
        if flow["status"] in record.ENDED:
            raise CommandError(
                75,
                f"the flow {flow_id} is {flow['status']}",
                f"next: emb flow --status {flow_id}",
            )
        directory = record.folder(flow_id, env)
        request = record.cancel_request(flow_id, env) or {
            "at": time.time(),
            "reason": reason,
        }
        run_record.write_private(directory / "startup.json", '{"at": 0}\n')
        run_record.write_private(directory / record.CANCEL_NAME, json.dumps(request))
        deadline = time.monotonic() + CANCEL_SECONDS
        _signal_cancel(flow, env)
    while True:
        try:
            with lifecycle(flow_id, env, blocking=False):
                if not lock.live(flow_id, env):
                    flow = _load(flow_id, env, missing_code=65)
                    if flow["status"] not in record.ENDED:
                        record.finish_cancel(flow, request, env)
                    elif flow["status"] != "cancelled":
                        raise CommandError(
                            75,
                            f"the flow {flow_id} is {flow['status']}",
                            f"next: emb flow --status {flow_id}",
                        )
                    result = _cancel_report(flow, env, err)
                    break
        except lock.Busy:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CommandError(
                75,
                f"the runner for {flow_id} is still running after {CANCEL_SECONDS:g} "
                "seconds",
                f"next: emb flow --status {flow_id}",
            )
        time.sleep(min(POLL_SECONDS, remaining))
    if as_json:
        print(json.dumps(result, indent=2), file=out)
    else:
        print(f"flow {flow_id} cancelled", file=out)
        if result["reason"] is not None:
            print(f"reason {result['reason']}", file=out)
        print(f"branch {result['branch']}", file=out)
        for item in result["task_branches"]:
            print(f"task {item['task']} branch {item['branch']}", file=out)
        for item in result["open_runs"]:
            print(f"task {item['task']} {item['role']} run {item['run']}", file=out)


def _lead(
    args: argparse.Namespace,
    env: Mapping[str, str],
    core: Core,
    argv: Callable[[str], list[str]],
    out: TextIO,
) -> None:
    """Validate and queue an answer, copying corrections inside the append lock."""
    flow_id, task_id = args.lead
    _load(flow_id, env)
    with lifecycle(flow_id, env):
        flow = _load(flow_id, env)
        _not_done(flow)
        if flow["status"] == "cancelled" or record.cancel_request(flow_id, env):
            raise CommandError(
                75,
                f"the flow {flow_id} is cancelled or cancelling",
                f"next: emb flow --resume {flow_id}",
            )
        try:
            task = record.task_entry(flow, task_id)
        except KeyError as error:
            raise CommandError(
                64, f"unknown task: {task_id}", f"next: emb flow --status {flow_id}"
            ) from error
        kind = next(name for name in record.ANSWER_KINDS if getattr(args, name))
        allowed = record.allowed_answers(task)
        if kind not in allowed:
            raise CommandError(
                75,
                f"task {task_id} is {task['status']}; "
                f"it takes {', '.join(allowed) or 'no answers'}",
                f"next: emb flow --status {flow_id}",
            )
        answer: dict[str, Any] = {"task": task_id, "kind": kind}
        if kind == "send":
            source = Path(args.send).absolute()
            try:
                text = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise CommandError(
                    66,
                    f"cannot read {source}: {error}",
                    f"next: ls -l {shlex.quote(str(source))}",
                ) from error
            if not text.strip():
                raise CommandError(
                    64, f"the correction {source} is empty", "next: emb flow --help"
                )
            answer["file"] = str(source)
        elif kind == "accept" and task["status"] == "conflict":
            _require_tool("git", env)
            _, _, commit = branches.resolve_base(
                flow["dir"], args.commit or task["merge_tip"], env
            )
            if commit != task["merge_tip"]:
                raise CommandError(
                    64,
                    f"task {task_id} is conflict; --accept takes only its "
                    f"approved tip {task['merge_tip']}",
                    f"next: emb flow --lead {flow_id} {task_id} --accept",
                )
            if not branches.is_contained(flow["worktree"], commit, flow["branch"], env):
                raise CommandError(
                    75,
                    f"task {task_id} is conflict and {flow['branch']} does not "
                    f"hold {commit}",
                    f"next: git -C {shlex.quote(flow['worktree'])} merge --no-ff "
                    f"--no-edit {commit}, resolve the conflicts and commit, then "
                    f"emb flow --lead {flow_id} {task_id} --accept",
                )
            answer["commit"] = commit
        elif kind == "accept":
            _require_tool("git", env)
            _, _, commit = branches.resolve_base(
                flow["dir"], args.commit or task["branch"], env
            )
            answer["commit"] = commit
        if kind == "worker" or (kind == "review" and args.reviewer is not None):
            role = "writer" if kind == "worker" else "reviewer"
            worker = _worker(
                plan.parse_worker(args.worker or args.reviewer, "answer"),
                role,
                _settings(None, env),
                core,
            )
            answer["worker"] = worker.text()
            answer["effort_frozen"] = True
        queued = record.append_answer(flow_id, answer, env)
        print(f"queued answer {queued['n']}: {kind} for {task_id}", file=out)
        if not lock.live(flow_id, env) and not _starting(flow, env):
            _session(flow, env, argv)
            print(f"flow {flow_id}", file=out)


def answer_choices(flow_id: str, task: Mapping[str, Any]) -> list[str]:
    """Return concrete command forms for every answer the task accepts."""
    suffix = {
        "send": "--send <file>",
        "accept": "--accept [--commit <sha>]",
        "review": "--review [--reviewer <worker>]",
        "worker": "--worker <worker>",
        "drop": "--drop",
    }
    return [
        f"emb flow --lead {flow_id} {task['id']} {suffix[kind]}"
        for kind in record.allowed_answers(task)
    ]


def _task_report(flow_id: str, task: Mapping[str, Any]) -> dict[str, Any]:
    """Keep finding objects and retained cleanup details beside summary counts."""
    result = {
        key: task[key]
        for key in (
            "id",
            "status",
            "reason",
            "round",
            "writer_run",
            "branch",
            "worktree",
            "merge_commit",
            "outside_files",
            "cleanup",
        )
    }
    result["findings"] = {
        severity: sum(item.get("severity") == severity for item in task["findings"])
        for severity in ("must", "should", "lead")
    }
    result["denied_paths"] = task.get("denied_paths", [])
    result["finding_details"] = task["findings"]
    result["choices"] = answer_choices(flow_id, task)
    return result


def _next(flow: Mapping[str, Any], event: str, code: int | None, live: bool) -> str:
    """Describe the next action for a stop, loss or terminal outcome."""
    flow_id, branch = flow["id"], flow["branch"]
    if flow["status"] == "cancelled":
        return f"next: emb flow --resume {flow_id}"
    if event == "runner_lost":
        return f"next: emb flow --resume {flow_id}"
    if event == "lead_stop":
        return "next: report each stop to the owner with every choice" + (
            f", then emb flow --watch {flow_id} --json"
            if live
            else "; an answer with emb flow --lead starts the runner"
        )
    if flow["status"] == "failed":
        if flow["check_pending"]:
            return (
                "next: fix the flow branch "
                f"(an emb run on {shlex.quote(flow['worktree'])}, "
                f"then emb merge <its branch> --into {branch} --verdict keep "
                "--note '<verdict line>'), "
                f"then emb flow --resume {flow_id}"
            )
        return f"next: emb flow --status {flow_id}"
    prefix = "report the unmerged tasks to the owner; " if code else ""
    return (
        f"next: {prefix}review {branch}, then emb merge {branch} --into <your branch> "
        f"--verdict keep --note '<verdict line>' --dir {shlex.quote(flow['dir'])}"
    )


def _current_events(
    flow: Mapping[str, Any], env: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Select events from the current runner attempt, retaining older history."""
    events = record.read_events(flow["id"], env)
    boundary = _startup(flow, env).get("after", 0)
    for event in events:
        if event.get("kind") == "flow" and event.get("status") == "running":
            boundary = max(boundary, event["seq"] - 1)
    return [event for event in events if event["seq"] > boundary]


def _watch_result(
    flow: dict[str, Any],
    env: Mapping[str, str],
    cursor: int,
) -> tuple[dict[str, Any], int] | None:
    """Select unread events or a current state outcome while watch.lock is held."""
    flow_id = flow["id"]
    live = lock.live(flow_id, env)
    if not live:
        # The runner may have saved its final state since the caller read it.
        flow = _load(flow_id, env)
    events = _current_events(flow, env)
    fresh = [event for event in events if event["seq"] > cursor]
    stops = [event for event in fresh if event.get("kind") == "lead_stop"]
    terminal = next(
        (event for event in reversed(events) if event.get("kind") == "end"), None
    )
    starting = not live and _starting(flow, env)
    reservation_at = _startup(flow, env).get("at", 0)
    current_record = (
        not reservation_at
        or (flow.get("runner") or {}).get("started_at", 0) >= reservation_at
    )
    code = None
    if (
        terminal is not None
        and current_record
        and flow["status"] in record.ENDED
        and not starting
    ):
        event = "end"
        code = terminal["exit"]
    elif stops:
        event = "lead_stop"
    elif not live and not starting:
        if not current_record:
            event, code = "runner_lost", exits.SESSION_GONE
        elif flow["status"] in record.ENDED:
            event = "end"
            code = (
                0
                if flow["status"] == "done"
                and all(task["status"] == "merged" for task in flow["tasks"])
                else NOT_MERGED
            )
        elif flow["status"] == "stopped":
            event = "lead_stop"
            # Repeated stops describe current state without redelivering events.
        else:
            event, code = "runner_lost", exits.SESSION_GONE
    else:
        return None
    result = {
        key: flow[key]
        for key in ("status", "reason", "branch", "worktree", "tip", "dir")
    }
    result.update(
        event=event,
        flow=flow_id,
        exit=code,
        tasks=[_task_report(flow_id, task) for task in flow["tasks"]],
        stops=stops,
        abandoned=flow["abandoned"],
        next=_next(flow, event, code, live),
    )
    return result, max([cursor, *(item["seq"] for item in fresh)])


def _task_line(task: Mapping[str, Any]) -> str:
    """Format a task's state and optional reason."""
    return f"{task['id']} {task['status']}" + (
        f": {task['reason']}" if task["reason"] else ""
    )


def _print_watch(result: dict[str, Any], as_json: bool, out: TextIO) -> None:
    """Print one report with findings, choices and retained cleanup details."""
    if as_json:
        print(json.dumps(result, indent=2), file=out)
        return
    print(
        f"flow {result['flow']} {result['status']}"
        + (f": {result['reason']}" if result["reason"] else ""),
        file=out,
    )
    for task in result["tasks"]:
        print(_task_line(task), file=out)
        for finding in task["finding_details"]:
            print(f"  finding: {json.dumps(finding)}", file=out)
        for choice in task["choices"]:
            print(f"  {choice}", file=out)
        for cleanup in task["cleanup"]:
            if cleanup.get("action") != "removed":
                print(
                    f"  retained: {json.dumps(cleanup)}; worktree {task['worktree']}",
                    file=out,
                )
    for stop in result["stops"]:
        print(f"stop {stop.get('task') or 'flow'}: {stop.get('reason')}", file=out)
        for choice in stop.get("choices", []):
            print(f"  {choice}", file=out)
    for branch in result["abandoned"]:
        print(f"retained branch: {branch}", file=out)


def _watch(
    flow_id: str, as_json: bool, env: Mapping[str, str], out: TextIO, err: TextIO
) -> int:
    """Poll files and serialize selection, output and cursor advancement."""
    _load(flow_id, env)
    while True:
        with _file_lock(record.folder(flow_id, env) / "watch.lock"):
            flow = _load(flow_id, env)
            selected = _watch_result(flow, env, record.read_cursor(flow_id, env))
            if selected is not None:
                result, seq = selected
                _print_watch(result, as_json, out)
                out.flush()
                print(result["next"], file=err)
                err.flush()
                record.write_cursor(flow_id, seq, env)
                return result["exit"] or 0
        time.sleep(POLL_SECONDS)


def _denial_status(flow: dict[str, Any], env: Mapping[str, str]) -> None:
    """Add retained and current worker denials to a flow status snapshot."""
    for task in flow["tasks"]:
        ids = [task.get("writer_run"), task.get("reviewer_run")]
        ids.extend(review.get("run") for review in task["reviews"])
        denials = list(task.get("denied_paths", []))
        for item in run_record.referenced_denials(ids, env):
            if item not in denials:
                denials.append(item)
        task["denied_paths"] = denials
    flow["denied_paths"] = [
        {**item, "task": task["id"]}
        for task in flow["tasks"]
        for item in task["denied_paths"]
    ]


def _status(
    flow_id: str, as_json: bool, env: Mapping[str, str], out: TextIO, err: TextIO
) -> None:
    """Print one complete record or an oldest-first summary of all flows."""
    if flow_id:
        flow = _load(flow_id, env)
        flow["runner_live"] = lock.live(flow_id, env)
        _denial_status(flow, env)
        if as_json:
            print(json.dumps(flow, indent=2), file=out)
        else:
            for key, value in flow.items():
                if key != "tasks":
                    print(f"{key}: {value}", file=out)
            for task in flow["tasks"]:
                print(_task_line(task), file=out)
                for item in task["denied_paths"]:
                    print(
                        f"  denied {item['path']} ({item['reason']}) "
                        f"run {item['run']} turn {item['turn']}",
                        file=out,
                    )
        return
    flows, errors = record.list_flows(env)
    for error in errors:
        print(f"emb flow: skipped {error}", file=err)
    items = []
    for flow in flows:
        _denial_status(flow, env)
        item = {key: flow[key] for key in ("id", "status", "reason", "dir", "branch")}
        item["denied_paths"] = flow["denied_paths"]
        item.update(
            runner_live=lock.live(flow["id"], env),
            merged=sum(task["status"] == "merged" for task in flow["tasks"]),
            tasks=len(flow["tasks"]),
        )
        items.append(item)
    if as_json:
        print(json.dumps(items, indent=2), file=out)
    else:
        for item in items:
            status = item["status"]
            if status == "cancelled" and item["reason"]:
                status += f": {item['reason']}"
            print(
                f"{item['id']}  {status}  "
                f"{item['merged']}/{item['tasks']} merged  {item['dir']}",
                file=out,
            )
            for denial in item["denied_paths"]:
                print(
                    f"  task {denial['task']} denied {denial['path']} "
                    f"({denial['reason']}) run {denial['run']} turn {denial['turn']}",
                    file=out,
                )


def main(
    args: argparse.Namespace,
    env: Mapping[str, str] | None,
    *,
    core_factory: Callable[..., Core] = Core,
    runner_argv: Callable[[str], list[str]] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Execute one flow command with injectable drivers, launcher and streams."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    environment = {
        key: value
        for key, value in (os.environ if env is None else env).items()
        if key not in GIT_LOCATION
    }
    argv = runner_argv or runner_command
    try:
        mode = _usage(args)
        if mode in ("dry_run", "start", "lead"):
            core = core_factory(
                environment, quiet=False, out=io.StringIO(), err=io.StringIO()
            )
            if mode != "lead":
                return _start(args, environment, core, argv, out, err)
            _lead(args, environment, core, argv, out)
            flow_id = args.lead[0]
        elif mode == "resume":
            flow_id = args.resume
            _resume(flow_id, environment, argv, out)
        elif mode == "watch":
            return _watch(args.watch, args.json, environment, out, err)
        elif mode == "cancel":
            _cancel(args.cancel, args.reason, args.json, environment, out, err)
            print(f"next: emb flow --resume {args.cancel}", file=err)
            return 0
        else:
            _status(args.status, args.json, environment, out, err)
            return 0
        print(f"next: emb flow --watch {flow_id} --json", file=err)
        return 0
    except plan.PlanError as error:
        for problem in error.problems:
            print(f"emb flow: {problem}", file=err)
        print(
            f"next: ls -l {shlex.quote(args.plan)}"
            if error.code == 66
            else "next: emb flow --help",
            file=err,
        )
        return error.code
    except (CommandError, plan.WorkerError, branches.BranchError) as error:
        print(f"emb flow: {error}", file=err)
        print(error.line, file=err)
        return error.code
    except (paths.RuntimeDirError, lock.LockError) as error:
        print(f"emb flow: {error}", file=err)
        print(exits.line(69, "runtime"), file=err)
        return 69
    except KeyboardInterrupt:
        print("emb flow: interrupted", file=err)
        print(
            f"next: emb flow --watch {args.watch} --json"
            if args.watch
            else "next: emb flow --status",
            file=err,
        )
        return 74
    except Exception as error:
        print(f"emb flow: {error}", file=err)
        print("next: emb flow --status", file=err)
        return 1
