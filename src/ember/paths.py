"""Where Ember keeps its files, and how it creates them privately.

Locations follow the XDG Base Directory Specification. Ember's folders are
mode 700 and its private files mode 600, whatever the umask.

Every function that reads the environment takes an optional `env` mapping,
which defaults to `os.environ`.
"""

import os
import pwd
import stat
from collections.abc import Mapping
from pathlib import Path

APP_NAME = "ember"
"""The name of Ember's folder inside each base directory."""

DIR_MODE = 0o700
"""The mode of every folder Ember creates."""

FILE_MODE = 0o600
"""The mode of every private file Ember creates."""

RUNTIME_FALLBACK_ROOT = Path("/run/user")
"""The folder holding `<uid>/` when `XDG_RUNTIME_DIR` is not usable."""


class RuntimeDirError(RuntimeError):
    """No runtime base directory is available."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def home_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the home folder.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `HOME` from `env` when it is set and not empty, otherwise the home
        folder in the password database for the current user.
    """
    home = _env(env).get("HOME", "")
    return Path(home) if home else Path(pwd.getpwuid(os.getuid()).pw_dir)


def _xdg_base(name: str, env: Mapping[str, str] | None) -> Path | None:
    """Return the XDG variable `name` as a path, or `None` if it is not usable.

    The specification ignores a variable that is unset, empty or not absolute.
    """
    value = _env(env).get(name, "")
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else None


def _base_or_default(
    name: str, default: tuple[str, ...], env: Mapping[str, str] | None
) -> Path:
    base = _xdg_base(name, env)
    if base is None:
        base = home_dir(env).joinpath(*default)
    return base / APP_NAME


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Ember's configuration folder.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `$XDG_CONFIG_HOME/ember`, with `~/.config` as the default base.
    """
    return _base_or_default("XDG_CONFIG_HOME", (".config",), env)


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Ember's state folder.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `$XDG_STATE_HOME/ember`, with `~/.local/state` as the default base.
    """
    return _base_or_default("XDG_STATE_HOME", (".local", "state"), env)


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Ember's cache folder.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `$XDG_CACHE_HOME/ember`, with `~/.cache` as the default base.
    """
    return _base_or_default("XDG_CACHE_HOME", (".cache",), env)


def runtime_dir(
    env: Mapping[str, str] | None = None,
    *,
    fallback_root: Path = RUNTIME_FALLBACK_ROOT,
) -> Path:
    """Return Ember's runtime folder, which holds locks and sockets.

    Args:
        env: The environment; `None` reads `os.environ`.
        fallback_root: The folder holding `<uid>/`, used when
            `XDG_RUNTIME_DIR` is unset, empty or not absolute.

    Returns:
        `$XDG_RUNTIME_DIR/ember`, or `<fallback_root>/<uid>/ember`.

    Raises:
        RuntimeDirError: `XDG_RUNTIME_DIR` is not usable and
            `<fallback_root>/<uid>` does not exist.
    """
    base = _xdg_base("XDG_RUNTIME_DIR", env)
    if base is None:
        base = fallback_root / str(os.getuid())
        if not base.is_dir():
            raise RuntimeDirError(
                "XDG_RUNTIME_DIR is unset, empty or not an absolute path, "
                f"and {base} does not exist"
            )
    return base / APP_NAME


def settings_db_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path of the settings database.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `settings.db` in the configuration folder.
    """
    return config_dir(env) / "settings.db"


def runs_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the folder of run and flow records.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `runs/` in the state folder.
    """
    return state_dir(env) / "runs"


def ensure_private_dir(path: Path, *, root: Path) -> Path:
    """Create an Ember folder and its Ember parents at mode 700.

    Each folder from `root` down to `path` is created at mode 700 whatever the
    umask, or, if it exists with permissions for group or others, changed to
    mode 700. Each missing folder above `root` is created at mode 700, as the
    XDG Base Directory Specification requires; existing folders above `root`
    keep their modes.

    Args:
        path: The folder to create; `root` itself or a folder inside it.
        root: Ember's folder in one base directory, such as `config_dir()`.

    Returns:
        `path`.

    Raises:
        ValueError: `path` is not `root` or inside it, or its part below
            `root` contains `..`.
        NotADirectoryError: An existing entry from `root` down to `path` is a
            symlink or not a folder.
    """
    if not path.is_relative_to(root):
        raise ValueError(f"{path} is not inside {root}")
    relative = path.relative_to(root)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{path} is not inside {root}")
    missing = [folder for folder in root.parents if not folder.exists()]
    for folder in reversed(missing):
        _make_folder(folder)
    folders = [root]
    for part in relative.parts:
        folders.append(folders[-1] / part)
    for folder in folders:
        created = _make_folder(folder)
        info = folder.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise NotADirectoryError(f"{folder} is a symlink, not a folder")
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(f"{folder} is not a folder")
        if not created and info.st_mode & 0o077:
            folder.chmod(DIR_MODE)
    return path


def _make_folder(folder: Path) -> bool:
    """Create `folder` at mode 700 whatever the umask.

    Returns:
        `True` if the folder was created, `False` if an entry already existed.
    """
    try:
        folder.mkdir(mode=DIR_MODE)
    except FileExistsError:
        return False
    folder.chmod(DIR_MODE)
    return True


def open_private_file(path: Path, flags: int = os.O_RDWR) -> int:
    """Create or open a private file at mode 600.

    A new file is created at mode 600, so it is never readable by group or
    others. An existing file's mode is set to 600.

    Args:
        path: The file to open. Its folder must exist.
        flags: `os.open` flags; `O_CREAT`, `O_NOFOLLOW` and `O_CLOEXEC` are
            added.

    Returns:
        An open file descriptor, which the caller closes.
    """
    fd = os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd
