import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from ember import autostart, deps
from ember.app import main, theme
from ember.app import tray as tray_module
from ember.engines import status

EMPTY_REPORT = deps.DepsReport(components=(), installed_read_at={}, newest_read_at={})


@pytest.fixture
def app_env(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, qapp: QApplication
) -> Iterator[dict[str, str]]:
    """An environment with a Qt platform, and stub engine and dependency reads."""
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
    started = main.start([], app_env)
    assert isinstance(started, main.EmberApp)
    qtbot.addWidget(started.window)
    qtbot.waitUntil(started.context.settings_ready, timeout=5000)
    tray = started.tray
    assert tray is not None
    waits: list[tuple[int, bool, bool, bool]] = []
    real_wait = started.context.tasks.wait

    def wait(msecs: int = -1) -> bool:
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
