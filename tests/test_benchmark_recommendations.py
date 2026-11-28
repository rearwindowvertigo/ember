import json
from pathlib import Path
from typing import NoReturn

import pytest

from ember import benchmark_recommendations as recommendations
from ember import benchmarks, choices, paths
from ember.benchmark_recommendations import RecommendationPoint
from ember.benchmark_sources import Observation, SourceError
from ember.cli import main
from ember.engines import models, status


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PATH": "",
    }


def _connections(env: dict[str, str], *connected: str) -> None:
    entries = {
        app: status.EngineStatus(
            engine=app,
            state=status.CONNECTED if app in connected else status.NOT_SIGNED_IN,
            reason=None,
            installed=True,
            binary=None,
            version=None,
            signed_in=app in connected,
            connected=app in connected,
            requirements=(),
            checked_at=10.0,
        ).to_json()
        for app in status.ENGINES
    }
    status.save_cache_file(status.cache_path(env), {"engines": entries}, env)


def _catalogue(env: dict[str, str], app: str, *entries: models.Model) -> None:
    catalogue = models.ModelList(entries, models.CACHE, 10.0, None)
    status.save_cache_file(
        models.cache_path(app, env),
        {**catalogue.to_json(), "format_version": models.CACHE_VERSION},
        env,
    )


def _row(
    model: str,
    effort: str,
    score: float,
    cost: float,
    *,
    creator: str = "OpenAI",
) -> Observation:
    return Observation(
        model=model,
        model_creator=creator,
        harness=None,
        effort=effort,
        metrics={"intelligence_index": score, "task_usd": cost},
    )


def _index(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, *rows: Observation
) -> None:
    benchmarks.configure_artificial_analysis("fixture-key", env)
    with monkeypatch.context() as patch:
        patch.setattr(
            benchmarks, "read_artificial_analysis", lambda key: ("5.0", list(rows))
        )
        patch.setattr(benchmarks, "read", _unexpected)
        assert benchmarks.refresh(env)["status"] == "ok"


def _unexpected(*args: object, **kwargs: object) -> NoReturn:
    pytest.fail("Unexpected probe, download or cache write")


def _names(points: list[RecommendationPoint]) -> list[tuple[str, str]]:
    return [(point["model"], point["effort"]) for point in points]


def test_inclusive_decimal_cutoff_and_one_per_model_after_frontier(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _connections(env, "codex")
    _catalogue(
        env,
        "codex",
        models.Model("gpt-6.3", "GPT-6.3", ("high",)),
        models.Model("gpt-6.2-astra", "Astra", ("high",)),
        models.Model("gpt-6.2-sol", "Sol", ("minimal", "low", "medium", "high", "max")),
    )
    _index(
        env,
        monkeypatch,
        _row("GPT-6.3", "high", 64.4, 0.90),
        _row("GPT-6.2 Sol", "low", 59.4, 0.01),
        _row("GPT-6.2 Sol", "minimal", 59.3999999999999, 0),
        _row("GPT-6.2 Sol", "high", 60.1, 0.02),
        _row("GPT-6.2 Sol", "medium", 60.1, 0.020001),
        _row("GPT-6.2 Sol", "max", 59.5, 0.02),
        _row("GPT-6.2 Astra", "high", 61, 0.03),
    )

    report = recommendations.read(env)
    assert report["status"] == "ok"
    assert report["score_margin"] == 5 and not report["one_per_model"]
    assert report["best_score"] == 64.4 and report["minimum_score"] == 59.4
    assert _names(report["points"]) == [
        ("GPT-6.2 Sol", "minimal"),
        ("GPT-6.2 Sol", "low"),
        ("GPT-6.2 Sol", "high"),
        ("GPT-6.2 Sol", "max"),
        ("GPT-6.2 Sol", "medium"),
        ("GPT-6.2 Astra", "high"),
        ("GPT-6.3", "high"),
    ]
    frontier = [
        ("GPT-6.2 Sol", "minimal"),
        ("GPT-6.2 Sol", "low"),
        ("GPT-6.2 Sol", "high"),
        ("GPT-6.2 Astra", "high"),
        ("GPT-6.3", "high"),
    ]
    assert _names(report["frontier"]) == frontier
    assert _names(report["recommendations"]) == frontier[1:4]
    assert report["recommendations"][0]["gap"] == 5.0
    assert report["points"][0]["gap"] > 5

    choices.set_settings({"benchmarks.one_per_model": "on"}, env=env)
    one_per_model = recommendations.read(env)
    assert _names(one_per_model["frontier"]) == frontier
    assert _names(one_per_model["recommendations"]) == [frontier[i] for i in (1, 3, 4)]


def test_zero_score_zero_cost_and_ties_have_a_stable_order(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _connections(env, "codex")
    _catalogue(
        env,
        "codex",
        models.Model("gpt-6-beta", "Beta", ("max",)),
        models.Model("gpt-6-alpha", "Alpha", ("max", "high")),
        models.Model("gpt-6-gamma", "Gamma", ("max",)),
    )
    _index(
        env,
        monkeypatch,
        _row("GPT-6 Beta", "max", 0, 0),
        _row("GPT-6 Alpha", "max", 0, 0),
        _row("GPT-6 Alpha", "high", 0, 0),
        _row("GPT-6 Gamma", "max", 0, 0.000000001),
    )
    choices.set_settings({"benchmarks.score_margin": "0"}, env=env)
    report = recommendations.read(env)
    assert report["best_score"] == report["minimum_score"] == 0
    assert report["frontier"] == report["recommendations"]
    assert _names(report["recommendations"]) == [
        ("GPT-6 Alpha", "high"),
        ("GPT-6 Alpha", "max"),
        ("GPT-6 Beta", "max"),
    ]
    assert all(row["task_usd"] == row["gap"] == 0 for row in report["recommendations"])

    choices.set_settings({"benchmarks.one_per_model": "on"}, env=env)
    assert _names(recommendations.read(env)["recommendations"]) == [
        ("GPT-6 Alpha", "high"),
        ("GPT-6 Beta", "max"),
    ]


def test_saved_selectors_match_creator_release_and_effort_across_apps(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _connections(env, "codex")
    _catalogue(
        env,
        "codex",
        models.Model("gpt-6.2-sol", "Sol", ("max",)),
        models.Model("gpt-6.3", "GPT-6.3", ("high",)),
        models.Model("gpt-6.4-latest", "GPT-6.4", ("high",)),
    )
    _catalogue(
        env,
        "opencode",
        models.Model("openai/gpt-6.2-sol", "OpenAI", ("max",)),
        models.Model("openrouter/openai/gpt-6.2-sol", "OpenRouter", ("max", "max")),
        models.Model("openrouter/openai/gpt-6.2-sol:free", "GPT-6.2 Sol", ("max",)),
        models.Model("other/gpt-6.2-sol", "GPT-6.2 Sol", ("max",)),
        models.Model("anthropic/claude-opus-5-4", "Opus 5.4", ("max",)),
        models.Model("anthropic/claude-opus-5-5", "Opus 5.5", ("max", "high")),
        models.Model("anthropic/unknown", "Claude Opus 5.5", ("high",)),
        models.Model(
            "openrouter/deepseek/deepseek-v4.1-flash-1017", "Flash", ("high",)
        ),
    )
    _index(
        env,
        monkeypatch,
        {**_row("GPT-6.2 Sol", "max", 62, 0.02), "model_release_date": "2026-10-17"},
        _row("GPT-6.2 Sol", "high", 90, 0.01),
        _row("GPT-6.2 Sol", "max", 92, 0.01, creator="Same Name Labs"),
        _row("GPT-6.3", "high", 67, 0.5),
        _row("GPT-6.4", "high", 92, 0.01),
        _row("Claude Opus 5.4", "max", 58, 0.01, creator="Anthropic"),
        _row("Claude Opus 5.5", "high", 60, 0.015, creator="Anthropic"),
        _row("Claude Opus 5.6", "high", 98, 0.001, creator="Other"),
        _row("DeepSeek V4.1 Flash", "high", 95, 0.001, creator="DeepSeek"),
    )
    choices.set_settings(
        {
            "engine.codex": "off",
            "engine.opencode": "off",
            "engine.claude": "off",
            "shortlist.codex": ["gpt-6.2-sol@max"],
            "fast.codex": ["gpt-6.2-sol"],
        },
        env=env,
    )
    first = recommendations.read(env)
    assert first["selected_apps"] == ["all"]
    assert first["effective_apps"] == ["codex"]
    assert _names(first["points"]) == [("GPT-6.2 Sol", "max"), ("GPT-6.3", "high")]
    assert first["best_score"] == 67

    _connections(env, "codex", "claude", "opencode")
    expanded = recommendations.read(env)
    assert expanded["selected_apps"] == ["all"]
    assert expanded["effective_apps"] == list(status.ENGINES)
    assert len(expanded["points"]) == 4
    older, current, sol, _ = expanded["points"]
    assert older["model"] == "Claude Opus 5.4"
    assert older["routes"] == [
        {
            "app": "opencode",
            "model_id": "anthropic/claude-opus-5-4",
            "effort": "max",
            "is_alias": False,
        }
    ]
    assert current["model"] == "Claude Opus 5.5"
    assert current["routes"] == [
        {"app": "claude", "model_id": "opus", "effort": "high", "is_alias": True},
        {
            "app": "opencode",
            "model_id": "anthropic/claude-opus-5-5",
            "effort": "high",
            "is_alias": False,
        },
    ]
    assert sol["model"] == "GPT-6.2 Sol" and sol["creator"] == "OpenAI"
    assert sol["release_date"] == "2026-10-17"
    assert sol["routes"] == [
        {"app": "codex", "model_id": "gpt-6.2-sol", "effort": "max", "is_alias": False},
        {
            "app": "opencode",
            "model_id": "openai/gpt-6.2-sol",
            "effort": "max",
            "is_alias": False,
        },
        {
            "app": "opencode",
            "model_id": "openrouter/openai/gpt-6.2-sol",
            "effort": "max",
            "is_alias": False,
        },
    ]

    choices.set_settings({"benchmarks.lead_apps": ["claude", "opencode"]}, env=env)
    _connections(env, "codex", "claude")
    narrowed = recommendations.read(env)
    assert narrowed["selected_apps"] == ["claude", "opencode"]
    assert narrowed["connected_apps"] == ["codex", "claude"]
    assert narrowed["effective_apps"] == ["claude"]
    assert narrowed["best_score"] == 60 and narrowed["minimum_score"] == 55
    assert _names(narrowed["recommendations"]) == [("Claude Opus 5.5", "high")]


def test_incomplete_ambiguous_or_unavailable_pairs_do_not_rank(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _connections(env, "codex")
    _catalogue(
        env,
        "codex",
        models.Model("gpt-6-dated", "Dated", ("high", "max")),
        models.Model("gpt-6-split", "Split", ("high", "low", "max")),
        models.Model("gpt-6-absent", "Absent", ("high",), default_effort="max"),
        models.Model("gpt-6-fallback", "Fallback", ("high",)),
    )
    dated = _row("GPT-6 Dated", "high", 50, 1)
    split = _row("GPT-6 Split", "high", 50, 1)
    fallback = _row("GPT-6 Fallback", "high", 50, 1)
    _index(
        env,
        monkeypatch,
        {**dated, "model_release_date": "2026-10-17"},
        {**dated, "effort": "max", "model_release_date": "2026-11-17"},
        {**split, "metrics": {"intelligence_index": 50}},
        {**split, "metrics": {"task_usd": 0}},
        _row("GPT-6 Split", "low", 50, -1),
        {**split, "effort": "max", "metrics": {"intelligence_index": 0}},
        _row("GPT-6 Absent", "max", 50, 1),
        fallback,
        {**fallback, "configuration": "Default Fallback"},
    )
    report = recommendations.read(env)
    assert report["status"] == "no_comparable_pairs"
    assert report["best_score"] is report["minimum_score"] is None
    assert report["points"] == report["frontier"] == report["recommendations"] == []


def test_actionable_states_and_retained_data(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert recommendations.read(env)["status"] == "disabled"
    assert not paths.settings_db_path(env).exists()
    assert not status.cache_path(env).exists()
    benchmarks.configure_artificial_analysis("fixture-key", env)
    assert recommendations.read(env)["status"] == "not_checked"

    _index(env, monkeypatch, _row("GPT-6.2 Sol", "max", 64.4, 0.02))
    assert recommendations.read(env)["status"] == "missing_engine_cache"
    _connections(env, "claude")
    choices.set_settings({"benchmarks.lead_apps": ["codex"]}, env=env)
    disconnected = recommendations.read(env)
    assert disconnected["status"] == "no_connected_apps"
    assert disconnected["selected_apps"] == ["codex"]
    assert disconnected["effective_apps"] == []
    _connections(env, "codex", "opencode")
    missing = recommendations.read(env)
    assert missing["status"] == "missing_model_cache"
    assert missing["missing_model_caches"] == ["codex"]
    choices.set_settings({"benchmarks.lead_apps": []}, env=env)
    assert recommendations.read(env)["status"] == "no_apps_selected"
    choices.clear_settings(["benchmarks.lead_apps"], env=env)
    _catalogue(env, "codex", models.Model("gpt-6.2-sol", "Sol", ("max",)))

    def failed(key: str) -> tuple[str, list[Observation]]:
        raise SourceError("fixture Index failure")

    monkeypatch.setattr(benchmarks, "read_artificial_analysis", failed)
    retained = benchmarks.refresh(env)
    checked_at = next(
        source["checked_at"]
        for source in retained["sources"].values()
        if source["edition"] == "5.0"
    )
    assert checked_at is not None
    report = recommendations.read(env, now=checked_at + benchmarks.MAX_CHECK_AGE)
    assert report["status"] == "ok" and report["missing_model_caches"] == ["opencode"]
    assert report["source"] is not None
    assert report["source"]["checked_at"] == checked_at
    assert report["source"]["result"] == "retained" and report["source"]["stale"]
    assert _names(report["recommendations"]) == [("GPT-6.2 Sol", "max")]
    assert any(
        "last checked" in line and "refresh due" in line
        for line in recommendations.lines(report)
    )


def test_cli_reads_the_same_report_and_applied_settings_without_writes(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _connections(env, "claude", "codex")
    _index(
        env,
        monkeypatch,
        _row("Claude Opus 5.5", "low", 59.4, 0.02, creator="Anthropic"),
        _row("Claude Opus 5.5", "high", 64.4, 0.1, creator="Anthropic"),
    )
    argv = [
        "choices",
        "--set",
        "benchmarks.lead_apps=claude",
        "--set",
        "benchmarks.score_margin=0",
        "--set",
        "benchmarks.one_per_model=on",
    ]
    with monkeypatch.context() as owner:
        owner.setattr("sys.stdin.isatty", lambda: True)
        assert main(argv, env=env) == 0
    capsys.readouterr()
    before = choices.snapshot(env=env)
    monkeypatch.setattr(status, "_check_all", _unexpected)
    monkeypatch.setattr(status, "save_cache_file", _unexpected)
    monkeypatch.setattr(models, "_read_codex", _unexpected)
    monkeypatch.setattr(models, "_read_opencode", _unexpected)
    monkeypatch.setattr(benchmarks, "read_artificial_analysis", _unexpected)
    monkeypatch.setattr(benchmarks, "read", _unexpected)
    assert main(["benchmarks", "recommend", "--json"], env=env) == 0
    captured = capsys.readouterr()
    assert not captured.err
    data = json.loads(captured.out)
    assert data == recommendations.read(env)
    assert data["selected_apps"] == data["effective_apps"] == ["claude"]
    assert data["one_per_model"] and data["score_margin"] == 0
    [row] = data["recommendations"]
    assert row["model"] == "Claude Opus 5.5"
    assert row["effort"] == "high" and row["gap"] == 0
    assert row["score"] == 64.4 and row["task_usd"] == 0.1

    assert main(["benchmarks", "recommend"], env=env) == 0
    captured = capsys.readouterr()
    assert "Searched Claude Code" in captured.out
    assert "Within 0 Index points" in captured.out
    assert "One effort per model" in captured.out
    assert "Artificial Analysis Intelligence Index 5.0" in captured.out
    assert "Best comparable score 64.4 · cutoff 64.4" in captured.out
    assert "Claude Opus 5.5 (high)" in captured.out
    assert "Claude Code · opus @ high (family selector)" in captured.out
    assert not captured.err
    assert choices.snapshot(env=env) == before
