import fcntl
import json
import os
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from ember import benchmark_sources, benchmarks, choices, paths
from ember.benchmark_sources import (
    ARTIFICIAL_ANALYSIS,
    SOURCES,
    Observation,
    SourceError,
)
from ember.cli import main
from ember.engines import models, status

TB41 = "terminal-bench-4.1"
TB21 = "terminal-bench-2.1"
LIVE = "livebench-2026-11-06"
WORKER = "codex:gpt-6.2-sol"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PATH": "",
    }


@pytest.fixture
def pool(env: dict[str, str]) -> dict[str, str]:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-6.2-sol@max"],
            "shortlist.claude": ["opus@max"],
            "shortlist.opencode": ["openai/gpt-6.2-sol@max"],
            "routing.pool": {
                WORKER: {
                    "usage": 8,
                    "scores": {"terminal": 99},
                    "notes": "Owner notes",
                },
                "claude:opus": {"scores": {}},
                "opencode:openai/gpt-6.2-sol": {"scores": {}},
            },
        },
        env=env,
    )
    return env


def _fail() -> list[Observation]:
    raise SourceError("fixture download failed")


def _source(monkeypatch: pytest.MonkeyPatch, key: str, *rows: Observation) -> None:
    reader = (lambda: list(rows)) if rows else _fail
    monkeypatch.setitem(SOURCES, key, replace(SOURCES[key], read=reader))


def _run(
    action: str, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    code = main(["benchmarks", action, "--json"], env=env)
    return code, json.loads(capsys.readouterr().out)


def test_empty_pool_and_status_do_not_fetch(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected() -> list[Observation]:
        pytest.fail("an offline read or empty pool made a download")

    for key, source in SOURCES.items():
        monkeypatch.setitem(SOURCES, key, replace(source, read=unexpected))
    code, reply = _run("refresh", env, capsys)
    assert code == 0 and reply["status"] == "empty_pool"
    assert "Team screen" in reply["summary"]
    assert not benchmarks.cache_path(env).exists()
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-6.2-sol@max"],
            "routing.pool": {WORKER: {"scores": {}}},
        },
        env=env,
    )
    code, reply = _run("status", env, capsys)
    assert code == 0 and reply["status"] == "not_checked"
    assert reply["workers"][WORKER]["matches"] == {}


def test_refresh_keeps_separate_series_and_preserves_owner_settings(
    pool: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in SOURCES:
        _source(monkeypatch, key)
    codex = Observation(
        model="GPT-6.2 Sol",
        harness="Codex",
        effort="max",
        metrics={"resolution": 61.52, "attempt_usd": 1.06, "pass_usd": 1.72},
    )
    opus = Observation(
        model="Claude Opus 5.5",
        harness="Claude Code",
        effort="max",
        metrics={"resolution": 65.76, "attempt_usd": 14.30, "pass_usd": 21.74},
    )
    _source(monkeypatch, TB41, codex, opus)
    _source(
        monkeypatch,
        TB21,
        {**codex, "metrics": {"resolution": 81.0, "attempt_usd": 0.55}},
    )
    _source(
        monkeypatch,
        LIVE,
        Observation(
            model="gpt-6.2-sol", harness=None, effort=None, metrics={"coding": 87}
        ),
    )
    choices.set_once({"single.writer": "auto"}, env=pool)
    settings = choices.snapshot(env=pool)
    code, refresh = _run("refresh", pool, capsys)
    assert code == 0 and refresh["status"] == "partial"
    assert refresh["sources"][TB41]["result"] == "updated"
    assert choices.snapshot(env=pool) == settings
    worker = refresh["workers"][WORKER]
    assert worker["matches"][TB41]["metrics"]["resolution"] == 61.52
    assert worker["matches"][TB21]["metrics"]["resolution"] == 81.0
    assert worker["missing"][TB21] == ["pass_usd"]
    assert worker["missing"][LIVE] == ["coding", "reasoning"]
    assert worker["related"][LIVE]["observation"]["metrics"] == {"coding": 87}
    assert refresh["sources"][LIVE]["publisher"] == "LiveBench"
    assert refresh["sources"][LIVE]["metrics"]["coding"]["suite"] == "Coding"
    assert refresh["sources"][LIVE]["metrics"]["reasoning"]["suite"] == "Reasoning"
    assert refresh["workers"]["claude:opus"]["matches"] == {}
    assert refresh["workers"]["claude:opus"]["related"][TB41]["observation"] == opus
    opencode = refresh["workers"]["opencode:openai/gpt-6.2-sol"]
    assert opencode["matches"] == {}
    assert opencode["related"][TB41]["reason"] == "different harness"

    atlas = "swe-atlas-qna-1.1"
    openai_route = "openai/gpt-6.2-sol"
    _source(
        monkeypatch,
        atlas,
        Observation(
            model=openai_route, harness="OpenCode", effort="max", metrics={"qna": 71}
        ),
    )
    _source(monkeypatch, TB41, opus, codex)
    _, repeat = _run("refresh", pool, capsys)
    assert repeat["sources"][TB41]["result"] == "unchanged"
    assert repeat["sources"][TB41]["revision"] == refresh["sources"][TB41]["revision"]
    checked = repeat["sources"][TB41]["checked_at"]
    assert checked >= refresh["sources"][TB41]["checked_at"]
    assert repeat["workers"][f"opencode:{openai_route}"]["matches"][atlas][
        "metrics"
    ] == {"qna": 71}
    assert atlas not in repeat["workers"][WORKER]["matches"]
    atlas_source = repeat["sources"][atlas]
    assert atlas_source["publisher"] == "Scale AI"
    assert atlas_source["edition"] == "1.1"
    assert atlas_source["metrics"]["qna"]["suite"] == "Codebase QnA"

    _source(monkeypatch, TB41)
    _source(monkeypatch, TB21, codex)
    _, partial = _run("refresh", pool, capsys)
    assert partial["sources"][TB41]["result"] == "retained"
    assert partial["sources"][TB41]["checked_at"] == checked
    assert partial["workers"][WORKER]["matches"][TB41] == codex
    assert partial["sources"][TB21]["result"] == "updated"

    choices.set_settings({"shortlist.codex": ["gpt-6.2-sol@high"]}, env=pool)
    code, local = _run("status", pool, capsys)
    assert code == 0
    assert local["workers"][WORKER]["matches"] == {}
    assert local["workers"][WORKER]["related"][TB41]["reason"] == "different effort"
    missing = local["workers"][WORKER]["missing"]
    assert all("pass_usd" in missing[key] for key in (TB41, TB21))


def test_failures_report_a_retained_series_without_making_it_fresh(
    pool: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in SOURCES:
        _source(monkeypatch, key)
    _source(
        monkeypatch,
        TB41,
        Observation(
            model="GPT-6.2 Sol",
            harness="Codex",
            effort="max",
            metrics={"resolution": 0, "attempt_usd": 0},
        ),
    )
    _, original = _run("refresh", pool, capsys)
    checked = original["sources"][TB41]["checked_at"]
    _source(monkeypatch, TB41)
    code, failed = _run("refresh", pool, capsys)
    assert code == 69 and failed["status"] == "unavailable"
    assert failed["sources"][TB41]["result"] == "retained"
    fresh = choices.read(env=pool)["answers"]
    report = benchmarks.cached(fresh, pool, now=checked + benchmarks.MAX_CHECK_AGE)
    assert report["sources"][TB41]["stale"]
    assert report["workers"][WORKER]["matches"][TB41]["metrics"] == {
        "resolution": 0,
        "attempt_usd": 0,
    }


def test_refresh_does_not_overwrite_a_cache_it_cannot_read(
    pool: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in SOURCES:
        _source(monkeypatch, key)
    _source(
        monkeypatch,
        TB41,
        Observation(
            model="GPT-6.2 Sol",
            harness="Codex",
            effort="max",
            metrics={"resolution": 61.52},
        ),
    )
    assert _run("refresh", pool, capsys)[0] == 0
    path = benchmarks.cache_path(pool)
    original = path.read_bytes()
    read_text = Path.read_text

    def blocked(file: Path, *, encoding: str) -> str:
        if file == path:
            raise PermissionError("fixture cache is unreadable")
        return read_text(file, encoding=encoding)

    _source(monkeypatch, TB41)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", blocked)
        code, reply = _run("refresh", pool, capsys)
    assert code == 74
    assert reply["error"]["messages"] == ["fixture cache is unreadable"]
    assert path.read_bytes() == original


def test_fast_and_ambiguous_rows_do_not_get_exact_costs(
    pool: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in SOURCES:
        _source(monkeypatch, key)
    sample = Observation(
        model="GPT-6.2 Sol",
        harness="Codex",
        effort="max",
        metrics={"resolution": 60, "attempt_usd": 1},
    )
    _source(monkeypatch, TB41, sample, {**sample, "metrics": {"resolution": 64}})
    result = benchmarks.refresh(pool)["workers"][WORKER]
    assert not result["matches"]
    assert "multiple" in result["related"][TB41]["reason"]

    choices.set_settings({"fast.codex": ["gpt-6.2-sol"]}, env=pool)
    answers = choices.read(env=pool)["answers"]
    result = benchmarks.cached(answers, pool)["workers"][WORKER]
    assert not result["matches"]
    assert "Fast" in result["related"][TB41]["reason"]


def test_concurrent_refresh_cannot_replace_the_cache(
    pool: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected() -> list[Observation]:
        pytest.fail("a competing refresh made a download")

    for key, source in SOURCES.items():
        monkeypatch.setitem(SOURCES, key, replace(source, read=unexpected))
    path = benchmarks.cache_path(pool)
    paths.ensure_private_dir(path.parent, root=path.parent)
    fd = paths.open_private_file(path.with_suffix(".lock"), os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, reply = _run("refresh", pool, capsys)
    finally:
        os.close(fd)
    assert code == 75
    assert reply["error"]["messages"] == ["a benchmark refresh is already running"]
    assert not path.exists()


def test_artificial_analysis_is_opt_in_and_retains_its_last_success(
    pool: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected(key: str) -> tuple[str, list[Observation]]:
        pytest.fail("Artificial Analysis was called without an opted-in refresh")

    monkeypatch.setattr(benchmarks, "read_artificial_analysis", unexpected)
    for key in SOURCES:
        _source(monkeypatch, key)
    exact = Observation(
        model="GPT-6.2 Sol", harness="Codex", effort="max", metrics={"resolution": 60}
    )
    _source(monkeypatch, TB41, exact)
    _, default = _run("refresh", pool, capsys)
    assert set(default["sources"]) == set(SOURCES)
    assert "model_reference" not in default["workers"][WORKER]

    choice_snapshot = choices.snapshot(env=pool)
    benchmarks.configure_artificial_analysis(" fixture-key ", pool)
    directory = benchmarks.artificial_analysis_directory(pool)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "api-key").stat().st_mode & 0o777 == 0o600
    _, waiting = _run("status", pool, capsys)
    assert waiting["sources"][ARTIFICIAL_ANALYSIS]["result"] == "not_checked"

    observation = Observation(
        model="GPT-6.2 Sol",
        model_creator="OpenAI",
        harness=None,
        effort="max",
        metrics={"intelligence_index": 57, "task_usd": 1.2345},
    )

    def reference(key: str) -> tuple[str, list[Observation]]:
        assert key == "fixture-key"
        return "5.0", [observation]

    monkeypatch.setattr(benchmarks, "read_artificial_analysis", reference)
    code, refreshed = _run("refresh", pool, capsys)
    assert code == 0
    series = f"{ARTIFICIAL_ANALYSIS}-5.0"
    source = refreshed["sources"][series]
    assert source["result"] == "updated"
    assert source["edition"] == "5.0" and source["publisher"] == "Artificial Analysis"
    assert source["metrics"]["task_usd"] == {
        "title": "USD / task",
        "suite": "Intelligence Index",
    }
    assert refreshed["workers"][WORKER]["model_reference"]["efforts"] == {
        "max": observation
    }
    assert refreshed["workers"][WORKER]["matches"] == {TB41: exact}
    cache = directory / "benchmarks.json"
    assert cache.stat().st_mode & 0o777 == 0o600
    assert "fixture-key" not in cache.read_text()
    assert ARTIFICIAL_ANALYSIS not in benchmarks.cache_path(pool).read_text()

    def rejected(request: Request, *, timeout: float) -> None:
        assert request.get_header("X-api-key") == "replacement-key"
        message = "reject replacement-key"
        raise HTTPError(request.full_url, 401, message, Message(), None)

    benchmarks.configure_artificial_analysis("replacement-key", pool)
    monkeypatch.setattr(
        benchmarks,
        "read_artificial_analysis",
        benchmark_sources.read_artificial_analysis,
    )
    monkeypatch.setattr(benchmark_sources.urllib.request, "urlopen", rejected)
    _, failed = _run("refresh", pool, capsys)
    assert failed["sources"][series]["result"] == "retained"
    assert failed["sources"][series]["error"] == "HTTP 401"
    assert failed["sources"][series]["checked_at"] == source["checked_at"]
    assert "replacement-key" not in json.dumps(failed)
    assert choices.snapshot(env=pool) == choice_snapshot

    monkeypatch.setattr(benchmarks, "read_artificial_analysis", unexpected)
    saved = benchmarks.cached(
        choices.read(env=pool)["answers"],
        pool,
        now=source["checked_at"] + benchmarks.MAX_CHECK_AGE,
    )
    assert saved["sources"][series]["stale"]
    assert saved["workers"][WORKER]["model_reference"]["efforts"]["max"] == observation
    benchmarks.configure_artificial_analysis(None, pool)
    assert not cache.exists() and not (directory / "api-key").exists()
    _, disabled = _run("status", pool, capsys)
    assert set(disabled["sources"]) == set(SOURCES)


def test_artificial_analysis_projects_supported_efforts_for_one_release(
    pool: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    deepseek = "deepseek/deepseek-v4.1-flash-1017"
    deepseek_worker = f"opencode:openrouter/{deepseek}"
    choices.set_settings(
        {
            "shortlist.claude": ["opus@high"],
            "shortlist.opencode": [f"openrouter/{deepseek}@max"],
            "routing.pool": {
                WORKER: {"scores": {}},
                "claude:opus": {"scores": {}},
                deepseek_worker: {"scores": {}},
            },
            "fast.codex": ["gpt-6.2-sol"],
            "engine.claude": "off",
        },
        env=pool,
    )
    for engine, model in (
        ("codex", models.Model("gpt-6.2-sol", "Sol", ("low", "max", "ultra"))),
        ("opencode", models.Model(deepseek, "Flash 1017", ("none", "low", "max"))),
    ):
        status.save_cache_file(
            models.cache_path(engine, pool),
            {"fetched_at": 0, "models": [model.to_json()]},
            pool,
        )
    baseline = {"intelligence_index": 50, "task_usd": 0.8}
    codex = Observation(
        model="GPT-6.2 Sol",
        model_creator="OpenAI",
        harness=None,
        effort="max",
        metrics=baseline,
    )
    opus = Observation(
        model="Claude Opus 5.5",
        model_creator="Anthropic",
        harness=None,
        effort="high",
        metrics=baseline,
        configuration="Default Fallback",
    )
    flash = Observation(
        model="DeepSeek V4.1 Flash 1017",
        model_creator="DeepSeek",
        harness=None,
        effort="none",
        metrics={"task_usd": 0},
    )
    rows: list[Observation] = [
        codex,
        {**codex, "effort": "low", "metrics": {"intelligence_index": 0}},
        {**codex, "effort": "ultra"},
        {**codex, "effort": "ultra", "configuration": "Default Fallback"},
        opus,
        {**opus, "model": "Claude Opus 5.4", "effort": "max"},
        flash,
        {**flash, "model": "DeepSeek V4.1 Flash", "effort": "max"},
        {**flash, "model_creator": "Other", "effort": "low"},
    ]
    for key in SOURCES:
        _source(monkeypatch, key)
    _source(
        monkeypatch, TB41, {**codex, "harness": "Codex", "metrics": {"resolution": 60}}
    )
    benchmarks.configure_artificial_analysis("fixture-key", pool)
    monkeypatch.setattr(
        benchmarks, "read_artificial_analysis", lambda key: ("5.0", rows)
    )
    report = benchmarks.refresh(pool)
    codex_evidence = report["workers"][WORKER]
    assert (
        "3/11 listed efforts have an Index score; 3/11 have a cost" in report["summary"]
    )
    assert codex_evidence["effort"] == "max" and codex_evidence["speed"] == "fast"
    assert not codex_evidence["matches"]
    assert not codex_evidence["matches_by_effort"]
    assert "Fast" in codex_evidence["related"][TB41]["reason"]
    assert codex_evidence["model_reference"]["efforts"] == {
        "low": rows[1],
        "max": codex,
        "ultra": None,
    }
    opus_ref = report["workers"]["claude:opus"]["model_reference"]
    assert (
        opus_ref["model"] == "Claude Opus 5.5" and opus_ref["effort_catalog_available"]
    )
    assert opus_ref["efforts"] == dict.fromkeys(models.CLAUDE_EFFORTS) | {"high": opus}
    deepseek_ref = report["workers"][deepseek_worker]["model_reference"]
    assert deepseek_ref["effort_catalog_available"]
    assert deepseek_ref["efforts"] == {"none": flash, "low": None, "max": None}


def test_index_only_refresh_preserves_public_evidence(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"shortlist.codex": ["gpt-6.2-sol@max"]}, env=env)
    for key in SOURCES:
        _source(monkeypatch, key)
    public = Observation(
        model="GPT-6.2 Sol", harness="Codex", effort="max", metrics={"resolution": 60}
    )
    _source(monkeypatch, TB41, public)
    code, first = _run("refresh", env, capsys)
    assert code == 0
    public_cache = benchmarks.cache_path(env)
    original = public_cache.read_bytes()
    choices.clear_settings(["shortlist.codex"], env=env)

    def unexpected() -> list[Observation]:
        pytest.fail("an Index-only refresh contacted a public source")

    for key, source in SOURCES.items():
        monkeypatch.setitem(SOURCES, key, replace(source, read=unexpected))
    observation = Observation(
        model="GPT-6.2 Sol",
        model_creator="OpenAI",
        harness=None,
        effort="max",
        metrics={"intelligence_index": 57, "task_usd": 1.2345},
    )
    keys = []

    def download(key: str) -> tuple[str, list[Observation]]:
        keys.append(key)
        return "5.0", [observation]

    benchmarks.configure_artificial_analysis("fixture-key", env)
    monkeypatch.setattr(benchmarks, "read_artificial_analysis", download)
    code, refreshed = _run("refresh", env, capsys)
    assert code == 0 and refreshed["status"] == "ok"
    assert keys == ["fixture-key"]
    series = f"{ARTIFICIAL_ANALYSIS}-5.0"
    assert set(refreshed["sources"]) == {*SOURCES, series}
    source = refreshed["sources"][series]
    assert source["result"] == "updated"
    assert refreshed["sources"][TB41]["result"] == "cached"
    assert refreshed["sources"][TB41]["revision"] == first["sources"][TB41]["revision"]
    assert refreshed["workers"] == refreshed["other_shortlisted_workers"] == {}

    def failed(key: str) -> tuple[str, list[Observation]]:
        raise SourceError("fixture Index failure")

    monkeypatch.setattr(benchmarks, "read_artificial_analysis", failed)
    code, retained = _run("refresh", env, capsys)
    assert code == 69 and retained["status"] == "retained"
    assert retained["sources"][series]["result"] == "retained"
    assert retained["sources"][series]["checked_at"] == source["checked_at"]
    assert benchmarks.read_cache(env)[ARTIFICIAL_ANALYSIS]["rows"] == [observation]
    assert public_cache.read_bytes() == original


def test_index_only_failure_without_previous_data(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed(key: str) -> tuple[str, list[Observation]]:
        raise SourceError("fixture Index failure")

    def unexpected() -> list[Observation]:
        pytest.fail("an Index-only refresh contacted a public source")

    for key, source in SOURCES.items():
        monkeypatch.setitem(SOURCES, key, replace(source, read=unexpected))
    benchmarks.configure_artificial_analysis("fixture-key", env)
    monkeypatch.setattr(benchmarks, "read_artificial_analysis", failed)
    code, unavailable = _run("refresh", env, capsys)
    assert code == 69 and unavailable["status"] == "unavailable"
    assert set(unavailable["sources"]) == {*SOURCES, ARTIFICIAL_ANALYSIS}
    assert unavailable["sources"][ARTIFICIAL_ANALYSIS]["checked_at"] is None
    assert not benchmarks.cache_path(env).exists()
    assert main(["benchmarks", "refresh"], env=env) == 69
    captured = capsys.readouterr()
    assert "Artificial Analysis Intelligence Index: unavailable" in captured.out
    assert "fixture Index failure" in captured.out
    assert captured.err == ""


def test_shortlisted_alternative_effort_has_evidence_without_pool_approval(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices.set_settings({"shortlist.codex": ["gpt-6.2-sol@max"]}, env=env)
    model = models.Model("gpt-6.2-sol", "Sol", ("low", "max"))
    status.save_cache_file(
        models.cache_path("codex", env),
        {"fetched_at": 0, "models": [model.to_json()]},
        env,
    )
    low = Observation(
        model="GPT-6.2 Sol",
        harness="Codex",
        effort="low",
        metrics={"resolution": 60, "attempt_usd": 0.25},
    )
    maximum: Observation = {**low, "effort": "max", "metrics": {"resolution": 80}}
    index = Observation(
        model="GPT-6.2 Sol",
        model_creator="OpenAI",
        harness=None,
        effort="low",
        metrics={"intelligence_index": 55, "task_usd": 0.2},
    )
    for key in SOURCES:
        _source(monkeypatch, key)
    _source(monkeypatch, TB41, low, maximum, {**low, "effort": "ultra"})
    _source(monkeypatch, TB21, low, {**low, "metrics": {"resolution": 62}})
    benchmarks.configure_artificial_analysis("fixture-key", env)
    monkeypatch.setattr(
        benchmarks, "read_artificial_analysis", lambda key: ("5.0", [index])
    )

    refreshed = benchmarks.refresh(env)
    assert refreshed["status"] == "partial"
    assert refreshed["workers"] == {}
    recommendation = refreshed["other_shortlisted_workers"][WORKER]
    assert recommendation["effort"] == "max"
    assert recommendation["supported_efforts"] == ("low", "max")
    assert recommendation["matches"] == {TB41: maximum}
    assert recommendation["matches_by_effort"] == {
        "low": {TB41: low},
        "max": {TB41: maximum},
    }
    assert recommendation["model_reference"]["efforts"] == {"low": index, "max": None}

    choices.set_settings({"routing.pool": {WORKER: {"scores": {}}}}, env=env)
    saved = benchmarks.cached(choices.read(env=env)["answers"], env)
    assert saved["other_shortlisted_workers"] == {}
    assert saved["workers"][WORKER] == recommendation
