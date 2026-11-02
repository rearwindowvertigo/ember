"""The Menu screen: which worker Claude uses for a single task (design C1, J2).

Two rows, Writer and Reviewer, each a picker of Ask each time and the
shortlisted models of every engine that is on, under the engine's name. While
a row holds a Claude model, a row under it picks how that model runs: as a
CLI or as a subagent. A saved value that the picker no longer offers stays
chosen, with a warning saying why. Each change saves at once. One-time
choices are not shown.
"""

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ember import choices
from ember.app import theme, widgets

if TYPE_CHECKING:
    from ember.app.context import AppContext

ENGINE_ORDER = ("codex", "opencode", "claude")
"""The engines in the order the pickers list them, as the Engines screen does."""

ENGINE_LABELS = {"codex": "Codex", "opencode": "OpenCode", "claude": "Claude"}
"""Each engine's shown name."""

SUBTITLE = (
    "Which worker Claude uses for a single task. Ask each time means Claude asks you."
)

ROLES = (
    ("writer", "Writer", "Writes the code for a single task."),
    ("reviewer", "Reviewer", "Reads the writer's branch and reports. Never edits."),
)
"""`(role, name, help)` for each row."""

RUN_AS = (("cli", "CLI"), ("subagent", "Subagent"))
"""The choices of how a Claude model runs."""

ASK = "ask"
"""The menu value of Ask each time."""

NO_SHORTLIST = "Shortlist models in Engines to offer them here."


class ToneLine(QLabel):
    """One line of text in a tone's colour, following the theme."""

    def __init__(self, tone: str = "warning", parent: QWidget | None = None) -> None:
        """Create the line, empty and hidden.

        Args:
            tone: One of `widgets.TONES`.
            parent: The parent widget.
        """
        super().__init__(parent)
        self._tone = tone
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setVisible(False)
        self._draw()
        theme.signals.changed.connect(self._draw)

    def _draw(self, *_args: object) -> None:
        self.setStyleSheet(f"color: {widgets.tone_color(self._tone).name()};")

    def show_text(self, text: str | None) -> None:
        """Show a line, or hide the line for `None` or empty text."""
        self.setText(text or "")
        self.setAccessibleName(text or "")
        self.setVisible(bool(text))


def screen_header(layout: QVBoxLayout, title: str, subtitle: str) -> None:
    """Add a screen's title, in the display type, and its subtitle.

    Args:
        layout: The screen's content layout.
        title: The screen's name.
        subtitle: One line under it.
    """
    heading = QLabel(title)
    heading.setFont(theme.font("display"))
    layout.addWidget(heading)
    line = QLabel(subtitle)
    line.setProperty("role", "secondary")
    line.setWordWrap(True)
    layout.addWidget(line)
    layout.addSpacing(16)


def scrolled(screen: QWidget) -> QVBoxLayout:
    """Give a screen a scrolling content area with the screen padding.

    Args:
        screen: The screen widget.

    Returns:
        The content layout, with 24 px padding and 16 px gaps.
    """
    outer = QVBoxLayout(screen)
    outer.setContentsMargins(0, 0, 0, 0)
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    content = QWidget()
    area.setWidget(content)
    outer.addWidget(area)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(16)
    return layout


def name_and_help(name: str, help_text: str) -> QWidget:
    """Return a row's name over its line of help."""
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(2)
    label = QLabel(name)
    label.setFont(theme.font("label"))
    column.addWidget(label)
    line = QLabel(help_text)
    line.setProperty("role", "secondary")
    line.setWordWrap(True)
    column.addWidget(line)
    return box


def _split(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    engine, colon, model = value.partition(":")
    if not colon or engine not in ENGINE_LABELS or not model:
        return None
    return engine, model


def offered(answers: dict[str, Any]) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Return what a picker offers besides Ask each time.

    Args:
        answers: The snapshot's resolved answers.

    Returns:
        `(engine, [(model, effort), ...])` for each engine that is on and has
        shortlisted models, in `ENGINE_ORDER`.
    """
    result: list[tuple[str, list[tuple[str, str | None]]]] = []
    for engine in ENGINE_ORDER:
        if answers[f"engine.{engine}"]["value"] != "on":
            continue
        models = choices.shortlist_models(
            engine, answers[f"shortlist.{engine}"]["value"]
        )
        if models:
            result.append((engine, list(models.items())))
    return result


def option_text(model: str, effort: str | None) -> str:
    """Return a shortlisted model's text in a picker: `<model> · <effort>`."""
    return model if effort is None else f"{model} · {effort}"


def orphan(value: object, answers: dict[str, Any]) -> tuple[str, str] | None:
    """Describe a saved value that the picker does not offer.

    Args:
        value: The saved menu value.
        answers: The snapshot's resolved answers.

    Returns:
        `(picker text, warning)`, or `None` when the value is Ask each time
        or offered.
    """
    if value == ASK:
        return None
    parts = _split(value)
    if parts is None:
        return f"{value} (not valid)", (
            f"{value} is not a worker Ember knows. Pick another worker."
        )
    engine, model = parts
    label = ENGINE_LABELS[engine]
    if answers[f"engine.{engine}"]["value"] != "on":
        return f"{value} ({label} is off)", (
            f"{label} is off. Turn it on in Engines or pick another worker."
        )
    models = choices.shortlist_models(engine, answers[f"shortlist.{engine}"]["value"])
    if model not in models:
        return f"{value} (not on the shortlist)", (
            f"{model} is not on the {label} shortlist. Shortlist it in Engines "
            "or pick another worker."
        )
    return None


class _Role:
    """The controls of one role: its picker, warning and run-as row."""

    def __init__(self, role: str, name: str) -> None:
        self.role = role
        self.key = f"single.{role}"
        self.picker = widgets.Picker()
        self.picker.setAccessibleName(name)
        self.picker.setMinimumWidth(280)
        self.warning = ToneLine("warning")
        self.run_as = widgets.Segmented(RUN_AS)
        self.run_as.setAccessibleName(f"Claude {role} runs as")
        self.run_as_row = QWidget()


class MenuScreen(QWidget):
    """The Menu screen: which worker Claude uses for a single task."""

    TITLE = "Menu"

    def __init__(self, context: AppContext) -> None:
        """Create the screen.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        layout = scrolled(self)
        screen_header(layout, self.TITLE, SUBTITLE)

        card = widgets.Card()
        grid = QGridLayout(card)
        grid.setContentsMargins(20, 20, 20, 20)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(16)
        grid.setColumnStretch(0, 1)
        self.roles: dict[str, _Role] = {}
        row = 0
        for role, name, help_text in ROLES:
            controls = _Role(role, name)
            self.roles[role] = controls
            grid.addWidget(name_and_help(name, help_text), row, 0)
            grid.addWidget(controls.picker, row, 1, Qt.AlignmentFlag.AlignVCenter)
            row += 1
            grid.addWidget(controls.warning, row, 0, 1, 2)
            row += 1
            run_as_layout = QHBoxLayout(controls.run_as_row)
            run_as_layout.setContentsMargins(0, 0, 0, 0)
            run_as_layout.addWidget(
                name_and_help(
                    f"Claude {role} runs as",
                    "CLI is a Claude Code worker started with emb run. "
                    "Subagent is a subagent inside Claude's own session.",
                ),
                1,
            )
            run_as_layout.addWidget(controls.run_as, 0, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(controls.run_as_row, row, 0, 1, 2)
            row += 1
            controls.picker.activated.connect(
                lambda _index, controls=controls: self._picked(controls)
            )
            controls.run_as.changed.connect(
                lambda value, controls=controls: self._save(
                    {f"{controls.key}_as": value}
                )
            )
        layout.addWidget(card)

        self.empty = QWidget()
        empty_layout = QHBoxLayout(self.empty)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.setSpacing(12)
        self.empty_line = QLabel(NO_SHORTLIST)
        self.empty_line.setProperty("role", "secondary")
        self.empty_line.setWordWrap(True)
        empty_layout.addWidget(self.empty_line, 1)
        self.open_engines = widgets.button("Open Engines", "secondary", size="sm")
        self.open_engines.clicked.connect(lambda: context.show_screen("engines"))
        empty_layout.addWidget(self.open_engines)
        layout.addWidget(self.empty)
        layout.addStretch(1)

        context.settings_changed.connect(self._redraw)
        context.error.connect(self._redraw)
        self._redraw()

    def shown(self) -> None:
        """Called by the window each time the screen becomes current."""
        self._redraw()

    # Drawing

    def _redraw(self, *_args: object) -> None:
        answers: dict[str, Any] = self.context.settings().get("answers", {})
        if not answers:
            return
        groups = offered(answers)
        self.empty.setVisible(not groups)
        menu = self.context.settings().get("menu") or {}
        for controls in self.roles.values():
            self._fill(controls, answers, groups, menu.get(controls.key) or {})

    def _fill(
        self,
        controls: _Role,
        answers: dict[str, Any],
        groups: list[tuple[str, list[tuple[str, str | None]]]],
        row: dict[str, Any],
    ) -> None:
        value = answers[controls.key]["value"]
        picker = controls.picker
        picker.blockSignals(True)
        try:
            picker.clear()
            picker.add_option("Ask each time", ASK)
            missing = orphan(value, answers)
            if missing is not None:
                picker.add_option(missing[0], value)
            for engine, models in groups:
                picker.add_header(ENGINE_LABELS[engine])
                for model, effort in models:
                    picker.add_option(option_text(model, effort), f"{engine}:{model}")
            picker.set_value(value)
        finally:
            picker.blockSignals(False)
        warning = None if missing is None else missing[1]
        if warning is None and row.get("state") == "not connected":
            engine = ENGINE_LABELS.get(str(row.get("engine")), "The engine")
            warning = (
                f"{engine} was not connected when last checked. Set it up in "
                "Engines or pick another worker."
            )
        controls.warning.show_text(warning)
        run_as = answers[f"{controls.key}_as"]["value"]
        controls.run_as.blockSignals(True)
        try:
            controls.run_as.set_value(run_as)
        finally:
            controls.run_as.blockSignals(False)
        self._show_run_as(controls)

    def _show_run_as(self, controls: _Role) -> None:
        parts = _split(controls.picker.value())
        controls.run_as_row.setVisible(parts is not None and parts[0] == "claude")

    # Saving

    def _picked(self, controls: _Role) -> None:
        self._show_run_as(controls)
        value = controls.picker.value()
        if value is None or value == self.context.setting(controls.key):
            return
        self._save({controls.key: value})

    def _save(self, values: dict[str, object]) -> None:
        self.context.save_settings(values)
