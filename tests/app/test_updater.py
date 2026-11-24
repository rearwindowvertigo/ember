from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from unittest.mock import Mock

import pytest
from PySide6.QtDBus import QDBusMessage, QDBusObjectPath, QDBusPendingCall
from pytestqt.qtbot import QtBot

from ember import deps
from ember.app import updater
from ember.app.context import AppContext

OFFERED = "2.0.0"
RUNNING = "1.0.0"
EMBER_ROW = deps.Component(
    "ember",
    "Ember",
    "apps",
    deps.UPDATE_AVAILABLE,
    RUNNING,
    OFFERED,
    None,
    None,
    None,
    None,
)


def _package(version: str) -> str:
    return f"ember;{version};amd64;ember-stable-main"


@dataclass
class Call:
    """A PackageKit method accepted by the transport, awaiting its result."""

    method: str
    arguments: Sequence[updater.Argument]
    done: Callable[[updater.TransactionResult], None]

    def finish(self, *packages: tuple[int, str], error: str | None = None) -> None:
        """Deliver the terminal signal."""
        self.done(updater.TransactionResult(packages, error))


class FakePackageKit:
    """Accept calls and let the test deliver their results separately."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def transact(
        self,
        method: str,
        arguments: Sequence[updater.Argument],
        done: Callable[[updater.TransactionResult], None],
    ) -> None:
        """Record a method request."""
        self.calls.append(Call(method, arguments, done))


@dataclass
class Harness:
    """The updater, background tasks and its observed UI state."""

    context: AppContext
    updates: updater.Updater
    transport: FakePackageKit
    notices: list[updater.UpdateNotice | None]
    restart: Mock
    qtbot: QtBot

    def offer(self, row: deps.Component = EMBER_ROW) -> updater.UpdateNotice:
        """Deliver a shared index read and wait for its notice."""
        with self.qtbot.waitSignal(self.updates.notice_changed, timeout=5000):
            self.context.ember_checked.emit(row)
        notice = self.notices[-1]
        assert notice is not None
        return notice

    def resolve(self, *packages: tuple[int, str]) -> None:
        """Confirm the initial offer, refresh, then deliver resolve results."""
        offer = self.offer()
        assert offer.update is not None
        offer.update()
        assert [(call.method, call.arguments) for call in self.transport.calls] == [
            ("RefreshCache", [True]),
        ]
        self.transport.calls[-1].finish()
        resolve = self.transport.calls[-1]
        assert resolve.method == "Resolve"
        assert resolve.arguments == [2, ["ember:amd64"]]
        with self.qtbot.waitSignal(self.updates.notice_changed, timeout=5000):
            resolve.finish(*packages)


@pytest.fixture
def harness(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> Harness:
    """Start checks with an empty stub report and a fake transport."""
    report = deps.DepsReport(components=(), installed_read_at={}, newest_read_at={})
    monkeypatch.setattr(deps, "read", lambda _env, **_kwargs: report)
    monkeypatch.setattr(updater, "running_deb", lambda _env: True)
    context = AppContext(env)
    transport = FakePackageKit()
    restart = Mock()
    updates = updater.Updater(context, RUNNING, restart, transport=transport)
    notices: list[updater.UpdateNotice | None] = []
    updates.notice_changed.connect(notices.append)
    with qtbot.waitSignal(context.deps_changed, timeout=5000):
        context.reload_settings()
    return Harness(context, updates, transport, notices, restart, qtbot)


@pytest.mark.parametrize("error", [None, "The password prompt was declined."])
def test_only_finished_success_restarts_once(
    harness: Harness, error: str | None
) -> None:
    harness.resolve(
        (2, "other;9.0.0;amd64;ember-stable-main"),
        (2, "ember;9.0.0;arm64;ember-stable-main"),
        (2, "ember;9.0.0;amd64;another-repo"),
        (24, _package(OFFERED)),
        (1, "ember;1.0.0;amd64;manual:local"),
        (2, _package("1.1.0")),
        (2, _package(OFFERED)),
        (2, _package("3.0.0")),
    )
    install = harness.transport.calls[-1]
    assert (install.method, install.arguments) == (
        "UpdatePackages",
        [2, [_package(OFFERED)]],
    )
    assert harness.updates.holds_restart()
    harness.restart.assert_not_called()
    harness.qtbot.wait(20)
    harness.restart.assert_not_called()
    install.finish(error=error)
    install.finish(error=error)
    if error is None:
        harness.restart.assert_called_once_with()
    else:
        assert not harness.updates.holds_restart()
        harness.restart.assert_not_called()
        notice = harness.notices[-1]
        assert notice is not None
        assert error in notice.text
        assert "version=2.0.0" in notice.text
        assert "curl -fLO" in notice.text
        assert "sudo apt install" in notice.text


@pytest.mark.parametrize("changed", ["1.5.0", "3.0.0"])
def test_changed_candidate_requires_fresh_confirmation(
    harness: Harness, changed: str
) -> None:
    harness.resolve((2, _package("1.0.1")), (2, _package(changed)))
    notice = harness.notices[-1]
    assert notice is not None
    assert all(version in notice.text for version in (RUNNING, OFFERED, changed))
    assert [call.method for call in harness.transport.calls] == [
        "RefreshCache",
        "Resolve",
    ]
    assert notice.update is not None
    notice.update()
    assert harness.transport.calls[-1].method == "RefreshCache"
    harness.transport.calls[-1].finish()
    assert harness.transport.calls[-1].method == "Resolve"
    with harness.qtbot.waitSignal(harness.updates.notice_changed, timeout=5000):
        harness.transport.calls[-1].finish((2, _package(changed)))
    assert harness.transport.calls[-1].method == "UpdatePackages"
    assert harness.transport.calls[-1].arguments == [2, [_package(changed)]]
    harness.restart.assert_not_called()


@pytest.mark.parametrize(
    "packages",
    [
        ((2, _package("0.9.0")),),
        ((2, _package(RUNNING)),),
        ((1, "ember;2.0.0;amd64;manual:ember-stable-main"), (2, _package(OFFERED))),
        ((24, _package(OFFERED)),),
    ],
)
def test_equal_older_or_untrusted_candidate_installs_nothing(
    harness: Harness, packages: tuple[tuple[int, str], ...]
) -> None:
    harness.resolve(*packages)
    assert [call.method for call in harness.transport.calls] == [
        "RefreshCache",
        "Resolve",
    ]
    notice = harness.notices[-1]
    assert notice is not None
    assert notice.tone == "danger"
    assert "PackageKit found" in notice.text
    assert "sudo apt install" in notice.text


def test_missing_packagekit_shows_the_reason(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harness.transport,
        "transact",
        Mock(side_effect=RuntimeError("The PackageKit service is unavailable.")),
    )
    offer = harness.offer()
    assert offer.update is not None
    offer.update()
    notice = harness.notices[-1]
    assert notice is not None
    assert "The PackageKit service is unavailable." in notice.text
    assert "sudo apt install" in notice.text
    harness.restart.assert_not_called()


def test_failed_index_shows_the_reason_and_manual_commands(harness: Harness) -> None:
    notice = harness.offer(replace(EMBER_ROW, newest=None, error="index: HTTP 503"))
    assert notice.tone == "danger"
    assert "index: HTTP 503" in notice.text and "version=X.Y.Z" in notice.text
    assert "curl -fLO" in notice.text and "sudo apt install" in notice.text
    assert notice.update is None


def test_packaged_runtime_with_a_current_deb_has_no_update_offer(
    harness: Harness,
) -> None:
    harness.offer()
    current = replace(EMBER_ROW, status=deps.CURRENT, installed=OFFERED)
    with harness.qtbot.waitSignal(harness.updates.notice_changed, timeout=5000):
        harness.context.ember_checked.emit(current)
    assert harness.notices[-1] is None
    assert harness.transport.calls == []
    harness.restart.assert_not_called()


def test_packaged_runtime_reports_the_installed_deb_version(harness: Harness) -> None:
    notice = harness.offer(replace(EMBER_ROW, installed="1.5.0"))
    assert "Installed version 1.5.0. Available version 2.0.0." in notice.text
    assert notice.update is not None
    notice.update()
    harness.transport.calls[-1].finish()
    with harness.qtbot.waitSignal(harness.updates.notice_changed, timeout=5000):
        harness.transport.calls[-1].finish((2, _package("1.3.0")))
    assert [call.method for call in harness.transport.calls] == [
        "RefreshCache",
        "Resolve",
    ]
    failure = harness.notices[-1]
    assert failure is not None and "The installed version is 1.5.0." in failure.text


def test_running_development_installation_only_gets_manual_commands(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater, "running_deb", lambda _env: False)
    notice = harness.offer(replace(EMBER_ROW, installed="1.9.0"))
    assert "Installed version 1.0.0. Available version 2.0.0." in notice.text
    assert "sudo apt install" in notice.text
    assert notice.update is None
    assert harness.transport.calls == []


@pytest.mark.parametrize("owner", ["ember", "ember:amd64", "another-package"])
def test_running_deb_checks_the_module_owner(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    module = "/usr/lib/ember/lib/python3.14/site-packages/ember/app/updater.py"
    monkeypatch.setattr(updater, "__file__", module)

    def query(args: list[str], *_args: object) -> tuple[int, str, str]:
        assert args[1:] == ["-S", module]
        return 0, f"{owner}: {module}\n", ""

    monkeypatch.setattr(deps, "run_command", query)
    assert updater.running_deb(env) == (owner in ("ember", "ember:amd64"))


@pytest.mark.parametrize("status", [1, 2])
def test_qt_transaction_waits_for_finished_and_keeps_error_code(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    bus = Mock()
    messages: list[QDBusMessage] = []

    def async_call(message: QDBusMessage) -> QDBusPendingCall:
        messages.append(message)
        reply = (
            message.createReply(QDBusObjectPath("/transaction"))
            if len(messages) == 1
            else message.createReply()
        )
        return QDBusPendingCall.fromCompletedCall(reply)

    bus.asyncCall.side_effect = async_call
    bus.connect.return_value = True
    monkeypatch.setattr(updater.QDBusConnection, "systemBus", lambda: bus)
    monkeypatch.setattr(updater, "QDBusServiceWatcher", Mock())
    results: list[updater.TransactionResult] = []
    transport = updater.QtPackageKit()
    transport.transact("UpdatePackages", [2, [_package(OFFERED)]], results.append)
    qtbot.waitUntil(lambda: len(messages) == 3)
    assert results == []
    assert [message.member() for message in messages] == [
        "CreateTransaction",
        "SetHints",
        "UpdatePackages",
    ]
    assert messages[1].arguments() == [["interactive=true"]]
    assert messages[-1].arguments()[0].currentSignature() == "t"
    assert messages[-1].isInteractiveAuthorizationAllowed()
    assert [call.args[3] for call in bus.connect.call_args_list] == [
        "Package",
        "ErrorCode",
        "Finished",
        "Destroy",
    ]
    receiver = bus.connect.call_args_list[-1].args[4]
    if status != 1:
        receiver._failed(48, "Authentication was cancelled.")
    qtbot.wait(20)
    assert results == []
    receiver._finished(status, 10)
    receiver._finished(status, 10)
    expected = None if status == 1 else "Authentication was cancelled."
    assert results == [updater.TransactionResult(error=expected)]
