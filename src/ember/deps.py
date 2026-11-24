"""The software Ember depends on: installed and newest versions, and status.

The components are listed in `ember/data/components.toml`. For each one Ember
reads the installed version on this machine and the newest version its source
offers, compares them and gives one status: `missing`, `check failed`,
`update available` or `current`, the first that applies.

Every command runs with `LC_ALL=C`, standard input from `/dev/null` and a
deadline of `TIMEOUT` seconds, in its own process group, which is killed whole
at the deadline. Every network read has a timeout of `TIMEOUT` seconds. A
component read that fails gives `check failed` with a reason, never an exception.

The last reads are cached in `dependencies.json` in Ember's cache folder.
Newest versions are read again when their cached read is `MAX_AGE` seconds old
or older, or when asked.

Every function takes an optional `env` mapping, which defaults to `os.environ`
and supplies `HOME`, the XDG variables and `PATH`.
"""

import contextlib
import dataclasses
import fcntl
import http.client
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import time
import tomllib
import urllib.request
from collections.abc import Callable, Collection, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from string import Formatter
from typing import Any

from ember import paths, skill
from ember.engines import status
from ember.web import USER_AGENT

TIMEOUT = 10
"""Seconds each command and each network read may take."""

MAX_AGE = 6 * 60 * 60
"""Seconds after which a cached newest version is read again."""

CACHE_NAME = "dependencies.json"
"""The cache's file name in Ember's cache folder."""

PYPI_URL = "https://pypi.org/pypi/{package}/json"
"""Where a Python package's newest version is read."""

MISSING = "missing"
CHECK_FAILED = "check failed"
UPDATE_AVAILABLE = "update available"
CURRENT = "current"
STATUSES = (MISSING, CHECK_FAILED, UPDATE_AVAILABLE, CURRENT)
"""Every component status, in the order they are decided."""

GROUPS = ("apps", "system", "libraries")
"""The component groups: engines and apps, system tools, Ember's libraries."""

INSTALLED_KINDS = ("apt", "command", "python", "skill")
"""How an installed version can be read."""

NEWEST_KINDS = ("apt", "apt-index", "json", "text", "pypi", "github", "none")
"""How a newest version can be read."""

_APT_NEWEST_KINDS = ("apt", "apt-index")

NEWEST_MODES = ("due", "always", "never")
"""When `read` reads newest versions."""

Fetch = Callable[[str, float], str]
"""Reads a URL with a timeout in seconds and returns the body as text."""

_ID = re.compile(r"[a-z0-9-]+")
_DOTTED = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_CANDIDATE = re.compile(r"^\s*Candidate:\s*(\S+)\s*$", re.MULTILINE)
_LINE_LIMIT = 200


class ComponentsError(Exception):
    """The component list cannot be read or is not valid."""


class ReadError(Exception):
    """A version could not be read or compared."""


@dataclass(frozen=True)
class Source:
    """Where one version of a component is read."""

    kind: str
    """One of `INSTALLED_KINDS` or `NEWEST_KINDS`."""

    package: str | None = None
    """The apt or Python package, for `apt`, `apt-index`, `python` and `pypi`."""

    engine: str | None = None
    """The engine whose CLI is run with `--version`, for `command`."""

    argv: tuple[str, ...] = ()
    """The command to run, for `command` without `engine`."""

    pattern: str | None = None
    """A regular expression whose group 1 is the version, for `command`."""

    url: str | None = None
    """The address read, for `apt-index`, `json` and `text`."""

    field: str | None = None
    """The JSON object's key that holds the version, for `json`."""

    commands: Mapping[str, str] = dataclasses.field(default_factory=dict)
    """Install commands by link state, for `skill`."""

    channel: str | None = None
    """Whose release channel fills `{channel}` in `url`, for `text`:
    `claude-code` reads Claude Code's `autoUpdatesChannel`."""

    repo: str | None = None
    """The owner/repository whose releases are read, for `github`."""


@dataclass(frozen=True)
class Method:
    """An install path pattern and its update command, or a dpkg lookup."""

    update: str
    path: str | None = None
    kind: str = "path"

    def to_json(self) -> dict[str, str]:
        """Return the method's component-table fields.

        Returns:
            The update template and path, or the dpkg kind.
        """
        if self.path is None:
            return {"kind": self.kind, "update": self.update}
        return {"path": self.path, "update": self.update}


@dataclass(frozen=True)
class Spec:
    """One component as `components.toml` lists it."""

    id: str
    label: str
    group: str
    installed: Source
    newest: Source
    update: str | None
    install: str | None
    update_note: str | None
    install_note: str | None
    methods: tuple[Method, ...] = ()
    updater: str | None = None
    updater_by_major: Mapping[str, str] = dataclasses.field(default_factory=dict)
    """Updater overrides by installed major version, before install methods."""


@dataclass(frozen=True)
class Component:
    """One component's status on this machine."""

    id: str
    """The component's id."""

    label: str
    """Its shown name."""

    group: str
    """One of `GROUPS`."""

    status: str
    """One of `STATUSES`."""

    installed: str | None
    """The installed version, or `None` when it was not read."""

    newest: str | None
    """The newest version, or `None` when it was not read."""

    error: str | None
    """Why the check failed: a read error or skill link state; `None` otherwise."""

    command: str | None
    """The update, install or skill repair command, or `None`."""

    command_kind: str | None
    """`update`, `install` or `None`, as `command`."""

    note: str | None
    """The status note, including an unidentified install method."""

    resolved_path: str | None = None
    """The engine launcher's target after following symlinks."""

    method: Method | None = None
    """The matched install method, or `None` when none matched."""

    installed_kind: str | None = None
    """The installed source kind."""

    newest_kind: str | None = None
    """The newest source kind; `none` has no newest reads."""

    link_state: str | None = None
    """The skill link state, in place of a version."""

    def to_json(self) -> dict[str, Any]:
        """Return the component as a JSON-ready dict.

        Returns:
            The report fields, including `link_state` for a skill.
        """
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "status": self.status,
            "installed": self.installed,
            "newest": self.newest,
            "error": self.error,
            "command": self.command,
            "command_kind": self.command_kind,
            "note": self.note,
            "resolved_path": self.resolved_path,
            "method": None if self.method is None else self.method.to_json(),
            **(
                {"link_state": self.link_state}
                if self.installed_kind == "skill"
                else {}
            ),
        }


@dataclass(frozen=True)
class DepsReport:
    """Every component's status, in the order `components.toml` lists them."""

    components: tuple[Component, ...]
    """The components."""

    installed_read_at: Mapping[str, float | None]
    """When each component's installed version was read, by id, in seconds
    since the epoch; `None` when it was never read."""

    newest_read_at: Mapping[str, float | None]
    """When each component's reported newest version was read, by id; `None`
    when none is reported."""

    def counts(self) -> int:
        """Return how many components need the owner.

        Returns:
            Non-current skills and other components that are missing or outdated.
        """
        return sum(
            item.status != CURRENT
            if item.installed_kind == "skill"
            else item.status in (UPDATE_AVAILABLE, MISSING)
            for item in self.components
        )

    def _needs_newest(self) -> list[str]:
        return [
            item.id
            for item in self.components
            if item.status != MISSING and item.newest_kind != "none"
        ]

    def oldest_newest_read(self) -> float | None:
        """Return the oldest newest-version read among installed components.

        Returns:
            The earliest `newest_read_at` of a component that is not missing,
            excluding newest kind `none`, or `None` when none was read.
        """
        times = [
            stamp
            for stamp in (self.newest_read_at.get(key) for key in self._needs_newest())
            if stamp is not None
        ]
        return min(times) if times else None

    def newest_read_complete(self) -> bool:
        """Return whether every installed component has a newest version read.

        Returns:
            True when every component that is not missing has a
            `newest_read_at`, excluding newest kind `none`.
        """
        return all(
            self.newest_read_at.get(key) is not None for key in self._needs_newest()
        )

    def to_json(self) -> dict[str, Any]:
        """Return the report as a JSON-ready dict.

        Returns:
            `components` as a list, both read-time mappings, `counts`,
            `newest_read_oldest` and `newest_read_complete`.
        """
        return {
            "components": [item.to_json() for item in self.components],
            "installed_read_at": dict(self.installed_read_at),
            "newest_read_at": dict(self.newest_read_at),
            "counts": self.counts(),
            "newest_read_oldest": self.oldest_newest_read(),
            "newest_read_complete": self.newest_read_complete(),
        }


# The component list


_REQUIRED = ("id", "label", "group", "installed", "newest")
_OPTIONAL = ("update", "install", "update_note", "install_note", "updater")
_SOURCE_FIELDS: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {
    ("installed", "apt"): (("package",), ()),
    ("installed", "command"): (("pattern",), ("engine", "argv")),
    ("installed", "python"): (("package",), ()),
    ("installed", "skill"): ((), ("commands",)),
    ("newest", "none"): ((), ()),
    ("newest", "apt"): (("package",), ()),
    ("newest", "apt-index"): (("url", "package"), ()),
    ("newest", "json"): (("url", "field"), ()),
    ("newest", "text"): (("url",), ("channel",)),
    ("newest", "pypi"): (("package",), ()),
    ("newest", "github"): (("repo",), ()),
}
"""For each (role, kind): the required fields and the optional ones."""


def _text(where: str, name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComponentsError(f"{where}: {name} must be a non-empty string")
    return value


def _source(where: str, role: str, value: object) -> Source:
    where = f"{where}.{role}"
    if not isinstance(value, dict):
        raise ComponentsError(f"{where} must be a table")
    kind = value.get("kind")
    kinds = INSTALLED_KINDS if role == "installed" else NEWEST_KINDS
    if not isinstance(kind, str) or kind not in kinds:
        raise ComponentsError(
            f"{where}: kind must be one of {', '.join(kinds)}, not {kind!r}"
        )
    required, optional = _SOURCE_FIELDS[(role, kind)]
    unknown = sorted(set(value) - {"kind", *required, *optional})
    if unknown:
        raise ComponentsError(f"{where}: unknown field {', '.join(unknown)}")
    for name in required:
        if name not in value:
            raise ComponentsError(f"{where}: missing field {name}")
        _text(where, name, value[name])
    if kind == "skill":
        commands = value.get("commands", {})
        states = {skill.MISSING, skill.BROKEN_LINK, skill.OTHER_LINK}
        if not isinstance(commands, dict) or set(commands) - states:
            raise ComponentsError(f"{where}: commands must map repairable link states")
        for state, command in commands.items():
            _text(where, state, command)
        return Source(kind=kind, commands=commands)
    if kind != "command":
        repo = value.get("repo")
        if repo is not None and not re.fullmatch(
            r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo
        ):
            raise ComponentsError(f"{where}: repo must be owner/repository")
        url = value.get("url")
        if url is not None and not url.startswith("https://"):
            raise ComponentsError(f"{where}: url must start with https://")
        channel = value.get("channel")
        if channel is not None and (
            channel not in CHANNELS or "{channel}" not in str(url)
        ):
            raise ComponentsError(
                f"{where}: channel must be one of {', '.join(CHANNELS)} "
                "with {channel} in url"
            )
        return Source(
            kind=kind,
            package=value.get("package"),
            url=url,
            field=value.get("field"),
            channel=channel,
            repo=repo,
        )
    if ("engine" in value) == ("argv" in value):
        raise ComponentsError(f"{where}: give exactly one of engine and argv")
    engine = value.get("engine")
    if engine is not None and engine not in status.ENGINES:
        raise ComponentsError(f"{where}: unknown engine {engine!r}")
    argv = value.get("argv", [])
    if engine is None and (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise ComponentsError(f"{where}: argv must be a non-empty list of strings")
    pattern = value["pattern"]
    try:
        groups = re.compile(pattern).groups
    except re.error as error:
        raise ComponentsError(f"{where}: pattern is not valid: {error}") from None
    if groups < 1:
        raise ComponentsError(f"{where}: pattern needs a group for the version")
    return Source(kind=kind, engine=engine, argv=tuple(argv), pattern=pattern)


def _fields(template: str) -> set[str]:
    fields = set()
    for _literal, field, format_spec, conversion in Formatter().parse(template):
        if field is not None:
            if field not in {"prefix", "package", "formula", "launcher"}:
                raise ValueError(f"unknown placeholder {field!r}")
            if format_spec or conversion:
                raise ValueError("placeholders cannot have formats or conversions")
            fields.add(field)
    return fields


def _path_pattern(pattern: str) -> re.Pattern[str]:
    captures = {
        "prefix": r".*",
        "package": r"(?:@[^/]+/[^/.][^/]*|[^/@.][^/]*)",
        "formula": r"[^/]+",
    }
    parts = []
    for literal, field, _format_spec, _conversion in Formatter().parse(pattern):
        parts.append(re.escape(literal).replace(r"\*", "[^/]*"))
        if field is not None:
            parts.append(f"(?P<{field}>{captures[field]})")
    return re.compile("^" + "".join(parts) + r"(?=/|$)")


def _methods(where: str, value: object) -> tuple[Method, ...]:
    if not isinstance(value, list):
        raise ComponentsError(f"{where}: methods must be a list of tables")
    methods = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ComponentsError(f"{where}: each method must be a table")
        kind = entry.get("kind", "path")
        if kind not in ("path", "dpkg"):
            raise ComponentsError(f"{where}: method kind must be path or dpkg")
        allowed = {"kind", "update"} | ({"path"} if kind == "path" else set())
        if set(entry) - allowed:
            raise ComponentsError(f"{where}: unknown method field")
        update = _text(where, "method update", entry.get("update"))
        pattern = (
            _text(where, "method path", entry.get("path")) if kind == "path" else None
        )
        try:
            fields = {"package"} if pattern is None else _fields(pattern)
            if pattern is not None:
                _path_pattern(pattern)
            if _fields(update) - (fields | {"launcher"}):
                raise ValueError("update uses a placeholder absent from the path")
        except (ValueError, KeyError, re.error) as error:
            raise ComponentsError(f"{where}: invalid method: {error}") from None
        methods.append(Method(update=update, path=pattern, kind=kind))
    return tuple(methods)


def _spec(index: int, value: object) -> Spec:
    where = f"components[{index}]"
    if not isinstance(value, dict):
        raise ComponentsError(f"{where} must be a table")
    unknown = sorted(
        set(value) - {*_REQUIRED, *_OPTIONAL, "methods", "updater_by_major"}
    )
    if unknown:
        raise ComponentsError(f"{where}: unknown field {', '.join(unknown)}")
    for name in _REQUIRED:
        if name not in value:
            raise ComponentsError(f"{where}: missing field {name}")
    ident = _text(where, "id", value["id"])
    if not _ID.fullmatch(ident):
        raise ComponentsError(f"{where}: id {ident!r} must match [a-z0-9-]+")
    where = f"component {ident}"
    label = _text(where, "label", value["label"])
    group = value["group"]
    if group not in GROUPS:
        raise ComponentsError(
            f"{where}: group must be one of {', '.join(GROUPS)}, not {group!r}"
        )
    installed = _source(where, "installed", value["installed"])
    newest = _source(where, "newest", value["newest"])
    if installed.kind == "skill" and newest.kind != "none":
        raise ComponentsError(f"{where}: a skill needs newest kind none")
    if newest.kind in _APT_NEWEST_KINDS and installed.kind != "apt":
        raise ComponentsError(
            f"{where}: an {newest.kind} newest read needs an apt installed read"
        )
    optional = {
        name: _text(where, name, value[name]) if name in value else None
        for name in _OPTIONAL
    }
    methods = _methods(where, value.get("methods", []))
    updater_by_major = value.get("updater_by_major", {})
    if not isinstance(updater_by_major, dict) or any(
        not isinstance(major, str) or not re.fullmatch(r"0|[1-9][0-9]*", major)
        for major in updater_by_major
    ):
        raise ComponentsError(f"{where}: updater_by_major must map major versions")
    for major, command in updater_by_major.items():
        _text(where, f"updater_by_major.{major}", command)
    if methods or optional["updater"] is not None or updater_by_major:
        if installed.engine is None or optional["updater"] is None:
            raise ComponentsError(f"{where}: methods need an engine and updater")
        if optional["update"] is not None:
            raise ComponentsError(f"{where}: give methods instead of update")
        for command in (optional["updater"], *updater_by_major.values()):
            try:
                updater = shlex.split(command or "")
            except ValueError as error:
                raise ComponentsError(f"{where}: invalid updater: {error}") from None
            if len(updater) < 2 or updater[0] != installed.engine:
                raise ComponentsError(
                    f"{where}: updater must name the engine and arguments"
                )
    return Spec(
        id=ident,
        label=label,
        group=group,
        installed=installed,
        newest=newest,
        update=optional["update"],
        install=optional["install"],
        update_note=optional["update_note"],
        install_note=optional["install_note"],
        methods=methods,
        updater=optional["updater"],
        updater_by_major=updater_by_major,
    )


def parse_components(text: str) -> tuple[Spec, ...]:
    """Parse and check a component list.

    Args:
        text: The TOML text.

    Returns:
        The components, in the order listed.

    Raises:
        ComponentsError: The text is not TOML or does not follow the schema.
    """
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ComponentsError(f"components.toml is not valid TOML: {error}") from None
    unknown = sorted(set(document) - {"components"})
    if unknown:
        raise ComponentsError(f"components.toml: unknown key {', '.join(unknown)}")
    items = document.get("components")
    if not isinstance(items, list) or not items:
        raise ComponentsError("components.toml needs a [[components]] list")
    specs = tuple(_spec(index, item) for index, item in enumerate(items))
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ComponentsError(f"components.toml lists {spec.id} twice")
        seen.add(spec.id)
    return specs


def components_text() -> str:
    """Return the text of the packaged component list.

    Returns:
        The content of `ember/data/components.toml`.

    Raises:
        ComponentsError: The file cannot be read.
    """
    try:
        return (
            resources.files("ember")
            .joinpath("data", "components.toml")
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError) as error:
        raise ComponentsError(f"components.toml cannot be read: {error}") from None


def load_components() -> tuple[Spec, ...]:
    """Read and check the packaged component list.

    Returns:
        The components, in the order listed.

    Raises:
        ComponentsError: The list cannot be read or is not valid.
    """
    return parse_components(components_text())


# Commands and network reads


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:_LINE_LIMIT]
    return "(no output)"


def run_command(
    argv: list[str], env: Mapping[str, str], deadline: float = TIMEOUT
) -> tuple[int, str, str]:
    """Run a read command in its own process group with a deadline.

    The command runs with `LC_ALL=C` and standard input from `/dev/null`. At
    the deadline its whole process group is killed.

    Args:
        argv: The command and its arguments.
        env: The command's environment, before `LC_ALL=C` is added.
        deadline: Seconds the command may run.

    Returns:
        The exit code, the standard output and the standard error.

    Raises:
        ReadError: The command cannot run or does not finish by the deadline.
    """
    shown = shlex.join([Path(argv[0]).name, *argv[1:]])
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={**env, "LC_ALL": "C"},
            start_new_session=True,
        )
    except OSError as error:
        raise ReadError(f"{shown} could not run: {error.strerror or error}") from None
    try:
        out, err = process.communicate(timeout=deadline)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ReadError(f"{shown} did not finish within {deadline:g} seconds") from None
    return process.returncode, out, err


def fetch_text(url: str, timeout: float) -> str:
    """GET `url` and return its body as text.

    Args:
        url: The address.
        timeout: Seconds the request may take.

    Returns:
        The body, decoded as UTF-8.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH", os.defpath))


def _tool(name: str, env: Mapping[str, str]) -> str:
    found = _which(name, env)
    if found is None:
        raise ReadError(f"{name} is not on PATH")
    return found


# Installed versions


@dataclass(frozen=True)
class _Installed:
    version: str | None
    missing: bool
    error: str | None
    read_at: float
    link_state: str | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "missing": self.missing,
            "error": self.error,
            "read_at": self.read_at,
            **(
                {"link_state": self.link_state, "note": self.note}
                if self.link_state is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class _Newest:
    version: str
    read_at: float
    channel: str | None = None
    """The release channel the version was read from, for a source with one."""

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"version": self.version, "read_at": self.read_at}
        if self.channel is not None:
            result["channel"] = self.channel
        return result


def _installed_apt(package: str, env: Mapping[str, str], deadline: float) -> str | None:
    binary = _tool("dpkg-query", env)
    code, out, err = run_command(
        [binary, "-W", "-f=${db:Status-Status}\t${Version}", package], env, deadline
    )
    if code == 1:
        return None
    if code != 0:
        raise ReadError(f"dpkg-query exited {code}: {_first_line(err or out)}")
    state, tab, version = out.strip("\n").partition("\t")
    if state.strip() != "installed":
        return None
    if not tab or not version.strip():
        raise ReadError(f"dpkg-query printed no version for {package}")
    return version.strip()


def _installed_command(
    source: Source, env: Mapping[str, str], deadline: float
) -> str | None:
    if source.engine is not None:
        binary = status.locate(source.engine, env)
        argv = [] if binary is None else [binary, "--version"]
        run_env = status.probe_env(source.engine, env)
    else:
        binary = _which(source.argv[0], env)
        argv = [] if binary is None else [binary, *source.argv[1:]]
        run_env = dict(env)
    if not argv:
        return None
    shown = shlex.join([Path(argv[0]).name, *argv[1:]])
    code, out, err = run_command(argv, run_env, deadline)
    if code != 0:
        raise ReadError(f"{shown} exited {code}: {_first_line(err or out)}")
    match = re.search(source.pattern or "", out.strip(), re.MULTILINE)
    if match is None or not match.group(1):
        raise ReadError(f"{shown} printed {_first_line(out)!r}, with no version")
    return match.group(1)


def _installed_python(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _read_installed(
    spec: Spec, env: Mapping[str, str], deadline: float, now: float
) -> _Installed:
    source = spec.installed
    if source.kind == "skill":
        current = skill.status(env, skill.source_candidates())
        note = None
        if current.source is None:
            note = "Reinstall Ember."
        elif current.state in (skill.FOLDER, skill.FILE):
            note = f"Move {current.link}, then run emb skill install."
        error = (
            None if current.state in (skill.INSTALLED, skill.MISSING) else current.state
        )
        return _Installed(
            None, current.state == skill.MISSING, error, now, current.state, note
        )
    try:
        if source.kind == "apt":
            version = _installed_apt(source.package or "", env, deadline)
        elif source.kind == "command":
            version = _installed_command(source, env, deadline)
        else:
            version = _installed_python(source.package or "")
    except ReadError as error:
        return _Installed(None, False, str(error), now)
    return _Installed(version, version is None, None, now)


# Newest versions


def _newest_apt(package: str, env: Mapping[str, str], deadline: float) -> str:
    binary = _tool("apt-cache", env)
    code, out, err = run_command([binary, "policy", package], env, deadline)
    if code != 0:
        raise ReadError(f"apt-cache policy exited {code}: {_first_line(err or out)}")
    match = _CANDIDATE.search(out)
    if match is None:
        raise ReadError(f"apt-cache policy {package} shows no candidate")
    if match.group(1) == "(none)":
        raise ReadError(f"apt-cache policy {package} shows the candidate (none)")
    return match.group(1)


def fetch_version_text(url: str, fetch: Fetch) -> str:
    """Fetch version source text with the dependency-read timeout.

    Raises:
        ReadError: The request failed or its body could not be decoded.
    """
    try:
        return fetch(url, TIMEOUT)
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise ReadError(f"{url}: {error}") from None


def _json_field(url: str, body: str, keys: tuple[str, ...]) -> str:
    try:
        value: object = json.loads(body)
    except ValueError:
        raise ReadError(f"{url} did not return JSON") from None
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ReadError(f"{url} has no {'.'.join(keys)}")
        value = value[key]
    if not isinstance(value, str) or not value.strip():
        raise ReadError(f"{url}: {'.'.join(keys)} is not a version string")
    return value.strip()


def _read_newest(
    spec: Spec, env: Mapping[str, str], deadline: float, fetch: Fetch
) -> str:
    source = spec.newest
    if source.kind == "apt":
        return _newest_apt(source.package or "", env, deadline)
    if source.kind == "apt-index":
        from ember import updates

        return updates.newest_version(
            env,
            url=source.url or "",
            package=source.package or "",
            fetch=fetch,
            deadline=deadline,
        )
    if source.kind == "pypi":
        url = PYPI_URL.format(package=source.package)
        return _json_field(url, fetch_version_text(url, fetch), ("info", "version"))
    if source.kind == "github":
        return _newest_github(source.repo or "", fetch)
    url = source.url or ""
    if source.channel == "claude-code":
        url = url.replace("{channel}", claude_channel(env))
    body = fetch_version_text(url, fetch)
    if source.kind == "json":
        return _json_field(url, body, (source.field or "",))
    version = body.strip()
    if not version or len(version.split()) != 1:
        raise ReadError(f"{url} did not return a single version")
    return version


def _newest_github(repo: str, fetch: Fetch) -> str:
    """Return the highest dotted release version, excluding drafts and prereleases.

    Args:
        repo: The GitHub owner/repository.
        fetch: Reads a URL as text.

    Returns:
        The highest version without its optional leading `v`.

    Raises:
        ReadError: The releases cannot be read or contain no stable versions.
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    try:
        releases = json.loads(fetch_version_text(url, fetch))
    except ValueError:
        raise ReadError(f"{url} did not return JSON") from None
    if not isinstance(releases, list):
        raise ReadError(f"{url} did not return a release list")
    versions: list[str] = []
    for release in releases:
        if not isinstance(release, dict):
            raise ReadError(f"{url} contains an invalid release")
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if isinstance(tag, str):
            version = tag.removeprefix("v")
            if _DOTTED.fullmatch(version):
                versions.append(version)
    if not versions:
        raise ReadError(f"{url} has no stable versioned releases")
    return max(versions, key=dotted)


CHANNELS = ("claude-code",)
"""The release channels a newest-version url can follow."""

CLAUDE_CHANNELS = ("latest", "stable")
"""Claude Code's release channels that have a newest-version file."""


def claude_channel(env: Mapping[str, str]) -> str:
    """Return the release channel Claude Code updates from.

    Claude Code reads `autoUpdatesChannel` from `settings.json` in its
    configuration folder (`CLAUDE_CONFIG_DIR`, else `~/.claude`); without it,
    or with a value that has no newest-version file, it is `latest`.
    """
    folder = env.get("CLAUDE_CONFIG_DIR") or str(paths.home_dir(env) / ".claude")
    try:
        settings = json.loads((Path(folder) / "settings.json").read_text("utf-8"))
    except OSError, UnicodeError, ValueError:
        return "latest"
    value = settings.get("autoUpdatesChannel") if isinstance(settings, dict) else None
    return value if value in CLAUDE_CHANNELS else "latest"


# Comparison


def dotted(version: str) -> tuple[int, ...]:
    """Return a dotted version as numbers for comparison.

    An optional leading `v` is removed and trailing `.0` parts are ignored, so
    `v1.2.0` and `1.2` are equal.

    Args:
        version: The version text.

    Returns:
        The numbers, without trailing zeros.

    Raises:
        ReadError: The version is not dotted numbers.
    """
    text = version.removeprefix("v")
    if not _DOTTED.fullmatch(text):
        raise ReadError(f"{version!r} is not a dotted version")
    parts = [int(part) for part in text.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def validate_debian_version(
    version: str, env: Mapping[str, str], deadline: float = TIMEOUT
) -> None:
    """Raise `ReadError` if `dpkg` cannot validate a version string."""
    if "\0" in version:
        raise ReadError("Debian version contains a NUL character")
    binary = _tool("dpkg", env)
    code, out, err = run_command(
        [binary, "--validate-version", "--", version], env, deadline
    )
    if code != 0:
        raise ReadError(f"invalid Debian version: {_first_line(err or out)}")


def debian_older(
    installed: str, newest: str, env: Mapping[str, str], deadline: float = TIMEOUT
) -> bool:
    """Return whether `installed` precedes `newest` in Debian version order."""
    binary = _tool("dpkg", env)
    code, out, err = run_command(
        [binary, "--compare-versions", installed, "lt", newest], env, deadline
    )
    if code not in (0, 1):
        raise ReadError(
            f"dpkg --compare-versions exited {code}: {_first_line(err or out)}"
        )
    return code == 0


def _older(
    spec: Spec, installed: str, newest: str, env: Mapping[str, str], deadline: float
) -> bool:
    if spec.newest.kind in _APT_NEWEST_KINDS:
        return debian_older(installed, newest, env, deadline)
    return dotted(installed) < dotted(newest)


# Cache


def cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path of the dependency cache.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `dependencies.json` in Ember's cache folder.
    """
    return paths.cache_dir(env) / CACHE_NAME


def _installed_from_json(value: object) -> _Installed | None:
    if not isinstance(value, dict):
        return None
    version, missing, error = (
        value.get("version"),
        value.get("missing"),
        value.get("error"),
    )
    read_at = status.finite_number(value.get("read_at"))
    if read_at is None or not isinstance(missing, bool):
        return None
    if not (version is None or isinstance(version, str)):
        return None
    if not (error is None or isinstance(error, str)):
        return None
    link_state, note = value.get("link_state"), value.get("note")
    if not (link_state is None or isinstance(link_state, str)):
        return None
    if not (note is None or isinstance(note, str)):
        return None
    if not missing and version is None and error is None and link_state is None:
        return None
    return _Installed(
        None if missing else version, missing, error, read_at, link_state, note
    )


def _newest_from_json(value: object) -> _Newest | None:
    if not isinstance(value, dict):
        return None
    version = value.get("version")
    read_at = status.finite_number(value.get("read_at"))
    channel = value.get("channel")
    if not isinstance(version, str) or not version or read_at is None:
        return None
    if channel is not None and not isinstance(channel, str):
        return None
    return _Newest(version, read_at, channel)


def _load_cache(
    env: Mapping[str, str],
) -> dict[str, tuple[_Installed | None, _Newest | None]]:
    try:
        path = cache_path(env)
    except OSError, KeyError:
        return {}
    value = status.load_cache_file(path)
    if not isinstance(value, dict) or not isinstance(value.get("components"), dict):
        return {}
    held: dict[str, tuple[_Installed | None, _Newest | None]] = {}
    for key, entry in value["components"].items():
        if isinstance(key, str) and isinstance(entry, dict):
            held[key] = (
                _installed_from_json(entry.get("installed")),
                _newest_from_json(entry.get("newest")),
            )
    return held


@contextlib.contextmanager
def _cache_write_lock(env: Mapping[str, str]) -> Generator[Path]:
    path = cache_path(env)
    paths.ensure_private_dir(path.parent, root=paths.cache_dir(env))
    fd = paths.open_private_file(path.with_suffix(".lock"))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def _save_cache(
    entries: dict[str, tuple[_Installed | None, _Newest | None]],
    env: Mapping[str, str],
    preserve: set[str],
) -> None:
    with contextlib.suppress(OSError, KeyError), _cache_write_lock(env) as path:
        if preserve:
            # A skipped component may have been refreshed by another reader.
            held = _load_cache(env)
            entries = entries | {key: held[key] for key in preserve if key in held}
        components: dict[str, dict[str, Any]] = {}
        for key, (installed, newest) in entries.items():
            entry: dict[str, Any] = {}
            if installed is not None:
                entry["installed"] = installed.to_json()
            if newest is not None:
                entry["newest"] = newest.to_json()
            components[key] = entry
        status.save_cache_file(path, {"components": components}, env)


# Status


@dataclass(frozen=True)
class _Options:
    env: Mapping[str, str]
    installed: bool
    newest: str
    now: float
    fetch: Fetch
    deadline: float


@dataclass(frozen=True)
class _Outcome:
    component: Component
    installed: _Installed | None
    newest: _Newest | None


def _due(held: _Newest | None, options: _Options) -> bool:
    if options.newest == "always":
        return True
    if options.newest == "never":
        return False
    return held is None or not 0 <= options.now - held.read_at < MAX_AGE


def _row(
    spec: Spec,
    state: str,
    installed: str | None,
    newest: str | None,
    error: str | None,
    *,
    link_state: str | None = None,
    note: str | None = None,
) -> Component:
    command = kind = None
    if spec.installed.kind == "skill":
        if note is None:
            command = spec.installed.commands.get(link_state or "")
            kind = "install"
    elif state == UPDATE_AVAILABLE:
        command, kind = spec.update, "update"
        note = spec.update_note
    elif state == MISSING:
        command, kind = spec.install, "install"
        note = spec.install_note
    if command is None:
        kind = None
    else:
        note = None
    return Component(
        id=spec.id,
        label=spec.label,
        group=spec.group,
        status=state,
        installed=installed,
        newest=newest,
        error=error,
        command=command,
        command_kind=kind,
        note=note,
        installed_kind=spec.installed.kind,
        newest_kind=spec.newest.kind,
        link_state=link_state,
    )


def _evaluate_versions(
    spec: Spec,
    held: tuple[_Installed | None, _Newest | None],
    options: _Options,
) -> _Outcome:
    held_installed, held_newest = held
    if options.installed:
        found = _read_installed(spec, options.env, options.deadline, options.now)
    else:
        found = held_installed
    if spec.newest.kind == "none":
        state = CHECK_FAILED
        error = "installed version: not read and nothing cached"
        if found is not None:
            state = (
                MISSING if found.missing else CHECK_FAILED if found.error else CURRENT
            )
            error = found.error
        row = _row(
            spec,
            state,
            None if found is None else found.version,
            None,
            error,
            link_state=None if found is None else found.link_state,
            note=None if found is None else found.note,
        )
        return _Outcome(row, found, None)
    if found is not None and found.missing:
        newest = held_newest.version if held_newest else None
        row = _row(spec, MISSING, None, newest, None)
        return _Outcome(row, found, held_newest)
    errors: list[str] = []
    if found is None:
        errors.append("installed version: not read and nothing cached")
    elif found.error is not None:
        errors.append(f"installed version: {found.error}")
    channel = claude_channel(options.env) if spec.newest.channel else None
    if held_newest is not None and held_newest.channel != channel:
        # A version cached from another release channel says nothing about
        # this one.
        held_newest = None
    reported = held_newest
    if _due(held_newest, options):
        try:
            version = _read_newest(spec, options.env, options.deadline, options.fetch)
        except ReadError as error:
            errors.append(f"newest version: {error}")
            reported = None
        else:
            reported = _Newest(version, options.now, channel)
    elif held_newest is None:
        errors.append("newest version: not read and nothing cached")
    installed = None if found is None else found.version
    newest = None if reported is None else reported.version
    if not errors and installed is not None and newest is not None:
        try:
            older = _older(spec, installed, newest, options.env, options.deadline)
        except ReadError as error:
            errors.append(f"comparison: {error}")
        else:
            if older:
                row = _row(spec, UPDATE_AVAILABLE, installed, newest, None)
                return _Outcome(row, found, reported)
            # A source can report an older release than the installed one,
            # such as apt lists read before a newer package was installed.
            with contextlib.suppress(ReadError):
                if installed != newest and _older(
                    spec, newest, installed, options.env, options.deadline
                ):
                    newest = installed
            return _Outcome(
                _row(spec, CURRENT, installed, newest, None), found, reported
            )
    row = _row(spec, CHECK_FAILED, installed, newest, "; ".join(errors))
    return _Outcome(row, found, reported)


def _dpkg_owner(resolved: Path, options: _Options) -> str | None:
    if not options.installed:
        return None
    try:
        binary = _tool("dpkg-query", options.env)
        code, out, _err = run_command(
            [binary, "-S", str(resolved)], options.env, options.deadline
        )
    except ReadError:
        return None
    lines = out.strip().splitlines()
    if code != 0 or len(lines) != 1:
        return None
    package, separator, owned = lines[0].partition(": ")
    if (
        not separator
        or owned != str(resolved)
        or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?", package)
    ):
        return None
    return package


def _update_command(
    spec: Spec,
    launcher: str,
    resolved: Path | None,
    method_path: Path | None,
    options: _Options,
    installed: str | None,
) -> tuple[str, Method | None]:
    command = shlex.join([launcher, *shlex.split(spec.updater or "")[1:]])
    matched = None
    folder = Path(launcher).parent if resolved is None else resolved.parent
    # Path methods retain their listed order; dpkg is always last.
    for method in sorted(spec.methods, key=lambda method: method.kind == "dpkg"):
        if resolved is None:
            break
        values = {"launcher": launcher}
        if method.path is not None:
            candidate_path = method_path or resolved
            candidate = str(candidate_path)
            if method.path.startswith("~/"):
                try:
                    relative = candidate_path.relative_to(
                        paths.home_dir(options.env).resolve()
                    )
                except OSError, RuntimeError, ValueError:
                    continue
                candidate = "~/" + str(relative)
            match = _path_pattern(method.path).match(candidate)
            if match is None:
                continue
            values.update(match.groupdict())
            if "prefix" in values:
                values["prefix"] = values["prefix"] or "/"
            if method.path == "{prefix}/lib/node_modules/{package}":
                folder = Path(values["prefix"]) / "lib" / "node_modules"
        else:
            package = _dpkg_owner(resolved, options)
            if package is None:
                continue
            values["package"] = package
        command = method.update.format_map(
            {name: shlex.quote(value) for name, value in values.items()}
        )
        matched = method
        break
    if installed is not None and spec.updater_by_major:
        with contextlib.suppress(ReadError):
            updater = spec.updater_by_major.get(str(dotted(installed)[0]))
            if updater is not None:
                command = shlex.join([launcher, *shlex.split(updater)[1:]])
                folder = Path(launcher).parent if resolved is None else resolved.parent
    if not os.access(folder, os.W_OK) and shlex.split(command)[0] != "sudo":
        command = "sudo " + command
    return command, matched


def _shim_target(resolved: Path) -> Path:
    """Return a script's normalized target path without following its symlinks."""
    limit = 64 * 1024
    try:
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return resolved
        with resolved.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit or not content.startswith(b"#!"):
            return resolved
        text = content.decode("utf-8")
        for match in re.finditer(r"""(["'])\$basedir/([^"'\r\n]+)\1""", text):
            relative = match.group(2)
            if not Path(relative).is_absolute() and "/node_modules/" in "/" + relative:
                target = Path(os.path.abspath(resolved.parent / relative))
                target.resolve(strict=True)
                return target
    except OSError, UnicodeError, RuntimeError:
        pass
    return resolved


def _evaluate(
    spec: Spec,
    held: tuple[_Installed | None, _Newest | None],
    options: _Options,
) -> _Outcome:
    outcome = _evaluate_versions(spec, held, options)
    if spec.installed.engine is None:
        return outcome
    launcher = status.locate(spec.installed.engine, options.env)
    if launcher is None:
        row = _row(spec, MISSING, None, outcome.component.newest, None)
        return replace(outcome, component=row)
    resolved = None
    with contextlib.suppress(OSError, RuntimeError):
        resolved = Path(launcher).resolve(strict=True)
    method_path = resolved
    if resolved is not None:
        target = _shim_target(resolved)
        with contextlib.suppress(OSError, RuntimeError):
            resolved, method_path = target.resolve(strict=True), target
    row = replace(
        outcome.component, resolved_path=None if resolved is None else str(resolved)
    )
    if spec.updater is not None:
        command, method = _update_command(
            spec, launcher, resolved, method_path, options, row.installed
        )
        row = replace(row, method=method)
        if row.status == UPDATE_AVAILABLE:
            row = replace(
                row,
                command=command,
                command_kind="update",
                note=(
                    "Ember could not tell how the tool was installed."
                    if method is None
                    else None
                ),
            )
    return replace(outcome, component=row)


def read(
    env: Mapping[str, str] | None = None,
    *,
    installed: bool = True,
    newest: str = "due",
    now: float | None = None,
    fetch: Fetch | None = None,
    deadline: float = TIMEOUT,
    components: tuple[Spec, ...] | None = None,
    exclude: Collection[str] = (),
) -> DepsReport:
    """Read the selected components' versions and status.

    Reads that ran are written to the cache; a newest read that failed is not,
    so the next due read tries again. A cache that cannot be read is treated
    as empty; one that cannot be written is left as it was.

    Args:
        env: The environment; `None` reads `os.environ`.
        installed: Read installed versions; False uses the cached ones.
        newest: `due` reads newest versions whose cached read is missing or
            `MAX_AGE` seconds old or older, `always` reads them all, `never`
            uses the cache.
        now: The time recorded for the reads; `None` for the current time.
        fetch: Reads a URL as text; `None` uses `fetch_text`.
        deadline: Seconds each command may run.
        components: The component list; `None` reads the packaged list.
        exclude: Component IDs to skip for this read, keeping their cache entries.

    Returns:
        The report, in the order of the component list.

    Raises:
        ValueError: `newest` is not one of `NEWEST_MODES`.
        ComponentsError: The packaged component list cannot be read or is not
            valid.
    """
    if newest not in NEWEST_MODES:
        raise ValueError(f"newest must be one of {', '.join(NEWEST_MODES)}")
    specs = load_components() if components is None else components
    skipped = {spec.id for spec in specs if spec.id in exclude}
    selected = tuple(spec for spec in specs if spec.id not in skipped)
    env = os.environ if env is None else env
    options = _Options(
        env=env,
        installed=installed,
        newest=newest,
        now=time.time() if now is None else now,
        fetch=fetch_text if fetch is None else fetch,
        deadline=deadline,
    )
    held = _load_cache(env)
    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
        outcomes = list(
            pool.map(
                lambda spec: _evaluate(spec, held.get(spec.id, (None, None)), options),
                selected,
            )
        )
    entries = {
        spec.id: (
            outcome.installed,
            None
            if spec.newest.kind == "none"
            else outcome.newest or held.get(spec.id, (None, None))[1],
        )
        for spec, outcome in zip(selected, outcomes, strict=True)
    }
    if installed or newest != "never":
        _save_cache(entries, env, preserve=skipped)
    return DepsReport(
        components=tuple(outcome.component for outcome in outcomes),
        installed_read_at={
            outcome.component.id: None
            if outcome.installed is None
            else outcome.installed.read_at
            for outcome in outcomes
        },
        newest_read_at={
            outcome.component.id: None
            if outcome.newest is None
            else outcome.newest.read_at
            for outcome in outcomes
        },
    )
