"""The Team screen: Single tasks, Work of several tasks and Panel cards.

Two rows, Writer and Reviewer, each a picker of Ask each time and the
shortlisted models of every engine that is on, under the engine's name. While
a row holds a Claude model, a row under it picks how that model runs: as a
CLI or as a subagent. A saved value that the picker no longer offers stays
chosen, with a warning saying why. A saved model whose engine was not
connected shows a warning too, from the engine status the app read last, or
from the snapshot's menu rows before the first read. Each change saves at
once; choosing the value already saved saves nothing. Multi-task work chooses
Ask each time, Flow or Claude Workflow. Panel lists up to eight members with
Add member and Remove. Panel controls wait for a save and refresh before
accepting another edit. One-time choices are not shown.

Planning sets the gap check: its reviewer, when it runs and which gaps reach
the owner. Flows and Claude Workflows hold the settings of each kind of
multi-task work; Flows shows unless Multi-task work is Claude Workflow, and
Claude Workflows unless it is Flow. Every setting there has a tooltip.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ember import choices, db
from ember.app import icons, theme, widgets
from ember.app.screens.common import (
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

SUBTITLE = "Which workers Claude uses. Ask each time means Claude asks you."

MULTI = (("ask", "Ask each time"), ("flow", "Flow"), ("workflow", "Claude Workflow"))
MULTI_HELP = "How Claude runs two or more tasks that each need a worker."
RUN_AS_HELP = (
    "How a Claude model runs. CLI is a Claude Code worker started with emb run. "
    "Subagent is a subagent inside Claude's own session."
)
ROUNDS = tuple((value, value) for value in choices.REVIEW_ROUNDS)
ROUNDS_HELP = (
    "The most times a reviewer can check one flow task. "
    "A flow plan that sets its own rounds wins."
)
WHOLE_HELP = (
    "One review of all the flow's changes together. After every task of a "
    "flow merges, the Flow reviewer reads the whole flow's changes once, with "
    "the review bar, before Claude merges the flow."
)
PANEL_HELP = "The models a panel asks when Claude is given no members."
PANEL_LIMIT = 8
ADD_MEMBER = "Add member"
NO_MEMBERS = (
    "No members. Claude asks for members, or uses the ones it is given, each time."
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
        "Writer. A flow Claude starts with named workers uses those.",
        "Same as Writer",
    ),
    (
        "flow.reviewer",
        "Flow reviewer",
        "Reviews every flow task and, with Review the whole flow on, the whole "
        "flow once before Claude merges it. Same as Reviewer uses the "
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
    "When Claude has a plan checked for gaps before a flow or Claude Workflow "
    "starts. A plan the setting skips starts without a check and without a "
    "question; Claude's report says so."
)
ASKS = (("high", "High only"), ("medium", "High and medium"), ("all", "All"))
ASKS_TIP = (
    "Which gaps Claude brings to you. High only: medium and low gaps take "
    "their default. High and medium: low gaps take their default. All: high "
    "gaps one by one, medium and low together in one question. A gap that is "
    "not asked takes the default the gap check proposes, written into the "
    "plan, and Claude lists it in its report."
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
        "waiting on it and asks Claude.",
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
        "What Claude does when a Claude Workflow agent runs past its deadline, "
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
        0,
        10,
    ),
    (
        "provider.capacity_wait",
        "Wait before a retry",
        "Seconds between a capacity error and its resend. "
        "The model and effort stay the same.",
        5,
        600,
    ),
)
"""The provider retry pickers and their inclusive ranges."""


def when_hint(value: str) -> str:
    """Return the hint line under a Run the gap check option."""
    if value == "off":
        return "Every flow and Claude Workflow starts without one."
    if value == "always":
        return "Before every flow and Claude Workflow."
    if value == "ask":
        return "Claude asks you before each flow or Claude Workflow."
    least = int(value)
    rest = "1 task" if least == 2 else f"1 to {least - 1} tasks"
    return f"Plans of {rest} start without one."


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


def offered(
    answers: Mapping[str, Any],
) -> list[tuple[str, list[tuple[str, str | None]]]]:
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
    """Return offered models excluding the panel's saved members."""
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
        self.asks.setAccessibleName("Gaps Claude asks you about")
        self.asks.changed.connect(
            lambda value: self._segment_picked("gapcheck.ask", value)
        )
        self.asks_row = TooltipRow("Gaps Claude asks you about", ASKS_TIP, self.asks)
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
        for key, name, tip, low, high in CAPACITY:
            picker = widgets.Picker()
            picker.setAccessibleName(name)
            for number in range(low, high + 1):
                text = (
                    ("Off" if number == 0 else f"Up to {number}")
                    if key == "provider.capacity_retries"
                    else f"{number}s"
                )
                picker.add_option(text, str(number))
            picker.activated.connect(lambda _index, key=key: self._capacity_picked(key))
            self.capacity[key] = picker
            rows[key] = TooltipRow(name, tip, picker)
            capacity_column.addWidget(rows[key])
        layout.addWidget(capacity_card)

        panel_card, panel_layout = titled_card("Panel", PANEL_HELP)
        self.panel_info = panel_card.findChild(widgets.InfoTip)
        self.members_layout = QVBoxLayout()
        panel_layout.addLayout(self.members_layout)
        self.member_removes: dict[str, QPushButton] = {}
        self._panel_pending = False
        self._panel_refresh = False
        self._panel_refresh_failed = False
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
        self._fill_panel(answers, panel)
        align_controls(self.tip_rows.values())
        self._chain_focus()

    def _fill_panel(self, answers: dict[str, Any], panel: dict[str, Any]) -> None:
        """Draw saved members and the available additions from the snapshot."""
        clear_layout(self.members_layout)
        self.member_removes = {}
        members = panel["value"]
        if not members:
            self.members_layout.addWidget(label(NO_MEMBERS, "secondary"))
        for member in panel["members"]:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
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
            remove.setEnabled(not self._panel_pending)
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
        self.remove_all.setEnabled(not self._panel_pending)
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
        self.add_member.blockSignals(True)
        try:
            self.add_member.clear()
            self.add_member.add_option(ADD_MEMBER, None)
            for engine, models in groups:
                self.add_member.add_header(ENGINE_LABELS[engine])
                for model, effort in models:
                    self.add_member.add_option(
                        option_text(model, effort), f"{engine}:{model}"
                    )
        finally:
            self.add_member.blockSignals(False)
        # A line on the card or the screen says why a member cannot be added:
        # the eight-member limit, a member to fix first, every shortlisted
        # model added, or no shortlist.
        self.add_member.setVisible(not blocked and not full and bool(groups))
        self.add_member.setEnabled(not self._panel_pending)

    def _draw_icons(self) -> None:
        """Keep the member removal icons in the current theme's text colour."""
        for remove in self.member_removes.values():
            remove.setIcon(icons.icon("x", theme.color("text-primary"), 16))

    def _chain_focus(self) -> None:
        """Put the controls in reading order, including rebuilt member rows."""
        order: list[QWidget] = []
        tips = self.tip_rows
        for controls in self.roles.values():
            order.extend((tips[controls.key].info, controls.picker))
            order.append(tips[f"{controls.key}_as"].info)
            order.extend(controls.run_as.buttons())
        order.append(self.open_engines)
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
        if self.panel_info is not None:
            order.append(self.panel_info)
        order.extend(self.member_removes.values())
        order.extend((self.remove_all, self.add_member))
        chain_tab_order(order)

    def _settings_changed(self, *_args: object) -> None:
        """Redraw settings, allowing a retry after the Panel refresh failed."""
        self._settings_read_failed = False
        if self._panel_refresh_failed:
            self._panel_refresh_failed = False
            self._refresh_panel()
        self._redraw()

    def _panel_done(self, *_args: object) -> None:
        """Release Panel controls after the save and its settings refresh."""
        self._panel_pending = self._panel_refresh = self._panel_refresh_failed = False
        self._redraw()

    def _panel_result(self, result: dict[str, Any] | Exception) -> None:
        """Unlock after this Panel save succeeds or its error is refreshed."""
        if isinstance(result, db.StaleGenerationError):
            if self._settings_read_failed:
                self._panel_refresh_failed = True
            else:
                self._panel_done()
        elif isinstance(result, Exception):
            self._refresh_panel()
        else:
            self._panel_done()

    def _refresh_panel(self) -> None:
        """Track the Panel's settings refresh until it succeeds or fails."""
        self._panel_refresh = True
        self.context.reload_settings(self._panel_done)

    def _settings_failed(self, *_args: object) -> None:
        """Keep Panel disabled until a failed settings refresh succeeds.

        The Panel's result callback checks the read outcome for a stale save.
        """
        self._settings_read_failed = True
        if self._panel_refresh:
            self._panel_refresh = False
            self._panel_refresh_failed = True

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
        """Save a provider retry setting only when it changed."""
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
        if self._panel_pending or value is None:
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
        if self._panel_pending:
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
        if self._panel_pending:
            return
        self._panel_pending = True
        self._remove_blocker = None
        self._redraw()
        self.context.save_settings(
            {"panel.members": members}, result=self._panel_result
        )

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
