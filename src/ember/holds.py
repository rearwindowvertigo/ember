"""Provider holds: the machine's record of models and engines that failed.

A run that ends with a provider error starts a hold. `capacity` holds the
model; `usage_limit` and `outage` hold the engine, because another model of
that engine would fail the same way. A hold lasts the owner's
`provider.hold_minutes`. While it lasts, a job that would start on the held
worker starts on the entry of `provider.fallback` that applies. When it has
ended, the next job starts on the first choice again.

The record is `provider-holds.json` in the state folder: a list of holds,
replaced atomically under the lock `provider-holds.lock` beside it. Every
`emb` command on the machine reads the same record.
"""

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ember import choices, paths
from ember.engines import PROVIDER_KINDS, status

HOLDS_NAME = "provider-holds.json"
"""The record's file in the state folder."""

LOCK_NAME = "provider-holds.lock"
"""The lock file beside the record."""

ENGINE_WIDE_KINDS = choices.ENGINE_WIDE_KINDS
"""The provider failures that hold an engine, not one model."""


@dataclass(frozen=True)
class Hold:
    """One held model or engine."""

    engine: str
    """The engine."""

    model: str | None
    """The held model, or `None` when the whole engine is held."""

    kind: str
    """`capacity`, `usage_limit` or `outage`."""

    since: float
    """Epoch seconds when the failure was recorded."""

    until: float
    """Epoch seconds when the hold ends."""

    run: str | None = None
    """The run whose failure started the hold, when known."""

    def name(self) -> str:
        """Return `<engine>`, or `<engine> <model>` for a held model."""
        return self.engine if self.model is None else f"{self.engine} {self.model}"


def path(env: Mapping[str, str] | None = None) -> Path:
    """Return the record's path.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `provider-holds.json` in the state folder.
    """
    return paths.state_dir(env) / HOLDS_NAME


@contextlib.contextmanager
def _locked(env: Mapping[str, str] | None) -> Iterator[None]:
    folder = paths.ensure_private_dir(paths.state_dir(env), root=paths.state_dir(env))
    fd = paths.open_private_file(folder / LOCK_NAME)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _valid(item: object) -> Hold | None:
    if not isinstance(item, dict):
        return None
    engine, model, kind = item.get("engine"), item.get("model"), item.get("kind")
    since, until, run = item.get("since"), item.get("until"), item.get("run")
    if (
        not isinstance(engine, str)
        or not (model is None or isinstance(model, str))
        or kind not in PROVIDER_KINDS
        or isinstance(since, bool)
        or isinstance(until, bool)
        or not isinstance(since, (int, float))
        or not isinstance(until, (int, float))
        or not (run is None or isinstance(run, str))
    ):
        return None
    return Hold(engine, model, kind, float(since), float(until), run)


def _load(env: Mapping[str, str] | None) -> list[Hold]:
    """Read every well-formed hold; a missing or unreadable record is empty."""
    try:
        value = json.loads(path(env).read_text(encoding="utf-8"))
    except OSError, ValueError, RecursionError:
        return []
    if not isinstance(value, list):
        return []
    return [hold for item in value if (hold := _valid(item)) is not None]


def _save(holds: list[Hold], env: Mapping[str, str] | None) -> None:
    target = path(env)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}")
    try:
        fd = paths.open_private_file(temporary, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps([asdict(hold) for hold in holds], indent=1))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def active(
    env: Mapping[str, str] | None = None, now: float | None = None
) -> list[Hold]:
    """Return the holds that have not ended, oldest first.

    Args:
        env: The environment; `None` reads `os.environ`.
        now: The time in epoch seconds; `None` for the current time.

    Returns:
        The holds whose end lies after `now`.
    """
    moment = time.time() if now is None else now
    return sorted(
        (hold for hold in _load(env) if hold.until > moment),
        key=lambda hold: hold.since,
    )


def find(
    engine: str,
    model: str,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Hold | None:
    """Return the hold that covers a worker.

    Args:
        engine: The worker's engine.
        model: The worker's model.
        env: The environment; `None` reads `os.environ`.
        now: The time in epoch seconds; `None` for the current time.

    Returns:
        The engine's hold when the whole engine is held, else the model's
        hold, else `None`.
    """
    holds = [hold for hold in active(env, now) if hold.engine == engine]
    for wanted in (None, model):
        for hold in holds:
            if hold.model == wanted:
                return hold
    return None


def start(
    engine: str,
    model: str,
    kind: str,
    minutes: float,
    *,
    run: str | None = None,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Hold | None:
    """Record a provider failure as a hold.

    A new hold replaces an earlier one for the same model or engine, and
    holds that have ended are dropped from the record.

    Args:
        engine: The failed worker's engine.
        model: The failed worker's model.
        kind: The failure's provider kind.
        minutes: How long the hold lasts; zero or less records nothing.
        run: The run that failed.
        env: The environment; `None` reads `os.environ`.
        now: The time in epoch seconds; `None` for the current time.

    Returns:
        The hold, or `None` when `minutes` is not positive.

    Raises:
        ValueError: `kind` is not a provider kind.
    """
    if kind not in PROVIDER_KINDS:
        raise ValueError(f"unknown provider kind {kind!r}")
    if minutes <= 0:
        return None
    moment = time.time() if now is None else now
    held = None if kind in ENGINE_WIDE_KINDS else model
    hold = Hold(engine, held, kind, moment, moment + minutes * 60, run)
    with _locked(env):
        kept = [
            item
            for item in _load(env)
            if item.until > moment and (item.engine, item.model) != (engine, held)
        ]
        _save([*kept, hold], env)
    return hold


def release(
    engine: str,
    model: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """End the hold of a model, or of a whole engine.

    Args:
        engine: The engine.
        model: The held model; `None` for the engine's own hold.
        env: The environment; `None` reads `os.environ`.

    Returns:
        Whether a hold was removed.
    """
    with _locked(env):
        holds = _load(env)
        kept = [item for item in holds if (item.engine, item.model) != (engine, model)]
        if len(kept) == len(holds):
            return False
        _save(kept, env)
    return True


def succeeded(engine: str, model: str, *, env: Mapping[str, str] | None = None) -> None:
    """End the holds that a successful turn on a worker disproves.

    Args:
        engine: The worker's engine.
        model: The worker's model.
        env: The environment; `None` reads `os.environ`.
    """
    if not path(env).exists():
        return
    with _locked(env):
        holds = _load(env)
        kept = [
            item
            for item in holds
            if item.engine != engine or item.model not in (None, model)
        ]
        if len(kept) != len(holds):
            _save(kept, env)


def replacement(
    answers: Mapping[str, Mapping[str, Any]],
    failed: Iterable[tuple[str, str, str]],
    *,
    avoid: Iterable[tuple[str, str]] = (),
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return the fallback list entry that takes over from a failed worker.

    It is the entry `choices.fallback_worker` picks with the engines'
    cached connection states, passing over every entry that is itself held.

    Args:
        answers: The resolved answers.
        failed: The `(engine, model, kind)` of each worker that failed on
            the job.
        avoid: Other `(engine, model)` pairs not to use.
        env: The environment; `None` reads `os.environ`.
        now: The time in epoch seconds; `None` for the current time.

    Returns:
        The entry's row with `engine`, `model` and `effort`, or `None`.
    """
    held = status.cached(env)
    connected = (
        None if held is None else {name: item.connected for name, item in held.items()}
    )
    failed, skipped = list(failed), list(avoid)
    while True:
        row = choices.fallback_worker(
            answers, failed, avoid=skipped, connected=connected
        )
        if row is None or find(row["engine"], row["model"], env, now) is None:
            return row
        skipped.append((row["engine"], row["model"]))


def minutes(answers: Mapping[str, Mapping[str, Any]]) -> int:
    """Return how long a hold lasts, from `provider.hold_minutes`."""
    return int(answers["provider.hold_minutes"]["value"])
