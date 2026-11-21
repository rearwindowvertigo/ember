"""The Codex driver.

Codex runs its interactive session in the run's pane. The command line
carries no prompt: tmux takes the whole session command in one message of at
most about 16 KB, which also holds the session's environment and the sandbox
options. The core pastes the attributed brief once Codex's composer waits for
input. The driver reads every turn from Codex's own files in its home folder
(`$CODEX_HOME`, or `~/.codex`):

- The rollout, `sessions/**/rollout-*.jsonl`, one JSON event per line. The
  driver finds the run's rollout by its working folder and the first line of
  its first message, which names the run, and skips rollouts that another
  run has claimed. `task_started` begins a turn; `task_complete`, with the
  final reply in `last_agent_message`, or `turn_aborted` ends it. `error`
  events and non-null `task_complete.error` fields report provider or
  sign-in failures. `user_message` events hold the messages typed or
  pasted into the session.
- The queue database, `queue_1.sqlite`, which holds messages waiting for a
  running turn. It is read from a private copy in memory, built under shared
  locks from the database and its write-ahead log, so reading it never
  creates or changes a file.

One state of a turn is read from the pane and not from a file. Codex retries
a request the provider refused or dropped inside the open turn. It shows the
retry on its status line, `Reconnecting... <n>/<m> (<time> • esc to
interrupt)`, with the provider's message on the line under it, and writes
neither to the rollout. `provider_wait` reads that line.

The driver's own state is `codex.json` in the run's folder: the rollout, the
session id, each message Ember pasted, in order, and, for each turn after
the first, the rollout's counts of ended turns and errors when its message
was sent.
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import time
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from ember import paths, tmux
from ember.engines import (
    DONE,
    FAILED,
    RUNNING,
    Driver,
    DriverError,
    IdleState,
    LaunchSpec,
    ProviderWait,
    TurnOutcome,
    models,
)
from ember.engines.diagnostics import output_text
from ember.runs import record
from ember.runs.core import BRIEF_LINE
from ember.runs.record import Run

STATE_NAME = "codex.json"
"""The driver's state file in the run's folder."""

CLAIM_LOCK_NAME = ".codex-rollouts.lock"
"""The lock, in the folder of the run folders, under which a run claims its
rollout."""

QUEUE_NAME = "queue_1.sqlite"
"""Codex's queue database in its home folder."""

READY_HINT = "? for shortcuts"
"""The footer Codex shows under its empty composer once it waits for input."""

MAX_QUEUE_BYTES = 64 * 1024 * 1024
"""The largest queue database the driver copies into memory."""

QUEUE_TRIES = 3
"""Reads of the queue database before its state is unknown."""

GIT_TIMEOUT = 30
"""Seconds the git command that finds the repository may take."""

DISCOVERY_SLACK = 5.0
"""Seconds before the launch from which a rollout's change time counts."""

ENDED = ("task_complete", "turn_aborted")
"""The rollout events that end a turn."""

COMPOSER_MARK = "›"
"""The first character of the composer's line in Codex's pane."""

_EFFORT_WORD = re.compile(r"[a-z][a-z0-9]*")

_WAIT_LINE = re.compile(
    r"^[^\w\s]\s+(?P<text>Reconnecting\.\.\.[^()]*?)\s*"
    r"\([^()]*esc to interrupt\)\s*$"
)
"""Codex's status line while it retries a request inside an open turn: one
mark in the first column, then the text. Text of a reply or of a tool's
output is indented and does not match."""

_WAIT_COUNT = re.compile(r"\s+\d+/\d+$")
"""The retry count at the end of the status line's text."""

_EXIT_ERROR = re.compile(r"^Error: \S")
"""The line Codex prints when it ends with an error."""

_EXIT_OUTAGE = re.compile(
    r"\baccount/[\w/]+ failed|request timed out"
    r"|failed to lookup address information|connection reset by peer"
    r"|error sending request|failed to connect to responses",
    re.IGNORECASE,
)
"""Texts of Codex's exit error when it could not reach its provider: a
failed request to the account service, a timed-out request, a failed name
lookup, a reset connection, a request that could not be sent, or a failed
connection to the Responses endpoint. A timeout or a failed connection with
other wording, such as one to a local server, is not among them."""

_SIGNED_OUT = re.compile(
    r"\b401\b|unauthori[sz]ed|(?:log|sign)\s?(?:in|out) again"
    r"|not (?:logged|signed) in|access token could not be refreshed",
    re.IGNORECASE,
)


def codex_home(env: Mapping[str, str]) -> Path:
    """Return Codex's home folder.

    Args:
        env: The environment.

    Returns:
        `$CODEX_HOME`, or `.codex` in the home folder.
    """
    value = env.get("CODEX_HOME", "")
    return Path(value) if value else paths.home_dir(env) / ".codex"


def events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield the payloads of a rollout's `event_msg` lines.

    Reading stops at the first line that is not complete JSON, such as the
    line Codex is still writing.

    Args:
        path: The rollout.

    Yields:
        Each event's payload, in order.
    """
    try:
        with Path(path).open(encoding="utf-8") as source:
            for line in source:
                try:
                    value = json.loads(line)
                except ValueError:
                    return
                if isinstance(value, dict) and value.get("type") == "event_msg":
                    payload = value.get("payload")
                    if isinstance(payload, dict):
                        yield payload
    except OSError, UnicodeError:
        return


@dataclass
class Turns:
    """What a rollout records about its turns."""

    started: int = 0
    """The number of turns started."""

    ended: list[tuple[str, str | None]] = field(default_factory=list)
    """Each ended turn's event and final reply, in order."""

    errors: list[str] = field(default_factory=list)
    """Each `error` event's message, in order."""

    errors_at_end: list[int] = field(default_factory=list)
    """For each ended turn, the number of `error` events before its end event."""

    completion_errors: list[str | None] = field(default_factory=list)
    """For each ended turn, its completion error's message, or `None`."""


def read_turns(path: str | Path) -> Turns:
    """Summarize a rollout's turns.

    Args:
        path: The rollout.

    Returns:
        The started and ended turns and the errors.
    """
    turns = Turns()
    for event in events(path):
        kind = event.get("type")
        if kind == "task_started":
            turns.started += 1
        elif kind in ENDED:
            reply = event.get("last_agent_message") if kind == "task_complete" else None
            turns.ended.append((str(kind), reply if isinstance(reply, str) else None))
            turns.errors_at_end.append(len(turns.errors))
            error = event.get("error") if kind == "task_complete" else None
            message = error.get("message") if isinstance(error, dict) else None
            turns.completion_errors.append(
                (message if isinstance(message, str) and message else "error")
                if error is not None
                else None
            )
        elif kind == "error":
            message = event.get("message")
            turns.errors.append(
                message if isinstance(message, str) and message else "error"
            )
    return turns


def user_messages(path: str | Path) -> list[str] | None:
    """Return the messages typed or pasted into a session, from its rollout.

    Reading stops at the first line that is not complete JSON, such as the
    line Codex is still writing.

    Args:
        path: The rollout.

    Returns:
        The text of each `user_message` event, in order; `None` when the
        rollout cannot be read or an event has no text.
    """
    messages = []
    try:
        with Path(path).open(encoding="utf-8") as source:
            for line in source:
                try:
                    value = json.loads(line)
                except ValueError:
                    break
                if not isinstance(value, dict):
                    continue
                payload = value.get("payload")
                if (
                    value.get("type") != "event_msg"
                    or not isinstance(payload, dict)
                    or payload.get("type") != "user_message"
                ):
                    continue
                message = payload.get("message")
                if not isinstance(message, str):
                    return None
                messages.append(message)
    except OSError, UnicodeError:
        return None
    return messages


def same_message(left: str, right: str) -> bool:
    """Return whether two message texts are the same message.

    Line endings, white space at the end of each line, and blank lines at
    the start and end do not count.

    Args:
        left: One text.
        right: The other.

    Returns:
        `True` when they match.
    """

    def normal(text: str) -> list[str]:
        lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return lines

    return normal(left) == normal(right)


def provider_kind(message: str) -> str:
    """Classify Codex's provider refusal text; unmatched errors are outages."""
    message = message.casefold().replace("’", "'")
    if "selected model is at capacity" in message:
        return "capacity"
    if "you've hit your usage limit" in message:
        return "usage_limit"
    return "outage"


def _wait_line(lines: list[str]) -> tuple[int, str, list[str]] | None:
    """Find Codex's retry status line in a pane.

    The status line sits directly above the live composer, the last line
    of the pane that starts with `COMPOSER_MARK`. Only the status line's
    detail lines and blank lines lie between the two. The same text higher
    in the pane, in a reply, a tool's output or above an earlier message,
    is not taken for it.

    Args:
        lines: The pane's lines.

    Returns:
        The line's index, its text without the timer, and its detail lines;
        `None` when the pane shows no composer or no retry above it.
    """
    composer = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if lines[index].startswith(COMPOSER_MARK)
        ),
        None,
    )
    if composer is None:
        return None
    index = composer - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    details: list[str] = []
    while index >= 0 and lines[index][:1].isspace() and lines[index].strip():
        details.insert(0, lines[index].strip().lstrip("└").strip())
        index -= 1
    if index < 0:
        return None
    match = _WAIT_LINE.match(lines[index])
    if match is None:
        return None
    return index, match["text"], details


def screen_wait(screen: str) -> ProviderWait | None:
    """Return the provider wait that Codex's pane shows.

    Args:
        screen: The pane's text.

    Returns:
        The wait, classified from the message under the status line; a
        status line without a message is an `outage`. `None` when the pane
        shows no retry.
    """
    found = _wait_line(screen.splitlines())
    if found is None:
        return None
    _, text, details = found
    # Without a message the count would make each retry a new wait.
    message = " ".join(details) or _WAIT_COUNT.sub("", text)
    return ProviderWait(provider_kind(message), message)


def screen_without_wait_timer(screen: str) -> str:
    """Return Codex's pane with the timer of its retry status line removed.

    Args:
        screen: The pane's text.

    Returns:
        The text, unchanged when the pane shows no retry.
    """
    lines = screen.splitlines()
    found = _wait_line(lines)
    if found is None:
        return screen
    index, text, _ = found
    lines[index] = text
    return "\n".join(lines)


def error_outcome(message: str) -> TurnOutcome:
    """Return the failed outcome of a turn that Codex reported an error for.

    Args:
        message: The error's message.

    Returns:
        A sign-in failure when the message says so; otherwise a provider
        error.
    """
    signed_out = _SIGNED_OUT.search(message) is not None
    return TurnOutcome(
        FAILED,
        reason=f"Codex reported an error: {message}",
        provider_error=not signed_out,
        signed_out=signed_out,
        provider_kind=None if signed_out else provider_kind(message),
    )


# The queue database, read from a private copy in memory


def wal_checksum(
    data: bytes, endian: str, checksum: tuple[int, int] = (0, 0)
) -> tuple[int, int]:
    """Return SQLite's write-ahead log checksum of data.

    Args:
        data: Bytes, a multiple of 8 long.
        endian: `<`, `>` or `=` for the byte order of the 32-bit words.
        checksum: The checksum the data continues.

    Returns:
        The two 32-bit checksum words.
    """
    first, second = checksum
    for left, right in struct.iter_unpack(endian + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


@dataclass(frozen=True)
class WalBoundary:
    """The last committed frame of a write-ahead log, as its index publishes it."""

    frames: int
    pages: int
    page_size: int
    big_endian: int
    salt: bytes
    checksum: tuple[int, int]


def wal_boundary(header: bytes) -> WalBoundary:
    """Check the two copies of a write-ahead log index header and read them.

    The layout is https://www.sqlite.org/walformat.html#the_wal_index_header.
    A log can hold frames of a transaction that failed and was never
    published; only frames up to the published boundary count.

    Args:
        header: The first 136 bytes of the `-shm` file.

    Returns:
        The published boundary.

    Raises:
        ValueError: The header is missing, inconsistent or not supported.
    """
    if len(header) < 136 or header[:48] != header[48:96]:
        raise ValueError("missing or inconsistent WAL-index header")
    version, unused = struct.unpack_from("=II", header)
    if version != 3007000 or unused or header[12] != 1 or header[13] not in (0, 1):
        raise ValueError("unsupported WAL-index header")
    if wal_checksum(header[:40], "=") != struct.unpack_from("=II", header, 40):
        raise ValueError("invalid WAL-index checksum")
    page_size = struct.unpack_from("=H", header, 14)[0]
    page_size = 65536 if page_size == 1 else page_size
    frames, pages = struct.unpack_from("=II", header, 16)
    backfill = struct.unpack_from("=I", header, 96)[0]
    attempted = struct.unpack_from("=I", header, 128)[0]
    if max(backfill, attempted) > frames:
        raise ValueError("invalid WAL checkpoint boundary")
    return WalBoundary(
        frames=frames,
        pages=pages,
        page_size=page_size,
        big_endian=header[13],
        salt=bytes(header[32:40]),
        checksum=struct.unpack_from("=II", header, 24),
    )


def apply_wal(image: bytes, wal: bytes, boundary: WalBoundary | None) -> bytearray:
    """Build a private database image with the published log frames applied.

    The format is https://www.sqlite.org/fileformat.html#walformat.

    Args:
        image: The main database file.
        wal: The log, up to the published boundary.
        boundary: The published boundary; `None` for a database not in
            write-ahead log mode.

    Returns:
        The image, in rollback journal mode.

    Raises:
        ValueError: The data is not valid or not supported.
    """
    if len(image) < 100 or image[:16] != b"SQLite format 3\x00":
        raise ValueError("invalid queue header")
    page_size = int.from_bytes(image[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if (
        not 512 <= page_size <= 65536
        or page_size & (page_size - 1)
        or len(image) % page_size
    ):
        raise ValueError("invalid queue page size")
    pages = len(image) // page_size
    committed: dict[int, bytes] = {}
    pending: dict[int, bytes] = {}
    if image[18:20] == b"\x02\x02" and boundary is None:
        raise ValueError("WAL published boundary unavailable")
    if boundary is not None and boundary.frames:
        if boundary.page_size != page_size:
            raise ValueError("WAL-index page size mismatch")
        if len(wal) < 32:
            raise ValueError("incomplete WAL header")
        magic, version, size = struct.unpack_from(">III", wal)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
            raise ValueError("unsupported WAL header")
        if size != page_size:
            raise ValueError("unsupported WAL header")
        if (magic & 1) != boundary.big_endian or wal[16:24] != boundary.salt:
            raise ValueError("WAL-index does not match WAL header")
        endian = "<" if magic == 0x377F0682 else ">"
        checksum = wal_checksum(wal[:24], endian)
        if checksum != struct.unpack_from(">II", wal, 24):
            raise ValueError("invalid WAL header checksum")
        frame_size = 24 + page_size
        end = 32 + boundary.frames * frame_size
        if len(wal) < end:
            raise ValueError("published WAL frames unavailable")
        for offset in range(32, end, frame_size):
            header = wal[offset : offset + 24]
            page, commit_size = struct.unpack_from(">II", header)
            if not page or header[8:16] != wal[16:24]:
                raise ValueError("invalid WAL frame")
            content = wal[offset + 24 : offset + frame_size]
            checksum = wal_checksum(header[:8], endian, checksum)
            checksum = wal_checksum(content, endian, checksum)
            if checksum != struct.unpack_from(">II", header, 16):
                raise ValueError("invalid WAL frame checksum")
            pending[page] = content
            if commit_size:
                if commit_size * page_size > MAX_QUEUE_BYTES:
                    raise ValueError("queue snapshot too large")
                committed.update(pending)
                committed = {
                    number: data
                    for number, data in committed.items()
                    if number <= commit_size
                }
                pending.clear()
                pages = commit_size
        if pending or pages != boundary.pages or checksum != boundary.checksum:
            raise ValueError("WAL published commit does not match WAL-index")
    result = bytearray(image[: pages * page_size])
    for page in range(len(result) // page_size + 1, pages + 1):
        if page not in committed:
            raise ValueError("missing queue page")
        result.extend(committed[page])
    for page, content in committed.items():
        result[(page - 1) * page_size : page * page_size] = content
    # Only the private image leaves write-ahead log mode.
    result[18:20] = b"\x01\x01"
    return result


def _read_limited(source: IO[bytes], length: int = MAX_QUEUE_BYTES) -> bytes:
    data = source.read(length + 1)
    if len(data) > MAX_QUEUE_BYTES:
        raise ValueError("queue snapshot too large")
    return data[:length]


def queue_snapshot(path: Path) -> bytearray:
    """Copy a queue database into memory under shared locks.

    The locks on the main file keep out rollback writes, journal mode changes
    and the deletion of its side files; the locks on the `-shm` file keep out
    log writes and checkpoints
    (https://www.sqlite.org/walformat.html#locking_matrix). Missing side files
    or busy locks fail without creating a file.

    Args:
        path: The database.

    Returns:
        The database image with its published log applied.

    Raises:
        OSError: A file is missing or a lock is busy.
        ValueError: The files are not a consistent, supported database.
    """
    with path.open("rb") as source, contextlib.ExitStack() as sidecars:
        fcntl.lockf(source, fcntl.LOCK_SH | fcntl.LOCK_NB, 512, 0x40000000)
        header = source.read(20)
        wal = b""
        boundary: WalBoundary | None = None
        index_header = b""
        shm: IO[bytes] | None = None
        opened: list[tuple[Path, IO[bytes]]] = [(path, source)]
        if header[18:20] == b"\x02\x02":
            shm_path = Path(f"{path}-shm")
            shm = sidecars.enter_context(shm_path.open("rb"))
            fcntl.lockf(shm, fcntl.LOCK_SH | fcntl.LOCK_NB, 2, 120)
            opened.append((shm_path, shm))
            index_header = shm.read(136)
            boundary = wal_boundary(index_header)
            wal_path = Path(f"{path}-wal")
            wal_source = sidecars.enter_context(wal_path.open("rb"))
            opened.append((wal_path, wal_source))
            # Unpublished writes and stale frames of a reused log are left out.
            length = (
                32 + boundary.frames * (24 + boundary.page_size)
                if boundary.frames
                else 0
            )
            if length > MAX_QUEUE_BYTES:
                raise ValueError("queue snapshot too large")
            wal = wal_source.read(length)
        elif header[18:20] != b"\x01\x01" or Path(f"{path}-wal").exists():
            raise ValueError("unknown queue journal mode")
        if Path(f"{path}-journal").exists():
            raise ValueError("queue may need journal recovery")
        source.seek(0)
        image = _read_limited(source)
        if shm is not None:
            shm.seek(0)
            if shm.read(96) != index_header[:96]:
                raise ValueError("WAL boundary changed during snapshot")
        for name, stream in opened:
            current, original = name.stat(), os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise ValueError("queue files replaced during snapshot")
        return apply_wal(image, wal, boundary)


def queued_for(home: Path, thread: str) -> int:
    """Return how many messages wait in Codex's queue for a session.

    Args:
        home: Codex's home folder.
        thread: The session id.

    Returns:
        The number of queued messages.

    Raises:
        OSError: The database cannot be read.
        ValueError: The session id is empty, or the database is not valid.
        sqlite3.Error: The database has no queue.
    """
    if not thread:
        raise ValueError("missing session id")
    image = queue_snapshot(home / QUEUE_NAME)
    with contextlib.closing(sqlite3.connect(":memory:")) as database:
        database.deserialize(bytes(image))
        row = database.execute(
            "SELECT count(*) FROM queued_items WHERE thread_id = ?", (thread,)
        ).fetchone()
    return int(row[0])


# The driver


def _same_folder(left: object, right: str) -> bool:
    if not isinstance(left, str) or not left:
        return False
    return os.path.realpath(left) == os.path.realpath(right)


def _names_run(lines: list[str], run_id: str) -> bool:
    """Return whether a user message in rollout lines starts with the run's line.

    The line is the first line of the run's first message,
    `Brief from Claude, the lead, for Ember run <id>.`
    """
    header = BRIEF_LINE.format(id=run_id)
    for line in lines:
        try:
            payload = json.loads(line).get("payload", {})
        except ValueError, AttributeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "message" or payload.get("role") != "user":
            continue
        content = payload.get("content")
        for part in content if isinstance(content, list) else []:
            text = part.get("text") if isinstance(part, dict) else None
            if not isinstance(text, str):
                continue
            first = next((row.strip() for row in text.splitlines() if row.strip()), "")
            if first == header:
                return True
    return False


class CodexDriver(Driver):
    """Runs Codex's interactive session in a run's tmux session."""

    name = "codex"

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Keep the environment for the model list.

        Args:
            env: The run's environment; `None` reads `os.environ`.
        """
        self.env: Mapping[str, str] = os.environ if env is None else env

    # State

    def _state_path(self, run: Run) -> Path:
        return run.folder / STATE_NAME

    def _load_state(self, run: Run) -> dict[str, Any]:
        path = self._state_path(run)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "launched": None,
                "rollout": "",
                "thread": "",
                "turns": {},
                "sent": [],
            }
        except (OSError, ValueError) as error:
            raise DriverError(f"cannot read {path}: {error}") from error
        if not isinstance(value, dict) or not isinstance(value.get("turns"), dict):
            raise DriverError(f"{path} is not a Codex driver state")
        return value

    def _save_state(self, run: Run, state: Mapping[str, Any]) -> None:
        path = self._state_path(run)
        try:
            record.write_private(path, json.dumps(state, indent=2) + "\n")
        except OSError as error:
            raise DriverError(f"cannot write {path}: {error}") from error

    def _claimed(self, run: Run) -> set[str]:
        """Return the rollouts other runs have claimed, as resolved paths."""
        claimed = set()
        try:
            folders = [
                entry
                for entry in run.folder.parent.iterdir()
                if entry.is_dir() and entry != run.folder
            ]
        except OSError:
            return claimed
        for folder in folders:
            try:
                value = json.loads((folder / STATE_NAME).read_text(encoding="utf-8"))
            except OSError, UnicodeError, ValueError:
                continue
            saved = value.get("rollout") if isinstance(value, dict) else None
            if isinstance(saved, str) and saved:
                claimed.add(os.path.realpath(saved))
        return claimed

    @contextlib.contextmanager
    def _claim_lock(self, run: Run) -> Iterator[None]:
        """Hold the lock under which runs find and claim their rollouts."""
        path = run.folder.parent / CLAIM_LOCK_NAME
        try:
            fd = paths.open_private_file(path)
        except OSError as error:
            raise DriverError(f"cannot open {path}: {error}") from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _rollout(self, run: Run, state: dict[str, Any]) -> Path | None:
        """Return the run's rollout, finding and claiming it the first time.

        Finding a rollout and saving it in the state file happen under one
        lock, and a rollout another run's state file names is skipped, so
        two runs in the same checkout never take the same rollout.

        Raises:
            DriverError: The saved rollout is gone, or the lock cannot be
                taken.
        """
        saved = state.get("rollout")
        if isinstance(saved, str) and saved:
            path = Path(saved)
            if not path.is_file():
                raise DriverError(f"the Codex rollout {path} is missing")
            return path
        with self._claim_lock(run):
            return self._find_rollout(run, state)

    def _find_rollout(self, run: Run, state: dict[str, Any]) -> Path | None:
        launched = state.get("launched")
        since = (
            launched - DISCOVERY_SLACK if isinstance(launched, (int, float)) else 0.0
        )
        sessions = codex_home(run.env) / "sessions"
        try:
            found = sorted(sessions.rglob("rollout-*.jsonl"))
        except OSError as error:
            raise DriverError(f"cannot read {sessions}: {error}") from error
        claimed = self._claimed(run)
        for path in found:
            if os.path.realpath(path) in claimed:
                continue
            try:
                if path.stat().st_mtime < since:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
                first = json.loads(lines[0]).get("payload", {})
            except OSError, UnicodeError, ValueError, AttributeError, IndexError:
                continue
            if not isinstance(first, dict):
                continue
            if not _same_folder(first.get("cwd"), run.worktree):
                continue
            if not _names_run(lines[1:], run.id):
                continue
            thread = first.get("id")
            state["rollout"] = str(path)
            state["thread"] = thread if isinstance(thread, str) else ""
            self._save_state(run, state)
            return path
        return None

    def _baseline(self, run: Run, state: Mapping[str, Any]) -> tuple[int, int]:
        """Return the ended turns and errors the rollout held before this turn."""
        saved = state["turns"].get(str(run.turn))
        if saved is None:
            if run.turn <= 1:
                return 0, 0
            raise DriverError(f"no record of when turn {run.turn} was sent")
        ended, errors = saved.get("ended"), saved.get("errors")
        if type(ended) is not int or type(errors) is not int:
            raise DriverError(f"the record of turn {run.turn} is not valid")
        return ended, errors

    # Starting

    def _repository_root(self, run: Run) -> Path:
        git = shutil.which("git", path=run.env.get("PATH", os.defpath))
        if git is None:
            raise DriverError("git is not on PATH")
        try:
            result = subprocess.run(
                [git, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=run.worktree,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=dict(run.env),
                timeout=GIT_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DriverError(f"cannot run git in {run.worktree}: {error}") from error
        if result.returncode:
            raise DriverError(f"not a git repository: {run.worktree}")
        return Path(result.stdout.strip()).parent

    def trusted(self, folder: Path, env: Mapping[str, str]) -> bool:
        """Return whether Codex's configuration trusts a folder.

        Args:
            folder: The folder.
            env: The environment, for Codex's home folder.

        Returns:
            `True` when `projects."<folder>".trust_level` is `trusted` in
            Codex's `config.toml`.
        """
        path = codex_home(env) / "config.toml"
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
            entry = config.get("projects", {}).get(str(folder), {})
            return entry.get("trust_level") == "trusted"
        except OSError, UnicodeError, ValueError, AttributeError:
            return False

    def check_trust(
        self,
        root: Path,
        env: Mapping[str, str],
        *,
        worktree: Path | None = None,
    ) -> None:
        """Refuse a repository Codex does not trust.

        Args:
            root: The repository root.
            env: The environment, for Codex's home folder.
            worktree: An existing worktree whose own trust also permits launch.

        Raises:
            DriverError: Neither the repository nor the worktree is trusted.
        """
        if self.trusted(root, env) or (
            worktree is not None and self.trusted(worktree, env)
        ):
            return
        raise DriverError(
            f"Codex does not trust {root}; the owner trusts it by running "
            "codex there once and choosing to trust the folder"
        )

    def command(self, run: Run, first_message: str) -> LaunchSpec:
        """Return the Codex command, with the first message to paste.

        Args:
            run: The run, with status `starting`.
            first_message: The attributed brief.

        Returns:
            `codex -C <worktree> -m <model> -c check_for_update_on_startup=false
            [-c model_reasoning_effort=...] [tier option] -a never
            [--sandbox danger-full-access] --search`, in the worktree, with
            the first message to paste. The sandbox option is
            added only for a run whose sandbox is `none`. Standard adds
            `-c features.fast_mode=false` and Fast adds `-c service_tier="fast"`.

        Raises:
            DriverError: Codex is not on PATH, or Codex does not trust the
                repository.
        """
        binary = shutil.which("codex", path=run.env.get("PATH", os.defpath))
        if binary is None:
            raise DriverError("the codex CLI is not on PATH")
        root = self._repository_root(run)
        self.check_trust(root, run.env, worktree=Path(run.worktree))
        argv = [binary, "-C", run.worktree, "-m", run.model]
        # An update prompt would hold the session before it takes the brief.
        argv += ["-c", "check_for_update_on_startup=false"]
        if run.effort is not None:
            argv += ["-c", f"model_reasoning_effort={json.dumps(run.effort)}"]
        if run.speed == "standard":
            argv += ["-c", "features.fast_mode=false"]
        elif run.speed == "fast":
            argv += ["-c", 'service_tier="fast"']
        argv += ["-a", "never"]
        if run.sandbox == "none":
            argv += ["--sandbox", "danger-full-access"]
        argv.append("--search")
        self._save_state(
            run,
            {
                "launched": time.time(),
                "rollout": "",
                "thread": "",
                "turns": {},
                "sent": [],
            },
        )
        return LaunchSpec(
            argv=tuple(argv), cwd=Path(run.worktree), first_message=first_message
        )

    def ready(self, run: Run) -> bool:
        """Return whether Codex's composer waits for the first message.

        Codex writes the rollout only after the first message, so the pane is
        the signal.

        Args:
            run: The run.

        Returns:
            `True` once a line of the pane shows `READY_HINT`.
        """
        return any(READY_HINT in line for line in self.screen(run).splitlines())

    # Turns

    def _record_sent(self, state: dict[str, Any], text: str) -> None:
        sent = state.get("sent")
        state["sent"] = [*(sent if isinstance(sent, list) else []), text]

    def send(self, run: Run, text: str) -> None:
        """Record where the rollout stands and the message, then paste it.

        The first message is pasted before the rollout exists; its turn
        counts from an empty rollout.

        Args:
            run: The run, with `turn` set to the message's turn.
            text: The message.

        Raises:
            DriverError: The run's rollout cannot be found or read after the
                first turn.
        """
        state = self._load_state(run)
        rollout = self._rollout(run, state)
        if rollout is None:
            if run.turn > 1:
                raise DriverError(f"cannot find the Codex session of {run.id}")
            self._paste(run, state, text)
            return
        turns = read_turns(rollout)
        state["turns"][str(run.turn)] = {
            "ended": len(turns.ended),
            "errors": len(turns.errors),
        }
        self._paste(run, state, text)

    def _paste(self, run: Run, state: dict[str, Any], text: str) -> None:
        """Record the message as sent, paste it, and drop it if the paste fails."""
        self._record_sent(state, text)
        self._save_state(run, state)
        try:
            tmux.send_text(run.id, text, env=run.env)
        except BaseException:
            state["sent"] = state["sent"][:-1]
            self._save_state(run, state)
            raise

    def accept_input(self, run: Run) -> None:
        """Record every message now in the rollout as one Ember accepts.

        Args:
            run: The run.

        Raises:
            DriverError: The rollout cannot be found or read.
        """
        state = self._load_state(run)
        rollout = self._rollout(run, state)
        if rollout is None:
            raise DriverError(f"cannot find the Codex session of {run.id}")
        messages = user_messages(rollout)
        if messages is None:
            raise DriverError(f"cannot read the Codex rollout {rollout}")
        state["sent"] = list(messages)
        self._save_state(run, state)

    def foreign_input(self, run: Run) -> str | None:
        """Compare the rollout's messages with the messages Ember pasted.

        The rollout's `user_message` events are matched in order against the
        messages recorded by `send`; every event that does not match the next
        recorded message is a message Ember did not send.

        Args:
            run: The run.

        Returns:
            `None` when every message in the rollout was pasted by Ember;
            otherwise how many were not, or why the rollout could not be
            checked.
        """
        unchecked = "the session history could not be checked"
        try:
            state = self._load_state(run)
            rollout = self._rollout(run, state)
        except DriverError as error:
            return f"{unchecked}: {error}"
        if rollout is None:
            return f"{unchecked}: no Codex session of {run.id} was found"
        sent = state.get("sent")
        if not isinstance(sent, list) or not all(
            isinstance(item, str) for item in sent
        ):
            return f"{unchecked}: {self._state_path(run)} has no valid sent messages"
        messages = user_messages(rollout)
        if messages is None:
            return f"{unchecked}: cannot read the Codex rollout {rollout}"
        index, foreign = 0, 0
        for message in messages:
            if index < len(sent) and same_message(message, sent[index]):
                index += 1
            else:
                foreign += 1
        if foreign:
            return f"{foreign} message(s) in the Codex session were not sent by Ember"
        return None

    def turn_output(self, run: Run) -> str:
        """Read tool result text after this attempt's saved rollout baseline."""
        state = self._load_state(run)
        rollout = self._rollout(run, state)
        if rollout is None:
            return ""
        baseline, _ = self._baseline(run, state)
        ended = 0
        output: list[str] = []
        try:
            with Path(rollout).open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        break
                    payload = value.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    kind = payload.get("type")
                    if ended == baseline and kind in (
                        "function_call_output",
                        "custom_tool_call_output",
                        "exec_command_end",
                        "error",
                    ):
                        output.append(output_text(payload))
                    if value.get("type") == "event_msg" and kind in ENDED:
                        ended += 1
                        if ended > baseline:
                            break
        except OSError, UnicodeError:
            return ""
        return "\n\n".join(output)

    def wait(self, run: Run) -> TurnOutcome:
        """Read the current turn from the rollout.

        Args:
            run: The run.

        Returns:
            `running` until a turn ends or Codex reports an error after the
            message was sent. A turn that Codex reported an error during is
            `failed`, as a sign-in failure when the error's message says so
            and otherwise as a provider error. Otherwise a completed turn is
            `done` with its final reply, and an interrupted turn `done` with
            no reply.

        Raises:
            DriverError: The driver state or the rollout cannot be read.
        """
        state = self._load_state(run)
        rollout = self._rollout(run, state)
        if rollout is None:
            return TurnOutcome(RUNNING)
        ended, errors = self._baseline(run, state)
        turns = read_turns(rollout)
        new_errors = turns.errors[errors:]
        if len(turns.ended) > ended:
            kind, reply = turns.ended[ended]
            completion_error = turns.completion_errors[ended]
            if completion_error is not None:
                return error_outcome(completion_error)
            during = turns.errors[errors : turns.errors_at_end[ended]]
            if during:
                return error_outcome(during[-1])
            if kind == "turn_aborted":
                return TurnOutcome(DONE)
            return TurnOutcome(DONE, reply=reply or None)
        if new_errors:
            return error_outcome(new_errors[-1])
        return TurnOutcome(RUNNING)

    def idle_state(self, run: Run) -> IdleState:
        """Return whether a turn runs or a message waits in Codex's queue.

        Args:
            run: The run.

        Returns:
            `busy` while a started turn has not ended or the queue holds a
            message for the session; `unknown` when the rollout or the queue
            cannot be read; otherwise `idle`.
        """
        try:
            state = self._load_state(run)
            rollout = self._rollout(run, state)
        except DriverError as error:
            return IdleState("unknown", str(error))
        if rollout is None:
            return IdleState("unknown", f"no Codex session of {run.id} was found")
        turns = read_turns(rollout)
        if turns.started > len(turns.ended):
            return IdleState("busy", "a turn is running")
        thread = state.get("thread")
        problem = ""
        for attempt in range(QUEUE_TRIES):
            try:
                queued = queued_for(
                    codex_home(run.env), thread if isinstance(thread, str) else ""
                )
            except (OSError, ValueError, sqlite3.Error) as error:
                problem = str(error)
                if attempt + 1 < QUEUE_TRIES:
                    time.sleep(0.05)
                continue
            if queued:
                return IdleState("busy", f"{queued} message(s) wait in Codex's queue")
            return IdleState("idle")
        return IdleState("unknown", f"cannot read Codex's queue: {problem}")

    def interrupt(self, run: Run) -> None:
        """Press Escape, which stops Codex's current turn.

        Args:
            run: The run.
        """
        tmux.send_keys(run.id, "Escape", env=run.env)

    def exit_words(self, screen: str) -> str | None:
        """Return Codex's `Error:` line when it is the pane's last line.

        Args:
            screen: The dead pane's text.

        Returns:
            The line, or `None`: without it the pane's last line is text of
            the session, not a word about the exit.
        """
        line = tmux.last_line(screen)
        # Codex prints its error in the first column; an indented line is
        # text of the session.
        if line is None or _EXIT_ERROR.match(line) is None:
            return None
        return line[: tmux.LAST_WORDS_LIMIT]

    def exit_outcome(self, words: str) -> TurnOutcome | None:
        """Classify Codex's exit error.

        Args:
            words: Codex's `Error:` line.

        Returns:
            An `outage` when the error says a request timed out or a
            connection or name lookup failed; otherwise `None`.
        """
        if (
            _EXIT_ERROR.match(words) is None
            or _EXIT_OUTAGE.search(words) is None
            or _SIGNED_OUT.search(words) is not None
        ):
            return None
        return TurnOutcome(
            FAILED, reason=words, provider_error=True, provider_kind="outage"
        )

    def provider_wait(self, screen: str) -> ProviderWait | None:
        """Read Codex's retry status line.

        Args:
            screen: The pane's text.

        Returns:
            The wait Codex shows while it retries inside an open turn, or
            `None`.
        """
        return screen_wait(screen)

    def stable_screen(self, screen: str) -> str:
        """Drop the timer of Codex's retry status line.

        Args:
            screen: The pane's text.

        Returns:
            The text the core compares for screen changes.
        """
        return screen_without_wait_timer(screen)

    # Efforts

    def _models(self) -> tuple[models.Model, ...]:
        return models.list_models("codex", self.env).models

    def valid_effort(self, effort: str) -> bool:
        """Return whether a model in Codex's model list takes an effort.

        Args:
            effort: The effort.

        Returns:
            Whether a listed model takes it; when the list gives no efforts,
            whether it is a lower-case word.
        """
        known = {level for model in self._models() for level in model.efforts}
        if known:
            return effort in known
        return _EFFORT_WORD.fullmatch(effort) is not None

    def default_effort(self, model: str) -> str | None:
        """Return the model's default effort from Codex's model list.

        Args:
            model: The model.

        Returns:
            Its `default_reasoning_level`, or `None` when the list does not
            give one.
        """
        for item in self._models():
            if item.id == model:
                return item.default_effort
        return None


def driver(env: Mapping[str, str] | None = None) -> CodexDriver:
    """Return the Codex driver.

    Args:
        env: The run's environment; `None` reads `os.environ`.

    Returns:
        A new driver.
    """
    return CodexDriver(env)
