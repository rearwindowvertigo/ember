"""Refresh and read the benchmark evidence for shortlisted workers."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from ember import choices, paths
from ember.benchmark_sources import (
    AA_METRICS,
    AA_TITLE,
    AA_URL,
    ARTIFICIAL_ANALYSIS,
    SOURCES,
    Metric,
    Observation,
    Source,
    SourceError,
    read,
    read_artificial_analysis,
)
from ember.engines import models, status

MAX_CHECK_AGE = 30 * 24 * 60 * 60
FORMAT_VERSION = 1
EMPTY_POOL = (
    "Shortlist models in Engines to view benchmarks. Approve automatic workers "
    "in the Routing pool on Ember’s Team screen."
)

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
    edition: NotRequired[str]


type Cache = dict[str, _Snapshot]
type EffortCatalog = dict[str, dict[str, tuple[str, ...]]]


class _CheckStatus(TypedDict):
    checked_at: float | None
    revision: str | None
    stale: bool
    result: str
    error: str | None


class SourceStatus(_CheckStatus):
    """The benchmark and the status of its saved download."""

    title: str
    edition: str
    url: str
    publisher: str
    metrics: dict[str, Metric]
    pricing_basis: str | None


class Related(TypedDict):
    """Published evidence that does not match the configured worker."""

    observation: Observation
    reason: str


class ModelReference(TypedDict):
    """Published model-level evidence by effort, independent of the engine."""

    source: str
    model: str | None
    effort_catalog_available: bool
    efforts: dict[str, Observation | None]


class WorkerEvidence(TypedDict):
    """Available and missing metrics, keyed by benchmark series."""

    effort: str | None
    speed: str | None
    shortlisted: bool
    supported_efforts: tuple[str, ...] | None
    matches: dict[str, Observation]
    matches_by_effort: dict[str, dict[str, Observation]]
    related: dict[str, Related]
    missing: dict[str, list[str]]
    model_reference: NotRequired[ModelReference]


class Report(TypedDict):
    """Saved evidence for the routing pool and other shortlisted workers."""

    status: str
    summary: str
    sources: dict[str, SourceStatus]
    workers: dict[str, WorkerEvidence]
    other_shortlisted_workers: dict[str, WorkerEvidence]


def cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path to the atomically replaced benchmark cache."""
    return paths.cache_dir(env) / "benchmarks.json"


def artificial_analysis_directory(env: Mapping[str, str] | None = None) -> Path:
    """Return the private folder containing the optional account's files."""
    return paths.config_dir(env) / "artificial-analysis"


def _key_path(env: Mapping[str, str] | None) -> Path:
    return artificial_analysis_directory(env) / "api-key"


def _reference_path(env: Mapping[str, str] | None) -> Path:
    return artificial_analysis_directory(env) / "benchmarks.json"


def _valid_key(key: str) -> bool:
    return re.fullmatch(r"[!-~]{1,512}", key) is not None


def _read_key(env: Mapping[str, str] | None) -> str | None:
    path = _key_path(env)
    try:
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise OSError("the API key directory is not private")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
        ):
            raise OSError("the API key file is not private")
        key = stream.read(513).decode("ascii")
    if not _valid_key(key):
        raise ValueError("invalid API key")
    return key


@contextlib.contextmanager
def _refresh_lock(env: Mapping[str, str] | None) -> Iterator[None]:
    path = cache_path(env)
    paths.ensure_private_dir(path.parent, root=path.parent)
    fd = paths.open_private_file(path.with_suffix(".lock"), os.O_WRONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RefreshBusyError("a benchmark refresh is already running") from error
        yield
    finally:
        os.close(fd)


def configure_artificial_analysis(
    key: str | None, env: Mapping[str, str] | None = None
) -> None:
    """Save or remove the owner's local API key without accessing the network."""
    if key is not None:
        key = key.strip()
        if not _valid_key(key):
            raise ValueError("Enter an Artificial Analysis API key without spaces.")
    with _refresh_lock(env):
        path = _key_path(env)
        if key is not None:
            paths.replace_private_file(
                path, key.encode("ascii"), root=paths.config_dir(env)
            )
        elif path.parent.exists() or path.parent.is_symlink():
            paths.ensure_private_dir(path.parent, root=paths.config_dir(env))
            _reference_path(env).unlink(missing_ok=True)
            path.unlink(missing_ok=True)


def _empty(error: str | None = None) -> _Snapshot:
    return {"checked_at": None, "revision": None, "error": error, "rows": []}


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _revision(rows: list[Observation]) -> str:
    return hashlib.sha256(_encode(rows).encode()).hexdigest()


def _read_public_cache(env: Mapping[str, str] | None) -> Cache:
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


def _read_reference(
    env: Mapping[str, str] | None,
) -> tuple[str | None, _Snapshot | None]:
    try:
        key = _read_key(env)
    except OSError, ValueError:
        return None, _empty("Could not read the local API key.")
    if key is None:
        return None, None
    try:
        value = json.loads(_reference_path(env).read_text(encoding="utf-8"))
        snapshot = _read_snapshot(value)
        if snapshot["rows"]:
            edition = value.get("edition")
            if not isinstance(edition, str) or not re.fullmatch(r"\d+\.\d+", edition):
                raise ValueError("invalid index edition")
            snapshot["edition"] = edition
    except FileNotFoundError:
        snapshot = _empty()
    except OSError:
        return None, _empty("Could not read the local benchmark data.")
    except ValueError, OverflowError, RecursionError:
        snapshot = _empty("Invalid local benchmark data.")
    return key, snapshot


def read_cache(env: Mapping[str, str] | None = None) -> Cache:
    """Read the last saved source snapshots, without exposing the optional API key."""
    snapshots = _read_public_cache(env)
    _, reference = _read_reference(env)
    if reference is not None:
        snapshots[ARTIFICIAL_ANALYSIS] = reference
    return snapshots


def effort_catalog(env: Mapping[str, str] | None = None) -> EffortCatalog:
    """Read the saved effort lists for matching benchmark evidence locally."""
    return {engine: models.saved_efforts(engine, env) for engine in status.ENGINES}


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
    saved_efforts: tuple[str, ...] | None,
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
        supported_efforts=saved_efforts,
        matches={},
        matches_by_effort={},
        related={},
        missing={key: list(source.metrics) for key, source in SOURCES.items()},
    )
    if model not in shortlist:
        return evidence
    for key, snapshot in snapshots.items():
        if key not in SOURCES:
            continue
        candidates = _related_rows(snapshot["rows"], engine, model)
        if not candidates:
            continue
        exact_by_effort: dict[str, list[Observation]] = {}
        for row, _ in candidates:
            tested = row["effort"]
            if tested is not None and (
                _match_reason(row, engine, model, tested, speed) is None
            ):
                exact_by_effort.setdefault(tested.casefold(), []).append(row)
        for supported in saved_efforts or ():
            matches = exact_by_effort.get(supported.casefold(), [])
            if len(matches) == 1:
                by_series = evidence["matches_by_effort"].setdefault(supported, {})
                by_series[key] = matches[0]
        exact = [] if effort is None else exact_by_effort.get(effort.casefold(), [])
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


def _model_reference(
    row: dict[str, Any],
    current_effort: str | None,
    saved_efforts: tuple[str, ...] | None,
    series: str,
    published: list[Observation],
) -> ModelReference:
    engine, model = row["engine"], row["model"]
    candidates = _related_rows(published, engine, model)
    vendor = {"claude": "anthropic", "codex": "openai"}.get(engine)
    if engine == "opencode" and model.count("/") >= 2:
        vendor = model.rsplit("/", 2)[1]
    if vendor:
        candidates = [
            (item, version)
            for item, version in candidates
            if _model_key(item.get("model_creator", "")) == _model_key(vendor)
        ]
    latest = max((version for _, version in candidates), default=())
    release = [item for item, version in candidates if version == latest]
    # Use a single named release for all efforts, including for moving aliases.
    releases = {
        (_model_key(item["model"]), _model_key(item.get("model_creator", "")))
        for item in release
    }
    dates = {date for item in release if (date := item.get("model_release_date"))}
    if len(releases) != 1 or len(dates) > 1:
        release = []
    efforts = saved_efforts
    if efforts is None:
        efforts = () if current_effort is None else (current_effort,)
    evidence: dict[str, Observation | None] = {}
    for effort in efforts:
        matches = [item for item in release if item["effort"] == effort]
        evidence[effort] = matches[0] if len(matches) == 1 else None
    return ModelReference(
        source=series,
        model=release[0]["model"] if release else None,
        effort_catalog_available=saved_efforts is not None,
        efforts=evidence,
    )


def _check_status(saved: _Snapshot, now: float, action: str | None) -> _CheckStatus:
    checked = saved["checked_at"]
    if action is None:
        if saved["error"]:
            action = "retained" if checked is not None else "unavailable"
        else:
            action = "cached" if checked is not None else "not_checked"
    return _CheckStatus(
        checked_at=checked,
        revision=saved["revision"],
        stale=checked is not None and now - checked >= MAX_CHECK_AGE,
        result=action,
        error=saved["error"],
    )


def _source_status(
    source: Source, saved: _Snapshot, now: float, action: str | None
) -> SourceStatus:
    return SourceStatus(
        title=source.title,
        edition=source.edition,
        url=source.url,
        publisher=source.publisher,
        metrics=source.metrics,
        pricing_basis=source.pricing_basis,
        **_check_status(saved, now, action),
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
    efforts: EffortCatalog,
) -> Report:
    sources = {
        key: _source_status(source, snapshots.get(key, _empty()), now, actions.get(key))
        for key, source in SOURCES.items()
    }
    pool = {row["value"]: row for row in choices.routing_rows(answers)}
    other_shortlisted = {
        f"{engine}:{model}": {"engine": engine, "model": model}
        for engine in status.ENGINES
        for model in choices.shortlist_models(
            engine, answers[f"shortlist.{engine}"]["value"]
        )
        if f"{engine}:{model}" not in pool
    }
    rows = pool | other_shortlisted
    evidence = {
        key: _worker(
            answers, row, snapshots, efforts.get(row["engine"], {}).get(row["model"])
        )
        for key, row in rows.items()
    }
    reference = snapshots.get(ARTIFICIAL_ANALYSIS)
    if reference is not None:
        edition = reference.get("edition")
        series = f"{ARTIFICIAL_ANALYSIS}-{edition}" if edition else ARTIFICIAL_ANALYSIS
        sources[series] = SourceStatus(
            title=AA_TITLE,
            edition=edition or "unknown",
            url=AA_URL,
            publisher="Artificial Analysis",
            metrics=AA_METRICS,
            pricing_basis="Publisher's weighted API inference cost per Index task",
            **_check_status(reference, now, actions.get(ARTIFICIAL_ANALYSIS)),
        )
        for key, row in rows.items():
            worker = evidence[key]
            if worker["shortlisted"]:
                worker["model_reference"] = _model_reference(
                    row,
                    worker["effort"],
                    worker["supported_efforts"],
                    series,
                    reference["rows"],
                )
    if not rows:
        return Report(
            status="empty_pool",
            summary=EMPTY_POOL,
            sources=sources,
            workers={},
            other_shortlisted_workers={},
        )
    workers = {key: evidence[key] for key in pool}
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
        if workers
        else "No workers in the Routing pool."
    )
    if reference is not None:
        variants = [
            observation
            for worker in evidence.values()
            if "model_reference" in worker
            for observation in worker["model_reference"]["efforts"].values()
        ]
        scores = sum(
            row is not None and "intelligence_index" in row["metrics"]
            for row in variants
        )
        priced = sum(
            row is not None and "task_usd" in row["metrics"] for row in variants
        )
        summary += (
            f" Artificial Analysis: {scores}/{len(variants)} listed efforts "
            f"have an Index score; {priced}/{len(variants)} have a cost."
        )
    return Report(
        status=outcome,
        summary=summary,
        sources=sources,
        workers=workers,
        other_shortlisted_workers={key: evidence[key] for key in other_shortlisted},
    )


def cached(
    answers: Mapping[str, Mapping[str, Any]],
    env: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
) -> Report:
    """Match saved benchmarks to the shortlists and pool without network access."""
    cache = read_cache(env)
    return report(answers, cache, efforts=effort_catalog(env), now=now)


def report(
    answers: Mapping[str, Mapping[str, Any]],
    cache: Cache,
    *,
    efforts: EffortCatalog | None = None,
    now: float | None = None,
) -> Report:
    """Project an already loaded cache onto the shortlists and routing pool."""
    checked_at = time.time() if now is None else now
    return _report(answers, cache, checked_at, {}, efforts or {})


def _refresh_reference(
    env: Mapping[str, str] | None, key: str, previous: _Snapshot
) -> tuple[_Snapshot, str]:
    snapshot: _Snapshot
    try:
        edition, downloaded = read_artificial_analysis(key)
        rows = sorted(downloaded, key=_encode)
        revision = _revision(rows)
        snapshot = _Snapshot(
            edition=edition,
            checked_at=time.time(),
            revision=revision,
            error=None,
            rows=rows,
        )
    except SourceError as error:
        snapshot = {**previous, "error": str(error)}
    try:
        paths.replace_private_file(
            _reference_path(env),
            _encode(snapshot).encode("utf-8"),
            root=paths.config_dir(env),
        )
    except OSError:
        snapshot = {**previous, "error": "Could not save the local benchmark data."}
    if snapshot["error"]:
        return snapshot, "retained" if snapshot["rows"] else "unavailable"
    unchanged = snapshot["revision"] == previous["revision"] and snapshot.get(
        "edition"
    ) == previous.get("edition")
    return snapshot, "unchanged" if unchanged else "updated"


def _refresh(path: Path, env: Mapping[str, str] | None) -> tuple[Cache, dict[str, str]]:
    snapshots = _read_public_cache(env)
    api_key, reference = _read_reference(env)
    actions = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        reference_job = (
            pool.submit(_refresh_reference, env, api_key, reference)
            if api_key is not None and reference is not None
            else None
        )
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
        if reference_job is not None:
            reference, actions[ARTIFICIAL_ANALYSIS] = reference_job.result()
    if reference is not None:
        snapshots[ARTIFICIAL_ANALYSIS] = reference
    return snapshots, actions


def refresh(env: Mapping[str, str] | None = None) -> Report:
    """Refresh public and opted-in results and report coverage of the current pool.

    A refresh is serialized with other refreshes and publishes whole series.
    It reads standing choices but does not claim or write any settings.
    """
    reply = choices.read(env=env)
    if reply["source_error"]:
        raise choices.InvalidChoiceError("; ".join(reply["errors"]))
    answers = reply["answers"]
    if not answers[choices.ROUTING_POOL]["value"] and not any(
        answers[f"shortlist.{engine}"]["value"] for engine in status.ENGINES
    ):
        return cached(answers, env)
    with _refresh_lock(env):
        snapshots, actions = _refresh(cache_path(env), env)
        reply = choices.read(env=env)
        if reply["source_error"]:
            raise choices.InvalidChoiceError("; ".join(reply["errors"]))
        return _report(
            reply["answers"],
            snapshots,
            time.time(),
            actions,
            effort_catalog(env),
        )


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
