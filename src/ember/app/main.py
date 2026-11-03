"""`emb app`: start the desktop app, or bring the running one forward (J1).

One instance runs at a time. It listens on a local socket,
`<runtime dir>/app.sock`; a second start connects, sends `show` (nothing with
`--hidden`) and exits 0, and the first instance raises its window on `show`.
On every such connection the running instance compares its own version with
the installed one; when they differ, its window offers a restart.
Two starts at the same moment can both find no instance listening; both then
run, and only the later one to listen is reachable on the socket.
"""

import argparse
import importlib.metadata
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QFileSystemWatcher,
    QObject,
    QProcess,
    QTimer,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from ember import autostart, paths
from ember.app import icons, theme, widgets
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

QUIT_WAIT_MS = 12_000
"""How long quitting waits for background reads to end.

Longer than the 10-second deadline of every engine and dependency read.
"""

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


def installed_version() -> str | None:
    """Return the installed `ember-app` version, read from disk each time."""
    try:
        return importlib.metadata.version("ember-app")
    except importlib.metadata.PackageNotFoundError:
        return None


UPGRADE_SETTLE_MS = 2_000
"""Milliseconds without further package changes before the version is read."""


def package_folder() -> Path | None:
    """Return the folder that holds the installed `ember-app` metadata.

    An upgrade replaces the `ember_app-<version>.dist-info` folder in it.
    """
    try:
        folder = importlib.metadata.distribution("ember-app").locate_file("")
    except importlib.metadata.PackageNotFoundError:
        return None
    path = Path(str(folder))
    return path if path.is_dir() else None


def restart_command() -> list[str]:
    """Return the command that starts the installed Ember's app."""
    return [sys.executable, "-I", "-m", "ember.cli", "app"]


@dataclass
class EmberApp:
    """The parts of a running app."""

    app: QApplication
    context: AppContext
    window: MainWindow
    server: QLocalServer
    tray: tray_module.Tray | None
    autostart_reason: str | None = None
    version: str | None = None
    """The version this process started as."""


class _Controller(QObject):
    """Connects the parts: the socket, settings, theme and autostart."""

    def __init__(self, parts: EmberApp) -> None:
        super().__init__(parts.context)
        self.parts = parts
        self._first_settings = True
        self._autostart_on: bool | None = None
        parts.server.newConnection.connect(self._connection)
        parts.context.settings_changed.connect(self._settings_changed)
        # An upgrade installed while Ember runs shows the restart offer once
        # the package folder has been still for a moment.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(UPGRADE_SETTLE_MS)
        self._settle.timeout.connect(self._check_version)
        self._watcher = QFileSystemWatcher(self)
        self._package = package_folder()
        if self._package is not None:
            self._watcher.addPath(str(self._package))
        self._watcher.directoryChanged.connect(self._package_changed)
        parts.app.aboutToQuit.connect(self._stop_watching)
        parts.window.destroyed.connect(self._stop_watching)
        hints = parts.app.styleHints()
        hints.colorSchemeChanged.connect(self._scheme_changed)

    def _connection(self) -> None:
        server = self.parts.server
        self._check_version()
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

    def _stop_watching(self) -> None:
        self._package = None
        self._settle.stop()
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())

    def _package_changed(self, _path: str) -> None:
        if self._package is None:
            return
        if str(self._package) not in self._watcher.directories():
            self._watcher.addPath(str(self._package))
        self._settle.start()

    def _check_version(self) -> None:
        """Offer a restart when the installed version is not this one."""
        installed = installed_version()
        if installed is None or installed == self.parts.version:
            return
        self.parts.window.show_restart(installed, lambda: restart(self.parts))

    def _apply_theme(self) -> None:
        setting = str(self.parts.context.setting("app.theme") or "system")
        mode = theme.resolve_mode(setting, self.parts.app)
        if mode != theme.current_mode() or self._first_settings:
            theme.apply(self.parts.app, mode)

    def _settings_changed(self, _snapshot: dict) -> None:
        self._apply_theme()
        self._first_settings = False
        on = self.parts.context.setting("app.autostart") != "off"
        if on == self._autostart_on:
            return
        self._autostart_on = on
        self.parts.autostart_reason = autostart.sync(on, self.parts.context.env)
        self.parts.context.set_autostart_reason(self.parts.autostart_reason)
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
    widgets.install_focus_tracking(app)
    widgets.install_tooltips(app)

    context = AppContext(environment)

    def quit_app() -> None:
        # The window and the icon go at once; `stop` waits for the reads.
        window.hide()
        if tray is not None:
            tray.hide()
        app.quit()

    window = MainWindow(context, has_tray=has_tray, quit_app=quit_app)

    def open_overview() -> None:
        window.show_screen("overview")
        window.bring_forward()

    tray = (
        tray_module.Tray(context, window.bring_forward, open_overview, window.quit_app)
        if has_tray
        else None
    )
    parts = EmberApp(app, context, window, server, tray, version=installed_version())
    _Controller(parts)
    if tray is not None:
        tray.show()
    if not args.hidden or tray is None:
        window.show()

    context.reload_settings()
    context.refresh_engines()
    context.refresh_deps(check_now=True)
    context.runs.start()
    context.flows.set_active(True)
    return parts


def restart(parts: EmberApp) -> None:
    """Close the socket, start the installed Ember and quit this one.

    Runs keep running in their tmux sessions.
    """
    parts.server.close()
    QProcess.startDetached(restart_command()[0], restart_command()[1:])
    parts.window.quit_app()


def stop(parts: EmberApp) -> None:
    """Hide the window and the tray, close the socket, and wait for reads.

    The window and the tray icon are hidden first. The wait, up to
    `QUIT_WAIT_MS`, lets running background reads end before the interpreter
    shuts down.

    Args:
        parts: The running app.
    """
    parts.window.hide()
    if parts.tray is not None:
        parts.tray.hide()
    parts.context.runs.stop()
    parts.context.flows.set_active(False)
    parts.server.close()
    QCoreApplication.processEvents()
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
