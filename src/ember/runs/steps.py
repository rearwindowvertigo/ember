"""Derive the next command from a run's existing records and git history."""

import shlex
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ember import db, skill
from ember.runs import exits, record
from ember.runs.record import Run


def derive(
    run: Run,
    session_state: str,
    needs_collection: bool,
    git: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str | None]:
    """Return the run's step and next line without changing its state.

    Args:
        run: The record, including its environment.
        session_state: The session state read by run status.
        needs_collection: The core's check for unfinished result collection.
        git: Run git with a directory followed by command arguments.

    Returns:
        The step name and its command line, or None when none applies.
    """
    if run.parent is not None:
        return "parent", None
    if run.owner_wait:
        return "owner", None
    if run.status == record.CLOSED:
        if (
            run.read_only
            or not record.work_tip(run)
            or _merged(run)
            or not _branch_exists(run, git)
        ):
            return _finish(run, git)
        return "merge", _verify(run, git)
    if run.status == record.FAILED or (
        run.turn_state != record.SENT and run.last_exit not in (None, 0)
    ):
        variant, fallback = shown_variant(run)
        text = exits.line(
            run.last_exit or 0,
            variant,
            run=run.id,
            worktree=run.worktree,
            engine=run.engine,
            log=shlex.quote(
                str((run.preparation or {}).get("log", run.folder / "prepare.log"))
            ),
            retry=retry_start(run),
            fallback=fallback,
            source=run.fallback_from,
        )
        if run.answered_at is not None and text and text.startswith("owner:"):
            text = "next: " + (
                exits.SEND.format(run=run.id)
                if run.turn and not run.last_start_failure
                else exits.START
            )
        return "failed", text
    if run.status == record.STARTING or run.turn == 0:
        return "starting", f"next: emb run --status {run.id}"
    if run.turn_state == record.SENT and session_state == "live":
        return "running", f"next: emb run --status {run.id}"
    if run.turn_state == record.SENT or needs_collection:
        return "recover", f"next: emb run --recover {run.id}"
    if not run.read_only and record.work_tip(run) and not _merged(run):
        return "merge", _verify(run, git)
    return "close", f"next: emb run --close {run.id}"


def retry_start(run: Run) -> str:
    """Return a new start using the saved brief and start options."""
    if run.start_argv:
        return shlex.join(run.start_argv)
    argv = [
        "emb",
        "run",
        "--engine",
        run.engine,
        "--dir",
        run.repo,
        "--task-file",
        str(run.folder / record.BRIEF_NAME),
        "--model",
        run.model,
    ]
    if run.effort is not None:
        argv.extend(("--effort", run.effort))
    if run.read_only:
        argv.append("--read-only")
    for path in run.read:
        argv.extend(("--read", path))
    for trailer in run.trailers:
        argv.extend(("--trailer", trailer))
    for flag, value in (
        ("--claim", run.claim),
        ("--continue-run", run.continues),
        ("--fallback-from", run.fallback_from),
        ("--retry-of", run.id if run.selection_origin is not None else None),
    ):
        if value is not None:
            argv.extend((flag, value))
    if run.selection_origin == record.SELECTION_OWNER:
        argv.append("--owner-choice")
    return shlex.join(argv)


@dataclass(frozen=True)
class ProviderFallback:
    """The owner's applicable replacement worker and any work it continues."""

    engine: str
    model: str
    effort: str | None
    continue_run: str | None


def applicable_fallback(run: Run) -> ProviderFallback | None:
    """Resolve the current fallback for a first-turn single-worker failure."""
    if (
        run.last_exit != exits.PROVIDER_ERROR
        or run.turn not in (0, 1)
        or run.parent is not None
        or run.status not in (record.FAILED, record.CLOSED)
        or run.turn_state == record.SENT
        or exit_variant(run) == "uncertain"
        or run.last_reason == "capacity retry delivery is uncertain; ask the lead"
    ):
        return None

    from ember import choices, holds

    try:
        turns, _ = record.turn_results(run)
        outcome = turns[-1] if turns else {}
        attempts = outcome.get("attempts", [])
        if attempts and attempts[-1]["sending"] and not attempts[-1]["sent"]:
            return None
        kind = outcome.get("provider_kind") or "outage"
        answers = choices.read(None, env=run.env)["answers"]
        # Every worker that failed on this job counts, so a chain of
        # fallbacks ends when the list is used up.
        failed = [
            (earlier.engine, earlier.model, record.provider_kind(earlier))
            for earlier in record.linked(run, "fallback_from")
        ]
        row = holds.replacement(
            answers,
            [*failed, (run.engine, run.model, kind)],
            env=run.env,
        )
    except Exception:  # noqa: BLE001 - an exit line never fails on the settings
        return None
    if row is None:
        return None
    return ProviderFallback(
        engine=row["engine"],
        model=row["model"],
        effort=row["effort"],
        continue_run=run.id if not run.read_only and record.work_tip(run) else None,
    )


def fallback_start(run: Run) -> str | None:
    """Return the start command when the owner's provider fallback applies."""
    fallback = applicable_fallback(run)
    if fallback is None:
        return None
    argv = ["emb", "run", "--engine", fallback.engine, "--model", fallback.model]
    if fallback.effort is not None:
        argv.extend(("--effort", fallback.effort))
    argv.extend(("--dir", run.repo, "--task-file", str(run.folder / record.BRIEF_NAME)))
    argv.extend(("--fallback-from", run.id))
    if run.read_only:
        argv.append("--read-only")
    if fallback.continue_run is not None:
        argv.extend(("--continue-run", fallback.continue_run))
    for path in run.read:
        argv.extend(("--read", path))
    for trailer in run.trailers:
        argv.extend(("--trailer", trailer))
    if run.claim is not None:
        argv.extend(("--claim", run.claim))
    return shlex.join(argv)


def exit_variant(run: Run) -> str | None:
    """Return the saved variant, falling back to older records' provider kind.

    The kind picks the exit line's variant, so a usage limit is not reported
    as an outage.
    """
    if run.last_variant is not None:
        return run.last_variant
    if run.last_exit != exits.PROVIDER_ERROR:
        return None
    try:
        turns, _ = record.turn_results(run)
    except OSError, record.RecordError:
        return None
    kind = turns[-1].get("provider_kind") if turns else None
    return kind if isinstance(kind, str) else None


def shown_variant(run: Run) -> tuple[str | None, str | None]:
    """Return the variant of a run's exit lines and its fallback start.

    A provider error on a run's first turn takes the `fallback` variant
    while the owner's fallback list names a worker that applies, whatever
    the list held when the run failed. An uncertain resend keeps its own
    variant, as does a run that a flow, panel or gap check started.

    Returns:
        The variant for `exits.line` and `exits.report`, and the fallback
        start command when the variant is `fallback`.
    """
    fallback = fallback_start(run)
    if fallback is not None:
        return "fallback", fallback
    return exit_variant(run), None


def _merged(run: Run) -> bool:
    from ember import branches

    try:
        tip = record.work_tip(run)
        return bool(
            tip
            and run.base_branch
            and branches.is_contained(
                run.main or run.repo,
                tip,
                run.base_branch,
                run.env,
                scratch=True,
            )
        )
    except OSError, subprocess.SubprocessError, branches.BranchError:
        return False


def _branch_exists(
    run: Run, git: Callable[..., subprocess.CompletedProcess[str]]
) -> bool:
    """Return whether a closed run's branch is still there to merge.

    A branch that git reports missing, or a checkout that is gone, has
    nothing left to merge. Any other git failure counts as present, so the
    merge line stays.
    """
    from ember.runs.core import RunError

    if run.branch is None:
        return False
    try:
        result = git(
            run.main or run.repo,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{run.branch}",
        )
    except RunError, OSError, subprocess.SubprocessError:
        return True
    return result.returncode not in (1, 128)


def claim_holder(
    claim: str,
    env: Mapping[str, str],
    git: Callable[..., subprocess.CompletedProcess[str]],
    skip: str | None = None,
) -> str | None:
    """Return the status line of an unfinished record under a claim.

    A run with the claim counts while it is not closed and has no parent.
    A flow counts while it has not ended, after `failed` while its check is
    pending, and after `done` while its branch exists. A panel counts while
    it is running. A record that cannot be read does not count.

    Args:
        claim: The claim id.
        env: The environment that locates the records.
        git: Run git with a directory followed by command arguments.
        skip: A run id to leave out.

    Returns:
        The `next:` line naming the first such record's status command, or
        None when the claim has none.
    """
    from ember.flows import record as flows
    from ember.panels import record as panels
    from ember.runs.core import RunError

    try:
        others = record.list_runs(env)[0]
        listed = flows.list_flows(env)[0]
        running = panels.list_panels(env)[0]
    except OSError:
        return None
    for other in others:
        if (
            other.id != skip
            and other.claim == claim
            and other.parent is None
            and other.status != record.CLOSED
        ):
            return f"next: emb run --status {other.id}"
    for flow in listed:
        if flow.get("claim") != claim:
            continue
        held = flow["status"] in ("starting", "running", "checking", "stopped") or (
            flow["status"] == "failed" and flow.get("check_pending")
        )
        if not held and flow["status"] == "done":
            try:
                result = git(
                    flow["main"],
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{flow['branch']}",
                )
                held = result.returncode == 0
            except RunError, OSError, subprocess.SubprocessError:
                held = True
        if held:
            return f"next: emb flow --status {flow['id']}"
    for panel in running:
        if panel.get("claim") == claim and panel["status"] == "running":
            return f"next: emb panel --status {panel['id']}"
    return None


def _finish(
    run: Run, git: Callable[..., subprocess.CompletedProcess[str]]
) -> tuple[str, str | None]:
    """Return the closed run's step and claim-finish line.

    While another record under the claim is unfinished, the line names that
    record's status command instead.
    """
    if run.claim is None:
        return "finish", None
    try:
        with db.open_read(run.env) as conn:
            if conn is not None:
                row = conn.execute(
                    "SELECT status FROM claims WHERE id = ?", (run.claim,)
                ).fetchone()
                if row is not None and row[0] == "gone":
                    return "done", None
    except OSError, sqlite3.Error:
        pass
    holder = claim_holder(run.claim, run.env, git, skip=run.id)
    if holder is not None:
        return "finish", holder
    return "finish", f"next: emb choices --finish {shlex.quote(run.claim)}"


def _verify(run: Run, git: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    from ember.runs.core import RunError

    base_branch = (
        shlex.quote(run.base_branch) if run.base_branch else "<session branch>"
    )
    branch = shlex.quote(run.branch) if run.branch else "<branch>"
    directory = run.main or run.repo
    script = skill.verification_script(run.env)
    line = (
        f"next: python3 {shlex.quote(str(script))} --repo {shlex.quote(directory)} "
        f"--base {base_branch} --branch {branch} "
        f"--worktree {shlex.quote(run.worktree)}"
    )
    base, tip = record.first_base(run), record.work_tip(run)
    if run.turn == 1 and base is not None and tip is not None:
        try:
            # A continued run's branch holds the earlier runs' work too.
            result = git(directory, "diff", "--name-only", "-z", base, tip)
            if result.returncode == 0:
                count = len([path for path in result.stdout.split("\0") if path])
                line += f" --expect-files {count}"
        except RunError, OSError, subprocess.SubprocessError:
            pass
    return (
        f"{line}, then emb merge {branch} --into {base_branch} "
        "--verdict keep --note '<verdict>' --subject '<subject>'"
    )
