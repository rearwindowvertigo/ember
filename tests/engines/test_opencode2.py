import contextlib
import dataclasses
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ember import engines, tmux
from ember.engines import DriverError, opencode, opencode2
from ember.engines.opencode2 import OpenCode2Driver
from ember.runs import record
from ember.runs.record import Run

# A private HTTP server, launched as a separate process so shutdown tests exercise
# the same process identity checks as the driver. All replies come from test files.
STUB_SERVER = r"""
import base64, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

folder = Path(os.environ["STUB_FOLDER"])
state_path = Path(os.environ["STUB_STATE"])

def held(name, default):
    path = folder / name
    return json.loads(path.read_text()) if path.exists() else default

if "serve" not in sys.argv:
    (folder / "view.json").write_text(json.dumps(sys.argv[1:]))
    sys.exit(0)

port = int(sys.argv[sys.argv.index("--port") + 1])
password = os.environ["OPENCODE_SERVER_PASSWORD"]
auth = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value=None):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self, method):
        if self.headers.get("Authorization") != auth:
            return self.reply(401, {})
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null")
        state = json.loads(state_path.read_text())
        with open(folder / "requests.jsonl", "a") as log:
            log.write(json.dumps({"method": method, "path": path, "body": body,
                                 "query": query, "state": state}) + "\n")
        if method == "GET":
            if path == "/api/session":
                return self.reply(held("health-code", 200), {"data": []})
            if path == "/api/session/active":
                return self.reply(held("active-code", 200),
                                  {"data": held("active.json", {})})
            if path.endswith("/message"):
                cursor = query.get("cursor", [None])[0]
                pages = held("pages.json", {})
                page = pages.get(cursor) if cursor else None
                if page is not None:
                    return self.reply(200, page)
                return self.reply(held("messages-code", 200), {
                    "data": held("messages.json", []),
                    "cursor": {"next": held("next.json", None), "previous": None},
                })
            if path == "/api/session/" + state["session"]:
                created = held("created.json", None)
                return self.reply(200 if created else 404, {"data": created})
        if method == "POST":
            if path == "/api/session":
                code = held("create-code", 200)
                if code == 200:
                    (folder / "created.json").write_text(json.dumps(body))
                return self.reply(code, {"data": {"id": body["id"]}})
            if path.endswith("/prompt"):
                return self.reply(held("prompt-code", 200), {"data": body})
            if path.endswith("/interrupt"):
                return self.reply(held("interrupt-code", 200), {"interrupted": True})
        return self.reply(404, {})

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    binary = tmp_path / "bin with 'quotes" / "opencode"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": str(binary.parent) + os.pathsep + os.defpath,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


@pytest.fixture
def run(env: dict[str, str], tmp_path: Path) -> Run:
    return Run(
        id="opencode-20260101-120000-abcdef",
        engine="opencode",
        model="openrouter/deepseek/deepseek-v4-pro",
        effort="high",
        read_only=False,
        repo=str(tmp_path / "repo"),
        worktree=str(tmp_path / "worktree"),
        branch="ember/opencode/test",
        base="a" * 40,
        sandbox="bubblewrap",
        network=False,
        read=(),
        read_effective=(),
        trailers=(),
        claim=None,
        status=record.STARTING,
        turn=0,
        turn_state=None,
        reply=None,
        expected_head=None,
        started_at=1.5,
        updated_at=1.5,
        ended_at=None,
        last_exit=None,
        last_reason=None,
        commits=(),
        subject="Do the thing",
        folder=record.make_run_dir("opencode-20260101-120000-abcdef", env),
        env=env,
    )


@pytest.fixture
def driver(env: dict[str, str]) -> OpenCode2Driver:
    return OpenCode2Driver(env, stop_seconds=2, poll_seconds=0.01)


class Server:
    def __init__(self, run: Run, folder: Path) -> None:
        self.run = run
        self.folder = folder
        self.process: subprocess.Popen[bytes] | None = None

    def state(self) -> dict[str, Any]:
        return json.loads((self.run.folder / opencode.STATE_NAME).read_text())

    def start(self, password: str | None = None, *, pane: bool = False) -> None:
        script = self.folder / "server.py"
        script.write_text(STUB_SERVER)
        variables = {
            **self.run.env,
            "OPENCODE_SERVER_PASSWORD": password
            or (self.run.folder / opencode.PASSWORD_NAME).read_text(),
            "STUB_FOLDER": str(self.folder),
            "STUB_STATE": str(self.run.folder / opencode.STATE_NAME),
        }
        state = self.state()
        args = [
            sys.executable, "-I", str(script),
            "serve", "--hostname", opencode.HOST, "--port", str(state["port"]),
        ]  # fmt: skip
        if pane:
            binary = Path(self.run.env["PATH"].split(os.pathsep)[0]) / "opencode"
            binary.write_text(f"#!{sys.executable}\n" + STUB_SERVER)
            # The pane has Python and sleep available, with no curl on PATH.
            (binary.parent / "python3").symlink_to(sys.executable)
            (binary.parent / "sleep").symlink_to("/bin/sleep")
            variables["PATH"] = str(binary.parent)
            args = [
                "/bin/sh", "-c", opencode2.PANE_SCRIPT, "ember-opencode2",
                str(binary), str(state["port"]), state["session"],
            ]  # fmt: skip
        self.process = subprocess.Popen(args, env=variables, start_new_session=True)
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(
                    (opencode.HOST, state["port"]), timeout=1
                ).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    def stop(self) -> None:
        if self.process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)

    def write(self, name: str, value: object) -> None:
        (self.folder / name).write_text(json.dumps(value))

    def requests(self, suffix: str = "") -> list[dict[str, Any]]:
        path = self.folder / "requests.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [r for line in lines if (r := json.loads(line))["path"].endswith(suffix)]


@pytest.fixture
def server(
    run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Server]:
    folder = tmp_path / "server"
    folder.mkdir()
    server = Server(run, folder)
    monkeypatch.setattr(
        tmux,
        "pane_pid",
        lambda *_args, **_kwargs: (
            None if server.process is None else server.process.pid
        ),
    )
    yield server
    server.stop()


def started(driver: OpenCode2Driver, run: Run, server: Server) -> Run:
    driver.command(run, "brief")
    server.start()
    assert driver.ready(run)
    run = dataclasses.replace(run, turn=1)
    driver.send(run, "brief")
    return run


def prompt(server: Server) -> dict[str, Any]:
    body = server.requests("/prompt")[-1]["body"]
    return {"type": "user", "id": body["id"], "text": body["text"]}


def idle(outcome: str = "succeeded", **fields: object) -> dict[str, Any]:
    return {"type": "idle", "id": "msg_idle", "outcome": outcome, **fields}


def assistant(*texts: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "id": "msg_assistant",
        "content": [{"type": "reasoning", "text": "hidden"}]
        + [{"type": "text", "text": text} for text in texts],
    }


def test_command(driver: OpenCode2Driver, run: Run, server: Server) -> None:
    spec = driver.command(run, "brief")
    state = server.state()
    assert re.fullmatch(r"ses_[0-9a-f]{26}", state["session"])
    assert spec.argv == (
        "sh", "-c", opencode2.PANE_SCRIPT, "ember-opencode2",
        str(Path(run.env["PATH"].split(os.pathsep)[0]) / "opencode"),
        str(state["port"]), state["session"],
    )  # fmt: skip
    assert spec.cwd == Path(run.worktree)
    assert spec.binary == spec.argv[4]
    assert spec.first_message == "brief"
    assert state["turns"] == {} and state["sent"] == []
    for name in (opencode.STATE_NAME, opencode.PASSWORD_NAME):
        assert stat.S_IMODE((run.folder / name).stat().st_mode) == 0o600
    assert (
        spec.env["OPENCODE_SERVER_PASSWORD"]
        == (run.folder / opencode.PASSWORD_NAME).read_text()
    )
    assert spec.env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert spec.env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"
    driver.command(run, "next")
    assert server.state()["port"] != state["port"]
    assert server.state()["session"] != state["session"]


def test_pane_waits_for_session_and_attaches(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    driver.command(run, "brief")
    server.start(pane=True)
    assert not (server.folder / "view.json").exists()
    assert driver.ready(run)
    assert server.process is not None
    assert server.state()["server"]["pid"] != server.process.pid
    assert server.process.wait(timeout=10) == 0
    assert json.loads((server.folder / "view.json").read_text()) == [
        "--server",
        f"http://127.0.0.1:{server.state()['port']}",
        "--session",
        server.state()["session"],
    ]
    driver.close(run)
    driver.close(run)


@pytest.mark.parametrize("http_code", ["000", "401", "404"])
def test_pane_handles_an_unavailable_session(
    driver: OpenCode2Driver, run: Run, tmp_path: Path, http_code: str
) -> None:
    spec = driver.command(run, "brief")
    folder = tmp_path / "script-bin"
    folder.mkdir()
    server_binary = folder / "opencode"
    server_pid = folder / "server.pid"
    view_args = folder / "view.args"
    server_binary.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = serve ]; then\n'
        '    echo $$ > "$SERVER_PID"\n'
        "    exec /bin/sleep 30\n"
        "fi\n"
        'printf "%s\\n" "$@" > "$VIEW_ARGS"\n'
    )
    server_binary.chmod(0o755)
    for name, text in {"python3": f"echo {http_code}", "sleep": ":"}.items():
        binary = folder / name
        binary.write_text(f"#!/bin/sh\n{text}\n")
        binary.chmod(0o755)
    process = subprocess.Popen(
        ["/bin/sh", *spec.argv[1:4], str(server_binary), *spec.argv[5:]],
        env={
            **run.env,
            **spec.env,
            "PATH": str(folder),
            "SERVER_PID": str(server_pid),
            "VIEW_ARGS": str(view_args),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, stderr = process.communicate(timeout=10)
        if http_code == "401":
            assert process.returncode == 1
            assert b"password" in stderr
            assert not view_args.exists()
        else:
            assert process.returncode == 0
            assert b"ready" in stderr
            assert view_args.read_text().splitlines() == [
                "--server",
                f"http://127.0.0.1:{spec.argv[-2]}",
            ]
            # Attaching the fallback view must leave the private server alive.
            pid = int(server_pid.read_text())
            os.kill(pid, 0)
            assert opencode.process_identity(pid)["pid"] == pid
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def test_config(tmp_path: Path) -> None:
    folder = tmp_path / "read-folder"
    folder.mkdir()
    file = tmp_path / "files" / "read.txt"
    file.parent.mkdir()
    file.touch()
    config = json.loads(opencode2.run_config("a/model", True, [str(folder), str(file)]))
    assert (
        config["provider"]
        == json.loads(opencode.run_config("a/model", True))["provider"]
    )
    agent = config["agents"]["ember-worker"]
    assert agent["mode"] == "primary"
    permissions = agent["permissions"]
    assert permissions[:1] == [
        {"action": "question", "resource": "*", "effect": "deny"}
    ]
    assert [p["resource"] for p in permissions if p["action"] == "shell"] == list(
        opencode.GIT_DENIED
    )
    assert all(p["effect"] == "deny" for p in permissions if p["action"] == "shell")
    assert {"action": "edit", "resource": "*", "effect": "deny"} in permissions
    assert [p for p in permissions if p["action"] == "external_directory"] == [
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        {"action": "external_directory", "resource": f"{folder}/*", "effect": "allow"},
        {
            "action": "external_directory",
            "resource": f"{file.parent}/*",
            "effect": "allow",
        },
    ]
    config = json.loads(opencode2.run_config("a/model", False))
    permissions = config["agents"]["ember-worker"]["permissions"]
    assert {
        "action": "external_directory",
        "resource": "*",
        "effect": "allow",
    } in permissions
    assert not any(p["action"] == "edit" for p in permissions)
    config = json.loads(opencode2.run_config("a/model", True))
    assert config["agents"]["ember-worker"]["permissions"][-1] == {
        "action": "external_directory",
        "resource": "*",
        "effect": "deny",
    }


def test_not_ready_and_safe_close(driver: OpenCode2Driver, run: Run) -> None:
    driver.close(run)
    driver.command(run, "brief")
    assert not driver.ready(run)
    assert not driver.ready(run)
    driver.close(run)


@pytest.mark.parametrize("effort", [None, "high"])
def test_ready_creates_one_session(
    driver: OpenCode2Driver, run: Run, server: Server, effort: str | None
) -> None:
    run = dataclasses.replace(run, effort=effort)
    driver.command(run, "brief")
    server.start()
    assert driver.ready(run)
    assert driver.ready(run)
    creates = [r for r in server.requests("/api/session") if r["method"] == "POST"]
    assert len(creates) == 1
    body = creates[0]["body"]
    assert body["id"] == server.state()["session"]
    assert body["agent"] == "ember-worker"
    assert body["title"] == run.subject
    model = {"id": "deepseek/deepseek-v4-pro", "providerID": "openrouter"}
    if effort:
        model["variant"] = effort
    assert body["model"] == model
    assert server.state()["server"] == opencode.process_identity(server.process.pid)  # type: ignore[union-attr]


def test_ready_rejects_401(driver: OpenCode2Driver, run: Run, server: Server) -> None:
    driver.command(run, "brief")
    server.start(password="other")
    with pytest.raises(DriverError, match="password"):
        driver.ready(run)


def test_ready_rejects_failed_create(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    driver.command(run, "brief")
    server.start()
    server.write("create-code", 400)
    with pytest.raises(DriverError, match="create a session: HTTP 400"):
        driver.ready(run)


def test_send_records_before_post_and_retains_turns(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    driver.send(dataclasses.replace(run, turn=2), "next")
    prompts = server.requests("/prompt")
    for turn, request in enumerate(prompts, 1):
        body = request["body"]
        assert body["text"] == ("brief" if turn == 1 else "next")
        assert re.fullmatch(r"msg_[0-9a-f]{26}", body["id"])
        assert request["state"]["turns"][str(turn)] == body["id"]
        assert body["id"] in request["state"]["sent"]
        assert set(body) == {"text", "id"}
    assert len(server.state()["sent"]) == 2


def test_send_rejects_failed_prompt(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write("prompt-code", 400)
    with pytest.raises(DriverError, match="HTTP 400"):
        driver.send(run, "next")


def test_send_needs_ready_session(driver: OpenCode2Driver, run: Run) -> None:
    driver.command(run, "brief")
    with pytest.raises(DriverError, match="no OpenCode session"):
        driver.send(run, "hi")


def test_wait_busy_and_pending(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write("active.json", {server.state()["session"]: {}})
    assert driver.wait(run).state == engines.RUNNING
    assert not server.requests("/message")
    server.write("active.json", {})
    assert driver.wait(run).state == engines.RUNNING
    server.write("messages.json", [assistant("partial"), prompt(server), idle()])
    assert driver.wait(run).state == engines.RUNNING


@pytest.mark.parametrize("texts", [(), ("first", "second")])
def test_wait_succeeded(
    driver: OpenCode2Driver, run: Run, server: Server, texts: tuple[str, ...]
) -> None:
    run = started(driver, run, server)
    server.write(
        "messages.json", [idle(), assistant(*texts), assistant("old"), prompt(server)]
    )
    outcome = driver.wait(run)
    assert (outcome.state, outcome.reply) == (engines.DONE, "\n".join(texts))


def test_wait_succeeds_without_assistant(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write("messages.json", [idle(), prompt(server)])
    assert driver.wait(run).reply == ""


@pytest.mark.parametrize(
    ("error", "signed_out", "provider_error"),
    [
        ({"type": "provider.auth", "message": "no key"}, True, False),
        (
            {"type": "provider.limit", "message": "no credit", "status": 402},
            False,
            True,
        ),
        ({"type": "tool.failed", "message": "failed"}, False, False),
    ],
)
def test_wait_failed(
    driver: OpenCode2Driver,
    run: Run,
    server: Server,
    error: dict[str, Any],
    signed_out: bool,
    provider_error: bool,
) -> None:
    run = started(driver, run, server)
    server.write("messages.json", [idle("failed", error=error), prompt(server)])
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.reason == error["message"]
    assert outcome.signed_out == signed_out
    assert outcome.provider_error == provider_error
    assert outcome.provider_kind == ("outage" if provider_error else None)


def test_wait_interrupted(driver: OpenCode2Driver, run: Run, server: Server) -> None:
    run = started(driver, run, server)
    server.write("messages.json", [idle("interrupted"), prompt(server)])
    assert driver.wait(run).reason == "turn 1 was interrupted"


def test_wait_uses_own_turn_before_owner_turn(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write(
        "messages.json",
        [
            idle(),
            assistant("owner reply"),
            {"type": "user", "id": "msg_owner"},
            idle(),
            assistant("Ember reply"),
            prompt(server),
            idle(),
            assistant("prior turn"),
        ],
    )
    assert driver.wait(run).reply == "Ember reply"
    assert (
        driver.foreign_input(run)
        == "1 message(s) in the OpenCode session were not sent by Ember"
    )
    driver.accept_input(run)
    assert driver.foreign_input(run) is None


def test_owner_input_skips_synthetic_messages(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write(
        "messages.json",
        [
            {"type": "synthetic", "id": "msg_resume", "text": "continue"},
            {"type": "compaction", "id": "msg_compact"},
            prompt(server),
        ],
    )
    assert driver.foreign_input(run) is None


def test_message_pagination(driver: OpenCode2Driver, run: Run, server: Server) -> None:
    run = started(driver, run, server)
    server.write("messages.json", [idle(), assistant("ok")])
    server.write("next.json", "older page/2")
    server.write(
        "pages.json",
        {
            "older page/2": {
                "data": [prompt(server), {"type": "user", "id": "msg_owner"}],
                "cursor": {"next": None},
            }
        },
    )
    assert driver.wait(run).reply == "ok"
    assert driver.foreign_input(run) is not None
    driver.accept_input(run)
    assert driver.foreign_input(run) is None


def test_unreadable_session(driver: OpenCode2Driver, run: Run, server: Server) -> None:
    run = started(driver, run, server)
    server.write("messages-code", 500)
    with pytest.raises(DriverError, match="cannot read"):
        driver.wait(run)
    assert len(server.requests("/message")) == 2
    assert str(driver.foreign_input(run)).startswith(
        "the session history could not be checked"
    )
    with pytest.raises(DriverError, match="cannot read"):
        driver.accept_input(run)
    server.stop()
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.reason == "the OpenCode server stopped before turn 1 ended"


def test_idle_and_interrupt(driver: OpenCode2Driver, run: Run, server: Server) -> None:
    assert driver.idle_state(run).state == engines.IDLE
    run = started(driver, run, server)
    assert driver.idle_state(run).state == engines.IDLE
    server.write("active.json", {server.state()["session"]: {}})
    assert driver.idle_state(run).state == engines.BUSY
    server.write("active-code", 500)
    assert driver.idle_state(run).state == engines.UNKNOWN
    driver.interrupt(run)
    assert server.requests("/interrupt")[-1]["method"] == "POST"
    server.write("interrupt-code", 500)
    with pytest.raises(DriverError, match="HTTP 500"):
        driver.interrupt(run)
    server.stop()
    with pytest.raises(DriverError, match="not running"):
        driver.interrupt(run)


@pytest.mark.parametrize(
    ("version", "major2"),
    [
        ("1.19.5", False),
        ("opencode v2.2.5", True),
        ("opencode v3.0.0", True),
        ("2.2.5", True),
        ("opencode v2.2.5-beta.1", True),
        ("unreadable", False),
        ("", False),
        ("opencode v1.19.5", False),
    ],
)
def test_version_choice(env: dict[str, str], version: str, major2: bool) -> None:
    binary = Path(env["PATH"].split(os.pathsep)[0]) / "opencode"
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    loaded = engines.load_driver("opencode", env)
    assert isinstance(loaded, OpenCode2Driver) == major2
    assert isinstance(loaded, opencode.OpenCodeDriver)
    assert loaded.env is env


@pytest.mark.parametrize("failure", ["missing", "failed", "timeout", "oserror"])
def test_version_unavailable(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    binary = Path(env["PATH"].split(os.pathsep)[0]) / "opencode"
    if failure == "missing":
        binary.unlink()
        env["PATH"] = str(binary.parent)
    elif failure == "failed":
        binary.write_text("#!/bin/sh\necho 'opencode v2.2.5'\nexit 1\n")
    else:

        def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired("opencode", 5)
            raise OSError("cannot run")

        monkeypatch.setattr(subprocess, "run", fail)
    assert type(opencode.driver(env)) is opencode.OpenCodeDriver


def test_turn_output_stops_at_current_prompt(
    driver: OpenCode2Driver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write(
        "messages.json",
        [
            idle(),
            assistant("Permission denied: /home/owner/tools/python"),
            prompt(server),
            assistant("old denial"),
        ],
    )
    output = driver.turn_output(run)
    assert "Permission denied: /home/owner/tools/python" in output
    assert "old denial" not in output
