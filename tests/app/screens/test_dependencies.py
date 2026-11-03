import datetime
import threading
import time
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from ember import deps
from ember.app import theme
from ember.app.context import AppContext
from ember.app.screens import dependencies as screen_module
from ember.app.screens.dependencies import DependenciesScreen, subtitle
from ember.app.window import MainWindow


def _component(
    id_: str,
    status: str,
    *,
    group: str = "system",
    label: str | None = None,
    installed: str | None = "1.0",
    newest: str | None = "1.0",
    error: str | None = None,
    command: str | None = None,
    command_kind: str | None = None,
    note: str | None = None,
) -> deps.Component:
    return deps.Component(
        id=id_,
        label=label or id_,
        group=group,
        status=status,
        installed=installed,
        newest=newest,
        error=error,
        command=command,
        command_kind=command_kind,
        note=note,
    )


def _report(*items: deps.Component, newest_at: float | None = None) -> deps.DepsReport:
    stamp = time.time() if newest_at is None else newest_at
    return deps.DepsReport(
        components=items,
        installed_read_at={item.id: stamp for item in items},
        newest_read_at={item.id: stamp for item in items},
    )


SAMPLE = (
    _component("tmux", deps.CURRENT, label="tmux"),
    _component(
        "git",
        deps.UPDATE_AVAILABLE,
        label="git",
        newest="2.0",
        command="sudo apt install --only-upgrade git",
        command_kind="update",
    ),
    _component(
        "bubblewrap",
        deps.MISSING,
        label="bubblewrap",
        installed=None,
        command="sudo apt install bubblewrap",
        command_kind="install",
    ),
    _component(
        "python3",
        deps.CHECK_FAILED,
        label="Python",
        newest=None,
        error="newest version: no candidate",
    ),
    _component("bash", deps.CURRENT, label="Bash"),
    _component(
        "claude-desktop",
        deps.MISSING,
        group="apps",
        label="Claude Desktop",
        installed=None,
        newest=None,
        note="Not installed.",
    ),
    _component("codex", deps.CURRENT, group="apps", label="Codex"),
    _component(
        "pyside6",
        deps.UPDATE_AVAILABLE,
        group="libraries",
        label="PySide6",
        newest="6.13.0",
        note="Updates with the next Ember release.",
    ),
)


class _Calls:
    def __init__(self) -> None:
        self.refresh: list[bool] = []
        self.commands: list[str] = []


def _screen(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    report: deps.DepsReport | None,
) -> tuple[DependenciesScreen, AppContext, _Calls]:
    context = AppContext(env)
    calls = _Calls()
    monkeypatch.setattr(
        context, "refresh_deps", lambda check_now: calls.refresh.append(check_now)
    )

    def run_command(_parent: QWidget, command: str) -> bool:
        calls.commands.append(command)
        return True

    monkeypatch.setattr(context, "run_command", run_command)
    context._deps = report
    widget = DependenciesScreen(context)
    qtbot.addWidget(widget)
    widget.show()
    widget.shown()
    return widget, context, calls


def test_groups_and_order(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    assert widget.order() == [
        ("apps", ["claude-desktop", "codex"]),
        ("system", ["git", "bubblewrap", "python3", "bash", "tmux"]),
        ("libraries", ["pyside6"]),
    ]
    assert [heading.text() for _key, heading in widget.groups] == [
        "Engines and apps",
        "System tools",
        "Ember's libraries",
    ]


def test_empty_group_left_out(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report(_component("tmux", deps.CURRENT))
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, report)
    assert widget.order() == [("system", ["tmux"])]


def test_tiles_for_every_status(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    tiles = widget.tiles
    assert (tiles["git"].badge.text(), tiles["git"].badge.tone()) == ("Update", "info")
    assert tiles["git"].versions.text() == "Installed 1.0 · Newest 2.0"
    assert (tiles["bubblewrap"].badge.text(), tiles["bubblewrap"].badge.tone()) == (
        "Missing",
        "warning",
    )
    assert tiles["bubblewrap"].versions.text() == "Newest 1.0"
    assert (tiles["python3"].badge.text(), tiles["python3"].badge.tone()) == (
        "Check failed",
        "danger",
    )
    assert tiles["python3"].versions.text() == "Installed 1.0 · Newest Not read"
    assert tiles["python3"].error.text() == "newest version: no candidate"
    assert not tiles["python3"].error.isHidden()
    assert (tiles["tmux"].badge.text(), tiles["tmux"].badge.tone()) == (
        "Current",
        "success",
    )
    assert tiles["tmux"].error.isHidden()
    desktop = tiles["claude-desktop"]
    assert desktop.versions.isHidden()
    assert desktop.note is not None
    assert desktop.note.text() == "Not installed."
    assert tiles["pyside6"].note is not None
    assert tiles["pyside6"].note.text() == "Updates with the next Ember release."


def test_command_only_for_update_and_install(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    with_command = {
        key: tile.command.command()
        for key, tile in widget.tiles.items()
        if tile.command is not None
    }
    assert with_command == {
        "git": "sudo apt install --only-upgrade git",
        "bubblewrap": "sudo apt install bubblewrap",
    }
    assert widget.tiles["tmux"].note is None


def test_run_goes_through_the_context(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    command = widget.tiles["git"].command
    assert command is not None
    command.run_button.click()
    assert calls.commands == ["sudo apt install --only-upgrade git"]


def test_opening_reads_nothing_new(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    widget.shown()
    assert calls.refresh == []


def test_check_now_reads(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, context, calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    widget.check_button.click()
    assert calls.refresh == [True]
    assert widget.check_button.isEnabled()
    assert not widget.checking_label.isHidden()
    report = _report(_component("tmux", deps.CURRENT))
    context._deps = report
    context.deps_changed.emit(report)
    assert widget.checking_label.isHidden()
    assert widget.order() == [("system", ["tmux"])]


def test_no_report_shows_loading_then_report(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, context, calls = _screen(env, qtbot, monkeypatch, None)
    assert calls.refresh == [False]
    assert widget.scroll_area.isHidden()
    qtbot.waitUntil(lambda: widget.state_view.loading.isVisible(), timeout=2000)
    report = _report(*SAMPLE)
    context._deps = report
    context.deps_changed.emit(report)
    assert not widget.state_view.loading.isVisible()
    assert widget.state_view.isHidden()
    assert not widget.scroll_area.isHidden()
    assert "claude-desktop" in widget.tiles


def test_error_without_report_offers_try_again(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, context, calls = _screen(env, qtbot, monkeypatch, None)
    context.deps_failed.emit("Ember could not read its dependencies: broken.")
    assert widget.state_view.error.isVisible()
    assert widget.state_view.error_text.text().startswith("Ember could not read")
    widget.state_view.retry_button.click()
    assert calls.refresh == [False, True]


def test_error_with_report_keeps_tiles(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, context, calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    context.deps_failed.emit("Ember could not read its dependencies: broken.")
    assert widget.alert is not None
    assert widget.alert.tone() == "danger"
    assert not widget.scroll_area.isHidden()
    widget.alert.buttons[0].click()
    assert calls.refresh == [True]
    context.deps_changed.emit(context.deps())
    assert widget.alert is None


def test_redraw_waits_while_hidden(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, context, _calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    widget.hide()
    report = _report(_component("tmux", deps.CURRENT))
    context._deps = report
    context.deps_changed.emit(report)
    assert "git" in widget.tiles
    widget.show()
    widget.shown()
    assert list(widget.tiles) == ["tmux"]


def test_empty_report(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, _report())
    assert widget.state_view.empty.isVisible()
    assert not widget.state_view.isHidden()
    assert widget.scroll_area.isHidden()


def test_tiles_take_the_whole_content_area(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    widget.resize(1000, 800)
    qtbot.waitUntil(lambda: widget.scroll_area.height() > 0, timeout=2000)
    assert widget.state_view.isHidden()
    assert widget.scroll_area.geometry().top() < 200


def test_subtitle_counts() -> None:
    now = time.time()
    current = _component("a", deps.CURRENT)
    update = _component("b", deps.UPDATE_AVAILABLE)
    missing = _component("c", deps.MISSING)
    failed = _component("d", deps.CHECK_FAILED)
    clock = datetime.datetime.fromtimestamp(now).strftime("%H:%M")
    assert subtitle(_report(current, newest_at=now), now) == (
        f"Everything is current. Newest versions read today {clock}."
    )
    assert subtitle(_report(update, update, update, newest_at=now), now) == (
        f"3 updates available. Newest versions read today {clock}."
    )
    assert subtitle(_report(missing, newest_at=now), now).startswith("1 item missing. ")
    assert subtitle(_report(update, missing, failed, failed, newest_at=now), now) == (
        "1 update available, 1 item missing and 2 checks failed. "
        f"Newest versions read today {clock}."
    )
    assert subtitle(_report(failed, newest_at=now), now).startswith("1 check failed.")


def test_subtitle_newest_coverage() -> None:
    now = time.time()
    clock = datetime.datetime.fromtimestamp(now - 3600).strftime("%H:%M")
    items = (_component("a", deps.CURRENT), _component("b", deps.CHECK_FAILED))
    none_read = deps.DepsReport(
        components=items,
        installed_read_at={"a": now, "b": now},
        newest_read_at={"a": None, "b": None},
    )
    assert subtitle(none_read, now).endswith("Newest versions not read yet.")
    partial = deps.DepsReport(
        components=items,
        installed_read_at={"a": now, "b": now},
        newest_read_at={"a": now - 3600, "b": None},
    )
    expected = f"Some newest versions not read; the others read today {clock}."
    if datetime.datetime.fromtimestamp(now - 3600).date() != (
        datetime.datetime.fromtimestamp(now).date()
    ):
        expected = f"Some newest versions not read; the others read yesterday {clock}."
    assert subtitle(partial, now).endswith(expected)


def test_keyboard_reach(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, _report(*SAMPLE))
    widget.check_button.setFocus()
    expected = [widget.check_button]
    for key in ("git", "bubblewrap"):
        command = widget.tiles[key].command
        assert command is not None
        expected += [command.copy_button, command.run_button]
    reached = []
    for _step in range(len(expected)):
        reached.append(widget.focusWidget())
        widget.focusNextChild()
    assert reached == expected


def test_sidebar_count_follows_deps_changed(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = AppContext(env)
    monkeypatch.setattr(context, "refresh_deps", lambda check_now: None)
    window = MainWindow(context, has_tray=True, quit_app=lambda: None)
    qtbot.addWidget(window)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    window.show()
    window.show_screen("dependencies")
    widget = window.screen_widget("dependencies")
    assert isinstance(widget, DependenciesScreen)
    report = _report(*SAMPLE)
    context._deps = report
    context.deps_changed.emit(report)
    assert window.nav["dependencies"].count() == report.counts() == 4
    assert widget.subtitle.text().startswith(
        "2 updates available, 2 items missing and 1 check failed."
    )
    assert screen_module.ordered(report)[0][0] == "apps"
    failed_skill = replace(
        _component("ember-skill", deps.CHECK_FAILED, group="apps"),
        installed_kind="skill",
        newest_kind="none",
        link_state="broken link",
    )
    report = _report(*SAMPLE, failed_skill)
    context._deps = report
    context.deps_changed.emit(report)
    assert window.nav["dependencies"].count() == report.counts() == 5


def test_check_now_without_a_report_shows_one_spinner(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget, _context, calls = _screen(env, qtbot, monkeypatch, None)
    widget.check_button.click()
    assert calls.refresh == [False, True]
    assert widget.spinner.isHidden()
    assert widget.checking_label.isHidden()
    qtbot.waitUntil(lambda: widget.state_view.loading.isVisible(), timeout=2000)


def test_an_earlier_read_keeps_the_check_spinner(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {False: threading.Event(), True: threading.Event()}
    asked: list[bool] = []

    def read(_env: object, *, installed: bool, newest: str) -> deps.DepsReport:
        asked.append(installed)
        gates[installed].wait(5)
        if installed:
            return _report(_component("tmux", deps.CURRENT))
        return _report(*SAMPLE)

    monkeypatch.setattr(deps, "read", read)
    context = AppContext(env)
    widget = DependenciesScreen(context)
    qtbot.addWidget(widget)
    widget.show()
    widget.shown()
    widget.check_button.click()
    assert widget.spinner.isHidden()
    with qtbot.waitSignal(context.deps_changed, timeout=5000):
        gates[False].set()
    assert "claude-desktop" in widget.tiles
    assert not widget.spinner.isHidden()
    assert not widget.checking_label.isHidden()
    with qtbot.waitSignal(context.deps_changed, timeout=5000):
        gates[True].set()
    assert asked == [False, True]
    assert widget.spinner.isHidden()
    assert widget.checking_label.isHidden()
    assert widget.order() == [("system", ["tmux"])]


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_unidentified_install_shows_path_command_and_note(
    env: dict[str, str],
    qtbot: QtBot,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    theme.apply(qapp, mode)
    item = replace(
        _component(
            "codex",
            deps.UPDATE_AVAILABLE,
            group="apps",
            newest="2.0",
            command="/tools/bin/codex update",
            command_kind="update",
            note="Ember could not tell how the tool was installed.",
        ),
        resolved_path="/tools/unknown/codex",
    )
    widget, _context, calls = _screen(env, qtbot, monkeypatch, _report(item))
    tile = widget.tiles["codex"]
    assert tile.badge.text() == "Update"
    assert tile.resolved_path.text() == item.resolved_path
    assert not tile.resolved_path.isHidden()
    assert (
        tile.resolved_path.textInteractionFlags()
        & Qt.TextInteractionFlag.TextSelectableByMouse
    )
    assert tile.note is not None and tile.note.text() == item.note
    assert not tile.note.isHidden()
    assert tile.command is not None and tile.command.command() == item.command
    tile.command.run_button.click()
    assert calls.commands == [item.command]


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize(
    ("state", "status", "command", "note"),
    [
        ("installed", deps.CURRENT, None, None),
        ("missing", deps.MISSING, "emb skill install", None),
        ("broken link", deps.CHECK_FAILED, "emb skill install", None),
        ("other link", deps.CHECK_FAILED, "emb skill install --force", None),
        ("folder", deps.CHECK_FAILED, None, "Move /skill, then run emb skill install."),
        ("file", deps.CHECK_FAILED, None, "Move /skill, then run emb skill install."),
        ("missing", deps.MISSING, None, "Reinstall Ember."),
    ],
)
def test_skill_tile(
    env: dict[str, str],
    qtbot: QtBot,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    state: str,
    status: str,
    command: str | None,
    note: str | None,
) -> None:
    theme.apply(qapp, mode)
    item = replace(
        _component(
            "ember-skill",
            status,
            group="apps",
            label="ember skill",
            installed=None,
            newest=None,
            error=state if status == deps.CHECK_FAILED else None,
            command=command,
            command_kind="install" if command else None,
            note=note,
        ),
        installed_kind="skill",
        newest_kind="none",
        link_state=state,
    )
    report = _report(item)
    assert "Newest versions" not in subtitle(report)
    widget, _context, calls = _screen(env, qtbot, monkeypatch, report)
    assert widget.order() == [("apps", ["ember-skill"])]
    tile = widget.tiles[item.id]
    assert tile.versions.text() == state
    assert tile.versions.isVisible()
    assert tile.error.isHidden()
    assert tile.badge.text() == screen_module.BADGES[status][0]
    if command:
        assert tile.command is not None
        assert tile.command.command() == command
        tile.command.run_button.click()
        assert calls.commands == [command]
    else:
        assert tile.command is None
    if note:
        assert tile.note is not None and tile.note.text() == note
    else:
        assert tile.note is None


def test_skill_does_not_make_newest_summary_incomplete() -> None:
    now = time.time()
    item = replace(
        _component("ember-skill", deps.CHECK_FAILED),
        installed_kind="skill",
        newest_kind="none",
        link_state="broken link",
    )
    report = deps.DepsReport(
        components=(_component("tool", deps.CURRENT), item),
        installed_read_at={"tool": now, "ember-skill": now},
        newest_read_at={"tool": now, "ember-skill": None},
    )
    assert "Newest versions read today" in subtitle(report, now)
    assert "not read" not in subtitle(report, now)
    assert report.counts() == 1


@pytest.mark.parametrize(
    ("command", "kind", "path", "shown"),
    [
        ("codex update", "update", "/tools/codex", True),
        (None, "update", "/tools/codex", False),
        ("install codex", "install", "/tools/codex", False),
        ("codex update", None, "/tools/codex", False),
        ("codex update", "update", None, False),
        (None, None, "/tools/codex", False),
    ],
)
def test_resolved_path_shown_only_with_update_command(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    command: str | None,
    kind: str | None,
    path: str | None,
    shown: bool,
) -> None:
    item = replace(
        _component("codex", deps.CURRENT, command=command, command_kind=kind),
        resolved_path=path,
    )
    widget, _context, _calls = _screen(env, qtbot, monkeypatch, _report(item))
    assert widget.tiles["codex"].resolved_path.isVisible() is shown
