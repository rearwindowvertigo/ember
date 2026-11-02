from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QDialog, QWidget
from pytestqt.qtbot import QtBot

from ember import choices, deps, terminals
from ember.app import context as context_module
from ember.app.context import AppContext
from ember.engines import status


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
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.save_settings({"app.theme": "dark"}, saved.append)
    assert len(saved) == 1
    assert context.setting("app.theme") == "dark"
    assert saved[0] is context.settings()


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
