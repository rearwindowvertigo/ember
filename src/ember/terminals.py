"""The terminals Ember knows, and the command lines that open them.

The `terminal` setting holds a command line prefix: the terminal and the
options after which it takes the command to run, such as `gnome-terminal --`.
A run's window adds `tmux … attach-session` to the prefix
(`ember.tmux.open_window`). An update, install or sign-in command is wrapped
so the window stays open after the command ends, showing its exit code until
Enter is pressed.
"""

import os
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Terminal:
    """A known terminal."""

    id: str
    """A short unique name."""

    label: str
    """The name shown in Settings."""

    executable: str
    """The program looked up on `PATH`."""

    prefix: str
    """The `terminal` setting's value for this terminal."""

    available: bool = True
    """Whether the executable was found on `PATH`."""


SYSTEM = "system"
"""The id of the desktop's default terminal, opened by `xdg-terminal-exec`."""

KNOWN: tuple[Terminal, ...] = (
    Terminal(SYSTEM, "System default", "xdg-terminal-exec", "xdg-terminal-exec"),
    Terminal("gnome-terminal", "GNOME Terminal", "gnome-terminal", "gnome-terminal --"),
    Terminal("ptyxis", "Ptyxis", "ptyxis", "ptyxis --new-window --"),
    Terminal("kgx", "Console", "kgx", "kgx --"),
    Terminal("kitty", "kitty", "kitty", "kitty"),
    Terminal("alacritty", "Alacritty", "alacritty", "alacritty -e"),
    Terminal("foot", "foot", "foot", "foot"),
    Terminal("wezterm", "WezTerm", "wezterm", "wezterm start --"),
    Terminal("ghostty", "Ghostty", "ghostty", "ghostty -e"),
    Terminal("konsole", "Konsole", "konsole", "konsole -e"),
    Terminal("xfce4-terminal", "Xfce Terminal", "xfce4-terminal", "xfce4-terminal -x"),
    Terminal("xterm", "XTerm", "xterm", "xterm -e"),
)
"""Every known terminal, System default first."""

WRAP = (
    'sh -c "$1"; code=$?; '
    'printf "\\nExit code %s. Press Enter to close.\\n" "$code"; read -r _'
)
"""The script that runs a command, then waits for Enter.

The command is the script's first argument, never part of the script.
"""


def found(env: Mapping[str, str] | None = None) -> list[Terminal]:
    """Return the known terminals on this machine.

    Args:
        env: The environment whose `PATH` is searched; `None` reads
            `os.environ`.

    Returns:
        System default first, with `available` false when
        `xdg-terminal-exec` is not on `PATH`, then each other known terminal
        whose executable is on `PATH`.
    """
    path = (os.environ if env is None else env).get("PATH", "")
    result: list[Terminal] = []
    for terminal in KNOWN:
        available = shutil.which(terminal.executable, path=path) is not None
        if terminal.id == SYSTEM or available:
            result.append(replace(terminal, available=available))
    return result


def _split(value: str) -> list[str] | None:
    try:
        return shlex.split(value)
    except ValueError:
        return None


def match(value: str, env: Mapping[str, str] | None = None) -> Terminal | None:
    """Return the known terminal a `terminal` setting value names.

    A saved known terminal is returned whether or not it is installed, so
    Settings can show it as unavailable until another value is chosen.

    Args:
        value: The setting's value.
        env: The environment whose `PATH` is searched; `None` reads
            `os.environ`.

    Returns:
        The known terminal whose prefix splits into the same words as the
        value, with `available` false when its executable is not on `PATH`,
        or `None` (shown as Other command).
    """
    words = _split(value)
    if not words:
        return None
    path = (os.environ if env is None else env).get("PATH", "")
    for terminal in KNOWN:
        if shlex.split(terminal.prefix) == words:
            available = shutil.which(terminal.executable, path=path) is not None
            return replace(terminal, available=available)
    return None


def command_argv(terminal: str, command: str) -> list[str]:
    """Return the command line that runs a command in a new terminal window.

    The window shows the command's exit code after it ends and stays open
    until Enter is pressed.

    Args:
        terminal: The `terminal` setting's value.
        command: The shell command to run, passed to the script as an
            argument.

    Returns:
        `[*shlex.split(terminal), "sh", "-c", WRAP, "ember-command", command]`.

    Raises:
        ValueError: The terminal value is empty or cannot be parsed.
    """
    words = _split(terminal)
    if words is None:
        raise ValueError(f"cannot parse the terminal command {terminal!r}")
    if not words:
        raise ValueError("the terminal command is empty")
    return [*words, "sh", "-c", WRAP, "ember-command", command]
