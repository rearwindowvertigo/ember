"""OpenCode 2 workers with a private server and an attached interactive view.

State, authenticated HTTP, process shutdown and effort lookup use the OpenCode
1 driver's helpers. Messages and configuration use OpenCode 2's API formats.
The core wraps the whole shell command in `ember.sandbox.opencode_wrap`.
"""

import http.client
import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ember import engines, tmux
from ember.engines import DriverError, IdleState, LaunchSpec, TurnOutcome, opencode
from ember.engines.opencode import (
    AGENT,
    DOWN,
    GIT_DENIED,
    HOST,
    PROVIDER,
    UNKNOWN,
    UP,
    OpenCodeDriver,
    _RefusedError,
    external_directory,
    process_identity,
)
from ember.runs.record import Run

# Arguments are passed separately so paths and session ids cannot become shell code.
PANE_SCRIPT = r"""
binary=$1
port=$2
session=$3
url=http://127.0.0.1:$port
"$binary" serve --hostname 127.0.0.1 --port "$port" >/dev/null 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null; wait "$server" 2>/dev/null' EXIT
trap 'exit 1' HUP INT TERM
attempt=0
while kill -0 "$server" 2>/dev/null; do
    code=$(python3 - "$url/api/session/$session" <<'PY'
import base64
import os
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

token = base64.b64encode(
    ("opencode:" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()
).decode("ascii")
request = Request(sys.argv[1], headers={"Authorization": "Basic " + token})
opener = build_opener(ProxyHandler({}))
try:
    with opener.open(request, timeout=2) as response:
        print(response.status)
except HTTPError as error:
    print(error.code)
except OSError:
    print("000")
PY
    )
    case "$code" in
        200) exec "$binary" --server "$url" --session "$session" ;;
        401) echo "OpenCode refused the run's password" >&2; exit 1 ;;
    esac
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 600 ]; then
        echo "OpenCode session did not become ready" >&2
        exec "$binary" --server "$url"
    fi
    sleep 0.2
done
echo "OpenCode server stopped before its session was ready" >&2
exit 1
"""
"""Start a server and attach its view, selecting Ember's session when ready."""


def run_config(model: str, read_only: bool, read: Sequence[str] = ()) -> str:
    """Return OpenCode 2's provider configuration and ordered agent permissions.

    Args:
        model: The OpenRouter model id.
        read_only: The run edits nothing.
        read: The run's effective read paths.

    Returns:
        JSON with OpenRouter zero data retention and the Ember worker agent.
    """
    provider = json.loads(opencode.run_config(model, read_only, read))["provider"]
    permissions = [
        {"action": "question", "resource": "*", "effect": "deny"},
        *(
            {"action": "shell", "resource": command, "effect": "deny"}
            for command in GIT_DENIED
        ),
    ]
    if read_only:
        permissions.append({"action": "edit", "resource": "*", "effect": "deny"})
    external = external_directory(read_only, read)
    rules = external if isinstance(external, dict) else {"*": external}
    permissions.extend(
        {"action": "external_directory", "resource": resource, "effect": effect}
        for resource, effect in rules.items()
    )
    return json.dumps(
        {
            "provider": provider,
            "agents": {
                AGENT: {
                    "description": "A worker started by Ember.",
                    "mode": "primary",
                    "permissions": permissions,
                }
            },
        },
        separators=(",", ":"),
    )


class OpenCode2Driver(OpenCodeDriver):
    """Run OpenCode 2's private server and attach its view to Ember's session."""

    server_path = "/api/session"

    def command(self, run: Run, first_message: str) -> LaunchSpec:
        """Prepare the state and return the pane's server-and-view shell command."""
        spec = super().command(run, first_message)
        session = "ses_" + secrets.token_hex(13)
        state = self._save(run, session=session, created=False)
        variables = {
            "OPENCODE_CONFIG_CONTENT": run_config(
                run.model.removeprefix(f"{PROVIDER}/"),
                run.read_only,
                run.read_effective,
            ),
            "OPENCODE_SERVER_PASSWORD": spec.env["OPENCODE_SERVER_PASSWORD"],
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        }
        return replace(
            spec,
            argv=(
                "sh",
                "-c",
                PANE_SCRIPT,
                "ember-opencode2",
                spec.argv[0],
                str(state["port"]),
                session,
            ),  # fmt: skip
            env=variables,
        )

    def _server_identity(self, run: Run) -> dict[str, Any] | None:
        """Find the private server beneath the pane, across the sandbox processes.

        The pane process execs the view, so its identity cannot identify the
        background server. Inspect only the pane's descendants and match the
        private server's host and port before recording its process identity.
        """
        pane = tmux.pane_pid(run.id, env=run.env)
        pending = [] if pane is None else [pane]
        port = self._port(self._state(run))
        suffix = [b"serve", b"--hostname", HOST.encode(), b"--port", str(port).encode()]
        while pending:
            pid = pending.pop()
            try:
                folder = Path(f"/proc/{pid}")
                args = (
                    folder.joinpath("cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                )
                if args[-5:] == suffix:
                    return process_identity(pid)
                pending.extend(
                    int(child)
                    for child in folder.joinpath(f"task/{pid}/children")
                    .read_text()
                    .split()
                )
            except OSError, ValueError, IndexError:
                continue
        return None

    def ready(self, run: Run) -> bool:
        """Create Ember's chosen session once its private server answers.

        Return false while the server or its process is unavailable. Refused
        authentication and failed session creation raise `DriverError`.
        """
        try:
            code, _ = self._request(run, "GET", self.server_path)
        except _RefusedError, OSError, http.client.HTTPException:
            return False
        if code == 401:
            raise DriverError("the OpenCode server refused the run's password")
        if code != 200:
            return False
        state = self._state(run)
        if not self._has_identity(state):
            identity = self._server_identity(run)
            if identity is None:
                return False
            state = self._save(run, server=identity)
        if state.get("created"):
            return True
        session = self._session(state)
        model = {"id": run.model.removeprefix(f"{PROVIDER}/"), "providerID": PROVIDER}
        if run.effort is not None:
            model["variant"] = run.effort
        body = {"id": session, "agent": AGENT, "model": model, "title": run.subject}
        try:
            code, value = self._request(run, "POST", self.server_path, body)
        except (_RefusedError, OSError, http.client.HTTPException) as error:
            raise DriverError(
                f"OpenCode could not create a session: {error}"
            ) from error
        data = value.get("data") if isinstance(value, dict) else None
        if code != 200 or not isinstance(data, dict) or data.get("id") != session:
            raise DriverError(f"OpenCode could not create a session: HTTP {code}")
        self._save(run, created=True)
        return True

    def send(self, run: Run, text: str) -> None:
        """Record Ember's message id before posting the prompt for this turn."""
        state = self._state(run)
        session = self._session(state)
        if not state.get("created"):
            raise DriverError("the run has no OpenCode session")
        message_id = "msg_" + secrets.token_hex(13)
        turns = state.get("turns") if isinstance(state.get("turns"), dict) else {}
        sent = state.get("sent") if isinstance(state.get("sent"), list) else []
        self._save(
            run, turns={**turns, str(run.turn): message_id}, sent=[*sent, message_id]
        )
        path = f"/api/session/{quote(session, safe='')}/prompt"
        try:
            code, _ = self._request(run, "POST", path, {"id": message_id, "text": text})
        except (_RefusedError, OSError, http.client.HTTPException) as error:
            raise DriverError(f"OpenCode did not take the message: {error}") from error
        if code != 200:
            raise DriverError(f"OpenCode did not take the message: HTTP {code}")

    def _messages(self, run: Run, session: str) -> list[Any] | None:
        """Read all message pages, newest first, retrying an unreadable page."""
        path = f"/api/session/{quote(session, safe='')}/message"
        messages: list[Any] = []
        cursors: set[str] = set()
        while True:
            value = self._get(run, path, dict)
            if value is None:
                time.sleep(self.poll_seconds)
                value = self._get(run, path, dict)
            if value is None or not isinstance(value.get("data"), list):
                return None
            messages.extend(value["data"])
            pagination = value.get("cursor")
            cursor = pagination.get("next") if isinstance(pagination, dict) else None
            if cursor is None:
                return messages
            if not isinstance(cursor, str) or cursor in cursors:
                return None
            cursors.add(cursor)
            path = (
                f"/api/session/{quote(session, safe='')}/message"
                f"?cursor={quote(cursor, safe='')}"
            )

    def _active(self, run: Run) -> dict[str, Any] | None:
        """Return active sessions, retrying once when the status is unreadable."""
        value = self._get(run, "/api/session/active", dict)
        if value is None:
            time.sleep(self.poll_seconds)
            value = self._get(run, "/api/session/active", dict)
        data = value.get("data") if value is not None else None
        return data if isinstance(data, dict) else None

    def wait(self, run: Run) -> TurnOutcome:
        """Read this turn's idle outcome and newest assistant text after its prompt."""
        state = self._state(run)
        session = self._session(state)
        turns = state.get("turns")
        message_id = turns.get(str(run.turn)) if isinstance(turns, dict) else None
        if not isinstance(message_id, str):
            raise DriverError(f"no message was sent for turn {run.turn}")
        active = self._active(run)
        if active is not None and session in active:
            return TurnOutcome(engines.RUNNING)
        messages = None if active is None else self._messages(run, session)
        if messages is None:
            if self._server_state(run) == DOWN:
                return TurnOutcome(
                    engines.FAILED,
                    reason=f"the OpenCode server stopped before turn {run.turn} ended",
                )
            raise DriverError("cannot read the OpenCode session")
        current: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise DriverError("cannot read the OpenCode session's messages")
            if message.get("id") == message_id:
                break
            current.append(message)
        else:
            return TurnOutcome(engines.RUNNING)
        current.reverse()
        end = next(
            (i for i, message in enumerate(current) if message.get("type") == "idle"),
            None,
        )
        if end is None:
            return TurnOutcome(engines.RUNNING)
        idle = current[end]
        outcome = idle.get("outcome")
        if outcome == "interrupted":
            return TurnOutcome(
                engines.FAILED, reason=f"turn {run.turn} was interrupted"
            )
        if outcome == "failed":
            error = idle.get("error")
            error = error if isinstance(error, dict) else {}
            reason = str(error.get("message") or "OpenCode turn failed")
            signed_out = (
                error.get("type") == "provider.auth" or error.get("status") == 401
            )
            return TurnOutcome(
                engines.FAILED,
                reason=reason,
                signed_out=signed_out,
                provider_error=not signed_out
                and str(error.get("type", "")).startswith("provider."),
            )
        if outcome != "succeeded":
            raise DriverError("cannot read the OpenCode turn's outcome")
        assistant = next(
            (m for m in reversed(current[:end]) if m.get("type") == "assistant"), {}
        )
        parts = assistant.get("content")
        texts = [
            part["text"]
            for part in (parts if isinstance(parts, list) else [])
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        return TurnOutcome(engines.DONE, reply="\n".join(texts))

    def idle_state(self, run: Run) -> IdleState:
        """Return idle, busy or unknown from the private server and active sessions."""
        server = self._server_state(run)
        if server == DOWN:
            return IdleState(engines.IDLE, "the server is not running")
        if server == UNKNOWN:
            return IdleState(engines.UNKNOWN, "cannot tell whether the server runs")
        state = self._state(run)
        if not self._has_identity(state):
            return IdleState(engines.UNKNOWN, "the server's process was not recorded")
        active = self._active(run)
        if active is None:
            return IdleState(engines.UNKNOWN, "cannot read the session status")
        if self._session(state) in active:
            return IdleState(engines.BUSY, "a turn is running")
        return IdleState(engines.IDLE)

    def _user_ids(self, run: Run) -> list[str]:
        """Read user message ids, failing when the history cannot be checked."""
        session = self._session(self._state(run))
        messages = self._messages(run, session)
        if messages is None:
            raise DriverError("cannot read the OpenCode session's messages")
        ids: list[str] = []
        for message in messages:
            if not isinstance(message, dict) or not isinstance(
                message.get("type"), str
            ):
                raise DriverError("a message in the session has no type")
            if message["type"] == "user":
                if not isinstance(message.get("id"), str):
                    raise DriverError("a user message in the session has no id")
                ids.append(message["id"])
        return ids

    def foreign_input(self, run: Run) -> str | None:
        """Report user messages whose ids Ember has neither sent nor accepted."""
        try:
            sent = self._state(run).get("sent")
            if not isinstance(sent, list):
                raise DriverError("the sent message ids were not recorded")
            foreign = sum(message_id not in sent for message_id in self._user_ids(run))
        except DriverError as error:
            return f"the session history could not be checked: {error}"
        if foreign:
            return (
                f"{foreign} message(s) in the OpenCode session were not sent by Ember"
            )
        return None

    def accept_input(self, run: Run) -> None:
        """Record the user messages currently in the session as accepted by Ember."""
        ids = self._user_ids(run)
        sent = self._state(run).get("sent", [])
        self._save(run, sent=list(dict.fromkeys([*sent, *ids])))

    def interrupt(self, run: Run) -> None:
        """Interrupt the session's current turn through the server API."""
        server = self._server_state(run)
        if server != UP:
            raise DriverError(
                "the OpenCode server is not running"
                if server == DOWN
                else "cannot tell whether the OpenCode server runs"
            )
        session = self._session(self._state(run))
        path = f"/api/session/{quote(session, safe='')}/interrupt"
        try:
            code, _ = self._request(run, "POST", path)
        except (_RefusedError, OSError, http.client.HTTPException) as error:
            raise DriverError(
                f"OpenCode did not interrupt the turn: {error}"
            ) from error
        if code != 200:
            raise DriverError(f"OpenCode did not interrupt the turn: HTTP {code}")
