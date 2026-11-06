"""The program a Claude run's pane runs: each requested turn, with its progress.

Usage: `python -P -m ember.engines.claude_host <folder> <worktree> <turn>`.
It writes `ready` in the driver's folder, then waits for `turn-<n>.json`
from `<turn>` on, runs that request's argv in the worktree with
`prompt-<n>.md` on standard input, copies standard output to
`turn-<n>.jsonl` and standard error to `turn-<n>.stderr`, and writes
`turn-<n>.exit` when the CLI ends. The CLI runs without the variables of the
session that started Ember, with each request's environment overrides applied
only to that turn. When the request names a sandbox, the CLI runs
inside `ember.sandbox.claude_wrap`; the program itself runs outside it. While
a tool call or a model message is in progress, the program prints a status
line at least once a minute. The program never ends by itself; ending the
session ends it. Ctrl+C in the pane ends the running turn as interrupted and
leaves the program running.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from ember import paths, sandbox
from ember.engines.claude import READY_NAME, turn_files
from ember.engines.status import CLAUDE_HIDDEN

POLL_SECONDS = 0.2
"""How often the program looks for the next request."""

STATUS_SECONDS = 60.0
"""The longest time without output before the program prints a status line."""

WAITING = "waiting for the model"
"""The activity between tool calls."""

_LINE_LIMIT = 160


def cli_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Return the CLI's environment.

    Args:
        env: The program's environment.

    Returns:
        A copy of `env` without the `CLAUDE_HIDDEN` variables.
    """
    return {
        key: value for key, value in env.items() if not key.startswith(CLAUDE_HIDDEN)
    }


def _first_line(value: object) -> str:
    text = str(value)
    return text.splitlines()[0][:_LINE_LIMIT] if text else ""


def show(event: Mapping[str, Any]) -> str:
    """Return the readable lines of one `stream-json` event.

    Args:
        event: The event.

    Returns:
        The lines to print; empty for an event that shows nothing.
    """
    kind = event.get("type")
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = (
        [block for block in content if isinstance(block, dict)]
        if isinstance(content, list)
        else []
    )
    if kind == "system" and event.get("subtype") == "init":
        return f"· session {event.get('session_id', '?')} · {event.get('model', '?')}"
    if kind == "assistant":
        lines = []
        for block in blocks:
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str) and text.strip():
                lines.append(text.rstrip())
            elif block.get("type") == "tool_use":
                detail = block.get("input")
                detail = detail if isinstance(detail, dict) else {}
                target = (
                    detail.get("command")
                    or detail.get("file_path")
                    or detail.get("pattern")
                    or ""
                )
                lines.append(f"→ {block.get('name')}: {_first_line(target)}")
        return "\n".join(lines)
    if kind == "user":
        for block in blocks:
            if block.get("type") == "tool_result" and block.get("is_error"):
                result = block.get("content")
                text = result if isinstance(result, str) else json.dumps(result)
                return f"  ✗ {_first_line(text) or 'tool error'}"
        return ""
    if kind == "result":
        seconds = event.get("duration_ms")
        seconds = round(seconds / 1000) if isinstance(seconds, (int, float)) else 0
        return f"· turn ended · {event.get('subtype', '')} · {seconds}s"
    return ""


class Progress:
    """What the running turn is doing, for the status line."""

    def __init__(self, now: float, every: float = STATUS_SECONDS) -> None:
        """Start a turn that waits for the model.

        Args:
            now: The current monotonic time.
            every: The longest time without output before a status line.
        """
        self.every = every
        self.activity = WAITING
        self.since = now
        self.printed = now
        self.lock = threading.Lock()

    def event(self, event: Mapping[str, Any], now: float) -> None:
        """Follow one `stream-json` event.

        A tool call starts `running <tool>`; anything else from the turn's
        own messages, such as a tool's result or the model's text, starts
        waiting for the model.

        Args:
            event: The event.
            now: The current monotonic time.
        """
        if event.get("parent_tool_use_id"):
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = (
            [block for block in content if isinstance(block, dict)]
            if isinstance(content, list)
            else []
        )
        tools = [
            str(block.get("name") or "a tool")
            for block in blocks
            if block.get("type") == "tool_use"
        ]
        activity = f"running {tools[-1]}" if tools else WAITING
        with self.lock:
            if activity != self.activity or tools:
                self.activity, self.since = activity, now

    def shown(self, now: float) -> None:
        """Note that the turn printed a line.

        Args:
            now: The current monotonic time.
        """
        with self.lock:
            self.printed = now

    def line(self, now: float) -> str | None:
        """Return the status line due now, and note it as printed.

        Args:
            now: The current monotonic time.

        Returns:
            `· <activity> for <n> min`, or `for <n> s` under a minute, when
            nothing was printed for `every` seconds; otherwise `None`.
        """
        with self.lock:
            if now - self.printed < self.every:
                return None
            self.printed = now
            seconds = int(now - self.since)
            took = f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"
            return f"· {self.activity} for {took}"


def _write_exit(path: Path, code: int) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"code": code}), encoding="utf-8")
    temporary.replace(path)


class Host:
    """Runs one run's turns in order."""

    def __init__(
        self,
        folder: Path,
        worktree: str,
        env: Mapping[str, str],
        *,
        status_seconds: float = STATUS_SECONDS,
    ) -> None:
        """Keep the driver's folder, the worktree and the CLI's environment.

        Args:
            folder: The driver's folder.
            worktree: The working folder of each turn.
            env: The program's environment.
            status_seconds: The longest time without output before a status
                line.
        """
        self.folder = folder
        self.worktree = worktree
        self.outer_env = dict(env)
        self.env = cli_environment(env)
        self.status_seconds = status_seconds
        self.running: int | None = None

    def on_interrupt(self, signum: int, frame: FrameType | None) -> None:
        """Mark the running turn interrupted; the CLI gets the same Ctrl+C.

        Args:
            signum: The signal.
            frame: The interrupted frame.
        """
        if self.running is not None:
            path = turn_files(self.folder, self.running)["interrupt"]
            with contextlib.suppress(OSError):
                path.write_text("interrupted\n", encoding="utf-8")

    def run_turn(self, turn: int) -> int:
        """Run one requested turn and write its exit file.

        Args:
            turn: The turn.

        Returns:
            The CLI's exit code; 127 when it could not start.
        """
        files = turn_files(self.folder, turn)
        print(f"\n=== turn {turn} ===", flush=True)
        try:
            request = json.loads(files["json"].read_text(encoding="utf-8"))
            argv = [str(item) for item in request["argv"]]
            prompt_path = Path(request["prompt"])
            box = request.get("sandbox")
            overrides = request.get("env", {})
            if not isinstance(overrides, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in overrides.items()
            ):
                raise TypeError("the request's env is not a string mapping")
            env = cli_environment({**self.env, **overrides})
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            return self._refuse(
                files, f"cannot read the request of turn {turn}: {error}"
            )
        try:
            argv = self.wrap(argv, box)
        except (KeyError, TypeError, paths.RuntimeDirError) as error:
            return self._refuse(
                files, f"cannot build the sandbox of turn {turn}: {error}"
            )
        self.running = turn
        try:
            code = self._run(argv, prompt_path, files, env)
        finally:
            self.running = None
        _write_exit(files["exit"], code)
        print("· waiting for the next turn", flush=True)
        return code

    def _refuse(self, files: Mapping[str, Path], reason: str) -> int:
        """End a turn that cannot start, with its reason as standard error."""
        files["stderr"].write_text(reason + "\n", encoding="utf-8")
        _write_exit(files["exit"], 127)
        print(f"· {reason}", flush=True)
        return 127

    def wrap(self, argv: list[str], box: object) -> list[str]:
        """Return a turn's command, inside the sandbox the request names.

        Args:
            argv: The `claude` command.
            box: The request's `sandbox`: `None`, or the worktree, common git
                folder, read paths, read-only mode and network value.

        Returns:
            `argv` itself for no sandbox; otherwise its `bwrap` command line.

        Raises:
            TypeError: The sandbox is not as the driver writes it.
            RuntimeDirError: No runtime base directory is available.
        """
        if box is None:
            return argv
        if not isinstance(box, dict):
            raise TypeError("the request's sandbox is not an object")
        read = box["read"]
        if not isinstance(read, list):
            raise TypeError("the request's read paths are not a list")
        return sandbox.claude_wrap(
            argv,
            worktree=Path(str(box["worktree"])),
            common=Path(str(box["common"])),
            read=[str(item) for item in read],
            read_only=box["read_only"] is True,
            network=box["network"] is True,
            env=self.outer_env,
        )

    def _report(self, progress: Progress, done: threading.Event) -> None:
        while not done.wait(1.0):
            text = progress.line(time.monotonic())
            if text:
                print(text, flush=True)

    def _run(
        self,
        argv: Sequence[str],
        prompt_path: Path,
        files: Mapping[str, Path],
        env: Mapping[str, str],
    ) -> int:
        """Run a turn with its environment and periodic progress reports."""
        progress = Progress(time.monotonic(), self.status_seconds)
        done = threading.Event()
        reporter = threading.Thread(
            target=self._report, args=(progress, done), daemon=True
        )
        reporter.start()
        try:
            return self._stream(argv, prompt_path, files, progress, env)
        finally:
            done.set()
            reporter.join()

    def _stream(
        self,
        argv: Sequence[str],
        prompt_path: Path,
        files: Mapping[str, Path],
        progress: Progress,
        env: Mapping[str, str],
    ) -> int:
        """Stream the CLI's output using the turn's environment."""
        with files["jsonl"].open("wb") as output, files["stderr"].open("wb") as errors:
            try:
                with prompt_path.open("rb") as prompt:
                    process = subprocess.Popen(
                        argv,
                        cwd=self.worktree,
                        stdin=prompt,
                        stdout=subprocess.PIPE,
                        stderr=errors,
                        env=env,
                    )
            except OSError as error:
                errors.write(f"cannot start claude: {error}\n".encode())
                return 127
            files["pid"].write_text(str(process.pid), encoding="utf-8")
            assert process.stdout is not None
            for line in process.stdout:
                output.write(line)
                output.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                text = ""
                if isinstance(event, dict):
                    progress.event(event, time.monotonic())
                    text = show(event)
                if text:
                    print(text, flush=True)
                    progress.shown(time.monotonic())
            return process.wait()

    def serve(self, turn: int) -> None:
        """Run turns from `turn` on, forever.

        Args:
            turn: The first turn to look for.
        """
        (self.folder / READY_NAME).write_text("", encoding="utf-8")
        while True:
            files = turn_files(self.folder, turn)
            if files["exit"].exists():
                turn += 1
            elif files["json"].exists():
                self.run_turn(turn)
                turn += 1
            else:
                time.sleep(POLL_SECONDS)


def main(argv: Sequence[str]) -> int:
    """Run the program.

    Args:
        argv: The program name, the driver's folder, the worktree and the
            first turn.

    Returns:
        2 for wrong arguments; otherwise it does not return.
    """
    if len(argv) != 4 or not argv[3].isdigit():
        print(
            "usage: python -m ember.engines.claude_host <folder> <worktree> <turn>",
            file=sys.stderr,
        )
        return 2
    host = Host(Path(argv[1]), argv[2], os.environ)
    signal.signal(signal.SIGINT, host.on_interrupt)
    host.serve(int(argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
