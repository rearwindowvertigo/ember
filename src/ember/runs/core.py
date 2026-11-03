"""Single worker runs: start, turns, commits and the commands on a run.

The core owns the run record, the worktree and branch, commits, exits and
the tmux session. A driver from `ember.engines` owns the engine's command
line and how its replies are read.

An editing run works in its own worktree, `.ember/worktrees/<id>` at the top
of the main checkout, on the branch `ember/<engine>/<id>` cut from the named
checkout's `HEAD`. A read-only run works in the checkout itself and has no
commit step. The core never pushes, merges, rebases or deletes a branch, and
never removes a worktree.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from ember import choices, db, engines, paths, processes, sandbox, tmux
from ember.engines import Driver, LaunchSpec, TurnOutcome, status
from ember.runs import exits, record
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

_EXCLUDE_LINES = (".ember/", "/.ember/", ".ember", "/.ember")

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
    """The model; `None` takes it from the menu."""

    effort: str | None = None
    """The effort; `None` takes it from the menu or the driver."""

    read_only: bool = False
    """The run edits nothing."""

    read: tuple[str, ...] = ()
    """Paths the run may read, in addition to the `sandbox.read` setting."""

    trailers: tuple[str, ...] = ()
    """Trailer lines added to each commit."""

    claim: str | None = None
    """The claim id the menu is read with."""

    window: bool = False
    """Open a terminal window on the session."""


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
            quiet: Print only the first line and each turn's summary.
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
                exits.ENGINE_FAILED, f"{run.id}: {error}", variant, run=run.id
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

    def _check_connected(self, engine: str) -> None:
        held = status.cached(self.env)
        if held is None:
            held = status.read(self.env)
        item = held[engine]
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
    ) -> tuple[Driver, str, str | None]:
        """Return the driver, model and effort of a new run."""
        engine = options.engine
        answers = settings["answers"]
        if answers[f"engine.{engine}"]["value"] != "on":
            raise RunError(
                exits.USAGE, f"{engine} is switched off", "engine_off", engine=engine
            )
        self._check_connected(engine)
        driver = self._driver(engine)
        name = "single.reviewer" if options.read_only else "single.writer"
        menu = settings["menu"]
        row: Mapping[str, Any] | None = None if menu is None else menu[name]
        model = options.model
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
        if (
            effort is None
            and row is not None
            and row["state"] == "set"
            and row["engine"] == engine
            and row["model"] == model
        ):
            effort = row["effort"]
        if effort is None:
            effort = driver.default_effort(model)
        if effort is not None and not driver.valid_effort(effort):
            raise RunError(exits.USAGE, f"{engine} does not take the effort {effort!r}")
        return driver, model, effort

    # Checkout and worktree

    def _main_checkout(self, top: str) -> Path:
        result = self._git(top, "worktree", "list", "--porcelain")
        if result.returncode:
            raise RunError(
                exits.WORKTREE_FAILED,
                f"git worktree list failed in {top}: {_first_line(result.stderr)}",
                dir=top,
            )
        block = result.stdout.split("\n\n", 1)[0].splitlines()
        if not block or not block[0].startswith("worktree ") or "bare" in block:
            raise RunError(
                exits.WORKTREE_FAILED,
                f"the repository of {top} has no main checkout",
                dir=top,
            )
        main = Path(block[0].removeprefix("worktree "))
        if not main.is_dir():
            raise RunError(
                exits.WORKTREE_FAILED,
                f"the main checkout {main} of {top} does not exist",
                dir=top,
            )
        return main

    def _exclude(self, common: Path) -> None:
        path = common / "info" / "exclude"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        if any(line.strip() in _EXCLUDE_LINES for line in text.splitlines()):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + ".ember/\n", encoding="utf-8")

    def _create_worktree(self, run: Run, main: Path, common: Path) -> None:
        try:
            self._exclude(common)
        except OSError as error:
            raise RunError(
                exits.WORKTREE_FAILED,
                f"cannot add .ember/ to {common / 'info' / 'exclude'}: {error}",
                dir=str(main),
            ) from error
        assert run.branch is not None and run.base is not None
        result = self._git(
            main, "worktree", "add", "-q", "-b", run.branch, run.worktree, run.base
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

        A Claude command runs as the driver returned it; its program wraps
        each turn.
        """
        if run.sandbox == sandbox.NONE or run.engine == "claude":
            return spec
        worktree = Path(run.worktree)
        binary = self._which(spec.argv[0]) if spec.argv else None
        if run.engine == "codex":
            options = sandbox.codex_args(
                worktree,
                common,
                run.read_effective,
                read_only=run.read_only,
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
            read_only=run.read_only,
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
        """Start a run and wait for its first turn.

        Args:
            options: The run's options.

        Returns:
            The exit code.

        Raises:
            RunError: The run cannot start, or its first turn did not end
                with exit 0.
        """
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
        driver, model, effort = self._select(options, settings)
        common_dir = self._git_out(
            top, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        if not common_dir:
            raise RunError(exits.NOT_A_CHECKOUT, f"cannot find the git folder of {top}")
        common = Path(common_dir)
        base = self._git_out(top, "rev-parse", "-q", "--verify", "HEAD^{commit}")
        base_branch = self._git_out(top, "symbolic-ref", "-q", "--short", "HEAD")
        run_id = record.new_id(engine)
        if options.read_only:
            main, worktree, branch = None, top, None
        else:
            main = self._main_checkout(top)
            if base is None:
                raise RunError(
                    exits.WORKTREE_FAILED, f"{top} has no commit to start from", dir=top
                )
            worktree = str(main / ".ember" / "worktrees" / run_id)
            branch = f"ember/{engine}/{run_id}"
        provider = self._sandbox_provider(engine)
        self._check_codex_config(engine, provider, worktree, common)
        self._require_sandbox(engine, provider)
        answers = settings["answers"]
        read = tuple(str(Path(path).resolve()) for path in options.read)
        now = time.time()
        with self._turn_lock(run_id):
            run = Run(
                id=run_id,
                engine=engine,
                model=model,
                effort=effort,
                read_only=options.read_only,
                repo=top,
                worktree=worktree,
                branch=branch,
                base=base,
                base_branch=base_branch,
                sandbox=provider,
                network=answers["worker.network"]["value"] == "on",
                read=read,
                read_effective=tuple(
                    dict.fromkeys([*answers["sandbox.read"]["value"], *read])
                ),
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
                folder=record.make_run_dir(run_id, self.env),
                env=self.env,
            )
            started = time.monotonic()
            try:
                record.write_private(run.folder / record.BRIEF_NAME, brief)
                record.save(run)
                self._say(f"run {run_id}")
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
            self._create_worktree(run, main, common)
            # The new worktree can hold a tracked .codex/config.toml.
            self._check_codex_config(run.engine, run.sandbox, run.worktree, common)
        if run.sandbox == sandbox.NONE:
            self._warn(sandbox.none_warning(run.engine))
        with self._engine_calls(run, "starting"):
            spec = driver.command(run, first_message(run.id, brief))
        spec = self._sandboxed(spec, run, common)
        if run.read_only:
            self._save_snapshot(run, 1)
        if spec.first_message is None:
            run = self._turn_sent(run, 1)
        try:
            tmux.new_session(
                run.id,
                cwd=spec.cwd,
                argv=spec.argv,
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
            run = self._turn_sent(run, 1)
            with self._engine_calls(run):
                driver.send(run, spec.first_message)
        return run, window

    def _turn_sent(self, run: Run, turn: int) -> Run:
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

    def _turn(
        self, run: Run, driver: Driver, started: float, window: str | None
    ) -> int:
        """Wait for the current turn, collect it and commit it."""
        try:
            outcome = self._wait_turn(run, driver)
        except KeyboardInterrupt as error:
            self._record_exit(run, exits.INTERRUPTED, "stopped waiting")
            raise RunError(
                exits.INTERRUPTED,
                f"stopped waiting; turn {run.turn} of {run.id} keeps running",
                run=run.id,
            ) from error
        return self._finish(run, driver, outcome, started, window)

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
        while True:
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
        if run.turn_state == record.SENT:
            self._save_outcome(run, outcome)
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
        code, message, variant = exits.OK, None, None
        context: dict[str, object] = {"run": run.id, "worktree": run.worktree}
        if outcome.state == engines.FAILED:
            message = outcome.reason or "the turn failed"
            if outcome.provider_error:
                code = exits.PROVIDER_ERROR
            elif outcome.signed_out:
                code, variant = exits.UNAVAILABLE, None
                fix = self._signed_out_fix(run.engine)
                if fix:
                    variant = "fix"
                context.update(engine=run.engine, fix=fix)
            else:
                code = exits.ENGINE_FAILED
        elif not reply:
            code, message = (
                exits.NO_REPLY,
                f"turn {run.turn} of {run.id} ended with no reply",
            )
        commit, files = None, 0
        if run.read_only:
            changed = self._changed(run)
            for name in changed:
                self._warn(f"WARNING: read-only run {run.id} changed {name}")
            if changed and not message:
                message = "changed " + ", ".join(changed)
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
        self._print_turn(
            run, code, files, commit, reply, time.monotonic() - started, window
        )
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

    def _outcome_path(self, run: Run) -> Path:
        return run.folder / f"turn-{run.turn}.outcome.json"

    def _save_outcome(self, run: Run, outcome: TurnOutcome) -> None:
        """Save how the current turn ended, for its commit and `--recover`."""
        data = {
            "state": outcome.state,
            "reason": outcome.reason,
            "provider_error": outcome.provider_error,
            "signed_out": outcome.signed_out,
            "incomplete": incomplete_reason(outcome),
        }
        record.write_private(self._outcome_path(run), json.dumps(data))

    def _saved_outcome(self, run: Run) -> dict[str, Any]:
        try:
            data = json.loads(self._outcome_path(run).read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _subject(self, run: Run) -> str:
        """Return the commit subject of the current turn."""
        incomplete = self._saved_outcome(run).get("incomplete")
        if incomplete not in INCOMPLETE_REASONS:
            incomplete = None
        return commit_subject(run.subject, run.turn, incomplete)

    def _files(self, run: Run, commit: str) -> int:
        names = self._git_out(run.worktree, "diff", "--name-only", f"{commit}^", commit)
        return len(names.splitlines()) if names else 0

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
            if run.turn_state == record.COLLECTED and not run.read_only:
                run, _, _, code, message, variant = self._commit(run)
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
            run = self._turn_sent(run, turn)
            try:
                with self._engine_calls(run, "send"):
                    driver.send(run, text)
            except RunError as error:
                # The message is not known to be delivered: the turn did not
                # start, and --send can be tried again.
                self._record_exit(run, error.code, str(error), **previous)
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
            if run.turn_state == record.COLLECTED and not run.read_only:
                driver = self._driver(run.engine)
                reply = None
                if run.reply is not None:
                    try:
                        reply = Path(run.reply).read_text(encoding="utf-8")
                    except OSError:
                        reply = None
                outcome = self._recovered_outcome(run, reply)
                return self._finish(run, driver, outcome, started, None)
            self._warn(
                f"emb run: nothing to recover in {run_id}: "
                f"turn {run.turn} is {run.turn_state or 'not started'}"
            )
            return exits.OK

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

        The worktree and branch are kept.

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
        if run.status == record.CLOSED:
            self._say(f"closed {run_id}")
            return exits.OK
        if force:
            self._end_session(run)
            return exits.OK
        with self._turn_lock(run_id, "close"):
            run = self._load(run_id)
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
            self._end_session(run)
        return exits.OK

    def _end_session(self, run: Run) -> None:
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
        self._say(f"closed {run.id}")

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
        if run_id is not None:
            document = self._document(self._load(run_id))
            if as_json:
                self._say(json.dumps(document, indent=2))
            else:
                for key, value in document.items():
                    shown = value if isinstance(value, str) else json.dumps(value)
                    self._say(f"{key}: {shown}")
            return exits.OK
        runs, errors = record.list_runs(self.env)
        for error in errors:
            self._warn(f"emb run: skipped {error}")
        documents = [self._document(run) for run in runs]
        if as_json:
            self._say(json.dumps(documents, indent=2))
        else:
            for item in documents:
                self._say(
                    f"{item['id']}  {item['status']}  {item['session_state']}  "
                    f"turn {item['turn']}  {item['subject']}"
                )
        return exits.OK

    def _document(self, run: Run) -> dict[str, Any]:
        return {**run.to_json(), "session_state": self._session_state(run.id)}


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
    """Let the driver clean up after a failed start; its own errors are dropped.

    The start's failure is the one reported, so a clean-up error must not
    replace it.
    """
    with contextlib.suppress(Exception):
        driver.close(run)
