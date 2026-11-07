"""Review a plan through one read-only run, or read an existing reply.

With `--previous`, an earlier result's gaps and answers go to the reviewer,
and the reviewer accounts for each active and settled gap. Settled answers
and closed history are carried into the new result. Exit 81 reports an
invalid or incomplete reply.
Answer mode exits 64 for invalid options or ids, 65 for an invalid
result, 66 for an unreadable file and 74 for a write failure. Other exits
use the run's codes. The command never creates or finishes a
claim.
"""

import argparse
import contextlib
import io
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, TextIO

from ember import choices, citations, db, paths
from ember.gapcheck import brief, plan, policy, previous, reply
from ember.runs import exits, record
from ember.runs.core import STALL_MINUTES, Core, StartOptions

INVALID_REPLY = 81
INVALID_PREVIOUS = 65
GIT_LOCATION = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
REVIEWER_LINE = (
    "owner: name the reviewer for this gap check, "
    "or set Gap check reviewer on the Team screen"
)


class CommandError(Exception):
    """A command failure with its exit code and next action."""

    def __init__(
        self, code: int, message: str, follow: str, *, origin: str = "gapcheck"
    ) -> None:
        """Store the failure and its action."""
        super().__init__(message)
        self.code = code
        self.follow = follow
        self.origin = origin


def _usage(args: argparse.Namespace) -> None:
    """Reject incompatible modes before reading any input."""
    problem = None
    if args.answer is not None:
        extra = [
            f"--{name}"
            for name in (
                "plan",
                "previous",
                "dir",
                "engine",
                "model",
                "effort",
                "claim",
                "stall",
                "reply",
            )
            if getattr(args, name) is not None
        ]
        extra += [
            f"--{name}"
            for name in ("brief", "read", "window", "confirmed")
            if getattr(args, name)
        ]
        if extra:
            problem = f"{', '.join(extra)} cannot be used with --answer"
        elif args.take_defaults and (args.id is not None or args.text is not None):
            problem = "--take-defaults cannot be combined with --id or --text"
        elif not args.take_defaults and (args.id is None or args.text is None):
            problem = "--answer needs --id and --text, or --take-defaults"
    elif args.take_defaults:
        problem = "--take-defaults needs --answer"
    elif args.id is not None or args.text is not None:
        problem = "--id and --text need --answer"
    elif args.plan is None:
        problem = "--plan is required"
    elif args.brief and args.reply is not None:
        problem = "--brief and --reply cannot be combined"
    elif args.brief or args.reply is not None:
        extra = [
            f"--{name}"
            for name in ("engine", "model", "effort", "claim", "stall")
            if getattr(args, name) is not None
        ]
        extra += ["--window"] if args.window else []
        # With --brief, read files cover citations and are listed as sources.
        extra += ["--read"] if args.read and not args.brief else []
        if args.brief and args.json:
            extra.append("--json")
        if args.reply is not None and args.dir is not None:
            extra.append("--dir")
        if extra:
            mode = "--brief" if args.brief else "--reply"
            problem = f"{', '.join(extra)} cannot be used with {mode}"
    elif args.engine is None and (args.model is not None or args.effort is not None):
        problem = "--model and --effort need --engine"
    if problem:
        raise CommandError(64, problem, "next: emb gapcheck --help")


def _checkout(directory: str | None, env: Mapping[str, str], command: str) -> str:
    """Find the checkout top with the environment used by the run."""
    folder = Path(directory or ".").expanduser().absolute()
    if not folder.is_dir():
        raise CommandError(
            66, f"not a directory: {folder}", f"next: ls -l {shlex.quote(str(folder))}"
        )
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=60,
        )
    except OSError as error:
        raise CommandError(
            69, f"git cannot run: {error}", "owner: install git"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise CommandError(
            1, "git rev-parse timed out after 60s", f"next: {command} ..."
        ) from error
    if result.returncode or not result.stdout.strip():
        raise CommandError(
            65,
            f"not a git checkout: {folder}",
            f"next: {command} --dir <a folder in the checkout>",
        )
    return result.stdout.strip()


def _settings(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, Any]:
    """Read the choices, checking the claim when one is given."""
    try:
        settings = choices.read(args.claim, env=env)
    except choices.InvalidChoiceError as error:
        raise CommandError(
            64, str(error), exits.line(64, "claim") or "", origin="run"
        ) from error
    except db.DatabaseError as error:
        raise CommandError(1, str(error), exits.line(1, "database") or "") from error
    if args.claim is not None:
        state = settings["claim"]["status"]
        if state in ("gone", "unknown"):
            raise CommandError(
                64,
                f"claim {args.claim} is {state}",
                exits.line(64, "claim") or "",
                origin="run",
            )
    return settings


def _engine(args: argparse.Namespace, settings: Mapping[str, Any], command: str) -> str:
    """Resolve the reviewer from the options, else the gap check reviewer row.

    A row's model and effort are copied into `args`, so the run uses the gap
    check reviewer rather than the single-task reviewer.
    """
    if args.model is not None:
        return args.engine
    menu = settings.get("menu")
    if settings.get("source_error") or menu is None:
        reason = "; ".join(settings.get("errors") or []) or "the menu is unavailable"
        raise CommandError(64, f"the settings cannot be read: {reason}", REVIEWER_LINE)
    row = menu["gapcheck.reviewer"]
    name = "single.reviewer" if row["same"] else "gapcheck.reviewer"
    if row["state"] != "set":
        raise CommandError(
            64,
            f"{name} is {row['state']}: {row['reason']}",
            REVIEWER_LINE,
        )
    if row["engine"] == "claude" and row["run_as"] == "subagent":
        raise CommandError(
            64,
            f"{name} runs claude:{row['model']} as a subagent",
            f"next: {command} --brief{' --confirmed' if args.confirmed else ''}, "
            "then give the brief to a read-only Claude "
            f"subagent on {row['model']} and read its answer with "
            f"{command} --reply <file>",
        )
    if args.engine is not None and args.engine != row["engine"]:
        return args.engine
    args.model = row["model"]
    if args.effort is None:
        args.effort = row["effort"]
    return row["engine"]


def _read_file(path: Path) -> str:
    """Read a reply or report its input error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CommandError(
            66,
            f"cannot read the reply {path}: {error}",
            f"next: ls -l {shlex.quote(str(path))}",
        ) from error


def _result(document: plan.Plan, earlier: previous.Previous | None) -> dict[str, Any]:
    """Build an incomplete result before a reply has been collected."""
    return {
        "plan": str(document.path),
        "plan_steps": document.kind,
        "failure_model": document.failure_model,
        "previous": None if earlier is None else str(earlier.path),
        "previous_plan": None if earlier is None else earlier.plan,
        "next_id": 1 if earlier is None else earlier.next_id,
        "dispositions": [],
        "closed": [] if earlier is None else list(earlier.closed),
        "reviewer": None,
        "run": None,
        "steps": [],
        "settled": [],
        "ranked": [],
        "counts": dict.fromkeys(reply.HARMS, 0),
        "uncovered_steps": [step.number for step in document.steps],
        "unknown_steps": [],
        "parse_error": "the run produced no reply",
    }


def _read_reply(
    text: str, document: plan.Plan, earlier: previous.Previous | None
) -> dict[str, Any]:
    """Read a reply, numbering new gaps after the earlier ones.

    Returns:
        The reply's result fields, settled answers and closed history.
    """
    fields = reply.read(
        text,
        document.steps,
        earlier_ids=None if earlier is None else earlier.ids(),
        first_number=1 if earlier is None else earlier.next_id,
        answered_ids=()
        if earlier is None
        else {gap.id for gap in earlier.gaps if gap.answer is not None},
    )
    fields["settled"], fields["closed"] = (
        ([], [])
        if earlier is None
        else previous.history(earlier, fields["dispositions"])
    )
    return fields


def _load_previous(path: str | None) -> previous.Previous | None:
    """Read the `--previous` result, or return None without one."""
    if path is None:
        return None
    try:
        return previous.load(path)
    except previous.PreviousError as error:
        location = shlex.quote(str(Path(path).expanduser().absolute()))
        if error.unreadable:
            raise CommandError(66, str(error), f"next: ls -l {location}") from error
        raise CommandError(
            INVALID_PREVIOUS,
            str(error),
            f"next: correct {location} to the emb gapcheck result format, "
            "with answers on their gaps in steps or settled",
        ) from error


def _answer(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Record answers after loading and validating the result."""
    saved = _load_previous(args.answer)
    assert saved is not None
    identities = [
        identity.strip()
        for group in args.take_defaults
        for identity in group.split(",")
    ]
    try:
        made = (
            previous.take_defaults(saved, identities)
            if args.take_defaults
            else previous.answer(saved, args.id, args.text)
        )
    except ValueError as error:
        raise CommandError(64, str(error), "next: emb gapcheck --help") from error
    except previous.PreviousError as error:
        code = 66 if error.unreadable else INVALID_PREVIOUS
        follow = (
            f"next: ls -l {shlex.quote(str(saved.path))}"
            if error.unreadable
            else "next: emb gapcheck --help"
        )
        raise CommandError(code, str(error), follow) from error
    except OSError as error:
        raise CommandError(
            74,
            f"cannot write the result {saved.path}: {error}",
            f"next: ls -l {shlex.quote(str(saved.path))}",
        ) from error
    if args.json:
        print(json.dumps(made, indent=2, ensure_ascii=False), file=out)
    elif args.take_defaults:
        print(f"recorded answers for {', '.join(identities)} in {saved.path}", file=err)
    else:
        print(f"recorded answer for {args.id} in {saved.path}", file=err)
    return 0


def _print_decision(
    document: plan.Plan,
    decision: str,
    why: str,
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
) -> None:
    """Report a plan the settings skip, or one that waits on the owner.

    With `--brief` the report goes to standard error and no brief is printed.
    """
    if args.brief:
        print(f"gap check {decision}: {why}", file=err)
    elif args.json:
        print(
            json.dumps(
                {"plan": str(document.path), "status": decision, "reason": why},
                indent=2,
            ),
            file=out,
        )
    else:
        print(f"gap check {decision}: {why}", file=out)


def _settings_quietly(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Read the choices for a reply, or `None` when they cannot be read."""
    try:
        return choices.read(None, env=env)
    except choices.InvalidChoiceError, db.DatabaseError:
        return None


def _mark_asks(result: dict[str, Any], settings: Mapping[str, Any] | None) -> None:
    """Mark each gap the owner is asked about, per `gapcheck.ask`.

    Without readable settings, including settings read with a source error
    (whose answers are defaults), every gap is asked about.
    """
    answers = (
        None
        if settings is None or settings.get("source_error")
        else settings.get("answers")
    )
    result["status"] = policy.RUN
    result["ask"] = "all" if answers is None else answers["gapcheck.ask"]["value"]
    for step in result["steps"]:
        for gap in step["gaps"]:
            gap["ask_owner"] = answers is None or policy.asks_owner(
                answers, gap["harm"]
            )


def _print_result(
    result: dict[str, Any],
    as_json: bool,
    out: TextIO,
    earlier: previous.Previous | None = None,
) -> None:
    """Print the result as JSON or one block per covered step.

    In text, a gap that names an earlier gap shows that gap's answer, and
    a check with `--previous` lists the settled gaps after the steps.
    """
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
        return
    answers = {} if earlier is None else {gap.id: gap.answer for gap in earlier.gaps}
    for step in result["steps"]:
        print(f"step {step['step']}: {step['title']}", file=out)
        if not step["gaps"]:
            print("  no gaps", file=out)
        for gap in step["gaps"]:
            asked = "" if gap.get("ask_owner", True) else " (default taken)"
            print(f"  {gap['id']} {gap['harm']}{asked}: {gap['gap']}", file=out)
            print(f"    why: {gap['why']}", file=out)
            print(f"    default: {gap['default']}", file=out)
            if gap["earlier"]:
                answer = answers.get(gap["earlier"])
                was = "unanswered" if answer is None else f"answered: {answer}"
                print(f"    earlier: {gap['earlier']}, {was}", file=out)
    if result["previous"] is not None:
        print(f"settled from {result['previous']}:", file=out)
        if not result["settled"]:
            print("  none", file=out)
        for gap in result["settled"]:
            print(
                f"  {gap['id']} {gap['harm']}, earlier step {gap['step']}: "
                f"{gap['gap']}",
                file=out,
            )
            print(f"    answer: {gap['answer']}", file=out)
    if result["closed"]:
        print("closed gaps:", file=out)
        for gap in result["closed"]:
            print(f"  {gap['id']}: {gap['gap']}", file=out)
            print(f"    reason: {gap['reason']}", file=out)
    print(
        "counts: "
        + ", ".join(f"{harm} {result['counts'][harm]}" for harm in reply.HARMS),
        file=out,
    )
    for name in ("uncovered_steps", "unknown_steps"):
        numbers = ", ".join(map(str, result[name])) or "none"
        print(f"{name.replace('_', ' ')}: {numbers}", file=out)


def _follow(command: str, reply_path: str | None, code: int) -> str:
    """Name an existing reply, or a new check when no reply exists."""
    if reply_path is None:
        return f"next: {command} ..."
    prefix = "once the turn ends, " if code not in (0, INVALID_REPLY) else ""
    return f"next: {prefix}{command} --reply {shlex.quote(reply_path)}"


def _preparation_retry(args: argparse.Namespace) -> str:
    """Repeat the original check after preparation failed before its reviewer."""
    argv = ["emb", "gapcheck"]
    for name in (
        "plan",
        "previous",
        "dir",
        "engine",
        "model",
        "effort",
        "claim",
        "stall",
    ):
        value = getattr(args, name)
        if value is not None:
            argv.extend((f"--{name}", str(value)))
    for path in args.read:
        argv.extend(("--read", path))
    for name in ("window", "json", "confirmed"):
        if getattr(args, name):
            argv.append(f"--{name}")
    return "next: " + shlex.join(argv)


@contextlib.contextmanager
def _stop_signals_interrupt() -> Iterator[None]:
    """Treat SIGTERM and SIGHUP as SIGINT while the reviewer run is started.

    The run core already handles an interrupt: it stops waiting, keeps the
    reviewer's turn and records the exit, and the gap check prints the
    `next:` line. Without this, a terminated gap check ended at once and left
    its run with no exit and no line.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous_handlers = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _run(
    args: argparse.Namespace,
    document: plan.Plan,
    earlier: previous.Previous | None,
    top: str,
    engine: str,
    env: Mapping[str, str],
    core_factory: Callable[..., Core],
    err: TextIO,
    sources: Sequence[Path] = (),
) -> tuple[dict[str, Any] | None, int, CommandError | None]:
    """Start, collect and close one run, forwarding every run diagnostic.

    `sources` are the read files listed in the brief.
    """
    output, errors = io.StringIO(), io.StringIO()
    temporary: Path | None = None
    created = False
    code = 0
    try:
        try:
            root = paths.runtime_dir(env)
            paths.ensure_private_dir(root, root=root)
            temporary = root / f"gapcheck-{os.getpid()}-{secrets.token_hex(3)}.md"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(brief.text(document, top, earlier, sources))
        except (OSError, paths.RuntimeDirError) as error:
            raise CommandError(
                69, str(error), exits.line(69, "runtime") or ""
            ) from error
        core = core_factory(
            env,
            stall_minutes=args.stall if args.stall is not None else STALL_MINUTES,
            quiet=False,
            out=output,
            err=errors,
        )
        options = StartOptions(
            engine,
            dir=top,
            task_file=str(temporary),
            model=args.model,
            effort=args.effort,
            read_only=True,
            read=tuple(args.read),
            trailers=(),
            claim=args.claim,
            window=args.window,
            parent=record.parent("gapcheck"),
        )
        with _stop_signals_interrupt():
            code = core.execute(lambda: core.start(options))
    finally:
        try:
            if created and temporary is not None:
                temporary.unlink(missing_ok=True)
        finally:
            diagnostics = errors.getvalue()
            if code == exits.PREPARATION_FAILED:
                diagnostics = "".join(
                    line
                    for line in diagnostics.splitlines(keepends=True)
                    if not line.startswith("next:")
                )
            err.write(diagnostics)
    first = output.getvalue().splitlines()
    match = re.fullmatch(r"run ([a-z0-9][a-z0-9-]{5,63})", first[0]) if first else None
    if match is None:
        return None, code, None
    run_id = match.group(1)
    result = _result(document, earlier)
    details: dict[str, Any] = {
        "id": run_id,
        "reply": None,
        "exit": code,
        "closed": False,
        "close_error": None,
    }
    result["run"] = details
    problem = None
    try:
        run = record.load(run_id, env)
        result["reviewer"] = {
            "engine": run.engine,
            "model": run.model,
            "effort": run.effort,
            "source": "option" if args.engine is not None else "menu",
        }
        candidate = Path(run.reply) if run.reply else run.folder / "turn-1.md"
        # A missing reply differs from an existing reply that cannot be read.
        try:
            candidate.stat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise CommandError(
                66,
                f"cannot read the reply {candidate}: {error}",
                f"next: ls -l {shlex.quote(str(candidate))}",
            ) from error
        else:
            details["reply"] = str(candidate)
            result.update(_read_reply(_read_file(candidate), document, earlier))
    except CommandError as error:
        problem = error
        result["parse_error"] = str(error)
    except record.RecordError as error:
        problem = CommandError(1, str(error), f"next: emb run --status {run_id}")
        result["parse_error"] = str(error)
    if code == 0:
        offset = len(errors.getvalue())
        close_code = core.execute(lambda: core.close(run_id))
        close_error = errors.getvalue()[offset:]
        err.write(close_error)
        details["closed"] = close_code == 0
        if close_code:
            details["close_error"] = next(
                (
                    line.removeprefix("emb run: ")
                    for line in close_error.splitlines()
                    if line.startswith("emb run: ")
                ),
                f"close exited {close_code}",
            )
    return result, problem.code if problem else code, problem


def main(
    args: argparse.Namespace,
    env: Mapping[str, str] | None,
    *,
    core_factory: Callable[..., Core] = Core,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run a gap check with the supplied streams and run factory.

    Args:
        args: Parsed gapcheck options.
        env: The environment; None reads os.environ.
        core_factory: Constructs the run core, including test drivers.
        out: Result output; None uses standard output.
        err: Diagnostics; None uses standard error.

    Returns:
        Zero for a covered plan, 81 for a bad reply, or an input or run exit.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    environment = {
        key: value
        for key, value in (os.environ if env is None else env).items()
        if key not in GIT_LOCATION
    }
    document = None
    command = "emb gapcheck"
    if args.plan is not None:
        command += f" --plan {shlex.quote(args.plan)}"
    try:
        _usage(args)
        if args.answer is not None:
            return _answer(args, out, err)
        assert args.plan is not None
        try:
            document = plan.load(args.plan)
        except plan.PlanError as error:
            raise CommandError(
                66, str(error), f"next: ls -l {shlex.quote(args.plan)}"
            ) from error
        command = f"emb gapcheck --plan {shlex.quote(str(document.path))}"
        if not document.text.strip():
            raise CommandError(
                64, f"the plan {document.path} is empty", "next: emb gapcheck --help"
            )
        empty_reads = []
        for item in args.read:
            path = Path(item).expanduser().absolute()
            try:
                if path.is_file() and not path.read_text(encoding="utf-8").strip():
                    empty_reads.append(item)
            except OSError, UnicodeError:
                continue
        if empty_reads:
            for item in empty_reads:
                print(f"emb gapcheck: the read file {item} is empty", file=err)
            print("next: emb gapcheck --help", file=err)
            return 64
        earlier = _load_previous(args.previous)
        if earlier is not None:
            command += f" --previous {shlex.quote(str(earlier.path))}"
            if earlier.plan != str(document.path):
                print(
                    f"WARNING: the previous result's plan {earlier.plan} differs "
                    f"from --plan {document.path}",
                    file=err,
                )
        problem = None
        reply_path = None
        settings = _settings(args, environment) if args.reply is None else None
        sources = citations.source_files(
            [
                *citations.standing(settings),
                *(str(Path(item).expanduser().absolute()) for item in args.read),
            ]
        )
        for item in citations.uncovered(
            citations.find("the plan", document.text), sources
        ):
            print(f"WARNING: {citations.warning(item)}", file=err)
        if settings is not None and not args.confirmed:
            if settings.get("source_error"):
                reason = "; ".join(settings.get("errors") or []) or "not valid"
                raise CommandError(
                    64, f"the settings cannot be read: {reason}", REVIEWER_LINE
                )
            decision, why = policy.decide(settings["answers"], len(document.steps))
            if decision != policy.RUN:
                _print_decision(document, decision, why, args, out, err)
                if decision == policy.ASK:
                    print(
                        "owner: ask whether to run a gap check on this plan; "
                        f"to run it: {command} ... --confirmed",
                        file=err,
                    )
                return 0
        if args.reply is not None:
            reply_path = str(Path(args.reply).expanduser().absolute())
            result = _result(document, earlier)
            result.update(_read_reply(_read_file(Path(reply_path)), document, earlier))
            code = 0
        else:
            top = _checkout(args.dir, environment, command)
            if args.brief:
                print(brief.text(document, top, earlier, sources), end="", file=out)
                return 0
            assert settings is not None
            engine = _engine(args, settings, command)
            result, code, problem = _run(
                args,
                document,
                earlier,
                top,
                engine,
                environment,
                core_factory,
                err,
                sources,
            )
            if result is None:
                if code:
                    print(
                        _preparation_retry(args)
                        if code == exits.PREPARATION_FAILED
                        else _follow(command, None, code),
                        file=err,
                    )
                return code
            reply_path = result["run"]["reply"]
        code = code or (INVALID_REPLY if result["parse_error"] else 0)
        _mark_asks(result, settings or _settings_quietly(environment))
        _print_result(result, args.json, out, earlier)
        if result["parse_error"]:
            print(f"emb gapcheck: {result['parse_error']}", file=err)
        if not document.failure_model:
            print(
                "WARNING: the plan has no failure model; "
                "every unsettled case is reported as a gap",
                file=err,
            )
        details = result["run"]
        if details and details["close_error"]:
            print(
                f"WARNING: the run {details['id']} stays open: "
                f"{details['close_error']}; "
                f"close it with emb run --close {details['id']}",
                file=err,
            )
        if code:
            print(
                _preparation_retry(args)
                if code == exits.PREPARATION_FAILED
                else problem.follow
                if problem
                else _follow(command, reply_path, code),
                file=err,
            )
        return code
    except CommandError as error:
        print(f"emb {error.origin}: {error}", file=err)
        print(error.follow, file=err)
        return error.code
    except KeyboardInterrupt:
        print("emb gapcheck: interrupted", file=err)
        print(f"next: {command} ...", file=err)
        return 74
