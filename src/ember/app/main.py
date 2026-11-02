"""`emb app`: start the desktop app, or bring the running one forward (J1).

One instance runs at a time. It listens on a local socket,
`<runtime dir>/app.sock`; a second start connects, sends `show` (nothing with
`--hidden`) and exits 0, and the first instance raises its window on `show`.
"""

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from ember import autostart, paths
from ember.app import icons, theme
from ember.app import tray as tray_module
from ember.app.context import AppContext
from ember.app.window import MainWindow

APP_ID = "io.github.rearwindowvertigo.Ember"
"""The app ID, also the desktop entry's name."""

SOCKET_NAME = "app.sock"
"""The single-instance socket's name in Ember's runtime folder."""

SHOW = b"show\n"
"""What a second start sends to bring the window forward."""

EX_OK = 0
EX_USAGE = 64
EX_UNAVAILABLE = 69

CONNECT_MS = 1000
"""How long a second start waits to reach the running instance."""

QUIT_WAIT_MS = 3000
"""How long quitting waits for background reads to end."""

NO_DISPLAY = "emb app: no graphical session (WAYLAND_DISPLAY and DISPLAY are unset)"


def socket_path(env: Mapping[str, str]) -> Path:
    """Return the single-instance socket's path.

    Args:
        env: The environment.

    Returns:
        `app.sock` in Ember's runtime folder.

    Raises:
        RuntimeDirError: No runtime base directory is available.
    """
    return paths.runtime_dir(env) / SOCKET_NAME


def has_display(env: Mapping[str, str]) -> bool:
    """Return whether a graphical session or a Qt platform is set."""
    return any(
        env.get(name) for name in ("WAYLAND_DISPLAY", "DISPLAY", "QT_QPA_PLATFORM")
    )


def hand_over(path: Path, *, hidden: bool) -> bool:
    """Reach a running instance and ask it to show its window.

    Args:
        path: The single-instance socket.
        hidden: Send nothing, so the running window stays as it is.

    Returns:
        True when an instance was listening.
    """
    client = QLocalSocket()
    client.connectToServer(str(path))
    if not client.waitForConnected(CONNECT_MS):
        return False
    if not hidden:
        client.write(SHOW)
        client.waitForBytesWritten(CONNECT_MS)
    client.disconnectFromServer()
    if client.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        client.waitForDisconnected(CONNECT_MS)
    return True


@dataclass
class EmberApp:
    """The parts of a running app."""

    app: QApplication
    context: AppContext
    window: MainWindow
    server: QLocalServer
    tray: tray_module.Tray | None
    autostart_reason: str | None = None


class _Controller(QObject):
    """Connects the parts: the socket, settings, theme and autostart."""

    def __init__(self, parts: EmberApp) -> None:
        super().__init__(parts.context)
        self.parts = parts
        self._first_settings = True
        parts.server.newConnection.connect(self._connection)
        parts.context.settings_changed.connect(self._settings_changed)
        hints = parts.app.styleHints()
        hints.colorSchemeChanged.connect(self._scheme_changed)

    def _connection(self) -> None:
        server = self.parts.server
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        data = bytes(socket.readAll().data())
        if b"show" in data:
            self.parts.window.bring_forward()

    def _apply_theme(self) -> None:
        setting = str(self.parts.context.setting("app.theme") or "system")
        mode = theme.resolve_mode(setting, self.parts.app)
        if mode != theme.current_mode() or self._first_settings:
            theme.apply(self.parts.app, mode)

    def _settings_changed(self, _snapshot: dict) -> None:
        self._apply_theme()
        if self._first_settings:
            self._first_settings = False
            on = self.parts.context.setting("app.autostart") != "off"
            self.parts.autostart_reason = autostart.sync(on, self.parts.context.env)
            if self.parts.autostart_reason:
                print(f"emb app: {self.parts.autostart_reason}", file=sys.stderr)

    def _scheme_changed(self, *_args: Any) -> None:  # noqa: ANN401 - Qt passes the scheme
        if (self.parts.context.setting("app.theme") or "system") == "system":
            mode = theme.resolve_mode("system", self.parts.app)
            if mode != theme.current_mode():
                theme.apply(self.parts.app, mode)


def _parse(argv: list[str]) -> argparse.Namespace | None:
    parser = argparse.ArgumentParser(
        prog="emb app", add_help=False, exit_on_error=False
    )
    parser.add_argument("--hidden", action="store_true")
    try:
        args, extra = parser.parse_known_args(argv)
    except argparse.ArgumentError:
        return None
    return None if extra else args


def start(argv: list[str], env: Mapping[str, str] | None = None) -> EmberApp | int:
    """Set up the app without running its event loop.

    Args:
        argv: The arguments after `emb app`: `--hidden` or nothing.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The running parts, or an exit code when the app does not start
        here: 0 when a running instance took over, 64 for a usage error, 69
        without a graphical session or runtime folder.
    """
    environment: Mapping[str, str] = os.environ if env is None else env
    args = _parse(argv)
    if args is None:
        print(f"emb app: unknown arguments: {' '.join(argv)}", file=sys.stderr)
        return EX_USAGE
    if not has_display(environment):
        print(NO_DISPLAY, file=sys.stderr)
        return EX_UNAVAILABLE
    try:
        folder = paths.runtime_dir(environment)
        paths.ensure_private_dir(folder, root=folder)
    except (paths.RuntimeDirError, OSError, ValueError) as error:
        print(f"emb app: {error}", file=sys.stderr)
        return EX_UNAVAILABLE
    path = folder / SOCKET_NAME
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(["emb"])
    if hand_over(path, hidden=args.hidden):
        return EX_OK
    QLocalServer.removeServer(str(path))
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(str(path)):
        print(
            f"emb app: cannot listen on {path}: {server.errorString()}", file=sys.stderr
        )
        return EX_UNAVAILABLE

    app.setApplicationName("Ember")
    QGuiApplication.setDesktopFileName(APP_ID)
    app.setWindowIcon(icons.ember_icon())
    has_tray = tray_module.available()
    app.setQuitOnLastWindowClosed(not has_tray)
    theme.apply(app, theme.resolve_mode("system", app))

    context = AppContext(environment)
    window = MainWindow(context, has_tray=has_tray, quit_app=app.quit)
    tray = (
        tray_module.Tray(context, window.bring_forward, window.quit_app)
        if has_tray
        else None
    )
    parts = EmberApp(app, context, window, server, tray)
    _Controller(parts)
    if tray is not None:
        tray.show()
    if not args.hidden or tray is None:
        window.show()

    context.reload_settings()
    context.refresh_engines()
    context.refresh_deps(check_now=True)
    context.runs.start()
    return parts


def stop(parts: EmberApp) -> None:
    """Close the socket and the tray, and wait briefly for background reads.

    Args:
        parts: The running app.
    """
    parts.context.runs.stop()
    parts.server.close()
    if parts.tray is not None:
        parts.tray.hide()
    parts.context.tasks.wait(QUIT_WAIT_MS)


def run(argv: list[str], env: Mapping[str, str] | None = None) -> int:
    """Run `emb app` until Quit Ember.

    Args:
        argv: The arguments after `emb app`: `--hidden` or nothing.
        env: The environment; `None` reads `os.environ`.

    Returns:
        The exit code: 0 after Quit Ember or when a running instance took
        over, 64 for a usage error, 69 without a graphical session.
    """
    started = start(argv, env)
    if isinstance(started, int):
        return started
    try:
        started.app.exec()
    finally:
        stop(started)
    return EX_OK
