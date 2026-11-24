import gc
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event, get_ident
from typing import Any

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from ember import autostart, choices, db, deps, paths, updates
from ember.app import main, theme, updater
from ember.app import tray as tray_module
from ember.app.screens.dependencies import DependenciesScreen
from ember.engines import status

from .test_flows import FLOW, PANEL, make_flow, make_panel  # pyrefly: ignore[missing-import]
from .test_updater import EMBER_ROW, FakePackageKit  # pyrefly: ignore[missing-import]

EMPTY_REPORT = deps.DepsReport(components=(), installed_read_at={}, newest_read_at={})
_read_deps = deps.read


def _use_ember_dependency_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = tuple(row for row in deps.load_components() if row.id in ("ember", "tmux"))
    monkeypatch.setattr(
        deps, "read", lambda env, **kwargs: _read_deps(env, components=specs, **kwargs)
    )
    monkeypatch.setattr(deps, "_installed_apt", lambda *_args: "1.0.0")
    monkeypatch.setattr(main, "installed_version", lambda: "1.0.0")


@pytest.fixture
def app_env(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, qapp: QApplication
) -> Iterator[dict[str, str]]:
    """An environment with a Qt platform, and stub engine and dependency reads."""
    monkeypatch.setattr(main.faulthandler, "enable", lambda **_kwargs: None)
    monkeypatch.setattr(main.faulthandler, "disable", lambda: None)
    monkeypatch.setattr(status, "read", lambda _env, *, refresh=False: {})
    monkeypatch.setattr(deps, "read", lambda _env, **_kwargs: EMPTY_REPORT)
    monkeypatch.setattr(tray_module, "available", lambda: False)
    yield {**env, "QT_QPA_PLATFORM": "offscreen"}
    qapp.setQuitOnLastWindowClosed(True)
    qapp.setStyleSheet("")
    theme.apply(qapp, "light")
    qapp.setStyleSheet("")


def _start(
    argv: list[str], env: dict[str, str], qtbot: QtBot
) -> Iterator[main.EmberApp]:
    started = main.start(argv, env)
    assert isinstance(started, main.EmberApp)
    qtbot.addWidget(started.window)
    try:
        qtbot.waitUntil(started.context.settings_ready, timeout=5000)
        yield started
    finally:
        main.stop(started)


def _check_dependencies(parts: main.EmberApp, trigger: str) -> None:
    if trigger == "daily":
        assert parts.updater is not None
        parts.updater._timer.timeout.emit()
    else:
        parts.window.show_screen("dependencies")
        screen = parts.window.screen_widget("dependencies")
        assert isinstance(screen, DependenciesScreen)
        screen.check_button.click()


def test_no_graphical_session_exits_69(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main.run([], env) == 69
    assert capsys.readouterr().err == (
        "emb app: no graphical session (WAYLAND_DISPLAY and DISPLAY are unset)\n"
    )


def test_unknown_argument_exits_64(
    app_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main.run(["--nope"], app_env) == 64
    assert "--nope" in capsys.readouterr().err


def test_starts_with_the_window_and_reads(
    app_env: dict[str, str], qtbot: QtBot, qapp: QApplication
) -> None:
    # Only the test's own folder, so an installed emb is not found on PATH.
    app_env = {**app_env, "PATH": app_env["PATH"].split(":")[0]}
    for parts in _start([], app_env, qtbot):
        assert parts.window.isVisible()
        assert parts.tray is None
        assert QApplication.applicationName() == "Ember"
        assert QApplication.desktopFileName() == main.APP_ID
        assert qapp.quitOnLastWindowClosed()
        assert parts.server.isListening()
        assert parts.server.fullServerName() == str(main.socket_path(app_env))
        qtbot.waitUntil(lambda parts=parts: parts.context.deps() is EMPTY_REPORT)
        qtbot.waitUntil(lambda parts=parts: parts.context.engines() == {})
        qtbot.waitUntil(parts.context.runs.loaded)
        assert theme.current_mode() in theme.MODES
        assert parts.autostart_reason is not None
        assert "emb is not on PATH" in parts.autostart_reason
        assert parts.context.autostart_reason() == parts.autostart_reason
        assert not autostart.path(app_env).exists()


def test_startup_and_daily_checks_share_a_fresh_index_read_with_dependencies(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_ember_dependency_reader(monkeypatch)
    release = "1.0.0"
    reads: list[str] = []
    apt_reads: list[str] = []
    gui_thread = get_ident()

    def fetch(url: str, _timeout: float) -> str:
        assert url == updates.PACKAGES_URL
        assert get_ident() != gui_thread
        reads.append(release)
        return f"Package: ember\nVersion: {release}\n"

    def read_apt(package: str, *_args: object) -> str:
        apt_reads.append(package)
        return "1.0.0"

    monkeypatch.setattr(deps, "_newest_apt", read_apt)
    deps.read(app_env, fetch=lambda _url, _timeout: "Package: ember\nVersion: 1.0.0\n")
    monkeypatch.setattr(deps, "fetch_text", fetch)
    release = "2.0.0"
    for parts in _start([], app_env, qtbot):
        active = parts.updater
        assert active is not None
        assert active._timer.interval() == 86_400_000
        qtbot.waitUntil(lambda parts=parts: parts.window.update_notice is not None)
        report = parts.context.deps()
        assert report is not None
        assert {row.id: row.newest for row in report.components} == {
            "ember": "2.0.0",
            "tmux": "1.0.0",
        }
        assert reads == ["2.0.0"] and apt_reads == ["tmux"]
        release = "3.0.0"
        with qtbot.waitSignal(active.notice_changed, timeout=5000):
            active._timer.timeout.emit()
        notice = parts.window.update_notice
        assert notice is not None
        assert (
            "1.0.0" in notice.text_label.text() and "3.0.0" in notice.text_label.text()
        )
        report = parts.context.deps()
        assert report is not None
        assert (
            next(row.newest for row in report.components if row.id == "ember")
            == "3.0.0"
        )
        assert reads == ["2.0.0", "3.0.0"] and apt_reads == ["tmux"]


def test_disabled_cold_start_and_check_now_skip_the_ember_index(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_ember_dependency_reader(monkeypatch)
    choices.set_settings({"updates.check": "off"}, env=app_env)
    apt_reads: list[str] = []

    def read_apt(package: str, *_args: object) -> str:
        apt_reads.append(package)
        return "1.0.0"

    def fetch(url: str, _timeout: float) -> str:
        pytest.fail(f"Unexpected index request for {url}")

    monkeypatch.setattr(deps, "_newest_apt", read_apt)
    monkeypatch.setattr(deps, "fetch_text", fetch)
    for parts in _start([], app_env, qtbot):
        qtbot.waitUntil(lambda parts=parts: parts.context.deps() is not None)
        assert apt_reads == ["tmux"]
        parts.window.show_screen("dependencies")
        screen = parts.window.screen_widget("dependencies")
        assert isinstance(screen, DependenciesScreen)
        with qtbot.waitSignal(parts.context.deps_changed, timeout=5000):
            screen.check_button.click()
        assert set(screen.tiles) == {"tmux"}
        assert parts.window.update_notice is None
        assert parts.updater is not None and parts.updater._timer.isActive()


@pytest.mark.parametrize("trigger", ["daily", "manual"])
def test_dependency_check_observes_external_disable_and_daily_check_resumes(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    _use_ember_dependency_reader(monkeypatch)
    installed_reads: list[str] = []
    index_reads: list[str] = []

    def installed(package: str, *_args: object) -> str:
        installed_reads.append(package)
        return "1.0.0"

    def fetch(url: str, _timeout: float) -> str:
        index_reads.append(url)
        return "Package: ember\nVersion: 2.0.0\n"

    monkeypatch.setattr(deps, "_installed_apt", installed)
    monkeypatch.setattr(deps, "_newest_apt", lambda *_args: "1.0.0")
    monkeypatch.setattr(deps, "fetch_text", fetch)
    for parts in _start([], app_env, qtbot):
        context, window = parts.context, parts.window
        qtbot.waitUntil(lambda window=window: window.update_notice is not None)
        assert index_reads == [updates.PACKAGES_URL]
        choices.set_settings({"updates.check": "off"}, env=app_env)
        assert context.setting("updates.check") == "on"
        active = parts.updater
        assert active is not None
        with qtbot.waitSignal(context.deps_changed, timeout=5000):
            _check_dependencies(parts, trigger)
        assert context.setting("updates.check") == "off"
        assert active._timer.isActive()
        report = context.deps()
        assert report is not None and [row.id for row in report.components] == ["tmux"]
        assert installed_reads.count("tmux") == 2
        assert installed_reads.count("ember") == 1
        assert index_reads == [updates.PACKAGES_URL]
        assert window.update_notice is None

        window.hide()
        with qtbot.waitSignal(context.deps_changed, timeout=5000):
            active._timer.timeout.emit()
        assert index_reads == [updates.PACKAGES_URL]
        assert window.update_notice is None
        choices.set_settings({"updates.check": "on"}, env=app_env)
        assert context.setting("updates.check") == "off"
        with qtbot.waitSignal(active.notice_changed, timeout=5000):
            active._timer.timeout.emit()
        assert context.setting("updates.check") == "on"
        assert active._timer.isActive() and not context.deps_refreshing()
        assert not window.isVisible() and window.update_notice is not None
        report = context.deps()
        assert report is not None
        assert {row.id: row.newest for row in report.components} == {
            "ember": "2.0.0",
            "tmux": "1.0.0",
        }
        assert installed_reads.count("ember") == 2
        assert index_reads == [updates.PACKAGES_URL, updates.PACKAGES_URL]


@pytest.mark.parametrize("trigger", ["daily", "manual"])
def test_dependency_check_replaces_an_update_offer_when_settings_cannot_be_read(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    _use_ember_dependency_reader(monkeypatch)
    monkeypatch.setattr(updater, "running_deb", lambda _env: True)
    monkeypatch.setattr(deps, "_newest_apt", lambda *_args: "1.0.0")
    index_reads: list[str] = []

    def fetch(url: str, _timeout: float) -> str:
        index_reads.append(url)
        return "Package: ember\nVersion: 2.0.0\n"

    def unavailable(**_kwargs: object) -> dict[str, Any]:
        raise db.DatabaseError("The settings database is unavailable.")

    monkeypatch.setattr(deps, "fetch_text", fetch)
    for parts in _start([], app_env, qtbot):
        context, window = parts.context, parts.window
        qtbot.waitUntil(lambda window=window: window.update_notice is not None)
        offer = window.update_notice
        assert offer is not None and offer.isVisible()
        assert [button.text() for button in offer.buttons] == ["Update", "Dismiss"]
        monkeypatch.setattr(choices, "snapshot", unavailable)

        with qtbot.waitSignal(context.deps_changed, timeout=5000):
            _check_dependencies(parts, trigger)

        assert index_reads == [updates.PACKAGES_URL]
        report = context.deps()
        assert report is not None and [row.id for row in report.components] == ["tmux"]
        notice = window.update_notice
        assert notice is not None and notice is not offer
        assert notice.title_label.text() == "Ember could not check for updates"
        assert "The settings database is unavailable." in notice.text_label.text()
        assert "curl -fLO" in notice.text_label.text()
        assert "sudo apt install" in notice.text_label.text()
        assert [button.text() for button in notice.buttons] == ["Dismiss"]


def test_switching_off_during_a_check_suppresses_the_late_offer(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_ember_dependency_reader(monkeypatch)
    monkeypatch.setattr(deps, "_newest_apt", lambda *_args: "1.0.0")
    entered, release = Event(), Event()

    def fetch(_url: str, _timeout: float) -> str:
        entered.set()
        assert release.wait(5)
        return "Package: ember\nVersion: 2.0.0\n"

    monkeypatch.setattr(deps, "fetch_text", fetch)
    try:
        for parts in _start([], app_env, qtbot):
            qtbot.waitUntil(entered.is_set)
            with qtbot.waitSignal(parts.context.settings_changed, timeout=5000):
                parts.context.save_settings({"updates.check": "off"})
            release.set()
            qtbot.waitUntil(lambda parts=parts: not parts.context.deps_refreshing())
            report = parts.context.deps()
            assert report is not None and [row.id for row in report.components] == [
                "tmux"
            ]
            assert parts.window.update_notice is None
    finally:
        release.set()


def test_second_instance_hands_over(app_env: dict[str, str], qtbot: QtBot) -> None:
    for parts in _start([], app_env, qtbot):
        parts.window.hide()
        assert main.start(["--hidden"], app_env) == 0
        qtbot.wait(200)
        assert not parts.window.isVisible()
        assert main.start([], app_env) == 0
        qtbot.waitUntil(parts.window.isVisible, timeout=5000)


def test_run_returns_0_when_handed_over(app_env: dict[str, str], qtbot: QtBot) -> None:
    for parts in _start([], app_env, qtbot):
        parts.window.hide()
        assert main.run([], app_env) == 0
        qtbot.waitUntil(parts.window.isVisible, timeout=5000)


def test_stale_socket_is_removed(app_env: dict[str, str], qtbot: QtBot) -> None:
    path = main.socket_path(app_env)
    path.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()
    for parts in _start([], app_env, qtbot):
        assert parts.server.isListening()
        assert parts.window.isVisible()


def test_hidden_with_a_tray_starts_in_the_tray(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    monkeypatch.setattr(tray_module, "available", lambda: True)
    for parts in _start(["--hidden"], app_env, qtbot):
        assert parts.tray is not None
        assert not parts.window.isVisible()
        assert not qapp.quitOnLastWindowClosed()
        assert parts.window.has_tray


def test_hidden_without_a_tray_shows_the_window(
    app_env: dict[str, str], qtbot: QtBot
) -> None:
    for parts in _start(["--hidden"], app_env, qtbot):
        assert parts.tray is None
        assert parts.window.isVisible()
        assert not parts.window.has_tray


def test_theme_follows_the_setting(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    for parts in _start([], app_env, qtbot):
        applied: list[str] = []
        theme.signals.changed.connect(applied.append)
        try:
            with qtbot.waitSignal(parts.context.settings_changed, timeout=5000):
                parts.context.save_settings({"app.theme": "dark"})
            assert theme.current_mode() == "dark"
            with qtbot.waitSignal(parts.context.settings_changed, timeout=5000):
                parts.context.save_settings({"app.theme": "light"})
            assert theme.current_mode() == "light"
            applied.clear()
            parts.app.styleHints().colorSchemeChanged.emit(
                parts.app.styleHints().colorScheme()
            )
            assert applied == []
        finally:
            theme.signals.changed.disconnect(applied.append)


def test_parse() -> None:
    parsed: Any = main._parse(["--hidden"])
    assert parsed.hidden
    assert not main._parse([]).hidden  # type: ignore[union-attr]
    assert main._parse(["extra"]) is None


def test_autostart_follows_the_setting(app_env: dict[str, str], qtbot: QtBot) -> None:
    emb = Path(app_env["PATH"].split(":")[0]) / "emb"
    emb.write_text("#!/bin/sh\nexit 0\n")
    emb.chmod(0o755)
    entry = autostart.path(app_env)
    for parts in _start(["--hidden"], app_env, qtbot):
        assert parts.autostart_reason is None
        assert entry.exists()
        with qtbot.waitSignal(
            parts.context.autostart_reason_changed, timeout=5000
        ) as blocker:
            parts.context.save_settings({"app.autostart": "off"})
        assert blocker.args == [None]
        assert not entry.exists()
        with qtbot.waitSignal(parts.context.settings_changed, timeout=5000):
            parts.context.save_settings({"app.autostart": "on"})
        assert entry.exists()
        assert parts.autostart_reason is None


def test_quit_hides_the_window_and_tray_at_once(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    monkeypatch.setattr(tray_module, "available", lambda: True)
    quits: list[bool] = []
    monkeypatch.setattr(qapp, "quit", lambda: quits.append(True))
    for parts in _start([], app_env, qtbot):
        assert parts.tray is not None
        parts.window.bring_forward()
        assert parts.window.isVisible()
        assert parts.tray.icon.isVisible()
        parts.window.quit_app()
        assert not parts.window.isVisible()
        assert not parts.tray.icon.isVisible()
        assert quits == [True]


def test_stop_hides_everything_before_waiting_for_reads(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tray_module, "available", lambda: True)
    waits: list[tuple[int, bool, bool, bool]] = []
    for started in _start([], app_env, qtbot):
        tray = started.tray
        assert tray is not None
        real_wait = started.context.tasks.wait

        def wait(
            msecs: int = -1,
            real_wait: Callable[[int], bool] = real_wait,
            started: main.EmberApp = started,
            tray: tray_module.Tray = tray,
        ) -> bool:
            """Record visibility and socket state when shutdown waits for jobs."""
            waits.append(
                (
                    msecs,
                    started.window.isVisible(),
                    tray.icon.isVisible(),
                    started.server.isListening(),
                )
            )
            return real_wait(msecs)

        monkeypatch.setattr(started.context.tasks, "wait", wait)
        main.stop(started)
        assert waits == [(12_000, False, False, False)]
        assert main.QUIT_WAIT_MS > 10_000


def test_flows_start_and_stop_with_app(app_env: dict[str, str], qtbot: QtBot) -> None:
    model = None
    for parts in _start([], app_env, qtbot):
        model = parts.context.flows
        assert model.active()
        qtbot.waitUntil(model.loaded)
        parts.window.hide()
        assert model.active()
    assert model is not None
    assert not model.active()
    assert not model.watched()


@pytest.mark.parametrize("kind", ["flow", "panel"])
def test_tray_parent_opens_overview(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(tray_module, "available", lambda: True)
    if kind == "flow":
        make_flow(app_env, status="stopped")
    else:
        make_panel(app_env)
    for parts in _start(["--hidden"], app_env, qtbot):
        assert parts.tray is not None
        qtbot.waitUntil(parts.context.flows.loaded)
        assert parts.tray.icon.toolTip() == f"Ember · 1 {kind} live"
        parts.window.show_screen("settings")
        parts.window.hide()
        parts.tray.menu.aboutToShow.emit()
        action = parts.tray.menu.actions()[1]
        assert (FLOW if kind == "flow" else PANEL) in action.text()
        action.trigger()
        assert parts.window.isVisible()
        assert parts.window.current_key() == "overview"
        parts.window.show_screen("settings")
        parts.window.hide()
        parts.tray.menu.actions()[-2].trigger()
        assert parts.window.isVisible()
        assert parts.window.current_key() == "settings"


def test_hand_over_with_the_same_version_offers_no_restart(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "installed_version", lambda: "0.4.0")
    for parts in _start([], app_env, qtbot):
        assert parts.version == "0.4.0"
        assert main.start(["--hidden"], app_env) == 0
        qtbot.wait(200)
        assert parts.window.update_box is None


def test_hand_over_after_an_upgrade_offers_a_restart(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = {"value": "0.4.0"}
    monkeypatch.setattr(main, "installed_version", lambda: version["value"])
    restarts: list[main.EmberApp] = []
    monkeypatch.setattr(main, "restart", restarts.append)
    for parts in _start([], app_env, qtbot):
        version["value"] = "0.5.0"
        assert main.start(["--hidden"], app_env) == 0
        qtbot.waitUntil(lambda parts=parts: parts.window.update_box is not None)
        box = parts.window.update_box
        assert box is not None
        assert box.label.text() == "Ember 0.5.0 is installed."
        assert box.restart_button.text() == "Restart"
        assert parts.window.sidebar.isAncestorOf(box)
        assert parts.window.notice is None
        version_top = parts.window.version_label.mapTo(parts.window, QPoint(0, 0))
        assert box.mapTo(parts.window, QPoint(0, 0)).y() < version_top.y()
        parts.window.show_notice("info", "Another notice")
        parts.window.clear_notice()
        assert parts.window.update_box is box
        assert main.start([], app_env) == 0
        qtbot.wait(200)
        assert parts.window.update_box is box
        version["value"] = "0.6.0"
        assert main.start([], app_env) == 0
        qtbot.waitUntil(lambda box=box: box.label.text() == "Ember 0.6.0 is installed.")
        assert parts.window.update_box is box
        box.restart_button.click()
        assert restarts == [parts]


def test_package_change_offers_a_restart_without_a_second_launch(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "site-packages"
    (package / "ember_app-0.4.0.dist-info").mkdir(parents=True)
    version = {"value": "0.4.0"}
    monkeypatch.setattr(main, "installed_version", lambda: version["value"])
    monkeypatch.setattr(main, "package_folder", lambda: package)
    monkeypatch.setattr(main, "UPGRADE_SETTLE_MS", 50)
    for parts in _start([], app_env, qtbot):
        (package / "ember_app-0.4.0.dist-info").rename(
            package / "ember_app-0.5.0.dist-info"
        )
        version["value"] = "0.5.0"
        qtbot.waitUntil(
            lambda parts=parts: parts.window.update_box is not None, timeout=5000
        )
        box = parts.window.update_box
        assert box is not None
        assert box.label.text() == "Ember 0.5.0 is installed."


def test_package_change_with_the_same_version_offers_no_restart(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "site-packages"
    package.mkdir()
    monkeypatch.setattr(main, "installed_version", lambda: "0.4.0")
    monkeypatch.setattr(main, "package_folder", lambda: package)
    monkeypatch.setattr(main, "UPGRADE_SETTLE_MS", 50)
    for parts in _start([], app_env, qtbot):
        (package / "other").mkdir()
        qtbot.wait(400)
        assert parts.window.update_box is None


@pytest.mark.parametrize("error", [None, "PackageKit could not finish the upgrade."])
def test_install_holds_both_restart_offers_until_finished(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: str | None,
) -> None:
    package = tmp_path / "site-packages"
    (package / "ember_app-1.0.0.dist-info").mkdir(parents=True)
    version = "1.0.0"
    monkeypatch.setattr(main, "installed_version", lambda: version)
    monkeypatch.setattr(main, "package_folder", lambda: package)
    monkeypatch.setattr(main, "UPGRADE_SETTLE_MS", 50)
    monkeypatch.setattr(updater, "running_deb", lambda _env: True)
    transport = FakePackageKit()
    monkeypatch.setattr(updater, "QtPackageKit", lambda _parent: transport)
    report = deps.DepsReport(
        components=(EMBER_ROW,),
        installed_read_at={},
        newest_read_at={},
    )
    monkeypatch.setattr(deps, "read", lambda _env, **_kwargs: report)
    restarts: list[main.EmberApp] = []
    monkeypatch.setattr(main, "restart", restarts.append)
    for parts in _start([], app_env, qtbot):
        window = parts.window
        qtbot.waitUntil(lambda window=window: window.update_notice is not None)
        notice = window.update_notice
        assert notice is not None and notice.buttons[0].text() == "Update"
        version = "1.1.0"
        assert main.start(["--hidden"], app_env) == 0
        qtbot.waitUntil(lambda window=window: window.restart_offer is not None)
        notice.buttons[0].click()
        assert window.restart_offer is None
        transport.calls[-1].finish()
        transport.calls[-1].finish((2, "ember;2.0.0;amd64;ember-stable-main"))
        qtbot.waitUntil(lambda: transport.calls[-1].method == "UpdatePackages")
        install = transport.calls[-1]
        version = "2.0.0"
        (package / "ember_app-1.0.0.dist-info").rename(
            package / "ember_app-2.0.0.dist-info"
        )
        window.hide()
        assert main.start([], app_env) == 0
        qtbot.waitUntil(window.isVisible, timeout=5000)
        qtbot.wait(100)
        assert window.restart_offer is None
        assert restarts == []
        install.finish(error=error)
        install.finish(error=error)
        if error is None:
            assert restarts == [parts]
            assert window.restart_offer is None
        else:
            assert restarts == [] and window.isVisible()
            failure = window.update_notice
            assert failure is not None and error in failure.text_label.text()
            assert "sudo apt install" in failure.text_label.text()
            restored = window.restart_offer
            assert restored is not None and restored.version == "2.0.0"


@pytest.mark.parametrize("refresh_error", [None, "Authorization was declined."])
def test_update_preparation_restores_an_existing_restart_offer(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    refresh_error: str | None,
) -> None:
    version = "1.0.0"
    monkeypatch.setattr(main, "installed_version", lambda: version)
    monkeypatch.setattr(updater, "running_deb", lambda _env: True)
    transport = FakePackageKit()
    monkeypatch.setattr(updater, "QtPackageKit", lambda _parent: transport)
    report = deps.DepsReport(
        components=(EMBER_ROW,),
        installed_read_at={},
        newest_read_at={},
    )
    monkeypatch.setattr(deps, "read", lambda _env, **_kwargs: report)
    restarts: list[main.EmberApp] = []
    monkeypatch.setattr(main, "restart", restarts.append)
    for parts in _start([], app_env, qtbot):
        window = parts.window
        active = parts.updater
        assert active is not None
        qtbot.waitUntil(lambda window=window: window.update_notice is not None)
        notice = window.update_notice
        assert notice is not None
        version = "1.1.0"
        assert main.start(["--hidden"], app_env) == 0
        qtbot.waitUntil(lambda window=window: window.restart_offer is not None)
        notice.buttons[0].click()
        assert window.restart_offer is None
        assert transport.calls[-1].method == "RefreshCache"
        if refresh_error:
            transport.calls[-1].finish(error=refresh_error)
        else:
            transport.calls[-1].finish()
            with qtbot.waitSignal(active.restart_released, timeout=5000):
                transport.calls[-1].finish(
                    (1, "ember;1.1.0;amd64;manual:ember-stable-main"),
                    (2, "ember;3.0.0;amd64;ember-stable-main"),
                )
        restored = window.restart_offer
        assert restored is not None and restored.version == "1.1.0"
        notice = window.update_notice
        assert notice is not None
        if refresh_error:
            assert refresh_error in notice.text_label.text()
        else:
            assert "PackageKit found 3.0.0" in notice.text_label.text()
            assert [button.text() for button in notice.buttons] == ["Update", "Dismiss"]
        assert not any(call.method == "UpdatePackages" for call in transport.calls)
        assert restarts == []


def test_package_folder_is_the_installed_metadata_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # The app conftest replaces package_folder.
    folder = main.package_folder()
    assert folder is not None
    assert any(folder.glob("ember_app-*.dist-info"))


def test_restart_closes_the_socket_starts_the_app_and_quits(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        main.QProcess,
        "startDetached",
        staticmethod(lambda program, args: started.append((program, args)) or True),
    )
    quits: list[bool] = []
    for parts in _start([], app_env, qtbot):
        monkeypatch.setattr(parts.window, "quit_app", lambda: quits.append(True))
        main.restart(parts)
        assert not parts.server.isListening()
        assert started == [(sys.executable, ["-I", "-m", "ember.cli", "app"])]
        assert quits == [True]


def test_activation_token_keeps_only_a_plain_token() -> None:
    assert main.activation_token({main.TOKEN: "gnome-shell/Ember/1234-5"}) == (
        "gnome-shell/Ember/1234-5"
    )
    assert main.activation_token({}) == ""
    assert main.activation_token({main.TOKEN: ""}) == ""
    assert main.activation_token({main.TOKEN: "two words"}) == ""
    assert main.activation_token({main.TOKEN: "a\nshow b"}) == ""
    assert main.activation_token({main.TOKEN: "x" * 513}) == ""


def test_second_start_hands_its_activation_token_to_the_running_window(
    app_env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "installed_version", lambda: "0.4.0")
    for parts in _start([], app_env, qtbot):
        tokens: list[str] = []
        monkeypatch.setattr(parts.window, "bring_forward", tokens.append)
        assert main.start([], {**app_env, main.TOKEN: "dock-token-1"}) == 0
        qtbot.waitUntil(lambda tokens=tokens: tokens == ["dock-token-1"])
        assert main.start([], app_env) == 0
        qtbot.waitUntil(lambda tokens=tokens: tokens == ["dock-token-1", ""])
        assert main.start(["--hidden"], {**app_env, main.TOKEN: "hidden"}) == 0
        qtbot.wait(200)
        assert tokens == ["dock-token-1", ""]


@pytest.mark.parametrize("quit_signal", [False, True])
def test_start_and_stop_collector_and_crash_log(
    app_env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    quit_signal: bool,
) -> None:
    """Startup installs both guards; stop and quitting release them after reads."""
    gc.enable()  # The app test fixture starts with collection off.
    enabled = True
    handlers: list[tuple[int, bool]] = []
    disabled: list[bool] = []

    def enable(*, file: int, all_threads: bool) -> None:
        """Record the descriptor and thread coverage given to faulthandler."""
        handlers.append((file, all_threads))

    def disable() -> None:
        """Check that the descriptor is still open when the handler is disabled."""
        os.fstat(handlers[0][0])
        disabled.append(True)

    monkeypatch.setattr(main.faulthandler, "enable", enable)
    monkeypatch.setattr(main.faulthandler, "disable", disable)
    log = paths.state_dir(app_env) / "app-crash.log"
    log.parent.mkdir(parents=True)
    log.write_text("earlier crash\n")
    log.chmod(0o644)
    for parts in _start([], app_env, qtbot):
        assert not gc.isenabled()
        assert parts.collector.timer.isActive()
        assert len(handlers) == 1
        fd, all_threads = handlers[0]
        assert all_threads
        assert log.stat().st_mode & 0o777 == 0o600
        assert log.parent.stat().st_mode & 0o777 == 0o700
        os.write(fd, b"new crash\n")
        assert log.read_text() == "earlier crash\nnew crash\n"
        # Handing over must not replace or close the first instance's guards.
        assert main.start(["--hidden"], app_env) == 0
        assert len(handlers) == 1
        real_wait = parts.context.tasks.wait

        def wait(msecs: int = -1, real_wait: Callable[[int], bool] = real_wait) -> bool:
            """Check that both guards remain installed while pool jobs finish."""
            assert not gc.isenabled()
            assert not disabled
            return real_wait(msecs)

        monkeypatch.setattr(parts.context.tasks, "wait", wait)
        if quit_signal:
            parts.app.aboutToQuit.emit()
        else:
            main.stop(parts)
        assert gc.isenabled() == enabled
        assert not parts.collector.timer.isActive()
        assert disabled == [True]
        with pytest.raises(OSError):
            os.fstat(fd)
    assert gc.isenabled() == enabled
    gc.disable()  # Back to the fixture's state before teardown delivers events.


def test_crash_log_open_failure_starts_without_it(
    app_env: dict[str, str], capsys: pytest.CaptureFixture[str], qtbot: QtBot
) -> None:
    """An unavailable crash log is reported, and the app starts without it."""
    gc.enable()  # The app test fixture starts with collection off.
    enabled = True
    folder = paths.state_dir(app_env)
    folder.mkdir(parents=True)
    (folder / "app-crash.log").mkdir()
    for parts in _start([], app_env, qtbot):
        assert parts.crash_log is None
        assert parts.collector.timer.isActive()
        assert "cannot open crash log" in capsys.readouterr().err
    assert gc.isenabled() == enabled
    gc.disable()  # Back to the fixture's state before teardown delivers events.


def test_failed_start_restores_gc(
    app_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setup exception stops the collector and closes the crash log."""
    gc.enable()  # The app test fixture starts with collection off.
    enabled = True
    disabled: list[bool] = []
    monkeypatch.setattr(main.faulthandler, "disable", lambda: disabled.append(True))

    def fail() -> bool:
        """Raise during setup after the collector has started."""
        assert not gc.isenabled()
        raise RuntimeError("setup failed")

    monkeypatch.setattr(tray_module, "available", fail)
    with pytest.raises(RuntimeError, match="setup failed"):
        main.start([], app_env)
    assert gc.isenabled() == enabled
    gc.disable()  # Back to the fixture's state before teardown delivers events.
    assert disabled == [True]


def test_native_crash_writes_all_threads_to_state_log(env: dict[str, str]) -> None:
    """A fatal signal writes the main and background Python stacks to the log."""
    script = """
import json
import os
import resource
import sys
import threading
from ember.app.main import _CrashLog

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
log = _CrashLog(json.loads(sys.argv[1]))
ready = threading.Event()
finish = threading.Event()
def waiting_thread():
    ready.set()
    finish.wait()
threading.Thread(target=waiting_thread, daemon=True).start()
ready.wait()
os.abort()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(env)],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode < 0
    contents = (paths.state_dir(env) / "app-crash.log").read_text()
    assert "Fatal Python error: Aborted" in contents
    assert "waiting_thread" in contents
    assert "<module>" in contents
