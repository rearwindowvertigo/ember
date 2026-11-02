"""The Claude Code driver: one non-interactive `claude -p` per turn.

The run's pane runs `ember.engines.claude_host`, which waits for numbered
turn requests in the driver's folder, `claude/` in the run folder, runs each
one in the worktree and shows its progress. For turn `n` the folder holds:

| File | Holds |
|---|---|
| `prompt-<n>.md` | The message, given to the CLI on standard input |
| `turn-<n>.json` | The request: the turn, the prompt file and the argv |
| `turn-<n>.jsonl` | The CLI's `stream-json` output |
| `turn-<n>.stderr` | The CLI's standard error |
| `turn-<n>.pid` | The CLI's process id while it runs |
| `turn-<n>.exit` | The CLI's exit code, written when the turn ends |
| `turn-<n>.interrupt` | Present when the turn was interrupted |

The host also writes `ready` there when it starts.

Turn 1's request is written by `ClaudeDriver.command`, so the brief goes in
without a paste. Later turns resume the CLI session that the newest earlier
turn reported.
"""

import json
import os
import re
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember import choices, db, engines
from ember.engines import DriverError, IdleState, LaunchSpec, TurnOutcome, models
from ember.engines import status as engine_status

if TYPE_CHECKING:
    from ember.runs.record import Run

FOLDER_NAME = "claude"
"""The driver's folder inside the run folder."""

HOST_MODULE = "ember.engines.claude_host"
"""The module the pane runs."""

READY_NAME = "ready"
"""The file the host writes in the driver's folder once it runs."""

WRITE_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
"""The tools of an editing run."""

READ_TOOLS = ("Read", "Glob", "Grep")
"""The tools of a read-only run."""

NETWORK_TOOLS = ("WebSearch", "WebFetch")
"""The tools an editing run also gets when `worker.network` is `on`."""

GIT_WRITES = (
    "add", "commit", "push", "merge", "rebase", "reset", "checkout", "switch",
    "branch", "cherry-pick", "tag", "stash", "restore", "rm", "mv", "am",
    "apply", "pull", "fetch",
)  # fmt: skip
"""The git commands refused to the `Bash` tool.

The core's check of the branch and `HEAD` after each turn is what finds a git
write; this list keeps the model from trying the common ones.
"""

PROVIDER_ERROR = re.compile(
    r"usage limit|hit your limit|limit reached|rate.?limit|overloaded|credit balance",
    re.IGNORECASE,
)
"""Text of a refusal for the account's limits or the model server's load."""

SIGNED_OUT = re.compile(
    r"not logged in|please run /login|invalid api key|authentication_failed"
    r"|oauth token has expired",
    re.IGNORECASE,
)
"""Text of a sign-in failure."""

SIGNED_OUT_ERROR = "authentication_failed"
"""The `error` field of the assistant event the CLI writes when signed out."""

_REASON_LIMIT = 500


def driver(env: Mapping[str, str] | None = None) -> ClaudeDriver:
    """Return the Claude Code driver.

    Args:
        env: The run's environment; `None` reads `os.environ`.

    Returns:
        A new driver.
    """
    return ClaudeDriver(env)


def folder(run: Run) -> Path:
    """Return the driver's folder of a run.

    Args:
        run: The run.

    Returns:
        `claude/` in the run folder.
    """
    return run.folder / FOLDER_NAME


def turn_files(base: Path, turn: int) -> dict[str, Path]:
    """Return the files of one turn.

    Args:
        base: The driver's folder.
        turn: The turn.

    Returns:
        The paths by kind: `prompt`, `json`, `jsonl`, `stderr`, `pid`, `exit`
        and `interrupt`.
    """
    files = {
        kind: base / f"turn-{turn}.{kind}"
        for kind in ("json", "jsonl", "stderr", "pid", "exit", "interrupt")
    }
    files["prompt"] = base / f"prompt-{turn}.md"
    return files


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read the complete `stream-json` lines of a turn.

    A last line without its newline is still being written and is left for
    the next read. Lines that are not JSON objects are skipped.

    Args:
        path: The turn's `.jsonl` file.

    Returns:
        The events, in order; empty when the file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.split("\n")[:-1]:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _blocks(event: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return None
    return [block for block in content if isinstance(block, dict)]


def final_reply(events: list[dict[str, Any]]) -> str:
    """Return a turn's last message from its events.

    The reply is every non-empty assistant text block after the turn's last
    tool result, in order; with no tool result, every assistant text block.
    Blocks are joined with a blank line, each without its leading and
    trailing newlines, except across a user message with no tool result:
    there the two texts join with one newline. The CLI adds such a message
    when a reply reaches the output token limit and the model goes on in a
    new message, so a table cut there stays whole. The result event holds
    only the last text block, so it is not used. Events with a
    `parent_tool_use_id` belong to a subagent and do not count.

    Args:
        events: The turn's events.

    Returns:
        The reply; empty when the turn wrote no text.
    """
    parts: list[str] = []
    resume = False
    for event in events:
        if event.get("parent_tool_use_id"):
            continue
        blocks = _blocks(event)
        if blocks is None:
            continue
        if event.get("type") == "user":
            if any(block.get("type") == "tool_result" for block in blocks):
                parts, resume = [], False
            else:
                resume = bool(parts)
        elif event.get("type") == "assistant":
            for block in blocks:
                text = block.get("text")
                if (
                    block.get("type") != "text"
                    or not isinstance(text, str)
                    or not text.strip()
                ):
                    continue
                if resume:
                    parts[-1] = parts[-1].rstrip("\n") + "\n" + text.lstrip("\n")
                    resume = False
                else:
                    parts.append(text)
    return "\n\n".join(part.strip("\n") for part in parts)


def session_of(events: list[dict[str, Any]]) -> str | None:
    """Return the CLI session a turn reported.

    Args:
        events: The turn's events.

    Returns:
        The result event's `session_id`, else the first system event's;
        `None` when neither has one.
    """
    result = _result(events)
    if result is not None and isinstance(result.get("session_id"), str):
        return result["session_id"] or None
    init = next((event for event in events if event.get("type") == "system"), None)
    value = (init or {}).get("session_id")
    return value if isinstance(value, str) and value else None


def _result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (event for event in reversed(events) if event.get("type") == "result"), None
    )


def _signed_out_event(events: list[dict[str, Any]]) -> bool:
    return any(
        event.get("type") == "assistant"
        and not event.get("parent_tool_use_id")
        and event.get("error") == SIGNED_OUT_ERROR
        for event in events
    )


def running_turn(base: Path) -> int | None:
    """Return the lowest requested turn that has not ended.

    Args:
        base: The driver's folder.

    Returns:
        The turn, or `None` when every requested turn has ended.
    """
    turns = []
    for request in base.glob("turn-*.json"):
        number = request.name[len("turn-") : -len(".json")]
        if number.isdigit():
            turns.append(int(number))
    for turn in sorted(turns):
        if not turn_files(base, turn)["exit"].exists():
            return turn
    return None


class ClaudeDriver(engines.Driver):
    """Runs Claude Code one `claude -p` turn at a time in a run's session."""

    name = "claude"

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Keep the run's environment.

        Args:
            env: The run's environment; `None` reads `os.environ`.
        """
        self.env = env

    # Commands

    def _claude(self) -> str:
        binary = engine_status.locate("claude", self.env)
        if binary is None:
            raise DriverError("claude (the Claude Code CLI) is not on PATH")
        return binary

    def _network(self) -> bool:
        try:
            reply = choices.read(env=self.env)
        except (choices.InvalidChoiceError, db.DatabaseError) as error:
            raise DriverError(f"cannot read worker.network: {error}") from error
        return reply["answers"]["worker.network"]["value"] == "on"

    def tools(self, run: Run) -> tuple[str, ...]:
        """Return the tools a run's turns may use.

        Args:
            run: The run.

        Returns:
            The read tools for a read-only run; otherwise the editing tools,
            with the web tools when `worker.network` is `on`.

        Raises:
            DriverError: The settings cannot be read.
        """
        if run.read_only:
            return READ_TOOLS
        if self._network():
            return (*WRITE_TOOLS, *NETWORK_TOOLS)
        return WRITE_TOOLS

    def turn_argv(self, run: Run, session: str | None) -> list[str]:
        """Return one turn's command.

        `--restricted` keeps the file tools in the working folder and loads no
        `CLAUDE.md`, memory or settings files; `--safe-mode` loads no plugins
        or hooks; `--strict-mcp-config` with no `--mcp-config` loads no MCP
        server.

        Args:
            run: The run.
            session: The CLI session to resume, or `None` for a new one.

        Returns:
            The argv.

        Raises:
            DriverError: The CLI is not on `PATH`, or the settings cannot be
                read.
        """
        tools = ",".join(self.tools(run))
        argv = [
            self._claude(), "-p", "--output-format", "stream-json", "--verbose",
            "--model", run.model,
            *(() if run.effort is None else ("--effort", run.effort)),
            "--restricted", "--safe-mode", "--strict-mcp-config",
            "--tools", tools, "--allowedTools", tools,
            "--disallowedTools", ",".join(f"Bash(git {verb}:*)" for verb in GIT_WRITES),
            "--permission-mode", "default" if run.read_only else "acceptEdits",
            "--permission-prompts", "none",
        ]  # fmt: skip
        if session:
            argv += ["--resume", session]
        return argv

    def _session(self, base: Path, turn: int) -> str | None:
        for earlier in range(turn - 1, 0, -1):
            session = session_of(read_events(turn_files(base, earlier)["jsonl"]))
            if session:
                return session
        return None

    def _request(self, run: Run, turn: int, text: str) -> None:
        """Write a turn's prompt and request; the request appears atomically."""
        base = folder(run)
        files = turn_files(base, turn)
        if files["json"].exists():
            raise DriverError(f"turn {turn} of {run.id} was already requested")
        argv = self.turn_argv(run, self._session(base, turn))
        request = {"turn": turn, "prompt": str(files["prompt"]), "argv": argv}
        temporary = files["json"].with_suffix(".tmp")
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            files["prompt"].write_text(text + "\n", encoding="utf-8")
            temporary.write_text(json.dumps(request), encoding="utf-8")
            temporary.replace(files["json"])
        except OSError as error:
            raise DriverError(
                f"cannot write turn {turn} of {run.id}: {error}"
            ) from error

    def command(self, run: Run, first_message: str) -> LaunchSpec:
        """Request turn 1 and return the host program's command.

        Args:
            run: The run, with status `starting`.
            first_message: The attributed brief.

        Returns:
            `python -P -m ember.engines.claude_host <folder> <worktree> 1` in
            the run's working folder, with no message to paste.

        Raises:
            DriverError: The CLI is not on `PATH`, the settings cannot be
                read, or the request cannot be written.
        """
        self._request(run, 1, first_message)
        argv = (
            sys.executable, "-P", "-m", HOST_MODULE,
            str(folder(run)), run.worktree, "1",
        )  # fmt: skip
        return LaunchSpec(argv=argv, cwd=Path(run.worktree), first_message=None)

    def ready(self, run: Run) -> bool:
        """Return whether the host program runs.

        Args:
            run: The run.

        Returns:
            `True` once the host has written its `ready` file.
        """
        return (folder(run) / READY_NAME).exists()

    def send(self, run: Run, text: str) -> None:
        """Request the current turn, `run.turn`, with a message.

        Args:
            run: The run, with the new turn recorded.
            text: The message.

        Raises:
            DriverError: The turn was already requested, or its request
                cannot be written.
        """
        self._request(run, run.turn, text)

    # Turns

    def wait(self, run: Run) -> TurnOutcome:
        """Observe the current turn from its files.

        Args:
            run: The run.

        Returns:
            `running` until the turn's exit file exists; then `done` with the
            final reply, or `failed` for an interrupted turn, a CLI that could
            not start, ended with an error or ended without a result.

        Raises:
            DriverError: The current turn was never requested.
        """
        files = turn_files(folder(run), run.turn)
        if not files["exit"].exists():
            if not files["json"].exists():
                raise DriverError(f"turn {run.turn} of {run.id} was not requested")
            return TurnOutcome(engines.RUNNING)
        events = read_events(files["jsonl"])
        result = _result(events)
        try:
            ended = json.loads(files["exit"].read_text(encoding="utf-8"))
            code = ended.get("code") if isinstance(ended, dict) else None
        except OSError, ValueError:
            code = None
        if files["interrupt"].exists():
            return TurnOutcome(
                engines.FAILED, reason=f"turn {run.turn} of {run.id} was interrupted"
            )
        if result is not None and not result.get("is_error") and code == 0:
            reply = final_reply(events) or str(result.get("result") or "")
            return TurnOutcome(engines.DONE, reply=reply)
        try:
            stderr = files["stderr"].read_text(errors="replace").strip()
        except OSError:
            stderr = ""
        text = " ".join(filter(None, [str((result or {}).get("result") or ""), stderr]))
        error = (text.splitlines() or [""])[-1][:_REASON_LIMIT]
        if not error:
            error = (
                "the CLI ended without a result"
                if code is None or code == 0
                else f"the CLI exited {code} without a result"
            )
        if _signed_out_event(events) or SIGNED_OUT.search(text):
            return TurnOutcome(
                engines.FAILED,
                reason=f"Claude Code is not signed in: {error}",
                signed_out=True,
            )
        if PROVIDER_ERROR.search(text):
            return TurnOutcome(
                engines.FAILED,
                reason=f"the model server refused the turn: {error}",
                provider_error=True,
            )
        return TurnOutcome(engines.FAILED, reason=f"claude failed: {error}")

    def idle_state(self, run: Run) -> IdleState:
        """Return whether a turn is running.

        Args:
            run: The run.

        Returns:
            `busy` while a requested turn has not ended, else `idle`.
        """
        turn = running_turn(folder(run))
        if turn is not None:
            return IdleState(engines.BUSY, f"turn {turn} is running")
        return IdleState(engines.IDLE, "no turn is running")

    def interrupt(self, run: Run) -> None:
        """Mark the running turn interrupted and send the CLI `SIGINT`.

        The host program keeps running and takes the next turn.

        Args:
            run: The run.

        Raises:
            DriverError: No turn is running, or its CLI cannot be signalled.
        """
        base = folder(run)
        turn = running_turn(base)
        if turn is None:
            raise DriverError(f"no turn is running in {run.id}")
        files = turn_files(base, turn)
        try:
            files["interrupt"].write_text("interrupted\n", encoding="utf-8")
            pid = int(files["pid"].read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGINT)
        except (OSError, ValueError) as error:
            raise DriverError(
                f"cannot signal turn {turn} of {run.id}: {error}"
            ) from error

    # Efforts

    def valid_effort(self, effort: str) -> bool:
        """Return whether `claude --effort` takes an effort.

        Args:
            effort: The effort.

        Returns:
            `True` for `low`, `medium`, `high`, `xhigh` and `max`.
        """
        return effort in models.CLAUDE_EFFORTS

    def default_effort(self, model: str) -> str | None:
        """Leave the effort to Claude Code.

        Args:
            model: The model.

        Returns:
            `None`.
        """
        return None
