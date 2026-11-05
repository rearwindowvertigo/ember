"""Single worker runs: start, turns, commits and the commands on a run.

The core owns the run record, the worktree and branch, commits, exits and
the tmux session. A driver from `ember.engines` owns the engine's command
line and how its replies are read.

An editing run works in its own worktree, `worktrees/<id>` in Ember's data
folder (`ember.paths.worktree_path`), outside every repository, on the branch
`ember/<engine>/<id>` cut from the named checkout's `HEAD`. The run record
keeps the worktree's resolved path. A read-only run works in a writable
detached worktree and has no commit step. Closing it removes its clean
worktree, including ignored build and test files.
The core never pushes, merges, rebases or deletes a branch.
"""

import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from ember import choices, db, engines, paths, processes, sandbox, tmux
from ember.engines import Driver, LaunchSpec, TurnOutcome, models, status
from ember.engines.opencode2 import DeliveryUncertainError
from ember.runs import exits, record, steps, worktrees
from ember.runs.record import Run

POLL_SECONDS = 1.0
"""Seconds between two observations of a turn."""

STALL_MINUTES = 10.0
"""Minutes without a change on the session's screen before a waiting command
stops with exit 124."""

SUBJECT_LIMIT = 72
"""The longest commit subject taken from the brief, before ` (turn <n>)`."""

INCOMPLETE_REASONS = ("sign-in failure", "engine failure", "provider error", "no reply")
"""The reasons in the subject of a commit of an incomplete turn."""

GIT_TIMEOUT = 120
"""Seconds each git command may take."""

HOOK_REFUSAL_NAME = "hook-refusal.txt"
"""The full stdout and stderr of the run's refused commit."""

LAUNCH_NAME = "launch.sh"
"""The script in a run's folder that the run's tmux session runs.

tmux refuses a command longer than its message size, about 16 KiB, and a
sandboxed command holds the whole deny list, so the session runs this
script, which holds the command, instead.
"""

HOOK_OUTPUT_LINES = 200
HOOK_OUTPUT_BYTES = 64 * 1024

BRIEF_LINE = "Brief from Claude, the lead, for Ember run {id}."
"""The first line of the first message, above the brief."""

REPLY_MARK = "--- reply ---"
"""The line above the reply text in a turn's output."""

LIVE = "live"
DEAD = "dead"
MISSING = "missing"
SESSION_STATES = (LIVE, DEAD, MISSING)
"""The values of `session_state` in `--status`."""

DriverLoader = Callable[[str], Driver]


class RunError(Exception):
    """An exit of `emb run`, with its message and the context of its line."""

    def __init__(
        self, code: int, message: str, variant: str | None = None, **context: object
    ) -> None:
        """Keep the exit code, the line's variant and its placeholders.

        Args:
            code: The exit code.
            message: What happened.
            variant: Which of the exit's lines in `ember.runs.exits`.
            **context: Values for the line's placeholders.
        """
        super().__init__(message)
        self.code = code
        self.variant = variant
        self.context = context


def worktree_removal_reason(run: Run) -> str | None:
    """Return why a run's worktree must stay, or None when removal is allowed.

    Removal callers hold the run command and record locks and reload the
    record before checking. Read-only collected turns have no commit step.
    """
    if run.status not in (record.CLOSED, record.FAILED):
        return "not ended"
    if run.turn_state == record.SENT and not (
        run.status == record.CLOSED and run.read_only and run.detached
    ):
        return f"turn {run.turn} of {run.id} is sent or delivery is uncertain"
    if run.turn_state == record.COLLECTED and not run.read_only:
        return f"turn {run.turn} of {run.id} is collected but not committed"
    try:
        if tmux.exists(run.id, env=run.env) and not tmux.pane_dead(run.id, env=run.env):
            return f"session of {run.id} is still running"
    except (tmux.TmuxError, paths.RuntimeDirError, OSError) as error:
        return f"cannot verify session of {run.id} has ended: {error}"
    return None


@dataclass(frozen=True)
class StartOptions:
    """The options of a new run."""

    engine: str
    """`codex`, `opencode` or `claude`."""

    dir: str
    """A folder inside the git checkout to work on."""

    task_file: str
    """The brief."""

    model: str | None = None
    """The model; `None` takes it from Team."""

    effort: str | None = None
    """The effort; `None` takes it from Team or the driver."""

    read_only: bool = False
    """The run edits nothing."""

    read: tuple[str, ...] = ()
    """Paths the run may read, in addition to the `sandbox.read` setting."""

    trailers: tuple[str, ...] = ()
    """Trailer lines added to each commit."""

    claim: str | None = None
    """The claim id Team is read with."""

    window: bool = False
    """Open a terminal window on the session."""

    base: str | None = None
    """A saved commit for a panel member; `None` uses the checkout's HEAD."""

    dry_run: bool = False
    """Check inputs and resolve the worker without starting anything."""

    as_json: bool = False
    """Print a dry run's worker as JSON."""

    parent: dict[str, Any] | None = None
    """The starting command from `record.parent()`; `None` for `emb run` itself."""


@dataclass(frozen=True)
class WorkerSelection:
    """A resolved worker and the source of its model and effort."""

    engine: str
    model: str
    effort: str | None
    model_source: str
    effort_source: str
    row: str
    screen: str = "Team"

    def line(self) -> str:
        """Return the worker's announce line."""
        source = self.model_source
        if self.effort_source != self.model_source:
            source = f"model: {self.model_source}, effort: {self.effort_source}"
        return (
            f"{self.screen}, {self.row}: {self.engine} {self.model} · "
            f"{self.effort or 'default'} ({source})"
        )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def brief_subject(brief: str) -> str:
    """Return the subject line of a brief.

    Args:
        brief: The brief text.

    Returns:
        Its first non-empty line, without trailing white space; empty when
        the brief has none.
    """
    for line in brief.splitlines():
        if line.strip():
            return line.rstrip()
    return ""


def commit_subject(subject: str, turn: int, incomplete: str | None = None) -> str:
    """Return the commit subject of a turn.

    Args:
        subject: The brief's first line.
        turn: The turn number.
        incomplete: Why the turn did not end with a reply, one of
            `INCOMPLETE_REASONS`; `None` for a turn that did.

    Returns:
        The subject cut to 72 characters, with ` (turn <n>)` after turn 1, or
        with ` (turn <n>, incomplete: <reason>)` for an incomplete turn.
    """
    text = subject[:SUBJECT_LIMIT]
    if incomplete:
        return f"{text} (turn {turn}, incomplete: {incomplete})"
    return text if turn <= 1 else f"{text} (turn {turn})"


def incomplete_reason(outcome: TurnOutcome) -> str | None:
    """Return why an ended turn is incomplete.

    Args:
        outcome: The turn's outcome, `done` or `failed`.

    Returns:
        `provider error` (exit 73), `sign-in failure` (69), `engine failure`
        (71) or `no reply` (80); `None` for a turn that ended with a reply.
    """
    if outcome.state == engines.FAILED:
        if outcome.provider_error:
            return "provider error"
        if outcome.signed_out:
            return "sign-in failure"
        return "engine failure"
    return None if outcome.reply else "no reply"


def _outcome_exit(outcome: TurnOutcome) -> int:
    """Return the engine result's exit before commit handling."""
    return {
        None: exits.OK,
        "provider error": exits.PROVIDER_ERROR,
        "sign-in failure": exits.UNAVAILABLE,
        "engine failure": exits.ENGINE_FAILED,
        "no reply": exits.NO_REPLY,
    }[incomplete_reason(outcome)]


def first_message(run_id: str, brief: str) -> str:
    """Return the first message of a run: the attributed brief.

    Args:
        run_id: The run id.
        brief: The brief text.

    Returns:
        The line naming Claude as the brief's author, a blank line, the brief.
    """
    return f"{BRIEF_LINE.format(id=run_id)}\n\n{brief}"


class Core:
    """Runs `emb run` commands for one environment."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        load_driver: DriverLoader | None = None,
        stall_minutes: float = STALL_MINUTES,
        quiet: bool = False,
        poll_seconds: float | None = None,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        """Set up the core.

        Args:
            env: The environment; `None` reads `os.environ`.
            load_driver: Returns the driver for an engine; `None` for
                `ember.engines.load_driver`.
            stall_minutes: Minutes with no change on the screen before a
                waiting command exits 124.
            quiet: Print only the run id, worker and each turn's summary.
            poll_seconds: Seconds between observations; `None` for
                `POLL_SECONDS`.
            out: Standard output; `None` for `sys.stdout`.
            err: Standard error; `None` for `sys.stderr`.
        """
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.load_driver = load_driver or (
            lambda name: engines.load_driver(name, self.env)
        )
        self.stall_seconds = stall_minutes * 60
        self.quiet = quiet
        self.poll_seconds = POLL_SECONDS if poll_seconds is None else poll_seconds
        self.out = out
        self.err = err
        self.last_run_id: str | None = None
        self.status_next: str | None = None

    # Output

    def _say(self, text: str) -> None:
        print(text, file=self.out or sys.stdout, flush=True)

    def _warn(self, text: str) -> None:
        print(text, file=self.err or sys.stderr, flush=True)

    def execute(self, action: Callable[[], int]) -> int:
        """Run one command and turn its failure into an exit.

        A failure prints `emb run: <message>` and the exit's line on standard
        error.

        Args:
            action: The command.

        Returns:
            The exit code.
        """
        try:
            return action()
        except RunError as error:
            self._warn(f"emb run: {error}")
            exits.emit(
                error.code,
                error.variant,
                stream=self.err or sys.stderr,
                **error.context,
            )
            return error.code
        except KeyboardInterrupt:
            self._warn("emb run: interrupted")
            exits.emit(exits.INTERRUPTED, "starting", stream=self.err or sys.stderr)
            return exits.INTERRUPTED
        except Exception as error:  # noqa: BLE001
            self._warn(f"emb run: unexpected error: {type(error).__name__}: {error}")
            exits.emit(exits.UNEXPECTED, stream=self.err or sys.stderr)
            return exits.UNEXPECTED

    # Tools

    def _which(self, name: str) -> str | None:
        return shutil.which(name, path=self.env.get("PATH", os.defpath))

    def _git(self, cwd: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                env=dict(self.env),
                timeout=GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise RunError(
                exits.UNEXPECTED, f"git {args[0]} timed out after {GIT_TIMEOUT}s"
            ) from error
        except OSError as error:
            raise RunError(
                exits.UNAVAILABLE, f"git cannot run: {error}", "git"
            ) from error

    def _git_out(self, cwd: str | Path, *args: str) -> str | None:
        result = self._git(cwd, *args)
        return result.stdout.strip() if result.returncode == 0 else None

    def _session_state(self, run_id: str) -> str:
        try:
            if not tmux.exists(run_id, env=self.env):
                return MISSING
            return DEAD if tmux.pane_dead(run_id, env=self.env) else LIVE
        except tmux.TmuxError, paths.RuntimeDirError:
            return MISSING

    @contextlib.contextmanager
    def _turn_lock(self, run_id: str, variant: str | None = None) -> Iterator[None]:
        try:
            root = paths.runtime_dir(self.env)
            folder = paths.ensure_private_dir(root / "locks", root=root)
            fd = paths.open_private_file(folder / f"{run_id}.lock")
        except paths.RuntimeDirError as error:
            raise RunError(
                exits.UNAVAILABLE,
                f"cannot open the lock of {run_id}: {error}",
                "runtime",
            ) from error
        except OSError as error:
            raise RunError(
                exits.UNEXPECTED, f"cannot open the lock of {run_id}: {error}"
            ) from error
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RunError(
                    exits.REFUSED,
                    f"another emb run command is working on {run_id}",
                    variant,
                    run=run_id,
                ) from error
            yield
        finally:
            # A concurrent fork may retain the open file description until exec.
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load(self, run_id: str) -> Run:
        try:
            return record.load(run_id, self.env)
        except record.RecordMissingError as error:
            raise RunError(exits.USAGE, f"no run {run_id}", "no_run") from error
        except record.RecordError as error:
            raise RunError(exits.UNEXPECTED, str(error)) from error

    def _driver(self, engine: str) -> Driver:
        try:
            return self.load_driver(engine)
        except engines.DriverMissingError as error:
            raise RunError(
                exits.UNAVAILABLE, str(error), "driver", engine=engine
            ) from error

    @contextlib.contextmanager
    def _engine_calls(self, run: Run, variant: str | None = None) -> Iterator[None]:
        """Turn driver, tmux and runtime folder failures into exits 71, 72, 69."""
        try:
            yield
        except engines.DriverError as error:
            raise RunError(
                exits.ENGINE_FAILED,
                f"{run.id}: {error}",
                None if isinstance(error, DeliveryUncertainError) else variant,
                run=run.id,
            ) from error
        except paths.RuntimeDirError as error:
            raise RunError(
                exits.UNAVAILABLE, f"{run.id}: {error}", "runtime", run=run.id
            ) from error
        except tmux.TmuxError as error:
            raise RunError(
                exits.SESSION_GONE, f"{run.id}: {error}", variant, run=run.id
            ) from error

    def _update(self, run: Run, **changes: object) -> Run:
        try:
            return record.update(run, changes)
        except record.RunClosedError as error:
            raise RunError(
                exits.SESSION_GONE, f"{run.id} was closed", run=run.id
            ) from error

    def _record_exit(
        self, run: Run, code: int, message: str | None, **changes: object
    ) -> Run:
        """Record an exit; a closed run keeps its record."""
        try:
            return record.update(
                run, {**changes, "last_exit": code, "last_reason": message}
            )
        except record.RunClosedError, record.RecordError:
            return run

    # Settings and selection

    def _settings(self, claim: str | None) -> dict[str, Any]:
        try:
            reply = choices.read(claim, env=self.env)
        except choices.InvalidChoiceError as error:
            raise RunError(exits.USAGE, str(error), "claim") from error
        except db.DatabaseError as error:
            raise RunError(exits.UNEXPECTED, str(error), "database") from error
        if claim is not None and reply["claim"]["status"] in ("gone", "unknown"):
            raise RunError(
                exits.USAGE,
                f"claim {claim} is {reply['claim']['status']}",
                "claim",
            )
        return reply

    def _check_connected(self, engine: str, *, cache: bool = True) -> None:
        held = status.cached(self.env)
        if held is None and cache:
            held = status.read(self.env)
        item = status.check(engine, self.env) if held is None else held[engine]
        if item.connected:
            return
        fix = next(
            (req.fix for req in item.requirements if not req.met and req.fix), None
        )
        raise RunError(
            exits.UNAVAILABLE,
            f"{engine} is not connected: {item.reason or item.state}",
            "fix" if fix else None,
            engine=engine,
            fix=fix,
        )

    def _select(
        self, options: StartOptions, settings: Mapping[str, Any]
    ) -> tuple[Driver, WorkerSelection]:
        """Return the driver and resolved worker, including option sources."""
        engine = options.engine
        answers = settings["answers"]
        if answers[f"engine.{engine}"]["value"] != "on":
            raise RunError(
                exits.USAGE, f"{engine} is switched off", "engine_off", engine=engine
            )
        self._check_connected(engine, cache=not options.dry_run)
        driver = self._driver(engine)
        name = "single.reviewer" if options.read_only else "single.writer"
        menu = settings["menu"]
        row: Mapping[str, Any] | None = None if menu is None else menu[name]
        model = options.model
        model_source = name if model is None else "--model"
        if (
            model is not None
            and row is not None
            and row.get("engine") == "claude"
            and row.get("run_as") == "subagent"
        ):
            # A subagent row is never read by emb run.
            row = None
        if model is None:
            if row is None:
                errors = "; ".join(settings["errors"]) or "not valid"
                raise RunError(
                    exits.USAGE,
                    f"the settings cannot be read: {errors}",
                    "menu",
                    row=name,
                )
            if row["state"] != "set":
                raise RunError(
                    exits.USAGE,
                    f"menu row {name} is {row['state']}: {row['reason']}",
                    "menu",
                    row=name,
                )
            if row["engine"] != engine:
                raise RunError(
                    exits.USAGE,
                    f"menu row {name} names {row['engine']}, not {engine}",
                    "menu",
                    row=name,
                )
            if engine == "claude" and row["run_as"] == "subagent":
                raise RunError(
                    exits.USAGE,
                    f"menu row {name} runs as a subagent inside Claude, "
                    "not through emb run",
                    "subagent",
                )
            model = str(row["model"])
        effort = options.effort
        effort_source = "--effort" if effort is not None else model_source
        if (
            effort is None
            and row is not None
            and row["state"] == "set"
            and row["engine"] == engine
            and row["model"] == model
        ):
            effort = row["effort"]
            if effort is not None:
                effort_source = name
        with (
            models.without_cache_writes()
            if options.dry_run
            else contextlib.nullcontext()
        ):
            if effort is None:
                effort = driver.default_effort(model)
            if effort is not None and not driver.valid_effort(effort):
                raise RunError(
                    exits.USAGE, f"{engine} does not take the effort {effort!r}"
                )
        return driver, WorkerSelection(
            engine=engine,
            model=model,
            effort=effort,
            model_source=model_source,
            effort_source=effort_source,
            row="Single tasks Reviewer" if options.read_only else "Single tasks Writer",
        )

    # Checkout and worktree

    @contextlib.contextmanager
    def _worktree_lock(self, common: Path) -> Iterator[None]:
        """Serialize worktree registration, listing and removal per repository."""
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(paths.worktree_lock(common, self.env))
            except paths.RuntimeDirError as error:
                raise RunError(exits.UNAVAILABLE, str(error), "runtime") from error
            except OSError as error:
                raise RunError(
                    exits.UNEXPECTED,
                    f"cannot open the worktree lock of {common}: {error}",
                ) from error
            yield

    def _checkouts(self, top: str) -> tuple[Path | None, list[Path]]:
        """Return the main checkout, if non-bare, and every working checkout."""
        result = self._git(top, "worktree", "list", "--porcelain", "-z")
        if result.returncode:
            raise RunError(
                exits.WORKTREE_FAILED,
                f"git worktree list failed in {top}: {_first_line(result.stderr)}",
                dir=top,
            )
        blocks = [
            [item for item in block.split("\0") if item]
            for block in result.stdout.split("\0\0")
        ]
        blocks = [block for block in blocks if block]
        first = blocks[0] if blocks else []
        if not first or not first[0].startswith("worktree "):
            raise RunError(
                exits.WORKTREE_FAILED,
                f"the repository of {top} has no main checkout",
                dir=top,
            )
        found = [
            Path(block[0].removeprefix("worktree "))
            for block in blocks
            if block[0].startswith("worktree ") and "bare" not in block
        ]
        main = None if "bare" in first else Path(first[0].removeprefix("worktree "))
        return main, found

    def _worktree_path(self, run_id: str, checkouts: list[Path], common: Path) -> Path:
        """Return a new run's worktree path, outside every repository folder.

        Raises:
            RunError: The worktree folder is inside a checkout of the
                repository or its common git folder (69).
        """
        worktree = paths.worktree_path(run_id, self.env)
        try:
            paths.check_worktree_path(worktree, [*checkouts, common], self.env)
        except paths.WorktreeDirError as error:
            setting = paths.worktree_setting(self.env)
            raise RunError(
                exits.UNAVAILABLE,
                str(error),
                "fix",
                fix=f"set {setting} so that {paths.worktrees_dir(self.env)} is "
                "outside every git checkout",
            ) from error
        return worktree

    def _create_worktree(self, run: Run, main: Path) -> None:
        try:
            paths.ensure_worktrees_dir(self.env)
        except (OSError, ValueError) as error:
            raise RunError(
                exits.WORKTREE_FAILED,
                f"cannot create the worktree folder "
                f"{paths.worktrees_dir(self.env)}: {error}",
                dir=str(main),
            ) from error
        assert run.base is not None
        mode = ("--detach",) if run.read_only else ("-b", str(run.branch))
        assert run.common is not None
        with self._worktree_lock(Path(run.common or run.repo)):
            result = self._git(
                main, "worktree", "add", "-q", *mode, run.worktree, run.base
            )
        if result.returncode:
            raise RunError(
                exits.WORKTREE_FAILED,
                f"git worktree add failed for {run.worktree}: "
                f"{_first_line(result.stderr)}",
                dir=str(main),
            )

    # Sandbox

    def _sandbox_provider(self, engine: str) -> str:
        try:
            return sandbox.method(engine, self.env)
        except db.DatabaseError as error:
            raise RunError(exits.UNEXPECTED, str(error), "database") from error

    def _require_sandbox(self, engine: str, provider: str) -> None:
        """Refuse with 69 a sandboxed run whose `bwrap` is missing or blocked."""
        if provider == sandbox.NONE:
            return
        try:
            sandbox.require(engine, self.env)
        except (sandbox.SandboxError, paths.RuntimeDirError) as error:
            raise RunError(
                exits.UNAVAILABLE,
                f"the {engine} sandbox cannot start: {error}",
                "bwrap",
                engine=engine,
            ) from error

    def _sandboxed(self, spec: LaunchSpec, run: Run, common: Path) -> LaunchSpec:
        """Wrap a driver's command in the run's sandbox.

        A Claude command keeps the driver's argv; its program wraps each
        turn. Detached review worktrees are writable; legacy read-only source
        checkouts retain filesystem protection.
        """
        if run.sandbox == sandbox.NONE or run.engine == "claude":
            return spec
        worktree = Path(run.worktree)
        program = spec.binary or (spec.argv[0] if spec.argv else None)
        binary = self._which(program) if program else None
        if run.engine == "codex":
            options = sandbox.codex_args(
                worktree,
                common,
                run.read_effective,
                read_only=run.read_only and not run.detached,
                network=run.network,
                env=self.env,
            )
            env_vars = dict(spec.env)
            try:
                _, folder = sandbox.codex_bwrap(Path(binary or spec.argv[0]), self.env)
            except sandbox.SandboxError as error:
                raise RunError(
                    exits.UNAVAILABLE, str(error), "bwrap", engine=run.engine
                ) from error
            if folder is not None:
                path = env_vars.get("PATH", self.env.get("PATH", os.defpath))
                env_vars["PATH"] = f"{folder}{os.pathsep}{path}"
            return replace(
                spec, argv=(spec.argv[0], *options, *spec.argv[1:]), env=env_vars
            )
        argv = sandbox.opencode_wrap(
            spec.argv,
            worktree=worktree,
            common=common,
            read=run.read_effective,
            read_only=run.read_only and not run.detached,
            binary=None if binary is None else Path(binary),
            env=self.env,
        )
        return replace(spec, argv=tuple(argv))

    def _check_foreign(self, run: Run, driver: Driver, variant: str) -> None:
        """Refuse with 75 a session holding messages Ember did not send."""
        with self._engine_calls(run, variant):
            found = driver.foreign_input(run)
        if found:
            raise RunError(
                exits.REFUSED,
                f"the session of {run.id} holds messages Ember did not send "
                f"({found}); Claude asks the owner before going on",
                "foreign",
                run=run.id,
            )

    # Read-only snapshots

    def _snapshot(self, worktree: str) -> dict[str, list[str | None]]:
        result = self._git(
            worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        if result.returncode:
            return {}
        entries = result.stdout.split("\0")
        state: dict[str, list[str | None]] = {}
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            code, name = entry[:2], entry[3:]
            names = [name]
            if code[0] in "RC" and index < len(entries):
                names.append(entries[index])
                index += 1
            for item in names:
                state[item] = [code, _hash(Path(worktree) / item)]
        return state

    def _snapshot_path(self, run: Run, turn: int) -> Path:
        return run.folder / f"turn-{turn}.status.json"

    def _save_snapshot(self, run: Run, turn: int) -> None:
        state = self._snapshot(run.worktree)
        record.write_private(self._snapshot_path(run, turn), json.dumps(state))

    def _changed(self, run: Run) -> list[str]:
        try:
            before = json.loads(self._snapshot_path(run, run.turn).read_text())
        except OSError, ValueError:
            before = {}
        after = self._snapshot(run.worktree)
        return sorted(
            name for name in {*before, *after} if before.get(name) != after.get(name)
        )

    # Start

    def start(self, options: StartOptions) -> int:
        """Check and preview a worker, or start a run and wait for its first turn.

        Args:
            options: The run's options.

        Returns:
            The exit code.

        Raises:
            RunError: The run cannot start, or its first turn did not end
                with exit 0.
        """
        self.last_run_id = None
        engine = options.engine
        for name in (options.dir, options.task_file, *options.read):
            if not os.path.exists(name):
                raise RunError(exits.NO_INPUT, f"{name} does not exist")
        try:
            brief = Path(options.task_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise RunError(
                exits.NO_INPUT, f"cannot read {options.task_file}: {error}"
            ) from error
        subject = brief_subject(brief)
        if not subject:
            raise RunError(exits.USAGE, f"the brief {options.task_file} is empty")
        for trailer in options.trailers:
            if not trailer.strip() or "\n" in trailer:
                raise RunError(exits.USAGE, f"--trailer {trailer!r} is not one line")
        if self._which("git") is None:
            raise RunError(exits.UNAVAILABLE, "git is not on PATH", "git")
        if self._which("tmux") is None:
            raise RunError(exits.UNAVAILABLE, "tmux is not on PATH", "tmux")
        folder = Path(options.dir).resolve()
        top = self._git_out(folder, "rev-parse", "--show-toplevel")
        if not top:
            raise RunError(exits.NOT_A_CHECKOUT, f"{folder} is not in a git checkout")
        settings = self._settings(options.claim)
        driver, worker = self._select(options, settings)
        common_dir = self._git_out(
            top, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        if not common_dir:
            raise RunError(exits.NOT_A_CHECKOUT, f"cannot find the git folder of {top}")
        common = Path(common_dir).resolve()
        self._check_codex_trust(engine, common)
        if options.base is not None and not options.read_only:
            raise RunError(exits.USAGE, "a saved base requires a read-only run")
        base = self._git_out(
            top, "rev-parse", "-q", "--verify", f"{options.base or 'HEAD'}^{{commit}}"
        )
        base_branch = self._git_out(top, "symbolic-ref", "-q", "--short", "HEAD")
        with self._worktree_lock(common):
            main_checkout, checkouts = self._checkouts(top)
        main = Path(top) if options.read_only else main_checkout
        if main is None or not main.is_dir():
            raise RunError(
                exits.WORKTREE_FAILED,
                f"the repository of {top} has no main checkout"
                if main is None
                else f"the main checkout {main} of {top} does not exist",
                dir=top,
            )
        if base is None:
            raise RunError(
                exits.WORKTREE_FAILED,
                f"{top} has no commit to start from"
                if options.base is None
                else f"{top} does not have the saved commit {options.base}",
                dir=top,
            )
        if options.dry_run:
            self._say(json.dumps(asdict(worker)) if options.as_json else worker.line())
            return exits.OK
        run_id = record.new_id(engine)
        worktree = str(self._worktree_path(run_id, checkouts, common))
        branch = None if options.read_only else f"ember/{engine}/{run_id}"
        provider = self._sandbox_provider(engine)
        answers = settings["answers"]
        read = tuple(str(Path(path).resolve()) for path in options.read)
        read_effective = tuple(
            dict.fromkeys([*answers["sandbox.read"]["value"], *read])
        )
        self._check_kept(engine, provider, worktree, common, read_effective)
        self._check_codex_config(engine, provider, worktree, common)
        self._require_sandbox(engine, provider)
        self._create_worker_cache(provider)
        now = time.time()
        with self._turn_lock(run_id):
            run = Run(
                id=run_id,
                engine=engine,
                capacity_retries=int(answers["provider.capacity_retries"]["value"]),
                capacity_wait=int(answers["provider.capacity_wait"]["value"]),
                model=worker.model,
                effort=worker.effort,
                read_only=options.read_only,
                detached=options.read_only,
                repo=top,
                main=str(main_checkout) if main_checkout is not None else None,
                common=str(common),
                worktree=worktree,
                branch=branch,
                base=base,
                base_branch=base_branch,
                sandbox=provider,
                network=answers["worker.network"]["value"] == "on",
                read=read,
                read_effective=read_effective,
                trailers=tuple(options.trailers),
                claim=options.claim,
                status=record.STARTING,
                turn=0,
                turn_state=None,
                reply=None,
                expected_head=None,
                started_at=now,
                updated_at=now,
                ended_at=None,
                last_exit=None,
                last_reason=None,
                commits=(),
                subject=subject,
                parent=options.parent,
                folder=record.make_run_dir(run_id, self.env),
                env=self.env,
            )
            started = time.monotonic()
            try:
                record.write_private(run.folder / record.BRIEF_NAME, brief)
                record.save(run)
                self.last_run_id = run.id
                self._say(f"run {run_id}")
                self._warn(worker.line())
                if run.read_only:
                    for name in self._snapshot(top):
                        self._warn(
                            f"WARNING: read-only run {run.id} "
                            f"does not see uncommitted {name}"
                        )
                run, window = self._launch(
                    run, driver, brief, options, settings, common, main
                )
            except RunError as error:
                _close_quietly(driver, run)
                self._record_exit(run, error.code, str(error), status=record.FAILED)
                raise
            except KeyboardInterrupt as error:
                _close_quietly(driver, run)
                self._record_exit(
                    run, exits.INTERRUPTED, "interrupted", status=record.FAILED
                )
                raise RunError(
                    exits.INTERRUPTED,
                    f"interrupted before {run_id} took its brief",
                    "starting",
                    run=run_id,
                ) from error
            except Exception as error:
                _close_quietly(driver, run)
                failure = _start_failure(run, error)
                self._record_exit(run, failure.code, str(failure), status=record.FAILED)
                raise failure from error
            return self._turn(run, driver, started, window)

    def _check_codex_trust(self, engine: str, common: Path) -> None:
        """Refuse an untrusted Codex repository before creating launch artifacts."""
        if engine != "codex":
            return
        from ember.engines.codex import CodexDriver

        root = common.parent
        try:
            CodexDriver(self.env).check_trust(root, self.env)
        except engines.DriverError as error:
            raise RunError(exits.UNAVAILABLE, str(error), "trust", dir=root) from error

    def _check_kept(
        self,
        engine: str,
        provider: str,
        worktree: str,
        common: Path,
        read: tuple[str, ...],
    ) -> None:
        """Refuse a sandboxed run whose kept path lies inside Ember's folders."""
        if provider == sandbox.NONE:
            return
        cache = paths.worker_cache_dir(self.env)
        try:
            sandbox.check_kept(Path(worktree), common, read, env=self.env)
        except sandbox.KeptPathError as error:
            read_paths = {Path(item) for item in read}
            read_paths |= {Path(item).resolve() for item in read}
            if error.path in read_paths:
                fix = f"leave {error.path} out of sandbox.read and --read"
            elif error.path.is_relative_to(paths.worktrees_dir(self.env).resolve()):
                fix = (
                    f"set {paths.worktree_setting(self.env)} so that "
                    f"{paths.worktrees_dir(self.env)} is outside {error.folder}"
                )
            elif error.path in (cache, cache.resolve()):
                fix = f"set XDG_CACHE_HOME so that {cache} is outside {error.folder}"
            else:
                fix = f"move {error.path} out of {error.folder}"
            raise RunError(
                exits.UNAVAILABLE,
                f"the {engine} sandbox cannot start: {error}",
                "fix",
                fix=f"{fix}, or set sandbox.{engine} to off",
            ) from error

    def _create_worker_cache(self, provider: str) -> None:
        """Create the workers' cache folder, or refuse a sandboxed run with 69."""
        if provider == sandbox.NONE:
            return
        try:
            paths.ensure_worker_cache_dir(self.env)
        except (OSError, ValueError) as error:
            folder = paths.worker_cache_dir(self.env)
            raise RunError(
                exits.UNAVAILABLE,
                f"cannot create the workers' cache folder {folder}: {error}",
                "fix",
                fix=f"make {folder} a folder you can write, or set XDG_CACHE_HOME",
            ) from error

    def _check_codex_config(
        self, engine: str, provider: str, worktree: str, common: Path
    ) -> None:
        """Refuse a sandboxed Codex run that a `sandbox_mode` key would undo."""
        if engine != "codex" or provider == sandbox.NONE:
            return
        conflicts = sandbox.codex_config_conflicts(Path(worktree), common, self.env)
        if conflicts:
            files = ", ".join(str(path) for path in conflicts)
            raise RunError(
                exits.UNAVAILABLE,
                f"a sandbox_mode key would turn off the codex sandbox: {files}",
                "sandbox",
                files=files,
            )

    def _launch(
        self,
        run: Run,
        driver: Driver,
        brief: str,
        options: StartOptions,
        settings: Mapping[str, Any],
        common: Path,
        main: Path | None,
    ) -> tuple[Run, str | None]:
        """Create the worktree and session and send the first message."""
        if main is not None:
            self._create_worktree(run, main)
            # The new worktree can hold a tracked .codex/config.toml.
            self._check_codex_config(run.engine, run.sandbox, run.worktree, common)
        if run.sandbox == sandbox.NONE:
            self._warn(sandbox.none_warning(run.engine))
        with self._engine_calls(run, "starting"):
            spec = driver.command(run, first_message(run.id, brief))
        if run.sandbox != sandbox.NONE:
            program = spec.binary or (spec.argv[0] if spec.argv else run.engine)
            binary = self._which(run.engine if run.engine == "claude" else program)
            run = self._update(
                run,
                sandbox_paths=sandbox.hidden_snapshot(
                    Path(run.worktree),
                    common,
                    run.read_effective,
                    engine=run.engine,
                    binary=Path(binary) if binary else None,
                    env=self.env,
                ),
            )
        spec = self._sandboxed(spec, run, common)
        if run.read_only:
            self._save_snapshot(run, 1)
        if spec.first_message is None:
            run = self._turn_sent(run, 1, first_message(run.id, brief))
        launch = run.folder / LAUNCH_NAME
        record.write_private(launch, f"exec {shlex.join(spec.argv)}\n")
        try:
            tmux.new_session(
                run.id,
                cwd=spec.cwd,
                argv=("/bin/sh", str(launch)),
                env_vars=spec.env,
                env=self.env,
            )
        except paths.RuntimeDirError as error:
            raise RunError(
                exits.UNAVAILABLE,
                f"cannot start the session of {run.id}: {error}",
                "runtime",
                run=run.id,
            ) from error
        except tmux.TmuxError as error:
            raise RunError(
                exits.ENGINE_FAILED,
                f"cannot start the session of {run.id}: {error}",
                "starting",
                run=run.id,
            ) from error
        window = self._open_window(run, settings) if options.window else None
        if spec.first_message is not None:
            self._wait_ready(run, driver)
            run = self._turn_sent(run, 1, first_message(run.id, brief))
            with self._engine_calls(run):
                driver.send(run, spec.first_message)
        return run, window

    def _turn_sent(self, run: Run, turn: int, text: str) -> Run:
        self._save_screen_baseline(run, turn)
        record.write_private(run.folder / f"turn-{turn}.prompt.md", text)
        return self._update(
            run,
            status=record.RUNNING,
            turn=turn,
            turn_state=record.SENT,
            reply=None,
            expected_head=None
            if run.read_only
            else (run.expected_head if turn > 1 else run.base),
        )

    def _save_screen_baseline(self, run: Run, turn: int) -> None:
        """Save the pane before an attempt is delivered, including for recovery."""
        if run.sandbox == sandbox.NONE:
            return
        screen = "" if turn == 1 and run.turn == 0 else None
        with contextlib.suppress(
            engines.DriverError, tmux.TmuxError, OSError, paths.RuntimeDirError
        ):
            screen = self._optional_driver(run.engine).screen(run)
        record.write_private(
            run.folder / f"turn-{turn}.screen.json", json.dumps(screen)
        )

    def _attempt_screen(self, run: Run, driver: Driver) -> str:
        """Return pane lines added since this attempt's delivery boundary."""
        try:
            before = json.loads(
                (run.folder / f"turn-{run.turn}.screen.json").read_text()
            )
        except OSError, ValueError:
            return ""
        # An unknown boundary must not attribute old pane contents to a turn.
        if not isinstance(before, str):
            return ""
        after = driver.screen(run).splitlines()
        comparison = difflib.SequenceMatcher(
            None, before.splitlines(), after, autojunk=False
        )
        return "\n\n".join(
            "\n".join(after[start:end])
            for kind, _, _, start, end in comparison.get_opcodes()
            if kind in ("insert", "replace")
        )

    def _open_window(self, run: Run, settings: Mapping[str, Any]) -> str | None:
        terminal = settings["answers"]["terminal"]["value"]
        try:
            return str(tmux.open_window(run.id, terminal=terminal, env=self.env))
        except (tmux.TmuxError, paths.RuntimeDirError) as error:
            self._warn(f"WARNING: no window for {run.id}: {error}")
            return None

    def _wait_ready(self, run: Run, driver: Driver) -> None:
        last_screen: str | None = None
        last_change = time.monotonic()
        while True:
            try:
                with self._engine_calls(run, "starting"):
                    if driver.ready(run):
                        return
            except RunError:
                self._stop_if_closed(run)
                raise
            state = self._session_state(run.id)
            if state != LIVE:
                self._stop_if_closed(run)
                raise RunError(
                    exits.ENGINE_FAILED,
                    f"the engine of {run.id} exited before it took its brief",
                    "starting",
                    run=run.id,
                )
            screen = self._screen_text(run.id, last_screen)
            if screen != last_screen:
                last_screen, last_change = screen, time.monotonic()
                run = self._update(run, last_screen_change_at=time.time())
            elif time.monotonic() - last_change >= self.stall_seconds:
                raise RunError(
                    exits.ENGINE_FAILED,
                    f"the engine of {run.id} did not become ready",
                    "starting",
                    run=run.id,
                )
            time.sleep(self.poll_seconds)

    def _screen_text(self, run_id: str, previous: str | None) -> str | None:
        try:
            return tmux.capture(run_id, env=self.env)
        except tmux.TmuxError, paths.RuntimeDirError:
            return previous

    # Turns

    @contextlib.contextmanager
    def _collection(self, run: Run, started: float) -> Iterator[float]:
        """Keep one result and yield a start adjusted by prior collection time."""
        saved = self._saved_outcome(run)
        seconds = saved.get("seconds") or 0.0
        if saved.get("retry_clock") is not None:
            until = time.time()
            marker = self._retry_cancel_path(run)
            if marker.exists():
                until = min(until, marker.stat().st_mtime)
            seconds += max(0, until - saved["retry_clock"])
            saved["retry_clock"] = time.time()
        started -= seconds
        data = {
            **dict.fromkeys(
                ("state", "reason", "provider_error", "signed_out", "incomplete")
            ),
            **saved,
            "exit": None,
            "seconds": time.monotonic() - started,
        }
        self._write_outcome(run, data)
        try:
            yield started
        finally:
            data = self._saved_outcome(run)
            if data.get("exit") is None:
                self._write_outcome(
                    run,
                    {
                        **data,
                        "seconds": time.monotonic() - started,
                        **(
                            {"retry_clock": time.time()}
                            if data.get("retry_clock")
                            else {}
                        ),
                    },
                )

    def _turn(
        self, run: Run, driver: Driver, started: float, window: str | None
    ) -> int:
        """Wait for the current turn, collect it and commit it."""
        with self._collection(run, started) as started:
            try:
                outcome = self._capacity_turn(run, driver, started)
            except KeyboardInterrupt as error:
                saved = self._saved_outcome(run)
                attempts = saved.get("attempts", [])
                if (
                    attempts
                    and not attempts[-1]["sent"]
                    and not attempts[-1]["sending"]
                ):
                    record.write_private(self._retry_cancel_path(run), "cancelled")
                    self._record_exit(
                        run, exits.INTERRUPTED, "capacity resend cancelled"
                    )
                    raise RunError(
                        exits.INTERRUPTED,
                        "capacity resend cancelled",
                        "retry",
                        run=run.id,
                    ) from error
                if attempts and attempts[-1]["sending"] and not attempts[-1]["sent"]:
                    message = "capacity retry delivery is uncertain; ask the lead"
                    self._record_exit(run, exits.INTERRUPTED, message)
                    raise RunError(
                        exits.INTERRUPTED, message, "uncertain", run=run.id
                    ) from error
                self._record_exit(run, exits.INTERRUPTED, "stopped waiting")
                raise RunError(
                    exits.INTERRUPTED,
                    f"stopped waiting; turn {run.turn} of {run.id} keeps running",
                    run=run.id,
                ) from error
            saved = self._saved_outcome(run)
            attempts = saved.get("attempts", [])
            if attempts and attempts[-1]["sending"] and not attempts[-1]["sent"]:
                # Delivery may have started a worker that is still editing.
                # Keep its work uncommitted until the lead inspects the session.
                seconds = time.monotonic() - started
                self._save_outcome(run, outcome, seconds)
                self._write_outcome(
                    run,
                    {**self._saved_outcome(run), "exit": exits.PROVIDER_ERROR},
                )
                self._record_exit(
                    run, exits.PROVIDER_ERROR, outcome.reason, status=record.FAILED
                )
                self._warn_denial_owners(run, exits.PROVIDER_ERROR)
                raise RunError(
                    exits.PROVIDER_ERROR, outcome.reason or "", "uncertain", run=run.id
                )
            return self._finish(run, driver, outcome, started, window)

    def _retry_cancel_path(self, run: Run) -> Path:
        """Return the cancellation marker of this turn's pending resend."""
        return run.folder / f"turn-{run.turn}.retry-cancelled"

    def _attempt(self, sent: bool) -> dict[str, Any]:
        """Make an attempt journal entry before delivery."""
        return {
            "id": secrets.token_hex(12),
            "sent": sent,
            "sending": False,
            "provider_kind": None,
            "result": None,
            "seconds": 0.0,
        }

    def _capacity_turn(self, run: Run, driver: Driver, started: float) -> TurnOutcome:
        """Observe attempts and resend capacity refusals within the saved budget."""
        saved = self._saved_outcome(run)
        attempts = saved.get("attempts")
        if not attempts:
            attempts = [self._attempt(True)]
            attempts[0]["seconds"] = time.monotonic() - started
        self._write_outcome(run, {**saved, "attempts": attempts})
        while True:
            attempt = attempts[-1]
            if not attempt["sent"]:
                previous = self._recovered_outcome(run, None)
                if attempt["sending"]:
                    return TurnOutcome(
                        engines.FAILED,
                        reason="capacity retry delivery is uncertain; ask the lead",
                        provider_error=True,
                        provider_kind="capacity",
                    )
                if self._retry_cancel_path(run).exists():
                    return previous
                deadline = self._saved_outcome(run).get("retry_at", time.time())
                while time.time() < deadline:
                    self._stop_if_closed(run)
                    if self._retry_cancel_path(run).exists():
                        return previous
                    saved = self._saved_outcome(run)
                    self._write_outcome(
                        run,
                        {
                            **saved,
                            "seconds": time.monotonic() - started,
                            "retry_clock": time.time(),
                        },
                    )
                    time.sleep(min(self.poll_seconds, max(0, deadline - time.time())))
                # Interrupt and delivery share the record lock, so cancellation
                # either prevents delivery or interrupts the delivered attempt.
                with record.record_lock(run):
                    self._stop_if_closed(run)
                    if self._retry_cancel_path(run).exists():
                        return previous
                    try:
                        text = (run.folder / f"turn-{run.turn}.prompt.md").read_text(
                            encoding="utf-8"
                        )
                    except OSError:
                        return TurnOutcome(
                            engines.FAILED,
                            reason="capacity resend stopped: missing original prompt",
                            provider_error=True,
                            provider_kind="capacity",
                        )
                    self._save_screen_baseline(run, run.turn)
                    attempt["sending"] = True
                    delivery_started = time.monotonic()
                    saved = self._saved_outcome(run)
                    self._write_outcome(
                        run, {**saved, "attempts": attempts, "retry_clock": None}
                    )
                    try:
                        with self._engine_calls(run):
                            driver.send(run, text)
                    except RunError:
                        return TurnOutcome(
                            engines.FAILED,
                            reason="capacity retry delivery is uncertain; ask the lead",
                            provider_error=True,
                            provider_kind="capacity",
                        )
                    attempt["seconds"] += time.monotonic() - delivery_started
                    attempt["sent"] = True
                    attempt["sending"] = False
                    self._write_outcome(
                        run,
                        {
                            **saved,
                            "attempts": attempts,
                            "retry_at": None,
                            "retry_clock": None,
                        },
                    )
            attempt_started = time.monotonic() - attempt["seconds"]
            try:
                outcome = self._wait_turn(run, driver)
            finally:
                attempt["seconds"] = time.monotonic() - attempt_started
                saved = self._saved_outcome(run)
                self._write_outcome(run, {**saved, "attempts": attempts})
            attempt.update(
                result=outcome.state,
                provider_kind=outcome.provider_kind
                or ("outage" if outcome.provider_error else None),
            )
            self._save_outcome(run, outcome, time.monotonic() - started)
            saved = self._saved_outcome(run)
            self._write_outcome(run, {**saved, "attempts": attempts})
            if (
                not outcome.provider_error
                or outcome.provider_kind != "capacity"
                or len(attempts) > run.capacity_retries
                or self._retry_cancel_path(run).exists()
            ):
                return outcome
            if not (run.folder / f"turn-{run.turn}.prompt.md").is_file():
                return outcome
            attempts.append(self._attempt(False))
            self._write_outcome(
                run,
                {
                    **self._saved_outcome(run),
                    "attempts": attempts,
                    "retry_at": time.time() + run.capacity_wait,
                    "retry_clock": time.time(),
                    "seconds": time.monotonic() - started,
                },
            )
            self._warn(
                f"WARNING: {run.engine} {run.model} at capacity; "
                f"retry {len(attempts) - 1} of {run.capacity_retries} "
                f"in {run.capacity_wait}s"
            )

    def _finish(
        self,
        run: Run,
        driver: Driver,
        outcome: TurnOutcome,
        started: float,
        window: str | None,
    ) -> int:
        """Collect and commit an ended turn; Ctrl-C leaves it to `--recover`."""
        try:
            return self._complete(run, driver, outcome, started, window)
        except KeyboardInterrupt as error:
            message = (
                f"interrupted while collecting turn {run.turn} of {run.id}; "
                "its saved state is kept"
            )
            self._record_exit(run, exits.INTERRUPTED, message)
            raise RunError(
                exits.INTERRUPTED, message, "complete", run=run.id
            ) from error

    def _stop_if_closed(self, run: Run) -> None:
        """Stop with 72 when the stored record is closed."""
        try:
            stored = record.load(run.id, self.env)
        except record.RecordError:
            return
        if stored.status == record.CLOSED:
            raise RunError(exits.SESSION_GONE, f"{run.id} was closed", run=run.id)

    def _observe(self, run: Run, driver: Driver) -> TurnOutcome:
        try:
            with self._engine_calls(run):
                return driver.wait(run)
        except RunError as error:
            self._stop_if_closed(run)
            self._record_exit(run, error.code, str(error), status=record.FAILED)
            raise

    def _wait_turn(self, run: Run, driver: Driver) -> TurnOutcome:
        last_screen: str | None = None
        last_change = time.monotonic()
        checkpoint = time.monotonic()
        while True:
            now = time.monotonic()
            elapsed, checkpoint = now - checkpoint, now
            saved = self._saved_outcome(run)
            if saved.get("attempts"):
                saved["attempts"][-1]["seconds"] += elapsed
                saved["seconds"] = (saved.get("seconds") or 0.0) + elapsed
                self._write_outcome(run, saved)
            outcome = self._observe(run, driver)
            if outcome.state != engines.RUNNING:
                return outcome
            stored = self._load(run.id)
            if stored.status == record.CLOSED:
                raise RunError(exits.SESSION_GONE, f"{run.id} was closed", run=run.id)
            state = self._session_state(run.id)
            if state != LIVE:
                self._stop_if_closed(run)
                outcome = self._observe(run, driver)
                if outcome.state != engines.RUNNING:
                    return outcome
                if state == DEAD:
                    # A failed turn: its edits are collected and committed.
                    message = f"the engine of {run.id} exited before the turn ended"
                    self._record_exit(
                        run,
                        exits.ENGINE_FAILED,
                        message,
                        status=record.FAILED,
                        ended_at=time.time(),
                    )
                    return TurnOutcome(engines.FAILED, reason=message)
                message = f"the session of {run.id} is gone"
                self._record_exit(
                    run,
                    exits.SESSION_GONE,
                    message,
                    status=record.FAILED,
                    ended_at=time.time(),
                )
                raise RunError(exits.SESSION_GONE, message, run=run.id)
            screen = self._screen_text(run.id, last_screen)
            if screen != last_screen:
                last_screen, last_change = screen, time.monotonic()
                run = self._update(run, last_screen_change_at=time.time())
            elif time.monotonic() - last_change >= self.stall_seconds:
                minutes = f"{self.stall_seconds / 60:g}"
                message = (
                    f"the screen of {run.id} did not change for {minutes} min; "
                    "the session is left running"
                )
                self._record_exit(run, exits.STALLED, message)
                raise RunError(exits.STALLED, message, run=run.id)
            time.sleep(self.poll_seconds)

    def _complete(
        self,
        run: Run,
        driver: Driver,
        outcome: TurnOutcome,
        started: float,
        window: str | None,
    ) -> int:
        """Collect an ended turn, commit it and print its summary."""
        reply = outcome.reply if outcome.state == engines.DONE else None
        if (
            run.turn_state == record.SENT
            or self._saved_outcome(run).get("state") is None
        ):
            self._save_outcome(run, outcome, time.monotonic() - started)
        if run.turn_state == record.SENT:
            reply_path = None
            if reply:
                path = run.folder / f"turn-{run.turn}.md"
                record.write_once(path, reply)
                reply_path = str(path)
            run = self._update(
                run,
                turn_state=record.COLLECTED,
                reply=reply_path,
                status=record.FAILED
                if outcome.state == engines.FAILED
                else record.WAITING,
            )
        code, message, variant = _outcome_exit(outcome), None, None
        context: dict[str, object] = {"run": run.id, "worktree": run.worktree}
        if outcome.state == engines.FAILED:
            message = outcome.reason or "the turn failed"
            if outcome.provider_error:
                variant = outcome.provider_kind or "outage"
                attempts = self._saved_outcome(run).get("attempts", [])
                if attempts and attempts[-1]["sending"] and not attempts[-1]["sent"]:
                    variant = "uncertain"
            if outcome.signed_out and not outcome.provider_error:
                fix = self._signed_out_fix(run.engine)
                if fix:
                    variant = "fix"
                context.update(engine=run.engine, fix=fix)
        elif not reply:
            message = f"turn {run.turn} of {run.id} ended with no reply"
        commit, files = None, 0
        if run.read_only:
            changed = self._changed(run)
            for name in changed:
                self._warn(
                    f"WARNING: read-only run {run.id} changed "
                    f"{Path(run.worktree) / name}"
                )
            if changed and not message:
                message = "changed " + ", ".join(
                    str(Path(run.worktree) / name) for name in changed
                )
        else:
            run, commit, files, commit_code, commit_message, commit_variant = (
                self._commit(run)
            )
            if commit_code and code:
                self._warn(f"emb run: also exit {commit_code}: {commit_message}")
            elif commit_code:
                code, message = commit_code, commit_message
                variant = commit_variant
        run = self._record_exit(run, code, message)
        saved = self._saved_outcome(run)
        seconds = time.monotonic() - started
        self._write_outcome(
            run,
            {
                **saved,
                "exit": code,
                "seconds": seconds,
                **({"retry_clock": None} if "retry_clock" in saved else {}),
            },
        )
        self._warn_denial_owners(run, code)
        self._print_turn(run, code, files, commit, reply, seconds, window)
        if code:
            raise RunError(code, message or "", variant, **context)
        return exits.OK

    def _signed_out_fix(self, engine: str) -> str | None:
        try:
            item = status.read(self.env, refresh=True)[engine]
        except Exception:  # noqa: BLE001
            return None
        return next(
            (req.fix for req in item.requirements if not req.met and req.fix), None
        )

    def _own_commit(self, run: Run, head: str) -> bool:
        """Return whether HEAD is this turn's commit, made but not recorded."""
        if head in run.commits or run.expected_head is None:
            return False
        parent = self._git_out(run.worktree, "rev-parse", "-q", "--verify", f"{head}^")
        subject = self._git_out(run.worktree, "log", "-1", "--format=%s", head)
        return parent == run.expected_head and subject == self._subject(run)

    def _write_outcome(self, run: Run, data: Mapping[str, Any]) -> None:
        record.write_private(
            record.outcome_path(run, run.turn), json.dumps({"denied_paths": [], **data})
        )

    def _save_outcome(self, run: Run, outcome: TurnOutcome, seconds: float) -> None:
        """Save how the current turn ended, for its commit and `--recover`."""
        saved = self._saved_outcome(run)
        denials = list(saved.get("denied_paths", []))
        if run.sandbox != sandbox.NONE and run.sandbox_paths:
            output = [outcome.reply or "", outcome.reason or ""]
            driver = self._optional_driver(run.engine)
            with contextlib.suppress(
                engines.DriverError, tmux.TmuxError, OSError, paths.RuntimeDirError
            ):
                output.append(driver.turn_output(run))
            with contextlib.suppress(
                engines.DriverError, tmux.TmuxError, OSError, paths.RuntimeDirError
            ):
                output.append(self._attempt_screen(run, driver))
            detected = [
                item
                for text in output
                for item in sandbox.denied_paths(text, run.sandbox_paths)
            ]
            identities = {
                item["path"]: item["resolved"]
                for item in run.sandbox_paths.get("hidden", [])
            }
            current: dict[str, dict[str, Any]] = {}
            for item in detected:
                target = identities.get(item["path"], item["path"])
                previous = current.get(target)
                current[target] = (
                    item if previous is None else sandbox.merge_denial(previous, item)
                )
            for target, item in current.items():
                previous = next(
                    (
                        old
                        for old in denials
                        if identities.get(old["path"], old["path"]) == target
                    ),
                    None,
                )
                if previous is not None:
                    previous.update(sandbox.merge_denial(previous, item))
                    continue
                denials.append(item)
                self._warn(
                    f"WARNING: {run.id} was denied {item['path']}"
                    + (f" ({item['reason']})" if item["reason"] != "hidden" else "")
                )
        data = {
            **saved,
            "denied_paths": denials,
            "state": outcome.state,
            "reason": outcome.reason,
            "provider_error": outcome.provider_error,
            "provider_kind": outcome.provider_kind
            or ("outage" if outcome.provider_error else None),
            "signed_out": outcome.signed_out,
            "incomplete": incomplete_reason(outcome),
            "seconds": seconds,
        }
        self._write_outcome(run, data)

    def _warn_denial_owners(self, run: Run, code: int) -> None:
        """Print denied-path owner actions after the turn's exit is recorded."""
        for item in self._saved_outcome(run).get("denied_paths", []):
            if item["reason"] == "startup file":
                continue
            problem = sandbox.read_path_problem(item["path"], self.env)
            if problem:
                self._warn(
                    f"owner: {problem}; it cannot be added to "
                    "Settings, Sandbox read paths"
                )
            elif item["reason"] == "hidden" and item["allow_path"] is None:
                self._warn(
                    f"owner: {item['path']} is hidden; no folder can be offered "
                    "because each folder that holds it also holds private files"
                )
            elif item["reason"] != "protected":
                reads = [p for p in run.read_effective if p not in run.read]
                with contextlib.suppress(db.DatabaseError):
                    reads = list(
                        choices.read(env=self.env)["answers"]["sandbox.read"]["value"]
                    )
                reads = list(
                    dict.fromkeys([*reads, item["allow_path"] or item["path"]])
                )
                setting = shlex.quote("sandbox.read=" + json.dumps(reads))
                qualification = (
                    "the turn finished (exit 0); read access is needed only if "
                    "the worker's report says the work needed the path; "
                    if code == exits.OK
                    else ""
                )
                self._warn(
                    f"owner: {qualification}Settings, Sandbox read paths; "
                    f"emb choices --set {setting}"
                )

    def _saved_outcome(self, run: Run) -> dict[str, Any]:
        try:
            return record.load_outcome(run, run.turn)
        except record.RecordError as error:
            if record.outcome_path(run, run.turn).exists():
                raise RunError(exits.ENGINE_FAILED, str(error), run=run.id) from error
            return {}

    def _subject(self, run: Run) -> str:
        """Return the commit subject of the current turn."""
        incomplete = self._saved_outcome(run).get("incomplete")
        if incomplete not in INCOMPLETE_REASONS:
            incomplete = None
        return commit_subject(run.subject, run.turn, incomplete)

    def _files(self, run: Run, commit: str) -> int:
        names = self._git_out(run.worktree, "diff", "--name-only", f"{commit}^", commit)
        return len(names.splitlines()) if names else 0

    def _retry_commit(
        self, run: Run
    ) -> tuple[Run, str | None, int, int, str | None, str | None]:
        """Complete an earlier turn's result before sending another message."""
        with self._collection(run, time.monotonic()) as started:
            outcome = self._recovered_outcome(run, self._read_reply(run))
            if self._saved_outcome(run).get("state") is None:
                self._save_outcome(run, outcome, time.monotonic() - started)
            result = (
                (run, None, 0, exits.OK, None, None)
                if run.read_only
                else self._commit(run)
            )
            saved = self._saved_outcome(run)
            self._write_outcome(
                run,
                {
                    **saved,
                    "exit": _outcome_exit(outcome) or result[3],
                    "seconds": time.monotonic() - started,
                },
            )
            self._warn_denial_owners(run, _outcome_exit(outcome) or result[3])
            return result

    def _commit(
        self, run: Run
    ) -> tuple[Run, str | None, int, int, str | None, str | None]:
        """Commit the turn's changes in the run's worktree.

        Returns:
            The run, the commit or `None`, the number of files, the exit code
            and its message and exit-line variant.
        """
        if run.turn_state == record.COMMITTED:
            return run, None, 0, exits.OK, None, None
        worktree = run.worktree
        branch = self._git_out(worktree, "symbolic-ref", "-q", "--short", "HEAD")
        head = self._git_out(worktree, "rev-parse", "-q", "--verify", "HEAD")
        if branch != run.branch or head != run.expected_head:
            if branch == run.branch and head and self._own_commit(run, head):
                run = self._update(
                    run,
                    commits=(*run.commits, head),
                    expected_head=head,
                    turn_state=record.COMMITTED,
                )
                (run.folder / HOOK_REFUSAL_NAME).unlink(missing_ok=True)
                return run, head, self._files(run, head), exits.OK, None, None
            message = (
                f"the branch or HEAD moved in {worktree}: {branch or 'detached'} at "
                f"{head}, expected {run.branch} at {run.expected_head}; "
                "nothing committed"
            )
            return run, None, 0, exits.MOVED, message, None
        added = self._git(worktree, "add", "-A")
        if added.returncode:
            message = f"git add failed in {worktree}: {_first_line(added.stderr)}"
            return run, None, 0, exits.HOOK_REFUSED, message, "add"
        staged = self._git(worktree, "diff", "--cached", "--name-only", "-z")
        names = [name for name in staged.stdout.split("\0") if name]
        if not names:
            run = self._update(run, turn_state=record.COMMITTED)
            (run.folder / HOOK_REFUSAL_NAME).unlink(missing_ok=True)
            return run, None, 0, exits.OK, None, None
        args = ["commit", "-q", "-m", self._subject(run)]
        if run.trailers:
            args += ["-m", "\n".join(run.trailers)]
        result = self._git(worktree, *args)
        if result.returncode:
            record.write_private(
                run.folder / HOOK_REFUSAL_NAME, result.stdout + result.stderr
            )
            reason = _first_line(result.stderr or result.stdout) or "no reason given"
            message = f"git commit was refused in {worktree}: {reason}"
            return run, None, 0, exits.HOOK_REFUSED, message, None
        commit = self._git_out(worktree, "rev-parse", "HEAD")
        if commit is None:
            raise RunError(exits.UNEXPECTED, f"cannot read HEAD in {worktree}")
        run = self._update(
            run,
            commits=(*run.commits, commit),
            expected_head=commit,
            turn_state=record.COMMITTED,
        )
        (run.folder / HOOK_REFUSAL_NAME).unlink(missing_ok=True)
        return run, commit, len(names), exits.OK, None, None

    def _hook_message(self, run: Run, text: str) -> str:
        """Prepend the saved refusal, capped by lines and UTF-8 bytes."""
        try:
            output = (run.folder / HOOK_REFUSAL_NAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            return text
        tail = "".join(output.splitlines(keepends=True)[-HOOK_OUTPUT_LINES:])
        tail = tail.encode("utf-8")[-HOOK_OUTPUT_BYTES:].decode(
            "utf-8", errors="ignore"
        )
        note = (
            "\n[Hook output cut to its last 200 lines and 64 KiB.]"
            if tail != output
            else ""
        )
        fence = "```"
        while fence in tail:
            fence += "`"
        end = "" if tail.endswith("\n") else "\n"
        return (
            "The commit hook refused your last turn's commit:\n\n"
            f"{fence}\n{tail}{end}{fence}{note}\n\n{text}"
        )

    def _print_turn(
        self,
        run: Run,
        code: int,
        files: int,
        commit: str | None,
        reply: str | None,
        seconds: float,
        window: str | None,
    ) -> None:
        effort = run.effort or "default"
        self._say(
            f"{run.engine} {run.model} · {effort} · {round(seconds)}s · "
            f"exit {code} · {files} files · turn {run.turn}"
        )
        if self.quiet:
            return
        for name, value in (
            ("run", run.id),
            ("worktree", run.worktree),
            ("branch", run.branch),
            ("commit", commit),
            ("window", window),
            ("reply", run.reply),
        ):
            self._say(f"{name} {value or '-'}")
        if reply is not None:
            self._say(REPLY_MARK)
            self._say(reply.rstrip("\n"))

    # Commands on a run

    def send(self, run_id: str, message_file: str, *, force: bool = False) -> int:
        """Send a message into a run's session and wait for its turn.

        Args:
            run_id: The run.
            message_file: The file holding the message.
            force: Send even when the session holds messages Ember did not
                send; every other check still applies.

        Returns:
            The exit code.

        Raises:
            RunError: The message cannot be sent, or the turn did not end
                with exit 0.
        """
        try:
            text = Path(message_file).read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise RunError(exits.NO_INPUT, f"{message_file} does not exist") from error
        except (OSError, UnicodeDecodeError) as error:
            raise RunError(
                exits.NO_INPUT, f"cannot read {message_file}: {error}"
            ) from error
        if not text.strip():
            raise RunError(exits.USAGE, f"the message {message_file} is empty")
        self._load(run_id)
        with self._turn_lock(run_id):
            run = self._load(run_id)
            if run.status == record.CLOSED:
                raise RunError(
                    exits.SESSION_GONE, f"{run_id} is closed", "send", run=run_id
                )
            if self._session_state(run_id) != LIVE:
                raise RunError(
                    exits.SESSION_GONE,
                    f"the session of {run_id} has ended",
                    "send",
                    run=run_id,
                )
            driver = self._driver(run.engine)
            if run.turn_state == record.SENT:
                raise RunError(
                    exits.REFUSED,
                    f"turn {run.turn} of {run_id} has not been collected",
                    "busy",
                    run=run_id,
                )
            if force:
                with contextlib.suppress(engines.DriverError, tmux.TmuxError):
                    driver.accept_input(run)
            else:
                self._check_foreign(run, driver, "send")
            if self._needs_collection(run):
                run, _, _, code, message, variant = self._retry_commit(run)
                if code and not (code == exits.HOOK_REFUSED and variant != "add"):
                    self._record_exit(run, code, message)
                    raise RunError(
                        code, message or "", variant, run=run_id, worktree=run.worktree
                    )
                if code == exits.HOOK_REFUSED:
                    text = self._hook_message(run, text)
            with self._engine_calls(run):
                idle = driver.idle_state(run)
            if idle.state != engines.IDLE:
                raise RunError(
                    exits.REFUSED,
                    f"{run_id} is {idle.state}: {idle.reason}",
                    "busy",
                    run=run_id,
                )
            started = time.monotonic()
            turn = run.turn + 1
            if run.read_only:
                self._save_snapshot(run, turn)
            previous = {
                "status": run.status,
                "turn": run.turn,
                "turn_state": run.turn_state,
                "reply": run.reply,
                "expected_head": run.expected_head,
            }
            run = self._turn_sent(run, turn, text)
            try:
                with self._engine_calls(run, "send"):
                    driver.send(run, text)
            except RunError as error:
                # An uncertain prompt keeps the new turn pending for recovery.
                changes = (
                    {}
                    if isinstance(error.__cause__, DeliveryUncertainError)
                    else previous
                )
                self._record_exit(run, error.code, str(error), **changes)
                error.context.setdefault("run", run_id)
                raise
            except KeyboardInterrupt as error:
                message = f"interrupted while sending to {run_id}"
                self._record_exit(run, exits.INTERRUPTED, message, **previous)
                raise RunError(
                    exits.INTERRUPTED, message, "send", run=run_id
                ) from error
            (run.folder / HOOK_REFUSAL_NAME).unlink(missing_ok=True)
            return self._turn(run, driver, started, None)

    def recover(self, run_id: str) -> int:
        """Collect a turn that ended while no command waited, or wait for it.

        Args:
            run_id: The run.

        Returns:
            The exit code.

        Raises:
            RunError: The turn did not end with exit 0.
        """
        self._load(run_id)
        with self._turn_lock(run_id):
            run = self._load(run_id)
            if run.status == record.CLOSED:
                raise RunError(exits.SESSION_GONE, f"{run_id} is closed", run=run_id)
            started = time.monotonic()
            if run.turn_state == record.SENT:
                driver = self._driver(run.engine)
                self._warn(f"emb run: waiting for turn {run.turn} of {run_id}")
                return self._turn(run, driver, started, None)
            if self._needs_collection(run):
                driver = self._driver(run.engine)
                reply = self._read_reply(run)
                with self._collection(run, started) as started:
                    outcome = self._recovered_outcome(run, reply)
                    return self._finish(run, driver, outcome, started, None)
            self._warn(
                f"emb run: nothing to recover in {run_id}: "
                f"turn {run.turn} is {run.turn_state or 'not started'}"
            )
            return exits.OK

    def _needs_collection(self, run: Run) -> bool:
        """Return whether commit handling or result completion remains."""
        if run.turn_state == record.COLLECTED and not run.read_only:
            return True
        if run.turn_state not in (record.COLLECTED, record.COMMITTED):
            return False
        saved = self._saved_outcome(run)
        return saved.get("exit") is None and saved.get("seconds") is not None

    def _read_reply(self, run: Run) -> str | None:
        """Read a collected reply, or return None when it is unavailable."""
        if run.reply is not None:
            with contextlib.suppress(OSError, UnicodeError):
                return Path(run.reply).read_text(encoding="utf-8")
        return None

    def _recovered_outcome(self, run: Run, reply: str | None) -> TurnOutcome:
        """Return the outcome of a collected turn, as saved when collected."""
        saved = self._saved_outcome(run)
        state = saved.get("state")
        if state in (engines.DONE, engines.FAILED):
            reason = saved.get("reason")
            return TurnOutcome(
                state,
                reply=reply if state == engines.DONE else None,
                reason=reason if isinstance(reason, str) else None,
                provider_error=saved.get("provider_error") is True,
                provider_kind=saved.get("provider_kind"),
                signed_out=saved.get("signed_out") is True,
            )
        if run.status == record.FAILED:
            return TurnOutcome(engines.FAILED, reason=run.last_reason)
        return TurnOutcome(engines.DONE, reply=reply)

    def screen(self, run_id: str) -> int:
        """Print the text of a run's session.

        Args:
            run_id: The run.

        Returns:
            The exit code.
        """
        run = self._load(run_id)
        if self._session_state(run_id) == MISSING:
            raise RunError(exits.SESSION_GONE, f"{run_id} has no session", run=run_id)
        with self._engine_calls(run):
            text = self._optional_driver(run.engine).screen(run)
        print(
            text, end="" if text.endswith("\n") else "\n", file=self.out or sys.stdout
        )
        return exits.OK

    def interrupt(self, run_id: str) -> int:
        """Interrupt a run's current turn.

        Args:
            run_id: The run.

        Returns:
            The exit code.
        """
        run = self._load(run_id)
        with record.record_lock(run):
            saved = self._saved_outcome(run)
            attempts = saved.get("attempts", [])
            if attempts and not attempts[-1]["sent"] and not attempts[-1]["sending"]:
                record.write_private(self._retry_cancel_path(run), "cancelled")
                self._warn(f"emb run: cancelled capacity resend of {run_id}")
                return exits.OK
            if self._session_state(run_id) != LIVE:
                raise RunError(
                    exits.SESSION_GONE, f"{run_id} has no live session", run=run_id
                )
            with self._engine_calls(run):
                self._optional_driver(run.engine).interrupt(run)
        self._warn(f"emb run: interrupted turn {run.turn} of {run_id}")
        return exits.OK

    def attach(self, run_id: str) -> int:
        """Open a new terminal window on a run's session.

        Args:
            run_id: The run.

        Returns:
            The exit code.
        """
        run = self._load(run_id)
        if self._session_state(run_id) == MISSING:
            raise RunError(exits.SESSION_GONE, f"{run_id} has no session", run=run_id)
        terminal = self._settings(None)["answers"]["terminal"]["value"]
        with self._engine_calls(run):
            pid = tmux.open_window(run_id, terminal=terminal, env=self.env)
        self._say(f"window {pid}")
        return exits.OK

    def close(self, run_id: str, *, force: bool = False) -> int:
        """Close a run: end its session and record it closed.

        Editing worktrees and branches are kept. Clean detached worktrees
        are removed; changed detached worktrees and their paths are kept.

        Args:
            run_id: The run.
            force: Close even while a turn runs, a command waits on it or its
                live session holds messages Ember did not send.

        Returns:
            The exit code.

        Raises:
            RunError: A turn is running, a command waits on the run, or its
                live session holds messages Ember did not send, and `force`
                is false.
        """
        run = self._load(run_id)
        if force and run.status != record.CLOSED:
            self._end_session(run)
            return exits.OK
        with self._turn_lock(run_id, "close"):
            run = self._load(run_id)
            if run.status == record.CLOSED:
                self._remove_detached(run, command_locked=True)
                self._say(f"closed {run_id}")
                return exits.OK
            if run.status == record.FAILED and run.turn_state == record.SENT:
                attempts = self._saved_outcome(run).get("attempts", [])
                uncertain = (
                    attempts and attempts[-1]["sending"] and not attempts[-1]["sent"]
                )
                # The recorded reason also preserves uncertainty when retry
                # attempt details are unavailable in an older run record.
                if uncertain or run.last_reason == (
                    "capacity retry delivery is uncertain; ask the lead"
                ):
                    raise RunError(
                        exits.REFUSED,
                        f"turn {run.turn} of {run_id} is sent or delivery is uncertain",
                        "close",
                        run=run_id,
                    )
            if run.turn_state == record.COLLECTED and not run.read_only:
                raise RunError(
                    exits.REFUSED,
                    f"turn {run.turn} of {run_id} is collected but not committed",
                    "collected",
                    run=run_id,
                )
            if self._session_state(run_id) == LIVE:
                if run.turn_state == record.SENT:
                    raise RunError(
                        exits.REFUSED,
                        f"turn {run.turn} of {run_id} is running",
                        "close",
                        run=run_id,
                    )
                driver = self._optional_driver(run.engine)
                with self._engine_calls(run):
                    idle = driver.idle_state(run)
                if idle.state != engines.IDLE:
                    raise RunError(
                        exits.REFUSED,
                        f"{run_id} is {idle.state}: {idle.reason}",
                        "close",
                        run=run_id,
                    )
                self._check_foreign(run, driver, "close")
            self._end_session(run, command_locked=True)
        return exits.OK

    def _end_session(self, run: Run, *, command_locked: bool = False) -> None:
        # Processes a worker's tools start in a session of their own survive
        # the tmux session, so the pane's descendants are found first.
        found: list[processes.Process] = []
        with contextlib.suppress(tmux.TmuxError):
            pid = tmux.pane_pid(run.id, env=self.env)
            if pid is not None:
                found = processes.tree(pid)
        try:
            self._optional_driver(run.engine).close(run)
        except (engines.DriverError, tmux.TmuxError) as error:
            self._warn(f"emb run: {run.id}: {error}")
        if self._session_state(run.id) != MISSING:
            with self._engine_calls(run):
                tmux.kill(run.id, env=self.env)
        left = processes.end(found)
        if left:
            self._warn(
                f"emb run: {run.id}: processes still running after SIGKILL: "
                + ", ".join(str(pid) for pid in left)
            )
        record.update(
            run,
            {"status": record.CLOSED, "ended_at": run.ended_at or time.time()},
            allow_closed=True,
        )
        self._remove_detached(run, command_locked=command_locked)
        self._say(f"closed {run.id}")

    def _remove_detached(self, run: Run, *, command_locked: bool = False) -> None:
        if not run.read_only or not run.detached:
            return
        command_lock = (
            contextlib.nullcontext()
            if command_locked
            else self._turn_lock(run.id, "close")
        )
        try:
            with command_lock, record.record_lock(run):
                run = record.load(run.id, self.env)
                reason = worktree_removal_reason(run)
                if reason:
                    raise OSError(reason)
                with self._worktree_lock(Path(run.common or run.repo)):
                    worktrees.remove_detached(run)
        except (RunError, record.RecordError, OSError) as error:
            self._warn(f"WARNING: read-only run {run.id}: {error}")

    def _optional_driver(self, engine: str) -> Driver:
        try:
            return self.load_driver(engine)
        except engines.DriverMissingError:
            return _SessionOnly()

    def status(self, run_id: str | None, *, as_json: bool = False) -> int:
        """Print one run's record, or every run's.

        Args:
            run_id: The run, or `None` for every run.
            as_json: Print JSON.

        Returns:
            The exit code.
        """
        self.status_next = None
        if run_id is not None:
            document = self._document(self._load(run_id))
            self.status_next = document["next"]
            self._warn_parent([document])
            if as_json:
                self._say(json.dumps(document, indent=2))
            else:
                for key, value in document.items():
                    if key == "next":
                        continue
                    if key == "status" and document["waiting_for_flow"]:
                        value = self._flow_wait(document)
                    shown = value if isinstance(value, str) else json.dumps(value)
                    self._say(f"{key}: {shown}")
            return exits.OK
        runs, errors = record.list_runs(self.env)
        for error in errors:
            self._warn(f"emb run: skipped {error}")
        documents = [self._document(run) for run in runs]
        self._warn_parent(documents)
        if as_json:
            self._say(json.dumps(documents, indent=2))
        else:
            for item in documents:
                status = (
                    self._flow_wait(item)
                    if item["waiting_for_flow"]
                    else item["status"]
                )
                self._say(
                    f"{item['id']}  {status}  {item['session_state']}  "
                    f"turn {item['turn']}  {item['subject']}"
                )
        return exits.OK

    def _document(self, run: Run) -> dict[str, Any]:
        waiting = record.waiting_for_flow(run)
        turns, errors = record.turn_results(run)
        for error in errors:
            self._warn(f"WARNING: {error}")
        session_state = self._session_state(run.id)
        step, next_line = self.step(run, session_state)
        return {
            **run.to_json(),
            "session_state": session_state,
            "orphaned": waiting is None and record.orphaned(run),
            "waiting_for_flow": waiting,
            "turns": turns,
            "step": step,
            "next": next_line,
        }

    def step(
        self, run: Run, session_state: str | None = None
    ) -> tuple[str, str | None]:
        """Derive a run's step using the core's session and collection checks.

        Args:
            run: The run record.
            session_state: An already observed session state, or None to read it.

        Returns:
            The step name and next line, without printing either.
        """
        try:
            needs_collection = self._needs_collection(run)
        except RunError:
            needs_collection = True
        return steps.derive(
            run,
            session_state if session_state is not None else self._session_state(run.id),
            needs_collection,
            self._git,
        )

    @staticmethod
    def _flow_wait(document: dict[str, Any]) -> str:
        """Describe the lead answer awaited by a run's parent flow."""
        return f"waiting for the lead's answer to flow {document['waiting_for_flow']}"

    def _warn_parent(self, documents: list[dict[str, Any]]) -> None:
        """Name flow waits and orphaned runs, with recovery only for orphans."""
        for document in documents:
            if document["waiting_for_flow"]:
                self._warn(
                    f"WARNING: run {document['id']} is {self._flow_wait(document)}"
                )
                continue
            if not document["orphaned"]:
                continue
            parent = document["parent"]
            starter = " ".join(
                part for part in ("emb", parent["command"], parent["id"]) if part
            )
            run_id = document["id"]
            self._warn(
                f"WARNING: run {run_id} is orphaned: {starter} ended "
                "without collecting it"
            )
            self._warn(
                f"next: emb run --recover {run_id}, then emb run --close {run_id}"
            )


class _SessionOnly(Driver):
    """Reaches a session through tmux alone, for a run whose driver is gone."""

    def command(self, run: Run, first_message: str) -> LaunchSpec:
        raise engines.DriverError(f"Ember has no driver for {run.engine}")

    def ready(self, run: Run) -> bool:
        return False

    def wait(self, run: Run) -> TurnOutcome:
        raise engines.DriverError(f"Ember has no driver for {run.engine}")

    def idle_state(self, run: Run) -> engines.IdleState:
        return engines.IdleState(
            engines.UNKNOWN, f"Ember has no driver for {run.engine}"
        )

    def valid_effort(self, effort: str) -> bool:
        return False

    def default_effort(self, model: str) -> str | None:
        return None


def _start_failure(run: Run, error: Exception) -> RunError:
    """Return the exit of an unexpected failure while a run starts.

    A runtime folder or sandbox problem is 69; anything else is 1.
    """
    context: dict[str, object] = {"run": run.id, "engine": run.engine}
    if isinstance(error, (paths.RuntimeDirError, sandbox.SandboxError)):
        variant = "runtime" if isinstance(error, paths.RuntimeDirError) else None
        return RunError(exits.UNAVAILABLE, f"{run.id}: {error}", variant, **context)
    if isinstance(error, OSError):
        return RunError(exits.UNEXPECTED, f"{run.id}: {error}", None, **context)
    return RunError(
        exits.UNEXPECTED,
        f"{run.id}: unexpected error: {type(error).__name__}: {error}",
        None,
        **context,
    )


def _hash(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 16), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _close_quietly(driver: Driver, run: Run) -> None:
    """Stop the driver, pane and descendants after a failed start.

    The start's failure is the one reported, so a clean-up error must not
    replace it.
    """
    found: list[processes.Process] = []
    with contextlib.suppress(Exception):
        pid = tmux.pane_pid(run.id, env=run.env)
        if pid is not None:
            found = processes.tree(pid)
    with contextlib.suppress(Exception):
        driver.close(run)
    with contextlib.suppress(Exception):
        if tmux.exists(run.id, env=run.env):
            tmux.kill(run.id, env=run.env)
    with contextlib.suppress(Exception):
        processes.end(found)
