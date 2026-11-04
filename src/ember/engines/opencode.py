"""The OpenCode driver.

The run's pane runs OpenCode's interactive view with its built-in server
listening on a free port of `127.0.0.1`, protected by a password. The core
wraps that one command in the run's sandbox. The driver talks to the server's
session API: once the server answers it creates the run's session and shows
it in the interactive view once the view has drawn its screen, sends each
message with `prompt_async`, reads the turn's end and reply from the
session's messages and the session status, and aborts a turn through the
API. A user message in the session whose id the driver did not record was
typed by someone else, such as the owner in a terminal window.

Every run uses OpenRouter as the provider, with zero data retention required
for every request, and the agent `ember-worker`, defined in the configuration
the driver passes in `OPENCODE_CONFIG_CONTENT`. A read-only run's agent may
not edit, write or patch files, or reach folders outside its checkout.

The driver keeps its own state in the run's folder: the server password in
`opencode.pass` and the port, session, server process identity and each
turn's message id in `opencode.json`. OpenCode keeps its sessions in its own
data folder.
"""

import base64
import contextlib
import http.client
import json
import os
import re
import secrets
import signal
import socket
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ember import engines, tmux
from ember.engines import (
    Driver,
    DriverError,
    IdleState,
    LaunchSpec,
    TurnOutcome,
    models,
    status,
)
from ember.runs import record
from ember.runs.record import Run

STATE_NAME = "opencode.json"
"""The driver's state file in the run's folder."""

PASSWORD_NAME = "opencode.pass"
"""The server password file in the run's folder."""

HOST = "127.0.0.1"
"""The address the server listens on."""

PROVIDER = "openrouter"
"""The provider every run uses."""

AGENT = "ember-worker"
"""The OpenCode agent every run uses."""

HTTP_TIMEOUT = 2.0
"""Seconds each request to the server may take."""

STOP_SECONDS = 10.0
"""Seconds `close` waits for the server to stop."""

POLL_SECONDS = 0.2
"""Seconds between reads while waiting."""

OUTPUT_TOKEN_MAX = "200000"
"""The output token limit set when the environment sets none."""

GIT_DENIED = (
    "git commit*",
    "git stash*",
    "git push*",
    "git merge*",
    "git rebase*",
    "git reset*",
    "git checkout*",
    "git switch*",
    "git branch*",
    "git cherry-pick*",
    "git tag*",
)
"""Shell commands the worker may not run; Ember makes the run's commits."""

_EFFORT = re.compile(r"[a-z][a-z0-9]*")

UP = "up"
DOWN = "down"
UNKNOWN = "unknown"

MATCHING = 0
STOPPED = 1
UNVERIFIED = 2


START_SCREEN = "█▀▀█ █▀▀█"
"""Part of the logo on OpenCode's start screen, which a shown session lacks."""

SHOW_SECONDS = 120
"""How long after a run starts Ember keeps the view on the run's session."""


class _RefusedError(Exception):
    """The server's port refused the connection."""


def external_directory(
    read_only: bool, read: Sequence[str] = ()
) -> str | dict[str, str]:
    """Return the `external_directory` permission of a run.

    OpenCode checks a path outside the checkout as `<folder>/*` and uses the
    last matching rule, so a read-only run denies everything first and then
    allows the folder of each read path: a folder itself, or a file's folder.
    The sandbox decides what is visible in that folder, as it does for every
    engine: the read paths alone inside the folders it empties, and the rest
    of the machine read-only.

    Args:
        read_only: The run edits nothing.
        read: The run's read paths, from `sandbox.read` and `--read`.

    Returns:
        "allow" for an editing run, "deny" for a read-only run without read
        paths, otherwise the rules by pattern.
    """
    if not read_only:
        return "allow"
    folders = [path if Path(path).is_dir() else str(Path(path).parent) for path in read]
    if not folders:
        return "deny"
    rules = {"*": "deny"}
    for folder in dict.fromkeys(folders):
        rules[f"{folder.rstrip('/')}/*"] = "allow"
    return rules


def run_config(model: str, read_only: bool, read: Sequence[str] = ()) -> str:
    """Return the OpenCode configuration of a run, as JSON.

    Args:
        model: The OpenRouter model id.
        read_only: The run edits nothing.
        read: The run's read paths, which a read-only run may read.

    Returns:
        The configuration: OpenRouter with zero data retention for every
        request, no title generation, and the `ember-worker` agent.
    """
    bash: dict[str, str] = {"*": "allow", **dict.fromkeys(GIT_DENIED, "deny")}
    permission: dict[str, Any] = {
        "edit": "deny" if read_only else "allow",
        "write": "deny" if read_only else "allow",
        "patch": "deny" if read_only else "allow",
        "bash": bash,
        "external_directory": external_directory(read_only, read),
        "question": "deny",
    }
    config = {
        "provider": {
            PROVIDER: {
                # Provider-wide routing also covers summary and compaction calls.
                "options": {"extraBody": {"provider": {"zdr": True}}},
                "models": {model: {"options": {"provider": {"zdr": True}}}},
            }
        },
        "agent": {
            "title": {"disable": True},
            AGENT: {
                "description": "A worker started by Ember.",
                "mode": "primary",
                "permission": permission,
            },
        },
    }
    return json.dumps(config, separators=(",", ":"))


def completed(info: Mapping[str, Any]) -> bool:
    """Return whether an assistant message has ended.

    Args:
        info: The message's `info`.

    Returns:
        `True` for an assistant message with an error, or with a completion
        time and a finish reason other than `tool-calls` or `unknown`.
    """
    if info.get("role") != "assistant":
        return False
    if info.get("error") is not None:
        return True
    time_info = info.get("time")
    finished = isinstance(time_info, dict) and time_info.get("completed") is not None
    return finished and info.get("finish") not in (None, "tool-calls", "unknown")


def boot_identity() -> str:
    """Return the machine's boot id.

    Returns:
        The id.

    Raises:
        OSError: The id cannot be read.
        ValueError: The id is empty.
    """
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not boot:
        raise ValueError("the boot id is empty")
    return boot


def process_identity(pid: int) -> dict[str, Any]:
    """Return what identifies a live process across process id reuse.

    Args:
        pid: The process id.

    Returns:
        `{"pid", "boot_id", "start_time"}`.

    Raises:
        ProcessLookupError: The process has ended.
        FileNotFoundError: No process has the id.
        OSError: The process cannot be read.
        ValueError: The id is not a process id, or `/proc` is not readable.
        IndexError: The process's `stat` file is not as expected.
    """
    if pid <= 1:
        raise ValueError(f"{pid} is not a server process id")
    boot = boot_identity()
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in ("Z", "X"):
        raise ProcessLookupError(pid)
    return {"pid": pid, "boot_id": boot, "start_time": fields[19]}


def choose_port(previous: int = 0) -> int:
    """Return a free port on `127.0.0.1`, other than `previous`.

    Args:
        previous: A port not to return.

    Returns:
        The port.

    Raises:
        DriverError: No port can be bound.
    """
    try:
        while True:
            with socket.socket() as probe:
                probe.bind((HOST, 0))
                port = probe.getsockname()[1]
            if port != previous:
                return port
    except OSError as error:
        raise DriverError(f"cannot choose a port for OpenCode: {error}") from error


def _made_by_opencode(message: Mapping[str, Any]) -> bool:
    """Return whether OpenCode made this user message when it compacted the session.

    OpenCode marks a compaction with a user message holding a `compaction`
    part, and after an automatic compaction adds a user message holding a
    synthetic text part.
    """
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        return False
    return all(
        isinstance(part, dict)
        and (
            part.get("type") == "compaction"
            or (part.get("type") == "text" and part.get("synthetic") is True)
        )
        for part in parts
    )


def _is_compaction_summary(message: Mapping[str, Any]) -> bool:
    """Return whether this is the assistant message that summarises a compaction."""
    info = message.get("info")
    return (
        isinstance(info, dict)
        and info.get("role") == "assistant"
        and (info.get("summary") is True or info.get("mode") == "compaction")
    )


def _content(message: Mapping[str, Any]) -> tuple[tuple[Any, Any], ...] | None:
    """Return the type and text of each part of a user message.

    Returns `None` for a message with a `compaction` part or no readable
    parts. When OpenCode compacts a session that overflowed, it copies the
    parts of the last such message into a new user message with new ids, so
    the copy has the same content as the message it replays.
    """
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    if not all(isinstance(part, dict) for part in parts):
        return None
    if any(part.get("type") == "compaction" for part in parts):
        return None
    return tuple((part.get("type"), part.get("text")) for part in parts)


class OpenCodeDriver(Driver):
    """Runs OpenCode's interactive view and its built-in server in a run's pane."""

    name = "opencode"

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        fetch: models.Fetch | None = None,
        stop_seconds: float = STOP_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        """Keep the run's environment.

        Args:
            env: The environment for `HOME`, XDG and `PATH`; `None` reads
                `os.environ`.
            fetch: Reads OpenRouter's model list; `None` uses
                `models.fetch_json`.
            stop_seconds: Seconds `close` waits for the server to stop.
            poll_seconds: Seconds between reads while waiting.
        """
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.fetch = fetch
        self.stop_seconds = stop_seconds
        self.poll_seconds = poll_seconds

    # State

    def _state(self, run: Run) -> dict[str, Any]:
        path = run.folder / STATE_NAME
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise DriverError(f"cannot read {path}: {error}") from error
        if not isinstance(value, dict):
            raise DriverError(f"{path} does not hold an object")
        return value

    def _save(self, run: Run, **changes: object) -> dict[str, Any]:
        state = {**self._state(run), **changes}
        try:
            record.write_private(
                run.folder / STATE_NAME, json.dumps(state, indent=2) + "\n"
            )
        except OSError as error:
            raise DriverError(
                f"cannot write {run.folder / STATE_NAME}: {error}"
            ) from error
        return state

    def _password(self, run: Run) -> str | None:
        try:
            password = (run.folder / PASSWORD_NAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise DriverError(
                f"cannot read the OpenCode server password: {error}"
            ) from error
        return password.strip() or None

    @staticmethod
    def _port(state: Mapping[str, Any]) -> int | None:
        port = state.get("port")
        return port if type(port) is int and 1 <= port <= 65535 else None

    @staticmethod
    def _session(state: Mapping[str, Any]) -> str:
        session = state.get("session")
        if not isinstance(session, str) or not session:
            raise DriverError("the run has no OpenCode session")
        return session

    # The server's API

    def _request(
        self, run: Run, method: str, path: str, body: object = None
    ) -> tuple[int, Any]:
        """Make one request to the run's server, with no proxy or redirect.

        Returns:
            The HTTP status and the decoded JSON body, or `None` when the body
            is empty or not JSON.

        Raises:
            _RefusedError: The port refused the connection.
            DriverError: The server has no port or password recorded.
            OSError: The connection failed otherwise.
            http.client.HTTPException: The reply is not valid HTTP.
        """
        port = self._port(self._state(run))
        password = self._password(run)
        if port is None or password is None:
            raise DriverError("no OpenCode server was started for the run")
        token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
        headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(HOST, port, timeout=HTTP_TIMEOUT)
        try:
            try:
                connection.request(method, path, body=data, headers=headers)
            except ConnectionRefusedError as error:
                raise _RefusedError(str(error)) from error
            with connection.getresponse() as response:
                raw = response.read()
                code = response.status
        finally:
            connection.close()
        try:
            value = json.loads(raw) if raw.strip() else None
        except ValueError:
            value = None
        return code, value

    def _get(self, run: Run, path: str, expected: type) -> Any:  # noqa: ANN401
        """Return a GET reply of the expected type, or `None` when unreadable."""
        try:
            code, value = self._request(run, "GET", path)
        except _RefusedError, OSError, http.client.HTTPException:
            return None
        if code != 200 or not isinstance(value, expected):
            return None
        return value

    def _messages(self, run: Run, session: str) -> list[Any] | None:
        """Return the session's messages, read a second time after a failure."""
        path = f"/session/{quote(session, safe='')}/message"
        messages = self._get(run, path, list)
        if messages is None:
            time.sleep(self.poll_seconds)
            messages = self._get(run, path, list)
        return messages

    def _show_session(self, run: Run, session: str) -> None:
        """Select the session again while the view shows its start screen.

        Inside the sandbox, OpenCode's interactive view shows the selected
        session and then returns to its start screen once it has loaded the
        checkout. For the first `SHOW_SECONDS` of a run, each check selects
        the session again while the start screen shows; later the view is
        left alone, so an owner who moves it elsewhere keeps their view.
        """
        if time.time() - run.started_at > SHOW_SECONDS:
            return
        try:
            screen = tmux.capture(run.id, env=run.env)
        except tmux.TmuxError:
            return
        if START_SCREEN in screen:
            self._select(run, session)

    def _view_drawn(self, run: Run) -> bool:
        """Return whether the interactive view has drawn anything in the pane."""
        try:
            return bool(tmux.capture(run.id, env=run.env).strip())
        except tmux.TmuxError:
            return False

    def _select(self, run: Run, session: str) -> None:
        """Show the session in the interactive view; a failure changes nothing."""
        with contextlib.suppress(_RefusedError, OSError, http.client.HTTPException):
            self._request(run, "POST", "/tui/select-session", {"sessionID": session})

    def _has_identity(self, state: Mapping[str, Any]) -> bool:
        identity = state.get("server")
        return isinstance(identity, dict) and bool(
            identity.get("boot_id") and identity.get("start_time")
        )

    def _server_process(self, run: Run, *, stop: bool = False) -> int:
        """Check the recorded server process, and send it SIGTERM if `stop`.

        Returns:
            `MATCHING` for the recorded process, `STOPPED` when it has ended
            or its id is reused, `UNVERIFIED` when it cannot be told.
        """
        state = self._state(run)
        if not self._has_identity(state):
            return UNVERIFIED
        identity = state["server"]
        handle = None
        try:
            if identity["boot_id"] != boot_identity():
                return STOPPED
            pid = int(identity["pid"])
            if stop:
                handle = os.pidfd_open(pid, 0)
            current = process_identity(pid)
            if any(
                str(identity.get(key)) != str(value) for key, value in current.items()
            ):
                return STOPPED
            if handle is not None:
                signal.pidfd_send_signal(handle, signal.SIGTERM)
            return MATCHING
        except ProcessLookupError, FileNotFoundError:
            return STOPPED
        except OSError, ValueError, KeyError, IndexError, TypeError:
            return UNVERIFIED
        finally:
            if handle is not None:
                os.close(handle)

    def _server_state(self, run: Run) -> str:
        """Return `up`, `down` or `unknown` for the run's server."""
        state = self._state(run)
        if self._password(run) is None or self._port(state) is None:
            # The password is written before the server starts.
            return DOWN
        try:
            self._request(run, "GET", "/session")
        except _RefusedError:
            if not self._has_identity(state) or self._server_process(run) == STOPPED:
                return DOWN
            return UNKNOWN
        except OSError, http.client.HTTPException:
            return UNKNOWN
        return UP

    # Driver

    def command(self, run: Run, first_message: str) -> LaunchSpec:
        """Return the interactive view with its server on a new port.

        Args:
            run: The run, with status `starting`.
            first_message: The attributed brief.

        Returns:
            The command, with the configuration, server password and output
            limit in its variables; the brief is sent once the server answers.

        Raises:
            DriverError: OpenCode is not installed, the environment already
                sets `OPENCODE_CONFIG_CONTENT`, or the run's files cannot be
                written.
        """
        binary = status.locate("opencode", self.env)
        if binary is None:
            raise DriverError("opencode is not installed")
        if self.env.get("OPENCODE_CONFIG_CONTENT"):
            raise DriverError(
                "OPENCODE_CONFIG_CONTENT is already set; Ember sets it for each run"
            )
        model = run.model.removeprefix(f"{PROVIDER}/")
        password = secrets.token_hex(16)
        try:
            record.write_private(run.folder / PASSWORD_NAME, password)
        except OSError as error:
            raise DriverError(
                f"cannot write the OpenCode server password: {error}"
            ) from error
        previous = self._port(self._state(run)) or 0
        port = choose_port(previous)
        self._save(run, port=port, session=None, server=None, turns={}, sent=[])
        argv = (
            binary,
            "--port", str(port),
            "--hostname", HOST,
            "--model", f"{PROVIDER}/{model}",
            "--agent", AGENT,
        )  # fmt: skip
        variables = {
            "OPENCODE_CONFIG_CONTENT": run_config(
                model, run.read_only, run.read_effective
            ),
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": self.env.get(
                "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX", OUTPUT_TOKEN_MAX
            ),
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        }
        return LaunchSpec(
            argv=argv,
            env=variables,
            cwd=Path(run.worktree),
            first_message=first_message,
        )

    def ready(self, run: Run) -> bool:
        """Return whether the server answers, creating the run's session.

        The first time the server answers, the driver records the identity
        of the pane's process, which runs the server, and creates the
        session. Once the interactive view has drawn its screen, so that it
        is connected to the server, the driver shows the session in it.

        Args:
            run: The run.

        Returns:
            `True` once the session exists and the view has drawn its screen.

        Raises:
            DriverError: The server refuses the password, or cannot create
                the session.
        """
        try:
            code, _ = self._request(run, "GET", "/session")
        except _RefusedError, OSError, http.client.HTTPException:
            return False
        if code == 401:
            raise DriverError("the OpenCode server refused the run's password")
        if code != 200:
            return False
        state = self._state(run)
        if not self._has_identity(state):
            pid = tmux.pane_pid(run.id, env=run.env)
            if pid is None:
                return False
            try:
                identity = process_identity(pid)
            except OSError, ValueError, IndexError:
                return False
            state = self._save(run, server=identity)
        session = state.get("session")
        if not isinstance(session, str) or not session:
            try:
                code, value = self._request(run, "POST", "/session", {})
            except (_RefusedError, OSError, http.client.HTTPException) as error:
                raise DriverError(
                    f"OpenCode could not create a session: {error}"
                ) from error
            session = value.get("id") if isinstance(value, dict) else None
            if code != 200 or not isinstance(session, str) or not session:
                raise DriverError(f"OpenCode could not create a session: HTTP {code}")
            self._save(run, session=session)
        if not self._view_drawn(run):
            return False
        self._select(run, session)
        return True

    def send(self, run: Run, text: str) -> None:
        """Send a message to the run's session as turn `run.turn`.

        The message id is recorded before the server takes the message.

        Args:
            run: The run.
            text: The message.

        Raises:
            DriverError: The run has no session, or the server did not take
                the message.
        """
        state = self._state(run)
        session = self._session(state)
        message_id = "msg" + secrets.token_hex(13)
        turns = state.get("turns") if isinstance(state.get("turns"), dict) else {}
        sent = state.get("sent") if isinstance(state.get("sent"), list) else []
        self._save(
            run,
            turns={**turns, str(run.turn): message_id},
            sent=[*sent, message_id],
        )
        body: dict[str, Any] = {
            "messageID": message_id,
            "model": {
                "providerID": PROVIDER,
                "modelID": run.model.removeprefix(f"{PROVIDER}/"),
            },
            "agent": AGENT,
            "parts": [{"type": "text", "text": text}],
        }
        if run.effort is not None:
            body["variant"] = run.effort
        self._select(run, session)
        path = f"/session/{quote(session, safe='')}/prompt_async"
        try:
            code, _ = self._request(run, "POST", path, body)
        except (_RefusedError, OSError, http.client.HTTPException) as error:
            raise DriverError(f"OpenCode did not take the message: {error}") from error
        if code != 204:
            raise DriverError(f"OpenCode did not take the message: HTTP {code}")

    def wait(self, run: Run) -> TurnOutcome:
        """Observe the current turn once, from the session status and messages.

        Args:
            run: The run.

        Returns:
            `running` while the session is busy or the turn has no ended
            reply; otherwise `done` with the text of the turn's last reply,
            or `failed` for an interrupted turn, a provider error, a sign-in
            failure or a server that stopped.

        Raises:
            DriverError: No message was sent for the turn, or the server
                cannot be read and may still be running.
        """
        state = self._state(run)
        session = self._session(state)
        turns = state.get("turns")
        message_id = turns.get(str(run.turn)) if isinstance(turns, dict) else None
        if not isinstance(message_id, str):
            raise DriverError(f"no message was sent for turn {run.turn}")
        self._show_session(run, session)
        statuses = self._get(run, "/session/status", dict)
        if statuses is None:
            time.sleep(self.poll_seconds)
            statuses = self._get(run, "/session/status", dict)
        messages = None if statuses is None else self._messages(run, session)
        if messages is None:
            if self._server_state(run) == DOWN:
                return TurnOutcome(
                    engines.FAILED,
                    reason=f"the OpenCode server stopped before turn {run.turn} ended",
                )
            raise DriverError("cannot read the OpenCode session")
        current = statuses.get(session)
        busy = isinstance(current, dict) and current.get("type") != "idle"
        own = [
            message
            for message in messages
            if isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("role") == "assistant"
            and message["info"].get("parentID") == message_id
        ]
        if busy or not any(completed(message["info"]) for message in own):
            return TurnOutcome(engines.RUNNING)
        return self._classify(run, own[-1])

    def _classify(self, run: Run, last: Mapping[str, Any]) -> TurnOutcome:
        """Return the outcome of an ended turn from its last reply."""
        error = last["info"].get("error")
        if error is not None:
            error = error if isinstance(error, dict) else {}
            name = str(error.get("name") or "ProviderError")
            if name == "MessageAbortedError":
                return TurnOutcome(
                    engines.FAILED, reason=f"turn {run.turn} was interrupted"
                )
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            detail = str(data.get("message") or error.get("message") or "")
            reason = f"{name}: {detail}" if detail else name
            if name == "ProviderAuthError" or data.get("statusCode") == 401:
                return TurnOutcome(engines.FAILED, reason=reason, signed_out=True)
            if re.search(r"402|credit|insufficient", reason.lower()):
                reason += " (OpenRouter credit is exhausted)"
            return TurnOutcome(engines.FAILED, reason=reason, provider_error=True)
        parts = last.get("parts")
        texts = [
            part["text"]
            for part in (parts if isinstance(parts, list) else [])
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        return TurnOutcome(engines.DONE, reply="\n".join(texts))

    def idle_state(self, run: Run) -> IdleState:
        """Return whether the session can take a message, from its status.

        Args:
            run: The run.

        Returns:
            `idle` when the server is down or the session is idle; `busy`
            while a turn runs; `unknown` when the server or its status cannot
            be read, or the server's process was not recorded.
        """
        server = self._server_state(run)
        if server == UNKNOWN:
            return IdleState(engines.UNKNOWN, "cannot tell whether the server runs")
        if server == DOWN:
            return IdleState(engines.IDLE, "the server is not running")
        state = self._state(run)
        if not self._has_identity(state):
            return IdleState(engines.UNKNOWN, "the server's process was not recorded")
        statuses = self._get(run, "/session/status", dict)
        session = state.get("session")
        current = statuses.get(session) if statuses is not None else None
        if statuses is None or (
            session in statuses
            and (
                not isinstance(current, dict)
                or not isinstance(current.get("type"), str)
            )
        ):
            return IdleState(engines.UNKNOWN, "cannot read the session status")
        if isinstance(current, dict) and current["type"] != "idle":
            return IdleState(engines.BUSY, "a turn is running")
        return IdleState(engines.IDLE)

    def foreign_input(self, run: Run) -> str | None:
        """Look for user messages in the session that the driver did not send.

        Args:
            run: The run.

        Returns:
            `None` when every user message in the session has an id the
            driver recorded when it sent it or was made by OpenCode when it
            compacted the session; otherwise how many do not, or why the
            session could not be read. A user message that directly follows
            a compaction summary and has the content of the last earlier
            user message without a compaction part is OpenCode's replay of
            that message, and counts only when that message counts.
        """
        unchecked = "the session history could not be checked"
        try:
            state = self._state(run)
            session = self._session(state)
        except DriverError as error:
            return f"{unchecked}: {error}"
        sent = state.get("sent")
        if not isinstance(sent, list):
            return f"{unchecked}: the sent message ids were not recorded"
        messages = self._messages(run, session)
        if messages is None:
            return f"{unchecked}: cannot read the OpenCode session's messages"
        foreign = 0
        previous: Mapping[str, Any] | None = None
        source: tuple[tuple[tuple[Any, Any], ...] | None, bool] = (None, False)
        for message in messages:
            info = message.get("info") if isinstance(message, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get("role"), str):
                return f"{unchecked}: a message in the session has no role"
            if info["role"] == "user":
                content = _content(message)
                if (
                    previous is not None
                    and _is_compaction_summary(previous)
                    and (content is not None and content == source[0])
                ):
                    counted = source[1]
                else:
                    counted = info.get("id") not in sent and not _made_by_opencode(
                        message
                    )
                foreign += counted
                if content is not None:
                    source = (content, counted)
            previous = message
        if foreign:
            return (
                f"{foreign} message(s) in the OpenCode session were not sent by Ember"
            )
        return None

    def accept_input(self, run: Run) -> None:
        """Record every user message now in the session as one Ember accepts.

        Args:
            run: The run.

        Raises:
            DriverError: The run has no session, or its messages cannot be
                read.
        """
        state = self._state(run)
        messages = self._messages(run, self._session(state))
        if messages is None:
            raise DriverError("cannot read the OpenCode session's messages")
        sent = state.get("sent") if isinstance(state.get("sent"), list) else []
        ids = [
            info["id"]
            for message in messages
            if isinstance(message, dict)
            and isinstance(info := message.get("info"), dict)
            and info.get("role") == "user"
            and isinstance(info.get("id"), str)
        ]
        self._save(run, sent=[*sent, *(i for i in ids if i not in sent)])

    def interrupt(self, run: Run) -> None:
        """Abort the session's current turn through the server.

        Args:
            run: The run.

        Raises:
            DriverError: The server is down, cannot be read, or did not abort.
        """
        server = self._server_state(run)
        if server != UP:
            raise DriverError(
                "the OpenCode server is not running"
                if server == DOWN
                else "cannot tell whether the OpenCode server runs"
            )
        session = self._session(self._state(run))
        path = f"/session/{quote(session, safe='')}/abort"
        try:
            code, _ = self._request(run, "POST", path)
        except (_RefusedError, OSError, http.client.HTTPException) as error:
            raise DriverError(f"OpenCode did not abort the turn: {error}") from error
        if code != 200:
            raise DriverError(f"OpenCode did not abort the turn: HTTP {code}")

    def close(self, run: Run) -> None:
        """Stop the server: send SIGTERM to its recorded process and wait.

        Nothing is done when no server was started or it has stopped.

        Args:
            run: The run.

        Raises:
            DriverError: The server's state or process cannot be verified, or
                it did not stop within `stop_seconds`.
        """
        server = self._server_state(run)
        if server == DOWN:
            return
        if server == UNKNOWN:
            raise DriverError("cannot tell whether the OpenCode server runs")
        if not self._has_identity(self._state(run)):
            port = self._port(self._state(run))
            raise DriverError(
                f"the OpenCode server on port {port} has no recorded process"
            )
        if self._server_process(run, stop=True) == UNVERIFIED:
            raise DriverError("cannot verify the OpenCode server's process")
        deadline = time.monotonic() + self.stop_seconds
        while self._server_state(run) != DOWN:
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"the OpenCode server did not stop within {self.stop_seconds:g} s"
                )
            time.sleep(self.poll_seconds)

    def valid_effort(self, effort: str) -> bool:
        """Return whether an effort is a lower-case OpenCode variant name.

        Args:
            effort: The effort, such as `high`.

        Returns:
            `True` for a lower-case word, digits allowed after the first
            letter.
        """
        return _EFFORT.fullmatch(effort) is not None

    def default_effort(self, model: str) -> str | None:
        """Return the model's default reasoning effort from OpenRouter's list.

        Args:
            model: The OpenRouter model id; a `:<variant>` id matches its
                base id too.

        Returns:
            The effort, or `None` when the list has none or cannot be read.
        """
        model = model.removeprefix(f"{PROVIDER}/")
        listed = models.list_models("opencode", self.env, fetch=self.fetch).models
        by_id = {entry.id: entry for entry in listed}
        entry = by_id.get(model) or by_id.get(model.split(":", 1)[0])
        return None if entry is None else entry.default_effort


def driver(env: Mapping[str, str] | None = None) -> Driver:
    """Return the OpenCode driver.

    Args:
        env: The run's environment; `None` reads `os.environ`.

    Returns:
        A new driver.
    """
    return OpenCodeDriver(env)
