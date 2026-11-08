import json
from pathlib import Path

import pytest

from ember import paths
from ember.app import state


def test_defaults_without_a_file(env: dict[str, str]) -> None:
    assert state.load(env) == state.AppState(1100, 720, "overview")


def test_round_trip_is_private(env: dict[str, str]) -> None:
    state.save(state.AppState(1300, 800, "engines"), env)
    assert state.load(env) == state.AppState(1300, 800, "engines")
    path = state.state_path(env)
    assert path == paths.state_dir(env) / "app.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text()) == {
        "width": 1300,
        "height": 800,
        "screen": "engines",
        "finished_open": False,
        "denied_dismissals": {},
        "denied_hidden": [],
        "sidebar_hidden": False,
    }


def test_finished_open_round_trips_and_needs_true(env: dict[str, str]) -> None:
    state.save(state.AppState(1300, 800, "overview", finished_open=True), env)
    assert state.load(env).finished_open is True
    path = state.state_path(env)
    path.write_text(json.dumps({"finished_open": "yes"}))
    assert state.load(env).finished_open is False


def test_sidebar_hidden_round_trips(env: dict[str, str]) -> None:
    saved = state.AppState(959, 480, "settings", sidebar_hidden=True)
    state.save(saved, env)
    assert state.load(env) == saved
    assert json.loads(state.state_path(env).read_text())["sidebar_hidden"] is True


@pytest.mark.parametrize("value", [False, 1, 0, "true", "yes", None, [], {}])
def test_sidebar_hidden_needs_true(env: dict[str, str], value: object) -> None:
    path = state.state_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"sidebar_hidden": value}))
    assert state.load(env).sidebar_hidden is False


def test_old_state_reads_sidebar_as_shown(env: dict[str, str]) -> None:
    path = state.state_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"width": 1200, "height": 800, "screen": "menu"}))
    assert state.load(env) == state.AppState(1200, 800, "menu", sidebar_hidden=False)


def test_corrupt_file_gives_defaults(env: dict[str, str]) -> None:
    path = state.state_path(env)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert state.load(env) == state.AppState()
    path.write_text("[1, 2]")
    assert state.load(env) == state.AppState()


def test_invalid_values_take_their_defaults(env: dict[str, str]) -> None:
    path = state.state_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"width": "wide", "height": 900, "screen": 3}))
    assert state.load(env) == state.AppState(1100, 900, "overview")
    path.write_text(json.dumps({"width": True, "height": -5, "screen": ""}))
    assert state.load(env) == state.AppState()


def test_write_errors_are_ignored(tmp_path: Path) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("a file where the folder should be")
    env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(blocker)}
    state.save(state.AppState(1200, 700, "menu"), env)
    assert state.load(env) == state.AppState()


def test_denial_dismissals_survive_restart_and_reject_malformed_values(
    env: dict[str, str],
) -> None:
    dismissed = {"/home/owner/tools": ["run-abcdef:1"]}
    state.save(state.AppState(denied_dismissals=dismissed), env)
    assert state.load(env).denied_dismissals == dismissed
    state.state_path(env).write_text(
        json.dumps(
            {"denied_dismissals": {"ok": ["run:1"], "bad": [1], "also bad": None}}
        )
    )
    assert state.load(env).denied_dismissals == {"ok": ["run:1"]}


def test_hidden_denial_folders_round_trip(env: dict[str, str]) -> None:
    saved = state.AppState(
        denied_hidden=["/home/owner/tools"],
        denied_dismissals={"/home/owner/other": ["run:1"]},
    )
    state.save(saved, env)
    assert state.load(env) == saved
    assert json.loads(state.state_path(env).read_text())["denied_hidden"] == [
        "/home/owner/tools"
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        (True, []),
        ("/home/owner/tools", []),
        ({"/home/owner/tools": True}, []),
        (["/tools", 1, None, {}, [], "", "/other", "/tools"], ["/other", "/tools"]),
    ],
)
def test_hidden_denial_folders_reject_invalid_entries(
    env: dict[str, str], value: object, expected: list[str]
) -> None:
    path = state.state_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"denied_hidden": value}))
    assert state.load(env).denied_hidden == expected
