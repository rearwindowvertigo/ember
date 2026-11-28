"""Rank lead model and effort pairs from the saved Intelligence Index."""

from collections.abc import Iterable, Mapping
from fractions import Fraction
from typing import Literal, TypedDict

from ember import benchmarks, choices
from ember.benchmark_sources import Observation
from ember.engines import models, status

_APP_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
_APP_CREATORS = {"claude": "anthropic", "codex": "openai"}

type RecommendationStatus = Literal[
    "ok",
    "no_apps_selected",
    "disabled",
    "not_checked",
    "unavailable",
    "missing_engine_cache",
    "no_connected_apps",
    "missing_model_cache",
    "no_comparable_pairs",
]

_SUMMARIES: dict[RecommendationStatus, str] = {
    "ok": "Lead recommendations from saved Index measurements.",
    "no_apps_selected": "Select at least one lead app in Benchmarks.",
    "disabled": "Add an Artificial Analysis API key and refresh in Benchmarks.",
    "not_checked": "Use Refresh in Benchmarks to download the Index.",
    "unavailable": "Resolve the Index source error and use Refresh in Benchmarks.",
    "missing_engine_cache": "Check app connections in Engines to save their status.",
    "no_connected_apps": (
        "Connect an app in Engines or select a connected app in Benchmarks."
    ),
    "missing_model_cache": "Refresh model catalogues in Engines.",
    "no_comparable_pairs": (
        "The saved models and efforts have no matching Index scores with costs. "
        "Refresh model catalogues in Engines to check which pairs are available."
    ),
}


class RecommendationRoute(TypedDict):
    """The app, exact selectable model ID and effort saved for a pair."""

    app: str
    model_id: str
    effort: str
    is_alias: bool


class RecommendationPoint(TypedDict):
    """A measured release and effort with at least one available route."""

    model: str
    creator: str
    release_date: str | None
    effort: str
    configuration: str | None
    score: float
    gap: float
    task_usd: float
    routes: list[RecommendationRoute]


class RecommendationReport(TypedDict):
    """Applied settings, Index source status and points for the chart and table.

    Points include every comparable pair in the effective scope. Points and
    frontier are ordered by cost, then descending score and release identity.
    Recommendations are the first three eligible frontier pairs after applying
    the saved score margin and effort limit.
    """

    status: RecommendationStatus
    summary: str
    source: benchmarks.SourceStatus | None
    score_margin: int
    one_per_model: bool
    selected_apps: list[str]
    connected_apps: list[str]
    effective_apps: list[str]
    missing_model_caches: list[str]
    best_score: float | None
    minimum_score: float | None
    points: list[RecommendationPoint]
    frontier: list[RecommendationPoint]
    recommendations: list[RecommendationPoint]


def _published_releases(
    rows: Iterable[Observation],
) -> dict[benchmarks.ModelIdentity, list[Observation]]:
    releases: dict[benchmarks.ModelIdentity, list[Observation]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model, creator = row.get("model"), row.get("model_creator")
        if not isinstance(model, str) or not model.strip():
            continue
        if not isinstance(creator, str) or not creator.strip():
            continue
        date = row.get("model_release_date")
        if date is not None and not isinstance(date, str):
            continue
        identity = benchmarks.model_identity(model, creator)
        releases.setdefault(identity, []).append(row)
    return releases


def _claude_releases(
    releases: Iterable[benchmarks.ModelIdentity],
) -> dict[str, benchmarks.ModelIdentity]:
    selected = {}
    for alias in models.CLAUDE_MODELS:
        family = [
            (version, identity)
            for identity in releases
            if identity[0] == _APP_CREATORS["claude"]
            and (version := benchmarks.claude_alias_version(identity[1], alias))
            is not None
        ]
        if family:
            latest, identity = max(family)
            if sum(version == latest for version, _ in family) == 1:
                selected[alias] = identity
    return selected


def _exact_release(app: str, model_id: str) -> benchmarks.ModelIdentity | None:
    if app != "opencode":
        return benchmarks.model_identity(model_id, _APP_CREATORS[app])
    # A direct integration supplies the creator, or the selectable ID names it
    # explicitly under the integration. Keep release and variant suffixes.
    parts = model_id.split("/")
    if len(parts) in (2, 3) and all(parts):
        return benchmarks.model_identity(parts[-1], parts[-2])
    return None


def _release_points(rows: list[Observation]) -> dict[str, RecommendationPoint]:
    dates = benchmarks.model_release_dates(rows)
    if len(dates) > 1:
        return {}
    release_date = next(iter(dates), None)
    by_effort: dict[str, list[Observation]] = {}
    for row in rows:
        effort = row.get("effort")
        if isinstance(effort, str) and effort:
            by_effort.setdefault(effort, []).append(row)
    result = {}
    for effort, observations in by_effort.items():
        if len(observations) != 1:
            continue
        row = observations[0]
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            continue
        try:
            score = status.finite_number(metrics.get("intelligence_index"))
            cost = status.finite_number(metrics.get("task_usd"))
        except OverflowError:
            continue
        if score is None or cost is None or cost < 0:
            continue
        result[effort] = RecommendationPoint(
            model=row["model"],
            creator=row["model_creator"],
            release_date=release_date,
            effort=effort,
            configuration=row.get("configuration"),
            score=score,
            gap=0.0,
            task_usd=cost,
            routes=[],
        )
    return result


def _point_identity(point: RecommendationPoint) -> benchmarks.ModelIdentity:
    return benchmarks.model_identity(point["model"], point["creator"])


def _points(
    rows: list[Observation], catalogues: Mapping[str, models.ModelList]
) -> list[RecommendationPoint]:
    releases = _published_releases(rows)
    aliases = _claude_releases(releases)
    measured = {
        identity: _release_points(group) for identity, group in releases.items()
    }
    for app, catalogue in sorted(catalogues.items()):
        for model in sorted(catalogue.models, key=lambda model: model.id):
            alias = app == "claude" and model.id in models.CLAUDE_MODELS
            identity = aliases.get(model.id) if alias else _exact_release(app, model.id)
            if identity is None or identity not in measured:
                continue
            for effort in model.efforts:
                point = measured[identity].get(effort)
                if point is None:
                    continue
                route = RecommendationRoute(
                    app=app, model_id=model.id, effort=effort, is_alias=alias
                )
                if route not in point["routes"]:
                    point["routes"].append(route)
    return sorted(
        (
            point
            for efforts in measured.values()
            for point in efforts.values()
            if point["routes"]
        ),
        key=lambda point: (
            point["task_usd"],
            -point["score"],
            _point_identity(point),
            point["effort"],
        ),
    )


def _frontier(points: list[RecommendationPoint]) -> list[RecommendationPoint]:
    result: list[RecommendationPoint] = []
    for point in points:
        if result:
            best = result[-1]
            if point["score"] < best["score"] or (
                point["score"] == best["score"] and point["task_usd"] > best["task_usd"]
            ):
                continue
        result.append(point)
    return result


def _rank(report: RecommendationReport) -> None:
    points = report["points"]
    best = max(point["score"] for point in points)
    # Subtract using the saved decimal values so equality at the cutoff holds.
    exact_best = Fraction(str(best))
    minimum = exact_best - report["score_margin"]
    report["best_score"] = best
    report["minimum_score"] = float(minimum)
    for point in points:
        point["gap"] = float(exact_best - Fraction(str(point["score"])))
    report["frontier"] = _frontier(points)
    seen: set[benchmarks.ModelIdentity] = set()
    for point in report["frontier"]:
        if Fraction(str(point["score"])) < minimum:
            continue
        identity = _point_identity(point)
        if report["one_per_model"] and identity in seen:
            continue
        seen.add(identity)
        report["recommendations"].append(point)
        if len(report["recommendations"]) == 3:
            break


def _report_status(
    report: RecommendationReport, connection_cache_available: bool
) -> RecommendationStatus:
    if not report["selected_apps"]:
        return "no_apps_selected"
    source = report["source"]
    if source is None:
        return "disabled"
    if source["checked_at"] is None:
        return "unavailable" if source["error"] else "not_checked"
    if not connection_cache_available:
        return "missing_engine_cache"
    if not report["effective_apps"]:
        return "no_connected_apps"
    if report["points"]:
        return "ok"
    return (
        "missing_model_cache"
        if report["missing_model_caches"]
        else "no_comparable_pairs"
    )


def read(
    env: Mapping[str, str] | None = None, *, now: float | None = None
) -> RecommendationReport:
    """Read and rank saved lead inputs without claims, probes or writes."""
    settings = choices.read(env=env)
    if settings["source_error"]:
        raise choices.InvalidChoiceError("\n".join(settings["errors"]))
    answers = settings["answers"]
    selected = list(answers[choices.BENCHMARK_LEAD_APPS]["value"])
    connections = status.cached(env)
    connected = [
        app for app in status.ENGINES if connections and connections[app].connected
    ]
    effective = [app for app in connected if selected == ["all"] or app in selected]
    with models.without_cache_writes(saved_only=True):
        catalogues = {app: models.list_models(app, env, now=now) for app in effective}
    source, rows = benchmarks.read_index(env, now=now)
    result = RecommendationReport(
        status="ok",
        summary="",
        source=source,
        score_margin=int(answers[choices.BENCHMARK_SCORE_MARGIN]["value"]),
        one_per_model=answers[choices.BENCHMARK_ONE_PER_MODEL]["value"] == "on",
        selected_apps=selected,
        connected_apps=connected,
        effective_apps=effective,
        missing_model_caches=[
            app for app, catalogue in catalogues.items() if catalogue.fetched_at is None
        ],
        best_score=None,
        minimum_score=None,
        points=_points(rows, catalogues) if rows else [],
        frontier=[],
        recommendations=[],
    )
    result["status"] = _report_status(result, connections is not None)
    result["summary"] = _SUMMARIES[result["status"]]
    if result["points"]:
        _rank(result)
    return result


def _app_names(apps: list[str]) -> str:
    return ", ".join(_APP_NAMES[app] for app in apps) or "none"


def lines(report: RecommendationReport) -> list[str]:
    """Format applied settings, source status and recommended pairs for text output."""
    selected = report["selected_apps"]
    selection = "All connected apps" if selected == ["all"] else _app_names(selected)
    effort_limit = (
        "One effort per model"
        if report["one_per_model"]
        else "Multiple efforts per model allowed"
    )
    margin = report["score_margin"]
    units = "point" if margin == 1 else "points"
    effective = _app_names(report["effective_apps"])
    result = [
        f"Lead apps set to {selection}. Searched {effective}.",
        f"Within {margin} Index {units} of the best. {effort_limit}.",
    ]
    source = report["source"]
    if source is not None:
        result.append(benchmarks.source_line(source, show_edition=True))
    if report["missing_model_caches"]:
        apps = _app_names(report["missing_model_caches"])
        result.append(f"Refresh model catalogues in Engines for {apps}.")
    best = report["best_score"]
    if best is None:
        if report["status"] != "missing_model_cache":
            result.append(report["summary"])
        return result
    result.append(f"Best comparable score {best} · cutoff {report['minimum_score']}")
    if source is not None and source["pricing_basis"]:
        result.append(source["pricing_basis"])
    for index, point in enumerate(report["recommendations"], 1):
        configuration = point["configuration"]
        effort = (
            f"{point['effort']}, {configuration}" if configuration else point["effort"]
        )
        result.append(
            f"{index}. {point['model']} ({effort}) · {point['score']} Index points "
            f"({point['gap']} below best) · ${point['task_usd']} per Index task"
        )
        for route in point["routes"]:
            app = _APP_NAMES[route["app"]]
            alias = " (family selector)" if route["is_alias"] else ""
            result.append(f"   {app} · {route['model_id']} @ {route['effort']}{alias}")
    return result
