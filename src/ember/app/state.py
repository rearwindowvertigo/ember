"""The window's size and last screen, kept between starts.

The file is `$XDG_STATE_HOME/ember/app.json`, mode 600 in a mode 700 folder:
`{"width", "height", "screen"}`. Content that cannot be read or is not valid
gives the defaults; a write that fails is ignored.
"""

import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ember import paths

FILE_NAME = "app.json"
"""The state file's name in the state folder."""

DEFAULT_WIDTH = 1100
"""The window's width before anything is saved."""

DEFAULT_HEIGHT = 720
"""The window's height before anything is saved."""

DEFAULT_SCREEN = "overview"
"""The screen shown before anything is saved."""


@dataclass(frozen=True)
class AppState:
    """What the window keeps between starts."""

    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    screen: str = DEFAULT_SCREEN

    def to_json(self) -> dict[str, object]:
        """Return the state as a JSON-ready dict."""
        return {"width": self.width, "height": self.height, "screen": self.screen}


def state_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the state file's path.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `app.json` in Ember's state folder.
    """
    return paths.state_dir(env) / FILE_NAME


def _size(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 100_000:
        return value
    return default


def load(env: Mapping[str, str] | None = None) -> AppState:
    """Read the saved state.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        The saved values; each one missing or not valid takes its default,
        and a file that cannot be read gives every default.
    """
    try:
        value = json.loads(state_path(env).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return AppState()
    if not isinstance(value, dict):
        return AppState()
    screen = value.get("screen")
    return AppState(
        width=_size(value.get("width"), DEFAULT_WIDTH),
        height=_size(value.get("height"), DEFAULT_HEIGHT),
        screen=screen if isinstance(screen, str) and screen else DEFAULT_SCREEN,
    )


def save(state: AppState, env: Mapping[str, str] | None = None) -> None:
    """Write the state, replacing the file whole; errors are ignored.

    Args:
        state: The state to keep.
        env: The environment; `None` reads `os.environ`.
    """
    with contextlib.suppress(OSError, ValueError):
        folder = paths.state_dir(env)
        paths.ensure_private_dir(folder, root=folder)
        target = folder / FILE_NAME
        temporary = folder / f".{FILE_NAME}.tmp"
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        fd = paths.open_private_file(temporary, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state.to_json(), stream)
        os.replace(temporary, target)
