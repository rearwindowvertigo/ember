"""Review a plan through one read-only run, or read an existing reply.

Exit 81 reports an invalid or incomplete reply; other exits use the run's
codes. The command never creates or finishes a claim.
"""

import argparse
import io
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from ember import choices, db, paths
from ember.gapcheck import brief, plan, reply
from ember.runs import exits, record
from ember.runs.core import STALL_MINUTES, Core, StartOptions

INVALID_REPLY = 81
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
    "owner: name the reviewer for this gap check, or set Reviewer on the Team screen"
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
    if args.brief and args.reply is not None:
        problem = "--brief and --reply cannot be combined"
    elif args.brief or args.reply is not None:
        extra = [
            f"--{name}"
            for name in ("engine", "model", "effort", "claim", "stall")
            if getattr(args, name) is not None
        ]
        extra += [f"--{name}" for name in ("read", "window") if getattr(args, name)]
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


def _engine(args: argparse.Namespace, env: Mapping[str, str], command: str) -> str:
    """Check the claim before inspecting the reviewer row."""
    settings: dict[str, Any] | None = None
    if args.claim is not None or args.model is None:
        try:
            settings = choices.read(args.claim, env=env)
        except choices.InvalidChoiceError as error:
            raise CommandError(
                64, str(error), exits.line(64, "claim") or "", origin="run"
            ) from error
        except db.DatabaseError as error:
            raise CommandError(
                1, str(error), exits.line(1, "database") or ""
            ) from error
        if args.claim is not None:
            state = settings["claim"]["status"]
            if state in ("gone", "unknown"):
                raise CommandError(
                    64,
                    f"claim {args.claim} is {state}",
                    exits.line(64, "claim") or "",
                    origin="run",
                )
    if args.model is not None:
        return args.engine
    assert settings is not None
    menu = settings.get("menu")
    if settings.get("source_error") or menu is None:
        reason = "; ".join(settings.get("errors") or []) or "the menu is unavailable"
        raise CommandError(64, f"the settings cannot be read: {reason}", REVIEWER_LINE)
    row = menu["single.reviewer"]
    if row["state"] != "set":
        raise CommandError(
            64,
            f"single.reviewer is {row['state']}: {row['reason']}",
            REVIEWER_LINE,
        )
    if row["engine"] == "claude" and row["run_as"] == "subagent":
        raise CommandError(
            64,
            f"single.reviewer runs claude:{row['model']} as a subagent",
            f"next: {command} --brief, then give the brief to a read-only Claude "
            f"subagent on {row['model']} and read its answer with "
            f"{command} --reply <file>",
        )
    return args.engine if args.engine is not None else row["engine"]


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


def _result(document: plan.Plan) -> dict[str, Any]:
    """Build a result before a reply has been collected."""
    return {
        "plan": str(document.path),
        "plan_steps": document.kind,
        "failure_model": document.failure_model,
        "reviewer": None,
        "run": None,
        "steps": [],
        "ranked": [],
        "counts": dict.fromkeys(reply.HARMS, 0),
        "uncovered_steps": [step.number for step in document.steps],
        "unknown_steps": [],
        "parse_error": "the run produced no reply",
    }


def _print_result(result: dict[str, Any], as_json: bool, out: TextIO) -> None:
    """Print the result as JSON or one block per covered step."""
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
        return
    for step in result["steps"]:
        print(f"step {step['step']}: {step['title']}", file=out)
        if not step["gaps"]:
            print("  no gaps", file=out)
        for gap in step["gaps"]:
            print(f"  {gap['harm']}: {gap['gap']}", file=out)
            print(f"    why: {gap['why']}", file=out)
            print(f"    default: {gap['default']}", file=out)
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


def _run(
    args: argparse.Namespace,
    document: plan.Plan,
    top: str,
    engine: str,
    env: Mapping[str, str],
    core_factory: Callable[..., Core],
    err: TextIO,
) -> tuple[dict[str, Any] | None, int, CommandError | None]:
    """Start, collect and close one run, forwarding every run diagnostic."""
    output, errors = io.StringIO(), io.StringIO()
    temporary: Path | None = None
    created = False
    try:
        try:
            root = paths.runtime_dir(env)
            paths.ensure_private_dir(root, root=root)
            temporary = root / f"gapcheck-{os.getpid()}-{secrets.token_hex(3)}.md"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(brief.text(document, top))
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
        code = core.execute(
            lambda: core.start(
                StartOptions(
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
                )
            )
        )
    finally:
        try:
            if created and temporary is not None:
                temporary.unlink(missing_ok=True)
        finally:
            err.write(errors.getvalue())
    first = output.getvalue().splitlines()
    match = re.fullmatch(r"run ([a-z0-9][a-z0-9-]{5,63})", first[0]) if first else None
    if match is None:
        return None, code, None
    run_id = match.group(1)
    result = _result(document)
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
            result.update(reply.read(_read_file(candidate), document.steps))
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
    command = f"emb gapcheck --plan {shlex.quote(args.plan)}"
    try:
        _usage(args)
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
        problem = None
        reply_path = None
        if args.reply is not None:
            reply_path = str(Path(args.reply).expanduser().absolute())
            result = _result(document)
            result.update(reply.read(_read_file(Path(reply_path)), document.steps))
            code = 0
        else:
            top = _checkout(args.dir, environment, command)
            if args.brief:
                print(brief.text(document, top), end="", file=out)
                return 0
            engine = _engine(args, environment, command)
            result, code, problem = _run(
                args,
                document,
                top,
                engine,
                environment,
                core_factory,
                err,
            )
            if result is None:
                if code:
                    print(_follow(command, None, code), file=err)
                return code
            reply_path = result["run"]["reply"]
        code = code or (INVALID_REPLY if result["parse_error"] else 0)
        _print_result(result, args.json, out)
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
                problem.follow if problem else _follow(command, reply_path, code),
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
