"""Worker sessions on a tmux server private to Ember.

Every command runs `tmux -S <socket>` with the socket in Ember's runtime
folder, never the default server, and targets a session by its exact name
(`=<name>`, or `=<name>:` for its pane). Each command may take 10 seconds;
a failure raises `TmuxError`.

Every function takes an optional `env` mapping, which defaults to
`os.environ`. It gives the `PATH` that finds `tmux`, the runtime folder, and
the environment of a server the command starts.
"""

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from ember import paths

TIMEOUT = 10
"""Seconds each tmux command may take."""

SOCKET_NAME = "tmux.sock"
"""The name of the server's socket in Ember's runtime folder."""

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

_PANE_OWNED = ("TMUX", "TMUX_PANE")
"""Variables tmux sets in each pane, left out of a session's environment."""

_NO_SERVER = ("no server running", "error connecting to")


class TmuxError(RuntimeError):
    """A tmux command failed, timed out or could not start."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def check_name(name: str) -> str:
    """Return a session name after checking it.

    Args:
        name: The session name.

    Returns:
        `name`.

    Raises:
        TmuxError: The name does not match `[a-z0-9][a-z0-9-]{0,63}`.
    """
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise TmuxError(f"invalid session name {name!r}")
    return name


def _session(name: str) -> str:
    return f"={check_name(name)}"


def _pane(name: str) -> str:
    return f"={check_name(name)}:"


def socket_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the server's socket, creating its folder at mode 700.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `tmux.sock` in Ember's runtime folder.

    Raises:
        RuntimeDirError: No runtime base directory is available.
    """
    root = paths.runtime_dir(env)
    paths.ensure_private_dir(root, root=root)
    return root / SOCKET_NAME


def _tmux_binary(env: Mapping[str, str]) -> str:
    found = shutil.which("tmux", path=env.get("PATH", os.defpath))
    if found is None:
        raise TmuxError("tmux is not installed or not on PATH")
    return found


def _run(
    args: Sequence[str],
    env: Mapping[str, str] | None,
    *,
    input: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one tmux command against Ember's server.

    Args:
        args: The tmux command and its arguments.
        env: The environment; `None` reads `os.environ`.
        input: Text for the command's standard input.
        check: Raise `TmuxError` when the command exits non-zero.

    Returns:
        The finished command, with standard output and error as text.

    Raises:
        TmuxError: tmux is missing, could not start, timed out or, with
            `check`, failed.
    """
    env = _env(env)
    argv = [_tmux_binary(env), "-S", str(socket_path(env)), *args]
    try:
        result = subprocess.run(
            argv,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=dict(env),
        )
    except subprocess.TimeoutExpired as error:
        raise TmuxError(f"tmux {args[0]} timed out after {TIMEOUT} seconds") from error
    except OSError as error:
        raise TmuxError(f"tmux {args[0]} could not start: {error}") from error
    if check and result.returncode:
        message = result.stderr.strip() or f"exit code {result.returncode}"
        raise TmuxError(f"tmux {args[0]} failed: {message.splitlines()[0]}")
    return result


def new_session(
    name: str,
    *,
    cwd: Path,
    argv: Sequence[str],
    env_vars: Mapping[str, str],
    width: int = 200,
    height: int = 50,
    env: Mapping[str, str] | None = None,
) -> None:
    """Start a detached session whose single pane runs a command.

    The pane's environment is `env` overlaid with `env_vars`, set on the
    session; the server's global environment is not changed. The server's
    `remain-on-exit` option is on, so the pane's last screen stays readable
    after the command ends.

    Args:
        name: The session name.
        cwd: The pane's working folder.
        argv: The command and its arguments, quoted with `shlex.join`.
        env_vars: Variables added to or replacing those of `env`.
        width: The pane's width in columns.
        height: The pane's height in lines.
        env: The environment; `None` reads `os.environ`.

    Raises:
        TmuxError: The name or command is not valid, or tmux failed, for
            example because the session exists.
    """
    check_name(name)
    if not argv:
        raise TmuxError("the session command is empty")
    variables = {**_env(env), **env_vars}
    settings = [
        arg
        for key, value in variables.items()
        if key not in _PANE_OWNED
        for arg in ("-e", f"{key}={value}")
    ]
    _run(
        [
            "set-option", "-g", "remain-on-exit", "on", ";",
            "new-session", "-d", "-s", name,
            "-x", str(width), "-y", str(height),
            "-c", str(cwd),
            *settings,
            shlex.join(argv),
        ],
        env,
    )  # fmt: skip


def exists(name: str, *, env: Mapping[str, str] | None = None) -> bool:
    """Return whether a session exists.

    Args:
        name: The session name.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `True` if the server has a session of exactly that name; `False` if
        it has none or no server is running.

    Raises:
        TmuxError: The name is not valid, or tmux is missing or timed out.
    """
    return _run(["has-session", "-t", _session(name)], env, check=False).returncode == 0


def send_text(name: str, text: str, *, env: Mapping[str, str] | None = None) -> None:
    """Type text into a session's pane, then press Enter.

    Text on one line is sent literally with `send-keys -l`. Text with a
    newline is loaded into a buffer from standard input and pasted with
    bracketed paste, so the program receives it as one paste. Enter is sent
    as a separate key.

    Args:
        name: The session name.
        text: The text.
        env: The environment; `None` reads `os.environ`.

    Raises:
        TmuxError: The session does not exist or tmux failed.
    """
    target = _pane(name)
    if "\n" in text:
        buffer = f"ember-{name}"
        _run(["load-buffer", "-b", buffer, "-"], env, input=text)
        _run(["paste-buffer", "-p", "-d", "-b", buffer, "-t", target], env)
    elif text:
        _run(["send-keys", "-t", target, "-l", "--", text], env)
    _run(["send-keys", "-t", target, "Enter"], env)


def send_keys(name: str, *keys: str, env: Mapping[str, str] | None = None) -> None:
    """Send tmux key names, such as `Enter` or `C-c`, to a session's pane.

    Args:
        name: The session name.
        *keys: The key names.
        env: The environment; `None` reads `os.environ`.

    Raises:
        TmuxError: No keys are given, the session does not exist or tmux
            failed.
    """
    target = _pane(name)
    if not keys:
        raise TmuxError("no keys to send")
    _run(["send-keys", "-t", target, *keys], env)


def interrupt(name: str, *, env: Mapping[str, str] | None = None) -> None:
    """Send Ctrl+C to a session's pane.

    Args:
        name: The session name.
        env: The environment; `None` reads `os.environ`.

    Raises:
        TmuxError: The session does not exist or tmux failed.
    """
    send_keys(name, "C-c", env=env)


def capture(
    name: str, *, lines: int | None = None, env: Mapping[str, str] | None = None
) -> str:
    """Return the text of a session's pane, with wrapped lines joined.

    Args:
        name: The session name.
        lines: `None` for the visible pane; otherwise the last `lines` lines
            available, counting the visible pane and its history.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The text, one line per pane line.

    Raises:
        TmuxError: `lines` is not a positive number, the session does not
            exist or tmux failed.
    """
    target = _pane(name)
    args = ["capture-pane", "-p", "-J", "-t", target]
    if lines is not None:
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
            raise TmuxError(f"lines must be a positive number, not {lines!r}")
        args += ["-S", f"-{lines}"]
    text = _run(args, env).stdout
    if lines is None:
        return text
    return "".join(text.splitlines(keepends=True)[-lines:])


def _display(name: str, form: str, env: Mapping[str, str] | None) -> str:
    return _run(["display-message", "-p", "-t", _pane(name), form], env).stdout.strip()


def pane_dead(name: str, *, env: Mapping[str, str] | None = None) -> bool:
    """Return whether the command in a session's pane has ended.

    Args:
        name: The session name.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `True` if the pane's command has ended.

    Raises:
        TmuxError: The session does not exist or tmux failed.
    """
    return _display(name, "#{pane_dead}", env) == "1"


def pane_pid(name: str, *, env: Mapping[str, str] | None = None) -> int | None:
    """Return the process id of the command in a session's pane.

    Args:
        name: The session name.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The process id, or `None` if the pane's command has ended.

    Raises:
        TmuxError: The session does not exist, tmux failed or its reply is
            not a process id.
    """
    reply = _display(name, "#{pane_dead} #{pane_pid}", env)
    dead, _, pid = reply.partition(" ")
    if dead == "1":
        return None
    if dead != "0" or not pid.isdigit():
        raise TmuxError(f"tmux display-message gave an unexpected reply: {reply!r}")
    return int(pid)


def kill(name: str, *, env: Mapping[str, str] | None = None) -> None:
    """End a session and the command in its pane.

    Args:
        name: The session name.
        env: The environment; `None` reads `os.environ`.

    Raises:
        TmuxError: The session does not exist or tmux failed.
    """
    _run(["kill-session", "-t", _session(name)], env)


def list_sessions(*, env: Mapping[str, str] | None = None) -> list[str]:
    """Return the names of the server's sessions.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        The names, in tmux's order; empty when no server is running.

    Raises:
        TmuxError: tmux failed for another reason.
    """
    if not os.path.lexists(socket_path(env)):
        return []
    result = _run(["list-sessions", "-F", "#{session_name}"], env, check=False)
    if result.returncode:
        if any(text in result.stderr for text in _NO_SERVER):
            return []
        message = result.stderr.strip() or f"exit code {result.returncode}"
        raise TmuxError(f"tmux list-sessions failed: {message.splitlines()[0]}")
    return [line for line in result.stdout.splitlines() if line]


def open_window(
    name: str, *, terminal: str, env: Mapping[str, str] | None = None
) -> int:
    """Open a terminal window attached to a session.

    The terminal runs detached in its own process group, with standard
    input, output and error on `/dev/null`. Closing the window leaves the
    session running.

    Args:
        name: The session name.
        terminal: The command line that opens a terminal and runs the
            command after it, such as `xdg-terminal-exec`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The terminal's process id.

    Raises:
        TmuxError: The terminal command is empty or cannot start, the session
            does not exist or tmux failed.
    """
    target = _session(name)
    try:
        command = shlex.split(terminal)
    except ValueError as error:
        raise TmuxError(f"cannot parse the terminal command: {error}") from error
    if not command:
        raise TmuxError("the terminal command is empty")
    if not exists(name, env=env):
        raise TmuxError(f"no session {name}")
    variables = {
        key: value for key, value in _env(env).items() if key not in _PANE_OWNED
    }
    argv = [
        *command,
        "tmux",
        "-S",
        str(socket_path(env)),
        "attach-session",
        "-t",
        target,
    ]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=variables,
        )
    except OSError as error:
        raise TmuxError(f"cannot start the terminal {command[0]}: {error}") from error
    return process.pid
