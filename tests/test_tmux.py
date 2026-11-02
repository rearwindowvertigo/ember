import contextlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ember import tmux

STUB = """#!{python}
import json, os, sys
args = sys.argv[1:]
data = sys.stdin.read()
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(here, "log.jsonl"), "a") as log:
    record = {{"args": args, "stdin": data, "env": dict(os.environ)}}
    log.write(json.dumps(record) + "\\n")
with open(os.path.join(here, "conf.json")) as conf:
    replies = json.load(conf)
reply = replies.get(args[2], {{}})
sys.stdout.write(reply.get("out", ""))
sys.stderr.write(reply.get("err", ""))
sys.exit(reply.get("code", 0))
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tmux"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    conf = tmp_path / "conf.json"
    conf.write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "PATH": str(bin_dir),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "STUB_LOG": str(tmp_path / "log.jsonl"),
        "STUB_CONF": str(conf),
    }


def _reply(env: dict[str, str], command: str, **reply: object) -> None:
    path = Path(env["STUB_CONF"])
    replies = json.loads(path.read_text())
    replies[command] = reply
    path.write_text(json.dumps(replies))


def _calls(env: dict[str, str]) -> list[dict[str, Any]]:
    path = Path(env["STUB_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _socket(env: dict[str, str]) -> str:
    return str(Path(env["XDG_RUNTIME_DIR"]) / "ember" / "tmux.sock")


def _commands(env: dict[str, str]) -> list[list[str]]:
    calls = _calls(env)
    for call in calls:
        assert call["args"][:2] == ["-S", _socket(env)]
    return [call["args"][2:] for call in calls]


def _make_socket(env: dict[str, str]) -> None:
    tmux.socket_path(env).touch()


def test_socket_path_creates_private_folder(env: dict[str, str]) -> None:
    path = tmux.socket_path(env)
    assert path == Path(env["XDG_RUNTIME_DIR"]) / "ember" / "tmux.sock"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("name", ["a", "run-1", "0abc", "a" * 64])
def test_valid_names(name: str) -> None:
    assert tmux.check_name(name) == name


@pytest.mark.parametrize("name", ["", "-a", "A", "a_b", "a.b", "a:b", "a" * 65, "a b"])
def test_invalid_names_raise_without_running_tmux(
    env: dict[str, str], name: str
) -> None:
    with pytest.raises(tmux.TmuxError):
        tmux.kill(name, env=env)
    with pytest.raises(tmux.TmuxError):
        tmux.exists(name, env=env)
    assert _calls(env) == []


def test_new_session(env: dict[str, str], tmp_path: Path) -> None:
    tmux.new_session(
        "run-1",
        cwd=tmp_path,
        argv=["codex", "--model", "a b", "it's"],
        env_vars={"EXTRA": "1", "HOME": "/elsewhere"},
        env={**env, "TMUX": "/tmp/other,1,0", "TMUX_PANE": "%1"},
    )
    (command,) = _commands(env)
    assert command[:5] == ["set-option", "-g", "remain-on-exit", "on", ";"]
    rest = command[5:]
    assert rest[:10] == [
        "new-session", "-d", "-s", "run-1",
        "-x", "200", "-y", "50", "-c", str(tmp_path),
    ]  # fmt: skip
    assert rest[-1] == "codex --model 'a b' 'it'\"'\"'s'"
    settings = rest[10:-1]
    pairs = [settings[i + 1] for i in range(0, len(settings), 2)]
    assert all(settings[i] == "-e" for i in range(0, len(settings), 2))
    assert "EXTRA=1" in pairs
    assert "HOME=/elsewhere" in pairs
    assert f"HOME={env['HOME']}" not in pairs
    assert f"PATH={env['PATH']}" in pairs
    assert not any(pair.startswith(("TMUX=", "TMUX_PANE=")) for pair in pairs)


def test_commands_run_with_empty_environment(
    env: dict[str, str], tmp_path: Path
) -> None:
    tmux.new_session(
        "s",
        cwd=tmp_path,
        argv=["x"],
        env_vars={},
        env={**env, "SECRET_KEY": "leak"},
    )
    tmux.exists("s", env=env)
    for call in _calls(env):
        assert "SECRET_KEY" not in call["env"]
        assert "STUB_LOG" not in call["env"]


def test_new_session_size(env: dict[str, str], tmp_path: Path) -> None:
    tmux.new_session(
        "s", cwd=tmp_path, argv=["x"], env_vars={}, width=80, height=24, env=env
    )
    (command,) = _commands(env)
    assert command[9:13] == ["-x", "80", "-y", "24"]


def test_new_session_rejects_empty_command(env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(tmux.TmuxError):
        tmux.new_session("s", cwd=tmp_path, argv=[], env_vars={}, env=env)
    assert _calls(env) == []


def test_new_session_failure_raises(env: dict[str, str], tmp_path: Path) -> None:
    _reply(env, "set-option", code=1, err="duplicate session: s\n")
    with pytest.raises(tmux.TmuxError, match="duplicate session"):
        tmux.new_session("s", cwd=tmp_path, argv=["x"], env_vars={}, env=env)


def test_exists(env: dict[str, str]) -> None:
    assert tmux.exists("s", env=env) is True
    _reply(env, "has-session", code=1, err="can't find session: s\n")
    assert tmux.exists("s", env=env) is False
    assert _commands(env) == [["has-session", "-t", "=s"]] * 2


def test_send_text_single_line(env: dict[str, str]) -> None:
    tmux.send_text("s", "-fix the bug", env=env)
    assert _commands(env) == [
        ["send-keys", "-t", "=s:", "-l", "--", "-fix the bug"],
        ["send-keys", "-t", "=s:", "Enter"],
    ]


def test_send_text_multi_line_uses_bracketed_paste(env: dict[str, str]) -> None:
    tmux.send_text("s", "one\ntwo", env=env)
    calls = _calls(env)
    assert [call["args"][2:] for call in calls] == [
        ["load-buffer", "-b", "ember-s", "-"],
        ["paste-buffer", "-p", "-d", "-b", "ember-s", "-t", "=s:"],
        ["send-keys", "-t", "=s:", "Enter"],
    ]
    assert calls[0]["stdin"] == "one\ntwo"


def test_send_text_empty_sends_enter(env: dict[str, str]) -> None:
    tmux.send_text("s", "", env=env)
    assert _commands(env) == [["send-keys", "-t", "=s:", "Enter"]]


def test_send_text_missing_session_raises(env: dict[str, str]) -> None:
    _reply(env, "send-keys", code=1, err="can't find session: s\n")
    with pytest.raises(tmux.TmuxError, match="can't find session"):
        tmux.send_text("s", "x", env=env)


def test_send_keys_and_interrupt(env: dict[str, str]) -> None:
    tmux.send_keys("s", "Escape", "Enter", env=env)
    tmux.interrupt("s", env=env)
    assert _commands(env) == [
        ["send-keys", "-t", "=s:", "Escape", "Enter"],
        ["send-keys", "-t", "=s:", "C-c"],
    ]
    with pytest.raises(tmux.TmuxError):
        tmux.send_keys("s", env=env)


def test_capture_visible(env: dict[str, str]) -> None:
    _reply(env, "capture-pane", out="a\nb\n")
    assert tmux.capture("s", env=env) == "a\nb\n"
    assert _commands(env) == [["capture-pane", "-p", "-J", "-t", "=s:"]]


def test_capture_last_lines(env: dict[str, str]) -> None:
    _reply(env, "capture-pane", out="h1\nh2\nh3\nv1\nv2\n")
    assert tmux.capture("s", lines=3, env=env) == "h3\nv1\nv2\n"
    assert _commands(env) == [["capture-pane", "-p", "-J", "-t", "=s:", "-S", "-3"]]


def test_capture_last_lines_drops_empty_rows_at_end(env: dict[str, str]) -> None:
    _reply(env, "capture-pane", out="line1\nline2\nline3\n" + "\n" * 47)
    assert tmux.capture("s", lines=5, env=env) == "line1\nline2\nline3\n"
    assert tmux.capture("s", lines=2, env=env) == "line2\nline3\n"
    _reply(env, "capture-pane", out="\n\n\n")
    assert tmux.capture("s", lines=2, env=env) == ""


@pytest.mark.parametrize("lines", [0, -1, True])
def test_capture_rejects_nonpositive(env: dict[str, str], lines: int) -> None:
    with pytest.raises(tmux.TmuxError):
        tmux.capture("s", lines=lines, env=env)
    assert _calls(env) == []


def test_pane_dead_and_pid(env: dict[str, str]) -> None:
    _reply(env, "display-message", out="0 4242\n")
    assert tmux.pane_pid("s", env=env) == 4242
    _reply(env, "display-message", out="1 4242\n")
    assert tmux.pane_pid("s", env=env) is None
    _reply(env, "display-message", out="1\n")
    assert tmux.pane_dead("s", env=env) is True
    _reply(env, "display-message", out="0\n")
    assert tmux.pane_dead("s", env=env) is False
    commands = _commands(env)
    assert commands[0] == [
        "display-message",
        "-p",
        "-t",
        "=s:",
        "#{pane_dead} #{pane_pid}",
    ]
    assert commands[2] == ["display-message", "-p", "-t", "=s:", "#{pane_dead}"]


def test_pane_pid_unexpected_reply_raises(env: dict[str, str]) -> None:
    _reply(env, "display-message", out="garbage\n")
    with pytest.raises(tmux.TmuxError, match="unexpected"):
        tmux.pane_pid("s", env=env)


def test_pane_queries_raise_for_missing_session(env: dict[str, str]) -> None:
    _reply(env, "display-message", code=1, err="can't find session: s\n")
    with pytest.raises(tmux.TmuxError):
        tmux.pane_pid("s", env=env)
    with pytest.raises(tmux.TmuxError):
        tmux.pane_dead("s", env=env)


def test_kill(env: dict[str, str]) -> None:
    tmux.kill("s", env=env)
    assert _commands(env) == [["kill-session", "-t", "=s"]]
    _reply(env, "kill-session", code=1, err="can't find session: s\n")
    with pytest.raises(tmux.TmuxError):
        tmux.kill("s", env=env)


def test_list_sessions_without_socket_is_empty(env: dict[str, str]) -> None:
    assert tmux.list_sessions(env=env) == []
    assert _calls(env) == []


def test_list_sessions(env: dict[str, str]) -> None:
    _make_socket(env)
    _reply(env, "list-sessions", out="a\nb-2\n")
    assert tmux.list_sessions(env=env) == ["a", "b-2"]
    assert _commands(env) == [["list-sessions", "-F", "#{session_name}"]]


@pytest.mark.parametrize(
    "err",
    ["no server running on /x/tmux.sock\n", "error connecting to /x (No such file)\n"],
)
def test_list_sessions_no_server_is_empty(env: dict[str, str], err: str) -> None:
    _make_socket(env)
    _reply(env, "list-sessions", code=1, err=err)
    assert tmux.list_sessions(env=env) == []


def test_list_sessions_other_failure_raises(env: dict[str, str]) -> None:
    _make_socket(env)
    _reply(env, "list-sessions", code=1, err="something else\n")
    with pytest.raises(tmux.TmuxError, match="something else"):
        tmux.list_sessions(env=env)


def test_missing_tmux_raises(env: dict[str, str], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(tmux.TmuxError, match="not installed"):
        tmux.kill("s", env={**env, "PATH": str(empty)})


def test_timeout_raises(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux, "TIMEOUT", 0.5)
    stub = Path(env["PATH"]) / "tmux"
    stub.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    with pytest.raises(tmux.TmuxError, match="timed out"):
        tmux.kill("s", env=env)


def _wait_for(path: Path) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().endswith("\n"):
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} was not written")


def test_open_window(env: dict[str, str], tmp_path: Path) -> None:
    out = tmp_path / "terminal.json"
    terminal = Path(env["PATH"]) / "term"
    terminal.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(out)!r} + '.tmp', 'w').write(json.dumps({{\n"
        "    'args': sys.argv[1:],\n"
        "    'pgid_is_pid': os.getpgid(0) == os.getpid(),\n"
        "    'tmux': os.environ.get('TMUX'),\n"
        "    'stdin_tty': sys.stdin.isatty(),\n"
        f"}}) + '\\n')\n"
        f"os.replace({str(out)!r} + '.tmp', {str(out)!r})\n"
    )
    terminal.chmod(0o755)
    pid = tmux.open_window(
        "s", terminal="term --title 'a b' --", env={**env, "TMUX": "/x,1,0"}
    )
    assert pid > 0
    record = json.loads(_wait_for(out))
    assert record["args"] == [
        "--title", "a b", "--",
        "tmux", "-S", _socket(env), "attach-session", "-t", "=s",
    ]  # fmt: skip
    assert record["pgid_is_pid"] is True
    assert record["tmux"] is None
    assert record["stdin_tty"] is False
    assert _commands(env) == [["has-session", "-t", "=s"]]
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def test_open_window_missing_session_raises(env: dict[str, str]) -> None:
    _reply(env, "has-session", code=1)
    with pytest.raises(tmux.TmuxError, match="no session"):
        tmux.open_window("s", terminal="term", env=env)


@pytest.mark.parametrize("terminal", ["", "  ", "'unclosed"])
def test_open_window_bad_terminal_raises(env: dict[str, str], terminal: str) -> None:
    with pytest.raises(tmux.TmuxError):
        tmux.open_window("s", terminal=terminal, env=env)


def test_open_window_missing_terminal_raises(env: dict[str, str]) -> None:
    with pytest.raises(tmux.TmuxError, match="cannot start"):
        tmux.open_window("s", terminal="no-such-terminal", env=env)


def test_control_argv(env: dict[str, str]) -> None:
    argv = tmux.control_argv("run-000001", env=env)
    assert argv == [
        str(Path(env["PATH"]) / "tmux"), "-S", _socket(env),
        "-C", "attach-session", "-f", "read-only,ignore-size", "-t", "=run-000001",
    ]  # fmt: skip
    assert _calls(env) == []


@pytest.mark.parametrize("name", ["", "Run", "a b", "=x", "a" * 65])
def test_control_argv_rejects_bad_names(env: dict[str, str], name: str) -> None:
    with pytest.raises(tmux.TmuxError, match="invalid session name"):
        tmux.control_argv(name, env=env)


def test_control_argv_without_tmux_raises(env: dict[str, str], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(tmux.TmuxError, match="not installed"):
        tmux.control_argv("s", env={**env, "PATH": str(empty)})
