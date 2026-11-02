from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from ember import choices
from ember.app.context import AppContext
from ember.app.screens.menu import MenuScreen
from ember.app.widgets import Picker


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
