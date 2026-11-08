"""The Settings screen: start at login, theme, and how workers run (design J2).

General holds Start at login and Theme; Workers holds Worker network,
Terminal and Sandbox read paths. Worktree preparation holds each repository's
preparation command. Each change saves at once.
The app's controller writes or removes the autostart entry after
`app.autostart` changes; this screen shows why it could not, when it could
not.
"""

import dataclasses
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ember import choices, sandbox, terminals
from ember.app import icons, state, theme, widgets
from ember.app.screens.common import (
    ToneLine,
    TooltipRow,
    align_controls,
    chain_tab_order,
    clear_layout,
    label,
    screen_header,
    scrolled,
    titled_card,
)

if TYPE_CHECKING:
    from ember.app.context import AppContext

SUBTITLE = "How Ember starts and looks, and how workers run."

THEMES = (("system", "System"), ("light", "Light"), ("dark", "Dark"))
"""The `app.theme` choices."""

SETTING_KEYS = {
    "Start at login": "app.autostart",
    "Theme": "app.theme",
    "Worker network": "worker.network",
    "Terminal": "terminal",
}
"""The single-value rows and their standing setting keys."""

OTHER = ""
"""The terminal picker's value for Other command; never a valid setting."""

NO_PATHS = "No folders added."


def choose_folder(parent: QWidget, start: str) -> str:
    """Ask the owner for a folder with the desktop's folder chooser.

    Args:
        parent: The chooser's parent.
        start: The folder it opens in.

    Returns:
        The chosen folder, or empty text when the owner cancelled.
    """
    return QFileDialog.getExistingDirectory(
        parent, "Add a folder workers may read", start
    )


def choose_repository(parent: QWidget, start: str) -> str:
    """Ask the owner for a repository's main checkout folder."""
    return QFileDialog.getExistingDirectory(
        parent, "Add a repository for worktree preparation", start
    )


def preparation_command(
    parent: QWidget, repository: str, command: str
) -> tuple[str, bool]:
    """Ask for a shell command, accepting empty text as no preparation."""
    dialog = PreparationCommandDialog(repository, command, parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return dialog.command.text(), accepted


class PreparationCommandDialog(QDialog):
    """Edit a repository's preparation command; Cancel is the default."""

    TITLE = "Worktree preparation"
    EXPLANATION = (
        "Runs in each new worktree outside the worker sandbox, before any "
        "worker or check starts. Leave empty for no preparation."
    )

    def __init__(
        self, repository: str, command: str, parent: QWidget | None = None
    ) -> None:
        """Create the dialog with the repository's saved command prefilled."""
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.title = QLabel(self.TITLE)
        self.title.setFont(theme.font("title"))
        layout.addWidget(self.title)
        self.repository = label(repository, "secondary")
        self.repository.setTextFormat(Qt.TextFormat.PlainText)
        self.repository.setWordWrap(True)
        self.repository.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.repository)
        heading = QHBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(QLabel("Command"))
        self.info = widgets.InfoTip("Command", self.EXPLANATION)
        self.info.setAutoDefault(False)
        heading.addWidget(self.info)
        heading.addStretch(1)
        layout.addLayout(heading)
        self.command = QLineEdit(command)
        self.command.setAccessibleName("Command")
        self.command.setPlaceholderText("npm ci")
        layout.addWidget(self.command)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = widgets.button("Cancel", "secondary")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = widgets.button("Save", "primary")
        self.save_button.setAutoDefault(True)
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)
        title_bar = self.fontMetrics().horizontalAdvance(self.TITLE) + 4 * (
            self.style().pixelMetric(QStyle.PixelMetric.PM_TitleBarHeight)
        )
        self.setMinimumWidth(max(480, title_bar))
        chain_tab_order([self.info, self.command, self.cancel_button, self.save_button])


def _words(value: str) -> list[str] | None:
    try:
        return shlex.split(value)
    except ValueError:
        return None


def _same_command(a: object, b: object) -> bool:
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return _words(a) == _words(b) and _words(a) is not None


class SettingsScreen(QWidget):
    """The Settings screen: start at login, theme, and how workers run."""

    TITLE = "Settings"

    def __init__(self, context: AppContext) -> None:
        """Create the screen.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        self._other_open = False
        self._preparation_saving = False
        layout = scrolled(self)
        screen_header(layout, self.TITLE, SUBTITLE)
        self.tip_rows: dict[str, TooltipRow] = {}
        """Each setting's row with an info icon, by its name."""
        layout.addWidget(self._general())
        layout.addWidget(self._workers())
        layout.addWidget(self._preparation())
        self.tip_rows["Sandbox read paths"].fitted.connect(self._fit_lists)
        align_controls(self.tip_rows.values())
        layout.addStretch(1)
        context.settings_changed.connect(self._redraw)
        context.error.connect(self._redraw)
        context.autostart_reason_changed.connect(self._autostart_reason)
        self._autostart_reason(context.autostart_reason())
        self._redraw()

    def shown(self) -> None:
        """Called by the window each time the screen becomes current."""
        self._redraw()

    # Building

    def _general(self) -> widgets.Card:
        card, column = titled_card("General")
        self.autostart = widgets.Switch()
        self.autostart.setAccessibleName("Start at login")
        self.autostart.clicked.connect(
            lambda checked: self._save({"app.autostart": "on" if checked else "off"})
        )
        self._row(
            column,
            "Start at login",
            "Ember starts in the tray when you log in.",
            self.autostart,
        )
        self.autostart_line = ToneLine("warning")
        column.addWidget(self.autostart_line)
        self.theme = widgets.Segmented(THEMES)
        self.theme.setAccessibleName("Theme")
        self.theme.changed.connect(self._theme_picked)
        self._row(
            column,
            "Theme",
            "Light or dark. System follows the desktop's light or dark mode.",
            self.theme,
        )
        self._row(
            column,
            "Hidden notices",
            "Denied-folder notices hidden with Don't show again. "
            "Show again restores notices for that folder. "
            "Run status, flow status and WARNING lines still report denials.",
            QWidget(),
        )
        self.hidden_box = QWidget()
        self.hidden_layout = QVBoxLayout(self.hidden_box)
        self.hidden_layout.setContentsMargins(0, 0, 0, 0)
        self.hidden_layout.setSpacing(8)
        column.addWidget(self.hidden_box)
        self.hidden_rows: dict[str, QPushButton] = {}
        return card

    def _workers(self) -> widgets.Card:
        card, column = titled_card("Workers")
        self.network = widgets.Switch()
        self.network.setAccessibleName("Worker network")
        self.network.clicked.connect(
            lambda checked: self._save({"worker.network": "on" if checked else "off"})
        )
        self._row(
            column,
            "Worker network",
            "Lets Codex workers and Claude's web tools reach the network. "
            "OpenCode keeps network for its providers.",
            self.network,
        )
        self.terminal = widgets.Picker()
        self.terminal.setAccessibleName("Terminal")
        self.terminal.activated.connect(self._terminal_picked)
        self._row(
            column,
            "Terminal",
            "The terminal that opens worker sessions and the commands you run.",
            self.terminal,
        )
        self.other = QLineEdit()
        self.other.setAccessibleName("Terminal command")
        self.other.setPlaceholderText("Command line, such as gnome-terminal --")
        self.other.editingFinished.connect(self._other_entered)
        self.other.setVisible(False)
        column.addWidget(self.other)
        self.terminal_line = ToneLine("warning")
        column.addWidget(self.terminal_line)
        self.add_folder = widgets.button("Add folder", "secondary", size="sm")
        widgets.set_icon(
            self.add_folder, icons.icon("folder-plus", theme.color("text-primary"), 16)
        )
        self.add_folder.clicked.connect(self._add_folder)
        self._row(
            column,
            "Sandbox read paths",
            "Folders a sandboxed worker may read, besides its worktree.",
            self.add_folder,
        )
        self.paths_box = QWidget()
        self.paths_layout = QVBoxLayout(self.paths_box)
        self.paths_layout.setContentsMargins(0, 0, 0, 0)
        self.paths_layout.setSpacing(4)
        column.addWidget(self.paths_box)
        self.paths_line = ToneLine("warning")
        column.addWidget(self.paths_line)
        self.path_rows: dict[str, QPushButton] = {}
        theme.signals.changed.connect(self._draw_icons)
        return card

    def _row(
        self, column: QVBoxLayout, name: str, tooltip: str, control: QWidget
    ) -> None:
        """Add a setting row to a card and keep it for `align_controls`."""
        row = TooltipRow(name, tooltip, control)
        if name in SETTING_KEYS:
            row.enable_reset(lambda: self._reset(SETTING_KEYS[name]))
        self.tip_rows[name] = row
        column.addWidget(row)

    def _preparation(self) -> widgets.Card:
        card, column = titled_card("Worktree preparation")
        self.preparation_card = card
        self.add_repository = widgets.button("Add repository", "secondary", size="sm")
        widgets.set_icon(
            self.add_repository,
            icons.icon("folder-plus", theme.color("text-primary"), 16),
        )
        self.add_repository.clicked.connect(self._add_repository)
        self._row(
            column,
            "Repositories",
            "A shell command Ember runs in each new worktree, outside the worker "
            "sandbox, before any worker or check starts. "
            "Choose the repository's main checkout folder.",
            self.add_repository,
        )
        self.preparation_box = QWidget()
        self.preparation_layout = QVBoxLayout(self.preparation_box)
        self.preparation_layout.setContentsMargins(0, 0, 0, 0)
        self.preparation_layout.setSpacing(8)
        column.addWidget(self.preparation_box)
        self.preparation_line = ToneLine("warning")
        column.addWidget(self.preparation_line)
        self.preparation_rows: dict[str, tuple[QPushButton, QPushButton]] = {}
        self.preparation_buttons: list[QHBoxLayout] = []
        return card

    def _fit_lists(self, stacked: bool, reset_width: int) -> None:
        """Align list actions with controls when settings sit beside their names."""
        for layout, populated in (
            (self.paths_layout, bool(self.path_rows)),
            (self.preparation_layout, bool(self.preparation_rows)),
            (self.hidden_layout, bool(self.hidden_rows)),
        ):
            inset = reset_width if populated and not stacked else 0
            layout.setContentsMargins(0, 0, inset, 0)
        for buttons in self.preparation_buttons:
            buttons.setAlignment(
                Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignRight
            )

    def _draw_icons(self, *_args: object) -> None:
        color = theme.color("text-primary")
        widgets.set_icon(self.add_folder, icons.icon("folder-plus", color, 16))
        widgets.set_icon(self.add_repository, icons.icon("folder-plus", color, 16))
        for remove in self.path_rows.values():
            remove.setIcon(icons.icon("x", color, 16))

    # Drawing

    def _redraw(self, *_args: object) -> None:
        self._draw_hidden_notices()
        answers: dict[str, Any] = self.context.settings().get("answers", {})
        if not answers:
            return
        self.autostart.setChecked(answers["app.autostart"]["value"] == "on")
        self.network.setChecked(answers["worker.network"]["value"] == "on")
        self.theme.blockSignals(True)
        try:
            self.theme.set_value(answers["app.theme"]["value"])
        finally:
            self.theme.blockSignals(False)
        self._draw_terminal(str(answers["terminal"]["value"]))
        self._draw_paths(list(answers["sandbox.read"]["value"]))
        self._draw_preparation(dict(answers["worktree.prepare"]["value"]))
        chain_tab_order(
            [
                self.add_folder,
                *self.path_rows.values(),
                self.tip_rows["Repositories"].info,
                self.add_repository,
                *(button for row in self.preparation_rows.values() for button in row),
            ]
        )
        for name, key in SETTING_KEYS.items():
            self.tip_rows[name].set_changed(self.context.setting_changed(key))

    def _draw_hidden_notices(self) -> None:
        """List the folders whose denial notices the owner has hidden."""
        clear_layout(self.hidden_layout)
        self.hidden_rows = {}
        hidden = state.load(self.context.env).denied_hidden
        if not hidden:
            self.hidden_layout.setContentsMargins(0, 0, 0, 0)
            self.hidden_layout.addWidget(label("No folders hidden.", "secondary"))
        for path in hidden:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(8)
            text = label(path, "secondary")
            text.setFont(theme.font("code"))
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setWordWrap(True)
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            line.addWidget(text, 1)
            show = widgets.button("Show again", "secondary", size="sm")
            show.setAccessibleName(f"Show notices again for {path}")
            show.clicked.connect(lambda _checked=False, p=path: self._show_notices(p))
            line.addWidget(show)
            self.hidden_layout.addWidget(row)
            self.hidden_rows[path] = show
        chain_tab_order(
            [
                self.tip_rows["Hidden notices"].info,
                *self.hidden_rows.values(),
                self.tip_rows["Worker network"].info,
            ]
        )

    def _draw_terminal(self, value: str) -> None:
        env = self.context.env
        listed = terminals.found(env)
        saved = terminals.match(value, env)
        if saved is not None and all(t.id != saved.id for t in listed):
            listed.append(saved)
        picker = self.terminal
        picker.blockSignals(True)
        try:
            picker.clear()
            for terminal in listed:
                text = terminal.label
                if not terminal.available:
                    text += " (not installed)"
                picker.add_option(text, terminal.prefix, terminal.prefix)
            picker.add_option("Other command", OTHER, "Any command line")
            if self._other_open or saved is None:
                picker.set_value(OTHER)
            else:
                picker.set_value(saved.prefix)
        finally:
            picker.blockSignals(False)
        align_controls(self.tip_rows.values())
        other = picker.value() == OTHER
        if other and not self._other_open:
            self.other.setText(value)
        self.other.setVisible(other)
        if not other:
            self.terminal_line.show_text(None)

    def _draw_paths(self, paths: list[str]) -> None:
        clear_layout(self.paths_layout)
        self.path_rows = {}
        if not paths:
            self.paths_layout.setContentsMargins(0, 0, 0, 0)
            self.paths_layout.addWidget(label(NO_PATHS, "secondary"))
            return
        color = theme.color("text-primary")
        for path in paths:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(8)
            text = QLabel(path)
            text.setFont(theme.font("code"))
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            line.addWidget(text, 1)
            remove = widgets.button("", "ghost", size="sm")
            remove.setIcon(icons.icon("x", color, 16))
            remove.setAccessibleName(f"Remove {path}")
            remove.clicked.connect(lambda _checked=False, path=path: self._remove(path))
            line.addWidget(remove)
            self.paths_layout.addWidget(row)
            self.path_rows[path] = remove

    def _autostart_reason(self, reason: object) -> None:
        self.autostart_line.show_text(None if reason is None else str(reason))

    def _draw_preparation(self, commands: dict[str, str]) -> None:
        clear_layout(self.preparation_layout)
        self.preparation_rows = {}
        self.preparation_buttons = []
        if not commands:
            self.preparation_layout.setContentsMargins(0, 0, 0, 0)
            self.preparation_layout.addWidget(
                label("No repositories added.", "secondary")
            )
        for repository, command in commands.items():
            row = QWidget()
            column = QVBoxLayout(row)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(4)
            for value in (repository, command or "No preparation"):
                text = QLabel(value)
                text.setFont(theme.font("code"))
                text.setTextFormat(Qt.TextFormat.PlainText)
                text.setWordWrap(True)
                text.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                column.addWidget(text)
            buttons = QHBoxLayout()
            edit = widgets.button("Edit", "secondary", size="sm")
            edit.setAccessibleName(f"Edit preparation for {repository}")
            edit.clicked.connect(
                lambda _checked=False, path=repository: self._edit_preparation(path)
            )
            buttons.addWidget(edit)
            remove = widgets.button("Remove", "ghost", size="sm")
            remove.setAccessibleName(f"Remove preparation for {repository}")
            remove.clicked.connect(
                lambda _checked=False, path=repository: self._remove_preparation(path)
            )
            buttons.addWidget(remove)
            buttons.setAlignment(
                Qt.AlignmentFlag.AlignLeft
                if self.tip_rows["Repositories"].stacked
                else Qt.AlignmentFlag.AlignRight
            )
            column.addLayout(buttons)
            self.preparation_buttons.append(buttons)
            self.preparation_layout.addWidget(row)
            self.preparation_rows[repository] = (edit, remove)

    # Saving

    def _show_notices(self, path: str) -> None:
        """Restore notices for the folder, including dismissed reports."""
        saved = state.load(self.context.env)
        state.save(
            dataclasses.replace(
                saved,
                denied_hidden=[
                    folder for folder in saved.denied_hidden if folder != path
                ],
                denied_dismissals={
                    folder: reports
                    for folder, reports in saved.denied_dismissals.items()
                    if folder != path
                },
            ),
            self.context.env,
        )
        self._draw_hidden_notices()

    def _reset(self, key: str) -> None:
        """Clear a saved setting and apply its confirmed default."""

        def cleared(_snapshot: dict[str, Any]) -> None:
            """Close Other command and apply the default theme after a reset."""
            if key == "terminal":
                self._other_open = False
                self._redraw()
            elif key == "app.theme":
                self._apply_theme(str(self.context.setting(key)))

        self.context.clear_settings([key], cleared)

    def _apply_theme(self, value: str) -> None:
        """Apply the confirmed theme setting to the application."""
        app = QApplication.instance()
        if isinstance(app, QApplication):
            mode = theme.resolve_mode(value, app)
            if mode != theme.current_mode():
                theme.apply(app, mode)

    def _save(
        self,
        values: dict[str, object],
        done: Callable[[dict[str, Any]], None] | None = None,
        *,
        result: Callable[[dict[str, Any] | Exception], None] | None = None,
    ) -> None:
        self.context.save_settings(values, done, result=result)

    def _theme_picked(self, value: str) -> None:
        if value == self.context.setting("app.theme"):
            return

        def applied(_snapshot: dict[str, Any]) -> None:
            self._apply_theme(value)

        self._save({"app.theme": value}, applied)

    def _terminal_picked(self, _index: int) -> None:
        value = self.terminal.value()
        if value == OTHER:
            self._other_open = True
            self.other.setVisible(True)
            self.other.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self._other_open = False
        self.other.setVisible(False)
        self.terminal_line.show_text(None)
        if isinstance(value, str) and value != self.context.setting("terminal"):
            self._save({"terminal": value})

    def _other_entered(self) -> None:
        """Save Other command's text on Enter or when the field loses focus.

        Text that cannot be saved keeps the field open on Enter; leaving the
        field with it returns the picker to the saved value. Either way the
        line under the field says why.
        """
        if self.terminal.value() != OTHER:
            return
        text = self.other.text().strip()
        problem = None
        if not text:
            problem = "Enter a command line, such as gnome-terminal --."
        else:
            try:
                choices.validate_value("terminal", text)
            except choices.InvalidChoiceError as error:
                problem = f"Not saved: {error}."
        if problem is not None:
            if not self.other.hasFocus():
                self._other_open = False
                self.other.clear()
                self._redraw()
            self.terminal_line.show_text(problem)
            return
        self.terminal_line.show_text(None)
        self._other_open = False
        if _same_command(text, self.context.setting("terminal")):
            self._redraw()
            return
        self._save({"terminal": text})

    def _paths(self) -> list[str]:
        return list(self.context.setting("sandbox.read") or [])

    def _add_folder(self) -> None:
        start = self.context.env.get("HOME", "")
        chosen = choose_folder(self, start)
        if not chosen:
            return
        path = os.path.abspath(chosen)
        problem = sandbox.read_path_problem(path, self.context.env)
        if problem:
            self.paths_line.show_text(f"Not saved: {problem}.")
            return
        paths = self._paths()
        if path in paths:
            self.paths_line.show_text(f"{path} is already in the list.")
            return
        self.paths_line.show_text(None)
        self._save({"sandbox.read": [*paths, path]})

    def _remove(self, path: str) -> None:
        self.paths_line.show_text(None)
        self._save({"sandbox.read": [p for p in self._paths() if p != path]})

    def _preparation_commands(self) -> dict[str, str]:
        return dict(self.context.setting("worktree.prepare") or {})

    def _add_repository(self) -> None:
        if self._preparation_saving:
            return
        chosen = choose_repository(self, self.context.env.get("HOME", ""))
        if not chosen:
            return
        try:
            repository = str(Path(chosen).resolve())
            commands = self._preparation_commands()
            if any(Path(path).resolve() == Path(repository) for path in commands):
                self.preparation_line.show_text(f"{repository} is already in the list.")
                return
        except (OSError, ValueError) as error:
            self.preparation_line.show_text(f"Not saved: {error}.")
            return
        command, accepted = preparation_command(self, repository, "")
        if accepted:
            self._save_preparation({**commands, repository: command})

    def _edit_preparation(self, repository: str) -> None:
        if self._preparation_saving:
            return
        commands = self._preparation_commands()
        command, accepted = preparation_command(self, repository, commands[repository])
        if accepted and command != commands[repository]:
            self._save_preparation({**commands, repository: command})

    def _remove_preparation(self, repository: str) -> None:
        if self._preparation_saving:
            return
        commands = self._preparation_commands()
        del commands[repository]
        self._save_preparation(commands)

    def _save_preparation(self, commands: dict[str, str]) -> None:
        self._preparation_saving = True
        self.preparation_card.setEnabled(False)
        self.preparation_line.show_text(None)

        def finished(result: dict[str, Any] | Exception) -> None:
            self._preparation_saving = False
            self.preparation_card.setEnabled(True)
            self._redraw()
            if isinstance(result, Exception):
                self.preparation_line.show_text(f"Not saved: {result}.")

        self._save({"worktree.prepare": commands}, result=finished)
