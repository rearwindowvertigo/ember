"""The Settings screen: start at login, theme, and how workers run (design J2).

Two groups in cards. General holds Start at login and Theme; Workers holds
Worker network, Terminal and Sandbox read paths. Each change saves at once.
The app's controller writes or removes the autostart entry after
`app.autostart` changes; this screen shows why it could not, when it could
not.
"""

import os
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ember import choices, terminals
from ember.app import icons, theme, widgets
from ember.app.screens.common import (
    ToneLine,
    TooltipRow,
    align_controls,
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
        layout = scrolled(self)
        screen_header(layout, self.TITLE, SUBTITLE)
        self.tip_rows: dict[str, TooltipRow] = {}
        """Each setting's row with an info icon, by its name."""
        layout.addWidget(self._general())
        layout.addWidget(self._workers())
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
            "OpenCode keeps network for OpenRouter.",
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
        self.tip_rows[name] = row
        column.addWidget(row)

    def _draw_icons(self, *_args: object) -> None:
        color = theme.color("text-primary")
        widgets.set_icon(self.add_folder, icons.icon("folder-plus", color, 16))
        for remove in self.path_rows.values():
            remove.setIcon(icons.icon("x", color, 16))

    # Drawing

    def _redraw(self, *_args: object) -> None:
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

    # Saving

    def _save(
        self,
        values: dict[str, object],
        done: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.context.save_settings(values, done)

    def _theme_picked(self, value: str) -> None:
        if value == self.context.setting("app.theme"):
            return

        def applied(_snapshot: dict[str, Any]) -> None:
            app = QApplication.instance()
            if isinstance(app, QApplication):
                mode = theme.resolve_mode(value, app)
                if mode != theme.current_mode():
                    theme.apply(app, mode)

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
        paths = self._paths()
        if path in paths:
            self.paths_line.show_text(f"{path} is already in the list.")
            return
        self.paths_line.show_text(None)
        self._save({"sandbox.read": [*paths, path]})

    def _remove(self, path: str) -> None:
        self.paths_line.show_text(None)
        self._save({"sandbox.read": [p for p in self._paths() if p != path]})
