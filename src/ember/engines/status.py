"""Whether each engine is installed, signed in and connected on this machine.

An engine is connected when every requirement is met: its CLI is found and its
sign-in check passes. Each check is a local command with a timeout of
`TIMEOUT` seconds; none spends a model turn. A command that cannot run, exits
with an unexpected code, prints something unexpected or times out gives the
state `check failed` with a reason, never an exception.

Results are cached in `engines.json` in Ember's cache folder. The cache is
read until a refresh is asked for; nothing refreshes it on a timer.

Every function takes an optional `env` mapping, which defaults to `os.environ`
and supplies `HOME`, the XDG variables and `PATH`.
"""

import contextlib
import json
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ember import paths

ENGINES = ("codex", "opencode", "claude")
"""The engines, in the order Ember lists them."""

TIMEOUT = 10
"""Seconds each check command may run."""

CACHE_NAME = "engines.json"
"""The status cache's file name in Ember's cache folder."""

CONNECTED = "connected"
NOT_INSTALLED = "not installed"
NOT_SIGNED_IN = "not signed in"
CHECK_FAILED = "check failed"
STATES = (CONNECTED, NOT_INSTALLED, NOT_SIGNED_IN, CHECK_FAILED)
"""Every engine state."""

INSTALL = {
    "codex": "npm install -g @openai/codex",
    "opencode": "curl -fsSL https://opencode.ai/install | bash",
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
}
"""The command that installs each engine's CLI."""

SIGN_IN_ARGS = {
    "codex": ("login",),
    "opencode": ("auth", "login"),
    "claude": ("auth", "login"),
}
"""The arguments to each engine's CLI that sign in."""

SIGN_IN_LABEL = {
    "codex": "a ChatGPT sign-in for Codex",
    "opencode": "at least one integration connected in OpenCode (use /connect)",
    "claude": "a Claude Code sign-in",
}
"""The sign-in requirement of each engine."""

CLAUDE_HIDDEN = ("CLAUDE_CODE_", "CLAUDECODE", "ANTHROPIC_")
"""Prefixes of variables removed before running `claude`.

A Claude worker never receives the calling session's own variables, so the
checks do not see them either.
"""

_LINE_LIMIT = 200


@dataclass(frozen=True)
class Requirement:
    """One thing an engine needs before it is connected."""

    label: str
    """What is needed."""

    met: bool
    """Whether it is in place."""

    fix: str | None
    """The exact command that fixes it; `None` when met or not known."""

    def to_json(self) -> dict[str, Any]:
        """Return the requirement as a JSON-ready dict.

        Returns:
            `{"label", "met", "fix"}`.
        """
        return {"label": self.label, "met": self.met, "fix": self.fix}


@dataclass(frozen=True)
class EngineStatus:
    """One engine's state on this machine."""

    engine: str
    """`codex`, `opencode` or `claude`."""

    state: str
    """One of `STATES`."""

    reason: str | None
    """Why the engine is not connected; `None` when connected."""

    installed: bool
    """Whether the engine's CLI was found."""

    binary: str | None
    """The absolute path of the engine's CLI."""

    version: str | None
    """The first line the CLI printed for `--version`."""

    signed_in: bool | None
    """Whether the CLI reports the sign-in; `None` when it cannot be read."""

    connected: bool
    """Whether every requirement is met and every check ran."""

    requirements: tuple[Requirement, ...]
    """Each requirement and whether it is met."""

    checked_at: float
    """When the checks ran, in seconds since the epoch."""

    def to_json(self) -> dict[str, Any]:
        """Return the status as a JSON-ready dict.

        Returns:
            Every field, with `requirements` as a list of dicts.
        """
        return {
            "engine": self.engine,
            "state": self.state,
            "reason": self.reason,
            "installed": self.installed,
            "binary": self.binary,
            "version": self.version,
            "signed_in": self.signed_in,
            "connected": self.connected,
            "requirements": [item.to_json() for item in self.requirements],
            "checked_at": self.checked_at,
        }


def finite_number(value: object) -> float | None:
    """Return `value` as a float when it is a finite JSON number.

    Args:
        value: A decoded JSON value.

    Returns:
        The number, or `None` when `value` is not a finite int or float.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _requirement_from_json(value: object) -> Requirement | None:
    if not isinstance(value, dict):
        return None
    label, met, fix = value.get("label"), value.get("met"), value.get("fix")
    if not isinstance(label, str) or not isinstance(met, bool):
        return None
    if not _optional_str(fix):
        return None
    return Requirement(label, met, fix)


def from_json(value: object) -> EngineStatus | None:
    """Rebuild a status from its `to_json()` form.

    Args:
        value: The decoded JSON value.

    Returns:
        The status, or `None` when `value` is not a valid status.
    """
    if not isinstance(value, dict):
        return None
    items = value.get("requirements")
    if not isinstance(items, list):
        return None
    requirements = [_requirement_from_json(item) for item in items]
    checked_at = finite_number(value.get("checked_at"))
    signed_in = value.get("signed_in")
    valid = (
        value.get("engine") in ENGINES
        and value.get("state") in STATES
        and _optional_str(value.get("reason"))
        and isinstance(value.get("installed"), bool)
        and _optional_str(value.get("binary"))
        and _optional_str(value.get("version"))
        and (signed_in is None or isinstance(signed_in, bool))
        and isinstance(value.get("connected"), bool)
        and checked_at is not None
        and all(item is not None for item in requirements)
    )
    if not valid or checked_at is None:
        return None
    return EngineStatus(
        engine=value["engine"],
        state=value["state"],
        reason=value["reason"],
        installed=value["installed"],
        binary=value["binary"],
        version=value["version"],
        signed_in=signed_in,
        connected=value["connected"],
        requirements=tuple(item for item in requirements if item is not None),
        checked_at=checked_at,
    )


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _check_engine(engine: str) -> None:
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}")


def locate(engine: str, env: Mapping[str, str] | None = None) -> str | None:
    """Find an engine's CLI.

    OpenCode is also looked for at `~/.opencode/bin/opencode`, where its
    installer puts it.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The CLI's absolute path, or `None` when it is not found.

    Raises:
        ValueError: `engine` is not an engine.
    """
    _check_engine(engine)
    env = _env(env)
    found = shutil.which(engine, path=env.get("PATH", os.defpath))
    if found is None and engine == "opencode":
        candidate = paths.home_dir(env) / ".opencode" / "bin" / "opencode"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found = str(candidate)
    return None if found is None else os.path.abspath(found)


def probe_env(engine: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment an engine's check commands run with.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        A copy of `env`; for `claude`, without the `CLAUDE_HIDDEN` variables.
    """
    env = _env(env)
    if engine == "claude":
        return {
            key: value
            for key, value in env.items()
            if not key.startswith(CLAUDE_HIDDEN)
        }
    return dict(env)


class ProbeError(Exception):
    """A check command could not run or did not finish."""


def first_line(text: str) -> str:
    """Return the first non-empty line of `text`, stripped and shortened.

    Args:
        text: Command output.

    Returns:
        The line, at most 200 characters, or `(no output)`.
    """
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:_LINE_LIMIT]
    return "(no output)"


def run_probe(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
    """Run a check command with standard error merged into its output.

    Args:
        argv: The command and its arguments.
        env: The command's whole environment.

    Returns:
        The exit code and the output.

    Raises:
        ProbeError: The command cannot run or exceeds `TIMEOUT` seconds.
    """
    shown = shlex.join([Path(argv[0]).name, *argv[1:]])
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=dict(env),
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError(f"{shown} did not finish within {TIMEOUT} seconds") from None
    except OSError as error:
        raise ProbeError(f"{shown} could not run: {error.strerror or error}") from None
    return done.returncode, done.stdout


def _version(binary: str, env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Return (version, failure reason) from `<binary> --version`."""
    try:
        code, output = run_probe([binary, "--version"], env)
    except ProbeError as error:
        return None, str(error)
    line = first_line(output)
    if code != 0:
        return None, f"{Path(binary).name} --version exited {code}: {line}"
    if not output.strip():
        return None, f"{Path(binary).name} --version printed nothing"
    return line, None


def _claude_signed_in(binary: str, env: Mapping[str, str]) -> bool:
    code, output = run_probe([binary, "auth", "status", "--json"], env)
    try:
        value = json.loads(output)
    except ValueError:
        value = None
    if isinstance(value, dict) and isinstance(value.get("loggedIn"), bool):
        return value["loggedIn"]
    if code != 0:
        raise ProbeError(f"claude auth status exited {code}: {first_line(output)}")
    raise ProbeError(f"claude auth status printed {first_line(output)!r}")


def _codex_signed_in(binary: str, env: Mapping[str, str]) -> bool:
    code, output = run_probe([binary, "login", "status"], env)
    lowered = output.lower()
    if "not logged in" in lowered:
        return False
    if code == 0 and "chatgpt" in lowered:
        return True
    if code == 0 and "logged in" in lowered:
        return False
    if code != 0:
        raise ProbeError(f"codex login status exited {code}: {first_line(output)}")
    raise ProbeError(f"codex login status printed {first_line(output)!r}")


_AUTH_LINE = re.compile(
    r"(?P<name>.+?)\s+\(?(?P<method>oauth|api(?:\s+key)?|well-?known|env)\)?"
    r"(?:\s+\S.*)?",
    re.IGNORECASE,
)
_AUTH_LOGIN = {"oauth": "oauth", "api": "api", "api key": "api"}


def parse_opencode_auth(output: str) -> tuple[tuple[str, str | None], ...]:
    """Read integration display names and login methods from auth-list output.

    OpenCode 1 prints `●  <name> <method>` lines inside terminal borders;
    OpenCode 2 prints `<name>  <method>  <state>` columns, where the method is
    `OAuth` or `API key`. ANSI styling and borders are ignored. `oauth` and
    `api` are the login methods; another recognized method, such as
    `wellknown` or `env`, is connected with an unknown login method. An
    explicit empty list is valid; output with neither credentials nor an
    empty-list message fails.

    Raises:
        ProbeError: The output is not a recognizable auth list.
    """
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    entries: list[tuple[str, str | None]] = []
    for line in text.splitlines():
        line = line.strip().lstrip("│┃┌└┐┘─●○◇◆■□ ").strip()
        match = _AUTH_LINE.fullmatch(line)
        if match:
            method = " ".join(match["method"].lower().split())
            entries.append((match["name"].strip(), _AUTH_LOGIN.get(method)))
    if entries:
        return tuple(entries)
    if re.search(
        r"\b0 credentials?\b|\bno (?:authenticated integrations|credentials)\b",
        text,
        re.IGNORECASE,
    ):
        return ()
    raise ProbeError(f"opencode auth list printed {first_line(text)!r}")


def opencode_auth(
    binary: str, env: Mapping[str, str], version: str | None
) -> tuple[tuple[str, str | None], ...]:
    """Read connected integrations, using standalone mode for OpenCode 2."""
    match = re.fullmatch(
        r"(?:opencode\s+)?v?([0-9]+)\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        version or "",
    )
    argv = [binary, "auth", "list"]
    if match is not None and int(match[1]) >= 2:
        argv.append("--standalone")
    code, output = run_probe(argv, env)
    if code != 0:
        raise ProbeError(f"opencode auth list exited {code}: {first_line(output)}")
    return parse_opencode_auth(output)


def _opencode_signed_in(
    binary: str, env: Mapping[str, str], version: str | None
) -> bool:
    """Return whether at least one OpenCode integration is connected."""
    return bool(opencode_auth(binary, env, version))


_SIGNED_IN = {
    "codex": _codex_signed_in,
    "claude": _claude_signed_in,
}


def _sign_in_fix(engine: str, binary: str, env: Mapping[str, str]) -> str:
    """Return the sign-in command, naming the CLI as `PATH` finds it."""
    on_path = shutil.which(engine, path=env.get("PATH", os.defpath))
    program = engine if on_path and os.path.abspath(on_path) == binary else binary
    return shlex.join([program, *SIGN_IN_ARGS[engine]])


def _check(engine: str, env: Mapping[str, str], now: float | None) -> EngineStatus:
    checked_at = time.time() if now is None else now
    binary = locate(engine, env)
    if binary is None:
        return EngineStatus(
            engine=engine,
            state=NOT_INSTALLED,
            reason=f"the {engine} CLI is not installed",
            installed=False,
            binary=None,
            version=None,
            signed_in=None,
            connected=False,
            requirements=(
                Requirement(f"the {engine} CLI", False, INSTALL[engine]),
                Requirement(SIGN_IN_LABEL[engine], False, None),
            ),
            checked_at=checked_at,
        )
    run_env = probe_env(engine, env)
    failures: list[str] = []
    version, failure = _version(binary, run_env)
    if failure is not None:
        failures.append(failure)
    signed_in: bool | None
    try:
        signed_in = (
            _opencode_signed_in(binary, run_env, version)
            if engine == "opencode"
            else _SIGNED_IN[engine](binary, run_env)
        )
    except ProbeError as error:
        signed_in = None
        failures.append(str(error))
    if failures:
        state, reason = CHECK_FAILED, "; ".join(failures)
    elif not signed_in:
        state, reason = NOT_SIGNED_IN, f"{SIGN_IN_LABEL[engine]} is missing"
    else:
        state, reason = CONNECTED, None
    sign_in = Requirement(
        SIGN_IN_LABEL[engine],
        signed_in is True,
        _sign_in_fix(engine, binary, env) if signed_in is False else None,
    )
    return EngineStatus(
        engine=engine,
        state=state,
        reason=reason,
        installed=True,
        binary=binary,
        version=version,
        signed_in=signed_in,
        connected=state == CONNECTED,
        requirements=(Requirement(f"the {engine} CLI", True, None), sign_in),
        checked_at=checked_at,
    )


def check(engine: str, env: Mapping[str, str] | None = None) -> EngineStatus:
    """Check one engine on this machine.

    The sign-in check runs only when the engine's CLI is found.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The engine's status.

    Raises:
        ValueError: `engine` is not an engine.
    """
    _check_engine(engine)
    return _check(engine, _env(env), None)


def _check_all(env: Mapping[str, str], now: float | None) -> dict[str, EngineStatus]:
    with ThreadPoolExecutor(max_workers=len(ENGINES)) as pool:
        results = pool.map(lambda engine: _check(engine, env, now), ENGINES)
        return dict(zip(ENGINES, results, strict=True))


def check_all(env: Mapping[str, str] | None = None) -> dict[str, EngineStatus]:
    """Check every engine on this machine at once.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        Each engine's status, keyed by engine, in `ENGINES` order.
    """
    return _check_all(_env(env), None)


def cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path of the status cache.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `engines.json` in Ember's cache folder.
    """
    return paths.cache_dir(env) / CACHE_NAME


def load_cache_file(path: Path) -> object | None:
    """Read a JSON cache file.

    Args:
        path: The file.

    Returns:
        The decoded value, or `None` when the file is missing, unreadable or
        not JSON.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None


def save_cache_file(path: Path, value: object, env: Mapping[str, str] | None) -> None:
    """Write a JSON cache file at mode 600, replacing it atomically.

    The value is written to a new private file beside `path`, which then
    replaces `path`, so a reader sees the old file or the new one whole.

    Args:
        path: The file, in Ember's cache folder.
        value: A JSON-ready value.
        env: The environment; `None` reads `os.environ`.

    Raises:
        OSError: The folder or the file cannot be written.
    """
    root = paths.cache_dir(env)
    paths.ensure_private_dir(path.parent, root=root)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    content = json.dumps(value).encode("utf-8")
    fd = paths.open_private_file(temporary, os.O_WRONLY | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cached(env: Mapping[str, str] | None = None) -> dict[str, EngineStatus] | None:
    """Read the status cache without running any check.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        Each engine's cached status, or `None` when the cache is missing or
        not valid.
    """
    try:
        path = cache_path(env)
    except OSError, KeyError:
        return None
    value = load_cache_file(path)
    if not isinstance(value, dict) or not isinstance(value.get("engines"), dict):
        return None
    held = value["engines"]
    result: dict[str, EngineStatus] = {}
    for engine in ENGINES:
        item = from_json(held.get(engine))
        if item is None or item.engine != engine:
            return None
        result[engine] = item
    return result


def read(
    env: Mapping[str, str] | None = None,
    *,
    refresh: bool = False,
    now: float | None = None,
) -> dict[str, EngineStatus]:
    """Return every engine's status, from the cache unless asked to refresh.

    Without a valid cache, or with `refresh`, every engine is checked and the
    cache is written. A cache that cannot be written is left as it was.

    Args:
        env: The environment; `None` reads `os.environ`.
        refresh: Check the engines even when the cache is valid.
        now: The time recorded as `checked_at`; `None` for the current time.

    Returns:
        Each engine's status, keyed by engine, in `ENGINES` order.
    """
    env = _env(env)
    if not refresh:
        held = cached(env)
        if held is not None:
            return held
    result = _check_all(env, now)
    payload = {"engines": {name: item.to_json() for name, item in result.items()}}
    with contextlib.suppress(OSError):
        save_cache_file(cache_path(env), payload, env)
    return result
