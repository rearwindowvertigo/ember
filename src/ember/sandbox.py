"""Worker sandboxes: the provider per engine, the deny list and its checks.

A sandboxed worker sees the machine read-only except its worktree, and does
not see the home folder apart from the paths it needs: the worktree, the
repository's common git folder, `~/.local/bin`, `~/.local/lib`, git's
configuration, the engine's own folders and the run's read paths. The whole
of `$XDG_RUNTIME_DIR` is hidden, and so are Ember's configuration and state
folders wherever they are.

Each engine has one switch, `sandbox.<engine>`, `on` or `off`. Codex's
sandbox is its own: it receives the deny list as a permission profile named
`ember` in `-c` options and applies it with a `bwrap`, the system one when it
is on `PATH`, else the one bundled with Codex. Ember runs OpenCode, and each
Claude Code turn, inside its own `bwrap` command line with the same list.

Every function that reads the environment takes an optional `env` mapping,
which defaults to `os.environ`.
"""

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ember import choices, db, paths

ENGINES = ("codex", "opencode", "claude")
"""The engines with a sandbox switch."""

CODEX = "codex"
BUBBLEWRAP = "bubblewrap"
NONE = "none"
PROVIDERS = (CODEX, BUBBLEWRAP, NONE)
"""Who provides a run's sandbox: Codex's own, Ember's `bwrap`, or nobody."""

PROFILE = "ember"
"""The name of the Codex permission profile Ember defines."""

TIMEOUT = 10
"""Seconds each command run by the sandbox check may take."""

PROBE_COMMAND = "/bin/true"
"""The command the sandbox check runs inside the sandbox."""

SYSTEM_CODEX_CONFIG = Path("/etc/codex/config.toml")
"""Codex's system-wide configuration file."""

APPARMOR_DIR = Path("/etc/apparmor.d")
"""The folder holding AppArmor profiles."""

APPARMOR_PROFILE = "bwrap-userns-restrict"
"""Ubuntu's AppArmor profile that restricts `bwrap` inside `bwrap`."""

DOCKER_SOCKETS = ("/run/docker.sock", "/var/run/docker.sock")
"""System Docker sockets, hidden from OpenCode workers."""

_NPM_ARCH = {"x86_64": "x64", "aarch64": "arm64"}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class SandboxError(RuntimeError):
    """A sandbox cannot be set up."""


@dataclass(frozen=True)
class SandboxCheck:
    """The result of running a command under an engine's sandbox.

    Attributes:
        engine: `codex`, `opencode` or `claude`.
        provider: Who provides the sandbox, one of `PROVIDERS`, or `None`
            when the setting cannot be read.
        bwrap_path: The `bwrap` that ran or would run, or `None`.
        bwrap_version: That `bwrap`'s version, or `None`.
        ok: The command ran in the sandbox and nothing blocks a run.
        detail: One line describing the result.
        causes: Known causes of a failure or a refused run.
        checked_at: When the check ran, in seconds since the epoch.
    """

    engine: str
    provider: str | None
    bwrap_path: str | None
    bwrap_version: str | None
    ok: bool
    detail: str
    causes: tuple[str, ...]
    checked_at: float

    def to_json(self) -> dict[str, Any]:
        """Return the check as a JSON-ready dict."""
        return {
            "engine": self.engine,
            "provider": self.provider,
            "bwrap_path": self.bwrap_path,
            "bwrap_version": self.bwrap_version,
            "ok": self.ok,
            "detail": self.detail,
            "causes": list(self.causes),
            "checked_at": self.checked_at,
        }


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _check_engine(engine: str) -> None:
    if engine not in ENGINES:
        raise ValueError(
            f"{engine!r} has no sandbox; expected codex, opencode or claude"
        )


def provider_of(engine: str, switch: str) -> str:
    """Return who provides an engine's sandbox for a switch value.

    Args:
        engine: `codex`, `opencode` or `claude`.
        switch: The `sandbox.<engine>` value, `on` or `off`.

    Returns:
        `codex` for Codex switched on, `bubblewrap` for OpenCode or Claude
        switched on, and `none` for `off`.

    Raises:
        ValueError: `engine` has no sandbox.
    """
    _check_engine(engine)
    if switch != "on":
        return NONE
    return CODEX if engine == "codex" else BUBBLEWRAP


def method(engine: str, env: Mapping[str, str] | None = None) -> str:
    """Return who provides an engine's sandbox.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `codex`, `bubblewrap` or `none`, from the resolved `sandbox.<engine>`
        answer, which is the default when the setting is unset.

    Raises:
        ValueError: `engine` has no sandbox.
        DatabaseError: The settings database cannot be read.
    """
    _check_engine(engine)
    switch = choices.read(env=env)["answers"][f"sandbox.{engine}"]["value"]
    return provider_of(engine, switch)


def _git_config_dir(home: Path, env: Mapping[str, str]) -> Path:
    value = env.get("XDG_CONFIG_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else home / ".config"
    return base / "git"


def kept_paths(
    home: Path,
    worktree: Path,
    common: Path,
    read: Iterable[str | Path] = (),
    *,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    """Return the paths a sandboxed worker keeps.

    Each path is listed as given and as resolved, so a symlink keeps its
    target without keeping the target's siblings.

    Args:
        home: The home folder.
        worktree: The worker's worktree.
        common: The repository's common git folder.
        read: The run's read paths.
        env: The environment, for `XDG_CONFIG_HOME`; `None` reads
            `os.environ`.

    Returns:
        The kept paths, sorted.
    """
    env = _env(env)
    entries = [
        home / ".local" / "bin",
        home / ".local" / "lib",
        home / ".gitconfig",
        _git_config_dir(home, env),
        Path(worktree),
        Path(common),
        *(Path(path) for path in read),
    ]
    return sorted(
        {path for entry in entries for path in (entry, entry.resolve())}, key=str
    )


def _related(path: Path, kept: Sequence[Path]) -> bool:
    """Return whether `path` is kept, inside a kept path or an ancestor of one."""
    return any(
        path.is_relative_to(other) or other.is_relative_to(path) for other in kept
    )


def deny_list(
    home: Path,
    kept: Sequence[Path],
    runtime: Path,
    *,
    protected: Iterable[Path] = (),
) -> list[Path]:
    """Return the paths a sandboxed worker is denied.

    The home folder is walked only along ancestors of kept paths; every other
    entry is denied by its own name. A symlink whose target is kept, inside a
    kept path or an ancestor of one is not denied, because Codex's sandbox
    follows a denied symlink and hides its target; a target that is an
    ancestor is walked like any other folder. No two entries resolve to the
    same path or one inside another, because the sandbox cannot create a
    mount point inside a path it has already hidden.

    Args:
        home: The home folder.
        kept: The kept paths, from `kept_paths`.
        runtime: The runtime base folder, `$XDG_RUNTIME_DIR`, denied whole.
        protected: Paths denied even when a kept path holds them, such as
            Ember's configuration and state folders. Missing ones are left
            out.

    Returns:
        The denied paths, sorted.
    """
    denied: list[Path] = []
    pending = [Path(home)]
    walked: set[Path] = set()
    while pending:
        folder = pending.pop()
        if folder in walked or any(folder.is_relative_to(path) for path in kept):
            continue
        walked.add(folder)
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if any(entry.is_relative_to(path) for path in kept):
                continue
            if entry.is_symlink():
                target = entry.resolve()
                if _related(target, kept):
                    if not any(target.is_relative_to(path) for path in kept):
                        pending.append(target)
                    continue
            if any(path.is_relative_to(entry) for path in kept):
                pending.append(entry)
            else:
                denied.append(entry)
    denied += [Path(path) for path in protected if os.path.lexists(path)]
    # A plain entry is kept over a symlink that resolves to the same path.
    entries = sorted(
        set([*denied, Path(runtime)]),
        key=lambda path: (path.is_symlink(), str(path)),
    )
    by_target: dict[Path, Path] = {}
    for entry in entries:
        target = entry.resolve()
        if any(target.is_relative_to(other) for other in by_target):
            continue
        by_target = {
            other: name
            for other, name in by_target.items()
            if not other.is_relative_to(target)
        }
        by_target[target] = entry
    return sorted(by_target.values(), key=str)


def _protected(env: Mapping[str, str]) -> list[Path]:
    """Return Ember's own folders, which no worker may read."""
    return [paths.config_dir(env), paths.state_dir(env)]


def _runtime_base(env: Mapping[str, str]) -> Path:
    return paths.runtime_dir(env).parent


def codex_args(
    worktree: Path,
    common: Path,
    read: Iterable[str | Path] = (),
    *,
    read_only: bool = False,
    network: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the Codex `-c` options that define and select Ember's profile.

    The profile extends Codex's `:workspace` profile, or `:read-only` for a
    read-only run, so Codex's own protection of git folders stays in place.

    Args:
        worktree: The worker's worktree, writable unless `read_only`.
        common: The repository's common git folder, read-only.
        read: The run's read paths.
        read_only: The worktree is read-only.
        network: The worker has network.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The options, each `-c` followed by its value.

    Raises:
        RuntimeDirError: No runtime base directory is available.
    """
    env = _env(env)
    home = paths.home_dir(env)
    kept = kept_paths(home, worktree, common, read, env=env)
    denied = deny_list(home, kept, _runtime_base(env), protected=_protected(env))
    filesystem = {str(path): "deny" for path in denied}
    filesystem[str(worktree)] = "read" if read_only else "write"
    filesystem[str(common)] = "read"
    filesystem[str(Path(worktree) / ".git")] = "read"
    # TOML takes Unicode scalar values, not JSON's surrogate pairs.
    entries = ", ".join(
        f"{json.dumps(path, ensure_ascii=False)} = {json.dumps(access)}"
        for path, access in filesystem.items()
    )
    extends = ":read-only" if read_only else ":workspace"
    return [
        "-c",
        f"default_permissions={json.dumps(PROFILE)}",
        "-c",
        f"permissions.{PROFILE}.extends={json.dumps(extends)}",
        "-c",
        f"permissions.{PROFILE}.filesystem={{{entries}}}",
        "-c",
        f"permissions.{PROFILE}.network.enabled={str(network).lower()}",
    ]


def codex_config_conflicts(
    worktree: Path, common: Path, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Return Codex configuration files that would override Ember's profile.

    A `sandbox_mode` key makes Codex ignore the permission profile. A file
    that exists but cannot be read or parsed is listed too, because it could
    hold the key.

    Args:
        worktree: The worker's worktree.
        common: The repository's common git folder.
        env: The environment, for `CODEX_HOME` and `HOME`; `None` reads
            `os.environ`.

    Returns:
        The conflicting files, in the order Codex reads them.
    """
    env = _env(env)
    codex_home = env.get("CODEX_HOME", "")
    folder = Path(codex_home) if codex_home else paths.home_dir(env) / ".codex"
    candidates = [
        SYSTEM_CODEX_CONFIG,
        folder / "config.toml",
        Path(common).parent / ".codex" / "config.toml",
        Path(worktree) / ".codex" / "config.toml",
    ]
    found = []
    for path in dict.fromkeys(candidates):
        try:
            if "sandbox_mode" in tomllib.loads(path.read_text(encoding="utf-8")):
                found.append(path)
        except FileNotFoundError:
            continue
        except OSError, ValueError:
            found.append(path)
    return found


def codex_bundled_bwrap(codex_binary: Path) -> Path | None:
    """Return the `bwrap` bundled with a Codex install, if there is one.

    The npm package `@openai/codex` holds the launcher `bin/codex.js` and a
    platform package `@openai/codex-linux-<arch>`, either inside its own
    `node_modules` or beside it, whose `vendor/<triple>/codex-resources/`
    holds `bwrap`. The native binary is at `vendor/<triple>/codex/codex`.

    Args:
        codex_binary: The `codex` command, such as `~/.local/bin/codex`;
            symlinks are followed.

    Returns:
        The bundled `bwrap`, or `None`.
    """
    resolved = Path(codex_binary).resolve()
    candidates = [resolved.parent.parent / "codex-resources" / "bwrap"]
    machine = platform.machine()
    arch = _NPM_ARCH.get(machine)
    if arch is not None:
        package = resolved.parent.parent
        for scope in (package / "node_modules" / "@openai", package.parent):
            vendor = scope / f"codex-linux-{arch}" / "vendor"
            candidates += sorted(
                vendor.glob(f"{machine}-*/codex-resources/bwrap"), key=str
            )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def codex_bundled_path(
    codex_binary: Path, env: Mapping[str, str] | None = None
) -> Path:
    """Create the folder that makes Codex use its bundled `bwrap`.

    The folder is `bin/` in Ember's runtime folder, at mode 700. It holds a
    `bwrap` symlink to the bundled one; a stale link is replaced atomically.
    The caller puts the folder first on the worker's `PATH`.

    Args:
        codex_binary: The `codex` command.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The folder.

    Raises:
        SandboxError: The Codex install has no bundled `bwrap`, or the link
            cannot be made.
        RuntimeDirError: No runtime base directory is available.
    """
    bundled = codex_bundled_bwrap(codex_binary)
    if bundled is None:
        raise SandboxError(f"the Codex install at {codex_binary} has no bundled bwrap")
    root = paths.runtime_dir(env)
    folder = paths.ensure_private_dir(root / "bin", root=root)
    link = folder / "bwrap"
    try:
        if link.is_symlink() and Path(os.readlink(link)) == bundled:
            return folder
        temporary = folder / f".bwrap-{os.getpid()}-{time.monotonic_ns()}"
        temporary.symlink_to(bundled)
        try:
            temporary.replace(link)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except OSError as error:
        raise SandboxError(f"cannot link {link} to {bundled}: {error}") from error
    return folder


def _xdg(home: Path, env: Mapping[str, str], variable: str, *default: str) -> Path:
    value = env.get(variable, "")
    return (
        Path(value) if value and Path(value).is_absolute() else home.joinpath(*default)
    )


def _opencode_folders(home: Path, env: Mapping[str, str]) -> list[tuple[Path, bool]]:
    """Return OpenCode's folders, each with whether it is writable.

    Its data, cache and state folders are writable and its configuration
    folder is read-only.
    """
    return [
        (_xdg(home, env, "XDG_DATA_HOME", ".local", "share") / "opencode", True),
        (_xdg(home, env, "XDG_CACHE_HOME", ".cache") / "opencode", True),
        (_xdg(home, env, "XDG_STATE_HOME", ".local", "state") / "opencode", True),
        (_xdg(home, env, "XDG_CONFIG_HOME", ".config") / "opencode", False),
    ]


def claude_folders(home: Path, env: Mapping[str, str]) -> list[Path]:
    """Return the folders and files Claude Code writes in the home folder.

    They are its configuration folder (`$CLAUDE_CONFIG_DIR`, or
    `~/.claude`), `~/.claude.json` and its backup, and its cache folders
    `claude` and `claude-cli-nodejs` in `$XDG_CACHE_HOME`.

    Args:
        home: The home folder.
        env: The environment, for `CLAUDE_CONFIG_DIR` and `XDG_CACHE_HOME`.

    Returns:
        The paths, whether or not they exist.
    """
    value = env.get("CLAUDE_CONFIG_DIR", "")
    config = Path(value) if value and Path(value).is_absolute() else home / ".claude"
    cache = _xdg(home, env, "XDG_CACHE_HOME", ".cache")
    return [
        config,
        home / ".claude.json",
        home / ".claude.json.backup",
        cache / "claude",
        cache / "claude-cli-nodejs",
    ]


def _wrap(
    argv: Sequence[str],
    *,
    worktree: Path,
    common: Path,
    read: Iterable[str | Path],
    read_only: bool,
    binary: Path | None,
    folders: Sequence[tuple[Path, bool]],
    env: Mapping[str, str],
) -> list[str]:
    """Return a `bwrap` command line with Ember's deny list.

    Args:
        argv: The command to run.
        worktree: The worker's worktree, also the working folder.
        common: The repository's common git folder, read-only.
        read: The run's read paths.
        read_only: The worktree is read-only.
        binary: The engine's binary; its folder is kept read-only when it is
            in the home folder.
        folders: The engine's own folders and files, each with whether it is
            writable; missing ones are left out.
        env: The environment.

    Returns:
        The command line.
    """
    home = paths.home_dir(env)
    args = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", str(home),
        "--tmpfs", str(_runtime_base(env)),
    ]  # fmt: skip
    if binary is not None:
        folder = Path(binary).resolve().parent
        if folder.is_relative_to(home) and folder.exists():
            args += ["--ro-bind", str(folder), str(folder)]
    # A command that reaches the Docker daemon can mount the whole disk.
    for socket in dict.fromkeys(Path(path).resolve() for path in DOCKER_SOCKETS):
        if socket.is_socket():
            args += ["--ro-bind", "/dev/null", str(socket)]
    kept = kept_paths(home, worktree, common, read, env=env)
    for path in sorted(kept, key=lambda path: (len(path.parts), str(path))):
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    for path, writable in folders:
        if path.exists():
            args += ["--bind" if writable else "--ro-bind", str(path), str(path)]
    args += [
        "--ro-bind" if read_only else "--bind", str(worktree), str(worktree),
        "--ro-bind", str(common), str(common),
    ]  # fmt: skip
    dotgit = Path(worktree) / ".git"
    if dotgit.exists():
        args += ["--ro-bind", str(dotgit), str(dotgit)]
    for path in _protected(env):
        if path.is_dir():
            args += ["--tmpfs", str(path)]
        elif path.exists():
            args += ["--ro-bind", "/dev/null", str(path)]
    return [
        *args,
        "--unshare-pid",
        "--die-with-parent",
        "--chdir",
        str(worktree),
        *argv,
    ]


def opencode_wrap(
    argv: Sequence[str],
    *,
    worktree: Path,
    common: Path,
    read: Iterable[str | Path] = (),
    read_only: bool = False,
    binary: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the `bwrap` command line that runs OpenCode in its sandbox.

    The whole machine is mounted read-only. Empty folders are mounted over the
    home folder, the runtime base folder and `/tmp`, then the kept paths are
    mounted back read-only, OpenCode's data, cache and state folders
    writable, its configuration folder read-only, and the worktree writable
    unless `read_only`. Ember's own folders and the system Docker sockets are
    hidden. The worker gets its own PID namespace and keeps network.

    Args:
        argv: The command to run.
        worktree: The worker's worktree, also the working folder.
        common: The repository's common git folder, read-only.
        read: The run's read paths.
        read_only: The worktree is read-only.
        binary: The `opencode` binary; its folder is kept when it is in the
            home folder.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The command line.

    Raises:
        RuntimeDirError: No runtime base directory is available.
    """
    env = _env(env)
    return _wrap(
        argv,
        worktree=worktree,
        common=common,
        read=read,
        read_only=read_only,
        binary=binary,
        folders=_opencode_folders(paths.home_dir(env), env),
        env=env,
    )


def claude_wrap(
    argv: Sequence[str],
    *,
    worktree: Path,
    common: Path,
    read: Iterable[str | Path] = (),
    read_only: bool = False,
    network: bool = True,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the `bwrap` command line that runs one Claude Code turn.

    The sandbox is built as `opencode_wrap` builds OpenCode's, with Claude
    Code's folders and files from `claude_folders` writable and the folder
    of the installed `claude`, `argv[0]` after symlinks, read-only when it is
    in the home folder. The turn keeps network whatever `network` says,
    because Claude Code needs its API; `worker.network` controls only its web
    tools, which the driver leaves out.

    Args:
        argv: The `claude` command to run; `argv[0]` is the `claude`
            executable.
        worktree: The worker's worktree, also the working folder.
        common: The repository's common git folder, read-only.
        read: The run's read paths.
        read_only: The worktree is read-only.
        network: The run's `worker.network` value; the sandbox keeps network
            either way.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The command line.

    Raises:
        RuntimeDirError: No runtime base directory is available.
    """
    del network  # The sandbox keeps network for Claude Code's API.
    env = _env(env)
    home = paths.home_dir(env)
    binary = None
    if argv:
        found = argv[0] if os.path.isabs(argv[0]) else _which(argv[0], env)
        binary = None if found is None else Path(found)
    return _wrap(
        argv,
        worktree=worktree,
        common=common,
        read=read,
        read_only=read_only,
        binary=binary,
        folders=[(path, True) for path in claude_folders(home, env)],
        env=env,
    )


def codex_bwrap(
    codex_binary: Path | None, env: Mapping[str, str] | None = None
) -> tuple[str, Path | None]:
    """Return the `bwrap` a sandboxed Codex uses, and how to select it.

    The system `bwrap` is used when it is on `PATH`; otherwise the one
    bundled with Codex, through the folder `codex_bundled_path` makes.

    Args:
        codex_binary: The `codex` command, or `None` when it is not found.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The `bwrap`, and the folder to put first on the worker's `PATH`, or
        `None` for the system `bwrap`.

    Raises:
        SandboxError: No `bwrap` is on `PATH` and Codex has no bundled one.
        RuntimeDirError: No runtime base directory is available.
    """
    env = _env(env)
    system = _which("bwrap", env)
    if system is not None:
        return system, None
    if codex_binary is None:
        raise SandboxError("bwrap is not on PATH and codex is not installed")
    folder = codex_bundled_path(codex_binary, env)
    return str(folder / "bwrap"), folder


def probe_bwrap(
    bwrap: str, env: Mapping[str, str] | None = None, *, run: Runner = subprocess.run
) -> str | None:
    """Run `/bin/true` in a minimal `bwrap` sandbox.

    Args:
        bwrap: The `bwrap` to run.
        env: The environment; `None` reads `os.environ`.
        run: Runs a command; `subprocess.run` or a stand-in with its
            signature.

    Returns:
        `None` when it ran; otherwise why it did not.
    """
    argv = [
        bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--unshare-pid", "--die-with-parent", PROBE_COMMAND,
    ]  # fmt: skip
    try:
        completed = run(
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=dict(_env(env)),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"it timed out after {TIMEOUT} seconds"
    except OSError as error:
        return f"it could not start: {error}"
    if completed.returncode:
        reason = _first_line(completed.stderr) or _first_line(completed.stdout)
        return f"it exited {completed.returncode}" + (f": {reason}" if reason else "")
    return None


def require(
    engine: str,
    env: Mapping[str, str] | None = None,
    *,
    run: Runner = subprocess.run,
) -> Path | None:
    """Check that an engine's sandbox can start, before a sandboxed run.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.
        run: Runs a command; `subprocess.run` or a stand-in with its
            signature.

    Returns:
        For Codex with no `bwrap` on `PATH`, the folder that selects its
        bundled `bwrap`; otherwise `None`.

    Raises:
        SandboxError: No `bwrap` is found, or it cannot start a sandbox.
        RuntimeDirError: No runtime base directory is available.
        ValueError: `engine` has no sandbox.
    """
    _check_engine(engine)
    env = _env(env)
    folder = None
    if engine == "codex":
        codex = _which("codex", env)
        bwrap, folder = codex_bwrap(None if codex is None else Path(codex), env)
    else:
        found = _which("bwrap", env)
        if found is None:
            raise SandboxError("bwrap is not installed or not on PATH")
        bwrap = found
    reason = probe_bwrap(bwrap, env, run=run)
    if reason is not None:
        raise SandboxError(f"{bwrap} cannot start a sandbox: {reason}")
    return folder


def none_warning(engine: str) -> str:
    """Return the line a run without a sandbox prints.

    Args:
        engine: The engine's name.

    Returns:
        The `WARNING:` line, without a newline.
    """
    return (
        f"WARNING: {engine} runs without a sandbox; "
        "it can read every file you can read."
    )


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _bwrap_version(
    bwrap: str | None, env: Mapping[str, str], run: Runner
) -> str | None:
    if bwrap is None:
        return None
    try:
        result = run(
            [bwrap, "--version"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=dict(env),
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.SubprocessError:
        return None
    line = _first_line(result.stdout)
    if result.returncode or not line:
        return None
    return line.removeprefix("bubblewrap ").strip()


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH", os.defpath))


@dataclass
class _Probe:
    """What the sandbox check runs and what it found before running."""

    argv: list[str] | None = None
    run_env: dict[str, str] | None = None
    bwrap: str | None = None
    causes: tuple[str, ...] = ()
    detail: str = ""


def _codex_probe(
    worktree: Path,
    common: Path,
    answers: Mapping[str, Any],
    env: Mapping[str, str],
) -> _Probe:
    probe = _Probe(run_env=dict(env))
    codex = _which("codex", env)
    if codex is None:
        probe.detail = "codex is not installed or not on PATH"
        return probe
    conflicts = codex_config_conflicts(worktree, common, env)
    probe.causes = tuple(
        f"{path} sets sandbox_mode or cannot be read; Codex would ignore "
        "the permission profile"
        for path in conflicts
    )
    try:
        probe.bwrap, folder = codex_bwrap(Path(codex), env)
    except (SandboxError, OSError, paths.RuntimeDirError) as error:
        probe.detail = str(error)
        return probe
    if folder is not None:
        probe.run_env = {
            **env,
            "PATH": os.pathsep.join([str(folder), env.get("PATH", "")]),
        }
    probe.argv = [
        codex,
        "sandbox",
        *codex_args(
            worktree,
            common,
            answers["sandbox.read"]["value"],
            network=answers["worker.network"]["value"] == "on",
            env=env,
        ),
        PROBE_COMMAND,
    ]
    return probe


def _wrap_probe(
    engine: str,
    worktree: Path,
    common: Path,
    answers: Mapping[str, Any],
    env: Mapping[str, str],
) -> _Probe:
    probe = _Probe(run_env=dict(env), bwrap=_which("bwrap", env))
    if probe.bwrap is None:
        probe.detail = "bwrap is not installed or not on PATH"
        return probe
    read = answers["sandbox.read"]["value"]
    if engine == "opencode":
        opencode = _which("opencode", env)
        argv = opencode_wrap(
            [PROBE_COMMAND],
            worktree=worktree,
            common=common,
            read=read,
            binary=None if opencode is None else Path(opencode),
            env=env,
        )
    else:
        argv = claude_wrap(
            [PROBE_COMMAND], worktree=worktree, common=common, read=read, env=env
        )
    probe.argv = [probe.bwrap, *argv[1:]]
    return probe


def _failure_causes(engine: str, env: Mapping[str, str]) -> list[str]:
    causes = []
    if (APPARMOR_DIR / APPARMOR_PROFILE).exists():
        causes.append(
            f"Ubuntu's {APPARMOR_PROFILE} AppArmor profile is present in {APPARMOR_DIR}"
        )
    codex = _which("codex", env) if engine == "codex" else None
    if codex is not None:
        home = paths.home_dir(env)
        resolved = Path(codex).resolve()
        kept = kept_paths(home, Path("/nonexistent"), Path("/nonexistent"), env=env)
        if resolved.is_relative_to(home) and not any(
            resolved.is_relative_to(path) for path in kept
        ):
            causes.append(
                f"Codex is installed at {resolved}, which the sandbox does not keep"
            )
    return causes


def check(
    engine: str,
    env: Mapping[str, str] | None = None,
    *,
    run: Runner = subprocess.run,
) -> SandboxCheck:
    """Run a command under an engine's sandbox and report the result.

    The check is generic to the engine: it builds a sandbox for a temporary
    folder laid out as a repository, with the standing `sandbox.read` and
    `worker.network` settings, and runs `/bin/true` in it. Codex runs it with
    `codex sandbox` and Ember's profile; the checks of OpenCode and Claude
    run their `bwrap` command lines. A `sandbox_mode` key in a Codex
    configuration file fails the check, because Ember refuses a sandboxed
    Codex run while it is there.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.
        run: Runs a command; `subprocess.run` or a stand-in with its
            signature.

    Returns:
        The result. Failures are reported in it, not raised.

    Raises:
        ValueError: `engine` has no sandbox.
    """
    _check_engine(engine)
    env = _env(env)
    started = time.time()

    def result(
        chosen: str | None,
        bwrap: str | None,
        ok: bool,
        detail: str,
        causes: Iterable[str] = (),
    ) -> SandboxCheck:
        return SandboxCheck(
            engine=engine,
            provider=chosen,
            bwrap_path=bwrap,
            bwrap_version=_bwrap_version(bwrap, env, run),
            ok=ok,
            detail=detail,
            causes=tuple(dict.fromkeys(causes)),
            checked_at=started,
        )

    try:
        answers = choices.read(env=env)["answers"]
    except db.DatabaseError as error:
        return result(None, None, False, f"cannot read the settings: {error}")
    chosen = provider_of(engine, answers[f"sandbox.{engine}"]["value"])
    if chosen == NONE:
        return result(chosen, _which("bwrap", env), True, "no sandbox")
    tmp = env.get("TMPDIR", "")
    try:
        with tempfile.TemporaryDirectory(
            prefix="ember-sandbox-check-", dir=tmp or None
        ) as folder:
            worktree = Path(folder).resolve() / "repo"
            common = worktree / ".git"
            common.mkdir(parents=True)
            if engine == "codex":
                probe = _codex_probe(worktree, common, answers, env)
            else:
                probe = _wrap_probe(engine, worktree, common, answers, env)
            if probe.argv is None:
                return result(chosen, probe.bwrap, False, probe.detail, probe.causes)
            try:
                completed = run(
                    probe.argv,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT,
                    env=probe.run_env,
                    cwd=worktree,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                detail = f"the sandbox test timed out after {TIMEOUT} seconds"
                return result(chosen, probe.bwrap, False, detail, probe.causes)
            except OSError as error:
                detail = f"the sandbox test could not start: {error}"
                return result(chosen, probe.bwrap, False, detail, probe.causes)
    except (OSError, paths.RuntimeDirError) as error:
        return result(chosen, None, False, f"cannot prepare the sandbox test: {error}")
    if completed.returncode:
        reason = _first_line(completed.stderr) or _first_line(completed.stdout)
        detail = f"the sandbox test failed with exit code {completed.returncode}"
        if reason:
            detail += f": {reason}"
        causes = [*probe.causes, *_failure_causes(engine, env)]
        return result(chosen, probe.bwrap, False, detail, causes)
    if probe.causes:
        detail = "the sandbox test passed, but Ember refuses a sandboxed run"
        return result(chosen, probe.bwrap, False, detail, probe.causes)
    sandbox = "Codex's sandbox" if chosen == CODEX else "bubblewrap"
    detail = f"{PROBE_COMMAND} ran under {sandbox}"
    return result(chosen, probe.bwrap, True, detail)
