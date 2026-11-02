"""What every screen shares: data, background reads and saves, and actions.

`AppContext` holds the last settings snapshot, engine status and dependency
report, the run records, and the task runner. Screens read the last values
and listen to the signals; every read and save runs in a task.
"""

import io
import subprocess
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QWidget

from ember import choices, db, terminals
from ember import deps as dependencies
from ember.app.runs import RunsModel
from ember.app.tasks import TaskRunner
from ember.app.widgets import RunCommandDialog
from ember.engines import status
from ember.runs import core as run_core

STALE_TITLE = "Settings changed elsewhere."
"""The first line of the alert shown after a save refused as stale."""

STALE_TEXT = "This screen now shows the new values. Make your change again."
"""The rest of that alert."""


def _start_detached(argv: list[str], env: Mapping[str, str]) -> None:
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=dict(env),
    )


def _attach(env: Mapping[str, str], run_id: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    core = run_core.Core(env, quiet=True, out=out, err=err)
    code = core.execute(lambda: core.attach(run_id))
    return code, err.getvalue().strip()


class AppContext(QObject):
    """The app's shared state and actions."""

    settings_changed = Signal(dict)
    """A new settings snapshot was read."""

    settings_failed = Signal(str)
    """The settings could not be read, with what happened and what to do."""

    stale = Signal()
    """A save was refused because the settings changed elsewhere; the new
    snapshot was read first."""

    error = Signal(str)
    """An action failed, with what happened and what to do."""

    engines_changed = Signal(dict)
    """New engine status was read."""

    engines_failed = Signal(str)
    """Engine status could not be read."""

    deps_changed = Signal(object)
    """A new dependency report was read."""

    deps_failed = Signal(str)
    """The dependency report could not be read."""

    screen_requested = Signal(str)
    """A screen asked the window to show another screen."""

    visible_changed = Signal(bool)
    """The window was shown or hidden."""

    autostart_reason_changed = Signal(object)
    """The autostart entry was written or removed: why it could not be, or
    `None`."""

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        tasks: TaskRunner | None = None,
        runs: RunsModel | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Create the context; nothing is read until asked.

        Args:
            env: The environment every read and command uses.
            tasks: The task runner; `None` creates one on the global pool.
            runs: The run model; `None` creates one.
            parent: The parent object.
        """
        super().__init__(parent)
        self.env = env
        self.tasks = TaskRunner(parent=self) if tasks is None else tasks
        self.runs = RunsModel(env, self.tasks, parent=self) if runs is None else runs
        self.runs.turn_failed.connect(self._turn_failed)
        self._settings: dict[str, Any] | None = None
        self._engines: dict[str, status.EngineStatus] | None = None
        self._deps: dependencies.DepsReport | None = None
        self._engines_busy = False
        self._engines_queued = False
        self._deps_busy = False
        self._deps_queued: bool | None = None
        self._visible = False
        self._autostart_reason: str | None = None
        self._save_queue: deque[
            tuple[
                dict[str, object],
                Callable[[dict[str, Any]], None] | None,
                Callable[[dict[str, Any] | Exception], None] | None,
            ]
        ] = deque()
        self._saving = False

    # Settings

    def settings_ready(self) -> bool:
        """Return whether a settings snapshot was read."""
        return self._settings is not None

    def settings(self) -> dict[str, Any]:
        """Return the last `choices.snapshot` result; empty before the first."""
        return {} if self._settings is None else self._settings

    def setting(self, name: str) -> Any:  # noqa: ANN401 - settings are typed per key
        """Return a setting's answer from the last snapshot.

        Args:
            name: A key of `choices.KEYS`.

        Returns:
            The value in effect, or `None` before the first snapshot.
        """
        answer = self.settings().get("answers", {}).get(name)
        return None if answer is None else answer.get("value")

    def reload_settings(self, done: Callable[[], None] | None = None) -> None:
        """Read the settings again in a task and emit `settings_changed`.

        Args:
            done: Called after `settings_changed` when the read succeeds.
        """
        self._read_settings(done, None)

    def _read_settings(
        self,
        done: Callable[[], None] | None,
        after: Callable[[bool], None] | None,
    ) -> None:
        """Read settings; `after` receives whether the read succeeded."""
        env = self.env

        def loaded(snapshot: dict[str, Any]) -> None:
            """Publish the snapshot and notify the read's callbacks."""
            self._settings = snapshot
            self.settings_changed.emit(snapshot)
            try:
                if done is not None:
                    done()
            finally:
                if after is not None:
                    after(True)

        def failed(error: Exception) -> None:
            """Report the read failure and notify its completion callback."""
            self.settings_failed.emit(
                f"Ember could not read its settings: {error}. "
                "Try again; if this keeps happening, check the settings "
                "database's folder."
            )
            if after is not None:
                after(False)

        self.tasks.run(lambda: choices.snapshot(env=env), loaded, failed)

    def save_settings(
        self,
        values: dict[str, object],
        done: Callable[[dict[str, Any]], None] | None = None,
        *,
        result: Callable[[dict[str, Any] | Exception], None] | None = None,
    ) -> None:
        """Save standing settings in a task, one save at a time.

        A save asked for while another is in flight waits for it, then is
        sent with the generation of the snapshot read after it. On success
        the new snapshot is read, `settings_changed` is emitted and `done`
        is called with it, followed by `result`. A save refused as stale drops
        the saves waiting behind it and reloads the settings. Each affected
        save's `result` receives the stale error in queue order, then `stale`
        is emitted if the reload succeeded. Saves queued during that reload
        run afterwards. Any other failure emits `error` and calls `result`
        with the exception. The next waiting save runs even if a callback
        raises.

        Args:
            values: Key to typed value.
            done: Called with the new snapshot after this save succeeds.
            result: Called once with the new snapshot or this save's exception.
        """
        self._save_queue.append((values, done, result))
        if not self._saving:
            self._next_save()

    def _next_save(self) -> None:
        """Send the next save and deliver its callbacks before advancing."""
        if not self._save_queue:
            self._saving = False
            return
        self._saving = True
        values, done, result = self._save_queue.popleft()
        env = self.env
        expect = self.settings().get("generation")

        def save() -> dict[str, Any]:
            """Write this save and read its new snapshot."""
            choices.set_settings(values, expect_generation=expect, env=env)
            return choices.snapshot(env=env)

        def saved(snapshot: dict[str, Any]) -> None:
            """Publish the snapshot and call both success callbacks."""
            self._settings = snapshot
            self.settings_changed.emit(snapshot)
            try:
                try:
                    if done is not None:
                        done(snapshot)
                finally:
                    if result is not None:
                        result(snapshot)
            finally:
                self._next_save()

        def failed(error: Exception) -> None:
            """Report this save's error and finish every refused save."""
            if isinstance(error, db.StaleGenerationError):
                affected = [result, *(entry[2] for entry in self._save_queue)]
                self._save_queue.clear()

                def reloaded(succeeded: bool) -> None:
                    """Notify refused saves after the reload, then resume saves."""
                    callback_error: Exception | None = None
                    try:
                        for callback in affected:
                            if callback is not None:
                                try:
                                    callback(error)
                                except Exception as exc:
                                    if callback_error is None:
                                        callback_error = exc
                        if callback_error is not None:
                            raise callback_error
                    finally:
                        try:
                            if succeeded:
                                self.stale.emit()
                        finally:
                            self._next_save()

                self._read_settings(None, reloaded)
                return
            try:
                if isinstance(error, choices.InvalidChoiceError):
                    self.error.emit(f"The setting was not saved: {error}")
                elif isinstance(error, db.DatabaseBusyError):
                    self.error.emit(
                        "The setting was not saved: the settings database is busy. "
                        "Try again."
                    )
                else:
                    self.error.emit(
                        f"The setting was not saved: {error}. Try again; if this "
                        "keeps happening, check the settings database's folder."
                    )
                if result is not None:
                    result(error)
            finally:
                self._next_save()

        self.tasks.run(save, saved, failed)

    # Engines

    def engines(self) -> dict[str, status.EngineStatus] | None:
        """Return the last engine status, or `None` before the first read."""
        return self._engines

    def refresh_engines(self) -> None:
        """Check every engine in a task and emit `engines_changed`.

        A refresh asked for while one runs follows it once.
        """
        if self._engines_busy:
            self._engines_queued = True
            return
        self._engines_busy = True
        env = self.env

        def loaded(result: dict[str, status.EngineStatus]) -> None:
            self._engines = result
            self._engines_done()
            self.engines_changed.emit(result)

        def failed(error: Exception) -> None:
            self._engines_done()
            self.engines_failed.emit(f"Ember could not check the engines: {error}")

        self.tasks.run(lambda: status.read(env, refresh=True), loaded, failed)

    def _engines_done(self) -> None:
        self._engines_busy = False
        if self._engines_queued:
            self._engines_queued = False
            self.refresh_engines()

    def _turn_failed(self, _engine: str) -> None:
        self.refresh_engines()

    # Dependencies

    def deps(self) -> dependencies.DepsReport | None:
        """Return the last dependency report, or `None` before the first."""
        return self._deps

    def deps_refreshing(self) -> bool:
        """Return whether a dependency read is in flight or waiting."""
        return self._deps_busy

    def refresh_deps(self, check_now: bool) -> None:
        """Read the dependency report in a task and emit `deps_changed`.

        Args:
            check_now: Read installed versions, and newest versions when
                due; otherwise show the cached reads only.
        """
        if self._deps_busy:
            self._deps_queued = bool(self._deps_queued) or check_now
            return
        self._deps_busy = True
        env = self.env

        def read() -> dependencies.DepsReport:
            if check_now:
                return dependencies.read(env, installed=True, newest="due")
            return dependencies.read(env, installed=False, newest="never")

        def loaded(report: dependencies.DepsReport) -> None:
            self._deps = report
            self._deps_done()
            self.deps_changed.emit(report)

        def failed(error: Exception) -> None:
            self._deps_done()
            self.deps_failed.emit(
                f"Ember could not read its dependencies: {error}. "
                "Reinstall Ember if this keeps happening."
            )

        self.tasks.run(read, loaded, failed)

    def _deps_done(self) -> None:
        self._deps_busy = False
        queued, self._deps_queued = self._deps_queued, None
        if queued is not None:
            self.refresh_deps(queued)

    # Actions

    def run_command(self, parent: QWidget, command: str) -> bool:
        """Ask before running a command in the owner's terminal (design J5).

        On Run in terminal the command starts detached in a new window of
        the `terminal` setting's terminal. A start failure emits `error`.

        Args:
            parent: The dialog's parent.
            command: The shell command.

        Returns:
            True when the owner chose Run in terminal.
        """
        dialog = RunCommandDialog(command, parent)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return False
        terminal = str(self.setting("terminal") or terminals.KNOWN[0].prefix)
        env = self.env

        def start() -> None:
            _start_detached(terminals.command_argv(terminal, command), env)

        def failed(error: Exception) -> None:
            self.error.emit(
                f"The terminal command {terminal!r} could not start: {error}. "
                "Pick another terminal in Settings."
            )

        self.tasks.run(start, lambda _result: None, failed)
        return True

    def open_run_terminal(self, run_id: str) -> None:
        """Open a terminal window on a run's session, as `emb run --attach`.

        A failure emits `error` with what `emb run` reported.

        Args:
            run_id: The run.
        """
        env = self.env

        def opened(result: tuple[int, str]) -> None:
            code, text = result
            if code:
                self.error.emit(text or f"Ember could not open {run_id} (exit {code}).")

        def failed(error: Exception) -> None:
            self.error.emit(f"Ember could not open {run_id}: {error}")

        self.tasks.run(lambda: _attach(env, run_id), opened, failed)

    def show_screen(self, key: str) -> None:
        """Ask the window to show a screen.

        Args:
            key: A key of `ember.app.screens.SCREENS`.
        """
        self.screen_requested.emit(key)

    def visible(self) -> bool:
        """Return whether the window is shown."""
        return self._visible

    def set_visible(self, visible: bool) -> None:
        """Record whether the window is shown and emit `visible_changed`.

        Args:
            visible: The window is shown.
        """
        if visible != self._visible:
            self._visible = visible
            self.visible_changed.emit(visible)

    # Start at login

    def autostart_reason(self) -> str | None:
        """Return why the last autostart change could not be made, or `None`."""
        return self._autostart_reason

    def set_autostart_reason(self, reason: str | None) -> None:
        """Record the result of writing or removing the autostart entry.

        Emits `autostart_reason_changed` each time, so a screen can show the
        result of the change it asked for.

        Args:
            reason: Why the entry could not be written or removed, or `None`.
        """
        self._autostart_reason = reason
        self.autostart_reason_changed.emit(reason)
