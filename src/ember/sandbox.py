"""Worker sandboxes: the provider per engine, the deny list and its checks.

A sandboxed worker sees the machine read-only except its worktree, `/tmp`
and the workers' cache folder, and does not see the home folder apart from
the paths it needs: the worktree, the repository's common git folder, `~/.local/bin`,
`~/.local/lib`, git's configuration, the workers' cache folder, the engine's
own folders and the run's read paths. The whole of `$XDG_RUNTIME_DIR` is
hidden, and so are Ember's configuration and state folders wherever they are,
even where a kept path holds them. The worktrees folder is walked and every
entry except the worker's own worktree is denied by name, through symlink
aliases too. No kept path is reopened inside a denied folder.
The worktrees folder is created before the sandbox starts, including for a
read-only worker.

The workers' cache folder, `paths.worker_cache_dir()`, is writable outside
the worktree. `worker_cache_env` gives the variables that point tools' caches
into that folder: `XDG_CACHE_HOME`, and the
variables in `WORKER_CACHE_VARIABLES` for tools that do not follow it.
Ember does not set `TMPDIR`. Bubblewrap mounts a private empty `/tmp` for
each worker; the Codex profile makes `/tmp` writable, including for read-only runs.

Each engine has one switch, `sandbox.<engine>`, `on` or `off`. Codex's
sandbox is its own: it receives the deny list as a permission profile named
`ember` in `-c` options and applies it with a `bwrap`, the system one when it
is on `PATH`, else the one bundled with Codex. Ember runs OpenCode, and each
Claude Code turn, inside its own `bwrap` command line with the same list.

The last check of each engine is kept in `sandbox.json` in Ember's cache
folder, so the app can show it without running the check again.

Every function that reads the environment takes an optional `env` mapping,
which defaults to `os.environ`.
"""

import fcntl
import json
import os
import platform
import re
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
from ember.engines import status

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

WORKER_CACHE_VARIABLES: tuple[tuple[str, str], ...] = (
    ("UV_CACHE_DIR", "uv"),
    ("UV_PYTHON_INSTALL_DIR", "uv-python"),
    ("PIP_CACHE_DIR", "pip"),
    ("npm_config_cache", "npm"),
    ("GOMODCACHE", "go/mod"),
)
"""Cache variables a sandboxed worker gets, each with its folder in the cache.

They cover tools that keep a cache in the home folder whatever
`XDG_CACHE_HOME` says (npm, Go modules), tools whose own
variable, when the owner sets it, overrides `XDG_CACHE_HOME` (uv, pip), and
uv's folder of Python installations, which uv must be able to read and which
the sandbox hides in `~/.local/share/uv`.
"""

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class SandboxError(RuntimeError):
    """A sandbox cannot be set up."""


class KeptPathError(SandboxError):
    """A kept path lies inside a folder the sandbox hides."""

    def __init__(self, path: Path, folder: Path) -> None:
        """Keep both paths.

        Args:
            path: The kept path.
            folder: The hidden folder that holds it.
        """
        super().__init__(
            f"the kept path {path} is inside {folder}, "
            "which the sandbox hides from every worker"
        )
        self.path = path
        self.folder = folder


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


def worker_cache_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the variables that point a sandboxed worker's caches to its folder.

    They replace any value the owner set, because the sandbox hides the
    folders those values name.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `XDG_CACHE_HOME` set to `paths.worker_cache_dir()`, and each variable
        in `WORKER_CACHE_VARIABLES` set to its folder inside it.
    """
    root = paths.worker_cache_dir(_env(env))
    result = {"XDG_CACHE_HOME": str(root)}
    result.update(
        (variable, str(root / folder)) for variable, folder in WORKER_CACHE_VARIABLES
    )
    return result


def _worker_cache(env: Mapping[str, str]) -> Path:
    """Return the workers' cache folder, created at mode 700 when possible.

    A run creates the folder when it starts and refuses to start when it
    cannot. Here a failure is left to the sandbox: the folder is then
    neither bound nor writable.
    """
    try:
        return paths.ensure_worker_cache_dir(env)
    except OSError, ValueError:
        return paths.worker_cache_dir(env)


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
        env: The environment, for `XDG_CONFIG_HOME` and the workers' cache
            folder; `None` reads `os.environ`.

    Returns:
        The kept paths, sorted.
    """
    env = _env(env)
    entries = [
        home / ".local" / "bin",
        home / ".local" / "lib",
        home / ".gitconfig",
        _git_config_dir(home, env),
        paths.worker_cache_dir(env),
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
    walk: Mapping[Path, Sequence[Path]] | None = None,
) -> list[Path]:
    """Return the paths a sandboxed worker is denied.

    The home folder is walked only along ancestors of kept paths; every other
    entry is denied by its own name. A symlink whose target is kept, inside a
    kept path or an ancestor of one is not denied, because Codex's sandbox
    follows a denied symlink and hides its target; a target that is an
    ancestor is walked like any other folder. Additional folders are walked
    with their own kept paths, even when a broader read path contains them.
    No two denials resolve to the same path or one inside another.

    Args:
        home: The home folder.
        kept: The kept paths, from `kept_paths`.
        runtime: The runtime base folder, `$XDG_RUNTIME_DIR`, denied whole.
        protected: Paths denied even when a kept path holds them, such as
            Ember's configuration and state folders. Missing ones are left
            out.
        walk: Additional folders and the paths to keep in each one.

    Returns:
        The denied paths, sorted.
    """
    denied: list[Path] = []
    for root, allowed in [(Path(home), kept), *(walk or {}).items()]:
        allowed = [
            *allowed,
            *(
                path
                for path in walk or {}
                if path != root and path.is_relative_to(root)
            ),
        ]
        pending = [root]
        walked: set[Path] = set()
        while pending:
            folder = pending.pop()
            if folder in walked or any(folder.is_relative_to(path) for path in allowed):
                continue
            walked.add(folder)
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                if any(entry.is_relative_to(path) for path in allowed):
                    continue
                if entry.is_symlink():
                    target = entry.resolve()
                    if _related(target, allowed):
                        if not any(target.is_relative_to(path) for path in allowed):
                            pending.append(target)
                        continue
                if any(path.is_relative_to(entry) for path in allowed):
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
    """Return Ember's configuration and state folders, which no worker may read."""
    return [paths.config_dir(env), paths.state_dir(env)]


# Browser profiles include credentials in native and packaged installations.
SECRET_HOME_PATHS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".config/gh",
    ".docker/config.json",
    ".netrc",
    ".kube",
    ".password-store",
    ".local/share/keyrings",
    ".mozilla",
    ".config/mozilla",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".config/opera",
    ".var/app/org.mozilla.firefox",
    ".var/app/org.mozilla.Thunderbird",
    ".var/app/com.google.Chrome",
    ".var/app/org.chromium.Chromium",
    ".var/app/com.brave.Browser",
    ".var/app/com.microsoft.Edge",
    ".var/app/com.vivaldi.Vivaldi",
    ".var/app/com.opera.Opera",
    "snap/firefox",
    "snap/chromium",
    "snap/brave",
    "snap/thunderbird",
    ".thunderbird",
)
"""Secret locations relative to the home folder, including browser profiles."""


STARTUP_HOME_FILES = (
    ".profile",
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".bash_logout",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".zlogin",
    ".zlogout",
    ".shrc",
    ".kshrc",
    ".mkshrc",
    ".cshrc",
    ".tcshrc",
    ".login",
    ".logout",
    ".yashrc",
    ".yash_profile",
    ".inputrc",
    ".editrc",
)
"""Home shell and line-editor startup files whose denials need no read offer."""


def _variants(entries: Iterable[Path]) -> list[str]:
    return sorted({str(p) for entry in entries for p in (entry, entry.resolve())})


def secret_paths(env: Mapping[str, str] | None = None) -> list[Path]:
    """Return the explicit credential inventory and Ember's private folders."""
    env = _env(env)
    home = paths.home_dir(env)
    config = _git_config_dir(home, env).parent
    native = [
        config / name
        for name in (
            "gcloud",
            "gh",
            "mozilla",
            "google-chrome",
            "chromium",
            "BraveSoftware",
            "microsoft-edge",
            "vivaldi",
            "opera",
        )
    ]
    return [*(home / name for name in SECRET_HOME_PATHS), *native, *_protected(env)]


def read_path_problem(
    path: str | Path, env: Mapping[str, str] | None = None
) -> str | None:
    """Return why a read setting names Ember's configuration or state folder."""
    env = _env(env)
    for candidate in _variants([Path(path)]):
        for folder in _variants(_protected(env)):
            if Path(candidate).is_relative_to(folder):
                return (
                    f"{path} is inside {folder}, Ember's configuration or state folder"
                )
    return None


def allow_reading_path(path: str | Path, env: Mapping[str, str] | None = None) -> bool:
    """Return whether a folder can be offered for automatic read permission."""
    env = _env(env)
    home = paths.home_dir(env)
    blocked = _variants(
        [
            *secret_paths(env),
            _git_config_dir(home, env).parent,
            _runtime_base(env),
            paths.worktrees_dir(env),
        ]
    )
    return not any(
        Path(candidate).is_relative_to(folder) or Path(folder).is_relative_to(candidate)
        for candidate in _variants([Path(path)])
        for folder in blocked
    )


def valid_snapshot(value: object) -> bool:
    """Check a stored launch snapshot, including the empty legacy default."""
    if not isinstance(value, dict):
        return False
    if not value:
        return True
    if not isinstance(value.get("home"), str) or not Path(value["home"]).is_absolute():
        return False
    startup = value.get("startup", [])
    if not isinstance(startup, list) or not all(
        isinstance(p, str) and Path(p).is_absolute() for p in startup
    ):
        return False
    for key in ("secret", "protected", "config", "own"):
        entries = value.get(key)
        if not isinstance(entries, list) or not all(
            isinstance(p, str) and Path(p).is_absolute() for p in entries
        ):
            return False
    hidden = value.get("hidden")
    return isinstance(hidden, list) and all(
        isinstance(entry, dict)
        and type(entry.get("directory")) is bool
        and all(
            isinstance(entry.get(key), str) and Path(entry[key]).is_absolute()
            for key in ("path", "resolved")
        )
        for entry in hidden
    )


def hidden_snapshot(
    worktree: Path,
    common: Path,
    read: Iterable[str | Path] = (),
    *,
    engine: str = "codex",
    binary: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Record hidden paths, startup files and their resolved names at launch."""
    env = _env(env)
    home = paths.home_dir(env)
    worktrees = paths.ensure_worktrees_dir(env)
    runtime = _runtime_base(env)
    kept = kept_paths(home, worktree, common, read, env=env)
    if engine != "codex":
        folders = (
            claude_folders(home, env)
            if engine == "claude"
            else [p for p, _ in _opencode_folders(home, env)]
        )
        if binary is not None and binary.resolve().parent.is_relative_to(home):
            folders.append(binary.resolve().parent)
        kept = sorted(
            {*kept, *(Path(p) for p in _variants(folders) if Path(p).exists())}
        )
    denied = deny_list(
        home,
        kept,
        runtime,
        protected=_protected(env),
        walk={worktrees: [worktree, worktree.resolve()]},
    )
    return {
        "hidden": [
            {
                "path": str(p),
                "resolved": str(p.resolve()),
                "directory": p.is_dir() or p == runtime,
            }
            for p in denied
        ],
        "secret": _variants(secret_paths(env)),
        "startup": _variants(home / name for name in STARTUP_HOME_FILES),
        "protected": _variants([runtime, worktrees]),
        "config": _variants([_git_config_dir(home, env).parent]),
        "own": _variants([worktree]),
        "home": str(home),
    }


def _inside(candidate: Path, folders: Iterable[str]) -> bool:
    return any(candidate.is_relative_to(folder) for folder in folders)


def merge_denial(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    """Merge reports of one resolved hidden path without weakening restrictions.

    Reasons rank protected, secret location, startup file, then hidden. A missing read
    permission is retained, and paths use lexical order to break ties.
    """
    ranks = {"hidden": 0, "startup file": 1, "secret location": 2, "protected": 3}
    reason = max((first["reason"], second["reason"]), key=ranks.__getitem__)
    allowances = (first["allow_path"], second["allow_path"])
    allow = (
        min(allowances)
        if reason == "hidden" and all(path is not None for path in allowances)
        else None
    )
    return {
        "path": min(first["path"], second["path"]),
        "reason": reason,
        "allow_path": allow,
    }


def denied_paths(text: str, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Find fully stated denied paths using the sandbox's launch-time inventory.

    Absolute and `~/` paths can occur on the error line or a neighboring
    diagnostic line. `~/` uses the home folder recorded at launch. Both given
    and resolved names are checked; relative paths are not resolved or reported.
    """
    found: dict[str, dict[str, Any]] = {}
    error_pattern = r"permission denied|operation not permitted|\bEACCES\b"

    def json_text(value: object, *, path_field: bool = False) -> str:
        """Keep diagnostic strings from structured output without its field names."""
        if isinstance(value, str):
            if value.startswith(("/", "~/")) and (
                path_field or not re.search(error_pattern, value, re.I)
            ):
                return json.dumps(value)
            return value
        if isinstance(value, dict):
            return "\n".join(
                json_text(item, path_field=key in ("path", "file_path", "filename"))
                for key, item in value.items()
                if key
                not in (
                    "cwd",
                    "workdir",
                    "working_directory",
                    "directory",
                    "input",
                    "arguments",
                    "args",
                    "command",
                    "cmd",
                    "metadata",
                    "context",
                )
            )
        if isinstance(value, list):
            return "\n".join(json_text(item, path_field=path_field) for item in value)
        return ""

    try:
        decoded = json.loads(text)
    except ValueError, RecursionError:
        pass
    else:
        text = json_text(decoded)
    text = text.replace("\\n", "\n").replace('\\"', '"')
    lines = text.splitlines()
    path_pattern = re.compile(
        r"(?P<quote>[\"'`])(?P<quoted>[^\n]*?)(?P=quote)"
        r"|(?<![\w./~])(?P<bare>(?:/|~/)[^\s\"'`<>]+)"
    )

    def relative_operand(line: str) -> bool:
        """Recognize a relative error operand without resolving it."""
        for match in path_pattern.finditer(line):
            name = match["quoted"]
            if (
                name
                and not name.startswith(("/", "~/"))
                and not re.search(error_pattern, name, re.I)
            ):
                return True
        return any(
            re.search(pattern, line, re.I)
            for pattern in (
                r"(?<![\w/])(?:\.{1,2}/|[\w.@+-]+/)[^\s\"'`<>]+",
                rf":\s*[^:\s]+\s*:\s*(?:{error_pattern})",
                rf"(?:{error_pattern}):\s*[^/\s~:\"'`][^\s:]*",
            )
        )

    def path_names(line: str) -> list[str]:
        """Extract fully stated paths, excluding relative names and their suffixes."""
        if not re.search(error_pattern, line, re.I) and re.match(
            r"\s*(?:cwd|workdir|working_directory|working directory|directory|"
            r"command|cmd)\s*[:=]",
            line,
            re.I,
        ):
            return []
        names = [
            m["quoted"] if m["quoted"] is not None else m["bare"].rstrip(":,;.)]")
            for m in path_pattern.finditer(line)
        ]
        return [name for name in names if name.startswith(("/", "~/"))]

    for index, line in enumerate(lines):
        if not re.search(error_pattern, line, re.I):
            continue
        names = path_names(line)
        if not names and relative_operand(line):
            # A relative error operand is not completed using an adjacent cwd.
            continue
        if not names:
            # A cause chain can name the path before the operating-system
            # error, and a wrapped diagnostic can put it on the next line.
            for neighbor in (index - 1, index + 1, index - 2):
                if not 0 <= neighbor < len(lines):
                    continue
                start, end = sorted((index, neighbor))
                if not all(part.strip() for part in lines[start : end + 1]):
                    continue
                names = path_names(lines[neighbor])
                if names:
                    break
        for name in names:
            if name.startswith("~/"):
                name = snapshot["home"] + name[1:]
            candidate = Path(os.path.normpath(name))
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            matches = [
                entry
                for entry in snapshot.get("hidden", [])
                if _inside(candidate, (entry["path"], entry["resolved"]))
                or _inside(resolved, (entry["path"], entry["resolved"]))
            ]
            if not matches:
                continue
            entry = max(matches, key=lambda item: len(Path(item["path"]).parts))
            # Map a given hidden name through its launch-time resolution.
            target = resolved
            if candidate.is_relative_to(entry["path"]):
                target = Path(entry["resolved"]) / candidate.relative_to(entry["path"])
            views = (candidate, resolved, target)
            if any(_inside(p, snapshot.get("own", [])) for p in views) and any(
                Path(own).is_relative_to(hidden)
                for own in snapshot.get("own", [])
                for hidden in (entry["path"], entry["resolved"])
            ):
                continue
            reason = "hidden"
            if any(str(p) in snapshot.get("startup", []) for p in views):
                reason = "startup file"
            elif any(_inside(p, snapshot.get("protected", [])) for p in views):
                reason = "protected"
            elif any(_inside(p, snapshot.get("secret", [])) for p in views):
                reason = "secret location"
            folder = candidate if candidate.is_dir() else candidate.parent
            if candidate == Path(entry["path"]) and entry["directory"]:
                folder = candidate
            forbidden = [*snapshot.get("secret", []), *snapshot.get("protected", [])]
            allow = str(folder)
            if reason != "hidden" or any(
                _inside(p, [*forbidden, *snapshot.get("config", [])])
                or any(Path(secret).is_relative_to(p) for secret in forbidden)
                for p in (
                    folder,
                    folder.resolve(),
                    target if folder == candidate else target.parent,
                )
            ):
                allow = None
            # Startup files inside hidden folders have their own identity so
            # their denials do not suppress reports of other files there.
            reported = (
                str(target)
                if reason == "startup file" and entry["directory"]
                else entry["path"]
            )
            key = str(target) if reason == "startup file" else entry["resolved"]
            previous = found.get(key)
            item = {"path": reported, "reason": reason, "allow_path": allow}
            found[key] = item if previous is None else merge_denial(previous, item)
    return list(found.values())


def check_kept(
    worktree: Path,
    common: Path,
    read: Iterable[str | Path] = (),
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Refuse a sandbox whose kept path lies inside one of Ember's own folders.

    The sandbox hides Ember's configuration and state folders even where a
    kept path holds them. Entries in the worktrees folder are denied by name
    except the worker's own worktree.
    Every kept path is checked, as given and with symlinks resolved: the
    worktree, the common git folder, the read paths and the others
    `kept_paths` lists, the workers' cache folder among them.

    Args:
        worktree: The worker's worktree.
        common: The repository's common git folder.
        read: The run's read paths.
        env: The environment; `None` reads `os.environ`.

    Raises:
        KeptPathError: A kept path is one of Ember's folders or inside one;
            the message names both.
    """
    env = _env(env)
    home = paths.home_dir(env)
    given = [Path(worktree), Path(common), *(Path(path) for path in read)]
    entries = [
        *given,
        *(path.resolve() for path in given),
        *kept_paths(home, worktree, common, read, env=env),
    ]
    protected = [
        folder
        for path in _protected(env)
        for folder in dict.fromkeys((path, path.resolve()))
    ]
    worktrees = paths.worktrees_dir(env).resolve()
    own = Path(worktree).resolve()
    for entry in dict.fromkeys(entries):
        for folder in protected:
            if entry.is_relative_to(folder):
                raise KeptPathError(entry, folder)
        target = entry.resolve()
        if target.is_relative_to(worktrees) and not target.is_relative_to(own):
            raise KeptPathError(entry, worktrees)


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
    legacy read-only source checkout. Detached review worktrees use the
    writable profile. Codex's own protection of git folders stays in place.
    The workers' cache folder is writable, also in a read-only run, and the
    commands Codex runs get the variables of `worker_cache_env` through
    `shell_environment_policy.set`. `/tmp` is writable in every profile.
    The cache folder is created when it is missing.

    Args:
        worktree: The worker's worktree, writable unless `read_only`.
        common: The repository's common git folder, read-only.
        read: The run's read paths.
        read_only: Protect a legacy read-only run's source checkout.
        network: The worker has network.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The options, each `-c` followed by its value.

    Raises:
        RuntimeDirError: No runtime base directory is available.
    """
    env = _env(env)
    home = paths.home_dir(env)
    cache = _worker_cache(env)
    worktrees = paths.ensure_worktrees_dir(env)
    kept = kept_paths(home, worktree, common, read, env=env)
    denied = deny_list(
        home,
        kept,
        _runtime_base(env),
        protected=_protected(env),
        walk={worktrees: [Path(worktree), Path(worktree).resolve()]},
    )
    filesystem = {str(path): "deny" for path in denied}
    for path in dict.fromkeys((cache, cache.resolve())):
        filesystem[str(path)] = "write"
    filesystem["/tmp"] = "write"
    for path in dict.fromkeys((Path(worktree), Path(worktree).resolve())):
        filesystem[str(path)] = "read" if read_only else "write"
    filesystem[str(common)] = "read"
    filesystem[str(Path(worktree) / ".git")] = "read"
    # TOML takes Unicode scalar values, not JSON's surrogate pairs.
    entries = ", ".join(
        f"{json.dumps(path, ensure_ascii=False)} = {json.dumps(access)}"
        for path, access in filesystem.items()
    )
    extends = ":read-only" if read_only else ":workspace"
    variables = [
        option
        for name, value in worker_cache_env(env).items()
        for option in (
            "-c",
            f"shell_environment_policy.set.{name}="
            f"{json.dumps(value, ensure_ascii=False)}",
        )
    ]
    return [
        "-c",
        f"default_permissions={json.dumps(PROFILE)}",
        "-c",
        f"permissions.{PROFILE}.extends={json.dumps(extends)}",
        "-c",
        f"permissions.{PROFILE}.filesystem={{{entries}}}",
        "-c",
        f"permissions.{PROFILE}.network.enabled={str(network).lower()}",
        *variables,
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

    Its data and state folders are writable and its configuration folder is
    read-only. Its cache is in the workers' cache folder, because the sandbox
    sets `XDG_CACHE_HOME`.
    """
    return [
        (_xdg(home, env, "XDG_DATA_HOME", ".local", "share") / "opencode", True),
        (_xdg(home, env, "XDG_STATE_HOME", ".local", "state") / "opencode", True),
        (_xdg(home, env, "XDG_CONFIG_HOME", ".config") / "opencode", False),
    ]


def claude_folders(home: Path, env: Mapping[str, str]) -> list[Path]:
    """Return the folders and files Claude Code writes in the home folder.

    They are its configuration folder (`$CLAUDE_CONFIG_DIR`, or
    `~/.claude`), `~/.claude.json` and its backup. Its cache is in the
    workers' cache folder, because the sandbox sets `XDG_CACHE_HOME`.

    Args:
        home: The home folder.
        env: The environment, for `CLAUDE_CONFIG_DIR`.

    Returns:
        The paths, whether or not they exist.
    """
    value = env.get("CLAUDE_CONFIG_DIR", "")
    config = Path(value) if value and Path(value).is_absolute() else home / ".claude"
    return [config, home / ".claude.json", home / ".claude.json.backup"]


def _mount_aliases(path: Path, mounts: Sequence[tuple[Path, Path]]) -> list[Path]:
    """Return every mount destination that exposes `path`, including aliases.

    A bind of a symlinked ancestor creates a separate view of its target.
    Masking the canonical path does not hide that view's corresponding subtree.
    """
    resolved = path.resolve()
    aliases = {path, resolved}
    for source, destination in mounts:
        root = source.resolve()
        if resolved.is_relative_to(root):
            aliases.add(destination / resolved.relative_to(root))
    return sorted(aliases, key=lambda item: (len(item.parts), str(item)))


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

    The workers' cache folder is writable, also in a read-only run, and the
    command gets the variables of `worker_cache_env`. Each worker gets a
    private empty `/tmp`.
    The cache folder is created when it is missing. Each denied entry gets
    a mask at every mount destination that exposes it, including symlink
    aliases. The worktrees folder itself is never masked around a kept path.

    Args:
        argv: The command to run.
        worktree: The worker's worktree, also the working folder.
        common: The repository's common git folder, read-only.
        read: The run's read paths.
        read_only: Protect a legacy read-only run's source checkout.
        binary: The engine's binary; its folder is kept read-only when it is
            in the home folder.
        folders: The engine's own folders and files, each with whether it is
            writable; missing ones are left out.
        env: The environment.

    Returns:
        The command line.
    """
    home = paths.home_dir(env)
    cache = _worker_cache(env)
    worktrees = paths.ensure_worktrees_dir(env)
    args = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
    ]  # fmt: skip
    mounts = [(Path("/"), Path("/"))]

    def bind(
        source: Path, *, target: Path | None = None, writable: bool = False
    ) -> None:
        """Append a bind and record its source and destination for later masks."""
        destination = source if target is None else target
        args.extend(
            ["--bind" if writable else "--ro-bind", str(source), str(destination)]
        )
        mounts.append((source, destination))

    if binary is not None:
        folder = Path(binary).resolve().parent
        if folder.is_relative_to(home) and folder.exists():
            bind(folder)
    # A command that reaches the Docker daemon can mount the whole disk.
    for socket in dict.fromkeys(Path(path).resolve() for path in DOCKER_SOCKETS):
        if socket.is_socket():
            bind(Path("/dev/null"), target=socket)
    kept = kept_paths(home, worktree, common, read, env=env)
    kept = sorted(
        {
            *kept,
            *(variant for path, _ in folders for variant in (path, path.resolve())),
            *([Path(binary).resolve().parent] if binary is not None else []),
        }
    )
    for path in sorted(kept, key=lambda path: (len(path.parts), str(path))):
        if path.exists():
            bind(path)
    for path, writable in folders:
        if path.exists():
            bind(path, writable=writable)
    for path in dict.fromkeys((cache, cache.resolve())):
        if path.is_dir():
            bind(path, writable=True)
    for name, value in worker_cache_env(env).items():
        args += ["--setenv", name, value]
    bind(Path(worktree), writable=not read_only)
    bind(Path(common))
    dotgit = Path(worktree) / ".git"
    if dotgit.exists():
        bind(dotgit)
    denied = deny_list(
        home,
        kept,
        _runtime_base(env),
        protected=_protected(env),
        walk={worktrees: [Path(worktree), Path(worktree).resolve()]},
    )
    for path in denied:
        if path.is_dir() or path == _runtime_base(env):
            for alias in _mount_aliases(path, mounts):
                args += ["--tmpfs", str(alias)]
        elif path.exists():
            for alias in _mount_aliases(path, mounts):
                bind(Path("/dev/null"), target=alias)
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

    The whole machine is mounted read-only. Unkept entries of the home folder
    and Ember's worktrees folder are masked by name. The runtime base
    is masked whole. Each worker gets a private empty `/tmp`.
    OpenCode's data and state folders and the workers' cache folder are
    writable, its configuration folder
    read-only, and detached review worktrees writable. `read_only` protects
    legacy source checkouts. `XDG_CACHE_HOME`
    and the variables of `WORKER_CACHE_VARIABLES` point into the workers'
    cache folder, for OpenCode and every command it runs. Ember's own folders
    and the system Docker sockets are hidden. The worker gets its own PID
    namespace and keeps network.

    Args:
        argv: The command to run.
        worktree: The worker's worktree, also the working folder.
        common: The repository's common git folder, read-only.
        read: The run's read paths.
        read_only: Protect a legacy read-only run's source checkout.
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
    Code's folders and files from `claude_folders` writable, the workers'
    cache folder writable with `XDG_CACHE_HOME` pointing to it, and the folder
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
        read_only: Protect a legacy read-only run's source checkout.
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


CHECK_CACHE_NAME = "sandbox.json"
"""The file in Ember's cache folder holding the last check of each engine."""

CHECK_LOCK_NAME = "sandbox.lock"
"""The file in Ember's cache folder locked while the check cache is written."""


def check_cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path of the sandbox check cache.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `sandbox.json` in Ember's cache folder.
    """
    return paths.cache_dir(env) / CHECK_CACHE_NAME


def _optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _check_from_json(engine: str, value: object) -> SandboxCheck | None:
    """Return a cached check of `engine`, or `None` when it is not valid."""
    if not isinstance(value, dict) or value.get("engine") != engine:
        return None
    provider = value.get("provider")
    causes = value.get("causes")
    checked_at = status.finite_number(value.get("checked_at"))
    if (
        (provider is not None and provider not in PROVIDERS)
        or not _optional_str(value.get("bwrap_path"))
        or not _optional_str(value.get("bwrap_version"))
        or not isinstance(value.get("ok"), bool)
        or not isinstance(value.get("detail"), str)
        or not isinstance(causes, list)
        or not all(isinstance(cause, str) for cause in causes)
        or checked_at is None
    ):
        return None
    return SandboxCheck(
        engine=engine,
        provider=provider,
        bwrap_path=value["bwrap_path"],
        bwrap_version=value["bwrap_version"],
        ok=value["ok"],
        detail=value["detail"],
        causes=tuple(causes),
        checked_at=checked_at,
    )


def _held_checks(path: Path) -> dict[str, Any]:
    """Return the cache file's entries that are valid checks, as JSON."""
    value = status.load_cache_file(path)
    if not isinstance(value, dict) or not isinstance(value.get("checks"), dict):
        return {}
    return {
        engine: item
        for engine, item in value["checks"].items()
        if engine in ENGINES and _check_from_json(engine, item) is not None
    }


def last_checks(env: Mapping[str, str] | None = None) -> dict[str, SandboxCheck]:
    """Read the last sandbox check of each engine from the cache.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        Each engine's last check, keyed by engine in `ENGINES` order. An
        engine with no check, or with an entry that is not valid, is left
        out; a cache that is missing or cannot be read gives `{}`.
    """
    try:
        path = check_cache_path(env)
    except OSError, KeyError:
        return {}
    held = _held_checks(path)
    result: dict[str, SandboxCheck] = {}
    for engine in ENGINES:
        item = _check_from_json(engine, held.get(engine))
        if item is not None:
            result[engine] = item
    return result


def save_check(check: SandboxCheck, env: Mapping[str, str] | None = None) -> None:
    """Keep a check as its engine's last check in the cache.

    The cache holds one entry per engine. This replaces the entry of the
    check's engine and keeps the others. The read, merge and write run while
    `sandbox.lock` in Ember's cache folder is locked, so writers in other
    threads or processes do not drop each other's entries. Writing is best
    effort: when the cache cannot be written it stays as it was and nothing
    is raised.

    Args:
        check: The check to keep.
        env: The environment; `None` reads `os.environ`.

    Raises:
        ValueError: The check's engine has no sandbox.
    """
    _check_engine(check.engine)
    try:
        path = check_cache_path(env)
        paths.ensure_private_dir(path.parent, root=paths.cache_dir(env))
        fd = paths.open_private_file(path.parent / CHECK_LOCK_NAME)
    except OSError, KeyError, ValueError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        held = _held_checks(path)
        held[check.engine] = check.to_json()
        ordered = {engine: held[engine] for engine in ENGINES if engine in held}
        status.save_cache_file(path, {"checks": ordered}, env)
    except OSError:
        return
    finally:
        os.close(fd)
