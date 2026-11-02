import subprocess
from pathlib import Path

import pytest

from ember import terminals


def _stub(folder: Path, name: str, body: str = "exit 0\n") -> Path:
    path = folder / name
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(0o755)
    return path


def test_known_order_and_prefixes() -> None:
    assert [(item.id, item.prefix) for item in terminals.KNOWN] == [
        ("system", "xdg-terminal-exec"),
        ("gnome-terminal", "gnome-terminal --"),
        ("ptyxis", "ptyxis --new-window --"),
        ("kgx", "kgx --"),
        ("kitty", "kitty"),
        ("alacritty", "alacritty -e"),
        ("foot", "foot"),
        ("wezterm", "wezterm start --"),
        ("ghostty", "ghostty -e"),
        ("konsole", "konsole -e"),
        ("xfce4-terminal", "xfce4-terminal -x"),
        ("xterm", "xterm -e"),
    ]
    assert terminals.KNOWN[0].label == "System default"


def test_found_lists_system_first_then_terminals_on_path(tmp_path: Path) -> None:
    _stub(tmp_path, "kitty")
    _stub(tmp_path, "gnome-terminal")
    found = terminals.found({"PATH": str(tmp_path)})
    assert [(item.id, item.available) for item in found] == [
        ("system", False),
        ("gnome-terminal", True),
        ("kitty", True),
    ]
    _stub(tmp_path, "xdg-terminal-exec")
    found = terminals.found({"PATH": str(tmp_path)})
    assert found[0].id == "system"
    assert found[0].available


def test_found_with_nothing_on_path(tmp_path: Path) -> None:
    found = terminals.found({"PATH": str(tmp_path)})
    assert [(item.id, item.available) for item in found] == [("system", False)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("xdg-terminal-exec", "system"),
        ("gnome-terminal  --", "gnome-terminal"),
        ("'ptyxis' --new-window --", "ptyxis"),
        ("kitty", "kitty"),
        ("alacritty -e", "alacritty"),
        ("alacritty", None),
        ("my-term --run", None),
        ("", None),
        ("'unclosed", None),
    ],
)
def test_match(value: str, expected: str | None, tmp_path: Path) -> None:
    found = terminals.match(value, {"PATH": str(tmp_path)})
    assert (None if found is None else found.id) == expected


def test_match_reports_whether_the_terminal_is_installed(tmp_path: Path) -> None:
    missing = terminals.match("kitty", {"PATH": str(tmp_path)})
    assert missing is not None
    assert missing.id == "kitty"
    assert not missing.available
    _stub(tmp_path, "kitty")
    installed = terminals.match("kitty", {"PATH": str(tmp_path)})
    assert installed is not None
    assert installed.available


@pytest.mark.parametrize("value", ["", "   ", "'unclosed"])
def test_command_argv_refuses_an_unusable_terminal(value: str) -> None:
    with pytest.raises(ValueError):
        terminals.command_argv(value, "true")


def test_command_argv_shape() -> None:
    argv = terminals.command_argv("gnome-terminal --", "codex update")
    assert argv == [
        "gnome-terminal",
        "--",
        "sh",
        "-c",
        terminals.WRAP,
        "ember-command",
        "codex update",
    ]


def test_command_runs_intact_and_waits_for_enter(tmp_path: Path) -> None:
    # The stub terminal runs its arguments, as `xterm -e` would.
    _stub(tmp_path, "term", 'exec "$@"\n')
    out = tmp_path / "out.txt"
    command = (
        "printf '%s\\n' \"it's \\\"quoted\\\"\" '$HOME stays' "
        f"| tr a-z A-Z > {out}; exit 3"
    )
    argv = terminals.command_argv(str(tmp_path / "term"), command)
    assert argv[-1] == command
    result = subprocess.run(
        argv,
        input="\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert out.read_text() == 'IT\'S "QUOTED"\n$HOME STAYS\n'
    assert result.stdout == "\nExit code 3. Press Enter to close.\n"
