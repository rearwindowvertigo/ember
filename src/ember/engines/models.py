"""Engine model discovery and cached integration metadata.

OpenCode supplies full integration/model ids. models.dev supplies details;
OpenCode's auth list supplies integration names and login methods. Failed
refreshes retain the cached list and report a reason. Unversioned OpenCode
caches are converted only as a fallback after a failed refresh.
"""

import contextlib
import http.client
import json
import math
import os
import re
import shlex
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeGuard

from ember import choices, paths
from ember.engines import status
from ember.web import USER_AGENT

MAX_AGE = 6 * 60 * 60
"""Seconds a cached model list is used before it is read again."""

TIMEOUT = 10
"""Seconds a model list command or request may take."""

CATALOGUE_URL = "https://models.dev/api.json"
"""Integration and model details, with prices per million tokens."""

CACHE_VERSION = 1

CODEX_COMMAND = ("debug", "models")
"""The arguments to `codex` that print its model list."""

CLAUDE_MODELS = ("fable", "opus", "sonnet", "haiku")
"""The model aliases Claude Code's `--model` option takes."""

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
"""The efforts Claude Code's `--effort` option takes."""

CLI = "cli"
CACHE = "cache"
SOURCES = (CLI, CACHE)
"""Where a model list came from."""

_OPENCODE_ID = re.compile(r"[^/\s]+/[^\s]+")

Fetch = Callable[[str, float], Any]

_CACHE_WRITES = ContextVar("model_cache_writes", default=True)


@contextlib.contextmanager
def without_cache_writes() -> Iterator[None]:
    """Disable model cache writes for nested lookups in the current context.

    Lookups still read caches, refresh them from their sources and fall back
    on discovery failures. The scope applies to lookups through drivers and
    restores the previous policy on exit. Other threads keep their policy.

    Yields:
        Nothing.
    """
    token = _CACHE_WRITES.set(False)
    try:
        yield
    finally:
        _CACHE_WRITES.reset(token)


@dataclass(frozen=True)
class Model:
    """One model an engine can run.

    Prices are in US dollars per token. Missing catalogue details are `None`.
    OpenCode ids include the integration prefix; provider metadata describes
    the integration serving the model, including models routed by OpenRouter.
    """

    id: str
    """The id the engine takes."""

    label: str
    """The model's display name."""

    efforts: tuple[str, ...] = ()
    """The efforts the model takes."""

    default_effort: str | None = None
    """The effort used when none is given."""

    context_length: int | None = None
    """The context window in tokens."""

    prompt_price: float | None = None
    """US dollars per input token."""

    completion_price: float | None = None
    """US dollars per output token."""

    supports_tools: bool | None = None
    """Whether the model takes tool calls."""

    provider: str | None = None
    """The OpenCode integration id; `None` for other engines."""

    provider_name: str | None = None
    """The integration display name from auth list or the catalogue."""

    login: str | None = None
    """`oauth`, `api`, or `None` when the login method is unknown."""

    def to_json(self) -> dict[str, Any]:
        """Return the model as a JSON-ready dict.

        Returns:
            Every field, with `efforts` as a list.
        """
        return {
            "id": self.id,
            "label": self.label,
            "efforts": list(self.efforts),
            "default_effort": self.default_effort,
            "context_length": self.context_length,
            "prompt_price": self.prompt_price,
            "completion_price": self.completion_price,
            "supports_tools": self.supports_tools,
            "provider": self.provider,
            "provider_name": self.provider_name,
            "login": self.login,
        }


@dataclass(frozen=True)
class Integration:
    """An OpenCode integration, including one with no model rows."""

    id: str
    name: str
    login: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the integration id, display name and login method."""
        return {"id": self.id, "name": self.name, "login": self.login}


@dataclass(frozen=True)
class ModelList:
    """An engine's model list and where it came from."""

    models: tuple[Model, ...]
    """The models, in the order the source lists them."""

    source: str
    """`cli` or `cache`."""

    fetched_at: float | None
    """When the list was read from its source; `None` when it never was."""

    reason: str | None
    """Why the list could not be read fresh; `None` when it was."""

    integrations: tuple[Integration, ...] = ()
    """Integrations independent of model rows, including connected empty ones."""

    def to_json(self) -> dict[str, Any]:
        """Return the list as a JSON-ready dict.

        Returns:
            `{"models", "integrations", "source", "fetched_at", "reason"}`.
        """
        return {
            "models": [model.to_json() for model in self.models],
            "integrations": [item.to_json() for item in self.integrations],
            "source": self.source,
            "fetched_at": self.fetched_at,
            "reason": self.reason,
        }


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _model_from_json(value: object) -> Model | None:
    if not isinstance(value, dict):
        return None
    ident, label = _str(value.get("id")), _str(value.get("label"))
    efforts = value.get("efforts")
    tools = value.get("supports_tools")

    def absent_or(name: str, read: Callable[[object], object]) -> bool:
        return value.get(name) is None or read(value.get(name)) is not None

    checks = (
        isinstance(efforts, list) and all(isinstance(e, str) for e in efforts),
        absent_or("default_effort", _str),
        absent_or("context_length", _int),
        absent_or("prompt_price", _number),
        absent_or("completion_price", _number),
        tools is None or isinstance(tools, bool),
        absent_or("provider", _str),
        absent_or("provider_name", _str),
        value.get("login") in (None, "oauth", "api"),
    )
    if not all(checks) or ident is None or label is None:
        return None
    return Model(
        id=ident,
        label=label,
        efforts=_strings(efforts),
        default_effort=_str(value.get("default_effort")),
        context_length=_int(value.get("context_length")),
        prompt_price=_number(value.get("prompt_price")),
        completion_price=_number(value.get("completion_price")),
        supports_tools=tools if isinstance(tools, bool) else None,
        provider=_str(value.get("provider")),
        provider_name=_str(value.get("provider_name")),
        login=_str(value.get("login")),
    )


def cache_path(engine: str, env: Mapping[str, str] | None = None) -> Path:
    """Return the path of an engine's model list cache.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.

    Returns:
        `models-<engine>.json` in Ember's cache folder.
    """
    return paths.cache_dir(env) / f"models-{engine}.json"


def _read_cache(value: object) -> tuple[float, tuple[Model, ...]] | None:
    """Return (fetched_at, models) from a cache file, or `None` if not valid."""
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        return None
    fetched_at = status.finite_number(value.get("fetched_at"))
    if fetched_at is None:
        return None
    models = [_model_from_json(item) for item in value["models"]]
    if any(model is None for model in models):
        return None
    return fetched_at, tuple(model for model in models if model is not None)


def parse_codex(text: str) -> tuple[Model, ...]:
    """Parse the JSON that `codex debug models` prints.

    Only models whose `visibility` is `list` are kept, in the order listed.

    Args:
        text: The command's standard output.

    Returns:
        The models.

    Raises:
        ValueError: The output is not JSON with a `models` list.
    """
    reply = json.loads(text)
    items = reply.get("models") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        raise ValueError("codex debug models printed no models list")
    result: list[Model] = []
    for item in items:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = _str(item.get("slug"))
        if slug is None:
            continue
        levels = item.get("supported_reasoning_levels")
        efforts = (
            tuple(
                level["effort"]
                for level in levels
                if isinstance(level, dict) and isinstance(level.get("effort"), str)
            )
            if isinstance(levels, list)
            else ()
        )
        result.append(
            Model(
                id=slug,
                label=_str(item.get("display_name")) or slug,
                efforts=efforts,
                default_effort=_str(item.get("default_reasoning_level")),
            )
        )
    return tuple(result)


def _is_catalogue(catalogue: object) -> TypeGuard[dict[str, dict[str, Any]]]:
    """Return whether `catalogue` has the models.dev provider shape."""
    return isinstance(catalogue, dict) and all(
        isinstance(value, dict) and isinstance(value.get("models"), dict)
        for value in catalogue.values()
    )


def parse_opencode(
    output: str, catalogue: object, auth: tuple[tuple[str, str | None], ...]
) -> tuple[tuple[Model, ...], tuple[Integration, ...]]:
    """Join authoritative OpenCode ids to catalogue details and auth entries.

    Auth names match an id or catalogue name, ignoring case. Ambiguous and
    unmatched names do not assign a login method. Catalogue-only providers
    are included only when matched by auth, including providers with no models.

    Raises:
        ValueError: The catalogue or model-command output is unreadable.
    """
    if not _is_catalogue(catalogue):
        raise ValueError("models.dev reply is not a provider catalogue")
    ids = tuple(
        dict.fromkeys(line.strip() for line in output.splitlines() if line.strip())
    )
    if any(not _OPENCODE_ID.fullmatch(ident) for ident in ids):
        raise ValueError("opencode models printed invalid model ids")
    served = dict.fromkeys(ident.split("/", 1)[0] for ident in ids)
    names = {
        ident: _str(item.get("name")) or ident for ident, item in catalogue.items()
    }
    for ident in served:
        names.setdefault(ident, ident)
    matched: dict[str, list[tuple[str, str | None]]] = {}
    for name, login in auth:
        matches = [
            ident
            for ident, label in names.items()
            if name.casefold() in (ident.casefold(), label.casefold())
        ]
        if len(matches) == 1:
            ident = matches[0]
            served.setdefault(ident, None)
            matched.setdefault(ident, []).append((name, login))
    integrations = tuple(
        Integration(ident, *matched[ident][0])
        if len(matched.get(ident, [])) == 1
        else Integration(ident, names[ident])
        for ident in served
    )
    by_id = {item.id: item for item in integrations}
    result: list[Model] = []
    for ident in ids:
        provider, model_id = ident.split("/", 1)
        integration = by_id[provider]
        item = catalogue.get(provider, {}).get("models", {}).get(model_id, {})
        item = item if isinstance(item, dict) else {}
        limit = item.get("limit")
        cost = item.get("cost")
        options = item.get("reasoning_options")
        efforts = tuple(
            value
            for option in (options if isinstance(options, list) else [])
            if isinstance(option, dict) and option.get("type") == "effort"
            for value in _strings(option.get("values"))
        )
        prompt = _number(cost.get("input")) if isinstance(cost, dict) else None
        completion = _number(cost.get("output")) if isinstance(cost, dict) else None
        tools = item.get("tool_call")
        result.append(
            Model(
                id=ident,
                label=_str(item.get("name")) or ident,
                efforts=efforts,
                context_length=_int(limit.get("context"))
                if isinstance(limit, dict)
                else None,
                prompt_price=None if prompt is None else prompt / 1_000_000,
                completion_price=None if completion is None else completion / 1_000_000,
                supports_tools=tools if isinstance(tools, bool) else None,
                provider=provider,
                provider_name=integration.name,
                login=integration.login,
            )
        )
    return tuple(result), integrations


def _read_opencode(
    env: Mapping[str, str], fetch: Fetch
) -> tuple[tuple[Model, ...], tuple[Integration, ...], str | None]:
    binary = status.locate("opencode", env)
    if binary is None:
        raise status.ProbeError("the opencode CLI is not installed")
    code, output, errors = _run_models([binary, "models"], env)
    if code != 0:
        raise status.ProbeError(
            f"opencode models exited {code}: {status.first_line(errors or output)}"
        )
    reason = None
    try:
        catalogue = fetch(CATALOGUE_URL, TIMEOUT)
        if not _is_catalogue(catalogue):
            raise ValueError("models.dev reply is not a provider catalogue")
    except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
        catalogue = {}
        reason = f"models.dev catalogue: {error}"
    try:
        _, version = status.run_probe([binary, "--version"], env)
        auth = status.opencode_auth(binary, env, version.strip())
    except status.ProbeError as error:
        auth = ()
        reason = str(error) if reason is None else f"{reason}; {error}"
    models, integrations = parse_opencode(output, catalogue, auth)
    return models, integrations, reason


def fetch_json(url: str, timeout: float) -> Any:  # noqa: ANN401
    """GET `url` and decode its JSON body.

    Args:
        url: The address.
        timeout: Seconds the request may take.

    Returns:
        The decoded body.
    """
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _run_models(argv: list[str], env: Mapping[str, str]) -> tuple[int, str, str]:
    """Run a model list command with standard error kept apart.

    Args:
        argv: The command and its arguments.
        env: The command's whole environment.

    Returns:
        The exit code, the standard output and the standard error.

    Raises:
        ProbeError: The command cannot run or exceeds `TIMEOUT` seconds.
    """
    shown = shlex.join([Path(argv[0]).name, *argv[1:]])
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            env=dict(env),
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise status.ProbeError(
            f"{shown} did not finish within {TIMEOUT} seconds"
        ) from None
    except OSError as error:
        raise status.ProbeError(
            f"{shown} could not run: {error.strerror or error}"
        ) from None
    return done.returncode, done.stdout, done.stderr


def _read_codex(env: Mapping[str, str]) -> tuple[Model, ...]:
    """Read Codex's model list from the standard output of `codex debug models`.

    Standard error is used only in the failure reason, and standard output
    when standard error is empty.
    """
    binary = status.locate("codex", env)
    if binary is None:
        raise status.ProbeError("the codex CLI is not installed")
    code, output, errors = _run_models([binary, *CODEX_COMMAND], env)
    if code != 0:
        shown = status.first_line(errors if errors.strip() else output)
        raise status.ProbeError(f"codex debug models exited {code}: {shown}")
    try:
        return parse_codex(output)
    except ValueError:
        raise status.ProbeError(
            f"codex debug models printed {status.first_line(output)!r}"
        ) from None


def _claude_list(now: float) -> ModelList:
    models = tuple(
        Model(id=name, label=name, efforts=CLAUDE_EFFORTS) for name in CLAUDE_MODELS
    )
    return ModelList(models=models, source=CLI, fetched_at=now, reason=None)


def list_models(
    engine: str,
    env: Mapping[str, str] | None = None,
    *,
    refresh: bool = False,
    fetch: Fetch | None = None,
    now: float | None = None,
) -> ModelList:
    """Return an engine's model list.

    Refreshed lists are saved unless inside `without_cache_writes`.

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.
        refresh: Read the source even when the cache is current.
        fetch: Reads models.dev; `None` uses `fetch_json`.
        now: The time; `None` for the current time.

    Returns:
        The list. When the source cannot be read it is the cached list with
        `source` `cache`. Without a cache, an auth-list failure returns models
        with unknown login methods; other failures return an empty list.
        Failed refreshes set `reason` and never replace the cache.

    Raises:
        ValueError: `engine` is not an engine.
    """
    if engine not in status.ENGINES:
        raise ValueError(f"unknown engine {engine!r}")
    at = time.time() if now is None else now
    if engine == "claude":
        return _claude_list(at)
    env = os.environ if env is None else env
    path = cache_path(engine, env)
    payload = status.load_cache_file(path)
    held = _read_cache(payload)
    cached_integrations: tuple[Integration, ...] = ()
    legacy = False
    if engine == "opencode" and held is not None and isinstance(payload, dict):
        legacy = "format_version" not in payload
        if legacy:
            held = (
                held[0],
                tuple(
                    replace(
                        model,
                        id=choices.full_opencode_id(model.id),
                        provider="openrouter",
                        provider_name="OpenRouter",
                        login=None,
                        default_effort=None,
                    )
                    for model in held[1]
                ),
            )
            cached_integrations = (Integration("openrouter", "OpenRouter"),)
        elif payload.get("format_version") != CACHE_VERSION:
            held = None
        else:
            entries = payload.get("integrations")
            if not isinstance(entries, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("name"), str)
                or item.get("login") not in (None, "oauth", "api")
                for item in entries
            ):
                held = None
            else:
                cached_integrations = tuple(
                    Integration(item["id"], item["name"], item.get("login"))
                    for item in entries
                )
    if held is not None and not legacy and not refresh and 0 <= at - held[0] < MAX_AGE:
        return ModelList(held[1], CACHE, held[0], None, cached_integrations)
    found: tuple[Model, ...] = ()
    integrations: tuple[Integration, ...] = ()
    try:
        if engine == "codex":
            found = _read_codex(env)
            reason = None
        else:
            found, integrations, reason = _read_opencode(
                env, fetch_json if fetch is None else fetch
            )
    except status.ProbeError as error:
        reason = str(error)
    except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
        reason = f"OpenCode model list: {error}"
    if reason is None:
        result = ModelList(found, CLI, at, None, integrations)
        if _CACHE_WRITES.get():
            payload = {**result.to_json(), "format_version": CACHE_VERSION}
            with contextlib.suppress(OSError):
                status.save_cache_file(path, payload, env)
        return result
    if held is not None:
        return ModelList(held[1], CACHE, held[0], reason, cached_integrations)
    return ModelList(found, CLI, None, reason, integrations)


def filter_opencode(
    models: Iterable[Model],
    *,
    text: str | None = None,
    max_prompt_price: float | None = None,
    min_context: int | None = None,
    tools: bool | None = None,
    provider: str | None = None,
) -> tuple[Model, ...]:
    """Keep the models that pass every given filter.

    A filter left at `None` keeps every model. A model whose value for an
    active filter is unknown is left out. OAuth integrations bypass the price
    filter. Bounds are inclusive.

    Args:
        models: The models to filter.
        text: Text the id or label contains, ignoring case.
        max_prompt_price: The highest price per input token, in US dollars.
        min_context: The smallest context window, in tokens.
        tools: Whether the model must, or must not, take tool calls.
        provider: Integration id; `None` keeps all integrations.

    Returns:
        The models kept, in their order.
    """
    needle = None if text is None else text.casefold()

    def keep(model: Model) -> bool:
        if needle is not None and not (
            needle in model.id.casefold() or needle in model.label.casefold()
        ):
            return False
        if provider is not None and model.provider != provider:
            return False
        if (
            max_prompt_price is not None
            and model.login != "oauth"
            and (model.prompt_price is None or model.prompt_price > max_prompt_price)
        ):
            return False
        if min_context is not None and (
            model.context_length is None or model.context_length < min_context
        ):
            return False
        return tools is None or (
            model.supports_tools is not None and model.supports_tools == tools
        )

    return tuple(model for model in models if keep(model))
