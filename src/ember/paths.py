"""Where Ember keeps its files, and how it creates them privately.

Locations follow the XDG Base Directory Specification, except run temporary
folders in `/tmp`. Ember's folders are mode 700 and its private files mode 600,
whatever the umask.

Every function that reads the environment takes an optional `env` mapping,
which defaults to `os.environ`.
"""

import contextlib
import fcntl
import hashlib
import os
import pwd
import re
import shutil
import stat
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

APP_NAME = "ember"
"""The name of Ember's folder inside each base directory."""

DIR_MODE = 0o700
"""The mode of every folder Ember creates."""

FILE_MODE = 0o600
"""The mode of every private file Ember creates."""

RUNTIME_FALLBACK_ROOT = Path("/run/user")
"""The folder holding `<uid>/` when `XDG_RUNTIME_DIR` is not usable."""


RUN_TMP_ROOT = Path("/tmp")
"""The base for short, private Codex run temporary folders."""

RUN_TMP_PATTERN = re.compile(r"ember-[0-9a-f]{6}\Z")
"""The exact names reserved for Codex run temporary folders."""


WORKTREES_NAME = "worktrees"
"""The folder in Ember's data folder that holds run and flow worktrees."""

WORKER_CACHE_NAME = "worker"
"""The folder in Ember's cache folder that sandboxed workers write caches to."""


class RuntimeDirError(RuntimeError):
    """No runtime base directory is available."""


class WorktreeDirError(ValueError):
    """The worktree folder is inside a Git working tree or repository folder."""


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


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Ember's data folder.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `$XDG_DATA_HOME/ember`, with `~/.local/share` as the default base.
    """
    return _base_or_default("XDG_DATA_HOME", (".local", "share"), env)


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Ember's cache folder.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `$XDG_CACHE_HOME/ember`, with `~/.cache` as the default base.
    """
    return _base_or_default("XDG_CACHE_HOME", (".cache",), env)


def worker_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the cache folder of sandboxed workers.

    Every sandboxed worker gets this folder writable and `XDG_CACHE_HOME` set
    to it, so the tools it runs can keep their caches.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `worker/` in Ember's cache folder.
    """
    return cache_dir(env) / WORKER_CACHE_NAME


def ensure_worker_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Create the cache folder of sandboxed workers and its parents at mode 700.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        The folder, from `worker_cache_dir`.

    Raises:
        NotADirectoryError: An entry from Ember's cache folder down is a
            symlink or not a folder.
        OSError: A folder cannot be created.
    """
    return ensure_private_dir(worker_cache_dir(env), root=cache_dir(env))


def run_tmp_dir(
    run_id: str,
    env: Mapping[str, str] | None = None,
    *,
    base: Path | None = None,
) -> Path:
    """Return `/tmp/ember-<6 hex>` without resolving its final entry.

    Args:
        run_id: A run id ending in its six hexadecimal digits.
        env: The environment; the temporary base does not depend on it.
        base: The temporary base; `None` uses `/tmp`.

    Raises:
        ValueError: The id contains `/` or NUL, or has no six-hex suffix.
    """
    suffix = run_id.rsplit("-", 1)[-1]
    if "/" in run_id or "\0" in run_id or not re.fullmatch(r"[0-9a-f]{6}", suffix):
        raise ValueError(f"{run_id!r} is not a run id")
    return (RUN_TMP_ROOT if base is None else base) / f"ember-{suffix}"


def ensure_run_tmp_dir(
    run_id: str,
    env: Mapping[str, str] | None = None,
    *,
    base: Path | None = None,
) -> Path:
    """Create a new run temporary folder at mode 700, refusing existing entries.

    The base must exist. No existing entry is followed or modified.

    Raises:
        FileExistsError: An entry, including a symlink, already exists.
        OSError: The folder cannot be created.
        ValueError: The run id is invalid.
    """
    folder = run_tmp_dir(run_id, env, base=base)
    fd = os.open(folder.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.mkdir(folder.name, mode=DIR_MODE, dir_fd=fd)
        os.chmod(folder.name, DIR_MODE, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    return folder


def remove_run_tmp_dir(
    run_id: str,
    env: Mapping[str, str] | None = None,
    *,
    base: Path | None = None,
) -> bool:
    """Remove a run's temporary entry without following symlinks out of it.

    Return whether an entry was removed. Missing folders need no removal.
    The parent is opened without following a symlink, and removal is relative
    to that descriptor. A symlink at the run's name is unlinked itself.

    Raises:
        OSError: An entry cannot be removed or its parent is a symlink.
        ValueError: The run id is invalid.
    """
    folder = run_tmp_dir(run_id, env, base=base)
    try:
        fd = os.open(folder.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            info = os.stat(folder.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(folder.name, dir_fd=fd)
            else:
                os.unlink(folder.name, dir_fd=fd)
        except FileNotFoundError:
            return False
    finally:
        os.close(fd)
    return True


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
    """Return the folder of run, flow and panel records.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `runs/` in the state folder.
    """
    return state_dir(env) / "runs"


def worktrees_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the folder of run and flow worktrees.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `worktrees/` in the data folder.
    """
    return data_dir(env) / WORKTREES_NAME


def worktree_path(worktree_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Return the path of a run's or flow's worktree, with symlinks resolved.

    Every worktree Ember creates is at this path.

    Args:
        worktree_id: The run id or flow id.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `worktrees/<worktree_id>` in the data folder, with the symlinks of
        its existing folders resolved.

    Raises:
        ValueError: `worktree_id` is empty, `.` or `..`, or holds a `/` or a
            NUL character.
    """
    if worktree_id in ("", ".", "..") or "/" in worktree_id or "\0" in worktree_id:
        raise ValueError(f"{worktree_id!r} is not a worktree id")
    return worktrees_dir(env).resolve() / worktree_id


def worktree_setting(env: Mapping[str, str] | None = None) -> str:
    """Return the environment variable that places the worktree folder.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `XDG_DATA_HOME` when it is set to an absolute path, otherwise `HOME`.
    """
    return "XDG_DATA_HOME" if _xdg_base("XDG_DATA_HOME", env) else "HOME"


def _is_git_working_tree(folder: Path) -> bool:
    """Return whether `folder` has Git's checkout or linked-worktree metadata."""
    git = folder / ".git"
    try:
        if git.is_file():
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir: "):
                return False
            git = folder / text.removeprefix("gitdir: ")
        if not os.path.lexists(git / "HEAD"):
            return False
        common = git
        if (git / "commondir").is_file():
            common = git / (git / "commondir").read_text(encoding="utf-8").strip()
        return (common / "objects").is_dir() and (common / "refs").is_dir()
    except OSError, ValueError:
        return False


def check_worktree_path(
    path: Path, folders: Iterable[Path], env: Mapping[str, str] | None = None
) -> None:
    """Refuse a worktree path inside any Git working tree or repository folder.

    A tool that searches parent folders for a project file, such as
    `pyproject.toml`, would find that folder's file from the worktree.

    Args:
        path: The worktree path, from `worktree_path`.
        folders: The repository's folders: every checkout `git worktree list`
            lists and the common git folder.
        env: The environment; `None` reads `os.environ`.

    Raises:
        WorktreeDirError: `path` is inside a Git working tree, or is one of
            `folders` or inside one; the message names the worktree folder,
            that folder and the setting that placed the worktree folder there.
    """
    resolved = path.resolve()
    ancestors = [
        folder
        for folder in (resolved.parent, *resolved.parent.parents)
        if _is_git_working_tree(folder)
    ]
    for item in [*folders, *ancestors]:
        folder = Path(item).resolve()
        if resolved.is_relative_to(folder):
            raise WorktreeDirError(
                f"the worktree folder {resolved.parent} is inside the repository "
                f"folder {folder}; {worktree_setting(env)} places it there"
            )


def ensure_worktrees_dir(env: Mapping[str, str] | None = None) -> Path:
    """Create the folder of run and flow worktrees and its parents at mode 700.

    Symlinks in the data and worktrees paths are resolved before checking or
    creating folders. Existing targets must be owned by the current user and
    have mode 700; their permissions are never changed through a symlink.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        The resolved folder, from `worktrees_dir`.

    Raises:
        PermissionError: An existing folder is owned by another user, or a
            symlink target does not have mode 700.
        NotADirectoryError: A target is not a folder.
        OSError: A folder cannot be created.
    """
    folders = [data_dir(env), worktrees_dir(env)]
    resolved = [folder.resolve(strict=folder.is_symlink()) for folder in folders]
    for given, target in zip(folders, resolved, strict=True):
        if not target.exists():
            continue
        info = target.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(f"{target} is not a folder")
        if info.st_uid != os.getuid():
            raise PermissionError(f"{target} is not owned by the current user")
        if given != target and stat.S_IMODE(info.st_mode) != DIR_MODE:
            raise PermissionError(f"{target} must have mode 700")
    for target in resolved:
        ensure_private_dir(target, root=target)
    return resolved[-1]


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


@contextlib.contextmanager
def worktree_lock(common: Path, env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Serialize Git worktree operations for one resolved common git folder.

    Acquire flow or panel locks before run command locks, then run record
    locks, then this lock. Never acquire those locks while holding this one.
    This lock is not reentrant; callers holding it use Git directly.

    Args:
        common: The repository's common git folder.
        env: The command environment; `None` reads `os.environ`.

    Yields:
        Nothing, while worktree add, list, remove or prune is serialized.
    """
    key = hashlib.sha256(os.fsencode(common.resolve())).hexdigest()
    root = runtime_dir(env)
    folder = ensure_private_dir(root / "locks", root=root)
    fd = open_private_file(folder / f"worktrees-{key}.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
