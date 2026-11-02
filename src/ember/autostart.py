"""Starting Ember at login (design E7).

The autostart entry follows the freedesktop.org Desktop Entry and Desktop
Application Autostart specifications. It starts `emb app --hidden`, which
opens Ember in the tray.
"""

import contextlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from ember import paths

APP_ID = "io.github.rearwindowvertigo.Ember"
"""The app ID, which names the desktop entry and the icon."""

FILE_NAME = f"{APP_ID}.desktop"
"""The autostart entry's file name."""

_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def path(env: Mapping[str, str] | None = None) -> Path:
    """Return the autostart entry's path.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `$XDG_CONFIG_HOME/autostart/io.github.rearwindowvertigo.Ember.desktop`,
        with `~/.config` as the default base.
    """
    return paths.config_dir(env).parent / "autostart" / FILE_NAME


def _exec_argument(value: str) -> str:
    r"""Quote one `Exec` argument as the Desktop Entry specification asks.

    An argument with a reserved character is put in double quotes, with `"`,
    `` ` ``, `$` and `\` escaped by a backslash. Then `%` is doubled and, as
    in every string value, each backslash is escaped again.
    """
    if any(char in _RESERVED for char in value):
        escaped = "".join("\\" + c if c in '"`$\\' else c for c in value)
        value = f'"{escaped}"'
    return value.replace("%", "%%").replace("\\", "\\\\")


def entry(emb: str) -> str:
    """Return the autostart entry's content.

    Args:
        emb: The absolute path of the `emb` command.

    Returns:
        The desktop entry text.
    """
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Ember\n"
        f"Exec={_exec_argument(emb)} app --hidden\n"
        f"Icon={APP_ID}\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _make_folders(folder: Path) -> None:
    """Create a folder and its missing parents at mode 700."""
    missing = [folder, *folder.parents]
    for item in reversed([part for part in missing if not part.exists()]):
        with contextlib.suppress(FileExistsError):
            item.mkdir(mode=paths.DIR_MODE)
            item.chmod(paths.DIR_MODE)


def _write(target: Path, text: str) -> None:
    _make_folders(target.parent)
    temporary = target.with_name(f".{target.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    fd = paths.open_private_file(temporary, os.O_WRONLY | os.O_TRUNC)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    os.replace(temporary, target)


def sync(on: bool, env: Mapping[str, str] | None = None) -> str | None:
    """Write or remove the autostart entry to match the setting.

    On, the entry is written when its content differs from the wanted one.
    Off, it is removed when present. Nothing raises.

    Args:
        on: The `app.autostart` setting is on.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `None` when the entry matches the setting, otherwise why it does
        not.
    """
    environment = os.environ if env is None else env
    try:
        target = path(environment)
    except OSError as error:
        return f"cannot find the autostart folder: {error}"
    if not on:
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            return f"cannot remove {target}: {error.strerror or error}"
        return None
    emb = shutil.which("emb", path=environment.get("PATH", ""))
    if emb is None:
        return (
            "emb is not on PATH, so Ember cannot start at login. "
            "Install Ember, then turn Start at login off and on again."
        )
    text = entry(str(Path(emb).absolute()))
    try:
        if target.read_text(encoding="utf-8") == text:
            return None
    except OSError, UnicodeDecodeError:
        pass
    try:
        _write(target, text)
    except OSError as error:
        return f"cannot write {target}: {error.strerror or error}"
    return None
