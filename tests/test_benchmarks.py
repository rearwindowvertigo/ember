import fcntl
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import benchmarks, choices, paths
from ember.benchmark_sources import SOURCES, Observation, SourceError
from ember.cli import main

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
