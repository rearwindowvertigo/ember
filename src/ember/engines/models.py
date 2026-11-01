"""Each engine's model list.

Codex's models come from `codex debug models`. Claude Code's are the model
aliases its `--model` option takes. OpenCode's are OpenRouter's public model
list, read through an injectable `fetch(url, timeout)` that returns the
decoded JSON reply; their ids are OpenRouter's own, as `shortlist.opencode`
holds them.

The Codex and OpenRouter lists are cached in `models-<engine>.json` in Ember's
cache folder and read again when the cache is `MAX_AGE` seconds old, missing
or not valid, or when a refresh is asked for. A read that fails returns the
cached list, however old, with the failure as its reason; without a cache it
returns an empty list and the reason. A failed read never changes the cache.
"""

import contextlib
import json
import math
import os
import re
import shlex
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ember import paths
from ember.engines import status

MAX_AGE = 6 * 60 * 60
"""Seconds a cached model list is used before it is read again."""

TIMEOUT = 10
"""Seconds a model list command or request may take."""

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
"""OpenRouter's public model list."""

CODEX_COMMAND = ("debug", "models")
"""The arguments to `codex` that print its model list."""

CLAUDE_MODELS = ("fable", "opus", "sonnet", "haiku")
"""The model aliases Claude Code's `--model` option takes."""

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
"""The efforts Claude Code's `--effort` option takes."""

CLI = "cli"
OPENROUTER = "openrouter"
CACHE = "cache"
SOURCES = (CLI, OPENROUTER, CACHE)
"""Where a model list came from."""

_OPENROUTER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*")

Fetch = Callable[[str, float], Any]


@dataclass(frozen=True)
class Model:
    """One model an engine can run.

    Prices are in US dollars per token. Fields that only OpenRouter reports
    are `None` for other engines and when OpenRouter does not report them.
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
        }


@dataclass(frozen=True)
class ModelList:
    """An engine's model list and where it came from."""

    models: tuple[Model, ...]
    """The models, in the order the source lists them."""

    source: str
    """`cli`, `openrouter` or `cache`."""

    fetched_at: float | None
    """When the list was read from its source; `None` when it never was."""

    reason: str | None
    """Why the list could not be read fresh; `None` when it was."""

    def to_json(self) -> dict[str, Any]:
        """Return the list as a JSON-ready dict.

        Returns:
            `{"models", "source", "fetched_at", "reason"}`.
        """
        return {
            "models": [model.to_json() for model in self.models],
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


def _read_cache(path: Path) -> tuple[float, tuple[Model, ...]] | None:
    """Return (fetched_at, models) from a cache file, or `None` if not valid."""
    value = status.load_cache_file(path)
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


def price(value: object) -> float | None:
    """Read a per-token price as OpenRouter writes it.

    Args:
        value: A decimal string or a number.

    Returns:
        US dollars per token, or `None` when missing, not a number or negative.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return float(number)


def parse_openrouter(reply: object) -> tuple[Model, ...]:
    """Parse OpenRouter's model list reply.

    Models whose id is not `<provider>/<model>` as `shortlist.opencode` takes
    it are left out, as is a repeated id.

    Args:
        reply: The decoded JSON reply, `{"data": [...]}`.

    Returns:
        The models, in the order listed.

    Raises:
        ValueError: The reply holds no list of models.
    """
    items = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        raise ValueError("the OpenRouter reply holds no model list")
    result: list[Model] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        ident = _str(item.get("id"))
        if ident is None or not _OPENROUTER_ID.fullmatch(ident) or ident in seen:
            continue
        seen.add(ident)
        pricing = item.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        reasoning = item.get("reasoning")
        reasoning = reasoning if isinstance(reasoning, dict) else {}
        parameters = item.get("supported_parameters")
        result.append(
            Model(
                id=ident,
                label=_str(item.get("name")) or ident,
                efforts=_strings(reasoning.get("supported_efforts")),
                default_effort=_str(reasoning.get("default_effort")),
                context_length=_int(item.get("context_length")),
                prompt_price=price(pricing.get("prompt")),
                completion_price=price(pricing.get("completion")),
                supports_tools=(
                    "tools" in parameters if isinstance(parameters, list) else None
                ),
            )
        )
    return tuple(result)


def fetch_json(url: str, timeout: float) -> Any:  # noqa: ANN401
    """GET `url` and decode its JSON body.

    Args:
        url: The address.
        timeout: Seconds the request may take.

    Returns:
        The decoded body.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _run_codex(argv: list[str], env: Mapping[str, str]) -> tuple[int, str, str]:
    """Run the Codex model list command with standard error kept apart.

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
    code, output, errors = _run_codex([binary, *CODEX_COMMAND], env)
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

    Args:
        engine: `codex`, `opencode` or `claude`.
        env: The environment; `None` reads `os.environ`.
        refresh: Read the source even when the cache is current.
        fetch: Reads OpenRouter's list; `None` uses `fetch_json`.
        now: The time; `None` for the current time.

    Returns:
        The list. When the source cannot be read it is the cached list with
        `source` `cache`, or an empty list with the source that was tried and
        `fetched_at` `None`; either way `reason` says what failed.

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
    held = _read_cache(path)
    if held is not None and not refresh and 0 <= at - held[0] < MAX_AGE:
        return ModelList(models=held[1], source=CACHE, fetched_at=held[0], reason=None)
    source = CLI if engine == "codex" else OPENROUTER
    try:
        if engine == "codex":
            models = _read_codex(env)
        else:
            reply = (fetch_json if fetch is None else fetch)(OPENROUTER_URL, TIMEOUT)
            models = parse_openrouter(reply)
    except status.ProbeError as error:
        reason = str(error)
    except (OSError, ValueError, TypeError) as error:
        reason = f"OpenRouter model list: {error}"
    else:
        payload = {"fetched_at": at, "models": [model.to_json() for model in models]}
        with contextlib.suppress(OSError):
            status.save_cache_file(path, payload, env)
        return ModelList(models=models, source=source, fetched_at=at, reason=None)
    if held is not None:
        return ModelList(
            models=held[1], source=CACHE, fetched_at=held[0], reason=reason
        )
    return ModelList(models=(), source=source, fetched_at=None, reason=reason)


def filter_openrouter(
    models: Iterable[Model],
    *,
    text: str | None = None,
    max_prompt_price: float | None = None,
    min_context: int | None = None,
    tools: bool | None = None,
) -> tuple[Model, ...]:
    """Keep the models that pass every given filter.

    A filter left at `None` keeps every model. A model whose value for an
    active filter is unknown is left out. Bounds are inclusive.

    Args:
        models: The models to filter.
        text: Text the id or label contains, ignoring case.
        max_prompt_price: The highest price per input token, in US dollars.
        min_context: The smallest context window, in tokens.
        tools: Whether the model must, or must not, take tool calls.

    Returns:
        The models kept, in their order.
    """
    needle = None if text is None else text.casefold()

    def keep(model: Model) -> bool:
        if needle is not None and not (
            needle in model.id.casefold() or needle in model.label.casefold()
        ):
            return False
        if max_prompt_price is not None and (
            model.prompt_price is None or model.prompt_price > max_prompt_price
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
