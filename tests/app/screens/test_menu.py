import threading
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from pytestqt.qtbot import QtBot

from ember import choices, db
from ember.app.context import AppContext
from ember.app.screens import menu
from ember.app.screens.menu import MenuScreen
from ember.app.widgets import Card, Picker
from ember.engines import status


def _ready(env: dict[str, str], qtbot: QtBot) -> AppContext:
    context = AppContext(env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    return context


def _screen(env: dict[str, str], qtbot: QtBot) -> tuple[AppContext, MenuScreen]:
    context = _ready(env, qtbot)
    screen = MenuScreen(context)
    qtbot.addWidget(screen)
    return context, screen


def _items(picker: Picker) -> list[tuple[str, Any, bool]]:
    return [
        (
            picker.itemText(row),
            picker.itemData(row, Qt.ItemDataRole.UserRole),
            picker.is_header(row),
        )
        for row in range(picker.count())
    ]


def _pick(context: AppContext, picker: Picker, value: str, qtbot: QtBot) -> None:
    row = next(
        r
        for r in range(picker.count())
        if picker.itemData(r, Qt.ItemDataRole.UserRole) == value
    )
    picker.setCurrentIndex(row)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        picker.activated.emit(row)


def _stored(env: dict[str, str], name: str) -> Any:  # noqa: ANN401 - a setting
    return choices.snapshot(env=env)["answers"][name]["value"]


SHORTLISTS: dict[str, object] = {
    "shortlist.codex": ["gpt-5@high"],
    "shortlist.opencode": ["openrouter/qwen3-coder"],
    "shortlist.claude": ["opus@medium", "sonnet@low"],
}


def test_empty_shortlists_offer_ask_and_open_engines(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context, screen = _screen(env, qtbot)
    for controls in screen.roles.values():
        assert _items(controls.picker) == [("Ask each time", "ask", False)]
        assert controls.picker.value() == "ask"
        assert not controls.warning.isVisibleTo(screen)
        assert not controls.run_as_row.isVisibleTo(screen)
    assert screen.empty.isVisibleTo(screen)
    assert screen.empty_line.text() == "Shortlist models in Engines to offer them here."
    with qtbot.waitSignal(context.screen_requested) as blocker:
        screen.open_engines.click()
    assert blocker.args == ["engines"]


def test_groups_list_shortlisted_models_of_engines_that_are_on(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    _context, screen = _screen(env, qtbot)
    picker = screen.roles["writer"].picker
    assert _items(picker) == [
        ("Ask each time", "ask", False),
        ("Codex", None, True),
        ("gpt-5 · high", "codex:gpt-5", False),
        ("OpenCode", None, True),
        ("openrouter/qwen3-coder", "opencode:openrouter/qwen3-coder", False),
        ("Claude", None, True),
        ("opus · medium", "claude:opus", False),
        ("sonnet · low", "claude:sonnet", False),
    ]
    assert not screen.empty.isVisibleTo(screen)


def test_headers_are_not_selectable(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    _context, screen = _screen(env, qtbot)
    picker = screen.roles["writer"].picker
    model = picker.model()
    for row in range(picker.count()):
        flags = model.flags(model.index(row, 0))
        selectable = bool(flags & Qt.ItemFlag.ItemIsSelectable)
        assert selectable is not picker.is_header(row)


def test_engines_that_are_off_are_left_out(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings({**SHORTLISTS, "engine.opencode": "off"}, env=env)
    _context, screen = _screen(env, qtbot)
    texts = [text for text, _v, _h in _items(screen.roles["reviewer"].picker)]
    assert "OpenCode" not in texts
    assert "openrouter/qwen3-coder" not in texts
    assert "Codex" in texts


def test_picking_saves_each_role(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    context, screen = _screen(env, qtbot)
    _pick(context, screen.roles["writer"].picker, "codex:gpt-5", qtbot)
    assert _stored(env, "single.writer") == "codex:gpt-5"
    _pick(
        context,
        screen.roles["reviewer"].picker,
        "opencode:openrouter/qwen3-coder",
        qtbot,
    )
    assert _stored(env, "single.reviewer") == "opencode:openrouter/qwen3-coder"
    _pick(context, screen.roles["writer"].picker, "ask", qtbot)
    assert _stored(env, "single.writer") == "ask"


def test_claude_rows_show_only_for_a_claude_model(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    context, screen = _screen(env, qtbot)
    writer = screen.roles["writer"]
    reviewer = screen.roles["reviewer"]
    assert not writer.run_as_row.isVisibleTo(screen)
    _pick(context, writer.picker, "claude:opus", qtbot)
    assert writer.run_as_row.isVisibleTo(screen)
    assert not reviewer.run_as_row.isVisibleTo(screen)
    assert writer.run_as.value() == "cli"
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        writer.run_as.buttons()[1].click()
    assert _stored(env, "single.writer_as") == "subagent"
    assert writer.run_as.value() == "subagent"
    _pick(context, writer.picker, "codex:gpt-5", qtbot)
    assert not writer.run_as_row.isVisibleTo(screen)
    _pick(context, reviewer.picker, "claude:sonnet", qtbot)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        reviewer.run_as.buttons()[1].click()
    assert _stored(env, "single.reviewer_as") == "subagent"


def test_a_value_off_the_shortlist_stays_with_a_warning(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    choices.set_settings({"single.writer": "codex:gpt-5"}, env=env)
    choices.set_settings({"shortlist.codex": []}, env=env)
    _context, screen = _screen(env, qtbot)
    writer = screen.roles["writer"]
    assert writer.picker.value() == "codex:gpt-5"
    assert writer.picker.currentText() == "codex:gpt-5 (not on the shortlist)"
    assert writer.warning.isVisibleTo(screen)
    assert writer.warning.text() == (
        "gpt-5 is not on the Codex shortlist. Shortlist it in Engines or pick "
        "another worker."
    )
    assert not screen.roles["reviewer"].warning.isVisibleTo(screen)


def test_a_value_of_an_engine_that_is_off_names_that_reason(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    choices.set_settings(
        {"single.reviewer": "claude:opus", "engine.claude": "off"}, env=env
    )
    _context, screen = _screen(env, qtbot)
    reviewer = screen.roles["reviewer"]
    assert reviewer.picker.currentText() == "claude:opus (Claude is off)"
    assert reviewer.warning.text() == (
        "Claude is off. Turn it on in Engines or pick another worker."
    )
    assert reviewer.run_as_row.isVisibleTo(screen)


def test_one_time_choices_are_not_shown(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    choices.set_once({"single.writer": "codex:gpt-5"}, env=env)
    _context, screen = _screen(env, qtbot)
    assert screen.roles["writer"].picker.value() == "ask"


def test_stale_save_reloads_the_screen(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    context, screen = _screen(env, qtbot)
    choices.set_settings({"single.reviewer": "codex:gpt-5"}, env=env)
    picker = screen.roles["writer"].picker
    row = next(
        r
        for r in range(picker.count())
        if picker.itemData(r, Qt.ItemDataRole.UserRole) == "claude:opus"
    )
    picker.setCurrentIndex(row)
    with qtbot.waitSignal(context.stale, timeout=5000):
        picker.activated.emit(row)
    assert _stored(env, "single.writer") == "ask"
    assert picker.value() == "ask"
    assert screen.roles["reviewer"].picker.value() == "codex:gpt-5"


def focus_chain(screen: QWidget, qtbot: QtBot, first: QWidget) -> list[QWidget]:
    screen.show()
    qtbot.waitExposed(screen)
    handle = screen.windowHandle()
    assert handle is not None
    handle.requestActivate()
    qtbot.waitUntil(screen.isActiveWindow, timeout=5000)
    first.setFocus(Qt.FocusReason.TabFocusReason)
    seen: list[QWidget] = []
    for _ in range(40):
        widget = QApplication.focusWidget()
        if widget is None or widget in seen:
            break
        seen.append(widget)
        screen.focusNextChild()
    return seen


def test_every_control_is_reached_with_tab(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings(
        {**SHORTLISTS, "single.writer": "claude:opus", "shortlist.codex": []}, env=env
    )
    choices.set_settings({"shortlist.opencode": [], "shortlist.claude": []}, env=env)
    _context, screen = _screen(env, qtbot)
    writer = screen.roles["writer"]
    reviewer = screen.roles["reviewer"]
    chain = focus_chain(screen, qtbot, writer.picker)
    expected = [
        writer.picker,
        writer.run_as.buttons()[0],
        reviewer.picker,
        screen.open_engines,
    ]
    # Tab reaches a segmented control's chosen option; arrows move within it.
    assert [w for w in chain if w in expected] == expected


def _engine_status(engine: str, connected: bool) -> status.EngineStatus:
    return status.EngineStatus(
        engine=engine,
        state=status.CONNECTED if connected else status.NOT_SIGNED_IN,
        reason=None if connected else "not signed in",
        installed=True,
        binary=f"/usr/bin/{engine}",
        version="1.0",
        signed_in=connected,
        connected=connected,
        requirements=(),
        checked_at=0.0,
    )


def test_not_connected_note_follows_the_engine_status_read(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    choices.set_settings({"single.writer": "codex:gpt-5"}, env=env)
    context, screen = _screen(env, qtbot)
    writer = screen.roles["writer"]
    assert not writer.warning.isVisibleTo(screen)
    engines = {
        engine: _engine_status(engine, engine != "codex") for engine in status.ENGINES
    }
    context._engines = engines
    context.engines_changed.emit(engines)
    assert writer.warning.isVisibleTo(screen)
    assert writer.warning.text().startswith("Codex was not connected")
    engines = {engine: _engine_status(engine, True) for engine in status.ENGINES}
    context._engines = engines
    context.engines_changed.emit(engines)
    assert not writer.warning.isVisibleTo(screen)


def test_choosing_the_saved_run_as_saves_nothing(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    choices.set_settings({"single.writer": "claude:opus"}, env=env)
    context, screen = _screen(env, qtbot)
    writer = screen.roles["writer"]
    with qtbot.assertNotEmitted(context.settings_changed, wait=200):
        writer.run_as.changed.emit(context.setting("single.writer_as"))
    assert context.tasks.pending() == 0


def _texts(widget: QWidget) -> list[str]:
    return [
        line.text() for line in widget.findChildren(QLabel) if line.isVisibleTo(widget)
    ]


def _panel(env: dict[str, str], members: list[str]) -> None:
    choices.set_settings(SHORTLISTS, env=env)
    choices.set_settings({"panel.members": members}, env=env)


def test_cards_and_subtitle(env: dict[str, str], qtbot: QtBot) -> None:
    _context, screen = _screen(env, qtbot)
    assert [_texts(card)[0] for card in screen.findChildren(Card)] == [
        "Single tasks",
        "Planning",
        "Work of several tasks",
        "Flows",
        "Claude Workflows",
        "Panel",
    ]
    assert menu.SUBTITLE in _texts(screen)
    assert menu.MULTI_HELP in _texts(screen)
    assert menu.PANEL_HELP in _texts(screen)
    assert (
        "Reads the writer's branch and reports. Never edits. "
        "Used for read-only single runs, and by the Gap check reviewer and "
        "Flow reviewer while they are Same as Reviewer."
    ) in _texts(screen)
    assert screen.multi.accessibleName() == "Multi-task work"
    assert screen.add_member.accessibleName() == "Add panel member"


@pytest.mark.parametrize("value", ["1", "3", "4"])
def test_review_rounds_show_saved_value_and_save_only_changes(
    env: dict[str, str], qtbot: QtBot, value: str
) -> None:
    choices.set_settings({"flow.review_rounds": value}, env=env)
    context, screen = _screen(env, qtbot)
    assert screen.rounds.accessibleName() == "Max review rounds"
    assert screen.rounds.value() == value
    assert [label for _, label in menu.ROUNDS] == ["1", "2", "3", "4"]
    generation = choices.snapshot(env=env)["generation"]
    with qtbot.assertNotEmitted(context.settings_changed, wait=50):
        screen.rounds.changed.emit(value)
    assert choices.snapshot(env=env)["generation"] == generation
    target = "2" if value != "2" else "1"
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.rounds.buttons()[int(target) - 1].click()
    assert _stored(env, "flow.review_rounds") == target
    assert screen.rounds.value() == target
    assert "Max review rounds" in _texts(screen)


def test_review_rounds_default_to_three(env: dict[str, str], qtbot: QtBot) -> None:
    _context, screen = _screen(env, qtbot)
    assert screen.rounds.value() == "3"


@pytest.mark.parametrize("value", ["ask", "flow", "workflow"])
def test_multi_shows_saved_value_and_saves_only_changes(
    env: dict[str, str], qtbot: QtBot, value: str
) -> None:
    choices.set_settings({"multi.runs_as": value}, env=env)
    context, screen = _screen(env, qtbot)
    assert screen.multi.value() == value
    generation = choices.snapshot(env=env)["generation"]
    with qtbot.assertNotEmitted(context.settings_changed, wait=50):
        screen.multi.changed.emit(value)
    assert choices.snapshot(env=env)["generation"] == generation
    target = "workflow" if value == "flow" else "flow"
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.multi.buttons()[[v for v, _ in menu.MULTI].index(target)].click()
    assert _stored(env, "multi.runs_as") == target
    assert screen.multi.value() == target
    # Multi-task work remains usable without any shortlisted model.
    assert screen.empty.isVisibleTo(screen)
    assert not screen.add_member.isEnabled()


def test_panel_empty_add_choices_and_save(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings({**SHORTLISTS, "engine.opencode": "off"}, env=env)
    context, screen = _screen(env, qtbot)
    assert menu.NO_MEMBERS in _texts(screen)
    assert _items(screen.add_member) == [
        (menu.ADD_MEMBER, None, False),
        ("Codex", None, True),
        ("gpt-5 · high", "codex:gpt-5", False),
        ("Claude", None, True),
        ("opus · medium", "claude:opus", False),
        ("sonnet · low", "claude:sonnet", False),
    ]
    _pick(context, screen.add_member, "codex:gpt-5", qtbot)
    assert _stored(env, "panel.members") == ["codex:gpt-5"]
    assert screen.add_member.currentText() == menu.ADD_MEMBER
    assert screen.add_member.value() is None
    assert menu.NO_MEMBERS not in _texts(screen)
    assert screen.add_member.isEnabled()


def test_panel_member_order_removal_and_excluded_options(
    env: dict[str, str], qtbot: QtBot
) -> None:
    members = ["claude:opus", "codex:gpt-5"]
    _panel(env, members)
    context, screen = _screen(env, qtbot)
    assert list(screen.member_removes) == members
    assert "Claude · opus · medium" in _texts(screen)
    assert "Codex · gpt-5 · high" in _texts(screen)
    assert all(v not in members for _, v, _ in _items(screen.add_member))
    remove = screen.member_removes[members[0]]
    assert remove.accessibleName() == "Remove Claude · opus · medium"
    assert not remove.icon().isNull()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        remove.click()
    assert _stored(env, "panel.members") == members[1:]
    assert list(screen.member_removes) == members[1:]


def test_panel_at_limit(env: dict[str, str], qtbot: QtBot) -> None:
    models = [f"model-{n}@high" for n in range(9)]
    members = [f"codex:model-{n}" for n in range(8)]
    choices.set_settings({"shortlist.codex": models}, env=env)
    choices.set_settings({"panel.members": members}, env=env)
    context, screen = _screen(env, qtbot)
    assert not screen.add_member.isEnabled()
    assert screen.full.isVisibleTo(screen)
    assert screen.full.text() == menu.FULL
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.member_removes[members[0]].click()
    assert screen.add_member.isEnabled()
    assert not screen.full.isVisibleTo(screen)


def test_panel_engine_off_warns_but_does_not_block_add(
    env: dict[str, str], qtbot: QtBot
) -> None:
    _panel(env, ["codex:gpt-5"])
    choices.set_settings({"engine.codex": "off"}, env=env)
    context, screen = _screen(env, qtbot)
    assert "Codex is off. Turn it on in Engines or remove this member." in _texts(
        screen
    )
    assert "codex:gpt-5" in _texts(screen)
    assert screen.add_member.isEnabled()
    _pick(context, screen.add_member, "claude:opus", qtbot)
    assert _stored(env, "panel.members") == ["codex:gpt-5", "claude:opus"]


@pytest.mark.parametrize("engine_off", [False, True])
def test_panel_off_shortlist_blocks_add_but_can_be_removed(
    env: dict[str, str], qtbot: QtBot, engine_off: bool
) -> None:
    _panel(env, ["codex:gpt-5", "claude:opus"])
    choices.set_settings(
        {"shortlist.codex": [], "engine.codex": "off" if engine_off else "on"},
        env=env,
    )
    context, screen = _screen(env, qtbot)
    assert not screen.add_member.isEnabled()
    assert screen.add_warning.text() == menu.ADD_BLOCKED
    if not engine_off:
        assert (
            "gpt-5 is not on the Codex shortlist. Shortlist it in Engines "
            "or remove this member."
        ) in _texts(screen)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.member_removes["codex:gpt-5"].click()
    assert _stored(env, "panel.members") == ["claude:opus"]
    assert screen.add_member.isEnabled()


@pytest.mark.parametrize("two_blockers", [False, True])
def test_blocked_removal_offers_remove_all_even_with_one_remaining_blocker(
    env: dict[str, str], qtbot: QtBot, two_blockers: bool
) -> None:
    _panel(env, ["claude:opus", "codex:gpt-5"])
    changes: dict[str, object] = {"shortlist.codex": []}
    if two_blockers:
        changes["shortlist.claude"] = []
    choices.set_settings(changes, env=env)
    context, screen = _screen(env, qtbot)
    assert not screen.remove_all.isVisibleTo(screen)
    generation = choices.snapshot(env=env)["generation"]
    with qtbot.assertNotEmitted(context.settings_changed, wait=50):
        screen.member_removes["claude:opus"].click()
    assert choices.snapshot(env=env)["generation"] == generation
    assert screen.remove_warning.text() == (
        "Shortlist gpt-5 in Engines, or remove every member not on its shortlist."
    )
    assert screen.remove_all.isVisibleTo(screen)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        screen.remove_all.click()
    assert _stored(env, "panel.members") == []
    assert not screen.remove_all.isVisibleTo(screen)


def test_panel_status_recomputes_warning(env: dict[str, str], qtbot: QtBot) -> None:
    _panel(env, ["codex:gpt-5"])
    context, screen = _screen(env, qtbot)
    engines = {e: _engine_status(e, e != "codex") for e in status.ENGINES}
    context._engines = engines
    context.engines_changed.emit(engines)
    warning = (
        "Codex was not connected when last checked. Set it up in Engines "
        "or remove this member."
    )
    assert warning in _texts(screen)
    assert screen.add_member.isEnabled()
    engines["codex"] = _engine_status("codex", True)
    context.engines_changed.emit(engines)
    assert warning not in _texts(screen)
    assert "Codex · gpt-5 · high" in _texts(screen)


@pytest.mark.parametrize("action", ["add", "remove"])
def test_panel_stale_save_refreshes_and_unlocks(
    env: dict[str, str], qtbot: QtBot, action: str
) -> None:
    _panel(env, ["codex:gpt-5"])
    context, screen = _screen(env, qtbot)
    choices.set_settings({"panel.members": ["claude:opus"]}, env=env)
    with qtbot.waitSignal(context.stale, timeout=5000):
        if action == "add":
            screen.add_member.set_value("claude:sonnet")
            screen.add_member.activated.emit(screen.add_member.currentIndex())
        else:
            screen.member_removes["codex:gpt-5"].click()
    assert _stored(env, "panel.members") == ["claude:opus"]
    assert list(screen.member_removes) == ["claude:opus"]
    assert screen.add_member.isEnabled()
    assert screen.member_removes["claude:opus"].isEnabled()


@pytest.mark.parametrize("action", ["add", "remove"])
def test_panel_edits_wait_for_save_and_refresh(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    _panel(env, ["codex:gpt-5", "claude:opus"])
    context, screen = _screen(env, qtbot)
    started, release = threading.Event(), threading.Event()
    snapshot = choices.snapshot

    def delayed_snapshot(*, env: dict[str, str]) -> dict[str, Any]:
        started.set()
        assert release.wait(5)
        return snapshot(env=env)

    monkeypatch.setattr(choices, "snapshot", delayed_snapshot)
    try:
        if action == "add":
            screen.add_member.set_value("claude:sonnet")
            screen.add_member.activated.emit(screen.add_member.currentIndex())
            expected = ["codex:gpt-5", "claude:opus", "claude:sonnet"]
        else:
            screen.member_removes["codex:gpt-5"].click()
            expected = ["claude:opus"]
        qtbot.waitUntil(started.is_set, timeout=5000)
        # The write has finished, but its refreshed snapshot is still pending.
        assert snapshot(env=env)["answers"]["panel.members"]["value"] == expected
        context.engines_changed.emit({})
        assert not screen.add_member.isEnabled()
        assert not screen.remove_all.isEnabled()
        assert all(not b.isEnabled() for b in screen.member_removes.values())
        # Even an already queued activation cannot submit another list edit.
        screen.add_member.set_value("opencode:openrouter/qwen3-coder")
        screen.add_member.activated.emit(screen.add_member.currentIndex())
        screen.member_removes["claude:opus"].clicked.emit()
        screen.remove_all.clicked.emit()
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            release.set()
    finally:
        release.set()
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    assert _stored(env, "panel.members") == expected
    assert list(screen.member_removes) == expected
    assert screen.add_member.isEnabled()
    assert all(b.isEnabled() for b in screen.member_removes.values())


def test_panel_failed_save_refreshes_and_unlocks(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    _panel(env, ["codex:gpt-5"])
    context, screen = _screen(env, qtbot)

    def fail(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise OSError("test save failure")

    monkeypatch.setattr(choices, "set_settings", fail)
    with qtbot.waitSignal(context.error, timeout=5000):
        screen.member_removes["codex:gpt-5"].click()
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    assert _stored(env, "panel.members") == ["codex:gpt-5"]
    assert screen.add_member.isEnabled()
    assert screen.member_removes["codex:gpt-5"].isEnabled()


@pytest.mark.parametrize("panel_fails", [False, True])
def test_panel_queued_behind_failed_save_stays_locked_until_its_own_completion(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    panel_fails: bool,
) -> None:
    _panel(env, ["codex:gpt-5", "claude:opus"])
    context, screen = _screen(env, qtbot)
    first_started, first_release = threading.Event(), threading.Event()
    panel_started, panel_release = threading.Event(), threading.Event()
    set_settings = choices.set_settings
    reload_settings = context.reload_settings
    reloads: list[Callable[[], None] | None] = []
    writes: list[dict[str, object]] = []

    def reload(done: Callable[[], None] | None = None) -> None:
        reloads.append(done)
        reload_settings(done)

    def save(
        values: Mapping[str, object],
        expect_generation: int | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        writes.append(dict(values))
        if "multi.runs_as" in values:
            first_started.set()
            assert first_release.wait(5)
            raise db.DatabaseBusyError("test earlier save is busy")
        panel_started.set()
        assert panel_release.wait(5)
        if panel_fails:
            raise db.DatabaseBusyError("test Panel save is busy")
        return set_settings(values, expect_generation=expect_generation, env=env)

    monkeypatch.setattr(choices, "set_settings", save)
    monkeypatch.setattr(context, "reload_settings", reload)
    try:
        screen.multi.buttons()[1].click()
        qtbot.waitUntil(first_started.is_set)
        screen.member_removes["codex:gpt-5"].click()
        with qtbot.waitSignal(context.error, timeout=5000):
            first_release.set()
        qtbot.waitUntil(panel_started.is_set)
        assert reloads == []
        context.error.emit("Ember could not open a terminal.")
        context.error.emit("The setting was not saved: another save failed.")
        context.stale.emit()
        context.settings_failed.emit("An unrelated settings read failed.")
        assert reloads == []
        assert not screen.add_member.isEnabled()
        assert not screen._panel_refresh_failed
        # A read can complete while the queued removal is still running.
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            context.reload_settings()
        assert _stored(env, "panel.members") == ["codex:gpt-5", "claude:opus"]
        assert not screen.add_member.isEnabled()
        assert not screen.remove_all.isEnabled()
        assert all(not b.isEnabled() for b in screen.member_removes.values())
        screen.add_member.set_value("claude:sonnet")
        screen.add_member.activated.emit(screen.add_member.currentIndex())
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            panel_release.set()
    finally:
        first_release.set()
        panel_release.set()
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    assert reloads == ([None, screen._panel_done] if panel_fails else [None])
    assert writes == [
        {"multi.runs_as": "flow"},
        {"panel.members": ["claude:opus"]},
    ]
    expected = ["codex:gpt-5", "claude:opus"] if panel_fails else ["claude:opus"]
    assert _stored(env, "panel.members") == expected
    assert list(screen.member_removes) == expected
    assert screen.add_member.isEnabled()
    assert all(b.isEnabled() for b in screen.member_removes.values())
    monkeypatch.setattr(choices, "set_settings", set_settings)
    _pick(context, screen.add_member, "claude:sonnet", qtbot)
    assert _stored(env, "panel.members") == [*expected, "claude:sonnet"]


@pytest.mark.parametrize("reload_succeeds", [False, True])
def test_panel_dropped_by_stale_save_waits_for_its_result_after_reload(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    reload_succeeds: bool,
) -> None:
    """A dropped Panel save unlocks only after its stale reload succeeds."""
    _panel(env, ["codex:gpt-5", "claude:opus"])
    context, screen = _screen(env, qtbot)
    choices.set_settings({"panel.members": ["claude:sonnet"]}, env=env)
    save_started, save_release = threading.Event(), threading.Event()
    read_started, read_release = threading.Event(), threading.Event()
    set_settings, snapshot = choices.set_settings, choices.snapshot
    writes: list[dict[str, object]] = []
    reloads: list[Callable[[], None] | None] = []
    reload_settings = context.reload_settings

    def save(
        values: Mapping[str, object],
        expect_generation: int | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Hold the sent save until the Panel save is queued behind it."""
        writes.append(dict(values))
        save_started.set()
        assert save_release.wait(5)
        return set_settings(values, expect_generation=expect_generation, env=env)

    def read(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Hold the stale reload and deliver its selected outcome."""
        read_started.set()
        assert read_release.wait(5)
        if not reload_succeeds:
            raise OSError("reload failed")
        return snapshot(env=env)

    def reload(done: Callable[[], None] | None = None) -> None:
        """Record refreshes requested through the context's public method."""
        reloads.append(done)
        reload_settings(done)

    monkeypatch.setattr(choices, "set_settings", save)
    monkeypatch.setattr(choices, "snapshot", read)
    monkeypatch.setattr(context, "reload_settings", reload)
    try:
        screen.multi.buttons()[1].click()
        qtbot.waitUntil(save_started.is_set)
        screen.member_removes["codex:gpt-5"].click()
        save_release.set()
        qtbot.waitUntil(read_started.is_set)
        context.error.emit("The setting was not saved: another save failed.")
        context.stale.emit()
        assert not screen.add_member.isEnabled()
        assert all(not b.isEnabled() for b in screen.member_removes.values())
        assert reloads == []
        signal = context.stale if reload_succeeds else context.settings_failed
        with qtbot.waitSignal(signal, timeout=5000):
            read_release.set()
        qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    finally:
        save_release.set()
        read_release.set()
    assert writes == [{"multi.runs_as": "flow"}]
    assert reloads == []
    if not reload_succeeds:
        assert not screen.add_member.isEnabled()
        assert screen._panel_pending
        assert screen._panel_refresh_failed
        monkeypatch.setattr(choices, "snapshot", snapshot)
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            context.reload_settings()
        qtbot.waitUntil(lambda: context.tasks.pending() == 0)
        assert reloads == [None, screen._panel_done]
    assert list(screen.member_removes) == ["claude:sonnet"]
    assert screen.add_member.isEnabled()
    assert screen.member_removes["claude:sonnet"].isEnabled()
    assert not screen._panel_pending


@pytest.mark.parametrize("write_succeeds", [False, True])
def test_panel_failed_save_and_failed_refresh_stay_locked_until_refresh_succeeds(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    write_succeeds: bool,
) -> None:
    """The Panel stays locked after its own refresh fails following an error."""
    _panel(env, ["codex:gpt-5", "claude:opus"])
    context, screen = _screen(env, qtbot)
    snapshot = choices.snapshot

    def fail_read(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Fail the save's snapshot and the Panel's settings refresh."""
        raise OSError("snapshot failed")

    def fail_write(*_args: object, **_kwargs: object) -> dict[str, Any]:
        """Fail the Panel's settings write."""
        raise OSError("write failed")

    monkeypatch.setattr(choices, "snapshot", fail_read)
    if not write_succeeds:
        monkeypatch.setattr(choices, "set_settings", fail_write)
    with qtbot.waitSignal(context.settings_failed, timeout=5000):
        screen.member_removes["codex:gpt-5"].click()
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    assert screen._panel_pending
    assert screen._panel_refresh_failed
    assert not screen.add_member.isEnabled()
    assert all(not b.isEnabled() for b in screen.member_removes.values())
    monkeypatch.setattr(choices, "snapshot", snapshot)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    expected = ["claude:opus"] if write_succeeds else ["codex:gpt-5", "claude:opus"]
    assert list(screen.member_removes) == expected
    assert screen.add_member.isEnabled()
    assert all(b.isEnabled() for b in screen.member_removes.values())
    assert not screen._panel_pending


def test_menu_none_uses_answers_and_snapshot_rows_until_status_read(
    env: dict[str, str], qtbot: QtBot
) -> None:
    _panel(env, ["codex:gpt-5"])
    context = _ready(env, qtbot)
    snapshot = context.settings()
    answers = snapshot["answers"]
    snapshot["menu"]["panel.members"] = choices.panel_row(answers, {"codex": False})
    screen = MenuScreen(context)
    qtbot.addWidget(screen)
    assert any("Codex was not connected" in text for text in _texts(screen))
    snapshot["menu"] = None
    context.settings_changed.emit(snapshot)
    assert "Codex · gpt-5 · high" in _texts(screen)
    assert screen.multi.value() == "ask"


def test_panel_tab_order_after_redraw_and_blocked_removal(
    env: dict[str, str], qtbot: QtBot
) -> None:
    _panel(env, ["codex:gpt-5", "claude:opus"])
    choices.set_settings(
        {"single.writer": "claude:opus", "single.reviewer": "claude:sonnet"}, env=env
    )
    context, screen = _screen(env, qtbot)
    writer, reviewer = screen.roles["writer"], screen.roles["reviewer"]
    expected: list[QWidget] = [
        writer.picker,
        writer.run_as.buttons()[0],
        reviewer.picker,
        reviewer.run_as.buttons()[0],
        screen.multi.buttons()[0],
        *screen.member_removes.values(),
        screen.add_member,
    ]
    chain = focus_chain(screen, qtbot, writer.picker)
    assert [w for w in chain if w in expected] == expected
    choices.set_settings({"shortlist.codex": []}, env=env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    screen.member_removes["claude:opus"].click()
    qtbot.waitUntil(lambda: all(b.isVisible() for b in screen.member_removes.values()))
    expected = [
        writer.picker,
        writer.run_as.buttons()[0],
        reviewer.picker,
        reviewer.run_as.buttons()[0],
        screen.multi.buttons()[0],
        *screen.member_removes.values(),
        screen.remove_all,
    ]
    chain = focus_chain(screen, qtbot, writer.picker)
    assert [w for w in chain if w in expected] == expected


def test_member_helpers_and_shortlist_checks(env: dict[str, str]) -> None:
    _panel(env, ["codex:gpt-5"])
    answers = choices.snapshot(env=env)["answers"]
    row = choices.panel_row(answers)["members"][0]
    assert menu.member_text(row) == "Codex · gpt-5 · high"
    assert menu.member_warning(row) is None
    row.update(engine="opencode", model="example/model", effort=None)
    assert menu.member_text(row) == "OpenCode · example/model"
    row.update(state="invalid", value="bad")
    assert menu.member_text(row) == "bad"
    assert (
        menu.member_warning(row)
        == "bad is not a worker Ember knows. Remove this member."
    )
    assert menu.blocks_save(answers, ["codex:gpt-5", "claude:missing"]) == [
        "claude:missing"
    ]
    answers["engine.claude"]["value"] = "off"
    assert menu.blocks_save(answers, ["claude:opus", "claude:missing"]) == [
        "claude:missing"
    ]
    assert menu.addable(answers, ["codex:gpt-5"]) == [
        ("opencode", [("openrouter/qwen3-coder", None)])
    ]
    assert menu.blocks_save(answers, []) == []


def test_panel_stale_save_with_failed_reload_unlocks_after_a_read(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    _panel(env, ["codex:gpt-5", "claude:opus"])
    context, screen = _screen(env, qtbot)
    choices.set_settings({"panel.members": ["claude:opus"]}, env=env)
    snapshot = choices.snapshot
    failures = [db.DatabaseBusyError("test settings read is busy")]

    def read(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        if failures:
            raise failures.pop()
        return snapshot(env=env)

    monkeypatch.setattr(choices, "snapshot", read)
    with qtbot.waitSignal(context.settings_failed, timeout=5000):
        screen.member_removes["codex:gpt-5"].click()
    assert not screen.add_member.isEnabled()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    qtbot.waitUntil(lambda: not screen._panel_pending)
    assert list(screen.member_removes) == ["claude:opus"]
    assert screen.add_member.isEnabled()
    assert screen.member_removes["claude:opus"].isEnabled()


def test_planning_rows_show_and_save_the_settings(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings({"gapcheck.when": "5", "gapcheck.ask": "medium"}, env=env)
    context, screen = _screen(env, qtbot)
    assert screen.when.value() == "5"
    assert screen.when_row.help.text() == (
        "Plans of 5 or more tasks. Plans of 1 to 4 tasks start without one."
    )
    assert screen.asks.value() == "medium"
    assert screen.derived["gapcheck.reviewer"].picker.value() == "same"
    assert screen.derived["gapcheck.reviewer"].picker.currentText() == (
        "Same as Reviewer"
    )
    assert screen.when_row.info.toolTip() == menu.WHEN_TIP
    assert screen.when.toolTip() == ""
    saved: list[dict[str, object]] = []
    context.save_settings = lambda values, **_kwargs: saved.append(values)  # type: ignore[method-assign]
    screen.when.set_value("off")
    screen._when_picked(0)
    assert saved == [{"gapcheck.when": "off"}]
    assert screen.when_row.help.text().startswith("No gap check.")


@pytest.mark.parametrize(
    ("runs_as", "flows", "workflows"),
    [("flow", True, False), ("workflow", False, True), ("ask", True, True)],
)
def test_flow_and_workflow_cards_follow_multi_task_work(
    env: dict[str, str], qtbot: QtBot, runs_as: str, flows: bool, workflows: bool
) -> None:
    choices.set_settings({"multi.runs_as": runs_as}, env=env)
    _context, screen = _screen(env, qtbot)
    screen.show()
    qtbot.waitUntil(lambda: screen.flow_card.isVisible() == flows, timeout=2000)
    assert screen.workflow_card.isVisible() == workflows


def test_flow_settings_show_their_values(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings(
        {
            "flow.parallel": "2",
            "flow.stall": "20",
            "flow.whole_review": "off",
            "workflow.agents": "16",
            "workflow.late": "stop",
        },
        env=env,
    )
    _context, screen = _screen(env, qtbot)
    assert screen.segments["flow.parallel"].value() == "2"
    assert screen.segments["flow.stall"].value() == "20"
    assert not screen.whole.isChecked()
    assert screen.segments["workflow.agents"].value() == "16"
    assert screen.segments["workflow.late"].value() == "stop"
    assert screen.derived["flow.writer"].picker.currentText() == "Same as Writer"


def test_limit_settings_name_themselves_as_maximums(
    env: dict[str, str], qtbot: QtBot
) -> None:
    _context, screen = _screen(env, qtbot)
    texts = [label.text() for label in screen.findChildren(QLabel)]
    for name in ("Max tasks at once", "Max agents at once", "Max review rounds"):
        assert name in texts
    assert "No more than this many flow tasks run at the same time." in texts
    assert (
        "No more than this many Claude Workflow agents run at the same time." in texts
    )
    assert "The most times a reviewer can check one flow task." in texts


def test_when_help_covers_every_value() -> None:
    for value, _text in menu.WHEN:
        assert menu.when_help(value)
    assert menu.when_help("2") == (
        "Plans of 2 or more tasks. Plans of 1 task start without one."
    )
