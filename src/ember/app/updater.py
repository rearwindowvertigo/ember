"""Check the Ember release index and install package updates on request."""

import ctypes
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import SLOT, QObject, QTimer, Signal, Slot
from PySide6.QtDBus import (
    QDBusArgument,
    QDBusConnection,
    QDBusMessage,
    QDBusObjectPath,
    QDBusPendingCallWatcher,
    QDBusServiceWatcher,
)
from shiboken6 import getCppPointer

from ember import deps
from ember.app.context import AppContext

DAY_MS = 24 * 60 * 60 * 1000
_SERVICE = "org.freedesktop.PackageKit"
_MANAGER_PATH = "/org/freedesktop/PackageKit"
_TRANSACTION = f"{_SERVICE}.Transaction"
_INTERACTIVE_METHODS = ("RefreshCache", "UpdatePackages")
# PackageKit's enum values and bitfields from pk-enum.h.
_FILTER_NONE = 1 << 1
_ONLY_TRUSTED = 1 << 1
_SUCCESS = 1
_CANCELLED = 3
_INSTALLED = 1
_AVAILABLE = 2
_TRUSTED = 25

type Argument = bool | int | list[str]


@dataclass(frozen=True)
class TransactionResult:
    """The package signals and any failure from a PackageKit transaction."""

    packages: tuple[tuple[int, str], ...] = ()
    error: str | None = None


class PackageKitTransport(Protocol):
    """Run one PackageKit transaction and deliver its terminal outcome."""

    def transact(
        self,
        method: str,
        arguments: Sequence[Argument],
        done: Callable[[TransactionResult], None],
    ) -> None:
        """Call done on the GUI thread when the transaction ends."""


def _uint64(value: int) -> QDBusArgument:
    # PySide's integer overloads marshal small ints as 16- or 32-bit values.
    # Call Qt 6's QDBusArgument::operator<<(qulonglong) for PackageKit bitfields.
    append = ctypes.CDLL("libQt6DBus.so.6")._ZN13QDBusArgumentlsEy
    append.argtypes = (ctypes.c_void_p, ctypes.c_ulonglong)
    append.restype = ctypes.c_void_p
    argument = QDBusArgument()
    append(getCppPointer(argument)[0], value)
    return argument


class QtPackageKit(QObject):
    """PackageKit transport over the system D-Bus."""

    def transact(
        self,
        method: str,
        arguments: Sequence[Argument],
        done: Callable[[TransactionResult], None],
    ) -> None:
        """Start a transaction and listen for its results without blocking."""
        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            raise RuntimeError(
                "PackageKit is unavailable because the system bus is down."
            )
        args = [
            arg if isinstance(arg, (bool, list)) else _uint64(arg) for arg in arguments
        ]
        _Transaction(bus, method, args, done, self)


class _Transaction(QObject):
    def __init__(
        self,
        bus: QDBusConnection,
        method: str,
        arguments: list[QDBusArgument | bool | list[str]],
        done: Callable[[TransactionResult], None],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._bus = bus
        self._method = method
        self._arguments = arguments
        self._done: Callable[[TransactionResult], None] | None = done
        self._packages: list[tuple[int, str]] = []
        self._error: str | None = None
        self._call(_MANAGER_PATH, _SERVICE, "CreateTransaction", [], self._created)

    def _call(
        self,
        path: str,
        interface: str,
        method: str,
        arguments: list[QDBusArgument | bool | list[str]],
        replied: Callable[[QDBusMessage], None] | None = None,
    ) -> None:
        message = QDBusMessage.createMethodCall(_SERVICE, path, interface, method)
        message.setArguments(arguments)
        message.setInteractiveAuthorizationAllowed(method in _INTERACTIVE_METHODS)
        watcher = QDBusPendingCallWatcher(self._bus.asyncCall(message), self)

        def read_reply() -> None:
            reply = watcher.reply()
            watcher.deleteLater()
            if self._done is None:
                return
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                self._complete(
                    self._error or f"PackageKit {method} failed: {reply.errorMessage()}"
                )
            elif replied is not None:
                replied(reply)

        watcher.finished.connect(read_reply)

    def _created(self, reply: QDBusMessage) -> None:
        args = reply.arguments()
        if len(args) != 1 or not isinstance(args[0], QDBusObjectPath):
            self._complete("PackageKit returned no transaction path.")
            return
        path = args[0].path()
        service = QDBusServiceWatcher(
            _SERVICE,
            self._bus,
            QDBusServiceWatcher.WatchModeFlag.WatchForUnregistration,
            self,
        )
        service.serviceUnregistered.connect(self._service_lost)
        for signal, slot in (
            ("Package", SLOT("_package(uint,QString,QString)")),
            ("ErrorCode", SLOT("_failed(uint,QString)")),
            ("Finished", SLOT("_finished(uint,uint)")),
            ("Destroy", SLOT("_destroyed()")),
        ):
            # PySide accepts SLOT's string, despite the bytes annotation in its stub.
            if not self._bus.connect(
                _SERVICE,
                path,
                _TRANSACTION,
                signal,
                self,
                slot,  # pyrefly: ignore[bad-argument-type]
            ):
                self._complete(f"Could not subscribe to PackageKit's {signal} signal.")
                return

        def start(_reply: QDBusMessage) -> None:
            # A successful method reply acknowledges the request. Finished
            # is the transaction's terminal result.
            self._call(path, _TRANSACTION, self._method, self._arguments)

        if self._method in _INTERACTIVE_METHODS:
            self._call(path, _TRANSACTION, "SetHints", [["interactive=true"]], start)
        else:
            start(reply)

    @Slot("uint", str, str)
    def _package(self, info: int, package_id: str, _summary: str) -> None:
        if self._method == "Resolve":
            self._packages.append((info, package_id))

    @Slot("uint", str)
    def _failed(self, code: int, details: str) -> None:
        self._error = details or f"PackageKit error {code}."

    @Slot("uint", "uint")
    def _finished(self, status: int, _runtime: int) -> None:
        if self._error or status == _SUCCESS:
            self._complete(self._error)
        elif status == _CANCELLED:
            self._complete(f"PackageKit cancelled {self._method}.")
        else:
            self._complete(f"PackageKit {self._method} failed (exit {status}).")

    @Slot()
    def _destroyed(self) -> None:
        self._complete(
            self._error
            or "PackageKit ended the transaction before reporting its result."
        )

    @Slot(str)
    def _service_lost(self, _name: str) -> None:
        self._complete(self._error or "The PackageKit service disconnected.")

    def _complete(self, error: str | None) -> None:
        done, self._done = self._done, None
        if done is not None:
            self.deleteLater()
            done(TransactionResult(tuple(self._packages), error))


@dataclass(frozen=True)
class UpdateNotice:
    """An update, progress message or reason with its available action."""

    title: str
    text: str
    tone: str = "info"
    update: Callable[[], None] | None = None
    dismissible: bool = True


@dataclass(frozen=True)
class _Candidate:
    id: str
    version: str


def _candidate(
    packages: tuple[tuple[int, str], ...],
    installed: str,
    offered: str,
    env: Mapping[str, str],
) -> tuple[_Candidate, str]:
    """Prefer the offered version among the available Ember archive packages."""
    available: dict[str, _Candidate] = {}
    for info, package_id in packages:
        fields = package_id.split(";")
        if len(fields) != 4 or fields[0] != "ember" or fields[2] != "amd64":
            continue
        _, version, _, origin = fields
        if info == _INSTALLED:
            if deps.debian_older(installed, version, env):
                installed = version
        elif info in (_AVAILABLE, _TRUSTED) and origin == "ember-stable-main":
            deps.validate_debian_version(version, env, deps.TIMEOUT)
            available[version] = _Candidate(package_id, version)

    selected = available.get(offered)
    if selected is None:
        for candidate in available.values():
            if selected is None or deps.debian_older(
                selected.version, candidate.version, env
            ):
                selected = candidate
    if selected is None:
        raise deps.ReadError(
            "PackageKit found no Ember amd64 candidate in the Ember archive."
        )
    if not deps.debian_older(installed, selected.version, env):
        raise deps.ReadError(
            f"PackageKit found Ember {selected.version}. "
            f"The installed version is {installed}."
        )
    return selected, installed


def running_deb(env: Mapping[str, str]) -> bool:
    """Return whether dpkg owns the updater module in Ember's packaged runtime."""
    module = Path(__file__).resolve()
    if not module.is_relative_to("/usr/lib/ember"):
        return False
    binary = shutil.which("dpkg-query", path=env.get("PATH"))
    if binary is None:
        return False
    try:
        code, out, _err = deps.run_command(
            [binary, "-S", str(module)], env, deps.TIMEOUT
        )
    except deps.ReadError:
        return False
    return code == 0 and out.strip() in (f"ember: {module}", f"ember:amd64: {module}")


def _manual_install(version: str | None) -> str:
    # Release assets use the project's X.Y.Z version, with no Debian suffix.
    release = (
        version
        if version and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        else "X.Y.Z"
    )
    return (
        "To install a release manually, run these commands in an empty folder. "
        "Set version to the release number.\n\n"
        f"version={release}\n"
        'curl -fLO "https://github.com/rearwindowvertigo/ember/releases/download/'
        'v${version}/ember_${version}_amd64.deb"\n'
        'sudo apt install "./ember_${version}_amd64.deb"'
    )


class Updater(QObject):
    """Schedule Ember checks and manage one owner-approved install at a time."""

    notice_changed = Signal(object)
    install_started = Signal()
    restart_released = Signal()

    def __init__(
        self,
        context: AppContext,
        version: str,
        restart: Callable[[], None],
        *,
        transport: PackageKitTransport | None = None,
    ) -> None:
        """Connect checks to the settings snapshot and the shared dependency read."""
        super().__init__(context)
        self._context = context
        self._version = version
        self._restart = restart
        self._transport = QtPackageKit(self) if transport is None else transport
        self._enabled: bool | None = None
        self._check_id = 0
        self._busy = False
        self._restarting = False
        self._stopped = False
        self._timer = QTimer(self)
        self._timer.setInterval(DAY_MS)
        self._timer.timeout.connect(self._check)
        context.settings_changed.connect(self._settings_changed)
        context.ember_checked.connect(self._checked)

    def stop(self) -> None:
        """Stop scheduling reads and delivering update results."""
        self._stopped = True
        self._timer.stop()

    def holds_restart(self) -> bool:
        """Return whether the updater is handling the package change."""
        return self._busy or self._restarting

    def _settings_changed(self) -> None:
        enabled = self._context.updates_enabled()
        if self._stopped or enabled == self._enabled:
            return
        first = self._enabled is None
        self._enabled = enabled
        self._check_id += 1
        if first:
            self._timer.start()
        if enabled:
            self._check()
        else:
            self.notice_changed.emit(None)
            self._context.refresh_deps(check_now=first)

    def _check(self) -> None:
        if self._enabled is not None and not self._stopped:
            self._context.refresh_deps(check_now=True)

    def _checked(self, result: deps.Component | str) -> None:
        if not self._enabled or self._stopped or self.holds_restart():
            return
        self._check_id += 1
        check_id = self._check_id
        newest = None if isinstance(result, str) else result.newest
        if newest is None:
            reason = result if isinstance(result, str) else result.error
            self._failure(
                "Ember could not check for updates",
                reason or "No release version was returned.",
            )
            return
        env = self._context.env
        assert isinstance(result, deps.Component)

        def compare() -> tuple[str, bool, bool]:
            packaged = running_deb(env)
            installed = result.installed if packaged else self._version
            if installed is None:
                raise deps.ReadError(
                    result.error or "The installed Ember version is unknown."
                )
            return installed, packaged, deps.debian_older(installed, newest, env)

        def compared(outcome: tuple[str, bool, bool]) -> None:
            if self._stopped or check_id != self._check_id:
                return
            installed, packaged, newer = outcome
            if newer:
                action = (lambda: self._begin(newest, installed)) if packaged else None
                text = f"Installed version {installed}. Available version {newest}."
                if action is None:
                    text += f"\n\n{_manual_install(newest)}"
                self.notice_changed.emit(
                    UpdateNotice("Ember update available", text, update=action)
                )
            else:
                self.notice_changed.emit(None)

        def failed(error: Exception) -> None:
            if not self._stopped and check_id == self._check_id:
                self._failure("Ember could not check for updates", str(error), newest)

        self._context.tasks.run(compare, compared, failed)

    def _failure(self, title: str, reason: str, version: str | None = None) -> None:
        self.notice_changed.emit(
            UpdateNotice(
                title, f"{reason}\n\n{_manual_install(version)}", tone="danger"
            )
        )

    def _begin(self, offered: str, installed: str) -> None:
        if not self._enabled or self._stopped or self.holds_restart():
            return
        self._busy = True
        self._check_id += 1
        self.install_started.emit()
        self.notice_changed.emit(
            UpdateNotice(
                "Preparing Ember update",
                "Refreshing package information.",
                dismissible=False,
            )
        )
        self._transaction(
            "RefreshCache", [True], offered, lambda _: self._resolve(offered, installed)
        )

    def _resolve(self, offered: str, installed: str) -> None:
        self._transaction(
            "Resolve",
            [_FILTER_NONE, ["ember:amd64"]],
            offered,
            lambda result: self._select(result, offered, installed),
        )

    def _select(self, result: TransactionResult, offered: str, installed: str) -> None:
        env = self._context.env

        def select() -> tuple[_Candidate, str]:
            return _candidate(result.packages, installed, offered, env)

        def selected(outcome: tuple[_Candidate, str]) -> None:
            if self._stopped:
                return
            candidate, current = outcome
            if candidate.version == offered:
                self._install(candidate)
                return
            if self._enabled:
                self.notice_changed.emit(
                    UpdateNotice(
                        "The available Ember version changed",
                        f"Installed version {current}. PackageKit found "
                        f"{candidate.version} after refreshing. The update offered "
                        f"before the refresh was {offered}.",
                        update=lambda: self._begin(candidate.version, current),
                    )
                )
            self._release_restart()

        self._context.tasks.run(
            select, selected, lambda error: self._install_failed(offered, str(error))
        )

    def _transaction(
        self,
        method: str,
        arguments: Sequence[Argument],
        version: str,
        done: Callable[[TransactionResult], None],
    ) -> None:
        def completed(result: TransactionResult) -> None:
            if self._stopped or not self._busy:
                return
            if result.error is not None:
                self._install_failed(version, result.error)
            else:
                done(result)

        try:
            self._transport.transact(method, arguments, completed)
        except (OSError, RuntimeError, AttributeError) as error:
            self._install_failed(version, str(error))

    def _install(self, candidate: _Candidate) -> None:
        self.notice_changed.emit(
            UpdateNotice(
                "Updating Ember",
                f"Installing Ember {candidate.version}.",
                dismissible=False,
            )
        )
        self._transaction(
            "UpdatePackages",
            [_ONLY_TRUSTED, [candidate.id]],
            candidate.version,
            self._installed,
        )

    def _install_failed(self, version: str, reason: str) -> None:
        if self._stopped:
            return
        self._failure("Ember could not install the update", reason, version)
        self._release_restart()

    def _release_restart(self) -> None:
        self._busy = False
        self.restart_released.emit()

    def _installed(self, _result: TransactionResult) -> None:
        self._busy = False
        self._restarting = True
        self._restart()
