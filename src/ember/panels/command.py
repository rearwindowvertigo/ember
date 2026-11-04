"""Ask panel members, report their answers and recover unfinished turns.

Exit 83 reports a member that did not answer or changed paths. Other exits
use ember.runs.exits.
"""

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, TextIO

from ember import choices, db, paths
from ember.gapcheck.command import GIT_LOCATION
from ember.panels import brief, members, record, run
from ember.runs import exits
from ember.runs import record as runs
from ember.runs.core import STALL_MINUTES, Core

SOME_FAILED = 83
TIMEOUT_MINUTES = 30.0


class CommandError(Exception):
    """A command diagnostic with its exit code and action."""

    def __init__(self, code: int, message: str, line: str) -> None:
        """Keep the diagnostic and its next or owner line."""
        super().__init__(message)
        self.code = code
        self.line = line


def _usage(args: argparse.Namespace) -> str:
    """Require one mode and reject options that do not apply to it."""
    modes = [
        name
        for name in ("question_file", "status", "recover", "retry", "close")
        if getattr(args, name) is not None
    ]
    problem = None
    if len(modes) != 1:
        problem = (
            "give exactly one of --question-file, --status, --recover, "
            "--retry or --close"
        )
    elif modes[0] != "question_file" and (
        args.dir is not None
        or args.member
        or args.claim is not None
        or args.read
        or args.dry_run
    ):
        problem = "--dir, --member, --claim, --read and --dry-run need --question-file"
    elif (args.stall is not None or args.timeout is not None) and (
        modes[0] in ("status", "close") or args.dry_run
    ):
        problem = (
            "--stall and --timeout need a start without --dry-run, --recover or --retry"
        )
    if problem:
        raise CommandError(exits.USAGE, problem, members.HELP_LINE)
    return modes[0]


@contextlib.contextmanager
def stop_handlers(stop: threading.Event) -> Iterator[None]:
    """Set the stop event on signals and restore the main thread's handlers."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handle(signum: int, frame: FrameType | None) -> None:
        """Request finalization without interrupting a record write."""
        stop.set()

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, handle)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _input_error(path: Path, message: str) -> CommandError:
    """Describe an unreadable input with a quoted path inspection command."""
    return CommandError(
        exits.NO_INPUT, message, f"next: ls -l {shlex.quote(str(path))}"
    )


def _checkout(directory: str | None, question: Path, env: Mapping[str, str]) -> str:
    """Resolve the checkout top using the cleaned environment."""
    folder = Path(directory or ".").expanduser().absolute()
    if not folder.is_dir():
        raise _input_error(folder, f"not a directory: {folder}")
    command = f"emb panel --question-file {shlex.quote(str(question))}"
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
            exits.UNAVAILABLE, f"git cannot run: {error}", "owner: install git"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise CommandError(
            exits.UNEXPECTED, "git rev-parse timed out after 60s", members.HELP_LINE
        ) from error
    if result.returncode or not result.stdout.strip():
        raise CommandError(
            exits.NOT_A_CHECKOUT,
            f"not a git checkout: {folder}",
            f"next: {command} --dir <a folder in the checkout>",
        )
    return result.stdout.strip()


def _settings(claim: str | None, env: Mapping[str, str]) -> dict[str, Any]:
    """Read settings once and refuse invalid or expired claims."""
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


def _left_out(
    items: Sequence[Mapping[str, str]], out: TextIO, prefix: str = ""
) -> None:
    """Print every excluded saved member with its reason."""
    for item in items:
        print(f"{prefix}left out {item['value']}: {item['reason']}", file=out)


def _dry_run(
    top: str, selection: members.Selection, as_json: bool, out: TextIO
) -> None:
    """Print resolved members without allocating records or checking launch tools."""
    entries = [
        {
            "n": n,
            "engine": m.engine,
            "model": m.model,
            "effort": m.effort,
            "source": m.source,
        }
        for n, m in enumerate(selection.members, 1)
    ]
    if as_json:
        print(
            json.dumps(
                {"dir": top, "members": entries, "left_out": selection.left_out},
                indent=2,
            ),
            file=out,
        )
        return
    print(f"checkout {top}", file=out)
    for item in entries:
        print(
            f"member {item['n']} {item['engine']}:{item['model']} · "
            f"{item['effort'] or 'default'} · {item['source']}",
            file=out,
        )
    _left_out(selection.left_out, out)


def _next(panel: Mapping[str, Any], summary: Path) -> str:
    """Choose one action for complete answers, failures or unfinished members."""
    if panel["exit"] == 0:
        return f"next: read {summary}, then compare the answers"
    unfinished = [m for m in panel["members"] if m["status"] in record.UNFINISHED]
    known = [m for m in unfinished if m["run"]]
    if known:
        return (
            f"next: emb panel --recover {panel['id']} "
            f"(collects {len(known)} unfinished members), or read {summary} now "
            "and close each open run with emb run --close <run> --force"
        )
    if unfinished or panel["exit"] == exits.INTERRUPTED:
        return (
            f"next: emb panel --status {panel['id']} and emb run --status; "
            "panel recovery cannot collect a member without a saved run id"
        )
    return (
        f"next: read {summary}; report each member that is not answered "
        "or changed paths to the owner"
    )


def _result(
    panel: Mapping[str, Any],
    env: Mapping[str, str],
    as_json: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    """Print the final result and exactly one next action."""
    folder = record.folder(panel["id"], env)
    summary = folder / record.SUMMARY_NAME
    line = _next(panel, summary)
    if as_json:
        result = {key: panel[key] for key in ("status", "exit", "dir", "left_out")}
        result.update(
            panel=panel["id"],
            folder=str(folder),
            summary=str(summary),
            members=[
                {key: value for key, value in m.items() if key != "attempts"}
                for m in panel["members"]
            ],
            next=line,
        )
        print(json.dumps(result, indent=2), file=out)
    else:
        _left_out(panel["left_out"], out)
        print(f"summary {summary}", file=out)
    print(line, file=err)
    return panel["exit"]


def _load(
    panel_id: str, env: Mapping[str, str], missing_code: int = 64
) -> dict[str, Any]:
    """Report invalid or missing panel ids with the mode's exit code."""
    if not record.is_panel_id(panel_id):
        raise CommandError(
            missing_code, f"invalid panel id: {panel_id}", "next: emb panel --status"
        )
    try:
        return record.load(panel_id, env)
    except record.PanelMissingError as error:
        raise CommandError(
            missing_code, str(error), "next: emb panel --status"
        ) from error


def _close_result(
    panel_id: str,
    results: list[dict[str, Any]],
    as_json: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    """Print each open run's close result and the aggregate exit."""
    code = 0 if all(item["closed"] for item in results) else exits.REFUSED
    line = f"next: emb panel --status {panel_id}"
    if as_json:
        print(
            json.dumps(
                {"panel": panel_id, "exit": code, "members": results, "next": line},
                indent=2,
            ),
            file=out,
        )
    else:
        for item in results:
            result = "closed" if item["closed"] else item["close_error"]
            print(f"member {item['n']} · run {item['run']} · {result}", file=out)
    print(line, file=err)
    return code


def _progress(panel: dict[str, Any], env: Mapping[str, str]) -> None:
    """Add run activity and current collection time to a status snapshot."""
    now = time.time()
    for member in panel["members"]:
        ids = [member.get("run"), *(a.get("run") for a in member["attempts"])]
        denials = list(member.get("denied_paths", []))
        for item in runs.referenced_denials(ids, env):
            if item not in denials:
                denials.append(item)
        member["denied_paths"] = denials
        member["last_screen_change_at"] = None
        if member["run"]:
            with contextlib.suppress(runs.RecordError):
                member["last_screen_change_at"] = runs.load(
                    member["run"], env
                ).last_screen_change_at
        if panel["status"] != "running" or member["status"] not in (
            "starting",
            "running",
        ):
            continue
        # A stall retry belongs to the same collection interval. Recovery and
        # replacement runs begin a new interval after the stored seconds.
        began = next(
            (
                attempt["at"]
                for attempt in reversed(member["attempts"])
                if attempt["kind"] in ("start", "recover", "retry")
            ),
            None,
        )
        if began is not None:
            member["seconds"] = (member["seconds"] or 0.0) + max(0.0, now - began)
    panel["denied_paths"] = [
        {**item, "member": member["n"]}
        for member in panel["members"]
        for item in member["denied_paths"]
    ]


def _status_member_line(member: Mapping[str, Any]) -> str:
    """Format member status with its last observed screen change."""
    activity = member["last_screen_change_at"]
    shown = "-" if activity is None else str(activity)
    line = f"{run.member_line(member)} · last screen change {shown}"
    for item in member.get("denied_paths", []):
        line += (
            f"\n  denied {item['path']} ({item['reason']}) "
            f"run {item['run']} turn {item['turn']}"
        )
    return line


def _status(
    panel_id: str, as_json: bool, env: Mapping[str, str], out: TextIO, err: TextIO
) -> None:
    """Print a complete record or summaries of readable panels oldest first."""
    if panel_id:
        panel = _load(panel_id, env)
        _progress(panel, env)
        panel["live"] = record.live(panel_id, env)
        if as_json:
            print(json.dumps(panel, indent=2), file=out)
        else:
            for key, value in panel.items():
                if key not in ("members", "left_out"):
                    shown = str(value).lower() if isinstance(value, bool) else value
                    print(f"{key}: {shown}", file=out)
            for member in panel["members"]:
                print(_status_member_line(member), file=out)
        return
    panels, errors = record.list_panels(env)
    for error in errors:
        print(f"emb panel: skipped {error}", file=err)
    items = []
    for panel in panels:
        _progress(panel, env)
        item = {
            key: panel[key] for key in ("id", "status", "dir", "exit", "started_at")
        }
        item.update(
            live=record.live(panel["id"], env),
            denied_paths=panel["denied_paths"],
            answered=sum(m["status"] == "answered" for m in panel["members"]),
            member_count=len(panel["members"]),
            running_members=[
                {key: m[key] for key in ("n", "seconds", "last_screen_change_at")}
                for m in panel["members"]
                if m["status"] in ("starting", "running")
            ],
        )
        items.append(item)
        if not as_json:
            question_path = record.folder(panel["id"], env) / record.QUESTION_NAME
            try:
                question = question_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                question = "(question unavailable)"
                print(f"emb panel: cannot read {question_path}: {error}", file=err)
            subject = brief.subject(panel["id"], question)[:60]
            print(
                f"{item['id']}  {item['status']}  "
                f"{item['answered']}/{item['member_count']} answered  "
                f"{item['dir']}  {subject}",
                file=out,
            )
            for member in panel["members"]:
                if (
                    member["status"] in ("starting", "running")
                    or member["denied_paths"]
                ):
                    print(_status_member_line(member), file=out)
    if as_json:
        print(json.dumps(items, indent=2), file=out)


def _prepare(
    args: argparse.Namespace,
    env: Mapping[str, str],
    core_factory: run.CoreFactory,
    err: TextIO,
) -> tuple[Path, str, str, list[str], members.Selection]:
    """Validate inputs and resolve members before allocating a panel."""
    question_path = Path(args.question_file).expanduser().absolute()
    try:
        question = question_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _input_error(
            question_path, f"cannot read {question_path}: {error}"
        ) from error
    if not question.strip():
        raise CommandError(
            64, f"the question {question_path} is empty", members.HELP_LINE
        )
    top = _checkout(args.dir, question_path, env)
    read = []
    for value in args.read:
        path = Path(value).expanduser().absolute()
        if not path.exists():
            raise _input_error(path, f"path does not exist: {path}")
        read.append(str(path))
    settings = _settings(args.claim, env)
    core = core_factory(env, quiet=True)
    selection = members.select(args.member, settings, env, core.load_driver)
    _left_out(selection.left_out, err, "emb panel: ")
    return question_path, question, top, read, selection


def main(
    args: argparse.Namespace,
    env: Mapping[str, str] | None,
    *,
    core_factory: run.CoreFactory = Core,
    spacing: float = run.START_SPACING,
    poll_seconds: float = run.POLL_SECONDS,
    stop: threading.Event | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Execute a panel command with injectable drivers, timing and streams."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    environment = {
        key: value
        for key, value in (os.environ if env is None else env).items()
        if key not in GIT_LOCATION
    }
    stop = stop if stop is not None else threading.Event()
    panel_id = None
    mode = None
    panel = None
    question = ""
    try:
        mode = _usage(args)
        if mode == "status":
            _status(args.status, args.json, environment, out, err)
            return exits.OK
        stall = args.stall if args.stall is not None else STALL_MINUTES
        timeout = args.timeout if args.timeout is not None else TIMEOUT_MINUTES
        if mode == "question_file":
            question_path, question, top, read, selection = _prepare(
                args, environment, core_factory, err
            )
            if args.dry_run:
                _dry_run(top, selection, args.json, out)
                return exits.OK
            if shutil.which("tmux", path=environment.get("PATH", os.defpath)) is None:
                raise CommandError(69, "tmux is not on PATH", "owner: install tmux")
            panel_id = record.new_id()
            panel = record.new_panel(
                panel_id,
                dir=top,
                question_file=str(question_path),
                read=read,
                claim=args.claim,
                stall_minutes=stall,
                timeout_minutes=timeout,
                members=[m.entry(n) for n, m in enumerate(selection.members, 1)],
                left_out=selection.left_out,
            )
        else:
            panel_id = args.retry[0] if mode == "retry" else getattr(args, mode)
            _load(panel_id, environment, 65 if mode in ("retry", "close") else 64)
        with record.hold(panel_id, environment), stop_handlers(stop):
            if mode == "close":
                return _close_result(
                    panel_id,
                    run.close(
                        panel_id, environment, core_factory=core_factory, err=err
                    ),
                    args.json,
                    out,
                    err,
                )
            if mode == "question_file":
                assert panel is not None
                try:
                    record.create(
                        panel, question, brief.text(panel_id, question), environment
                    )
                except (OSError, paths.RuntimeDirError) as error:
                    raise CommandError(
                        69, str(error), exits.line(69, "runtime") or ""
                    ) from error
            if not args.json:
                print(f"panel {panel_id}", file=out, flush=True)
            if mode == "question_file":
                outcome = run.start(
                    panel_id,
                    environment,
                    core_factory=core_factory,
                    stop=stop,
                    spacing=spacing,
                    poll_seconds=poll_seconds,
                    err=err,
                )
            elif mode == "retry":
                outcome = run.retry(
                    panel_id,
                    args.retry[1],
                    environment,
                    stall_minutes=stall,
                    timeout_minutes=timeout,
                    core_factory=core_factory,
                    stop=stop,
                    poll_seconds=poll_seconds,
                    err=err,
                )
            else:
                outcome = run.recover(
                    panel_id,
                    environment,
                    stall_minutes=stall,
                    timeout_minutes=timeout,
                    core_factory=core_factory,
                    stop=stop,
                    poll_seconds=poll_seconds,
                    err=err,
                )
            return _result(outcome.panel, environment, args.json, out, err)
    except members.MemberError as error:
        _left_out(error.left_out, err, "emb panel: ")
        code, message, line = error.code, str(error), error.line
    except CommandError as error:
        code, message, line = error.code, str(error), error.line
    except record.Busy:
        code = (
            exits.REFUSED if mode in ("recover", "retry", "close") else exits.UNEXPECTED
        )
        message = f"the panel {panel_id} is still running"
        line = f"next: emb panel --status {panel_id}"
    except run.NothingToRecover as error:
        code, message = exits.REFUSED, str(error)
        line = f"next: emb panel --status {panel_id}"
    except (run.RetryRefused, run.MemberMissingError) as error:
        code = 65 if isinstance(error, run.MemberMissingError) else exits.REFUSED
        message = str(error)
        line = f"next: emb panel --status {panel_id}"
    except (record.LockError, paths.RuntimeDirError) as error:
        code, message, line = exits.UNAVAILABLE, str(error), exits.line(69, "runtime")
    except Exception as error:
        code, message = exits.UNEXPECTED, str(error)
        if panel_id:
            message = f"panel {panel_id}: {message}"
        line = "next: emb panel --status" + (f" {panel_id}" if panel_id else "")
    print(f"emb panel: {message}", file=err)
    print(line, file=err)
    return code
