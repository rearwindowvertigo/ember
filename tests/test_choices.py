import json
import multiprocessing
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ember import choices, db, paths
from ember.engines import status

T0 = 1_800_000_000.0
DAY = 24 * 60 * 60


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": str(empty_bin),
    }


def _db_path(env: dict[str, str]) -> Path:
    return paths.settings_db_path(env)


def _generation(env: dict[str, str]) -> int:
    return choices.read(env=env)["generation"]


def _answer(reply: dict[str, Any], key: str) -> tuple[object, str]:
    answer = reply["answers"][key]
    return answer["value"], answer["layer"]


def _raw(env: dict[str, str], sql: str, *args: object) -> None:
    conn = sqlite3.connect(_db_path(env))
    conn.execute(sql, args)
    conn.commit()
    conn.close()


VALID = [
    ("flow.review_rounds", "1"),
    ("flow.review_rounds", "4"),
    ("worker.network", "on"),
    ("worker.network", "off"),
    ("engine.claude", "off"),
    ("engine.codex", "on"),
    ("engine.opencode", "off"),
    ("shortlist.claude", ["opus@high", "sonnet-4.5@low"]),
    ("shortlist.codex", ["gpt-5_x@medium"]),
    ("shortlist.codex", []),
    ("shortlist.opencode", ["openrouter/qwen:free@high", "anthropic/claude-3.5"]),
    ("shortlist.opencode", []),
    ("single.writer", "ask"),
    ("single.reviewer", "ask"),
    ("single.writer_as", "subagent"),
    ("single.reviewer_as", "cli"),
    ("sandbox.codex", "on"),
    ("sandbox.codex", "off"),
    ("sandbox.opencode", "on"),
    ("sandbox.claude", "off"),
    ("sandbox.read", ["/opt/data", "/srv"]),
    ("sandbox.read", []),
    ("terminal", "gnome-terminal -- 'a b'"),
    ("app.autostart", "off"),
    ("app.theme", "dark"),
]

INVALID = [
    ("flow.review_rounds", "0"),
    ("flow.review_rounds", "5"),
    ("flow.review_rounds", 2),
    ("worker.network", "yes"),
    ("worker.network", True),
    ("engine.claude", "ON"),
    ("shortlist.claude", ["opus"]),
    ("shortlist.claude", ["opus@High"]),
    ("shortlist.claude", ["opus@high", "opus@low"]),
    ("shortlist.claude", "opus@high"),
    ("shortlist.codex", ["-gpt@high"]),
    ("shortlist.codex", ["gpt@high\n"]),
    ("shortlist.codex", [""]),
    ("shortlist.opencode", ["qwen@high"]),
    ("shortlist.opencode", ["a/b@x", "a/b"]),
    ("shortlist.opencode", ["a/:b"]),
    ("shortlist.opencode", [1]),
    ("single.writer", "codex"),
    ("single.writer", "gpt:x"),
    ("single.writer", "codex:gpt@high"),
    ("single.writer", "opencode:qwen"),
    ("single.reviewer", ""),
    ("single.writer_as", "agent"),
    ("sandbox.codex", "landlock"),
    ("sandbox.codex", "bubblewrap"),
    ("sandbox.opencode", "none"),
    ("sandbox.claude", "codex-bundled"),
    ("sandbox.read", ["relative/path"]),
    ("sandbox.read", ["/a", "/a"]),
    ("sandbox.read", [""]),
    ("terminal", ""),
    ("terminal", "   "),
    ("terminal", "xterm -e 'open"),
    ("app.autostart", "maybe"),
    ("app.theme", "blue"),
]


@pytest.mark.parametrize(("key", "value"), VALID)
def test_valid_value_is_saved_and_read(
    env: dict[str, str], key: str, value: object
) -> None:
    reply = choices.set_settings({key: value}, env=env)
    assert reply["settings"][key] == value
    assert _answer(choices.read(env=env), key) == (value, "setting")


@pytest.mark.parametrize(("key", "value"), INVALID)
def test_invalid_value_is_refused(env: dict[str, str], key: str, value: object) -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_settings({key: value}, env=env)
    assert not _db_path(env).exists()


def test_unknown_key_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_settings({"worker.colour": "on"}, env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.clear_settings(["worker.colour"], env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.read(request={"nope": "x"}, env=env)


def test_defaults(env: dict[str, str]) -> None:
    answers = choices.read(env=env)["answers"]
    values = {key: answer["value"] for key, answer in answers.items()}
    assert {answer["layer"] for answer in answers.values()} == {"default"}
    assert values == {
        "worker.network": "off",
        "engine.claude": "on",
        "engine.codex": "on",
        "engine.opencode": "on",
        "shortlist.claude": [],
        "shortlist.codex": [],
        "shortlist.opencode": [],
        "single.writer": "ask",
        "single.writer_as": "cli",
        "single.reviewer": "ask",
        "single.reviewer_as": "cli",
        "multi.runs_as": "ask",
        "flow.review_rounds": "3",
        "panel.members": [],
        "sandbox.codex": "off",
        "sandbox.opencode": "off",
        "sandbox.claude": "off",
        "sandbox.read": [],
        "terminal": "xdg-terminal-exec",
        "app.autostart": "on",
        "app.theme": "system",
    }


def test_sandbox_defaults_to_on_when_bwrap_is_on_path(
    env: dict[str, str], tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bwrap = bin_dir / "bwrap"
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)
    with_bwrap = {**env, "PATH": f"{env['PATH']}:{bin_dir}"}
    answers = choices.read(env=with_bwrap)["answers"]
    for engine in ("codex", "opencode", "claude"):
        assert answers[f"sandbox.{engine}"]["value"] == "on"
    answers = choices.read(env=env)["answers"]
    for engine in ("codex", "opencode", "claude"):
        assert answers[f"sandbox.{engine}"]["value"] == "off"


def test_codex_sandbox_defaults_to_on_with_a_bundled_bwrap(
    env: dict[str, str], tmp_path: Path
) -> None:
    package = tmp_path / "codex-package"
    (package / "bin").mkdir(parents=True)
    (package / "codex-resources").mkdir()
    (package / "codex-resources" / "bwrap").write_text("#!/bin/sh\n")
    codex = package / "bin" / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    with_codex = {**env, "PATH": f"{env['PATH']}:{package / 'bin'}"}
    answers = choices.read(env=with_codex)["answers"]
    assert answers["sandbox.codex"]["value"] == "on"
    assert answers["sandbox.opencode"]["value"] == "off"
    assert answers["sandbox.claude"]["value"] == "off"


def test_missing_database_reads_defaults_without_creating_it(
    env: dict[str, str],
) -> None:
    reply = choices.read(env=env)
    assert reply["generation"] == 0
    assert reply["source_error"] is False
    assert reply["errors"] == []
    assert reply["claim"] is None
    assert reply["menu"]["single.writer"]["state"] == "ask"
    choices.read("run-000001", env=env)
    choices.snapshot(env=env)
    assert not (Path(env["XDG_CONFIG_HOME"])).exists()


def test_unreadable_database_raises_74(env: dict[str, str]) -> None:
    path = _db_path(env)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a database file at all" * 100)
    with pytest.raises(db.DatabaseUnavailableError) as info:
        choices.read(env=env)
    assert info.value.exit_code == 74
    with pytest.raises(db.DatabaseUnavailableError):
        choices.set_settings({"app.theme": "dark"}, env=env)


def test_newer_schema_raises_78(env: dict[str, str]) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    _raw(env, "UPDATE meta SET value = '2' WHERE key = 'schema'")
    with pytest.raises(db.UnsupportedSchemaError) as info:
        choices.read(env=env)
    assert info.value.exit_code == 78
    with pytest.raises(db.UnsupportedSchemaError):
        choices.set_settings({"app.theme": "light"}, env=env)


def test_settings_are_canonical_json(env: dict[str, str]) -> None:
    choices.set_settings({"shortlist.codex": ["a@b", "c@d"]}, env=env)
    conn = sqlite3.connect(_db_path(env))
    stored = conn.execute("SELECT value FROM settings").fetchone()[0]
    conn.close()
    assert stored == '["a@b","c@d"]'


# Menu values and shortlists


def test_menu_value_must_be_shortlisted_at_save(env: dict[str, str]) -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_settings({"single.writer": "codex:gpt-5"}, env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_once({"single.writer": "codex:gpt-5"}, env=env)
    choices.set_settings(
        {"shortlist.codex": ["gpt-5@high"], "single.writer": "codex:gpt-5"}, env=env
    )
    choices.set_once({"single.reviewer": "codex:gpt-5"}, env=env)


def test_menu_row_set(env: dict[str, str]) -> None:
    choices.set_settings(
        {
            "shortlist.claude": ["opus@high"],
            "shortlist.opencode": ["openrouter/qwen:free"],
            "single.writer": "claude:opus",
            "single.writer_as": "subagent",
            "single.reviewer": "opencode:openrouter/qwen:free",
        },
        env=env,
    )
    menu = choices.read(env=env)["menu"]
    assert menu["single.writer"] == {
        "value": "claude:opus",
        "state": "set",
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "run_as": "subagent",
        "reason": None,
    }
    assert menu["single.reviewer"]["state"] == "set"
    assert menu["single.reviewer"]["model"] == "openrouter/qwen:free"
    assert menu["single.reviewer"]["effort"] is None
    assert menu["single.reviewer"]["run_as"] is None


def test_saved_model_that_left_the_shortlist_reads_not_shortlisted(
    env: dict[str, str],
) -> None:
    choices.set_settings(
        {"shortlist.codex": ["gpt-5@high"], "single.writer": "codex:gpt-5"}, env=env
    )
    choices.set_settings({"shortlist.codex": ["o3@low"]}, env=env)
    reply = choices.read(env=env)
    assert reply["source_error"] is False
    assert _answer(reply, "single.writer") == ("codex:gpt-5", "setting")
    row = reply["menu"]["single.writer"]
    assert row["state"] == "not shortlisted"
    assert row["reason"]


def test_menu_row_engine_off(env: dict[str, str]) -> None:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high"],
            "single.writer": "codex:gpt-5",
            "engine.codex": "off",
        },
        env=env,
    )
    row = choices.read(env=env)["menu"]["single.writer"]
    assert row["state"] == "engine off"
    assert row["reason"]


def test_menu_row_invalid() -> None:
    answers = {
        "single.writer": {"value": "nonsense", "layer": "request"},
        "single.writer_as": {"value": "cli", "layer": "default"},
    }
    row = choices.menu_row(answers, "single.writer")
    assert row["state"] == "invalid"
    assert row["reason"]


def test_menu_row_ask_has_reason(env: dict[str, str]) -> None:
    row = choices.read(env=env)["menu"]["single.reviewer"]
    assert row["state"] == "ask"
    assert row["reason"]


# Layers


def test_request_beats_one_time_beats_setting_beats_default(
    env: dict[str, str],
) -> None:
    choices.set_settings(
        {
            "single.writer_as": "subagent",
            "single.reviewer_as": "subagent",
            "single.reviewer": "ask",
        },
        env=env,
    )
    choices.set_once({"single.writer_as": "cli", "single.reviewer_as": "cli"}, env=env)
    reply = choices.claim(
        "run-000001", request={"single.writer_as": "subagent"}, env=env
    )
    assert _answer(reply, "single.writer_as") == ("subagent", "request")
    assert _answer(reply, "single.reviewer_as") == ("cli", "one-time")
    assert _answer(reply, "single.reviewer") == ("ask", "setting")
    assert _answer(reply, "single.writer") == ("ask", "default")


def test_ask_in_an_upper_layer_wins(env: dict[str, str]) -> None:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high"],
            "single.writer": "codex:gpt-5",
            "single.reviewer": "codex:gpt-5",
        },
        env=env,
    )
    choices.set_once({"single.writer": "ask"}, env=env)
    reply = choices.claim("run-000001", request={"single.reviewer": "ask"}, env=env)
    assert _answer(reply, "single.writer") == ("ask", "one-time")
    assert _answer(reply, "single.reviewer") == ("ask", "request")
    assert reply["menu"]["single.writer"]["state"] == "ask"


@pytest.mark.parametrize("key", ["worker.network", "shortlist.codex", "terminal"])
def test_request_or_once_for_standing_only_key_is_refused(
    env: dict[str, str], key: str
) -> None:
    value = choices.read(env=env)["answers"][key]["value"]
    with pytest.raises(choices.InvalidChoiceError):
        choices.read(request={key: value}, env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.claim("run-000001", request={key: value}, env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_once({key: value}, env=env)
    assert not _db_path(env).exists()


def test_invalid_request_is_refused_before_anything_is_written(
    env: dict[str, str],
) -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.claim("run-000001", request={"single.writer": "bad"}, env=env)
    assert not _db_path(env).exists()


def test_read_without_id_ignores_pending_once(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    assert _answer(choices.read(env=env), "single.writer_as") == ("cli", "default")


def test_invalid_stored_setting_fails_closed(env: dict[str, str]) -> None:
    choices.set_settings({"app.theme": "dark", "worker.network": "on"}, env=env)
    _raw(env, "UPDATE settings SET value = '\"purple\"' WHERE key = 'app.theme'")
    reply = choices.read(request={"single.writer_as": "subagent"}, env=env)
    assert reply["source_error"] is True
    assert reply["menu"] is None
    assert any("app.theme" in error for error in reply["errors"])
    assert {a["layer"] for a in reply["answers"].values()} == {"default"}
    assert _answer(reply, "worker.network") == ("off", "default")


@pytest.mark.parametrize("key", ["notify.run_finished", "notify.run_failed"])
def test_removed_notification_keys_are_refused(key: str) -> None:
    for value in ("on", "off"):
        with pytest.raises(choices.InvalidChoiceError, match="unknown key"):
            choices.validate_value(key, value)


def test_stored_removed_key_is_reported_like_an_unknown_key(
    env: dict[str, str],
) -> None:
    choices.set_settings({"worker.network": "on"}, env=env)
    _raw(
        env,
        "INSERT INTO settings (key, value) VALUES ('notify.run_failed', ?)",
        '"on"',
    )
    reply = choices.read(env=env)
    assert reply["source_error"] is True
    assert any("notify.run_failed" in error for error in reply["errors"])
    assert any("unknown key" in error for error in reply["errors"])


@pytest.mark.parametrize("stored", ["not json", "[1]", '{"terminal": "x"}'])
def test_invalid_stored_once_fails_closed(env: dict[str, str], stored: str) -> None:
    choices.set_once({"single.writer": "ask"}, env=env)
    _raw(env, "UPDATE once SET value = ?", stored)
    reply = choices.read(env=env)
    assert reply["source_error"] is True
    assert reply["menu"] is None
    assert reply["errors"]


def test_invalid_claim_copy_fails_closed(env: dict[str, str]) -> None:
    choices.set_once({"single.writer": "ask"}, env=env)
    choices.claim("run-000001", env=env)
    _raw(env, 'UPDATE claims SET copy = \'{"single.writer": "zz"}\'')
    reply = choices.read("run-000001", env=env)
    assert reply["source_error"] is True
    assert reply["claim"] == {"id": "run-000001", "status": "claimed"}
    assert _answer(reply, "single.writer") == ("ask", "default")


# Claims


def test_claim_with_pending_once(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    reply = choices.claim("run-000001", env=env, now=T0 + 1)
    assert reply["claim"] == {"id": "run-000001", "status": "claimed"}
    assert _answer(reply, "single.writer_as") == ("subagent", "one-time")
    assert choices.snapshot(env=env, now=T0 + 2)["once"] is None
    reply = choices.read("run-000001", env=env, now=T0 + 2)
    assert _answer(reply, "single.writer_as") == ("subagent", "one-time")


def test_claim_without_pending_once(env: dict[str, str]) -> None:
    choices.set_settings({"single.writer_as": "subagent"}, env=env)
    reply = choices.claim("run-000001", env=env)
    assert reply["claim"] == {"id": "run-000001", "status": "none"}
    assert _answer(reply, "single.writer_as") == ("subagent", "setting")


def test_claim_retry_writes_nothing_and_keeps_newer_once(
    env: dict[str, str],
) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    choices.claim("run-000001", env=env, now=T0)
    choices.set_once({"single.writer_as": "cli"}, env=env, now=T0 + 10)
    generation = _generation(env)
    reply = choices.claim("run-000001", env=env, now=T0 + 20)
    assert reply["claim"]["status"] == "claimed"
    assert _answer(reply, "single.writer_as") == ("subagent", "one-time")
    assert reply["generation"] == generation
    once = choices.snapshot(env=env, now=T0 + 20)["once"]
    assert once["values"] == {"single.writer_as": "cli"}


def test_claim_retry_of_none_stays_none(env: dict[str, str]) -> None:
    choices.claim("run-000001", env=env)
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    reply = choices.claim("run-000001", env=env)
    assert reply["claim"]["status"] == "none"
    assert choices.snapshot(env=env)["once"] is not None


def test_read_of_unknown_id_is_unknown_and_defaults(env: dict[str, str]) -> None:
    choices.set_settings({"single.writer_as": "subagent"}, env=env)
    reply = choices.read(
        "run-000009", request={"single.reviewer_as": "subagent"}, env=env
    )
    assert reply["claim"] == {"id": "run-000009", "status": "unknown"}
    assert {a["layer"] for a in reply["answers"].values()} == {"default"}


def test_finish_marks_gone_and_is_idempotent(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    choices.claim("run-000001", env=env)
    generation = _generation(env)
    reply = choices.finish("run-000001", env=env)
    assert reply == {
        "claim": {"id": "run-000001", "status": "gone"},
        "generation": generation,
        "errors": [],
    }
    again = choices.finish("run-000001", env=env)
    assert again["generation"] == generation
    reply = choices.read(
        "run-000001", request={"single.reviewer_as": "subagent"}, env=env
    )
    assert reply["claim"]["status"] == "gone"
    assert {a["layer"] for a in reply["answers"].values()} == {"default"}
    assert choices.claim("run-000001", env=env)["claim"]["status"] == "gone"


def test_finish_of_unknown_id_records_gone(env: dict[str, str]) -> None:
    reply = choices.finish("run-000002", env=env)
    assert reply["claim"]["status"] == "gone"
    assert reply["generation"] == 0
    assert choices.read("run-000002", env=env)["claim"]["status"] == "gone"
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    assert choices.claim("run-000002", env=env)["claim"]["status"] == "gone"
    assert choices.snapshot(env=env)["once"] is not None


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "short", "Run-000001", "run-000001\n", "-run-00001", "a" * 65],
)
def test_bad_ids_are_refused(env: dict[str, str], bad_id: str) -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.read(bad_id, env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.claim(bad_id, env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.finish(bad_id, env=env)
    assert not _db_path(env).exists()


@pytest.mark.parametrize("good_id", ["abcdef", "0" * 64, "run-1-2-3"])
def test_good_ids_are_accepted(env: dict[str, str], good_id: str) -> None:
    assert choices.claim(good_id, env=env)["claim"]["status"] == "none"


# Expiry


def test_once_expires_at_exactly_24_hours(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    snap = choices.snapshot(env=env, now=T0 + DAY - 0.5)
    assert snap["once"] == {
        "values": {"single.writer_as": "subagent"},
        "set_at": T0,
        "expires_at": T0 + DAY,
    }
    assert choices.snapshot(env=env, now=T0 + DAY)["once"] is None
    reply = choices.claim("run-000001", env=env, now=T0 + DAY)
    assert reply["claim"]["status"] == "none"
    assert reply["generation"] == snap["generation"]


def test_once_just_before_expiry_is_claimed(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    reply = choices.claim("run-000001", env=env, now=T0 + DAY - 0.5)
    assert reply["claim"]["status"] == "claimed"


def test_expiry_is_projected_by_reads_and_stored_by_next_write(
    env: dict[str, str],
) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    generation = _generation(env)
    assert choices.snapshot(env=env, now=T0 + DAY)["generation"] == generation
    reply = choices.set_settings({}, env=env, now=T0 + DAY)
    assert reply["generation"] == generation
    conn = sqlite3.connect(_db_path(env))
    assert conn.execute("SELECT count(*) FROM once").fetchone()[0] == 0
    conn.close()


def test_claim_expires_at_exactly_7_days(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    choices.claim("run-000001", env=env, now=T0)
    week = 7 * DAY
    before = choices.read("run-000001", env=env, now=T0 + week - 0.5)
    assert before["claim"]["status"] == "claimed"
    assert choices.snapshot(env=env, now=T0 + week - 0.5)["claims"] == [
        {"id": "run-000001", "at": T0}
    ]
    after = choices.read("run-000001", env=env, now=T0 + week)
    assert after["claim"]["status"] == "gone"
    assert after["generation"] == before["generation"]
    assert _answer(after, "single.writer_as") == ("cli", "default")
    assert choices.snapshot(env=env, now=T0 + week)["claims"] == []
    generation = _generation(env)
    choices.clear_once(env=env, now=T0 + week)
    assert _generation(env) == generation
    conn = sqlite3.connect(_db_path(env))
    row = conn.execute("SELECT status, copy FROM claims").fetchone()
    conn.close()
    assert row == ("gone", None)


# Generation


def test_reads_never_write(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    choices.claim("run-000001", env=env, now=T0)
    path = _db_path(env)
    conn = sqlite3.connect(path)
    before = conn.execute("SELECT * FROM meta ORDER BY key").fetchall()
    conn.close()
    choices.read(env=env, now=T0 + 30 * DAY)
    choices.read("run-000001", env=env, now=T0 + 30 * DAY)
    choices.read("run-000002", env=env, now=T0 + 30 * DAY)
    choices.snapshot(env=env, now=T0 + 30 * DAY)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT * FROM meta ORDER BY key").fetchall() == before
    assert conn.execute("SELECT status FROM claims").fetchall() == [("claimed",)]
    assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    conn.close()


def test_generation_rises_for_settings_and_once_writes(env: dict[str, str]) -> None:
    assert choices.set_settings({"app.theme": "dark"}, env=env)["generation"] == 1
    reply = choices.set_settings(
        {"app.theme": "light", "worker.network": "on"}, env=env
    )
    assert reply["generation"] == 2
    assert choices.set_once({"single.writer": "ask"}, env=env)["generation"] == 3
    assert choices.claim("run-000001", env=env)["generation"] == 4
    assert choices.claim("run-000002", env=env)["generation"] == 4
    assert choices.read("run-000002", env=env)["generation"] == 4
    assert choices.finish("run-000002", env=env)["generation"] == 4
    assert choices.clear_settings(["app.theme"], env=env)["generation"] == 5
    choices.set_once({"single.writer": "ask"}, env=env)
    assert choices.clear_once(env=env)["generation"] == 7


def test_no_op_writes_keep_the_generation(env: dict[str, str]) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    assert choices.set_settings({"app.theme": "dark"}, env=env)["generation"] == 1
    assert choices.set_settings({}, env=env)["generation"] == 1
    assert choices.clear_settings(["worker.network"], env=env)["generation"] == 1
    assert choices.clear_once(env=env)["generation"] == 1
    choices.finish("run-000001", env=env)
    assert choices.finish("run-000001", env=env)["generation"] == 1
    choices.claim("run-000003", env=env)
    assert choices.claim("run-000003", env=env)["generation"] == 1


def test_replacing_once_with_the_same_values_raises_generation(
    env: dict[str, str],
) -> None:
    values = {"single.writer": "ask"}
    assert choices.set_once(values, env=env, now=T0)["generation"] == 1
    assert choices.set_once(values, env=env, now=T0)["generation"] == 2


def test_consuming_once_refuses_a_save_with_the_old_generation(
    env: dict[str, str],
) -> None:
    generation = choices.set_once({"single.writer": "ask"}, env=env)["generation"]
    assert choices.claim("run-000001", env=env)["generation"] == generation + 1
    with pytest.raises(db.StaleGenerationError):
        choices.set_settings({"app.theme": "dark"}, generation, env=env)
    assert choices.snapshot(env=env)["settings"] == {}


def test_existing_database_keeps_its_generation(env: dict[str, str]) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    _raw(env, "UPDATE meta SET value = '42' WHERE key = 'generation'")
    assert choices.snapshot(env=env)["generation"] == 42
    assert choices.claim("run-000001", env=env)["generation"] == 42
    assert choices.finish("run-000001", env=env)["generation"] == 42
    reply = choices.set_settings({"app.theme": "light"}, 42, env=env)
    assert reply["generation"] == 43


def test_stale_generation_changes_nothing(env: dict[str, str]) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    writes = [
        lambda: choices.set_settings({"app.theme": "light"}, 0, env=env),
        lambda: choices.clear_settings(["app.theme"], 0, env=env),
        lambda: choices.set_once({"single.writer": "ask"}, 0, env=env),
        lambda: choices.clear_once(0, env=env),
        lambda: choices.claim("run-000001", expect_generation=0, env=env),
        lambda: choices.finish("run-000001", expect_generation=0, env=env),
    ]
    for write in writes:
        with pytest.raises(db.StaleGenerationError) as info:
            write()
        assert info.value.exit_code == 75
    snap = choices.snapshot(env=env)
    assert snap["generation"] == 1
    assert snap["settings"] == {"app.theme": "dark"}
    assert snap["once"] is None
    assert choices.read("run-000001", env=env)["claim"]["status"] == "unknown"
    reply = choices.set_settings({"app.theme": "light"}, 1, env=env)
    assert reply["generation"] == 2


def test_expect_generation_refuses_bool(env: dict[str, str]) -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_settings({"app.theme": "dark"}, True, env=env)  # pyrefly: ignore
    assert not _db_path(env).exists()


def test_set_once_replaces_whole_and_needs_a_value(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env, now=T0)
    choices.set_once({"single.reviewer_as": "subagent"}, env=env, now=T0 + 5)
    once = choices.snapshot(env=env, now=T0 + 6)["once"]
    assert once["values"] == {"single.reviewer_as": "subagent"}
    assert once["set_at"] == T0 + 5
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_once({}, env=env)


def test_snapshot_lists_allowed_values(env: dict[str, str]) -> None:
    choices.set_settings(
        {"shortlist.codex": ["gpt-5@high"], "app.theme": "dark"}, env=env
    )
    snap = choices.snapshot(env=env)
    assert snap["generation"] == 1
    assert snap["settings"] == {"shortlist.codex": ["gpt-5@high"], "app.theme": "dark"}
    assert snap["allowed"]["single.writer"] == {"values": ["ask", "codex:gpt-5"]}
    assert snap["allowed"]["app.theme"] == {"values": ["system", "light", "dark"]}
    assert "pattern" in snap["allowed"]["terminal"]
    assert snap["source_error"] is False
    assert snap["menu"]["single.writer"]["state"] == "ask"


# Concurrency


def _run_together(count: int, target: Callable[[int], object]) -> list[object]:
    barrier = threading.Barrier(count)
    results: list[object] = [None] * count

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = target(index)
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_two_concurrent_claims_of_one_once(env: dict[str, str]) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    results = _run_together(
        2, lambda i: choices.claim(f"run-00000{i}", env=env)["claim"]["status"]
    )
    assert sorted(str(r) for r in results) == ["claimed", "none"]


def test_two_concurrent_checked_writes(env: dict[str, str]) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    themes = ["light", "system"]
    results = _run_together(
        2, lambda i: choices.set_settings({"app.theme": themes[i]}, 1, env=env)
    )
    saved = [r for r in results if isinstance(r, dict)]
    stale = [r for r in results if isinstance(r, db.StaleGenerationError)]
    assert len(saved) == 1
    assert len(stale) == 1
    snap = choices.snapshot(env=env)
    assert snap["generation"] == 2
    assert snap["settings"]["app.theme"] == saved[0]["settings"]["app.theme"]


def test_concurrent_first_writes_create_one_database(env: dict[str, str]) -> None:
    keys = ["app.theme", "worker.network", "app.autostart", "terminal"]
    values = ["dark", "on", "off", "kitty"]
    results = _run_together(
        4, lambda i: choices.set_settings({keys[i]: values[i]}, env=env)
    )
    assert all(isinstance(r, dict) for r in results), results
    snap = choices.snapshot(env=env)
    assert snap["generation"] == 4
    assert snap["settings"] == dict(zip(keys, values, strict=True))


def test_check_id_refuses_non_strings() -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.check_id(7)


def test_parse_value() -> None:
    assert choices.parse_value("shortlist.codex", "") == []
    assert choices.parse_value("shortlist.codex", "a@b,c@d") == ["a@b", "c@d"]
    assert choices.parse_value("terminal", "kitty -e") == "kitty -e"
    with pytest.raises(choices.InvalidChoiceError):
        choices.parse_value("nope", "x")


def test_reply_is_json_serialisable(env: dict[str, str]) -> None:
    choices.set_once({"single.writer": "ask"}, env=env)
    json.dumps(choices.claim("run-000001", env=env))
    json.dumps(choices.snapshot(env=env))


def _claim_in_process(
    env: dict[str, str],
    run_id: str,
    barrier: Any,  # noqa: ANN401
    results: Any,  # noqa: ANN401
) -> None:
    barrier.wait()
    try:
        results.put(choices.claim(run_id, env=env)["claim"]["status"])
    except Exception as error:
        results.put(repr(error))


def _claim_in_processes(env: dict[str, str], count: int) -> list[str]:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(count)
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_in_process, args=(env, f"run-{i:06d}", barrier, results)
        )
        for i in range(count)
    ]
    for process in processes:
        process.start()
    statuses = [results.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(timeout=60)
    return statuses


def test_first_claims_from_processes_on_a_new_database(
    env: dict[str, str],
) -> None:
    assert not _db_path(env).exists()
    assert _claim_in_processes(env, 6) == ["none"] * 6
    assert _generation(env) == 0


def test_claims_from_processes_take_a_pending_once_once(
    env: dict[str, str],
) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    statuses = _claim_in_processes(env, 6)
    assert sorted(statuses) == ["claimed"] + ["none"] * 5
    assert _generation(env) == 2


# An invalid pending once


def _store_invalid_once(env: dict[str, str]) -> None:
    choices.set_once({"single.writer": "ask"}, env=env)
    _raw(env, "UPDATE once SET value = ?", '{"single.writer": "zz"}')


def test_invalid_pending_once_does_not_affect_runs_with_a_record(
    env: dict[str, str],
) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    choices.claim("run-000001", env=env)
    choices.claim("run-000002", env=env)
    _store_invalid_once(env)
    for reply in (
        choices.read("run-000001", env=env),
        choices.claim("run-000001", env=env),
    ):
        assert reply["source_error"] is False
        assert reply["claim"]["status"] == "claimed"
        assert _answer(reply, "single.writer_as") == ("subagent", "one-time")
        assert _answer(reply, "app.theme") == ("dark", "setting")
    reply = choices.read("run-000002", env=env)
    assert reply["source_error"] is False
    assert reply["claim"]["status"] == "none"
    assert _answer(reply, "app.theme") == ("dark", "setting")


def test_invalid_pending_once_fails_closed_where_it_is_read(
    env: dict[str, str],
) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    _store_invalid_once(env)
    assert choices.read(env=env)["source_error"] is True
    assert choices.snapshot(env=env)["source_error"] is True
    generation = _generation(env)
    reply = choices.claim("run-000003", env=env)
    assert reply["source_error"] is True
    assert reply["claim"] == {"id": "run-000003", "status": "unknown"}
    assert _answer(reply, "app.theme") == ("system", "default")
    assert reply["generation"] == generation
    conn = sqlite3.connect(_db_path(env))
    assert conn.execute("SELECT count(*) FROM once").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
    conn.close()


# Engine status in the menu


def _engine_cache(env: dict[str, str], connected: dict[str, bool]) -> None:
    def entry(engine: str) -> dict[str, Any]:
        ok = connected.get(engine, True)
        return status.EngineStatus(
            engine=engine,
            state=status.CONNECTED if ok else status.NOT_SIGNED_IN,
            reason=None if ok else "signed out",
            installed=True,
            binary=f"/usr/bin/{engine}",
            version="1",
            signed_in=ok,
            connected=ok,
            requirements=(),
            checked_at=T0,
        ).to_json()

    payload = {"engines": {engine: entry(engine) for engine in status.ENGINES}}
    status.save_cache_file(status.cache_path(env), payload, env)


def _codex_writer(env: dict[str, str], shortlist: list[str]) -> None:
    choices.set_settings(
        {"shortlist.codex": shortlist, "single.writer": "codex:gpt-5"}, env=env
    )


def test_menu_row_not_connected(env: dict[str, str]) -> None:
    _codex_writer(env, ["gpt-5@high"])
    _engine_cache(env, {"codex": False})
    row = choices.read(env=env)["menu"]["single.writer"]
    assert row["state"] == "not connected"
    assert row["reason"] == "codex is not connected"
    assert row["engine"] == "codex"
    assert row["model"] == "gpt-5"


def test_not_connected_comes_before_not_shortlisted(env: dict[str, str]) -> None:
    _codex_writer(env, ["gpt-5@high"])
    choices.set_settings({"shortlist.codex": ["o3@low"]}, env=env)
    _engine_cache(env, {"codex": False})
    row = choices.read(env=env)["menu"]["single.writer"]
    assert row["state"] == "not connected"


def test_engine_off_comes_before_not_connected(env: dict[str, str]) -> None:
    _codex_writer(env, ["gpt-5@high"])
    choices.set_settings({"engine.codex": "off"}, env=env)
    _engine_cache(env, {"codex": False})
    row = choices.read(env=env)["menu"]["single.writer"]
    assert row["state"] == "engine off"


def test_connected_engine_stays_set(env: dict[str, str]) -> None:
    _codex_writer(env, ["gpt-5@high"])
    _engine_cache(env, {"claude": False})
    row = choices.read(env=env)["menu"]["single.writer"]
    assert row["state"] == "set"
    assert row["effort"] == "high"


@pytest.mark.parametrize("content", [None, "{broken", '{"engines": {}}'])
def test_missing_or_invalid_engine_cache_changes_nothing(
    env: dict[str, str], content: str | None
) -> None:
    _codex_writer(env, ["gpt-5@high"])
    path = status.cache_path(env)
    if content is not None:
        path.parent.mkdir(parents=True)
        path.write_text(content)
    row = choices.read(env=env)["menu"]["single.writer"]
    assert row["state"] == "set"
    if content is None:
        assert not path.exists()


def test_menu_never_runs_engines(env: dict[str, str], tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    stub = Path(env["PATH"]) / "codex"
    stub.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    stub.chmod(0o755)
    _codex_writer(env, ["gpt-5@high"])
    choices.read(env=env)
    choices.snapshot(env=env)
    assert not marker.exists()
    assert not status.cache_path(env).exists()


def test_snapshot_menu_reports_not_connected(env: dict[str, str]) -> None:
    _codex_writer(env, ["gpt-5@high"])
    _engine_cache(env, {"codex": False})
    row = choices.snapshot(env=env)["menu"]["single.writer"]
    assert row["state"] == "not connected"


def test_opencode_shortlist_takes_tilde_provider_ids(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    choices.set_settings(
        {
            "shortlist.opencode": ["~anthropic/claude-opus-latest@high"],
            "single.writer": "opencode:~anthropic/claude-opus-latest",
        },
        env=env,
    )
    answers = choices.read(env=env)["answers"]
    assert answers["single.writer"]["value"] == "opencode:~anthropic/claude-opus-latest"


def test_multi_runs_as_default(env: dict[str, str]) -> None:
    reply = choices.read(env=env)
    assert reply["menu"]["multi.runs_as"] == {
        "value": "ask",
        "state": "ask",
        "reason": "asks at each run",
    }
    assert choices.MENU_KEYS == ("single.writer", "single.reviewer")
    assert choices.allowed_values(reply["answers"])["multi.runs_as"] == {
        "values": ["ask", "flow", "workflow"],
    }


def test_multi_runs_as_layers(env: dict[str, str]) -> None:
    choices.set_settings({"multi.runs_as": "flow"}, env=env)
    assert choices.read(env=env)["menu"]["multi.runs_as"] == {
        "value": "flow",
        "state": "set",
        "reason": None,
    }
    choices.set_once({"multi.runs_as": "workflow"}, env=env)
    claimed = choices.claim("flow-choice-123", env=env)
    assert claimed["menu"]["multi.runs_as"]["value"] == "workflow"
    assert (
        choices.read(id="flow-choice-123", env=env)["answers"]["multi.runs_as"]["layer"]
        == "one-time"
    )
    requested = choices.read(
        id="flow-choice-123", request={"multi.runs_as": "ask"}, env=env
    )
    assert requested["menu"]["multi.runs_as"]["state"] == "ask"
    choices.finish("flow-choice-123", env=env)
    assert choices.read(env=env)["menu"]["multi.runs_as"]["value"] == "flow"


def test_multi_runs_as_invalid(env: dict[str, str]) -> None:
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_settings({"multi.runs_as": "panel"}, env=env)
    with pytest.raises(choices.InvalidChoiceError):
        choices.set_once({"multi.runs_as": "panel"}, env=env)


def test_flow_plan_schema_agreement(env: dict[str, str]) -> None:
    from ember.flows import plan

    schema = json.loads(plan.schema_text())
    assert schema["$id"] == "ember/flow-plan/1"
    assert schema["required"] == ["dir", "tasks"]
    assert schema["properties"]["tasks"]["minItems"] == 1
    assert schema["additionalProperties"] is False
    definitions = schema["$defs"]
    assert definitions["task"]["required"] == ["id", "task", "files"]
    assert definitions["task"]["additionalProperties"] is False
    assert definitions["worker"]["required"] == ["engine", "model"]
    assert definitions["worker"]["additionalProperties"] is False
    assert definitions["worker"]["properties"]["engine"]["enum"] == list(status.ENGINES)
    fields = definitions["task"]["properties"]
    assert fields["id"]["pattern"] == "^" + plan.TASK_ID + r"$(?![\s\S])"
    assert fields["subject"]["maxLength"] == plan.SUBJECT_LIMIT
    for key, minimum, maximum in (("rounds", 0, 3), ("parallel", 1, 8)):
        assert schema["properties"][key]["minimum"] == minimum
        assert schema["properties"][key]["maximum"] == maximum


@pytest.fixture
def panel_env(tmp_path: Path) -> dict[str, str]:
    from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]

    return make_env(tmp_path)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("codex:gpt-6-astra", "not a list of strings"),
        ([1], "not a list of strings"),
        (["bad"], "not <engine>:<model>"),
        (["ask"], "not <engine>:<model>"),
        (["codex:gpt-6-astra@low"], "not <engine>:<model>"),
        (["codex:gpt-6-astra"] * 2, "listed more than once"),
        ([f"codex:model-{n}" for n in range(9)], "at most 8 members"),
    ],
)
def test_panel_members_invalid(value: object, message: str) -> None:
    with pytest.raises(choices.InvalidChoiceError, match=message):
        choices.validate_value("panel.members", value)


@pytest.mark.parametrize("once", [False, True])
def test_panel_members_must_be_shortlisted(
    panel_env: dict[str, str], once: bool
) -> None:
    save = choices.set_once if once else choices.set_settings
    with pytest.raises(
        choices.InvalidChoiceError,
        match="panel.members: other is not on the codex shortlist",
    ):
        save({"panel.members": ["codex:other"]}, env=panel_env)


def test_panel_members_read_reply_and_allowed(panel_env: dict[str, str]) -> None:
    choices.set_settings(
        {
            "shortlist.opencode": ["vendor/model:free"],
            "panel.members": ["codex:gpt-5", "opencode:vendor/model:free"],
        },
        env=panel_env,
    )
    reply = choices.read(env=panel_env)
    assert list(reply["menu"]) == [
        "single.writer",
        "single.reviewer",
        "multi.runs_as",
        "panel.members",
    ]
    assert reply["menu"]["panel.members"] == {
        "value": ["codex:gpt-5", "opencode:vendor/model:free"],
        "state": "set",
        "members": [
            {
                "value": "codex:gpt-5",
                "state": "set",
                "engine": "codex",
                "model": "gpt-5",
                "effort": "high",
                "reason": None,
            },
            {
                "value": "opencode:vendor/model:free",
                "state": "set",
                "engine": "opencode",
                "model": "vendor/model:free",
                "effort": None,
                "reason": None,
            },
        ],
    }
    assert choices.allowed_values(reply["answers"])["panel.members"] == {
        "values": ["claude:opus", "codex:gpt-5", "opencode:vendor/model:free"]
    }
    assert choices.parse_value(
        "panel.members", "opencode:vendor/model:free,codex:gpt-6-astra"
    ) == ["opencode:vendor/model:free", "codex:gpt-6-astra"]
    assert choices.parse_value("panel.members", "") == []
    choices.set_settings({"panel.members": []}, env=panel_env)
    assert choices.read(env=panel_env)["menu"]["panel.members"] == {
        "value": [],
        "state": "empty",
        "members": [],
    }


@pytest.mark.parametrize(
    ("value", "enabled", "connected", "shortlist", "state", "reason"),
    [
        ("bad", "off", False, [], "invalid", "not <engine>:<model>"),
        ("ask", "on", True, [], "invalid", "not <engine>:<model>"),
        ("codex:gpt-5", "off", False, [], "engine off", "codex is off"),
        ("codex:gpt-5", "on", False, [], "not connected", "codex is not connected"),
        (
            "codex:gpt-5",
            "on",
            True,
            [],
            "not shortlisted",
            "not on the codex shortlist",
        ),
        ("codex:gpt-5", "on", True, ["gpt-5@low"], "set", None),
    ],
)
def test_panel_member_row_check_order(
    panel_env: dict[str, str],
    value: str,
    enabled: str,
    connected: bool,
    shortlist: list[str],
    state: str,
    reason: str | None,
) -> None:
    answers = choices.read(env=panel_env)["answers"]
    answers["panel.members"]["value"] = [value]
    answers["engine.codex"]["value"] = enabled
    answers["shortlist.codex"]["value"] = shortlist
    result = choices.panel_row(answers, {"codex": connected})
    assert result["state"] == "set"
    row = result["members"][0]
    assert row["state"] == state
    assert row["reason"] == reason
    assert row["effort"] == ("low" if state == "set" else None)
    assert "run_as" not in row
    assert row["engine"] == (None if state == "invalid" else "codex")
    assert row["model"] == (None if state == "invalid" else "gpt-5")


def test_panel_members_accepts_eight_and_empty_layers(
    panel_env: dict[str, str],
) -> None:
    values = [f"codex:model-{n}" for n in range(8)]
    choices.set_settings(
        {
            "shortlist.codex": [f"model-{n}@low" for n in range(8)],
            "panel.members": values,
        },
        env=panel_env,
    )
    assert choices.read(env=panel_env)["menu"]["panel.members"]["value"] == values
    choices.set_once({"panel.members": []}, env=panel_env)
    choices.claim("panel-choice", env=panel_env)
    reply = choices.read("panel-choice", env=panel_env)
    assert _answer(reply, "panel.members") == ([], "one-time")
    assert reply["menu"]["panel.members"]["state"] == "empty"
    reply = choices.read(
        "panel-choice", request={"panel.members": values}, env=panel_env
    )
    assert _answer(reply, "panel.members") == (values, "request")


@pytest.mark.parametrize("layer", ["setting", "one-time", "claim"])
def test_broken_panel_data_preserves_other_settings(
    panel_env: dict[str, str], layer: str
) -> None:
    choices.set_settings(
        {"engine.codex": "off", "sandbox.codex": "on", "worker.network": "on"},
        env=panel_env,
    )
    claim = None
    if layer == "setting":
        _raw(panel_env, "INSERT INTO settings VALUES (?, ?)", "panel.members", "{")
    elif layer == "one-time":
        choices.set_once({"panel.members": []}, env=panel_env)
        _raw(
            panel_env,
            "UPDATE once SET value = ?",
            json.dumps({"panel.members": "bad", "multi.runs_as": "flow"}),
        )
    else:
        choices.set_once({"panel.members": [], "multi.runs_as": "flow"}, env=panel_env)
        choices.claim("panel-choice", env=panel_env)
        _raw(
            panel_env,
            "UPDATE claims SET copy = ? WHERE id = ?",
            json.dumps({"panel.members": "bad", "multi.runs_as": "flow"}),
            "panel-choice",
        )
        claim = "panel-choice"
    reply = choices.read(claim, env=panel_env)
    assert reply["source_error"] is True
    assert reply["menu"] is None
    assert "panel.members" in reply["errors"][0]
    assert _answer(reply, "engine.codex") == ("off", "setting")
    assert _answer(reply, "sandbox.codex") == ("on", "setting")
    assert _answer(reply, "worker.network") == ("on", "setting")
    if layer == "claim":
        assert _answer(reply, "multi.runs_as") == ("flow", "one-time")


def test_panel_error_does_not_hide_other_corruption(panel_env: dict[str, str]) -> None:
    choices.set_settings({"panel.members": [], "app.theme": "dark"}, env=panel_env)
    _raw(
        panel_env,
        "UPDATE settings SET value = ? WHERE key IN (?, ?)",
        '"bad"',
        "panel.members",
        "app.theme",
    )
    reply = choices.read(env=panel_env)
    assert reply["source_error"]
    assert len(reply["errors"]) == 2
    assert all(answer["layer"] == "default" for answer in reply["answers"].values())


def test_invalid_panel_once_is_not_claimed(panel_env: dict[str, str]) -> None:
    choices.set_once({"panel.members": []}, env=panel_env)
    _raw(panel_env, "UPDATE once SET value = ?", '{"panel.members":"bad"}')
    reply = choices.claim("panel-choice", env=panel_env)
    assert reply["claim"]["status"] == "unknown"
    assert reply["source_error"]
    with sqlite3.connect(_db_path(panel_env)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM once").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
