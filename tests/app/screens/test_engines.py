import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from pytestqt.qtbot import QtBot

from ember import choices, sandbox
from ember.app.context import AppContext
from ember.app.screens.engines import (
    NO_EFFORT_NOTE,
    EnginesScreen,
    badge_for,
    cause_advice,
    context_text,
    initial_effort,
    per_token,
    price_text,
    shortlist_with,
    when,
)
from ember.app.widgets import CommandLine
from ember.engines import models, status

NOW = time.time()


def _status(engine: str, state: str, reason: str | None = None) -> status.EngineStatus:
    installed = state != status.NOT_INSTALLED
    if state == status.NOT_INSTALLED:
        requirements = (
            status.Requirement(f"the {engine} CLI", False, status.INSTALL[engine]),
            status.Requirement(status.SIGN_IN_LABEL[engine], False, None),
        )
        reason = reason or f"the {engine} CLI is not installed"
    elif state == status.NOT_SIGNED_IN:
        fix = " ".join([engine, *status.SIGN_IN_ARGS[engine]])
        requirements = (
            status.Requirement(f"the {engine} CLI", True, None),
            status.Requirement(status.SIGN_IN_LABEL[engine], False, fix),
        )
    elif state == status.CHECK_FAILED:
        requirements = (
            status.Requirement(f"the {engine} CLI", True, None),
            status.Requirement(status.SIGN_IN_LABEL[engine], False, None),
        )
    else:
        requirements = (
            status.Requirement(f"the {engine} CLI", True, None),
            status.Requirement(status.SIGN_IN_LABEL[engine], True, None),
        )
    return status.EngineStatus(
        engine=engine,
        state=state,
        reason=reason,
        installed=installed,
        binary=f"/usr/bin/{engine}" if installed else None,
        version=f"{engine} 1.0.0" if installed else None,
        signed_in=state == status.CONNECTED if installed else None,
        connected=state == status.CONNECTED,
        requirements=requirements,
        checked_at=NOW,
    )


def _all(**states: str) -> dict[str, status.EngineStatus]:
    return {
        engine: _status(engine, states.get(engine, status.CONNECTED))
        for engine in status.ENGINES
    }


def _check(
    engine: str, ok: bool = True, detail: str = "", causes: tuple[str, ...] = ()
) -> sandbox.SandboxCheck:
    return sandbox.SandboxCheck(
        engine=engine,
        provider=sandbox.BUBBLEWRAP,
        bwrap_path="/usr/bin/bwrap",
        bwrap_version="0.11.1",
        ok=ok,
        detail=detail or "/bin/true ran under bubblewrap",
        causes=causes,
        checked_at=NOW,
    )


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub engine status, sandbox checks and model lists."""
    held: dict[str, Any] = {
        "engines": _all(),
        "checks": [],
        "lists": {},
        "list_calls": [],
    }

    def read(_env: object, *, refresh: bool = False) -> dict[str, status.EngineStatus]:
        return held["engines"]

    def check(engine: str, _env: object = None) -> sandbox.SandboxCheck:
        held["checks"].append(engine)
        return held.get("check_result", {}).get(engine) or _check(engine)

    def list_models(
        engine: str, _env: object = None, *, refresh: bool = False
    ) -> models.ModelList:
        held["list_calls"].append((engine, refresh))
        return held["lists"][engine]

    monkeypatch.setattr(status, "read", read)
    monkeypatch.setattr(sandbox, "check", check)
    monkeypatch.setattr(models, "list_models", list_models)
    return held


def _screen(env: dict[str, str], qtbot: QtBot) -> tuple[EnginesScreen, AppContext]:
    context = AppContext(env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    screen = EnginesScreen(context)
    qtbot.addWidget(screen)
    return screen, context


def _shown(screen: EnginesScreen, context: AppContext, qtbot: QtBot) -> None:
    with qtbot.waitSignal(context.engines_changed, timeout=5000):
        screen.shown()
    context.tasks.wait(5000)
    QApplication.processEvents()


def _save(context: AppContext, qtbot: QtBot, action: Callable[[], None]) -> None:
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        action()


# Pure functions


def test_badge_order() -> None:
    connected = _status("codex", status.CONNECTED)
    assert badge_for(False, None, "none") == ("Off", "neutral")
    assert badge_for(False, _status("codex", status.NOT_INSTALLED), "codex") == (
        "Off",
        "neutral",
    )
    assert badge_for(True, None, "codex") == ("Not checked", "neutral")
    assert badge_for(True, _status("codex", status.NOT_INSTALLED), "none") == (
        "Not installed",
        "danger",
    )
    assert badge_for(True, _status("codex", status.NOT_SIGNED_IN), "none") == (
        "Not signed in",
        "warning",
    )
    assert badge_for(True, _status("codex", status.CHECK_FAILED, "x"), "none") == (
        "Check failed",
        "danger",
    )
    assert badge_for(True, connected, "none") == ("No sandbox", "warning")
    assert badge_for(True, connected, "codex") == ("Connected", "success")


def test_when_formats_local_times() -> None:
    now = datetime(2026, 11, 2, 12, 0).timestamp()
    assert when(datetime(2026, 11, 2, 10, 42).timestamp(), now) == "Today 10:42"
    assert when(datetime(2026, 11, 1, 17, 3).timestamp(), now) == "Yesterday 17:03"
    assert when(datetime(2026, 10, 30, 14, 5).timestamp(), now) == "30 Oct 14:05"


def test_shortlist_entries_per_engine() -> None:
    assert shortlist_with("codex", [], "gpt-5", "high", True) == ["gpt-5@high"]
    assert shortlist_with("claude", ["opus@max"], "sonnet", "low", True) == [
        "opus@max",
        "sonnet@low",
    ]
    assert shortlist_with("opencode", [], "qwen/coder", None, True) == ["qwen/coder"]
    assert shortlist_with("opencode", [], "qwen/coder", "high", True) == [
        "qwen/coder@high"
    ]
    entries = ["a@low", "b@high", "c@low"]
    assert shortlist_with("codex", entries, "b", "max", True) == [
        "a@low",
        "b@max",
        "c@low",
    ]
    assert shortlist_with("codex", entries, "b", None, False) == ["a@low", "c@low"]


def test_initial_effort() -> None:
    codex = models.Model("gpt", "GPT", ("low", "high"), "high")
    assert initial_effort("codex", codex, "low") == "low"
    assert initial_effort("codex", codex, "bogus") == "high"
    assert initial_effort("codex", codex, None) == "high"
    claude = models.Model("opus", "opus", models.CLAUDE_EFFORTS)
    assert initial_effort("claude", claude, None) == "low"
    assert initial_effort("claude", claude, "max") == "max"
    assert initial_effort("codex", models.Model("x", "x"), None) is None
    router = models.Model("a/b", "B", ("low", "high"), "low")
    assert initial_effort("opencode", router, None) is None
    assert initial_effort("opencode", router, "high") == "high"


def test_number_formats() -> None:
    assert context_text(131_072) == "131k"
    assert context_text(1_000_000) == "1M"
    assert context_text(None) == "—"
    assert price_text(0.0000005) == "$0.50"
    assert price_text(None) == "—"
    assert per_token("0.5") == 0.0000005
    assert per_token(None) is None


def test_cause_advice_names_what_to_do() -> None:
    assert "sandbox_mode" in cause_advice(
        "/etc/codex/config.toml sets sandbox_mode or cannot be read"
    )
    assert "AppArmor" not in cause_advice("Ubuntu's AppArmor profile is present")
    assert "~/.local" in cause_advice(
        "Codex is installed at /x, which the sandbox does not keep"
    )


# Cards


def test_cards_show_badges_and_versions(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["engines"] = _all(codex=status.NOT_SIGNED_IN, opencode=status.NOT_INSTALLED)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert screen.cards["codex"].badge.text() == "Not signed in"
    assert screen.cards["opencode"].badge.text() == "Not installed"
    assert screen.cards["opencode"].badge.tone() == "danger"
    claude = screen.cards["claude"]
    expected = (
        "Connected" if context.setting("sandbox.claude") == "on" else "No sandbox"
    )
    assert claude.badge.text() == expected
    assert claude.version_label.text() == "claude 1.0.0"
    assert screen.subtitle.text().startswith("Checked today ")
    assert claude.logo.pixmap() is not None


def test_engine_off_shows_off(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"engine.codex": "off"}, env=env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert screen.cards["codex"].badge.text() == "Off"
    assert not screen.cards["codex"].use_switch.isChecked()


def test_no_sandbox_badge_and_warning(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"sandbox.claude": "off"}, env=env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    card = screen.cards["claude"]
    assert card.badge.text() == "No sandbox"
    assert card.provider_label.text() == "Provider: None"
    assert not card.none_label.isHidden()


def test_setup_steps_per_state(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["engines"] = {
        "codex": _status("codex", status.NOT_SIGNED_IN),
        "opencode": _status("opencode", status.NOT_INSTALLED),
        "claude": _status("claude", status.CHECK_FAILED, "claude auth status exited 2"),
    }
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    codex = screen.cards["codex"]
    assert [line.command() for line in codex.commands] == ["codex login"]
    opencode = screen.cards["opencode"]
    assert [line.command() for line in opencode.commands] == [
        "curl -fsSL https://opencode.ai/install | bash"
    ]
    claude = screen.cards["claude"]
    assert claude.commands == []
    assert not claude.setup_widget.isHidden()
    assert claude.reason_label.text() == "Claude auth status exited 2"
    texts = [w.text() for w in claude.setup_widget.findChildren(QLabel)]
    assert any("Claude auth status exited 2" in text for text in texts)


def test_signed_in_engine_has_no_setup(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert screen.cards["codex"].setup_widget.isHidden()
    assert screen.cards["codex"].findChildren(CommandLine) == []


@pytest.mark.parametrize("accepted", [True, False])
def test_run_goes_through_the_context(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    accepted: bool,
) -> None:
    stubs["engines"] = _all(codex=status.NOT_SIGNED_IN)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    asked: list[str] = []

    def run_command(_parent: QWidget, command: str) -> bool:
        asked.append(command)
        return accepted

    monkeypatch.setattr(context, "run_command", run_command)
    card = screen.cards["codex"]
    qtbot.mouseClick(card.commands[0].run_button, Qt.MouseButton.LeftButton)
    assert asked == ["codex login"]
    assert card.again_widget.isHidden() is not accepted


def test_check_now_reads_status_and_checks_engines_that_are_on(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"engine.opencode": "off"}, env=env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert stubs["checks"] == []
    with qtbot.waitSignal(context.engines_changed, timeout=5000):
        qtbot.mouseClick(screen.check_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not screen.checking, timeout=5000)
    assert sorted(stubs["checks"]) == ["claude", "codex"]
    assert (
        screen.cards["codex"]
        .check_label.text()
        .startswith("Last check: Passed, today ")
    )
    assert screen.cards["opencode"].check_label.text() == "Last check: Not run"
    assert set(sandbox.last_checks(env)) == {"claude", "codex"}


def test_sandbox_switch_saves_then_runs_the_check(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"sandbox.opencode": "on"}, env=env)
    stubs["check_result"] = {
        "opencode": _check("opencode", ok=False, detail="the sandbox test failed")
    }
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    card = screen.cards["opencode"]
    assert card.sandbox_switch.isChecked()
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(card.sandbox_switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("sandbox.opencode") == "off"
    qtbot.waitUntil(lambda: stubs["checks"] == ["opencode"], timeout=5000)
    qtbot.waitUntil(lambda: not screen.checking, timeout=5000)
    assert "Failed, today" in card.check_label.text()
    assert card.check_label.text().endswith(": the sandbox test failed")
    assert card.bwrap_label.text() == "bwrap: /usr/bin/bwrap, 0.11.1"
    assert sandbox.last_checks(env)["opencode"].ok is False


def test_use_switch_saves(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    screen, context = _screen(env, qtbot)
    card = screen.cards["claude"]
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(card.use_switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("engine.claude") == "off"
    assert card.badge.text() == "Off"


def test_blocking_causes_show_an_alert(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    cause = "/etc/codex/config.toml sets sandbox_mode or cannot be read"
    sandbox.save_check(_check("codex", ok=False, causes=(cause,)), env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    alert = screen.cards["codex"].cause_alert
    assert alert is not None
    assert alert.tone() == "danger"
    assert "Remove the sandbox_mode key" in alert.text_label.text()


def test_failed_status_read_offers_try_again(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read(_env: object, *, refresh: bool = False) -> dict[str, status.EngineStatus]:
        raise OSError("disk gone")

    monkeypatch.setattr(status, "read", read)
    screen, context = _screen(env, qtbot)
    with qtbot.waitSignal(context.engines_failed, timeout=5000):
        screen.shown()
    assert screen.failure is not None
    assert [b.text() for b in screen.failure.buttons] == ["Try again"]


# Model lists


def _codex_list() -> models.ModelList:
    return models.ModelList(
        models=(
            models.Model("gpt-5", "GPT-5", ("low", "medium", "high"), "medium"),
            models.Model("gpt-mini", "GPT mini", (), None),
        ),
        source=models.CLI,
        fetched_at=NOW,
        reason=None,
    )


def _router_list() -> models.ModelList:
    return models.ModelList(
        models=(
            models.Model(
                "acme/big",
                "Big",
                ("low", "high"),
                None,
                1_000_000,
                0.000002,
                0.000008,
                True,
            ),
            models.Model("acme/small", "Small", (), None, 32_768, 0.0000001, 0, True),
            models.Model(
                "other/chat", "Chat", (), None, 128_000, 0.0000004, 0.000001, False
            ),
        ),
        source=models.OPENROUTER,
        fetched_at=NOW,
        reason=None,
    )


def _open(screen: EnginesScreen, context: AppContext, qtbot: QtBot, engine: str) -> Any:  # noqa: ANN401
    qtbot.mouseClick(screen.cards[engine].models_button, Qt.MouseButton.LeftButton)
    panel = screen.panel
    assert panel is not None
    qtbot.waitUntil(lambda: panel.models is not None or panel.state.error.isVisible())
    return panel


def test_models_open_one_at_a_time(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list(), "opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    _open(screen, context, qtbot, "codex")
    assert screen.open_engine == "codex"
    assert screen.cards["codex"].models_button.isChecked()
    _open(screen, context, qtbot, "opencode")
    assert screen.open_engine == "opencode"
    assert not screen.cards["codex"].models_button.isChecked()
    qtbot.mouseClick(screen.cards["opencode"].models_button, Qt.MouseButton.LeftButton)
    assert screen.panel is None
    assert screen.open_engine is None


def test_codex_shortlist_and_effort(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    row = panel.rows[0]
    assert row.effort() == "medium"
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.codex") == ["gpt-5@medium"]
    assert screen.cards["codex"].shortlisted_label.text() == "1 shortlisted"
    index = row.picker.findData("high")

    def pick() -> None:
        row.picker.setCurrentIndex(index)
        row.picker.activated.emit(index)

    _save(context, qtbot, pick)
    assert context.setting("shortlist.codex") == ["gpt-5@high"]
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.codex") == []


def test_effort_change_off_the_shortlist_saves_nothing(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    row = panel.rows[0]
    generation = context.settings()["generation"]
    index = row.picker.findData("low")
    row.picker.setCurrentIndex(index)
    row.picker.activated.emit(index)
    context.tasks.wait(5000)
    QApplication.processEvents()
    assert context.settings()["generation"] == generation
    assert row.effort() == "low"


def test_model_without_effort_cannot_be_shortlisted(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    row = panel.rows[1]
    assert row.picker is None
    qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton)
    assert not row.switch.isChecked()
    assert row.note.text() == NO_EFFORT_NOTE
    assert context.setting("shortlist.codex") == []


def test_claude_shortlist_entry(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {
        "claude": models.ModelList(
            models=tuple(
                models.Model(name, name, models.CLAUDE_EFFORTS)
                for name in models.CLAUDE_MODELS
            ),
            source=models.CLI,
            fetched_at=NOW,
            reason=None,
        )
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "claude")
    row = next(r for r in panel.rows if r.model.id == "opus")
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.claude") == ["opus@low"]


def test_opencode_entries_with_and_without_effort(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    big, small = panel.rows[0], panel.rows[1]
    assert big.effort() is None
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(small.switch, Qt.MouseButton.LeftButton),
    )
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(big.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.opencode") == ["acme/small", "acme/big"]
    index = big.picker.findData("high")

    def pick() -> None:
        big.picker.setCurrentIndex(index)
        big.picker.activated.emit(index)

    _save(context, qtbot, pick)
    assert context.setting("shortlist.opencode") == ["acme/small", "acme/big@high"]


def test_opencode_filters_and_search(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")

    def shown() -> list[str]:
        return [row.model.id for row in panel.rows if not row.isHidden()]

    assert shown() == ["acme/big", "acme/small"]
    assert panel.count_label.text() == "2 of 3 models"
    qtbot.mouseClick(panel.tools_switch, Qt.MouseButton.LeftButton)
    assert shown() == ["acme/big", "acme/small", "other/chat"]
    panel.price_picker.set_value("0.5")
    assert shown() == ["acme/small", "other/chat"]
    panel.context_picker.set_value(64_000)
    assert shown() == ["other/chat"]
    panel.context_picker.set_value(None)
    panel.search.setText("SMALL")
    assert shown() == ["acme/small"]
    assert panel.count_label.text() == "1 of 3 models"
    panel.search.setText("nothing")
    assert shown() == []
    assert not panel.state.empty.isHidden()
    assert panel.state.empty_title.text() == "No models match"


def test_saved_entry_missing_from_the_list_can_be_removed(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"shortlist.codex": ["gone@high", "gpt-5@low"]}, env=env)
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    assert panel.rows[0].switch.isChecked()
    assert panel.rows[0].effort() == "low"
    assert [row.model_id for row in panel.missing] == ["gone"]
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(
            panel.missing[0].remove_button, Qt.MouseButton.LeftButton
        ),
    )
    assert context.setting("shortlist.codex") == ["gpt-5@low"]
    assert panel.missing == []


def test_removing_a_menu_model_keeps_the_menu_value(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings(
        {"shortlist.codex": ["gpt-5@low"], "single.writer": "codex:gpt-5"}, env=env
    )
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(panel.rows[0].switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.codex") == []
    assert context.setting("single.writer") == "codex:gpt-5"


def test_list_with_a_reason_keeps_cached_rows(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    held = _codex_list()
    stubs["lists"] = {
        "codex": models.ModelList(held.models, models.CACHE, NOW, "codex exited 1")
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    assert len(panel.rows) == 2
    assert panel.notice is not None
    assert [b.text() for b in panel.notice.buttons] == ["Try again"]
    stubs["lists"] = {"codex": held}
    qtbot.mouseClick(panel.notice.buttons[0], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(stubs["list_calls"]) == 2, timeout=5000)
    context.tasks.wait(5000)
    QApplication.processEvents()
    assert stubs["list_calls"][-1] == ("codex", True)
    assert panel.notice is None


def test_unreadable_list_shows_error_with_try_again(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {
        "opencode": models.ModelList((), models.OPENROUTER, None, "no network")
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    assert "no network" in panel.state.error_text.text()
    assert not panel.state.retry_button.isHidden()
    stubs["lists"] = {"opencode": _router_list()}
    qtbot.mouseClick(panel.state.retry_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(panel.rows) == 3, timeout=5000)
    assert stubs["list_calls"][-1] == ("opencode", True)


def test_unreadable_list_lists_no_shortlist_entry_as_missing(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings(
        {"shortlist.opencode": ["openai/gpt-5@high", "anthropic/claude-x"]}, env=env
    )
    stubs["lists"] = {
        "opencode": models.ModelList((), models.OPENROUTER, None, "no network")
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    assert "no network" in panel.state.error_text.text()
    assert panel.missing == []
    assert panel.missing_widget.isHidden()
    stubs["lists"] = {"opencode": _router_list()}
    qtbot.mouseClick(panel.state.retry_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(panel.rows) == 3, timeout=5000)
    assert sorted(row.model_id for row in panel.missing) == [
        "anthropic/claude-x",
        "openai/gpt-5",
    ]


def test_refresh_list_reads_again(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    assert stubs["list_calls"] == [("codex", False)]
    qtbot.mouseClick(panel.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(stubs["list_calls"]) == 2, timeout=5000)
    assert stubs["list_calls"][-1] == ("codex", True)


def test_stale_save_reloads_the_shortlist(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    choices.set_settings({"shortlist.codex": ["gpt-5@high"]}, env=env)
    row = panel.rows[0]
    assert not row.switch.isChecked()
    with qtbot.waitSignal(context.stale, timeout=5000):
        qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton)
    assert context.setting("shortlist.codex") == ["gpt-5@high"]
    assert row.switch.isChecked()
    assert row.effort() == "high"


def test_refused_save_leaves_the_switch_as_saved(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise choices.InvalidChoiceError("refused")

    monkeypatch.setattr(choices, "set_settings", refuse)
    row = panel.rows[0]
    with qtbot.waitSignal(context.error, timeout=5000) as blocker:
        qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton)
    assert blocker.args == ["The setting was not saved: refused"]
    assert not row.switch.isChecked()


# Keyboard


def test_every_control_is_reached_with_tab(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["engines"] = _all(codex=status.NOT_SIGNED_IN)
    stubs["lists"] = {"opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    screen.resize(1100, 720)
    screen.show()
    qtbot.waitExposed(screen)
    _shown(screen, context, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    expected: list[QWidget] = [screen.check_button]
    for card in screen.cards.values():
        expected += [card.use_switch, card.sandbox_switch]
        for line in card.commands:
            expected += [line.copy_button, line.run_button]
        expected.append(card.models_button)
    expected += [
        panel.refresh_button,
        panel.search,
        panel.tools_switch,
        panel.price_picker,
        panel.context_picker,
    ]
    for row in panel.rows:
        if not row.isHidden():
            expected += row.focus_order()
    screen.activateWindow()
    screen.check_button.setFocus(Qt.FocusReason.TabFocusReason)
    reached: list[QWidget] = []
    for _ in range(len(expected) + 5):
        widget = QApplication.focusWidget()
        if widget is not None and widget not in reached:
            reached.append(widget)
        screen.focusNextChild()
    assert [w for w in reached if w in expected] == expected
