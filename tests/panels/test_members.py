import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from ember import choices, paths
from ember.engines import Driver, DriverMissingError
from ember.panels import members
from ember.panels.members import HELP_LINE, NO_MEMBER_LINE, Member, MemberError
from ember.runs import exits
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    engine_cache,
    make_env,
    script_path,
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return make_env(tmp_path)


@pytest.fixture
def load_driver(env: dict[str, str]) -> Callable[[str], Driver]:
    return lambda name: StubDriver(name, script_path(env))


@pytest.mark.parametrize(
    ("value", "engine", "model", "effort"),
    [
        ("codex:gpt-6-astra", "codex", "gpt-6-astra", None),
        ("codex:gpt-6-astra@low", "codex", "gpt-6-astra", "low"),
        (
            "opencode:deepseek/deepseek-v4.1-flash-1017@low",
            "opencode",
            "deepseek/deepseek-v4.1-flash-1017",
            "low",
        ),
        ("opencode:vendor/model:free", "opencode", "vendor/model:free", None),
        ("claude:haiku", "claude", "haiku", None),
    ],
)
def test_parse_options(value: str, engine: str, model: str, effort: str | None) -> None:
    assert members.parse_options([value]) == [Member(engine, model, effort, "option")]
    assert members.parse_options([value])[0].text() == value


@pytest.mark.parametrize(
    "values",
    [
        ["codex"],
        ["unknown:model"],
        ["codex:"],
        ["codex:gpt-6-astra@High"],
        ["codex:some model"],
        ["codex:model@low@high"],
        ["codex:gpt-6-astra@low", "codex:gpt-6-astra@high"],
        [f"codex:model-{n}" for n in range(9)],
    ],
)
def test_parse_options_refuses_invalid(values: list[str]) -> None:
    with pytest.raises(MemberError) as caught:
        members.parse_options(values)
    assert caught.value.code == 64
    assert caught.value.line == HELP_LINE


def test_parse_options_empty_eight_and_same_model_other_engine() -> None:
    assert members.parse_options([]) == []
    assert len(members.parse_options([f"codex:model-{n}" for n in range(8)])) == 8
    assert len(members.parse_options(["codex:model", "claude:model"])) == 2


def test_saved_members_and_layers(env: dict[str, str]) -> None:
    choices.set_settings({"panel.members": ["codex:gpt-5"]}, env=env)
    assert members.from_settings(choices.read(env=env)) == (
        [Member("codex", "gpt-5", "high", "setting")],
        [],
    )
    choices.set_once({"panel.members": ["claude:opus"]}, env=env)
    choices.claim("panel-choice", env=env)
    assert members.from_settings(choices.read("panel-choice", env=env)) == (
        [Member("claude", "opus", "high", "one-time")],
        [],
    )
    assert members.from_settings(
        choices.read(
            "panel-choice", request={"panel.members": ["codex:gpt-5"]}, env=env
        )
    ) == ([Member("codex", "gpt-5", "high", "request")], [])


def test_saved_members_left_out(env: dict[str, str]) -> None:
    choices.set_settings(
        {
            "shortlist.opencode": ["vendor/model"],
            "panel.members": ["codex:gpt-5", "claude:opus", "opencode:vendor/model"],
        },
        env=env,
    )
    choices.set_settings({"engine.codex": "off", "shortlist.opencode": []}, env=env)
    engine_cache(env, {"claude": False})
    assert members.from_settings(choices.read(env=env)) == (
        [],
        [
            {"value": "codex:gpt-5", "reason": "codex is off"},
            {"value": "claude:opus", "reason": "claude is not connected"},
            {
                "value": "opencode:vendor/model",
                "reason": "not on the opencode shortlist",
            },
        ],
    )


def _break_panel(env: dict[str, str]) -> None:
    with sqlite3.connect(paths.settings_db_path(env)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings VALUES (?, ?)", ("panel.members", '"bad"')
        )


def test_saved_members_source_error(env: dict[str, str]) -> None:
    _break_panel(env)
    with pytest.raises(MemberError, match="the settings cannot be read") as caught:
        members.from_settings(choices.read(env=env))
    assert caught.value.code == 64
    assert caught.value.line == NO_MEMBER_LINE
    assert "panel.members" in str(caught.value)


@pytest.mark.parametrize("kind", ["off", "disconnected", "driver", "effort"])
def test_check_gates(
    env: dict[str, str],
    load_driver: Callable[[str], Driver],
    kind: str,
) -> None:
    if kind == "off":
        choices.set_settings({"engine.codex": "off"}, env=env)
    if kind == "disconnected":
        engine_cache(env, {"codex": False})
    if kind == "driver":

        def missing(name: str) -> Driver:
            raise DriverMissingError(f"Ember has no driver for {name}")

        load_driver = missing
    member = Member("codex", "gpt-5", "ultra" if kind == "effort" else None, "option")
    with pytest.raises(MemberError) as caught:
        members.check(member, choices.read(env=env), env, load_driver)
    code, line = {
        "off": (64, exits.line(64, "engine_off", engine="codex")),
        "disconnected": (69, "owner: codex login"),
        "driver": (69, exits.line(69, "driver", engine="codex")),
        "effort": (64, HELP_LINE),
    }[kind]
    assert caught.value.code == code
    assert caught.value.line == line


def test_effort_defaults_and_explicit(load_driver: Callable[[str], Driver]) -> None:
    assert members.resolve_effort(
        Member("codex", "gpt-5", None, "option"), load_driver
    ) == (Member("codex", "gpt-5", "medium", "option"))
    assert members.resolve_effort(
        Member("codex", "gpt-5", "low", "option"), load_driver
    ) == (Member("codex", "gpt-5", "low", "option"))


@pytest.mark.parametrize("default", [None, "ultra"])
def test_selection_driver_default(env: dict[str, str], default: str | None) -> None:
    class DefaultDriver(StubDriver):
        def default_effort(self, model: str) -> str | None:
            return default

        def valid_effort(self, effort: str) -> bool:
            assert effort is not None
            return super().valid_effort(effort)

    driver = DefaultDriver("codex", script_path(env))
    if default is None:
        selected = members.select(
            ["codex:gpt-5"], choices.read(env=env), env, lambda name: driver
        )
        assert selected.members[0].effort is None
    else:
        with pytest.raises(
            MemberError, match="does not take the effort ultra"
        ) as caught:
            members.select(
                ["codex:gpt-5"], choices.read(env=env), env, lambda name: driver
            )
        assert caught.value.code == 64
        assert caught.value.line == HELP_LINE


def test_options_win_and_ignore_other_rows(
    env: dict[str, str],
    load_driver: Callable[[str], Driver],
) -> None:
    choices.set_settings(
        {"panel.members": ["codex:gpt-5"], "single.reviewer_as": "subagent"}, env=env
    )
    selected = members.select(["claude:opus"], choices.read(env=env), env, load_driver)
    assert selected == members.Selection(
        (Member("claude", "opus", "medium", "option"),), ()
    )
    _break_panel(env)
    selected = members.select(["claude:haiku"], choices.read(env=env), env, load_driver)
    assert selected.members == (Member("claude", "haiku", "medium", "option"),)


def test_broken_saved_panel_does_not_enable_engine(
    env: dict[str, str],
    load_driver: Callable[[str], Driver],
) -> None:
    choices.set_settings({"engine.codex": "off", "sandbox.codex": "on"}, env=env)
    _break_panel(env)
    settings = choices.read(env=env)
    assert settings["answers"]["sandbox.codex"]["value"] == "on"
    with pytest.raises(MemberError, match="codex is switched off") as caught:
        members.select(["codex:gpt-5"], settings, env, load_driver)
    assert caught.value.code == 64
    assert caught.value.line == exits.line(64, "engine_off", engine="codex")


def test_saved_driver_failure_left_out(
    env: dict[str, str],
    load_driver: Callable[[str], Driver],
) -> None:
    choices.set_settings({"panel.members": ["codex:gpt-5", "claude:opus"]}, env=env)

    def available(name: str) -> Driver:
        if name == "codex":
            raise DriverMissingError("Ember has no driver for codex")
        return load_driver(name)

    selected = members.select([], choices.read(env=env), env, available)
    assert selected.members == (Member("claude", "opus", "high", "setting"),)
    assert selected.left_out == (
        {"value": "codex:gpt-5", "reason": "Ember has no driver for codex"},
    )


@pytest.mark.parametrize("excluded", [False, True])
def test_no_members(
    env: dict[str, str],
    load_driver: Callable[[str], Driver],
    excluded: bool,
) -> None:
    if excluded:
        choices.set_settings(
            {"panel.members": ["codex:gpt-5"], "engine.codex": "off"}, env=env
        )
    with pytest.raises(MemberError) as caught:
        members.select([], choices.read(env=env), env, load_driver)
    assert str(caught.value) == "no panel member to run" + (
        ": every saved member was left out" if excluded else ""
    )
    assert caught.value.code == 64
    assert caught.value.line == NO_MEMBER_LINE
    assert caught.value.left_out == (
        ({"value": "codex:gpt-5", "reason": "codex is off"},) if excluded else ()
    )


def test_all_gates_failed_retains_exclusion(env: dict[str, str]) -> None:
    choices.set_settings({"panel.members": ["codex:gpt-5"]}, env=env)

    def missing(name: str) -> Driver:
        raise DriverMissingError(f"Ember has no driver for {name}")

    with pytest.raises(MemberError) as caught:
        members.select([], choices.read(env=env), env, missing)
    assert caught.value.left_out == (
        {"value": "codex:gpt-5", "reason": "Ember has no driver for codex"},
    )


def test_member_entry() -> None:
    member = Member("codex", "gpt-6-astra", "low", "option")
    assert member.entry(3) == {
        "n": 3,
        "engine": "codex",
        "model": "gpt-6-astra",
        "effort": "low",
        "source": "option",
        "status": "pending",
        "run": None,
        "exit": None,
        "reason": None,
        "reply": None,
        "bytes": 0,
        "changed": [],
        "seconds": None,
        "closed": False,
        "close_error": None,
        "attempts": [],
    }
    first = member.entry(1)
    first["attempts"].append({"kind": "start", "exit": None})
    first["changed"].append("file")
    assert member.entry(2)["attempts"] == []
    assert member.entry(2)["changed"] == []
