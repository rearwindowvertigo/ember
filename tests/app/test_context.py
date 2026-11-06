import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QProcessEnvironment
from PySide6.QtWidgets import QDialog, QWidget
from pytestqt.qtbot import QtBot

from ember import choices, db, deps, terminals, tmux
from ember.app import context as context_module
from ember.app.context import AppContext
from ember.engines import status

from .test_runs import make_run, write_run  # pyrefly: ignore[missing-import]

type _SettingsTask = tuple[
    Callable[[], Any], Callable[[Any], None], Callable[[Exception], None]
]


def _hold_tasks(
    context: AppContext, monkeypatch: pytest.MonkeyPatch
) -> list[_SettingsTask]:
    """Hold tasks for synchronous delivery of their outcomes."""
    tasks: list[_SettingsTask] = []

    def run(
        work: Callable[[], Any],
        done: Callable[[Any], None],
        failed: Callable[[Exception], None],
    ) -> None:
        """Keep the task and its callbacks in submission order."""
        tasks.append((work, done, failed))

    monkeypatch.setattr(context.tasks, "run", run)
    return tasks


def _ready(env: dict[str, str], qtbot: QtBot) -> AppContext:
    context = AppContext(env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    return context


def _first(args: list[Any] | None) -> Any:  # noqa: ANN401 - a signal's argument
    assert args is not None
    return args[0]


def test_reload_reads_defaults_without_a_database(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = AppContext(env)
    assert not context.settings_ready()
    assert context.settings() == {}
    assert context.setting("app.theme") is None
    with qtbot.waitSignal(context.settings_changed, timeout=5000) as blocker:
        context.reload_settings()
    assert context.settings_ready()
    assert _first(blocker.args)["generation"] == context.settings()["generation"]
    assert context.setting("app.theme") == "system"
    assert context.setting("terminal") == "xdg-terminal-exec"


def test_save_settings_calls_back_after_success(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = _ready(env, qtbot)
    saved: list[dict[str, Any]] = []
    results: list[dict[str, Any] | Exception] = []
    order: list[str] = []
    context.settings_changed.connect(lambda _s: order.append("settings"))

    def done(snapshot: dict[str, Any]) -> None:
        """Record the success callback before the result callback."""
        saved.append(snapshot)
        order.append("done")

    def result(outcome: dict[str, Any] | Exception) -> None:
        """Record the outcome and its callback order."""
        results.append(outcome)
        order.append("result")

    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.save_settings({"app.theme": "dark"}, done, result=result)
    assert len(saved) == 1
    assert context.setting("app.theme") == "dark"
    assert saved[0] is context.settings()
    assert results == saved
    assert results[0] is context.settings()
    assert order == ["settings", "done", "result"]


def test_save_after_another_caller_claims_and_finishes(
    env: dict[str, str], qtbot: QtBot
) -> None:
    choices.set_settings({"app.theme": "light"}, env=env)
    context = _ready(env, qtbot)
    snapshot = context.settings()
    assert snapshot["once"] is None
    claim = choices.claim("run-000001", env=env)
    assert claim["claim"]["status"] == "none"
    choices.finish("run-000001", env=env)
    assert context.settings() is snapshot
    saved: list[dict[str, Any]] = []
    with (
        qtbot.assertNotEmitted(context.stale),
        qtbot.assertNotEmitted(context.error),
        qtbot.waitSignal(context.settings_changed, timeout=5000),
    ):
        context.save_settings({"app.theme": "dark"}, saved.append)
    assert saved == [context.settings()]
    assert context.setting("app.theme") == "dark"
    assert context.settings()["generation"] == snapshot["generation"] + 1
    assert choices.snapshot(env=env)["settings"]["app.theme"] == "dark"


@pytest.mark.parametrize(
    "error",
    [
        choices.InvalidChoiceError("invalid"),
        db.DatabaseBusyError("busy"),
        OSError("write failed"),
    ],
)
def test_save_result_reports_its_error_before_the_next_save(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Each sent failure emits one error and receives its own exception."""
    context = _ready(env, qtbot)
    tasks = _hold_tasks(context, monkeypatch)
    results: list[dict[str, Any] | Exception] = []
    done: list[dict[str, Any]] = []
    messages: list[str] = []
    context.error.connect(messages.append)

    def result(outcome: dict[str, Any] | Exception) -> None:
        """Receive the failure before any following task is submitted."""
        assert tasks == []
        assert len(messages) == 1
        results.append(outcome)

    context.save_settings({"app.theme": "dark"}, done.append, result=result)
    context.save_settings({"app.theme": "light"}, result=results.append)
    _, _, failed = tasks.pop(0)
    failed(error)
    assert results == [error]
    assert done == []
    work, saved, _ = tasks.pop(0)
    saved(work())
    assert len(messages) == 1
    assert results == [error, context.settings()]
    assert tasks == []


def test_snapshot_failure_is_the_save_result(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful write with an unreadable snapshot reports the read error."""
    context = _ready(env, qtbot)
    snapshot = choices.snapshot
    error = OSError("snapshot failed")
    results: list[dict[str, Any] | Exception] = []
    done: list[dict[str, Any]] = []
    messages: list[str] = []
    context.error.connect(messages.append)

    def read(*, env: object) -> dict[str, Any]:
        """Fail the snapshot read after the settings write."""
        raise error

    monkeypatch.setattr(choices, "snapshot", read)
    context.save_settings({"app.theme": "dark"}, done.append, result=results.append)
    qtbot.waitUntil(lambda: context.tasks.pending() == 0)
    assert results == [error]
    assert done == []
    assert len(messages) == 1
    assert snapshot(env=env)["answers"]["app.theme"]["value"] == "dark"


@pytest.mark.parametrize("reload_succeeds", [False, True])
def test_stale_results_finish_in_order_and_keep_saves_queued_during_reload(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    reload_succeeds: bool,
) -> None:
    """Stale callbacks cover the refused batch after either reload outcome."""
    context = _ready(env, qtbot)
    tasks = _hold_tasks(context, monkeypatch)
    error = db.StaleGenerationError("changed elsewhere")
    results: list[tuple[int, dict[str, Any] | Exception]] = []
    done: list[dict[str, Any]] = []
    order: list[str] = []
    context.settings_changed.connect(lambda _s: order.append("settings"))
    context.settings_failed.connect(lambda _s: order.append("read failed"))
    context.stale.connect(lambda: order.append("stale"))
    context.error.connect(lambda _s: order.append("error"))

    def result(index: int) -> Callable[[dict[str, Any] | Exception], None]:
        """Build a callback recording the save's index and outcome."""

        def record(outcome: dict[str, Any] | Exception) -> None:
            """Record the callback after the reload outcome."""
            results.append((index, outcome))
            order.append(f"result {index}")

        return record

    for index in range(3):
        context.save_settings({"app.theme": "light"}, done.append, result=result(index))
    _, _, refused = tasks.pop(0)
    refused(error)
    assert results == []
    context.save_settings({"worker.network": "on"}, result=result(3))
    assert len(tasks) == 1
    read, loaded, failed = tasks.pop(0)
    if reload_succeeds:
        choices.set_settings({"app.theme": "dark"}, env=env)
        loaded(read())
    else:
        failed(OSError("reload failed"))
    assert results == [(0, error), (1, error), (2, error)]
    assert done == []
    assert order == [
        "settings" if reload_succeeds else "read failed",
        "result 0",
        "result 1",
        "result 2",
        *(["stale"] if reload_succeeds else []),
    ]
    assert len(tasks) == 1
    work, saved, _ = tasks.pop(0)
    saved(work())
    assert context.setting("worker.network") == "on"
    assert context.setting("app.theme") == ("dark" if reload_succeeds else "system")
    assert results[-1] == (3, context.settings())
    assert len(results) == 4
    assert tasks == []


@pytest.mark.parametrize("callback", ["done", "result", "failed result"])
def test_callback_exception_propagates_after_the_next_save_starts(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    callback: str,
) -> None:
    """A raising save callback still delivers the result and advances saves."""
    context = _ready(env, qtbot)
    tasks = _hold_tasks(context, monkeypatch)
    results: list[dict[str, Any] | Exception] = []
    error = RuntimeError("callback failed")

    def raises(outcome: dict[str, Any] | Exception) -> None:
        """Record the callback's outcome and raise its exception."""
        results.append(outcome)
        raise error

    context.save_settings(
        {"app.theme": "dark"},
        raises if callback == "done" else None,
        result=results.append if callback == "done" else raises,
    )
    context.save_settings({"worker.network": "on"}, result=results.append)
    work, saved, failed = tasks.pop(0)
    with pytest.raises(RuntimeError) as raised:
        if callback == "failed result":
            failed(OSError("write failed"))
        else:
            saved(work())
    assert raised.value is error
    assert len(results) == (2 if callback == "done" else 1)
    assert len(tasks) == 1
    work, saved, _ = tasks.pop(0)
    saved(work())
    assert context.setting("worker.network") == "on"
    assert tasks == []


@pytest.mark.parametrize("raising_save", [0, 1])
@pytest.mark.parametrize("reload_succeeds", [False, True])
def test_stale_callback_exception_still_notifies_the_batch_and_starts_next_save(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    raising_save: int,
    reload_succeeds: bool,
) -> None:
    """A raising stale callback leaves every affected result delivered once."""
    context = _ready(env, qtbot)
    tasks = _hold_tasks(context, monkeypatch)
    results: list[dict[str, Any] | Exception] = []
    stale_error = db.StaleGenerationError("changed elsewhere")
    callback_error = RuntimeError("callback failed")
    stale: list[bool] = []
    context.stale.connect(lambda: stale.append(True))

    def raises(outcome: dict[str, Any] | Exception) -> None:
        """Record this stale result before raising."""
        results.append(outcome)
        raise callback_error

    for index in range(3):
        context.save_settings(
            {"app.theme": "light"},
            result=raises if index == raising_save else results.append,
        )
    _, _, refused = tasks.pop(0)
    refused(stale_error)
    context.save_settings({"worker.network": "on"})
    read, loaded, failed = tasks.pop(0)
    with pytest.raises(RuntimeError) as raised:
        if reload_succeeds:
            loaded(read())
        else:
            failed(OSError("reload failed"))
    assert raised.value is callback_error
    assert results == [stale_error] * 3
    assert stale == ([True] if reload_succeeds else [])
    assert len(tasks) == 1
    work, saved, _ = tasks.pop(0)
    saved(work())
    assert context.setting("worker.network") == "on"
    assert tasks == []


def test_stale_save_reloads_then_emits_stale(env: dict[str, str], qtbot: QtBot) -> None:
    choices.set_settings({"app.theme": "light"}, env=env)
    context = _ready(env, qtbot)
    assert context.setting("app.theme") == "light"
    choices.set_settings({"app.theme": "dark"}, env=env)
    order: list[str] = []
    context.settings_changed.connect(lambda _s: order.append("settings"))
    context.stale.connect(lambda: order.append("stale"))
    saved: list[dict[str, Any]] = []
    with qtbot.waitSignal(context.stale, timeout=5000):
        context.save_settings({"worker.network": "on"}, saved.append)
    assert order == ["settings", "stale"]
    assert saved == []
    assert context.setting("app.theme") == "dark"
    assert context.setting("worker.network") == "off"


def test_quick_saves_wait_and_use_the_new_generation(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = _ready(env, qtbot)
    first = context.settings()["generation"]
    stale: list[bool] = []
    context.stale.connect(lambda: stale.append(True))
    done: list[int] = []
    context.save_settings({"app.theme": "dark"}, lambda s: done.append(1))
    context.save_settings({"worker.network": "on"}, lambda s: done.append(2))
    context.save_settings({"app.theme": "light"}, lambda s: done.append(3))
    qtbot.waitUntil(lambda: done == [1, 2, 3], timeout=5000)
    assert stale == []
    assert context.setting("app.theme") == "light"
    assert context.setting("worker.network") == "on"
    assert context.settings()["generation"] > first


def test_stale_save_drops_the_saves_behind_it(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = _ready(env, qtbot)
    choices.set_settings({"app.theme": "dark"}, env=env)
    done: list[int] = []
    with qtbot.waitSignal(context.stale, timeout=5000):
        context.save_settings({"worker.network": "on"}, lambda s: done.append(1))
        context.save_settings({"app.theme": "light"}, lambda s: done.append(2))
    context.tasks.wait(5000)
    assert done == []
    assert context.setting("app.theme") == "dark"
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.save_settings({"worker.network": "on"}, lambda s: done.append(3))
    assert done == [3]


def test_a_failed_save_lets_the_next_one_run(env: dict[str, str], qtbot: QtBot) -> None:
    context = _ready(env, qtbot)
    done: list[int] = []
    with qtbot.waitSignal(context.error, timeout=5000):
        context.save_settings({"app.theme": "purple"}, lambda s: done.append(1))
        context.save_settings({"app.theme": "dark"}, lambda s: done.append(2))
    qtbot.waitUntil(lambda: done == [2], timeout=5000)
    assert context.setting("app.theme") == "dark"


def test_invalid_value_emits_error(env: dict[str, str], qtbot: QtBot) -> None:
    context = _ready(env, qtbot)
    with qtbot.waitSignal(context.error, timeout=5000) as blocker:
        context.save_settings({"app.theme": "purple"})
    assert _first(blocker.args).startswith("The setting was not saved:")


def test_unreadable_settings_emit_settings_failed(
    env: dict[str, str], qtbot: QtBot
) -> None:
    config = Path(env["XDG_CONFIG_HOME"]) / "ember"
    config.mkdir(parents=True)
    (config / "settings.db").write_text("not a database")
    context = AppContext(env)
    with qtbot.waitSignal(context.settings_failed, timeout=5000) as blocker:
        context.reload_settings()
    assert "could not read its settings" in _first(blocker.args)
    assert not context.settings_ready()


def test_refresh_engines_reads_and_coalesces(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def read(_env: object, *, refresh: bool = False) -> dict[str, Any]:
        calls.append(refresh)
        return {"codex": "stub"}

    monkeypatch.setattr(status, "read", read)
    context = AppContext(env)
    results: list[dict[str, Any]] = []
    context.engines_changed.connect(results.append)
    for _ in range(4):
        context.refresh_engines()
    qtbot.waitUntil(lambda: len(results) == 2 and context.tasks.pending() == 0)
    assert calls == [True, True]
    assert context.engines() == {"codex": "stub"}


def test_turn_failed_refreshes_engines(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(status, "read", lambda _env, *, refresh=False: {})
    context = AppContext(env)
    with qtbot.waitSignal(context.engines_changed, timeout=5000):
        context.runs.turn_failed.emit("codex")


@pytest.mark.parametrize(
    ("check_now", "installed", "newest"), [(True, True, "due"), (False, False, "never")]
)
def test_refresh_deps(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    check_now: bool,
    installed: bool,
    newest: str,
) -> None:
    seen: list[tuple[bool, str]] = []
    report = deps.DepsReport(components=(), installed_read_at={}, newest_read_at={})

    def read(_env: object, *, installed: bool, newest: str) -> deps.DepsReport:
        seen.append((installed, newest))
        return report

    monkeypatch.setattr(deps, "read", read)
    context = AppContext(env)
    with qtbot.waitSignal(context.deps_changed, timeout=5000) as blocker:
        context.refresh_deps(check_now)
    assert blocker.args == [report]
    assert context.deps() is report
    assert seen == [(installed, newest)]


def test_refresh_deps_failure(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read(_env: object, **_kwargs: object) -> None:
        raise deps.ComponentsError("components.toml: bad")

    monkeypatch.setattr(deps, "read", read)
    context = AppContext(env)
    with qtbot.waitSignal(context.deps_failed, timeout=5000) as blocker:
        context.refresh_deps(True)
    assert "components.toml: bad" in _first(blocker.args)
    assert context.deps() is None


def _accept(monkeypatch: pytest.MonkeyPatch, accepted: bool) -> list[str]:
    shown: list[str] = []
    code = QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    def run_dialog(dialog: QDialog) -> int:
        shown.append(dialog.command.text())  # type: ignore[attr-defined]
        return code

    monkeypatch.setattr(context_module.RunCommandDialog, "exec", run_dialog)
    return shown


def test_run_command_cancelled_runs_nothing(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready(env, qtbot)
    shown = _accept(monkeypatch, False)
    started: list[list[str]] = []
    monkeypatch.setattr(
        context_module, "_start_detached", lambda a, _e: started.append(a)
    )
    parent = QWidget()
    qtbot.addWidget(parent)
    assert context.run_command(parent, "codex update") is False
    assert shown == ["codex update"]
    qtbot.wait(50)
    assert started == []


def test_run_command_starts_the_terminal(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "args.txt"
    term = Path(env["PATH"].split(":")[0]) / "term"
    term.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}\n')
    term.chmod(0o755)
    choices.set_settings({"terminal": f"{term} -e"}, env=env)
    context = _ready(env, qtbot)
    _accept(monkeypatch, True)
    parent = QWidget()
    qtbot.addWidget(parent)
    assert context.run_command(parent, "sudo apt install tmux") is True
    qtbot.waitUntil(lambda: out.exists() and out.read_text().count("\n") >= 6)
    lines = out.read_text().splitlines()
    assert lines[0] == "-e"
    assert lines[-1] == "sudo apt install tmux"
    assert terminals.WRAP.splitlines()[0] in lines


def test_run_command_start_failure_names_the_terminal(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    choices.set_settings({"terminal": "no-such-terminal --"}, env=env)
    context = _ready(env, qtbot)
    _accept(monkeypatch, True)
    parent = QWidget()
    qtbot.addWidget(parent)
    with qtbot.waitSignal(context.error, timeout=5000) as blocker:
        assert context.run_command(parent, "codex update") is True
    assert "'no-such-terminal --'" in _first(blocker.args)
    assert "Pick another terminal in Settings." in _first(blocker.args)


def test_run_command_leaves_the_native_activation_token_alone(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings commands use the context snapshot and do not consume a token."""
    context = _ready(env, qtbot)
    tasks = _hold_tasks(context, monkeypatch)
    _accept(monkeypatch, True)
    parent = QWidget()
    qtbot.addWidget(parent)
    name = "XDG_ACTIVATION_TOKEN"
    monkeypatch.setenv(name, "python-cached-token")
    os.putenv(name, "tray-click-token")
    popen = Mock()
    monkeypatch.setattr(context_module.subprocess, "Popen", popen)

    assert context.run_command(parent, "codex update") is True
    work, _done, _failed = tasks[0]
    work()

    assert popen.call_args.kwargs["env"] == env
    assert name not in env
    assert QProcessEnvironment.systemEnvironment().value(name) == "tray-click-token"


def test_open_run_terminal_reports_the_failure(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = AppContext(env)
    with qtbot.waitSignal(context.error, timeout=5000) as blocker:
        context.open_run_terminal("codex-20260101-120000-abcdef")
    assert "codex-20260101-120000-abcdef" in _first(blocker.args)


def test_open_run_terminal_calls_on_failure_after_the_error(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = AppContext(env)
    order: list[str] = []
    context.error.connect(lambda _text: order.append("error"))
    with qtbot.waitSignal(context.error, timeout=5000):
        context.open_run_terminal(
            "codex-20260101-120000-abcdef", lambda: order.append("failure")
        )
    qtbot.waitUntil(lambda: order == ["error", "failure"])


@pytest.mark.parametrize("token", [None, "", "tray-click-token"])
@pytest.mark.parametrize("snapshot_token", [None, "startup-token"])
def test_open_run_terminal_consumes_the_native_token_once(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    snapshot_token: str | None,
) -> None:
    """The terminal gets this click's native token once, on its own copy."""
    name = "XDG_ACTIVATION_TOKEN"
    monkeypatch.setenv(name, "python-cached-token")
    if token is None:
        os.unsetenv(name)
    else:
        os.putenv(name, token)
    if snapshot_token is not None:
        env[name] = snapshot_token
    snapshot = dict(env)
    choices.set_settings({"terminal": "test-terminal --"}, env=env)
    run = write_run(make_run(env))
    monkeypatch.setattr(tmux, "exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tmux, "pane_dead", lambda *_args, **_kwargs: False)
    popen = Mock(return_value=Mock(pid=123))
    monkeypatch.setattr(tmux.subprocess, "Popen", popen)
    context = AppContext(env)
    tasks = _hold_tasks(context, monkeypatch)

    context.open_run_terminal(run.id)
    assert not QProcessEnvironment.systemEnvironment().contains(name)
    context.open_run_terminal(run.id)
    assert len(tasks) == 2
    for work, _done, _failed in tasks:
        assert work() == (0, "")

    first, second = popen.call_args_list
    assert first.args[0][:2] == ["test-terminal", "--"]
    assert first.args[0][-3:] == ["attach-session", "-t", f"={run.id}"]
    expected = {key: value for key, value in snapshot.items() if key != name}
    assert first.kwargs["env"] == ({**expected, name: token} if token else expected)
    assert second.kwargs["env"] == expected
    assert context.env is env
    assert env == snapshot
    assert os.environ[name] == "python-cached-token"


def test_open_run_terminal_keeps_each_queued_clicks_token(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each queued attachment keeps the token available when clicked."""
    name = "XDG_ACTIVATION_TOKEN"
    monkeypatch.setenv(name, "python-cached-token")
    context = AppContext(env)
    tasks = _hold_tasks(context, monkeypatch)
    attach = Mock(return_value=(0, ""))
    monkeypatch.setattr(context_module, "_attach", attach)

    for token in ("first-click-token", "second-click-token"):
        os.putenv(name, token)
        context.open_run_terminal("codex-20260101-120000-abcdef")
        assert not QProcessEnvironment.systemEnvironment().contains(name)
    for work, _done, _failed in reversed(tasks):
        work()

    assert [call.args[0][name] for call in attach.call_args_list] == [
        "second-click-token",
        "first-click-token",
    ]
    assert name not in env


def test_show_screen_and_visibility(env: dict[str, str], qtbot: QtBot) -> None:
    context = AppContext(env)
    with qtbot.waitSignal(context.screen_requested) as blocker:
        context.show_screen("engines")
    assert blocker.args == ["engines"]
    with qtbot.waitSignal(context.visible_changed) as blocker:
        context.set_visible(True)
    assert blocker.args == [True]
    assert context.visible()
    with qtbot.assertNotEmitted(context.visible_changed):
        context.set_visible(True)


def test_autostart_reason_is_kept_and_signalled(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = AppContext(env)
    assert context.autostart_reason() is None
    with qtbot.waitSignal(context.autostart_reason_changed) as blocker:
        context.set_autostart_reason("emb is not on PATH")
    assert blocker.args == ["emb is not on PATH"]
    assert context.autostart_reason() == "emb is not on PATH"
    with qtbot.waitSignal(context.autostart_reason_changed) as blocker:
        context.set_autostart_reason(None)
    assert blocker.args == [None]
    assert context.autostart_reason() is None


def test_flows_default_inactive_and_injectable(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = AppContext(env)
    assert not context.flows.active()
    assert not context.flows.loaded()
    assert context.tasks.pending() == 0
    other = AppContext(env, flows=context.flows)
    assert other.flows is context.flows
    with qtbot.assertNotEmitted(context.flows.changed, wait=50):
        context.flows.reload()
    assert context.tasks.pending() == 0


def test_defaults_and_changed_compare_saved_values_even_with_invalid_panel(
    env: dict[str, str], qtbot: QtBot
) -> None:
    """Defaults accompany each read; invalid answers do not mask saved changes."""
    choices.set_settings({"app.theme": "dark"}, env=env)
    with db.open_write(env=env) as conn:
        assert conn is not None
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("panel.members", '"invalid"'),
        )
    context = _ready(env, qtbot)
    assert context.settings()["errors"]
    assert context.setting_changed("app.theme")
    assert not context.setting_changed("worker.network")
    assert context.settings()["defaults"] == {
        key: spec.default(env) for key, spec in choices.KEYS.items()
    }


def test_clear_shares_save_queue_and_waits_for_confirmed_snapshot(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaved saves and clears use each preceding generation and callbacks."""
    choices.set_settings({"app.theme": "dark"}, env=env)
    context = _ready(env, qtbot)
    tasks = _hold_tasks(context, monkeypatch)
    results: list[dict[str, Any] | Exception] = []
    order: list[str] = []
    context.settings_changed.connect(lambda _s: order.append("settings"))

    def done(_snapshot: dict[str, Any]) -> None:
        """Record completion after the snapshot is published."""
        order.append("done")

    context.save_settings({"worker.network": "on"})
    context.clear_settings(["app.theme"], done, result=results.append)
    context.clear_settings(["app.theme"])
    context.save_settings({"app.theme": "light"})
    assert len(tasks) == 1
    assert context.setting_changed("app.theme")
    work, saved, _ = tasks.pop(0)
    saved(work())
    assert context.setting_changed("app.theme")
    work, saved, _ = tasks.pop(0)
    snapshot = work()
    assert context.setting_changed("app.theme")
    saved(snapshot)
    assert not context.setting_changed("app.theme")
    assert "app.theme" not in context.settings()["settings"]
    assert order == ["settings", "settings", "done"]
    assert results == [snapshot]
    while tasks:
        work, saved, _ = tasks.pop(0)
        saved(work())
    assert context.setting("app.theme") == "light"
    assert context.setting("worker.network") == "on"


@pytest.mark.parametrize("stale", [False, True])
def test_clear_failure_keeps_marker_and_reports_existing_signals(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    """A refused clear preserves the saved value and uses the save failure path."""
    choices.set_settings({"app.theme": "dark"}, env=env)
    context = _ready(env, qtbot)
    tasks = _hold_tasks(context, monkeypatch)
    results: list[dict[str, Any] | Exception] = []
    messages: list[str] = []
    context.error.connect(messages.append)
    context.stale.connect(lambda: messages.append("stale"))
    context.clear_settings(["app.theme"], result=results.append)
    context.save_settings({"worker.network": "on"}, result=results.append)
    work, _, failed = tasks.pop(0)
    if stale:
        choices.set_settings({"app.theme": "light"}, env=env)
        with pytest.raises(db.StaleGenerationError) as caught:
            work()
        error = caught.value
    else:
        error = db.DatabaseBusyError("busy")
    failed(error)
    assert context.setting_changed("app.theme")
    while tasks:
        work, loaded, _ = tasks.pop(0)
        loaded(work())
    assert context.setting_changed("app.theme")
    assert results[0] is error
    assert len(messages) == 1
    if stale:
        assert results == [error, error]
        assert messages == ["stale"]
        assert context.setting("app.theme") == "light"
    else:
        assert context.setting("worker.network") == "on"
        assert "busy" in messages[0]
