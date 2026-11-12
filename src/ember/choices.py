"""Standing settings, one-time choices, run claims and their resolution.

Each key resolves to the first of: the request layer, the claimed one-time
copy, the standing setting, then the key's default. Values passed to these
functions are typed: strings, lists of strings, and objects for object keys.

Every function takes an optional `env` mapping, passed to `ember.paths`, and an
optional `now` in seconds since the epoch, which defaults to the current time.

The worker menu reads each engine's last checked status from the status cache
of `ember.engines.status`; it never runs an engine.
"""

import json
import os
import re
import shlex
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ember import db, skill
from ember.engines import status

EX_USAGE = 64

ONCE_TTL = 24 * 60 * 60
"""Seconds from `set_at` until a pending one-time choice expires."""

CLAIM_TTL = 7 * 24 * 60 * 60
"""Seconds from the claim time until a claim reads as `gone`."""

REQUEST = "request"
ONCE = "one-time"
SETTING = "setting"
DEFAULT = "default"

ENGINES = ("claude", "codex", "opencode")
SANDBOXED = ("codex", "opencode", "claude")
"""The engines with a `sandbox.<engine>` switch, in display order."""
MENU_KEYS = ("single.writer", "single.reviewer")
SAME = "same"
"""The value of a derived worker row that follows its single-task row."""
DERIVED_KEYS = {
    "gapcheck.reviewer": "single.reviewer",
    "flow.writer": "single.writer",
    "flow.reviewer": "single.reviewer",
}
"""Worker rows that follow a single-task row while set to `same`."""

_ID = re.compile(r"[a-z0-9][a-z0-9-]{5,63}")
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_OPENCODE_PROVIDER = r"~?[A-Za-z0-9][A-Za-z0-9._-]*"
_OPENCODE_MODEL = r"~?[A-Za-z0-9][A-Za-z0-9._:-]*(?:/~?[A-Za-z0-9][A-Za-z0-9._:-]*)*"
_EFFORT = r"[a-z]+"
_SHORTLIST_ENTRY = {
    "claude": re.compile(rf"(?P<model>{_NAME})@(?P<effort>{_EFFORT})"),
    "codex": re.compile(rf"(?P<model>{_NAME})@(?P<effort>{_EFFORT})"),
    "opencode": re.compile(
        rf"(?P<model>{_OPENCODE_PROVIDER}/{_OPENCODE_MODEL})(?:@(?P<effort>{_EFFORT}))?"
    ),
}
_MENU_MODEL = {
    "claude": re.compile(_NAME),
    "codex": re.compile(_NAME),
    "opencode": re.compile(rf"{_OPENCODE_PROVIDER}/{_OPENCODE_MODEL}"),
}


OPENCODE_ID_FORMAT = "opencode_id_format"
"""Marker stored as 1 when OpenCode model ids include their integration."""


def full_opencode_id(model: str) -> str:
    """Convert a legacy stored OpenRouter id, retaining an existing prefix."""
    return model if model.startswith("openrouter/") else f"openrouter/{model}"


def _convert_stored_choice(name: str, value: object) -> object:
    """Convert OpenCode selections in one unmarked settings value."""
    if name == "shortlist.opencode" and isinstance(value, list):
        return [
            full_opencode_id(item) if isinstance(item, str) else item for item in value
        ]
    if name == "panel.members" and isinstance(value, list):
        return [_convert_stored_choice("single.writer", item) for item in value]
    if (
        name in (*MENU_KEYS, *DERIVED_KEYS)
        and isinstance(value, str)
        and value.startswith("opencode:")
    ):
        return "opencode:" + full_opencode_id(value.removeprefix("opencode:"))
    return value


def _convert_stored_object(value: object) -> object:
    if not isinstance(value, dict):
        return value
    return {name: _convert_stored_choice(name, item) for name, item in value.items()}


def _legacy_ids(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM meta WHERE key = ?", (OPENCODE_ID_FORMAT,)
        ).fetchone()
        is None
    )


def _migrate_ids(conn: sqlite3.Connection) -> bool:
    """Convert every settings layer and its marker within the caller's transaction."""
    if not _legacy_ids(conn):
        return False
    for name, text in conn.execute("SELECT key, value FROM settings").fetchall():
        try:
            value = _convert_stored_choice(name, json.loads(text))
        except ValueError:
            continue
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?", (_canonical(value), name)
        )
    for table, column in (("once", "value"), ("claims", "copy")):
        for ident, text in conn.execute(f"SELECT id, {column} FROM {table}").fetchall():
            if text is None:
                continue
            try:
                value = _convert_stored_object(json.loads(text))
            except ValueError:
                continue
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE id = ?",
                (_canonical(value), ident),
            )
    conn.execute("INSERT INTO meta (key, value) VALUES (?, '1')", (OPENCODE_ID_FORMAT,))
    return True


class InvalidChoiceError(ValueError):
    """A key, value, id or argument is not valid."""

    exit_code = EX_USAGE


Validator = Callable[[object], None]
Default = Callable[[Mapping[str, str] | None], object]


@dataclass(frozen=True)
class Key:
    """One settings key.

    Attributes:
        name: The key.
        layers: The layers that may hold it, besides the default.
        validate: Raises `InvalidChoiceError` when a value's syntax is not valid.
        default: Returns the default for an environment.
        is_list: Whether the value is a list of strings.
        allowed: The allowed values, or `None` when they follow a pattern.
        pattern: A description of the allowed values.
    """

    name: str
    layers: frozenset[str]
    validate: Validator
    default: Default
    is_list: bool = False
    allowed: tuple[str, ...] | None = None
    pattern: str = ""


def _fail(message: str) -> None:
    raise InvalidChoiceError(message)


def _enum(*values: str) -> Validator:
    def validate(value: object) -> None:
        if not isinstance(value, str) or value not in values:
            _fail(f"{value!r} is not one of {', '.join(values)}")

    return validate


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise InvalidChoiceError(f"{value!r} is not a list of strings")
    return value


def shortlist_models(engine: str, entries: Iterable[str]) -> dict[str, str | None]:
    """Map each shortlisted model to its effort.

    Args:
        engine: `claude`, `codex` or `opencode`.
        entries: The shortlist entries, already valid.

    Returns:
        Model to effort; the effort is `None` for an OpenCode entry without one.
    """
    result: dict[str, str | None] = {}
    for entry in entries:
        match = _SHORTLIST_ENTRY[engine].fullmatch(entry)
        if match:
            result[match["model"]] = match["effort"]
    return result


def _shortlist(engine: str) -> Validator:
    def validate(value: object) -> None:
        seen: set[str] = set()
        for entry in _string_list(value):
            match = _SHORTLIST_ENTRY[engine].fullmatch(entry)
            if match is None:
                _fail(f"{entry!r} is not a valid {engine} shortlist entry")
                return
            if match["model"] in seen:
                _fail(f"{match['model']!r} is listed more than once")
            seen.add(match["model"])

    return validate


def _split_menu(value: str) -> tuple[str, str] | None:
    engine, colon, model = value.partition(":")
    if not colon or engine not in ENGINES:
        return None
    if _MENU_MODEL[engine].fullmatch(model) is None:
        return None
    return engine, model


def _menu_value(value: object) -> None:
    if not isinstance(value, str):
        _fail(f"{value!r} is not a string")
        return
    if value != "ask" and _split_menu(value) is None:
        _fail(f"{value!r} is not ask or <engine>:<model>")


def _derived_value(value: object) -> None:
    if not isinstance(value, str):
        _fail(f"{value!r} is not a string")
        return
    if value != SAME and _split_menu(value) is None:
        _fail(f"{value!r} is not same or <engine>:<model>")


def _panel_members(value: object) -> None:
    """Validate the saved panel member syntax, count and uniqueness."""
    entries = _string_list(value)
    seen: set[str] = set()
    for entry in entries:
        if _split_menu(entry) is None:
            _fail(f"{entry!r} is not <engine>:<model>")
        if entry in seen:
            _fail(f"{entry!r} is listed more than once")
        seen.add(entry)
    if len(entries) > 8:
        _fail("at most 8 members")


def _absolute_paths(value: object) -> None:
    items = _string_list(value)
    for item in items:
        if not os.path.isabs(item):
            _fail(f"{item!r} is not an absolute path")
    if len(set(items)) != len(items):
        _fail("a path is listed more than once")


def _command_line(value: object) -> None:
    if not isinstance(value, str):
        _fail(f"{value!r} is not a string")
        return
    try:
        words = shlex.split(value)
    except ValueError as error:
        raise InvalidChoiceError(
            f"{value!r} is not a valid command line: {error}"
        ) from None
    if not words:
        _fail("the command line is empty")


def _worktree_prepare(value: object) -> None:
    """Validate checkout paths and their shell commands."""
    if not isinstance(value, dict):
        raise InvalidChoiceError("not a JSON object of checkout paths to commands")
    seen: set[Path] = set()
    for checkout, command in value.items():
        if not isinstance(checkout, str) or not os.path.isabs(checkout):
            raise InvalidChoiceError(f"{checkout!r} is not an absolute checkout path")
        if not isinstance(command, str):
            raise InvalidChoiceError(f"command for {checkout!r} is not a string")
        try:
            resolved = Path(checkout).resolve()
        except (OSError, ValueError) as error:
            raise InvalidChoiceError(f"cannot resolve {checkout!r}: {error}") from error
        if resolved in seen:
            raise InvalidChoiceError(
                f"{checkout!r} resolves to a checkout listed twice"
            )
        seen.add(resolved)


def _prepare_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse repeated JSON keys before they are reduced to a mapping."""
    value = dict(pairs)
    if len(value) != len(pairs):
        raise InvalidChoiceError("worktree.prepare: a checkout path is listed twice")
    return value


def _constant(value: object) -> Default:
    return lambda env: value


def _sandbox_default(engine: str) -> Default:
    """Return the default of `sandbox.<engine>`.

    The default is `on` when the engine's sandbox can work: a `bwrap` on
    `PATH`, or for Codex also the `bwrap` bundled with Codex.
    """

    def default(env: Mapping[str, str] | None) -> str:
        search = (os.environ if env is None else env).get("PATH", os.defpath)
        if shutil.which("bwrap", path=search):
            return "on"
        if engine == "codex":
            codex = shutil.which("codex", path=search)
            # ember.sandbox reads this module, so it is imported here.
            from ember import sandbox

            if (
                codex is not None
                and sandbox.codex_bundled_bwrap(Path(codex)) is not None
            ):
                return "on"
        return "off"

    return default


_STANDING = frozenset({SETTING})
_ALL = frozenset({REQUEST, ONCE, SETTING})
_ON_OFF = ("on", "off")


def _key(name: str, values: tuple[str, ...], default: str) -> Key:
    return Key(name, _STANDING, _enum(*values), _constant(default), allowed=values)


REVIEW_ROUNDS = ("1", "2", "3", "4")
"""The values of `flow.review_rounds`: the most reviews of one flow task."""

GAPCHECK_WHEN = ("off", "always", "2", "3", "4", "5", "6", "8", "10", "ask")
"""The values of `gapcheck.when`: off, always, a least task count, or ask."""

GAPCHECK_ASK = ("high", "medium", "all")
"""The values of `gapcheck.ask`: the lowest gap harm put to the owner."""

FLOW_PARALLEL = ("1", "2", "4", "6")
"""The values of `flow.parallel`: flow tasks that run at once."""

FLOW_STALL = ("5", "10", "20")
"""The values of `flow.stall`: minutes without screen changes before a stop."""

WORKFLOW_AGENTS = ("4", "8", "16")
"""The values of `workflow.agents`: Claude Workflow agents at once."""

WORKFLOW_LATE = ("ask", "wait", "stop")
"""The values of `workflow.late`: what Claude does when an agent runs late."""


def _keys() -> dict[str, Key]:
    keys = [_key("worker.network", _ON_OFF, "off")]
    keys.append(
        Key(
            "worktree.prepare",
            _STANDING,
            _worktree_prepare,
            _constant({}),
            pattern="a JSON object of absolute main-checkout paths to shell commands",
        )
    )
    keys += [_key(f"engine.{engine}", _ON_OFF, "on") for engine in ENGINES]
    keys += [
        Key(
            f"shortlist.{engine}",
            _STANDING,
            _shortlist(engine),
            _constant([]),
            is_list=True,
            pattern=(
                "<provider>/<model>[@<effort>], comma-separated"
                if engine == "opencode"
                else "<model>@<effort>, comma-separated"
            ),
        )
        for engine in ENGINES
    ]
    for role in ("writer", "reviewer"):
        keys.append(
            Key(
                f"single.{role}",
                _ALL,
                _menu_value,
                _constant("ask"),
                pattern="ask or <engine>:<model> from that engine's shortlist",
            )
        )
        keys.append(
            Key(
                f"single.{role}_as",
                _ALL,
                _enum("cli", "subagent"),
                _constant("cli"),
                allowed=("cli", "subagent"),
            )
        )
    keys.append(
        Key(
            "multi.runs_as",
            _ALL,
            _enum("ask", "flow", "workflow"),
            _constant("ask"),
            allowed=("ask", "flow", "workflow"),
        )
    )
    keys.append(
        Key(
            "flow.review_rounds",
            _ALL,
            _enum(*REVIEW_ROUNDS),
            _constant("3"),
            allowed=REVIEW_ROUNDS,
        )
    )
    for name in DERIVED_KEYS:
        keys.append(
            Key(
                name,
                _ALL,
                _derived_value,
                _constant(SAME),
                pattern="same or <engine>:<model> from that engine's shortlist",
            )
        )
    for name, values, default in (
        ("gapcheck.when", GAPCHECK_WHEN, "always"),
        ("gapcheck.ask", GAPCHECK_ASK, "high"),
        ("flow.parallel", FLOW_PARALLEL, "4"),
        ("flow.stall", FLOW_STALL, "10"),
        ("flow.whole_review", _ON_OFF, "on"),
        ("workflow.agents", WORKFLOW_AGENTS, "8"),
        ("workflow.late", WORKFLOW_LATE, "ask"),
    ):
        keys.append(Key(name, _ALL, _enum(*values), _constant(default), allowed=values))
    keys.append(
        Key(
            "panel.members",
            _ALL,
            _panel_members,
            _constant([]),
            is_list=True,
            pattern=(
                "<engine>:<model> from that engine's shortlist, "
                "comma-separated; at most 8"
            ),
        )
    )
    for name, low, high, default in (
        ("provider.capacity_retries", 0, 10, "2"),
        ("provider.capacity_wait", 5, 600, "30"),
    ):
        values = tuple(str(value) for value in range(low, high + 1))
        keys.append(
            Key(name, _STANDING, _enum(*values), _constant(default), allowed=values)
        )
    for engine in SANDBOXED:
        keys.append(
            Key(
                f"sandbox.{engine}",
                _STANDING,
                _enum(*_ON_OFF),
                _sandbox_default(engine),
                allowed=_ON_OFF,
            )
        )
    keys.append(
        Key(
            "sandbox.read",
            _STANDING,
            _absolute_paths,
            _constant([]),
            is_list=True,
            pattern="absolute paths, comma-separated",
        )
    )
    keys.append(
        Key(
            "sandbox.hidden_notices",
            _STANDING,
            _absolute_paths,
            _constant([]),
            is_list=True,
            pattern="absolute paths, comma-separated",
        )
    )
    keys.append(
        Key(
            "terminal",
            _STANDING,
            _command_line,
            _constant("xdg-terminal-exec"),
            pattern="a command line",
        )
    )
    keys.append(_key("app.autostart", _ON_OFF, "on"))
    keys.append(_key("app.theme", ("system", "light", "dark"), "system"))
    return {key.name: key for key in keys}


KEYS: dict[str, Key] = _keys()
"""Every settings key, in display order."""

PATH_LISTS = ("sandbox.read", "sandbox.hidden_notices")
"""Path-list keys whose command-line value may also be a JSON string list."""


def _get_key(name: str, layer: str) -> Key:
    key = KEYS.get(name)
    if key is None:
        raise InvalidChoiceError(f"unknown key {name!r}")
    if layer not in key.layers:
        raise InvalidChoiceError(
            f"{name} is a standing setting only; it has no {layer} value"
        )
    return key


def validate_value(name: str, value: object, layer: str = SETTING) -> None:
    """Check a key and the syntax of its value for a layer.

    Args:
        name: The key.
        value: The typed value.
        layer: `request`, `one-time` or `setting`.

    Raises:
        InvalidChoiceError: The key is unknown, not allowed in `layer`, or the
            value is not valid.
    """
    key = _get_key(name, layer)
    try:
        key.validate(value)
    except InvalidChoiceError as error:
        raise InvalidChoiceError(f"{name}: {error}") from None


def parse_value(name: str, text: str) -> object:
    """Turn a command-line value into a typed value.

    Args:
        name: The key.
        text: The text after `=`; lists are comma-separated. `sandbox.read`
            and `sandbox.hidden_notices` also accept a JSON string list for
            paths containing commas.
            `worktree.prepare` accepts a JSON object.

    Returns:
        A string, a list of strings, or a checkout-to-command object.

    Raises:
        InvalidChoiceError: The key is unknown or its JSON value is not valid.
    """
    key = KEYS.get(name)
    if key is None:
        raise InvalidChoiceError(f"unknown key {name!r}")
    if name == "worktree.prepare":
        try:
            value = json.loads(text, object_pairs_hook=_prepare_object)
        except json.JSONDecodeError as error:
            raise InvalidChoiceError("worktree.prepare is not valid JSON") from error
        validate_value(name, value)
        return value
    if name in PATH_LISTS and text.startswith("["):
        try:
            value = json.loads(text)
        except ValueError as error:
            raise InvalidChoiceError(f"{name} is not a JSON string list") from error
        return _string_list(value)
    if key.is_list:
        return text.split(",") if text else []
    return text


def check_id(run_id: object) -> str:
    """Check a claim id.

    Args:
        run_id: The id.

    Returns:
        The id.

    Raises:
        InvalidChoiceError: The id does not match `[a-z0-9][a-z0-9-]{5,63}`.
    """
    if not isinstance(run_id, str) or _ID.fullmatch(run_id) is None:
        raise InvalidChoiceError(f"{run_id!r} is not a valid id")
    return run_id


def _check_values(values: Mapping[str, object], layer: str) -> dict[str, object]:
    if not isinstance(values, Mapping):
        raise InvalidChoiceError("values must be a mapping")
    for name, value in values.items():
        validate_value(name, value, layer)
    return dict(values)


def _check_expect(expect_generation: object) -> int | None:
    if expect_generation is None:
        return None
    if isinstance(expect_generation, bool) or not isinstance(expect_generation, int):
        raise InvalidChoiceError("expect_generation must be an int")
    return expect_generation


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _now(now: float | None) -> float:
    return time.time() if now is None else now


@dataclass
class _State:
    """What one transaction read from the database."""

    generation: int
    settings: dict[str, object]
    once: dict[str, object] | None
    once_set_at: float | None
    claim_status: str | None
    claim_copy: dict[str, object] | None
    errors: list[str]
    panel_errors: list[str] = field(default_factory=list)


def _decode_object(
    text: str,
    what: str,
    errors: list[str],
    panel_errors: list[str] | None = None,
    *,
    legacy: bool = False,
) -> dict[str, object] | None:
    try:
        value = json.loads(text)
    except ValueError:
        errors.append(f"{what} is not valid JSON")
        return None
    if not isinstance(value, dict):
        errors.append(f"{what} is not a JSON object")
        return None
    if legacy:
        value = {
            name: _convert_stored_choice(name, item) for name, item in value.items()
        }
    result: dict[str, object] = {}
    for name, item in value.items():
        try:
            validate_value(name, item, ONCE)
        except InvalidChoiceError as error:
            if name == "panel.members" and panel_errors is not None:
                panel_errors.append(f"{what}: {error}")
                continue
            errors.append(f"{what}: {error}")
            return None
        result[name] = item
    return result


def _load(
    conn: sqlite3.Connection | None,
    generation: int | None,
    run_id: str | None,
    now: float,
    *,
    include_once: bool,
) -> _State:
    """Read settings, one claim and, if asked, the live pending once.

    Expiry is projected without writing. The pending once is checked only
    when `include_once` is true, so an invalid one makes only those replies
    fail closed.
    """
    state = _State(generation or 0, {}, None, None, None, None, [])
    if conn is None or generation is None:
        if run_id is not None:
            state.claim_status = "unknown"
        return state
    legacy = _legacy_ids(conn)
    for name, text in conn.execute("SELECT key, value FROM settings ORDER BY key"):
        try:
            value = json.loads(text)
            if legacy:
                value = _convert_stored_choice(name, value)
            validate_value(name, value, SETTING)
        except ValueError as error:
            errors = state.panel_errors if name == "panel.members" else state.errors
            errors.append(f"setting {name}: {error}")
            continue
        state.settings[name] = value
    row = conn.execute("SELECT value, set_at FROM once WHERE id = 1").fetchone()
    if include_once and row is not None and row[1] + ONCE_TTL > now:
        state.once = _decode_object(
            row[0], "one-time choice", state.errors, state.panel_errors, legacy=legacy
        )
        state.once_set_at = row[1]
    if run_id is not None:
        row = conn.execute(
            "SELECT status, at, copy FROM claims WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            state.claim_status = "unknown"
        else:
            status, at, copy = row
            if status == "claimed" and at + CLAIM_TTL <= now:
                status = "gone"
            state.claim_status = status
            if status == "claimed":
                if copy is None:
                    state.errors.append(f"claim {run_id} has no copy")
                else:
                    state.claim_copy = _decode_object(
                        copy,
                        f"claim {run_id} copy",
                        state.errors,
                        state.panel_errors,
                        legacy=legacy,
                    )
    return state


def _defaults(env: Mapping[str, str] | None) -> dict[str, dict[str, Any]]:
    return {
        name: {"value": key.default(env), "layer": DEFAULT}
        for name, key in KEYS.items()
    }


def _resolve(
    state: _State, request: Mapping[str, object], env: Mapping[str, str] | None
) -> dict[str, dict[str, Any]]:
    # A broken panel list must not replace engine and sandbox switches with
    # defaults when explicit members are used. Its menu remains unavailable.
    answers = _defaults(env)
    if state.errors or state.claim_status in ("gone", "unknown"):
        return answers
    layers: list[tuple[str, Mapping[str, object]]] = [
        (REQUEST, request),
        (ONCE, state.claim_copy or {}),
        (SETTING, state.settings),
    ]
    for name in KEYS:
        for layer, values in layers:
            if name in values:
                answers[name] = {"value": values[name], "layer": layer}
                break
    return answers


def menu_row(
    answers: Mapping[str, Mapping[str, Any]],
    name: str,
    connected: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Describe one worker menu row from resolved answers.

    Args:
        answers: Resolved answers, key to `{"value", "layer"}`.
        name: `single.writer` or `single.reviewer`.
        connected: Whether each engine was connected when last checked; an
            engine that is missing, or `None`, is not reported as not
            connected.

    Returns:
        `{"value", "state", "engine", "model", "effort", "run_as", "reason"}`.
    """
    value = answers[name]["value"]
    row: dict[str, Any] = {
        "value": value,
        "state": "set",
        "engine": None,
        "model": None,
        "effort": None,
        "run_as": None,
        "reason": None,
    }
    if value == "ask":
        row.update(state="ask", reason="asks at each run")
        return row
    member = _member_row(answers, value, connected)
    row.update(member)
    if row["state"] == "invalid":
        row.update(state="invalid", reason="not ask or <engine>:<model>")
    if row["engine"] == "claude":
        row["run_as"] = answers[f"{name}_as"]["value"]
    return row


def derived_row(
    answers: Mapping[str, Mapping[str, Any]],
    name: str,
    connected: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Describe a gap check or flow worker row.

    While its value is `same`, the row is its single-task row's, with
    `same` true. Otherwise it is checked as a worker of its own; a Claude
    model runs as its single-task row's `_as` choice says.

    Args:
        answers: Resolved answers, key to `{"value", "layer"}`.
        name: A key of `DERIVED_KEYS`.
        connected: As for `menu_row`.

    Returns:
        `menu_row`'s fields plus `same`.
    """
    base = DERIVED_KEYS[name]
    value = answers[name]["value"]
    if value == SAME:
        return {**menu_row(answers, base, connected), "same": True}
    row: dict[str, Any] = {
        "value": value,
        "state": "set",
        "engine": None,
        "model": None,
        "effort": None,
        "run_as": None,
        "reason": None,
    }
    row.update(_member_row(answers, value, connected))
    if row["engine"] == "claude":
        row["run_as"] = answers[f"{base}_as"]["value"]
    return {**row, "same": False}


def _member_row(
    answers: Mapping[str, Mapping[str, Any]],
    value: object,
    connected: Mapping[str, bool] | None,
) -> dict[str, Any]:
    """Describe one engine and model using the shared worker checks."""
    row: dict[str, Any] = {
        "value": value,
        "state": "set",
        "engine": None,
        "model": None,
        "effort": None,
        "reason": None,
    }
    parts = _split_menu(value) if isinstance(value, str) else None
    if parts is None:
        row.update(state="invalid", reason="not <engine>:<model>")
        return row
    engine, model = parts
    row.update(engine=engine, model=model)
    if answers[f"engine.{engine}"]["value"] != "on":
        row.update(state="engine off", reason=f"{engine} is off")
        return row
    if connected is not None and connected.get(engine) is False:
        row.update(state="not connected", reason=f"{engine} is not connected")
        return row
    models = shortlist_models(engine, answers[f"shortlist.{engine}"]["value"])
    if model not in models:
        row.update(state="not shortlisted", reason=f"not on the {engine} shortlist")
        return row
    row["effort"] = models[model]
    return row


def panel_row(
    answers: Mapping[str, Mapping[str, Any]],
    connected: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Describe the panel list and each member without a run-as choice."""
    values = answers["panel.members"]["value"]
    return {
        "value": values,
        "state": "set" if values else "empty",
        "members": [_member_row(answers, value, connected) for value in values],
    }


def runs_as_row(answers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Describe how multi-task work runs, without naming a worker."""
    value = answers["multi.runs_as"]["value"]
    return {
        "value": value,
        "state": "ask" if value == "ask" else "set",
        "reason": "asks at each run" if value == "ask" else None,
    }


def _reply(
    state: _State,
    run_id: str | None,
    request: Mapping[str, object],
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    answers = _resolve(state, request, env)
    errors = [*state.errors, *state.panel_errors]
    source_error = bool(errors)
    held = None if source_error else status.cached(env)
    connected = (
        None if held is None else {name: item.connected for name, item in held.items()}
    )
    return {
        "answers": answers,
        "claim": None
        if run_id is None
        else {"id": run_id, "status": state.claim_status},
        "generation": state.generation,
        "errors": errors,
        "source_error": source_error,
        "menu": None
        if source_error
        else {
            **{name: menu_row(answers, name, connected) for name in MENU_KEYS},
            **{name: derived_row(answers, name, connected) for name in DERIVED_KEYS},
            "multi.runs_as": runs_as_row(answers),
            "panel.members": panel_row(answers, connected),
        },
    }


def _read_state(
    env: Mapping[str, str] | None, run_id: str | None, now: float
) -> _State:
    with db.translate_errors(), db.open_read(env) as conn:
        if conn is None:
            return _load(None, None, run_id, now, include_once=run_id is None)
        with db.read_transaction(conn):
            generation = db.read_generation(conn)
            return _load(conn, generation, run_id, now, include_once=run_id is None)


BRIEF_ANSWER_PREFIXES = ("engine.", "shortlist.")
"""The answer keys a brief reply keeps: the engine switches and shortlists."""


def brief(reply: Mapping[str, Any]) -> dict[str, Any]:
    """Shorten a read or claim reply to what a lead reads to pick a worker.

    Args:
        reply: A reply from `read` or `claim`.

    Returns:
        The reply with every key but `answers` unchanged, and `answers`
        holding only the `engine.<name>` and `shortlist.<name>` keys.
    """
    answers = reply.get("answers") or {}
    return {
        **reply,
        "answers": {
            key: value
            for key, value in answers.items()
            if key.startswith(BRIEF_ANSWER_PREFIXES)
        },
    }


def read(
    id: str | None = None,
    request: Mapping[str, object] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve every key without writing anything.

    Args:
        id: A claim id whose one-time copy is used; `None` for no claim.
        request: Request-layer values, validated before anything is read.
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        The read reply: answers, claim, generation, errors, source_error, menu.

    Raises:
        InvalidChoiceError: The id or a request value is not valid.
        DatabaseError: The database cannot be read or has another schema.
    """
    run_id = None if id is None else check_id(id)
    request = _check_values(request or {}, REQUEST)
    state = _read_state(env, run_id, _now(now))
    return _reply(state, run_id, request, env)


def _write(
    env: Mapping[str, str] | None,
    now: float,
    expect_generation: object,
    change: Callable[[sqlite3.Connection], bool],
    reply: Callable[[sqlite3.Connection, int], dict[str, Any]],
) -> dict[str, Any]:
    """Run `change` in one write transaction and build the reply.

    The transaction checks `expect_generation`, migrates legacy model ids,
    stores expiry, runs `change` and raises the generation by one when
    `change` reports a settings or
    pending one-time choice write. Claim-table writes and expiry do not
    raise the generation. A model-id migration shares the same increment.
    """
    expect = _check_expect(expect_generation)
    with db.translate_errors(), db.open_write(env) as conn, db.transaction(conn):
        generation = db.ensure_schema(conn)
        if expect is not None and expect != generation:
            raise db.StaleGenerationError(
                f"generation is {generation}, not {expect}; reload and try again"
            )
        migrated = _migrate_ids(conn)
        conn.execute("DELETE FROM once WHERE set_at + ? <= ?", (ONCE_TTL, now))
        conn.execute(
            "UPDATE claims SET status = 'gone', copy = NULL "
            "WHERE status = 'claimed' AND at + ? <= ?",
            (CLAIM_TTL, now),
        )
        changed = change(conn)
        if changed or migrated:
            generation = db.bump_generation(conn)
        return reply(conn, generation)


def claim(
    id: str,
    request: Mapping[str, object] | None = None,
    *,
    skill_version: str | None = None,
    expect_generation: int | None = None,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Claim the pending one-time choice for a run id.

    An unknown id takes a copy of the live pending one-time choice, which is
    then removed, and is recorded `claimed`; with none pending it is recorded
    `none`. A known id returns its stored status without changing the claim.
    Legacy model ids are migrated in the write transaction.
    A pending one-time choice that is not valid is not consumed: nothing is
    recorded, the reply has status `unknown` and fails closed.

    Args:
        id: The run's claim id.
        request: Request-layer values, validated before anything is read.
        skill_version: The loaded skill's version; `None` skips the comparison.
        expect_generation: The generation the caller last read, or `None`.
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        The read reply for `id`, with `skill`, `loaded_skill_version` and
        `installed_skill_version` when `skill_version` is given. `skill` is
        `current`, `stale` or `unknown` for an unreadable installed version.

    Raises:
        InvalidChoiceError: The id, a request value or skill version is not valid.
        DatabaseError: The database cannot be written, is busy, has another
            schema, or is at another generation than `expect_generation`.
    """
    run_id = check_id(id)
    request = _check_values(request or {}, REQUEST)
    if skill_version is not None and not skill.is_valid_version(skill_version):
        raise InvalidChoiceError(
            f"--skill needs a version in x.y.z form, not {skill_version!r}"
        )
    at = _now(now)

    refused: list[bool] = []

    def change(conn: sqlite3.Connection) -> bool:
        known = conn.execute("SELECT 1 FROM claims WHERE id = ?", (run_id,)).fetchone()
        if known is not None:
            return False
        row = conn.execute("SELECT value FROM once WHERE id = 1").fetchone()
        if row is not None and _decode_object(row[0], "one-time choice", []) is None:
            refused.append(True)
            return False
        if row is None:
            conn.execute(
                "INSERT INTO claims (id, status, at, copy) VALUES (?, 'none', ?, NULL)",
                (run_id, at),
            )
            return False
        conn.execute(
            "INSERT INTO claims (id, status, at, copy) VALUES (?, 'claimed', ?, ?)",
            (run_id, at, row[0]),
        )
        conn.execute("DELETE FROM once WHERE id = 1")
        return True

    def reply(conn: sqlite3.Connection, generation: int) -> dict[str, Any]:
        state = _load(conn, generation, run_id, at, include_once=bool(refused))
        return _reply(state, run_id, request, env)

    result = _write(env, at, expect_generation, change, reply)
    if skill_version is not None:
        installed = skill.installed_version(env)
        result.update(
            skill="unknown"
            if installed is None
            else "current"
            if installed == skill_version
            else "stale",
            loaded_skill_version=skill_version,
            installed_skill_version=installed,
        )
    return result


def finish(
    id: str,
    *,
    expect_generation: int | None = None,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Release a claim; the id is recorded `gone`, also when it was unknown.

    `gone` is the status of every finished claim. The reply's `was` is the
    status before this call: `claimed`, `none`, `gone` (already finished or
    expired) or `unknown`.

    Args:
        id: The run's claim id.
        expect_generation: The generation the caller last read, or `None`.
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        `{"claim": {"id", "status": "gone", "was"}, "generation", "errors"}`.

    Raises:
        InvalidChoiceError: The id is not valid.
        DatabaseError: The database cannot be written, is busy, has another
            schema, or is at another generation than `expect_generation`.
    """
    run_id = check_id(id)
    at = _now(now)
    was: list[str] = []

    def change(conn: sqlite3.Connection) -> bool:
        # Expiry has already run in this transaction, so an expired claim
        # reads as `gone` here.
        row = conn.execute(
            "SELECT status FROM claims WHERE id = ?", (run_id,)
        ).fetchone()
        was.append("unknown" if row is None else row[0])
        conn.execute(
            "INSERT INTO claims (id, status, at, copy) VALUES (?, 'gone', ?, NULL) "
            "ON CONFLICT (id) DO UPDATE SET status = 'gone', copy = NULL, "
            "at = excluded.at WHERE status != 'gone'",
            (run_id, at),
        )
        return False

    def reply(conn: sqlite3.Connection, generation: int) -> dict[str, Any]:
        return {
            "claim": {"id": run_id, "status": "gone", "was": was[0]},
            "generation": generation,
            "errors": [],
        }

    return _write(env, at, expect_generation, change, reply)


def _stored_settings(conn: sqlite3.Connection) -> dict[str, object]:
    """Return the standing settings, leaving out values that are not JSON."""
    result: dict[str, object] = {}
    for name, text in conn.execute("SELECT key, value FROM settings ORDER BY key"):
        try:
            result[name] = json.loads(text)
        except ValueError:
            continue
    return result


def _check_shortlisted(
    values: Mapping[str, object], settings: Mapping[str, object]
) -> None:
    """Refuse a menu value whose model is not on its engine's shortlist."""
    entries = [(name, values.get(name)) for name in (*MENU_KEYS, *DERIVED_KEYS)]
    entries.extend(
        ("panel.members", value)
        for value in _string_list(values.get("panel.members", []))
    )
    for name, value in entries:
        if not isinstance(value, str) or value in ("ask", SAME):
            continue
        parts = _split_menu(value)
        if parts is None:
            continue
        engine, model = parts
        shortlist_key = f"shortlist.{engine}"
        shortlist = settings.get(shortlist_key, [])
        try:
            validate_value(shortlist_key, shortlist)
        except InvalidChoiceError as error:
            raise InvalidChoiceError(f"{name}: stored {error}") from None
        if model not in shortlist_models(engine, _string_list(shortlist)):
            raise InvalidChoiceError(
                f"{name}: {model} is not on the {engine} shortlist"
            )


def _settings_reply(conn: sqlite3.Connection, generation: int) -> dict[str, Any]:
    return {"settings": _stored_settings(conn), "generation": generation, "errors": []}


def set_settings(
    values: Mapping[str, object],
    expect_generation: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Save standing settings.

    A menu value must name a model on its engine's shortlist, as saved after
    this write.

    Args:
        values: Key to typed value.
        expect_generation: The generation the caller last read, or `None`.
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        `{"settings": {...}, "generation", "errors"}` with every standing
        setting.

    Raises:
        InvalidChoiceError: A key or value is not valid.
        DatabaseError: The database cannot be written, is busy, has another
            schema, or is at another generation than `expect_generation`.
    """
    values = _check_values(values, SETTING)
    if "sandbox.read" in values:
        from ember import sandbox

        for path in _string_list(values["sandbox.read"]):
            problem = sandbox.read_path_problem(path, env)
            if problem:
                _fail(problem)

    def change(conn: sqlite3.Connection) -> bool:
        _check_shortlisted(values, {**_stored_settings(conn), **values})
        before = conn.total_changes
        for name, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value "
                "WHERE value != excluded.value",
                (name, _canonical(value)),
            )
        return conn.total_changes != before

    return _write(env, _now(now), expect_generation, change, _settings_reply)


def clear_settings(
    keys: Iterable[str],
    expect_generation: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Remove standing settings, so they resolve to their defaults.

    Args:
        keys: The keys to remove.
        expect_generation: The generation the caller last read, or `None`.
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        `{"settings": {...}, "generation", "errors"}` with every standing
        setting left.

    Raises:
        InvalidChoiceError: A key is unknown.
        DatabaseError: The database cannot be written, is busy, has another
            schema, or is at another generation than `expect_generation`.
    """
    names = list(keys)
    for name in names:
        _get_key(name, SETTING)

    def change(conn: sqlite3.Connection) -> bool:
        return (
            conn.executemany(
                "DELETE FROM settings WHERE key = ?", [(n,) for n in names]
            ).rowcount
            > 0
        )

    return _write(env, _now(now), expect_generation, change, _settings_reply)


def set_once(
    values: Mapping[str, object],
    expect_generation: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Replace the pending one-time choice whole; it expires after 24 hours.

    A menu value must name a model on its engine's standing shortlist.

    Args:
        values: Key to typed value; at least one.
        expect_generation: The generation the caller last read, or `None`.
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        `{"once": {...}, "generation", "errors"}`.

    Raises:
        InvalidChoiceError: No values, or a key or value is not valid.
        DatabaseError: The database cannot be written, is busy, has another
            schema, or is at another generation than `expect_generation`.
    """
    values = _check_values(values, ONCE)
    if not values:
        raise InvalidChoiceError("a one-time choice needs at least one value")
    at = _now(now)

    def change(conn: sqlite3.Connection) -> bool:
        _check_shortlisted(values, _stored_settings(conn))
        conn.execute(
            "INSERT OR REPLACE INTO once (id, value, set_at) VALUES (1, ?, ?)",
            (_canonical(values), at),
        )
        return True

    def reply(conn: sqlite3.Connection, generation: int) -> dict[str, Any]:
        return {"once": values, "generation": generation, "errors": []}

    return _write(env, at, expect_generation, change, reply)


def clear_once(
    expect_generation: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Remove the pending one-time choice.

    Args:
        expect_generation: The generation the caller last read, or `None`.
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        `{"cleared": "one-time", "generation", "errors"}`.

    Raises:
        DatabaseError: The database cannot be written, is busy, has another
            schema, or is at another generation than `expect_generation`.
    """

    def change(conn: sqlite3.Connection) -> bool:
        return conn.execute("DELETE FROM once WHERE id = 1").rowcount > 0

    def reply(conn: sqlite3.Connection, generation: int) -> dict[str, Any]:
        return {"cleared": "one-time", "generation": generation, "errors": []}

    return _write(env, _now(now), expect_generation, change, reply)


def allowed_values(answers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Describe the values each key accepts.

    Args:
        answers: Resolved answers, which supply the shortlists.

    Returns:
        Key to `{"values": [...]}` for keys with a fixed set of values, or
        `{"pattern": ...}` for the others. Worker menu keys list `ask` and
        every shortlisted `<engine>:<model>`; `panel.members` omits `ask`.
    """
    result: dict[str, Any] = {}
    for name, key in KEYS.items():
        if name in MENU_KEYS or name == "panel.members":
            options = ["ask"] if name in MENU_KEYS else []
            for engine in ENGINES:
                shortlist = answers[f"shortlist.{engine}"]["value"]
                options += [
                    f"{engine}:{m}" for m in shortlist_models(engine, shortlist)
                ]
            result[name] = {"values": options}
        elif key.allowed is not None:
            result[name] = {"values": list(key.allowed)}
        else:
            result[name] = {"pattern": key.pattern}
    return result


def snapshot(
    *, env: Mapping[str, str] | None = None, now: float | None = None
) -> dict[str, Any]:
    """Read everything the app shows, without writing anything.

    Args:
        env: The environment; `None` reads `os.environ`.
        now: The time; `None` for the current time.

    Returns:
        generation, settings (standing values), once (`{"values", "set_at",
        "expires_at"}` or `None`), claims (live claimed ids with `at`),
        answers, menu, allowed, errors and source_error.

    Raises:
        DatabaseError: The database cannot be read or has another schema.
    """
    at = _now(now)
    claims: list[dict[str, Any]] = []
    with db.translate_errors(), db.open_read(env) as conn:
        if conn is None:
            state = _load(None, None, None, at, include_once=True)
        else:
            with db.read_transaction(conn):
                generation = db.read_generation(conn)
                state = _load(conn, generation, None, at, include_once=True)
                if generation is not None:
                    claims = [
                        {"id": run_id, "at": claimed_at}
                        for run_id, claimed_at in conn.execute(
                            "SELECT id, at FROM claims WHERE status = 'claimed' "
                            "AND at + ? > ? ORDER BY at, id",
                            (CLAIM_TTL, at),
                        )
                    ]
    reply = _reply(state, None, {}, env)
    once = None
    if state.once is not None and state.once_set_at is not None:
        once = {
            "values": state.once,
            "set_at": state.once_set_at,
            "expires_at": state.once_set_at + ONCE_TTL,
        }
    return {
        "generation": state.generation,
        "settings": state.settings,
        "once": once,
        "claims": claims,
        "answers": reply["answers"],
        "menu": reply["menu"],
        "allowed": allowed_values(reply["answers"]),
        "errors": reply["errors"],
        "source_error": reply["source_error"],
    }
