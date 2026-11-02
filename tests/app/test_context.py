from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QDialog, QWidget
from pytestqt.qtbot import QtBot

from ember import choices, db, deps, terminals
from ember.app import context as context_module
from ember.app.context import AppContext
from ember.engines import status

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


def test_open_run_terminal_reports_the_failure(
    env: dict[str, str], qtbot: QtBot
) -> None:
    context = AppContext(env)
    with qtbot.waitSignal(context.error, timeout=5000) as blocker:
        context.open_run_terminal("codex-20260101-120000-abcdef")
    assert "codex-20260101-120000-abcdef" in _first(blocker.args)


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
