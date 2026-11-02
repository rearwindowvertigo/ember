"""The Menu screen: which worker Claude uses for a single task (design C1, J2).

Two rows, Writer and Reviewer, each a picker of Ask each time and the
shortlisted models of every engine that is on, under the engine's name. While
a row holds a Claude model, a row under it picks how that model runs: as a
CLI or as a subagent. A saved value that the picker no longer offers stays
chosen, with a warning saying why. A saved model whose engine was not
connected shows a warning too, from the engine status the app read last, or
from the snapshot's menu rows before the first read. Each change saves at
once; choosing the value already saved saves nothing. One-time choices are
not shown.
"""

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget

from ember import choices
from ember.app import widgets
from ember.app.screens.common import (
    ToneLine,
    engine_name,
    label,
    name_and_help,
    screen_header,
    scrolled,
)

if TYPE_CHECKING:
    from ember.app.context import AppContext

ENGINE_ORDER = ("codex", "opencode", "claude")
"""The engines in the order the pickers list them, as the Engines screen does."""

ENGINE_LABELS = {engine: engine_name(engine) for engine in ENGINE_ORDER}
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
                lambda value, controls=controls: self._run_as_picked(controls, value)
            )
        layout.addWidget(card)

        self.empty = QWidget()
        empty_layout = QHBoxLayout(self.empty)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.setSpacing(12)
        self.empty_line = label(NO_SHORTLIST, "secondary")
        empty_layout.addWidget(self.empty_line, 1)
        self.open_engines = widgets.button("Open Engines", "secondary", size="sm")
        self.open_engines.clicked.connect(lambda: context.show_screen("engines"))
        empty_layout.addWidget(self.open_engines)
        layout.addWidget(self.empty)
        layout.addStretch(1)

        context.settings_changed.connect(self._redraw)
        context.engines_changed.connect(self._redraw)
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
        engines = self.context.engines()
        if engines is not None:
            connected = {name: item.connected for name, item in engines.items()}
            row = choices.menu_row(answers, controls.key, connected)
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

    def _run_as_picked(self, controls: _Role, value: str) -> None:
        key = f"{controls.key}_as"
        if value != self.context.setting(key):
            self._save({key: value})

    def _save(self, values: dict[str, object]) -> None:
        self.context.save_settings(values)
