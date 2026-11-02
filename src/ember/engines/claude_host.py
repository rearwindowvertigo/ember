"""The program a Claude run's pane runs: each requested turn, with its progress.

Usage: `python -P -m ember.engines.claude_host <folder> <worktree> <turn>`.
It writes `ready` in the driver's folder, then waits for `turn-<n>.json`
from `<turn>` on, runs that request's argv in the worktree with
`prompt-<n>.md` on standard input, copies standard output to
`turn-<n>.jsonl` and standard error to `turn-<n>.stderr`, and writes
`turn-<n>.exit` when the CLI ends. The CLI runs without the variables of the
session that started Ember. The program never ends by itself; ending the
session ends it. Ctrl+C in the pane ends the running turn as interrupted and
leaves the program running.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from ember.engines.claude import READY_NAME, turn_files
from ember.engines.status import CLAUDE_HIDDEN

POLL_SECONDS = 0.2
"""How often the program looks for the next request."""

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


def _write_exit(path: Path, code: int) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"code": code}), encoding="utf-8")
    temporary.replace(path)


class Host:
    """Runs one run's turns in order."""

    def __init__(self, folder: Path, worktree: str, env: Mapping[str, str]) -> None:
        """Keep the driver's folder, the worktree and the CLI's environment.

        Args:
            folder: The driver's folder.
            worktree: The working folder of each turn.
            env: The program's environment.
        """
        self.folder = folder
        self.worktree = worktree
        self.env = cli_environment(env)
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
        except (OSError, ValueError, KeyError, TypeError) as error:
            files["stderr"].write_text(
                f"cannot read the request of turn {turn}: {error}\n", encoding="utf-8"
            )
            _write_exit(files["exit"], 127)
            return 127
        self.running = turn
        try:
            code = self._run(argv, prompt_path, files)
        finally:
            self.running = None
        _write_exit(files["exit"], code)
        print("· waiting for the next turn", flush=True)
        return code

    def _run(
        self, argv: Sequence[str], prompt_path: Path, files: Mapping[str, Path]
    ) -> int:
        with files["jsonl"].open("wb") as output, files["stderr"].open("wb") as errors:
            try:
                with prompt_path.open("rb") as prompt:
                    process = subprocess.Popen(
                        argv,
                        cwd=self.worktree,
                        stdin=prompt,
                        stdout=subprocess.PIPE,
                        stderr=errors,
                        env=self.env,
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
                    text = show(event) if isinstance(event, dict) else ""
                except ValueError:
                    text = ""
                if text:
                    print(text, flush=True)
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
