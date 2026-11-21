import json
import stat
from pathlib import Path

import pytest

from ember import choices, holds


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "state")}


def test_no_record_holds_nothing(env: dict[str, str]) -> None:
    assert holds.active(env) == []
    assert holds.find("codex", "sol", env) is None
    assert holds.release("codex", "sol", env=env) is False
    holds.succeeded("codex", "sol", env=env)
    assert not holds.path(env).exists()


def test_capacity_holds_the_model_for_the_set_minutes(env: dict[str, str]) -> None:
    hold = holds.start("codex", "sol", "capacity", 15, run="codex-1", env=env, now=100)
    assert hold == holds.Hold("codex", "sol", "capacity", 100.0, 1000.0, "codex-1")
    assert hold is not None and hold.name() == "codex sol"
    assert stat.S_IMODE(holds.path(env).stat().st_mode) == 0o600
    assert holds.find("codex", "sol", env, now=999) == hold
    assert holds.find("codex", "old", env, now=999) is None
    assert holds.find("claude", "sol", env, now=999) is None
    assert holds.find("codex", "sol", env, now=1000) is None
    assert holds.active(env, now=1000) == []


@pytest.mark.parametrize("kind", ["usage_limit", "outage"])
def test_other_kinds_hold_the_whole_engine(env: dict[str, str], kind: str) -> None:
    hold = holds.start("codex", "sol", kind, 5, env=env, now=0)
    assert hold is not None and (hold.model, hold.until) == (None, 300.0)
    assert hold.name() == "codex"
    assert holds.find("codex", "sol", env, now=1) == hold
    assert holds.find("codex", "other", env, now=1) == hold
    assert holds.find("opencode", "sol", env, now=1) is None


def test_zero_minutes_records_nothing(env: dict[str, str]) -> None:
    assert holds.start("codex", "sol", "capacity", 0, env=env) is None
    assert not holds.path(env).exists()
    with pytest.raises(ValueError, match="unknown provider kind"):
        holds.start("codex", "sol", "busy", 5, env=env)


def test_a_new_hold_replaces_the_same_one_and_drops_ended_ones(
    env: dict[str, str],
) -> None:
    holds.start("codex", "sol", "capacity", 5, env=env, now=0)
    holds.start("codex", "old", "capacity", 60, env=env, now=10)
    holds.start("claude", "opus", "outage", 60, env=env, now=20)
    later = holds.start("codex", "sol", "capacity", 5, env=env, now=400)
    saved = json.loads(holds.path(env).read_text())
    assert [(item["engine"], item["model"]) for item in saved] == [
        ("codex", "old"),
        ("claude", None),
        ("codex", "sol"),
    ]
    assert holds.find("codex", "sol", env, now=401) == later
    assert [hold.name() for hold in holds.active(env, now=401)] == [
        "codex old",
        "claude",
        "codex sol",
    ]


def test_the_engine_hold_comes_before_the_model_hold(env: dict[str, str]) -> None:
    holds.start("codex", "sol", "capacity", 60, env=env, now=0)
    engine = holds.start("codex", "sol", "outage", 5, env=env, now=10)
    assert holds.find("codex", "sol", env, now=20) == engine
    found = holds.find("codex", "sol", env, now=400)
    assert found is not None and found.kind == "capacity"


def test_release_and_success_end_holds(env: dict[str, str]) -> None:
    holds.start("codex", "sol", "capacity", 60, env=env, now=0)
    holds.start("codex", "old", "capacity", 60, env=env, now=0)
    holds.start("codex", "sol", "outage", 60, env=env, now=0)
    assert holds.release("codex", env=env) is True
    assert holds.release("codex", env=env) is False
    assert [hold.name() for hold in holds.active(env, now=1)] == [
        "codex sol",
        "codex old",
    ]
    holds.start("codex", "sol", "usage_limit", 60, env=env, now=0)
    holds.succeeded("codex", "sol", env=env)
    assert [hold.name() for hold in holds.active(env, now=1)] == ["codex old"]
    assert holds.release("codex", "old", env=env) is True
    assert holds.active(env, now=1) == []


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "{}",
        json.dumps([1, {"engine": "codex"}, {"engine": 1, "model": None}]),
        json.dumps(
            [
                {
                    "engine": "codex",
                    "model": "sol",
                    "kind": "busy",
                    "since": 0,
                    "until": 9e9,
                }
            ]
        ),
    ],
)
def test_an_unreadable_record_holds_nothing(env: dict[str, str], content: str) -> None:
    holds.start("codex", "sol", "capacity", 60, env=env, now=0)
    holds.path(env).write_text(content)
    assert holds.active(env, now=1) == []
    assert holds.start("codex", "old", "capacity", 60, env=env, now=1) is not None
    assert [hold.name() for hold in holds.active(env, now=2)] == ["codex old"]


def test_replacement_passes_over_held_and_unconnected_entries(tmp_path: Path) -> None:
    from runs.stub_engine import (  # pyrefly: ignore[missing-import]
        engine_cache,
        make_env,
    )

    env = make_env(tmp_path)
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high", "old@low"],
            "provider.fallback": ["codex:gpt-5", "codex:old", "claude:opus"],
        },
        env=env,
    )

    def pick(*failed: tuple[str, str, str]) -> tuple[str, str] | None:
        answers = choices.read(None, env=env)["answers"]
        row = holds.replacement(answers, failed, env=env)
        return None if row is None else (row["engine"], row["model"])

    sol = ("codex", "gpt-5", "capacity")
    assert pick(sol) == ("codex", "old")
    holds.start("codex", "old", "capacity", 15, env=env)
    assert pick(sol) == ("claude", "opus")
    engine_cache(env, {"claude": False})
    assert pick(sol) is None
    assert holds.release("codex", "old", env=env)
    assert pick(sol) == ("codex", "old")
    assert pick(sol, ("codex", "old", "outage")) is None
    # Without a status cache the connection state is unknown and not a bar.
    Path(env["XDG_CACHE_HOME"]).joinpath("ember").rename(tmp_path / "moved")
    assert pick(sol, ("codex", "old", "capacity")) == ("claude", "opus")
