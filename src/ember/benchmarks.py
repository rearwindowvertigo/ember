"""Refresh and read the benchmark evidence for the owner's routing pool."""

import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypedDict

from ember import choices, paths
from ember.benchmark_sources import (
    SOURCES,
    Metric,
    Observation,
    Source,
    SourceError,
    read,
)
from ember.engines import status

MAX_CHECK_AGE = 30 * 24 * 60 * 60
FORMAT_VERSION = 1
EMPTY_POOL = "Add models to the routing pool in Ember’s Team screen first."

_HARNESSES = {
    "codex": frozenset({"codex", "codex cli"}),
    "claude": frozenset({"claude code"}),
    "opencode": frozenset({"opencode"}),
}
_CLAUDE_ALIASES = frozenset({"fable", "opus", "sonnet", "haiku"})


class RefreshBusyError(Exception):
    """Another process is already refreshing the benchmarks."""


class CacheError(OSError):
    """The benchmark cache exists but does not contain a readable snapshot."""


class _Snapshot(TypedDict):
    checked_at: float | None
    revision: str | None
    error: str | None
    rows: list[Observation]


type Cache = dict[str, _Snapshot]


class SourceStatus(TypedDict):
    """The benchmark and the status of its saved download."""

    title: str
    edition: str
    url: str
    publisher: str
    metrics: dict[str, Metric]
    pricing_basis: str | None
    checked_at: float | None
    revision: str | None
    stale: bool
    result: str
    error: str | None


class Related(TypedDict):
    """Published evidence that does not match the configured worker."""

    observation: Observation
    reason: str


class WorkerEvidence(TypedDict):
    """Available and missing metrics, keyed by benchmark series."""

    effort: str | None
    speed: str | None
    shortlisted: bool
    matches: dict[str, Observation]
    related: dict[str, Related]
    missing: dict[str, list[str]]


class Report(TypedDict):
    """Evidence for the current pool and the result of reading the sources."""

    status: str
    summary: str
    sources: dict[str, SourceStatus]
    workers: dict[str, WorkerEvidence]


def cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path to the atomically replaced benchmark cache."""
    return paths.cache_dir(env) / "benchmarks.json"


def _empty() -> _Snapshot:
    return {"checked_at": None, "revision": None, "error": None, "rows": []}


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _revision(rows: list[Observation]) -> str:
    return hashlib.sha256(_encode(rows).encode()).hexdigest()


def read_cache(env: Mapping[str, str] | None = None) -> Cache:
    """Read the last saved source snapshots locally."""
    path = cache_path(env)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, RecursionError) as error:
        raise CacheError(f"benchmark cache is not valid JSON: {path}") from error
    if not isinstance(payload, dict) or payload.get("format_version") != FORMAT_VERSION:
        raise CacheError(f"unsupported benchmark cache format: {path}")
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise CacheError(f"benchmark cache has no source snapshots: {path}")
    result: Cache = {}
    for key, saved in sources.items():
        if key in SOURCES:
            try:
                result[key] = _read_snapshot(saved)
            except (ValueError, OverflowError) as error:
                raise CacheError(f"invalid {key} snapshot in {path}") from error
    return result


def _read_snapshot(saved: object) -> _Snapshot:
    if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list):
        raise ValueError("expected a snapshot with rows")
    rows, error = saved["rows"], saved.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("invalid source error")
    checked_at, revision = saved.get("checked_at"), saved.get("revision")
    if not rows and checked_at is None and revision is None:
        return _Snapshot(checked_at=None, revision=None, error=error, rows=[])
    checked = status.finite_number(checked_at)
    if not rows or checked is None or revision != _revision(rows):
        raise ValueError("missing rows, check time or revision")
    return _Snapshot(checked_at=checked, revision=revision, error=error, rows=rows)


def _model_key(text: str) -> str:
    return re.sub(r"[ ._-]+", "-", text.casefold())


def _related_rows(
    rows: list[Observation], engine: str, model: str
) -> list[tuple[Observation, tuple[int, ...]]]:
    """Keep the named model, or known releases in an unpinned Claude family."""
    model_id = model.rsplit("/", 1)[-1] if engine == "opencode" else model
    key = _model_key(model_id)
    if engine == "opencode":
        return [
            (row, ())
            for row in rows
            if _model_key(row["model"].rsplit("/", 1)[-1]) == key
        ]
    if engine != "claude" or model not in _CLAUDE_ALIASES:
        return [(row, ()) for row in rows if _model_key(row["model"]) == key]
    pattern = re.compile(rf"claude-{model}-(\d+(?:-\d+)*)\Z")
    result = []
    for row in rows:
        if match := pattern.fullmatch(_model_key(row["model"])):
            result.append((row, tuple(int(part) for part in match[1].split("-"))))
    return result


def _same_harness(row: Observation, engine: str) -> bool:
    return (row["harness"] or "").casefold() in _HARNESSES[engine]


def _match_reason(
    row: Observation, engine: str, model: str, effort: str | None, speed: str | None
) -> str | None:
    if engine == "claude" and model in _CLAUDE_ALIASES:
        return "Claude model alias; the tested release is shown"
    if row["harness"] is None:
        return "the download does not specify a harness"
    if not _same_harness(row, engine):
        return "different harness"
    if engine == "opencode" and row["model"] != model:
        return "the download does not specify the OpenCode integration and model ID"
    if effort is None or row["effort"] is None:
        return "unspecified effort"
    if row["effort"].casefold() != effort.casefold():
        return "different effort"
    if speed == "fast":
        return "benchmark costs and scores do not specify Codex Fast"
    return None


def _worker(
    answers: Mapping[str, Mapping[str, Any]],
    worker: dict[str, Any],
    snapshots: Mapping[str, _Snapshot],
) -> WorkerEvidence:
    engine, model = worker["engine"], worker["model"]
    shortlist = choices.shortlist_models(
        engine, answers[f"shortlist.{engine}"]["value"]
    )
    effort = shortlist.get(model)
    speed = choices.codex_speed(answers, model) if engine == "codex" else None
    evidence = WorkerEvidence(
        effort=effort,
        speed=speed,
        shortlisted=model in shortlist,
        matches={},
        related={},
        missing={key: list(source.metrics) for key, source in SOURCES.items()},
    )
    if model not in shortlist:
        return evidence
    for key, snapshot in snapshots.items():
        candidates = _related_rows(snapshot["rows"], engine, model)
        if not candidates:
            continue
        exact = [
            row
            for row, _ in candidates
            if _match_reason(row, engine, model, effort, speed) is None
        ]
        if len(exact) == 1:
            row = exact[0]
            evidence["matches"][key] = row
            evidence["missing"][key] = [
                metric
                for metric in SOURCES[key].metrics
                if metric not in row["metrics"]
            ]
            continue
        row, _ = max(
            candidates,
            key=lambda candidate: (
                _same_harness(candidate[0], engine),
                candidate[1],  # Newest published release for a moving alias.
                (candidate[0]["effort"] or "").casefold() == (effort or "").casefold(),
                candidate[0].get("updated_at", ""),
            ),
        )
        reason = (
            "multiple published rows match this setup"
            if exact
            else _match_reason(row, engine, model, effort, speed)
        )
        assert reason is not None
        evidence["related"][key] = {"observation": row, "reason": reason}
    return evidence


def _source_status(
    source: Source, saved: _Snapshot, now: float, action: str | None
) -> SourceStatus:
    checked = saved["checked_at"]
    if action is None:
        if saved["error"]:
            action = "retained" if checked is not None else "unavailable"
        else:
            action = "cached" if checked is not None else "not_checked"
    return SourceStatus(
        title=source.title,
        edition=source.edition,
        url=source.url,
        publisher=source.publisher,
        metrics=source.metrics,
        pricing_basis=source.pricing_basis,
        checked_at=checked,
        revision=saved["revision"],
        stale=checked is not None and now - checked >= MAX_CHECK_AGE,
        result=action,
        error=saved["error"],
    )


def has_cost(worker: WorkerEvidence) -> bool:
    """Return whether a matching benchmark reports a cost for this worker."""
    return any(
        metric.endswith("_usd")
        for row in worker["matches"].values()
        for metric in row["metrics"]
    )


def _report(
    answers: Mapping[str, Mapping[str, Any]],
    snapshots: Mapping[str, _Snapshot],
    now: float,
    actions: Mapping[str, str],
) -> Report:
    sources = {
        key: _source_status(source, snapshots.get(key, _empty()), now, actions.get(key))
        for key, source in SOURCES.items()
    }
    workers = {
        worker["value"]: _worker(answers, worker, snapshots)
        for worker in choices.routing_rows(answers)
    }
    if not workers:
        return Report(
            status="empty_pool", summary=EMPTY_POOL, sources=sources, workers={}
        )
    errors = sum(source["error"] is not None for source in sources.values())
    checks = sum(source["checked_at"] is not None for source in sources.values())
    if errors == len(sources):
        outcome = "unavailable"
    elif errors or (0 < checks < len(sources)):
        outcome = "partial"
    elif not checks:
        outcome = "not_checked"
    else:
        outcome = "ok"
    covered = sum(bool(worker["matches"]) for worker in workers.values())
    costs = sum(has_cost(worker) for worker in workers.values())
    summary = (
        f"{covered}/{len(workers)} pool workers have a matching result; "
        f"{costs}/{len(workers)} have a matching benchmark cost."
    )
    return Report(status=outcome, summary=summary, sources=sources, workers=workers)


def cached(
    answers: Mapping[str, Mapping[str, Any]],
    env: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
) -> Report:
    """Match the saved benchmarks to the current pool, without network access."""
    return report(answers, read_cache(env), now=now)


def report(
    answers: Mapping[str, Mapping[str, Any]],
    cache: Cache,
    *,
    now: float | None = None,
) -> Report:
    """Project an already loaded cache onto the current routing pool."""
    return _report(answers, cache, time.time() if now is None else now, {})


def _refresh(path: Path, env: Mapping[str, str] | None) -> tuple[Cache, dict[str, str]]:
    snapshots = read_cache(env)
    actions = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {key: pool.submit(read, source) for key, source in SOURCES.items()}
        for key, job in jobs.items():
            previous = snapshots.get(key, _empty())
            try:
                rows = sorted(job.result(), key=_encode)
            except SourceError as error:
                previous["error"] = str(error)
                snapshots[key] = previous
                actions[key] = "retained" if previous["rows"] else "unavailable"
                continue
            revision = _revision(rows)
            snapshots[key] = _Snapshot(
                checked_at=time.time(), revision=revision, error=None, rows=rows
            )
            actions[key] = (
                "unchanged" if previous["revision"] == revision else "updated"
            )
    status.save_cache_file(
        path, {"format_version": FORMAT_VERSION, "sources": snapshots}, env
    )
    return snapshots, actions


def refresh(env: Mapping[str, str] | None = None) -> Report:
    """Refresh supported public results and report coverage of the current pool.

    A refresh is serialized with other refreshes and publishes whole series.
    It reads standing choices but does not claim or write any settings.
    """
    reply = choices.read(env=env)
    if reply["source_error"]:
        raise choices.InvalidChoiceError("; ".join(reply["errors"]))
    answers = reply["answers"]
    if not answers[choices.ROUTING_POOL]["value"]:
        return cached(answers, env)
    path = cache_path(env)
    paths.ensure_private_dir(path.parent, root=path.parent)
    fd = paths.open_private_file(path.with_suffix(".lock"), os.O_WRONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RefreshBusyError("a benchmark refresh is already running") from error
        snapshots, actions = _refresh(path, env)
    finally:
        os.close(fd)
    reply = choices.read(env=env)
    if reply["source_error"]:
        raise choices.InvalidChoiceError("; ".join(reply["errors"]))
    return _report(reply["answers"], snapshots, time.time(), actions)


def lines(report: Report) -> list[str]:
    """Return plain text refresh and source status lines for the terminal."""
    if report["status"] == "empty_pool":
        return [report["summary"]]
    result = [report["summary"]]
    for source in report["sources"].values():
        checked = source["checked_at"]
        date = (
            ""
            if checked is None
            else time.strftime(" (last checked %Y-%m-%d UTC)", time.gmtime(checked))
        )
        reminder = " · refresh due" if source["stale"] else ""
        error = "" if not source["error"] else f" — {source['error']}"
        result.append(
            f"{source['title']}: {source['result'].replace('_', ' ')}"
            f"{date}{reminder}{error}"
        )
    return result
