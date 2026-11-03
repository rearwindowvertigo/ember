import dataclasses
import json
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
from ember.engines import DriverError, opencode
from ember.engines.opencode import OpenCodeDriver
from ember.runs import record
from ember.runs.record import Run

RUN_ID = "opencode-20260101-120000-abcdef"

# A stand-in for OpenCode's built-in server: it checks the password, answers
# from files in its folder and appends each POST to requests.jsonl.
STUB_SERVER = r"""
import base64, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

port, folder = int(sys.argv[1]), Path(sys.argv[2])
password = os.environ["OPENCODE_SERVER_PASSWORD"]
auth = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()


def held(name, default):
    path = folder / name
    return json.loads(path.read_text()) if path.exists() else default


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value=None):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != auth:
            return self.reply(401, {})
        if self.path == "/session":
            return self.reply(200, [])
        if self.path == "/session/status":
            return self.reply(held("status-code", 200), held("status.json", {}))
        if self.path == "/session/ses_test/message":
            return self.reply(held("messages-code", 200), held("messages.json", []))
        self.reply(404, {})

    def do_POST(self):
        if self.headers.get("Authorization") != auth:
            return self.reply(401, {})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null")
        with open(folder / "requests.jsonl", "a") as log:
            log.write(json.dumps({"path": self.path, "body": body}) + "\n")
        if self.path == "/session":
            return self.reply(200, {"id": "ses_test"})
        if self.path == "/tui/select-session":
            return self.reply(200, True)
        if self.path == "/session/ses_test/prompt_async":
            return self.reply(held("prompt-code", 204))
        if self.path == "/session/ses_test/abort":
            return self.reply(200, True)
        self.reply(404, {})


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "opencode"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": str(bin_dir),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def make_run(
    env: dict[str, str],
    tmp_path: Path,
    *,
    read_only: bool = False,
    effort: str | None = "high",
    model: str = "deepseek/deepseek-v4-pro",
) -> Run:
    return Run(
        id=RUN_ID,
        engine="opencode",
        model=model,
        effort=effort,
        read_only=read_only,
        repo=str(tmp_path / "repo"),
        worktree=str(tmp_path / "worktree"),
        branch=None if read_only else f"ember/opencode/{RUN_ID}",
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
        folder=record.make_run_dir(RUN_ID, env),
        env=env,
    )


@pytest.fixture
def driver(env: dict[str, str]) -> OpenCodeDriver:
    return OpenCodeDriver(env, stop_seconds=5, poll_seconds=0.01)


@pytest.fixture
def run(env: dict[str, str], tmp_path: Path) -> Run:
    return make_run(env, tmp_path)


class Server:
    def __init__(self, run: Run, folder: Path) -> None:
        self.run = run
        self.folder = folder
        self.process: subprocess.Popen[bytes] | None = None
        self.screen = "OpenCode's view\n"

    @property
    def port(self) -> int:
        state = json.loads((self.run.folder / opencode.STATE_NAME).read_text())
        return state["port"]

    def start(self, password: str | None = None) -> None:
        script = self.folder / "server.py"
        script.write_text(STUB_SERVER)
        if password is None:
            password = (self.run.folder / opencode.PASSWORD_NAME).read_text()
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(script), str(self.port), str(self.folder)],
            env={"OPENCODE_SERVER_PASSWORD": password},
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        if self.process is not None:
            self.process.wait(timeout=10)

    def write(self, name: str, value: object) -> None:
        (self.folder / name).write_text(json.dumps(value))

    def requests(self) -> list[dict[str, Any]]:
        path = self.folder / "requests.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def server(
    run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Server]:
    folder = tmp_path / "server"
    folder.mkdir()
    stub = Server(run, folder)

    def pane_pid(name: str, *, env: object = None) -> int | None:
        assert name == RUN_ID
        return None if stub.process is None else stub.process.pid

    def capture(name: str, *, lines: int | None = None, env: object = None) -> str:
        assert name == RUN_ID
        return stub.screen

    monkeypatch.setattr(tmux, "pane_pid", pane_pid)
    monkeypatch.setattr(tmux, "capture", capture)
    yield stub
    stub.stop()


def started(driver: OpenCodeDriver, run: Run, server: Server) -> Run:
    """Start the run: launch, server up, session ready, first message sent."""
    driver.command(run, "the brief")
    server.start()
    assert driver.ready(run)
    run = dataclasses.replace(run, turn=1)
    driver.send(run, "the brief")
    return run


def prompt_id(server: Server) -> str:
    sent = [r for r in server.requests() if r["path"].endswith("/prompt_async")]
    return sent[-1]["body"]["messageID"]


def assistant(
    parent: str,
    parts: list[dict[str, Any]],
    *,
    finish: str | None = "stop",
    error: dict[str, Any] | None = None,
    completed: bool = True,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": f"msg_reply_{len(parts)}",
        "role": "assistant",
        "parentID": parent,
        "time": {"created": 1, "completed": 2 if completed else None},
        "finish": finish,
    }
    if error is not None:
        info["error"] = error
    return {"info": info, "parts": parts}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


# Command


def test_command_runs_the_view_with_its_server(
    driver: OpenCodeDriver, run: Run, env: dict[str, str]
) -> None:
    spec = driver.command(run, "the brief")
    port = json.loads((run.folder / opencode.STATE_NAME).read_text())["port"]
    assert spec.argv == (
        str(Path(env["PATH"]) / "opencode"),
        "--port", str(port),
        "--hostname", "127.0.0.1",
        "--model", "openrouter/deepseek/deepseek-v4-pro",
        "--agent", "ember-worker",
    )  # fmt: skip
    assert spec.cwd == Path(run.worktree)
    assert spec.first_message == "the brief"
    password = run.folder / opencode.PASSWORD_NAME
    assert stat.S_IMODE(password.stat().st_mode) == 0o600
    assert spec.env["OPENCODE_SERVER_PASSWORD"] == password.read_text()
    assert spec.env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] == "200000"
    assert spec.env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"
    config = json.loads(spec.env["OPENCODE_CONFIG_CONTENT"])
    routing = config["provider"]["openrouter"]
    assert routing["options"]["extraBody"]["provider"]["zdr"] is True
    model = routing["models"]["deepseek/deepseek-v4-pro"]
    assert model["options"]["provider"]["zdr"] is True
    agent = config["agent"]["ember-worker"]["permission"]
    assert (agent["edit"], agent["write"], agent["patch"]) == ("allow",) * 3
    assert agent["bash"]["git commit*"] == "deny"
    assert config["agent"]["title"] == {"disable": True}
    assert not Path(run.worktree).exists()


def test_read_only_agent_cannot_edit(
    driver: OpenCodeDriver, env: dict[str, str], tmp_path: Path
) -> None:
    run = make_run(env, tmp_path, read_only=True, model="openrouter/x/y:free")
    spec = driver.command(run, "brief")
    config = json.loads(spec.env["OPENCODE_CONFIG_CONTENT"])
    agent = config["agent"]["ember-worker"]["permission"]
    assert (agent["edit"], agent["write"], agent["patch"]) == ("deny",) * 3
    assert agent["external_directory"] == "deny"
    assert "x/y:free" in config["provider"]["openrouter"]["models"]
    assert "openrouter/x/y:free" in spec.argv


def test_read_only_agent_may_read_its_read_paths(
    driver: OpenCodeDriver, env: dict[str, str], tmp_path: Path
) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    note = tmp_path / "notes" / "brief.md"
    note.parent.mkdir()
    note.write_text("x")
    run = dataclasses.replace(
        make_run(env, tmp_path, read_only=True),
        read_effective=(str(folder), str(note), f"{folder}/"),
    )
    config = json.loads(driver.command(run, "brief").env["OPENCODE_CONFIG_CONTENT"])
    rules = config["agent"]["ember-worker"]["permission"]["external_directory"]
    assert list(rules.items()) == [
        ("*", "deny"),
        (f"{folder}/*", "allow"),
        (f"{note.parent}/*", "allow"),
    ]


def test_editing_agent_reaches_outside_its_checkout() -> None:
    assert opencode.external_directory(False, ("/x",)) == "allow"
    assert opencode.external_directory(True, ()) == "deny"


def test_command_takes_a_new_port_each_time(driver: OpenCodeDriver, run: Run) -> None:
    driver.command(run, "brief")
    first = json.loads((run.folder / opencode.STATE_NAME).read_text())["port"]
    driver.command(run, "brief")
    second = json.loads((run.folder / opencode.STATE_NAME).read_text())["port"]
    assert first != second


def test_command_keeps_a_set_output_limit(env: dict[str, str], run: Run) -> None:
    env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = "1000"
    spec = OpenCodeDriver(env).command(run, "brief")
    assert spec.env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] == "1000"


def test_command_needs_opencode(env: dict[str, str], run: Run, tmp_path: Path) -> None:
    env["PATH"] = str(tmp_path / "empty")
    with pytest.raises(DriverError, match="not installed"):
        OpenCodeDriver(env).command(run, "brief")


def test_command_refuses_an_inherited_config(env: dict[str, str], run: Run) -> None:
    env["OPENCODE_CONFIG_CONTENT"] = "{}"
    with pytest.raises(DriverError, match="OPENCODE_CONFIG_CONTENT"):
        OpenCodeDriver(env).command(run, "brief")


# Ready and send


def test_not_ready_before_the_server_answers(driver: OpenCodeDriver, run: Run) -> None:
    driver.command(run, "brief")
    assert driver.ready(run) is False


def test_ready_records_the_server_and_creates_one_session(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    driver.command(run, "brief")
    server.start()
    assert driver.ready(run)
    assert driver.ready(run)
    state = json.loads((run.folder / opencode.STATE_NAME).read_text())
    assert state["session"] == "ses_test"
    assert server.process is not None
    assert state["server"]["pid"] == server.process.pid
    paths = [r["path"] for r in server.requests()]
    assert paths.count("/session") == 1
    selects = [r for r in server.requests() if r["path"] == "/tui/select-session"]
    assert selects[0]["body"] == {"sessionID": "ses_test"}


def test_ready_waits_for_the_view_before_showing_the_session(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    driver.command(run, "brief")
    server.start()
    server.screen = "  \n"
    assert not driver.ready(run)
    state = json.loads((run.folder / opencode.STATE_NAME).read_text())
    assert state["session"] == "ses_test"
    assert not [r for r in server.requests() if r["path"] == "/tui/select-session"]
    server.screen = "OpenCode's view\n"
    assert driver.ready(run)
    selects = [r for r in server.requests() if r["path"] == "/tui/select-session"]
    assert len(selects) == 1
    assert [r["path"] for r in server.requests()].count("/session") == 1


def test_ready_refuses_a_wrong_password(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    driver.command(run, "brief")
    server.start(password="other")
    with pytest.raises(DriverError, match="password"):
        driver.ready(run)


def test_send_posts_the_message_with_its_variant(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    prompts = [r for r in server.requests() if r["path"].endswith("/prompt_async")]
    body = prompts[0]["body"]
    assert body["model"] == {
        "providerID": "openrouter",
        "modelID": "deepseek/deepseek-v4-pro",
    }
    assert body["agent"] == "ember-worker"
    assert body["variant"] == "high"
    assert body["parts"] == [{"type": "text", "text": "the brief"}]
    state = json.loads((run.folder / opencode.STATE_NAME).read_text())
    assert state["turns"] == {"1": body["messageID"]}
    assert state["sent"] == [body["messageID"]]
    assert body["messageID"].startswith("msg")


def test_send_without_effort_sends_no_variant(
    driver: OpenCodeDriver, env: dict[str, str], tmp_path: Path, server: Server
) -> None:
    run = make_run(env, tmp_path, effort=None)
    server.run = run
    started(driver, run, server)
    prompts = [r for r in server.requests() if r["path"].endswith("/prompt_async")]
    assert "variant" not in prompts[0]["body"]


def test_send_fails_when_the_server_refuses_the_message(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    server.write("prompt-code", 400)
    with pytest.raises(DriverError, match="HTTP 400"):
        started(driver, run, server)


def test_send_needs_a_session(driver: OpenCodeDriver, run: Run) -> None:
    driver.command(run, "brief")
    with pytest.raises(DriverError, match="no OpenCode session"):
        driver.send(run, "hello")


# Wait


def test_wait_runs_while_the_session_is_busy(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    parent = prompt_id(server)
    server.write("status.json", {"ses_test": {"type": "busy"}})
    server.write("messages.json", [assistant(parent, [text("partial")])])
    assert driver.wait(run).state == engines.RUNNING
    server.write("status.json", {"ses_test": {"type": "retry", "attempt": 1}})
    assert driver.wait(run).state == engines.RUNNING


def test_wait_runs_until_a_reply_has_ended(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    parent = prompt_id(server)
    assert driver.wait(run).state == engines.RUNNING
    server.write(
        "messages.json",
        [
            assistant(parent, [{"type": "tool", "tool": "bash"}], finish="tool-calls"),
            assistant(parent, [text("not yet")], completed=False),
        ],
    )
    assert driver.wait(run).state == engines.RUNNING


def test_wait_reads_the_last_reply_split_across_parts(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    parent = prompt_id(server)
    server.write(
        "messages.json",
        [
            assistant("msg_other", [text("someone else's reply")]),
            assistant(
                parent,
                [text("looking"), {"type": "tool", "tool": "read"}],
                finish="tool-calls",
            ),
            assistant(
                parent,
                [
                    {"type": "reasoning", "text": "hidden"},
                    text("first half"),
                    {"type": "step-finish"},
                    text("second half"),
                ],
            ),
        ],
    )
    outcome = driver.wait(run)
    assert outcome.state == engines.DONE
    assert outcome.reply == "first half\nsecond half"


def test_wait_gives_an_empty_reply(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write("messages.json", [assistant(prompt_id(server), [])])
    outcome = driver.wait(run)
    assert (outcome.state, outcome.reply) == (engines.DONE, "")


def test_wait_reports_an_interrupted_turn(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    error = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
    server.write(
        "messages.json", [assistant(prompt_id(server), [], finish=None, error=error)]
    )
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.reason == "turn 1 was interrupted"
    assert not outcome.provider_error


def test_wait_reports_a_provider_error(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    error = {"name": "APIError", "data": {"message": "HTTP 402 payment required"}}
    server.write("messages.json", [assistant(prompt_id(server), [], error=error)])
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.provider_error
    assert outcome.reason == (
        "APIError: HTTP 402 payment required (OpenRouter credit is exhausted)"
    )
    error = {"name": "APIError", "data": {"message": "rate limited"}}
    server.write("messages.json", [assistant(prompt_id(server), [], error=error)])
    assert driver.wait(run).reason == "APIError: rate limited"


@pytest.mark.parametrize(
    "error",
    [
        {"name": "ProviderAuthError", "data": {"message": "no key"}},
        {"name": "APIError", "data": {"message": "unauthorized", "statusCode": 401}},
    ],
)
def test_wait_reports_a_sign_in_failure(
    driver: OpenCodeDriver, run: Run, server: Server, error: dict[str, Any]
) -> None:
    run = started(driver, run, server)
    server.write("messages.json", [assistant(prompt_id(server), [], error=error)])
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.signed_out
    assert not outcome.provider_error


def test_wait_fails_the_turn_when_the_server_stops(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.stop()
    outcome = driver.wait(run)
    assert outcome.state == engines.FAILED
    assert outcome.reason == "the OpenCode server stopped before turn 1 ended"


def test_wait_reads_the_messages_again_after_a_failed_read(
    driver: OpenCodeDriver, run: Run, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = started(driver, run, server)
    server.write("messages.json", [assistant(prompt_id(server), [text("ok")])])
    reads: list[str] = []
    real_get = driver._get

    def flaky_get(run_: Run, path: str, expected: type) -> Any:  # noqa: ANN401
        if path.endswith("/message"):
            reads.append(path)
            if len(reads) == 1:
                return None
        return real_get(run_, path, expected)

    monkeypatch.setattr(driver, "_get", flaky_get)
    outcome = driver.wait(run)
    assert (outcome.state, outcome.reply) == (engines.DONE, "ok")
    assert len(reads) == 2


def test_wait_raises_when_a_live_server_cannot_be_read(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write("messages-code", 500)
    with pytest.raises(DriverError, match="cannot read"):
        driver.wait(run)


def test_wait_needs_the_turn_s_message(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    with pytest.raises(DriverError, match="turn 2"):
        driver.wait(dataclasses.replace(run, turn=2))


# Messages Ember did not send


def user(message_id: str) -> dict[str, Any]:
    return {"info": {"id": message_id, "role": "user"}, "parts": [text("hi")]}


def test_foreign_input_is_none_when_ember_sent_every_message(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    server.write("messages.json", [user(first), assistant(first, [text("ok")])])
    assert driver.foreign_input(run) is None


def test_foreign_input_counts_messages_the_owner_typed(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    server.write(
        "messages.json",
        [user(first), assistant(first, [text("ok")]), user("msg_owner")],
    )
    assert driver.foreign_input(run) == (
        "1 message(s) in the OpenCode session were not sent by Ember"
    )


def test_accept_input_accepts_the_messages_in_the_session(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    server.write(
        "messages.json",
        [user(first), assistant(first, [text("ok")]), user("msg_owner")],
    )
    driver.accept_input(run)
    assert driver.foreign_input(run) is None


def test_foreign_input_skips_the_messages_of_a_compaction(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    compaction = {
        "info": {"id": "msg_compaction", "role": "user"},
        "parts": [{"type": "compaction", "auto": True}],
    }
    resume = {
        "info": {"id": "msg_continue", "role": "user"},
        "parts": [
            {
                "type": "text",
                "text": "Continue if you have next steps",
                "synthetic": True,
                "metadata": {"compaction_continue": True},
            }
        ],
    }
    server.write(
        "messages.json",
        [user(first), assistant(first, [text("ok")]), compaction, resume],
    )
    assert driver.foreign_input(run) is None


def test_foreign_input_counts_a_typed_message_after_a_compaction(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    compaction = {
        "info": {"id": "msg_compaction", "role": "user"},
        "parts": [{"type": "compaction", "auto": True}],
    }
    server.write(
        "messages.json",
        [user(first), assistant(first, [text("ok")]), compaction, user("msg_owner")],
    )
    assert driver.foreign_input(run) == (
        "1 message(s) in the OpenCode session were not sent by Ember"
    )


def overflow_compaction(
    replayed: str, replay_parts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the messages OpenCode adds when a turn overflows the context."""
    summary = assistant("msg_compaction", [text("summary")])
    summary["info"].update(mode="compaction", summary=True)
    return [
        assistant(replayed, [text("working")], finish=None),
        {
            "info": {"id": "msg_compaction", "role": "user"},
            "parts": [{"type": "compaction", "auto": True, "overflow": True}],
        },
        summary,
        {"info": {"id": "msg_replay", "role": "user"}, "parts": replay_parts},
    ]


def test_foreign_input_skips_the_replay_of_an_overflow_compaction(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    server.write(
        "messages.json",
        [
            user(first),
            *overflow_compaction(first, [{**text("hi"), "id": "prt_copy"}]),
            assistant("msg_replay", [text("ok")]),
        ],
    )
    assert driver.foreign_input(run) is None


def test_foreign_input_skips_the_replay_of_a_replay(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    second = [
        {**message, "info": {**message["info"], "id": f"{message['info']['id']}2"}}
        for message in overflow_compaction("msg_replay", [text("hi")])
    ]
    server.write(
        "messages.json",
        [user(first), *overflow_compaction(first, [text("hi")]), *second],
    )
    assert driver.foreign_input(run) is None


def test_foreign_input_counts_the_replay_of_a_typed_message(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    server.write(
        "messages.json",
        [
            user(first),
            assistant(first, [text("ok")]),
            {"info": {"id": "msg_owner", "role": "user"}, "parts": [text("typed")]},
            *overflow_compaction("msg_owner", [text("typed")]),
        ],
    )
    assert driver.foreign_input(run) == (
        "2 message(s) in the OpenCode session were not sent by Ember"
    )


def test_foreign_input_counts_a_different_message_after_a_summary(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    first = prompt_id(server)
    server.write(
        "messages.json",
        [user(first), *overflow_compaction(first, [text("typed")])],
    )
    assert driver.foreign_input(run) == (
        "1 message(s) in the OpenCode session were not sent by Ember"
    )


def test_foreign_input_reports_an_unreadable_session(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    server.write("messages-code", 500)
    found = driver.foreign_input(run)
    assert found is not None
    assert found.startswith("the session history could not be checked")


def test_foreign_input_without_a_session(driver: OpenCodeDriver, run: Run) -> None:
    driver.command(run, "brief")
    found = driver.foreign_input(run)
    assert found is not None
    assert found.startswith("the session history could not be checked")


# Idle state, interrupt and close


def test_idle_state_follows_the_session_status(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    assert driver.idle_state(run).state == engines.IDLE
    server.write("status.json", {"ses_test": {"type": "busy"}})
    assert driver.idle_state(run).state == engines.BUSY
    server.write("status.json", {"ses_test": "odd"})
    assert driver.idle_state(run).state == engines.UNKNOWN
    server.write("status.json", {"ses_test": {"type": "idle"}})
    assert driver.idle_state(run).state == engines.IDLE
    server.stop()
    assert driver.idle_state(run).state == engines.IDLE


def test_idle_state_is_unknown_without_the_server_process(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    driver.command(run, "brief")
    server.start()
    assert driver.idle_state(run).state == engines.UNKNOWN


def test_idle_state_of_a_run_that_never_started(
    driver: OpenCodeDriver, run: Run
) -> None:
    assert driver.idle_state(run).state == engines.IDLE


def test_interrupt_aborts_the_session(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    driver.interrupt(run)
    assert server.requests()[-1]["path"] == "/session/ses_test/abort"
    server.stop()
    with pytest.raises(DriverError, match="not running"):
        driver.interrupt(run)


def test_close_stops_the_server(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    driver.close(run)
    assert server.process is not None
    assert server.process.wait(timeout=10) != 0
    driver.close(run)


def test_close_is_safe_when_nothing_started(driver: OpenCodeDriver, run: Run) -> None:
    driver.close(run)
    driver.command(run, "brief")
    driver.close(run)
    driver.close(run)


def test_close_refuses_a_server_without_a_recorded_process(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    driver.command(run, "brief")
    server.start()
    with pytest.raises(DriverError, match="no recorded process"):
        driver.close(run)
    assert server.process is not None
    assert server.process.poll() is None


def test_close_leaves_a_reused_process_id_alone(
    driver: OpenCodeDriver, run: Run, server: Server
) -> None:
    run = started(driver, run, server)
    path = run.folder / opencode.STATE_NAME
    state = json.loads(path.read_text())
    state["server"]["start_time"] = "1"
    path.write_text(json.dumps(state))
    with pytest.raises(DriverError, match="did not stop"):
        OpenCodeDriver(run.env, stop_seconds=0.2, poll_seconds=0.01).close(run)
    assert server.process is not None
    assert server.process.poll() is None


# Efforts


def test_valid_effort() -> None:
    driver = OpenCodeDriver({})
    assert driver.valid_effort("high")
    assert driver.valid_effort("max")
    assert driver.valid_effort("x2")
    assert not driver.valid_effort("High")
    assert not driver.valid_effort("")
    assert not driver.valid_effort("2x")


def test_default_effort_comes_from_the_openrouter_list(env: dict[str, str]) -> None:
    reply = {
        "data": [
            {"id": "a/thinker", "reasoning": {"default_effort": "medium"}},
            {"id": "a/plain"},
        ]
    }
    fetched: list[str] = []

    def fetch(url: str, timeout: float) -> object:
        fetched.append(url)
        return reply

    driver = OpenCodeDriver(env, fetch=fetch)
    assert driver.default_effort("a/thinker") == "medium"
    assert driver.default_effort("openrouter/a/thinker:free") == "medium"
    assert driver.default_effort("a/plain") is None
    assert driver.default_effort("a/missing") is None
    assert len(fetched) == 1


def test_default_effort_without_a_list(env: dict[str, str]) -> None:
    def fetch(url: str, timeout: float) -> object:
        raise OSError("offline")

    assert OpenCodeDriver(env, fetch=fetch).default_effort("a/thinker") is None


def test_load_driver(env: dict[str, str]) -> None:
    loaded = engines.load_driver("opencode", env)
    assert isinstance(loaded, OpenCodeDriver)
    assert loaded.env is env


@pytest.mark.parametrize(
    ("screen", "age", "selects"),
    [
        (f"  {opencode.START_SCREEN} █▀▀▄\n  Ask anything", 5, 1),
        ("  Brief from Claude\n  Thinking", 5, 0),
        (f"  {opencode.START_SCREEN}", opencode.SHOW_SECONDS + 1, 0),
    ],
)
def test_wait_shows_the_session_while_the_start_screen_returns(
    driver: OpenCodeDriver,
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    screen: str,
    age: float,
    selects: int,
) -> None:
    run = dataclasses.replace(make_run(env, tmp_path), started_at=time.time() - age)
    calls: list[str] = []
    monkeypatch.setattr(tmux, "capture", lambda *_args, **_kwargs: screen)
    monkeypatch.setattr(driver, "_select", lambda _run, session: calls.append(session))
    driver._show_session(run, "ses_1")
    assert calls == ["ses_1"] * selects
