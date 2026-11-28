"""The Team screen for worker, planning and provider settings.

Writer and Reviewer each offer Ask each time, Let the lead choose and the
shortlisted models of every engine that is on, under the engine's name. The
routing pool holds approved models, owner scores and notes. While
a row holds a Claude model, a row under it picks how that model runs: as a
CLI or as a subagent. A saved value that the picker no longer offers stays
chosen, with a warning saying why. A saved model whose engine was not
connected shows a warning too, from the engine status the app read last, or
from the snapshot's menu rows before the first read. Each change saves at
once; choosing the value already saved saves nothing. Multi-task work chooses
Ask each time, Flow or Claude Workflow. Panel lists up to eight members with
Add member and Remove. Panel controls wait for a save and refresh before
accepting another edit. One-time choices are not shown. Provider capacity sets
the retry limit, retry delay and hold duration. The Fallback models card lists
workers in the order Ember tries them after a provider error.

Planning sets the gap check: its reviewer, when it runs and which gaps reach
the owner. Flows and Claude Workflows hold the settings of each kind of
multi-task work; Flows shows unless Multi-task work is Claude Workflow, and
Claude Workflows unless it is Flow. Every setting there has a tooltip.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ember import choices, db
from ember.app import icons, theme, widgets
from ember.app.screens.common import (
    FittingRow,
    ToneLine,
    TooltipRow,
    align_controls,
    chain_tab_order,
    clear_layout,
    engine_name,
    label,
    screen_header,
    scrolled,
    titled_card,
)

if TYPE_CHECKING:
    from ember.app.context import AppContext

ENGINE_ORDER = ("codex", "opencode", "claude")
"""The engines in the order the pickers list them, as the Engines screen does."""

ENGINE_LABELS = {engine: engine_name(engine) for engine in ENGINE_ORDER}
"""Each engine's shown name."""

SUBTITLE = "Which workers the lead uses. Ask each time means the lead asks you."

MULTI = (("ask", "Ask each time"), ("flow", "Flow"), ("workflow", "Claude Workflow"))
MULTI_HELP = (
    "How the lead runs two or more tasks that each need a worker. "
    "A Claude Workflow needs the Workflow tool in Claude Code."
)
RUN_AS_HELP = (
    "How a Claude model runs. CLI is a Claude Code worker started with emb run. "
    "Subagent requires a Claude Code lead with a subagent tool."
)
ROUNDS = tuple((value, value) for value in choices.REVIEW_ROUNDS)
ROUNDS_HELP = (
    "The most times a reviewer can check one flow task. "
    "A flow plan that sets its own rounds wins."
)
WHOLE_HELP = (
    "One review of all the flow's changes together. After every task of a "
    "flow merges, the Flow reviewer reads the whole flow's changes once, with "
    "the review bar, before the lead merges the flow."
)
PANEL_HELP = "The models a panel asks when the request names no members."
PANEL_LIMIT = 8
ADD_MEMBER = "Add member"
NO_MEMBERS = (
    "No members. The lead asks for members, or uses the ones named in the request."
)
FULL = "A panel has at most eight members."
ALL_ADDED = "Every shortlisted model is a member. Shortlist more in Engines to add one."
ADD_BLOCKED = (
    "Remove the members not on their shortlist, or shortlist them "
    "in Engines, to add a member."
)

ROLES = (
    ("writer", "Writer", "Writes the code for a single task."),
    (
        "reviewer",
        "Reviewer",
        "Reads the writer's branch and reports. Never edits. "
        "Used for read-only single runs, and by the Gap check reviewer and "
        "Flow reviewer while they are Same as Reviewer.",
    ),
)
"""`(role, name, tooltip)` for each row."""

DERIVED = (
    (
        "gapcheck.reviewer",
        "Gap check reviewer",
        "Reads a flow or Claude Workflow plan before any worker starts and "
        "lists what it leaves open. Same as Reviewer uses the single-task "
        "Reviewer.",
        "Same as Reviewer",
    ),
    (
        "flow.writer",
        "Flow writer",
        "Writes every task of a flow. Same as Writer uses the single-task "
        "Writer. A flow started with named workers uses those.",
        "Same as Writer",
    ),
    (
        "flow.reviewer",
        "Flow reviewer",
        "Reviews every flow task and, with Review the whole flow on, the whole "
        "flow once before the lead merges it. Same as Reviewer uses the "
        "single-task Reviewer.",
        "Same as Reviewer",
    ),
)
"""`(key, name, tooltip, same text)` for each worker row with a Same choice."""

WHEN = tuple(
    (
        value,
        {"off": "Off", "always": "Always", "ask": "Ask each time"}.get(
            value, f"{value}+ tasks"
        ),
    )
    for value in choices.GAPCHECK_WHEN
)
WHEN_TIP = (
    "When the lead has a plan checked for gaps before a flow or Claude Workflow "
    "starts. A plan the setting skips starts without a check and without a "
    "question; the lead reports that it skipped the check."
)
ASKS = (("high", "High only"), ("medium", "High and medium"), ("all", "All"))
ASKS_LABEL = "Gaps to ask you about"
ASKS_TIP = (
    "Which gaps the lead brings to you. High only: medium and low gaps take "
    "their default. High and medium: low gaps take their default. All: high "
    "gaps one by one, medium and low together in one question. A gap that is "
    "not asked takes the default the gap check proposes, written into the "
    "plan, and the lead lists it in its report."
)
PARALLEL = tuple((value, value) for value in choices.FLOW_PARALLEL)
STALL = tuple((value, f"{value} min") for value in choices.FLOW_STALL)
AGENTS = tuple((value, value) for value in choices.WORKFLOW_AGENTS)
LATE = (("ask", "Ask me"), ("wait", "Wait once more"), ("stop", "Stop"))
SEGMENTS = (
    (
        "flow.parallel",
        "Max tasks at once",
        "No more than this many flow tasks run at the same time. A plan that "
        "sets its own parallel count wins.",
        PARALLEL,
    ),
    (
        "flow.stall",
        "Stall after",
        "How long a flow worker's screen may stay still before Ember stops "
        "waiting on it and asks the lead.",
        STALL,
    ),
    (
        "workflow.agents",
        "Max agents at once",
        "No more than this many Claude Workflow agents run at the same time. "
        "Agents use this session's Claude model.",
        AGENTS,
    ),
    (
        "workflow.late",
        "When an agent runs late",
        "What the lead does when a Claude Workflow agent runs past its deadline, "
        "its expected time plus half again, or 10 minutes: ask you, wait one "
        "more deadline, or stop the agent.",
        LATE,
    ),
)
"""`(key, name, tooltip, choices)` for each segmented setting."""


CAPACITY = (
    (
        "provider.capacity_retries",
        "Capacity retries",
        "The limit on resends of one turn after a capacity error. "
        "Zero turns retries off. Runs keep the settings they started with.",
        tuple((str(n), "Off" if n == 0 else f"Up to {n}") for n in range(11)),
    ),
    (
        "provider.capacity_wait",
        "Wait before a retry",
        "Seconds between a capacity error and its resend. "
        "The model and effort stay the same.",
        tuple(
            (wait, f"{wait}s" if int(wait) <= 60 else f"{int(wait) // 60} min")
            for wait in choices.CAPACITY_WAITS
        ),
    ),
    (
        "provider.hold_minutes",
        "Try the first choice again after",
        "How long Ember passes over a model or engine after a provider error "
        "before a job tries it again. Every job records no hold.",
        tuple(
            (v, "Every job" if v == "0" else f"{v} min") for v in choices.HOLD_MINUTES
        ),
    ),
)
"""The provider capacity settings and their picker options."""

FALLBACK_HELP = (
    "After a provider error Ember starts the first model here that applies. "
    "A capacity error takes the next model. A usage limit or an outage takes "
    "the next model of another engine. A review never takes the model that "
    "wrote the work."
)
NO_FALLBACK = "No fallback models. A provider error then stops the run for the lead."
ROUTING_HELP = (
    "Let the lead choose can start a suitable shortlisted model in this pool "
    "at any supported effort. Ask each time can recommend any shortlisted "
    "model and effort, then waits for your answer."
)
NO_ROUTING_MODELS = "The lead asks you when the pool has no suitable model."
ADD_ROUTING_MODEL = "Add model"


def when_hint(value: str) -> str:
    """Return the hint line under a Run the gap check option."""
    if value == "off":
        return "Every flow and Claude Workflow starts without one."
    if value == "always":
        return "Before every flow and Claude Workflow."
    if value == "ask":
        return "The lead asks you before each flow or Claude Workflow."
    least = int(value)
    rest = "1 task" if least == 2 else f"1 to {least - 1} tasks"
    return f"Plans of {rest} start without one."


RUN_AS = (("cli", "CLI"), ("subagent", "Subagent"))
"""The choices of how a Claude model runs."""

ASK = "ask"
"""The menu value of Ask each time."""

AUTO_TEXT = "Let the lead choose"

NO_SHORTLIST = "Shortlist models in Engines to offer them here."


def _split(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    engine, colon, model = value.partition(":")
    if not colon or engine not in ENGINE_LABELS or not model:
        return None
    return engine, model


def offered(
    answers: Mapping[str, Any],
) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Return the models a worker picker offers.

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


def member_text(row: Mapping[str, Any]) -> str:
    """Return a panel member's engine, model and effort, or its saved value."""
    if row["state"] != "set":
        return str(row["value"])
    return f"{engine_name(row['engine'])} · {option_text(row['model'], row['effort'])}"


def member_warning(row: Mapping[str, Any]) -> str | None:
    """Describe why a saved panel member cannot run and how to change it."""
    engine = engine_name(str(row.get("engine")))
    match row["state"]:
        case "engine off":
            return f"{engine} is off. Turn it on in Engines or remove this member."
        case "not connected":
            return (
                f"{engine} was not connected when last checked. Set it up in "
                "Engines or remove this member."
            )
        case "not shortlisted":
            return (
                f"{row['model']} is not on the {engine} shortlist. Shortlist it "
                "in Engines or remove this member."
            )
        case "invalid":
            return f"{row['value']} is not a worker Ember knows. Remove this member."
    return None


def addable(
    answers: Mapping[str, Any], members: Sequence[str]
) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Return offered models excluding the saved entries."""
    result = []
    for engine, models in offered(answers):
        remaining = [
            (model, effort)
            for model, effort in models
            if f"{engine}:{model}" not in members
        ]
        if remaining:
            result.append((engine, remaining))
    return result


def blocks_save(answers: Mapping[str, Any], members: Sequence[str]) -> list[str]:
    """Return members missing from their shortlists, even when engines are off."""
    blocked = []
    for member in members:
        parts = _split(member)
        if parts is None:
            continue
        engine, model = parts
        if model not in choices.shortlist_models(
            engine, answers[f"shortlist.{engine}"]["value"]
        ):
            blocked.append(member)
    return blocked


def orphan(value: object, answers: dict[str, Any]) -> tuple[str, str] | None:
    """Describe a saved value that the picker does not offer.

    Args:
        value: The saved menu value.
        answers: The snapshot's resolved answers.

    Returns:
        `(picker text, warning)`, or `None` when the value is a policy or
        an offered model.
    """
    if value in (ASK, choices.AUTO):
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
        self.warning = ToneLine("warning")
        self.run_as = widgets.Segmented(RUN_AS)
        self.run_as.setAccessibleName(f"Claude {role} runs as")
        self.run_as_row: QWidget = QWidget()


class _Derived:
    """A worker row with a Same choice: its picker and warning."""

    def __init__(self, key: str, name: str, same: str) -> None:
        self.key = key
        self.same = same
        self.picker = widgets.Picker()
        self.picker.setAccessibleName(name)
        self.warning = ToneLine("warning")


class _RoutingDialog(QDialog):
    """Edit the owner's evidence for a pooled model."""

    def __init__(
        self, model: str, entry: choices.RoutingEntry | None, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Routing pool model")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        model_label = label(model)
        model_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(model_label)
        if entry is None:
            layout.addWidget(
                label(
                    "With Let the lead choose, this model can start automatically "
                    "at any supported effort.",
                    "secondary",
                )
            )

        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(label("Owner scores (0–100, optional)."))
        self.scores: dict[str, QLineEdit] = {}
        saved = {} if entry is None else entry["scores"]
        for capability, title in choices.ROUTING_CAPABILITIES.items():
            field = QLineEdit()
            field.setAccessibleName(title)
            field.setPlaceholderText("Unknown")
            validator = QDoubleValidator(0, 100, -1, field)
            validator.setLocale(QLocale.c())
            field.setValidator(validator)
            if capability in saved:
                value = saved[capability]
                field.setText(str(value) if value else "0")
            self.scores[capability] = field
            form.addRow(title, field)

        self.notes = QPlainTextEdit()
        self.notes.setAccessibleName("Owner notes")
        self.notes.setPlaceholderText("Work this model fits or sources for your scores")
        self.notes.setFixedHeight(72)
        self.notes.setTabChangesFocus(True)
        if entry is not None:
            self.notes.setPlainText(entry.get("notes", ""))
        form.addRow("Sources and notes (optional)", self.notes)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = widgets.button("Cancel", "secondary")
        cancel.clicked.connect(self.reject)
        self.save_button = widgets.button("Save", "primary")
        self.save_button.clicked.connect(self.accept)
        for button in (cancel, self.save_button):
            buttons.addWidget(button)
        cancel.setDefault(True)
        layout.addLayout(buttons)
        for field in self.scores.values():
            field.textChanged.connect(self._validate)
        self._validate()

    def entry(self) -> choices.RoutingEntry | None:
        """Return entered scores and notes, or None if a field is invalid."""
        scores: dict[str, float] = {}
        for capability, field in self.scores.items():
            text = field.text().strip()
            if not text:
                continue
            if not field.hasAcceptableInput():
                return None
            score, ok = QLocale.c().toDouble(text)
            if not ok:
                return None
            scores[capability] = score
        entry: choices.RoutingEntry = {"scores": scores}
        notes = self.notes.toPlainText()
        if notes.strip():
            entry["notes"] = notes
        return entry

    def _validate(self) -> None:
        self.save_button.setEnabled(self.entry() is not None)


class _ListSave:
    """Keep a list's controls pending until its saved settings have been read."""

    def __init__(self, screen: MenuScreen, key: str) -> None:
        self.screen = screen
        self.key = key
        self.pending = False
        self.refreshing = False
        self.refresh_failed = False

    def save(self, values: list[str] | dict[str, choices.RoutingEntry]) -> None:
        """Submit one edit while the list is ready."""
        if self.pending:
            return
        generation = self.screen.context.settings().get("generation")
        self.pending = True
        self.screen._redraw()
        self.screen.context.save_settings(
            {self.key: values},
            result=self._result,
            expect_generation=generation if isinstance(values, dict) else None,
        )

    def _result(self, result: dict[str, Any] | Exception) -> None:
        if isinstance(result, db.StaleGenerationError):
            if self.screen._settings_read_failed:
                self.refresh_failed = True
            else:
                self.done()
        elif isinstance(result, Exception):
            self.refresh()
        else:
            self.done()

    def refresh(self) -> None:
        """Read the list again after a save failure."""
        self.refresh_failed = False
        self.refreshing = True
        self.screen.context.reload_settings(self.done)

    def done(self) -> None:
        """Unlock the controls after reading the settings."""
        self.pending = self.refreshing = self.refresh_failed = False
        self.screen._redraw()


class MenuScreen(QWidget):
    """The standing choices for single tasks, multi-task work and panels."""

    TITLE = "Team"

    def __init__(self, context: AppContext) -> None:
        """Create the screen.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        layout = scrolled(self)
        screen_header(layout, self.TITLE, SUBTITLE)

        card, column = titled_card("Single tasks")
        self.roles: dict[str, _Role] = {}
        rows: dict[str, TooltipRow] = {}
        self.tip_rows = rows
        """Each setting's row with an info icon, by settings key."""
        for role, name, tooltip in ROLES:
            controls = _Role(role, name)
            self.roles[role] = controls
            rows[controls.key] = TooltipRow(name, tooltip, controls.picker)
            column.addWidget(rows[controls.key])
            column.addWidget(controls.warning)
            controls.run_as_row = TooltipRow(
                f"Claude {role} runs as", RUN_AS_HELP, controls.run_as
            )
            rows[f"{controls.key}_as"] = controls.run_as_row
            column.addWidget(controls.run_as_row)
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

        routing_card, routing = titled_card("Routing pool", ROUTING_HELP)
        self.routing_info = routing_card.findChild(widgets.InfoTip)
        self.routing_layout = QVBoxLayout()
        routing.addLayout(self.routing_layout)
        self.routing_save = _ListSave(self, choices.ROUTING_POOL)
        self.routing_buttons: list[QPushButton] = []
        self.add_routing = widgets.Picker()
        self.add_routing.setAccessibleName("Add routing pool model")
        self.add_routing.activated.connect(self._add_routing)
        self.view_benchmarks = widgets.button("View benchmarks", size="sm")
        self.view_benchmarks.clicked.connect(lambda: context.show_screen("benchmarks"))
        routing_actions = FittingRow((self.add_routing, self.view_benchmarks))
        routing_actions.box.addStretch(1)
        routing.addWidget(routing_actions)
        layout.addWidget(routing_card)

        self.derived: dict[str, _Derived] = {}
        self.segments: dict[str, widgets.Segmented] = {}
        for key, name, tip, same in DERIVED:
            derived = _Derived(key, name, same)
            self.derived[key] = derived
            rows[key] = TooltipRow(name, tip, derived.picker)
            derived.picker.activated.connect(
                lambda _index, derived=derived: self._derived_picked(derived)
            )
        for key, name, tip, values in SEGMENTS:
            control = widgets.Segmented(values)
            control.setAccessibleName(name)
            control.changed.connect(
                lambda value, key=key: self._segment_picked(key, value)
            )
            self.segments[key] = control
            rows[key] = TooltipRow(name, tip, control)

        planning_card, planning = titled_card("Planning")
        planning.addWidget(rows["gapcheck.reviewer"])
        planning.addWidget(self.derived["gapcheck.reviewer"].warning)
        self.when = widgets.Picker()
        self.when.setAccessibleName("Run the gap check")
        for value, text in WHEN:
            self.when.add_option(text, value, when_hint(value))
        self.when.activated.connect(self._when_picked)
        self.when_row = TooltipRow("Run the gap check", WHEN_TIP, self.when)
        rows["gapcheck.when"] = self.when_row
        planning.addWidget(self.when_row)
        self.asks = widgets.Segmented(ASKS)
        self.asks.setAccessibleName(ASKS_LABEL)
        self.asks.changed.connect(
            lambda value: self._segment_picked("gapcheck.ask", value)
        )
        self.asks_row = TooltipRow(ASKS_LABEL, ASKS_TIP, self.asks)
        rows["gapcheck.ask"] = self.asks_row
        planning.addWidget(self.asks_row)
        layout.addWidget(planning_card)

        multi_card, multi_layout = titled_card("Work of several tasks")
        self.multi = widgets.Segmented(MULTI)
        self.multi.setAccessibleName("Multi-task work")
        self.multi.changed.connect(self._multi_picked)
        rows["multi.runs_as"] = TooltipRow("Multi-task work", MULTI_HELP, self.multi)
        multi_layout.addWidget(rows["multi.runs_as"])
        layout.addWidget(multi_card)

        self.flow_card, flows = titled_card("Flows")
        for key in ("flow.writer", "flow.reviewer"):
            flows.addWidget(rows[key])
            flows.addWidget(self.derived[key].warning)
        self.rounds = widgets.Segmented(ROUNDS)
        self.rounds.setAccessibleName("Max review rounds")
        self.rounds.changed.connect(self._rounds_picked)
        rows["flow.review_rounds"] = TooltipRow(
            "Max review rounds", ROUNDS_HELP, self.rounds
        )
        flows.addWidget(rows["flow.review_rounds"])
        flows.addWidget(rows["flow.parallel"])
        flows.addWidget(rows["flow.stall"])
        self.whole = widgets.Switch()
        self.whole.setAccessibleName("Review the whole flow")
        self.whole.clicked.connect(self._whole_picked)
        rows["flow.whole_review"] = TooltipRow(
            "Review the whole flow", WHOLE_HELP, self.whole
        )
        flows.addWidget(rows["flow.whole_review"])
        layout.addWidget(self.flow_card)

        self.workflow_card, workflows = titled_card("Claude Workflows")
        workflows.addWidget(rows["workflow.agents"])
        workflows.addWidget(rows["workflow.late"])
        layout.addWidget(self.workflow_card)

        capacity_card, capacity_column = titled_card("Provider capacity")
        self.capacity: dict[str, widgets.Picker] = {}
        for key, name, tip, options in CAPACITY:
            picker = widgets.Picker()
            picker.setAccessibleName(name)
            for value, text in options:
                picker.add_option(text, value)
            picker.activated.connect(lambda _index, key=key: self._capacity_picked(key))
            self.capacity[key] = picker
            rows[key] = TooltipRow(name, tip, picker)
            capacity_column.addWidget(rows[key])
        layout.addWidget(capacity_card)

        fallback_card, fallback_layout = titled_card("Fallback models", FALLBACK_HELP)
        self.fallback_info = fallback_card.findChild(widgets.InfoTip)
        self.fallback_layout = QVBoxLayout()
        fallback_layout.addLayout(self.fallback_layout)
        self.fallback_save = _ListSave(self, "provider.fallback")
        self.fallback_buttons: list[QPushButton] = []
        self.add_fallback = widgets.Picker()
        self.add_fallback.setAccessibleName("Add fallback model")
        self.add_fallback.activated.connect(self._add_fallback)
        fallback_layout.addWidget(self.add_fallback, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(fallback_card)

        panel_card, panel_layout = titled_card("Panel", PANEL_HELP)
        self.panel_info = panel_card.findChild(widgets.InfoTip)
        self.members_layout = QVBoxLayout()
        panel_layout.addLayout(self.members_layout)
        self.member_removes: dict[str, QPushButton] = {}
        self.panel_save = _ListSave(self, "panel.members")
        self._settings_read_failed = False
        self._remove_blocker: str | None = None
        self.remove_warning = ToneLine("warning")
        panel_layout.addWidget(self.remove_warning)
        self.remove_all = widgets.button("Remove all", "secondary", size="sm")
        self.remove_all.clicked.connect(lambda: self._save_panel([]))
        panel_layout.addWidget(self.remove_all, 0, Qt.AlignmentFlag.AlignLeft)
        self.add_warning = ToneLine("warning")
        panel_layout.addWidget(self.add_warning)
        self.full = label(FULL, "secondary")
        panel_layout.addWidget(self.full)
        self.add_member = widgets.Picker()
        self.add_member.setAccessibleName("Add panel member")
        self.add_member.activated.connect(self._add_member)
        panel_layout.addWidget(self.add_member, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(panel_card)
        layout.addStretch(1)
        for key, row in rows.items():
            row.enable_reset(lambda key=key: context.clear_settings([key]))
        align_controls(rows.values())

        context.settings_changed.connect(self._settings_changed)
        context.engines_changed.connect(self._redraw)
        context.settings_failed.connect(self._settings_failed)
        theme.signals.changed.connect(self._draw_icons)
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
        multi = menu.get("multi.runs_as") or choices.runs_as_row(answers)
        panel = menu.get("panel.members") or choices.panel_row(answers)
        engines = self.context.engines()
        connected = None
        if engines is not None:
            connected = {name: item.connected for name, item in engines.items()}
            panel = choices.panel_row(answers, connected)
            multi = choices.runs_as_row(answers)
        self.multi.set_value(multi["value"])
        self.flow_card.setVisible(multi["value"] != "workflow")
        self.workflow_card.setVisible(multi["value"] != "flow")
        self.rounds.set_value(answers["flow.review_rounds"]["value"])
        for derived in self.derived.values():
            self._fill_derived(derived, answers, groups)
        when = str(answers["gapcheck.when"]["value"])
        self.when.blockSignals(True)
        try:
            self.when.set_value(when)
        finally:
            self.when.blockSignals(False)
        asks = str(answers["gapcheck.ask"]["value"])
        self.asks.set_value(asks)
        for key, control in self.segments.items():
            control.set_value(answers[key]["value"])
        for key, picker in self.capacity.items():
            picker.blockSignals(True)
            picker.setCurrentIndex(picker.findData(answers[key]["value"]))
            picker.blockSignals(False)
        self.whole.setChecked(answers["flow.whole_review"]["value"] == "on")
        self._fill_routing(answers, choices.routing_rows(answers, connected))
        self._fill_fallback(answers, choices.fallback_rows(answers, connected))
        self._fill_panel(answers, panel)
        for key, row in self.tip_rows.items():
            row.set_changed(self.context.setting_changed(key))
        align_controls(self.tip_rows.values())
        self._chain_focus()

    def _fill_routing(
        self, answers: dict[str, Any], entries: list[dict[str, Any]]
    ) -> None:
        """Show approved models, their preferred efforts and availability."""
        clear_layout(self.routing_layout)
        self.routing_buttons.clear()
        if not entries:
            self.routing_layout.addWidget(label(NO_ROUTING_MODELS, "secondary"))
        for entry in entries:
            value = entry["value"]
            text = str(value)
            if entry["engine"] is not None:
                text = f"{engine_name(entry['engine'])} · {entry['model']}"
                if effort := entry["effort"]:
                    text += f" · Preferred effort {effort}"
            row = widgets.PillRow()
            title = label(text)
            title.setTextFormat(Qt.TextFormat.PlainText)
            row.line.addWidget(title, 1)
            for caption, action in (
                ("Edit", self._edit_routing),
                ("Remove", self._remove_routing),
            ):
                button = widgets.button(caption, "secondary", size="sm")
                button.setAccessibleName(f"{caption} {text}")
                button.clicked.connect(
                    lambda _checked=False, action=action, value=value: action(value)
                )
                row.line.addWidget(button)
                self.routing_buttons.append(button)
            row.setEnabled(not self.routing_save.pending)
            self.routing_layout.addWidget(row)
            if entry["state"] != "set":
                warning = ToneLine("warning")
                self.routing_layout.addWidget(warning)
                warning.show_text(member_warning(entry))
        groups = addable(answers, [entry["value"] for entry in entries])
        self._fill_add_picker(self.add_routing, ADD_ROUTING_MODEL, groups)
        self.add_routing.setEnabled(bool(groups) and not self.routing_save.pending)

    def _add_routing(self, _index: int) -> None:
        value = self.add_routing.value()
        self.add_routing.set_value(None)
        if isinstance(value, str) and self.add_routing.isEnabled():
            self._edit_routing(value, adding=True)

    def _edit_routing(self, model: str, *, adding: bool = False) -> None:
        if self.routing_save.pending:
            return
        pool = self.context.setting(choices.ROUTING_POOL)
        before = pool.get(model)
        if adding != (before is None):
            return
        dialog = _RoutingDialog(model, before, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        entry = dialog.entry()
        dialog.deleteLater()
        if not accepted or entry is None or entry == before:
            return
        current = self.context.setting(choices.ROUTING_POOL)
        if current.get(model) != before:
            self.context.error.emit("The model changed. Open it again to edit.")
            return
        self.routing_save.save({**current, model: entry})

    def _remove_routing(self, model: str) -> None:
        pool = self.context.setting(choices.ROUTING_POOL)
        if model in pool:
            remaining = {key: entry for key, entry in pool.items() if key != model}
            self.routing_save.save(remaining)

    def _fill_fallback(
        self, answers: dict[str, Any], entries: list[dict[str, Any]]
    ) -> None:
        """Draw the ordered fallback list, its warnings and available additions."""
        clear_layout(self.fallback_layout)
        self.fallback_buttons.clear()
        values = [entry["value"] for entry in entries]
        if not entries:
            self.fallback_layout.addWidget(label(NO_FALLBACK, "secondary"))
        for index, entry in enumerate(entries):
            row = widgets.PillRow()
            text = str(entry["value"])
            if entry["engine"] is not None:
                model = option_text(entry["model"], entry["effort"])
                text = f"{engine_name(entry['engine'])} · {model}"
            title = label(f"{index + 1}. {text}")
            title.setTextFormat(Qt.TextFormat.PlainText)
            row.line.addWidget(title, 1)
            if index:
                move = widgets.button("Move up", "secondary", size="sm")
                move.setAccessibleName(f"Move up {text}")
                move.clicked.connect(
                    lambda _checked=False, index=index: self._move_fallback(index)
                )
                row.line.addWidget(move)
                self.fallback_buttons.append(move)
            remove = widgets.button("", "ghost", size="sm")
            remove.setAccessibleName(f"Remove {text}")
            remove.clicked.connect(
                lambda _checked=False, index=index: self.fallback_save.save(
                    values[:index] + values[index + 1 :]
                )
            )
            row.line.addWidget(remove)
            self.fallback_buttons.append(remove)
            row.setEnabled(not self.fallback_save.pending)
            self.fallback_layout.addWidget(row)
            if entry["state"] != "set":
                warning = ToneLine("warning")
                self.fallback_layout.addWidget(warning)
                warning.show_text(entry["reason"])
        groups = addable(answers, values)
        self._fill_add_picker(self.add_fallback, "Add fallback model", groups)
        self.add_fallback.setEnabled(
            bool(groups)
            and len(values) < choices.FALLBACK_LIMIT
            and not self.fallback_save.pending
        )

    def _move_fallback(self, index: int) -> None:
        values = list(self.context.setting("provider.fallback"))
        values[index - 1], values[index] = values[index], values[index - 1]
        self.fallback_save.save(values)

    def _add_fallback(self, _index: int) -> None:
        value = self.add_fallback.value()
        self.add_fallback.set_value(None)
        if isinstance(value, str) and self.add_fallback.isEnabled():
            self.fallback_save.save([*self.context.setting("provider.fallback"), value])

    def _fill_panel(self, answers: dict[str, Any], panel: dict[str, Any]) -> None:
        """Draw saved members and the available additions from the snapshot."""
        clear_layout(self.members_layout)
        self.member_removes = {}
        members = panel["value"]
        if not members:
            self.members_layout.addWidget(label(NO_MEMBERS, "secondary"))
        for member in panel["members"]:
            row = widgets.PillRow()
            line = row.line
            text = member_text(member)
            title = label(text)
            title.setTextFormat(Qt.TextFormat.PlainText)
            line.addWidget(title, 1)
            remove = widgets.button("", "ghost", size="sm")
            remove.setAccessibleName(f"Remove {text}")
            value = member["value"]
            remove.clicked.connect(
                lambda _checked=False, value=value: self._remove_member(value)
            )
            remove.setEnabled(not self.panel_save.pending)
            line.addWidget(remove)
            self.members_layout.addWidget(row)
            self.member_removes[value] = remove
            warning = ToneLine("warning")
            self.members_layout.addWidget(warning)
            warning.show_text(member_warning(member))
        self._draw_icons()
        blocked = blocks_save(answers, members)
        if self._remove_blocker not in blocked:
            self._remove_blocker = None
        warning_text = None
        if self._remove_blocker is not None:
            model = self._remove_blocker.partition(":")[2]
            warning_text = (
                f"Shortlist {model} in Engines, or remove every member "
                "not on its shortlist."
            )
        self.remove_warning.show_text(warning_text)
        self.remove_all.setVisible(warning_text is not None)
        self.remove_all.setEnabled(not self.panel_save.pending)
        groups = addable(answers, members)
        full = len(members) >= PANEL_LIMIT
        if blocked:
            add_text = ADD_BLOCKED
        elif not full and not groups and offered(answers):
            add_text = ALL_ADDED
        else:
            add_text = None
        self.add_warning.show_text(add_text)
        self.full.setVisible(full)
        self._fill_add_picker(self.add_member, ADD_MEMBER, groups)
        # A line on the card or the screen says why a member cannot be added:
        # the eight-member limit, a member to fix first, every shortlisted
        # model added, or no shortlist.
        self.add_member.setVisible(not blocked and not full and bool(groups))
        self.add_member.setEnabled(not self.panel_save.pending)

    def _fill_add_picker(
        self,
        picker: widgets.Picker,
        prompt: str,
        groups: list[tuple[str, list[tuple[str, str | None]]]],
    ) -> None:
        """Offer each available model under its engine heading."""
        picker.blockSignals(True)
        try:
            picker.clear()
            picker.add_option(prompt, None)
            for engine, models in groups:
                picker.add_header(ENGINE_LABELS[engine])
                for model, effort in models:
                    picker.add_option(option_text(model, effort), f"{engine}:{model}")
        finally:
            picker.blockSignals(False)

    def _draw_icons(self) -> None:
        """Keep icon-only Remove buttons in the current theme's text colour."""
        icon = icons.icon("x", theme.color("text-primary"), 16)
        for button in self.fallback_buttons:
            if not button.text():
                button.setIcon(icon)
        for remove in self.member_removes.values():
            remove.setIcon(icon)

    def _chain_focus(self) -> None:
        """Put the controls in reading order, including rebuilt member rows."""
        order: list[QWidget] = []
        tips = self.tip_rows
        for controls in self.roles.values():
            order.extend((tips[controls.key].info, controls.picker))
            order.append(tips[f"{controls.key}_as"].info)
            order.extend(controls.run_as.buttons())
        order.append(self.open_engines)
        if self.routing_info is not None:
            order.append(self.routing_info)
        order.extend(self.routing_buttons)
        order.extend((self.add_routing, self.view_benchmarks))
        order.extend(
            (tips["gapcheck.reviewer"].info, self.derived["gapcheck.reviewer"].picker)
        )
        order.extend((tips["gapcheck.when"].info, self.when))
        order.append(tips["gapcheck.ask"].info)
        order.extend(self.asks.buttons())
        order.append(tips["multi.runs_as"].info)
        order.extend(self.multi.buttons())
        for key in ("flow.writer", "flow.reviewer"):
            order.extend((tips[key].info, self.derived[key].picker))
        order.append(tips["flow.review_rounds"].info)
        order.extend(self.rounds.buttons())
        for key in ("flow.parallel", "flow.stall"):
            order.append(tips[key].info)
            order.extend(self.segments[key].buttons())
        order.extend((tips["flow.whole_review"].info, self.whole))
        for key in ("workflow.agents", "workflow.late"):
            order.append(tips[key].info)
            order.extend(self.segments[key].buttons())
        for key, picker in self.capacity.items():
            order.extend((tips[key].info, picker))
        if self.fallback_info is not None:
            order.append(self.fallback_info)
        order.extend(self.fallback_buttons)
        order.append(self.add_fallback)
        if self.panel_info is not None:
            order.append(self.panel_info)
        order.extend(self.member_removes.values())
        order.extend((self.remove_all, self.add_member))
        for row in tips.values():
            last = (
                row.control.buttons()[-1]
                if isinstance(row.control, widgets.Segmented)
                else row.control
            )
            if row.reset is not None:
                order.insert(order.index(last) + 1, row.reset)
        chain_tab_order(order)

    def _settings_changed(self, *_args: object) -> None:
        """Redraw settings and retry any failed list refresh."""
        self._settings_read_failed = False
        for save in (self.panel_save, self.fallback_save, self.routing_save):
            if save.refresh_failed:
                save.refresh()
        self._redraw()

    def _settings_failed(self, *_args: object) -> None:
        """Keep pending lists disabled until their settings can be read."""
        self._settings_read_failed = True
        for save in (self.panel_save, self.fallback_save, self.routing_save):
            if save.refreshing:
                save.refreshing = False
                save.refresh_failed = True

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
            picker.add_option("Ask each time", ASK, "The lead recommends and waits.")
            picker.add_option(
                AUTO_TEXT,
                choices.AUTO,
                "The lead starts a suitable routing pool model.",
            )
            missing = orphan(value, answers)
            if missing is not None:
                picker.add_option(missing[0], value)
            for engine, models in groups:
                picker.add_header(ENGINE_LABELS[engine])
                for model, effort in models:
                    text = option_text(model, effort)
                    picker.add_option(
                        text,
                        f"{engine}:{model}",
                        closed=f"{ENGINE_LABELS[engine]} · {text}",
                    )
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

    def _fill_derived(
        self,
        derived: _Derived,
        answers: dict[str, Any],
        groups: list[tuple[str, list[tuple[str, str | None]]]],
    ) -> None:
        value = answers[derived.key]["value"]
        picker = derived.picker
        picker.blockSignals(True)
        try:
            picker.clear()
            role = "writer" if derived.same == "Same as Writer" else "reviewer"
            resolved = self.roles[role].picker.closed_text()
            picker.add_option(
                derived.same, choices.SAME, closed=f"{derived.same} · {resolved}"
            )
            missing = None if value == choices.SAME else orphan(value, answers)
            if missing is not None:
                picker.add_option(missing[0], value)
            for engine, models in groups:
                picker.add_header(ENGINE_LABELS[engine])
                for model, effort in models:
                    text = option_text(model, effort)
                    picker.add_option(
                        text,
                        f"{engine}:{model}",
                        closed=f"{ENGINE_LABELS[engine]} · {text}",
                    )
            picker.set_value(value)
        finally:
            picker.blockSignals(False)
        derived.warning.show_text(None if missing is None else missing[1])

    def _show_run_as(self, controls: _Role) -> None:
        parts = _split(controls.picker.value())
        controls.run_as_row.setVisible(parts is not None and parts[0] == "claude")

    # Saving

    def _multi_picked(self, value: str) -> None:
        """Save the multi-task choice only when it changed."""
        if value != self.context.setting("multi.runs_as"):
            self._save({"multi.runs_as": value})

    def _derived_picked(self, derived: _Derived) -> None:
        value = derived.picker.value()
        if value is not None and value != self.context.setting(derived.key):
            self._save({derived.key: value})

    def _when_picked(self, _index: int) -> None:
        value = self.when.value()
        if value is not None and value != self.context.setting("gapcheck.when"):
            self._save({"gapcheck.when": value})

    def _capacity_picked(self, key: str) -> None:
        """Save a provider capacity setting only when it changed."""
        value = self.capacity[key].value()
        if value is not None and value != self.context.setting(key):
            self._save({key: value})

    def _segment_picked(self, key: str, value: str) -> None:
        """Save a segmented setting only when it changed."""
        if value != self.context.setting(key):
            self._save({key: value})

    def _whole_picked(self, checked: bool) -> None:
        value = "on" if checked else "off"
        if value != self.context.setting("flow.whole_review"):
            self._save({"flow.whole_review": value})

    def _rounds_picked(self, value: str) -> None:
        """Save the flow review rounds only when they changed."""
        if value != self.context.setting("flow.review_rounds"):
            self._save({"flow.review_rounds": value})

    def _add_member(self, _index: int) -> None:
        """Append the selected member and reset the picker to its prompt."""
        value = self.add_member.value()
        self.add_member.set_value(None)
        if self.panel_save.pending or value is None:
            return
        answers = self.context.settings()["answers"]
        members = answers["panel.members"]["value"]
        if len(members) >= PANEL_LIMIT or blocks_save(answers, members):
            return
        available = {
            f"{engine}:{model}"
            for engine, models in addable(answers, members)
            for model, _effort in models
        }
        if value in available:
            self._save_panel([*members, str(value)])

    def _remove_member(self, value: str) -> None:
        """Remove a member, or explain which remaining member blocks saving."""
        if self.panel_save.pending:
            return
        answers = self.context.settings()["answers"]
        members = [m for m in answers["panel.members"]["value"] if m != value]
        blocked = blocks_save(answers, members)
        if blocked:
            self._remove_blocker = blocked[0]
            self._redraw()
            return
        self._save_panel(members)

    def _save_panel(self, members: list[str]) -> None:
        """Serialize Panel edits through the context's save and refresh."""
        if self.panel_save.pending:
            return
        self._remove_blocker = None
        self.panel_save.save(members)

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
