"""The Engines screen.

One card per engine, side by side: the maker's logo, the version, a status
badge, the Use and Sandbox switches, the sandbox provider, `bwrap` and the
last sandbox check, known blocking causes and, while the engine is not
connected, its setup steps with the commands that fix them. Models opens
one engine's model list below the cards, where models are put on the
shortlist with an effort.

Opening the screen reads the engine status again. Check now reads it and
runs the sandbox check of every engine whose Use and Sandbox switches are
on. A card whose sandbox is off shows its sandbox check as not run.
Every read, check and save runs in a task.
"""

import html
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from math import ceil
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ember import choices, sandbox
from ember.app import icons, theme
from ember.app.screens.common import (
    CARD_MARGIN,
    chain_tab_order,
    clear_layout,
    discard,
    engine_name,
    fitting_columns,
    inline,
    label,
    plural,
    screen_header,
    scrolled,
    sentence,
    when,
)
from ember.app.widgets import (
    Alert,
    Badge,
    Card,
    CommandLine,
    ElidedLabel,
    InfoTip,
    Picker,
    StateView,
    Switch,
    button,
    set_icon,
)
from ember.engines import models, status

if TYPE_CHECKING:
    from ember.app.context import AppContext

NAMES = {engine: engine_name(engine) for engine in status.ENGINES}
"""Each engine's shown name."""

PROVIDER_TEXT = {
    sandbox.CODEX: "Codex's own sandbox",
    sandbox.BUBBLEWRAP: "Ember's bubblewrap",
    sandbox.NONE: "None",
}
"""Who provides a sandbox, as the card names it."""

PRICE_CHOICES: tuple[tuple[str, str | None], ...] = (
    ("Any", None),
    ("$0.5", "0.5"),
    ("$1", "1"),
    ("$2", "2"),
    ("$5", "5"),
    ("$10", "10"),
)
"""The OpenCode input price filter: label and US dollars per million tokens."""

CONTEXT_CHOICES: tuple[tuple[str, int | None], ...] = (
    ("Any", None),
    ("32k", 32_000),
    ("64k", 64_000),
    ("128k", 128_000),
    ("256k", 256_000),
    ("1M", 1_000_000),
)
"""The OpenCode context filter: label and the smallest context in tokens."""

NO_EFFORT_NOTE = "This model reports no effort, so it cannot be shortlisted."
"""Shown on a Codex or Claude model row that has no effort to save."""

CHECK_AGAIN = "Check again after the command finishes."

CARD_GAP = 16
"""The space between engine cards."""
"""Shown on a card after the owner ran one of its setup commands."""


def badge_for(
    enabled: bool, engine_status: status.EngineStatus | None, provider: str
) -> tuple[str, str]:
    """Return a card's status badge, the first state that applies.

    Args:
        enabled: The engine's `engine.<x>` setting is on.
        engine_status: The engine's last status, or `None` before a read.
        provider: Who provides its sandbox, one of `sandbox.PROVIDERS`.

    Returns:
        `(word, tone)`.
    """
    if not enabled:
        return "Off", "neutral"
    if engine_status is None:
        return "Not checked", "neutral"
    if engine_status.state == status.NOT_INSTALLED:
        return "Not installed", "danger"
    if engine_status.state == status.NOT_SIGNED_IN:
        return "Not signed in", "warning"
    if engine_status.state == status.CHECK_FAILED:
        return "Check failed", "danger"
    if provider == sandbox.NONE:
        return "No sandbox", "warning"
    return "Connected", "success"


def cause_advice(cause: str) -> str:
    """Return what to do about a known sandbox blocking cause.

    Args:
        cause: One of `SandboxCheck.causes`.

    Returns:
        One sentence.
    """
    if "AppArmor" in cause:
        return (
            "Change that profile so bwrap can run, or turn this engine's sandbox off."
        )
    if "sandbox_mode" in cause:
        return "Remove the sandbox_mode key from that file."
    if "does not keep" in cause:
        return "Install Codex under ~/.local or a system folder."
    return "Remove the cause, or turn this engine's sandbox off."


def entry(engine: str, model: str, effort: str | None) -> str:
    """Return a shortlist entry as the `shortlist.<engine>` key holds it.

    Args:
        engine: `codex`, `opencode` or `claude`.
        model: The model id.
        effort: The effort; `None` only for OpenCode.

    Returns:
        `<model>@<effort>`, or `<model>` for OpenCode without an effort.
    """
    return model if effort is None else f"{model}@{effort}"


def shortlist_with(
    engine: str, entries: Sequence[str], model: str, effort: str | None, on: bool
) -> list[str]:
    """Return a shortlist with one model added, changed or removed.

    Other entries keep their order. A model added goes last; a model whose
    effort changes keeps its place.

    Args:
        engine: `codex`, `opencode` or `claude`.
        entries: The saved shortlist.
        model: The model id.
        effort: Its effort.
        on: Whether the model is on the shortlist after the change.

    Returns:
        The new shortlist.
    """
    result: list[str] = []
    placed = False
    for item in entries:
        if model in choices.shortlist_models(engine, [item]):
            if on and not placed:
                result.append(entry(engine, model, effort))
                placed = True
            continue
        result.append(item)
    if on and not placed:
        result.append(entry(engine, model, effort))
    return result


def initial_effort(engine: str, model: models.Model, saved: str | None) -> str | None:
    """Return the effort a model row shows first.

    A valid saved effort is kept. Otherwise OpenCode shows no effort (the
    model's default); Codex and Claude show the model's default effort when
    it is one of its efforts, else its first effort.

    Args:
        engine: `codex`, `opencode` or `claude`.
        model: The model.
        saved: The effort saved on the shortlist, or `None`.

    Returns:
        The effort, or `None` when there is none to show.
    """
    if saved is not None and saved in model.efforts:
        return saved
    if engine == "opencode":
        return None
    if model.default_effort in model.efforts:
        return model.default_effort
    return model.efforts[0] if model.efforts else None


def per_token(per_million: str | None) -> float | None:
    """Return a price per million tokens as US dollars per token."""
    if per_million is None:
        return None
    return float(Decimal(per_million) / Decimal(1_000_000))


def context_text(tokens: int | None) -> str:
    """Return a context window as "128k" or "1M"."""
    if tokens is None:
        return "—"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.3g}M"
    if tokens >= 1000:
        return f"{round(tokens / 1000)}k"
    return str(tokens)


def price_text(per_token_price: float | None) -> str:
    """Return a per-token price as US dollars per million tokens."""
    if per_token_price is None:
        return "—"
    return f"${per_token_price * 1_000_000:.2f}"


class _EngineCard(Card):
    """One engine's card."""

    def __init__(self, screen: EnginesScreen, engine: str) -> None:
        super().__init__()
        self.owner = screen
        self.engine = engine
        name = NAMES[engine]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_MARGIN, CARD_MARGIN, CARD_MARGIN, CARD_MARGIN)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        self.logo = QLabel()
        self.logo.setFixedSize(32, 32)
        self.logo.setAccessibleName(f"{name} logo")
        head.addWidget(self.logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.name_label = QLabel(name)
        self.name_label.setFont(theme.font("heading"))
        names.addWidget(self.name_label)
        self.version_label = ElidedLabel()
        self.version_label.setProperty("role", "secondary")
        names.addWidget(self.version_label)
        head.addLayout(names, 1)
        self.badge = Badge("Not checked")
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        self.reason_label = label(role="secondary")
        layout.addWidget(self.reason_label)

        use = QHBoxLayout()
        use_label = QLabel("Use")
        use_label.setFont(theme.font("label"))
        use.addWidget(use_label, 1)
        self.use_switch = Switch()
        self.use_switch.setAccessibleName(f"Use {name}")
        self.use_switch.clicked.connect(self._use_clicked)
        use.addWidget(self.use_switch)
        layout.addLayout(use)

        box = QHBoxLayout()
        box_label = QLabel("Sandbox")
        box_label.setFont(theme.font("label"))
        box.addWidget(box_label, 1)
        self.sandbox_switch = Switch()
        self.sandbox_switch.setAccessibleName(f"Sandbox for {name}")
        self.sandbox_switch.clicked.connect(self._sandbox_clicked)
        box.addWidget(self.sandbox_switch)
        layout.addLayout(box)
        self.provider_label = label(role="secondary")
        layout.addWidget(self.provider_label)
        self.none_label = label(
            f"{name} runs without a sandbox. Its workers can read every file "
            "you can, including logins and keys.",
            "secondary",
        )
        layout.addWidget(self.none_label)
        self.bwrap_label = label(role="secondary")
        self.bwrap_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.bwrap_label)
        self.check_label = label(role="secondary")
        self.check_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.check_label)
        self.cause_area = QVBoxLayout()
        self.cause_area.setSpacing(8)
        layout.addLayout(self.cause_area)
        self.cause_alert: Alert | None = None

        self.setup_widget = QWidget()
        self.setup_area = QVBoxLayout(self.setup_widget)
        self.setup_area.setContentsMargins(0, 0, 0, 0)
        self.setup_area.setSpacing(8)
        layout.addWidget(self.setup_widget)
        self.commands: list[CommandLine] = []

        self.again_widget = QWidget()
        again = QHBoxLayout(self.again_widget)
        again.setContentsMargins(0, 0, 0, 0)
        self.again_label = label(CHECK_AGAIN, "secondary")
        again.addWidget(self.again_label, 1)
        self.again_button = button("Check now", "secondary", size="sm")
        self.again_button.setAccessibleName(f"Check {name} now")
        self.again_button.clicked.connect(screen.check_now)
        again.addWidget(self.again_button)
        self.again_widget.hide()
        layout.addWidget(self.again_widget)

        layout.addStretch(1)
        models_row = QHBoxLayout()
        self.models_button = button("Models", "secondary", size="sm")
        self.models_button.setCheckable(True)
        self.models_button.setAccessibleName(f"{name} models")
        self.models_button.clicked.connect(lambda: screen.toggle_models(engine))
        models_row.addWidget(self.models_button)
        self.shortlisted_label = label(role="secondary")
        models_row.addWidget(self.shortlisted_label, 1)
        layout.addLayout(models_row)

        self._setup_key: object = None
        self.draw_logo()

    def draw_logo(self) -> None:
        """Draw the maker's logo in the current theme."""
        self.logo.setPixmap(icons.logo(self.engine, 32))

    def _use_clicked(self, on: bool) -> None:
        self.owner.save_switch(f"engine.{self.engine}", on)

    def _sandbox_clicked(self, on: bool) -> None:
        self.owner.save_sandbox(self.engine, on)

    def show_check_again(self, shown: bool) -> None:
        """Show or hide the line asking to check again."""
        self.again_widget.setVisible(shown)

    def update_view(self) -> None:
        """Draw the card from the screen's current data."""
        screen = self.owner
        engine = self.engine
        engines = screen.context.engines()
        engine_status = None if engines is None else engines.get(engine)
        enabled = screen.setting(f"engine.{engine}", "on") == "on"
        switch = screen.setting(f"sandbox.{engine}", "off")
        provider = sandbox.provider_of(engine, str(switch))
        word, tone = badge_for(enabled, engine_status, provider)
        self.badge.set_tone(tone, word)
        self.badge.setAccessibleName(f"{NAMES[engine]}: {word}")

        if engine_status is None:
            self.version_label.setText("Version not read yet")
        elif engine_status.version:
            self.version_label.setText(engine_status.version)
        else:
            self.version_label.setText("Version unknown")
        failed = (
            engine_status is not None and engine_status.state == status.CHECK_FAILED
        )
        self.reason_label.setVisible(failed)
        if failed and engine_status is not None:
            self.reason_label.setText(sentence(engine_status.reason or ""))

        self.use_switch.setChecked(enabled)
        self.sandbox_switch.setChecked(switch == "on")
        self.provider_label.setText(f"Provider: {PROVIDER_TEXT[provider]}")
        self.none_label.setVisible(provider == sandbox.NONE)
        self._draw_check(
            screen.checks.get(engine), engine in screen.checking, switch == "on"
        )

        saved = screen.setting(f"shortlist.{engine}", [])
        count = len(saved) if isinstance(saved, list) else 0
        self.shortlisted_label.setText(f"{count} shortlisted")
        self.models_button.setChecked(screen.open_engine == engine)
        self._draw_setup(engine_status)

    def _draw_check(
        self, check: sandbox.SandboxCheck | None, checking: bool, switched_on: bool
    ) -> None:
        """Draw the bwrap line, the last sandbox check and its blocking causes.

        With the sandbox switch off the card shows the check as not run.
        A check that ran without a sandbox is never shown as passed.
        """
        if check is not None and check.provider == sandbox.NONE:
            check = None
        if check is None:
            self.bwrap_label.setText("bwrap: not checked yet")
        elif check.bwrap_path is None:
            self.bwrap_label.setText("bwrap: not found")
        else:
            version = check.bwrap_version or "version unknown"
            family = html.escape(theme.font("code").family())
            path = html.escape(check.bwrap_path)
            self.bwrap_label.setText(
                f"bwrap: <span style=\"font-family: '{family}'\">{path}</span>, "
                f"{html.escape(version)}"
            )
        if not switched_on:
            self.check_label.setText("Sandbox check: not run")
            check = None
        elif checking:
            self.check_label.setText("Sandbox check: checking now")
        elif check is None:
            self.check_label.setText("Sandbox check: not run")
        else:
            at = inline(when(check.checked_at))
            if check.ok:
                self.check_label.setText(f"Sandbox check: passed, {at}")
            else:
                self.check_label.setText(f"Sandbox check: failed, {at}: {check.detail}")
        text = ""
        if check is not None and check.causes:
            text = "\n".join(
                f"{sentence(cause)}. {cause_advice(cause)}" for cause in check.causes
            )
        if (
            self.cause_alert is not None
            and self.cause_alert.accessibleDescription() == text
        ):
            return
        discard(self.cause_alert, self.cause_area)
        self.cause_alert = None
        if text:
            self.cause_alert = Alert("danger", "The sandbox is blocked", text)
            self.cause_area.addWidget(self.cause_alert)

    def _draw_setup(self, engine_status: status.EngineStatus | None) -> None:
        key = None if engine_status is None else engine_status.to_json()
        if key == self._setup_key:
            return
        self._setup_key = key
        clear_layout(self.setup_area)
        self.commands = []
        unmet = (
            []
            if engine_status is None or engine_status.connected
            else [item for item in engine_status.requirements if not item.met]
        )
        if engine_status is None or not unmet:
            # A failed check whose requirements are all met (only the
            # version could not be read) shows its reason above, not here.
            self.setup_widget.hide()
            return
        heading = QLabel("Setup")
        heading.setFont(theme.font("label"))
        self.setup_area.addWidget(heading)
        for requirement in unmet:
            self.setup_area.addWidget(label(sentence(requirement.label)))
            if requirement.fix is None:
                reason = engine_status.reason or "Ember found no command that fixes it."
                self.setup_area.addWidget(label(sentence(reason), "secondary"))
                continue
            line = CommandLine(requirement.fix)
            line.run_requested.connect(self._run)
            self.setup_area.addWidget(line)
            self.commands.append(line)
        self.setup_widget.show()

    def _run(self, command: str) -> None:
        self.owner.run_command(self.engine, command)

    def focus_order(self) -> list[QWidget]:
        """Return the card's controls in reading order."""
        order: list[QWidget] = [self.use_switch, self.sandbox_switch]
        if self.cause_alert is not None:
            order += self.cause_alert.buttons
        for line in self.commands:
            order += [line.copy_button, line.run_button]
        order += [self.again_button, self.models_button]
        return order


class _ModelRow(QWidget):
    """One model in a model list: its columns, effort and Use switch."""

    def __init__(
        self,
        panel: _ModelPanel,
        model: models.Model,
        effort: str | None,
        columns: bool,
    ) -> None:
        super().__init__()
        self.panel = panel
        self.model = model
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        engine = panel.engine
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(2)
        layout = QHBoxLayout()
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        self.cells = QWidget()
        self.cells.setLayout(layout)
        self.cells.setFixedHeight(theme.CONTROL_MD)
        outer.addWidget(self.cells)
        self.name = ElidedLabel(model.id)
        self.name.setFixedWidth(panel.name_width)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.name, 0, Qt.AlignmentFlag.AlignVCenter)
        self.figures: list[QLabel] = []
        if columns:
            provider = ElidedLabel(model.provider_name or model.provider or "—")
            provider.setProperty("role", "secondary")
            provider.setFixedWidth(_PROVIDER_WIDTH)
            layout.addWidget(provider, 0, Qt.AlignmentFlag.AlignVCenter)
            self.figures.append(provider)
            for text in (
                context_text(model.context_length),
                "Subscription"
                if model.login == "oauth"
                else price_text(model.prompt_price),
                "Subscription"
                if model.login == "oauth"
                else price_text(model.completion_price),
            ):
                cell = QLabel(text)
                cell.setProperty("role", "secondary")
                cell.setFixedWidth(_COLUMN)
                cell.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                layout.addWidget(cell, 0, Qt.AlignmentFlag.AlignVCenter)
                self.figures.append(cell)
        self.picker: Picker | None = None
        if model.efforts:
            self.picker = Picker()
            self.picker.setFixedWidth(_EFFORT_WIDTH)
            self.picker.setAccessibleName(f"Effort for {model.id}")
            if engine == "opencode":
                self.picker.add_option("Default", None)
            for item in model.efforts:
                self.picker.add_option(item, item)
            self.picker.set_value(effort)
            self.picker.activated.connect(lambda _index: panel.effort_changed(self))
            self.effort_cell = self.picker
        else:
            none = QLabel()
            none.setFixedWidth(_EFFORT_WIDTH)
            self.effort_cell = none
        layout.addWidget(self.effort_cell, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        self.switch = Switch()
        self.switch.setAccessibleName(f"Shortlist {model.id}")
        self.switch.clicked.connect(lambda on: panel.use_changed(self, on))
        use = QWidget()
        use.setFixedWidth(panel.use_width)
        use_layout = QHBoxLayout(use)
        use_layout.setContentsMargins(0, 0, 0, 0)
        use_layout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(use, 0, Qt.AlignmentFlag.AlignVCenter)
        self.note = label(role="secondary")
        self.note.hide()
        outer.addWidget(self.note)

    def effort(self) -> str | None:
        """Return the effort the row shows, or `None`."""
        if self.picker is None:
            return None
        value = self.picker.value()
        return value if isinstance(value, str) else None

    def show_note(self, text: str) -> None:
        """Show a line under the row; empty text hides it."""
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def focus_order(self) -> list[QWidget]:
        """Return the row's controls in reading order."""
        return [w for w in (self.picker, self.switch) if w is not None]


class _MissingRow(QWidget):
    """A shortlisted model the engine's list no longer holds."""

    def __init__(self, panel: _ModelPanel, model: str, effort: str | None) -> None:
        super().__init__()
        self.model_id = model
        self.setFixedHeight(theme.CONTROL_MD + 8)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)
        self.name = ElidedLabel(model)
        self.name.setFixedWidth(panel.name_width)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.name, 0, Qt.AlignmentFlag.AlignVCenter)
        state = "Not in the list"
        if effort is not None:
            state += f" · effort {effort}"
        self.info = ElidedLabel(state)
        self.info.setProperty("role", "secondary")
        figures_width = (
            _PROVIDER_WIDTH + 12 + 3 * (_COLUMN + 12) if panel.columns else 0
        )
        self.info.setFixedWidth(figures_width + _EFFORT_WIDTH)
        layout.addWidget(self.info, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        self.remove_button = button("Remove", "secondary", size="sm")
        self.remove_button.setAccessibleName(f"Remove {model} from the shortlist")
        self.remove_button.setFixedWidth(panel.use_width)
        self.remove_button.clicked.connect(lambda: panel.remove(model))
        layout.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignVCenter)


_COLUMN = 110
_PROVIDER_WIDTH = 130
_EFFORT_WIDTH = 130


class _ModelPanel(Card):
    """One engine's model list, below the cards."""

    def __init__(self, screen: EnginesScreen, engine: str) -> None:
        super().__init__()
        self.owner = screen
        self.engine = engine
        self.columns = engine == "opencode"
        self.name_width = 0
        measure = button("Remove", "secondary", size="sm")
        self.use_width = max(Switch.WIDTH + 4, measure.sizeHint().width())
        measure.deleteLater()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_MARGIN, CARD_MARGIN, CARD_MARGIN, CARD_MARGIN)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel(f"{NAMES[engine]} models")
        title.setFont(theme.font("heading"))
        head.addWidget(title, 1)
        self.refresh_button = button("Refresh list", "secondary", size="sm")
        self.refresh_button.clicked.connect(lambda: self.load(refresh=True))
        head.addWidget(self.refresh_button)
        layout.addLayout(head)

        self.notice_area = QVBoxLayout()
        layout.addLayout(self.notice_area)
        self.notice: Alert | None = None

        self.search: QLineEdit | None = None
        self.provider_picker: Picker | None = None
        self.provider_info: InfoTip | None = None
        self.tools_switch: Switch | None = None
        self.price_picker: Picker | None = None
        self.context_picker: Picker | None = None
        self.count_label: QLabel | None = None
        if self.columns:
            self._add_filters(layout)

        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        self.name_header = label("Model", "muted", wrap=False)
        header.addWidget(self.name_header)
        if self.columns:
            provider = label("Provider", "muted", wrap=False)
            provider.setFixedWidth(_PROVIDER_WIDTH)
            header.addWidget(provider)
            for text in ("Context", "Input $/M", "Output $/M"):
                cell = QLabel(text)
                cell.setProperty("role", "muted")
                cell.setFixedWidth(_COLUMN)
                cell.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                header.addWidget(cell)
        effort = QLabel("Effort")
        effort.setProperty("role", "muted")
        effort.setFixedWidth(_EFFORT_WIDTH)
        header.addWidget(effort)
        header.addStretch(1)
        use = QLabel("Use")
        use.setProperty("role", "muted")
        use.setFixedWidth(self.use_width)
        use.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(use)
        self.header.hide()
        layout.addWidget(self.header)

        self.rows_widget = QWidget()
        self.rows_area = QVBoxLayout(self.rows_widget)
        self.rows_area.setContentsMargins(0, 0, 0, 0)
        self.rows_area.setSpacing(0)
        layout.addWidget(self.rows_widget)
        self.rows: list[_ModelRow] = []

        self.missing_widget = QWidget()
        self.missing_area = QVBoxLayout(self.missing_widget)
        self.missing_area.setContentsMargins(0, 0, 0, 0)
        self.missing_area.setSpacing(0)
        layout.addWidget(self.missing_widget)
        self.missing: list[_MissingRow] = []

        self.state = StateView()
        layout.addWidget(self.state)
        self.models: tuple[models.Model, ...] | None = None
        self.delivered = False
        self._token = 0

    def _add_filters(self, layout: QVBoxLayout) -> None:
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search models")
        self.search.setAccessibleName("Search models")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filters)
        layout.addWidget(self.search)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(QLabel("Provider"))
        self.provider_info = InfoTip(
            "Provider",
            "The OpenCode integration that serves the model. Ember lists the "
            "integrations you connected with /connect in OpenCode.",
        )
        row.addWidget(self.provider_info)
        self.provider_picker = Picker()
        self.provider_picker.setAccessibleName("Provider")
        self.provider_picker.add_option("All providers", None)
        self.provider_picker.currentIndexChanged.connect(
            lambda _i: self.apply_filters()
        )
        row.addWidget(self.provider_picker)
        row.addSpacing(12)
        row.addWidget(QLabel("Tools"))
        row.addWidget(
            InfoTip(
                "Tools",
                "Shows only models that take tool calls. A worker needs tool "
                "calls to read and change files.",
            )
        )
        self.tools_switch = Switch()
        self.tools_switch.setChecked(True)
        self.tools_switch.setAccessibleName("Only models that take tool calls")
        self.tools_switch.clicked.connect(lambda _on: self.apply_filters())
        row.addWidget(self.tools_switch)
        row.addStretch(1)
        self.count_label = label(role="secondary")
        row.addWidget(self.count_label)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(QLabel("Max price"))
        row.addWidget(
            InfoTip(
                "Max price",
                "No more than this input price per million tokens. Models that "
                "show Subscription always pass.",
            )
        )
        self.price_picker = Picker()
        self.price_picker.setAccessibleName("Max price")
        for text, value in PRICE_CHOICES:
            self.price_picker.add_option(text, value)
        self.price_picker.setCurrentIndex(0)
        self.price_picker.currentIndexChanged.connect(lambda _i: self.apply_filters())
        row.addWidget(self.price_picker)
        row.addSpacing(12)
        row.addWidget(QLabel("Min context"))
        row.addWidget(
            InfoTip(
                "Min context",
                "At least this many tokens of context: how much text the model "
                "takes in at once.",
            )
        )
        self.context_picker = Picker()
        self.context_picker.setAccessibleName("Min context")
        for text, value in CONTEXT_CHOICES:
            self.context_picker.add_option(text, value)
        self.context_picker.setCurrentIndex(0)
        self.context_picker.currentIndexChanged.connect(lambda _i: self.apply_filters())
        row.addWidget(self.context_picker)
        row.addStretch(1)
        layout.addLayout(row)

    def load(self, refresh: bool = False) -> None:
        """Read the model list in a task.

        Args:
            refresh: Read the source even when the cache is current.
        """
        self._token += 1
        token = self._token
        engine, env = self.engine, self.owner.context.env
        self._set_notice(None)
        if self.models is None:
            self.state.show_loading(f"Reading the {NAMES[engine]} model list")

        def read() -> models.ModelList:
            return models.list_models(engine, env, refresh=refresh)

        def failed(error: Exception) -> None:
            self.owner.model_list_failed(self, token, error)

        self.owner.context.tasks.run(
            read,
            lambda result: self.owner.model_list_read(self, token, result),
            failed,
        )

    def is_current(self, token: int) -> bool:
        """Return whether a read's result is still wanted."""
        return token == self._token

    def show_list(self, result: models.ModelList) -> None:
        """Show a model list that was read."""
        self.state.clear()
        clear_layout(self.rows_area)
        self.rows = []
        self.models = result.models
        if self.provider_picker is not None:
            selected = self.provider_picker.value()
            counts = Counter(model.provider for model in result.models)
            self.provider_picker.blockSignals(True)
            try:
                self.provider_picker.clear()
                self.provider_picker.add_option(
                    "All providers", None, plural(len(result.models), "model")
                )
                for integration in result.integrations:
                    self.provider_picker.add_option(
                        integration.name,
                        integration.id,
                        plural(counts[integration.id], "model"),
                    )
                index = self.provider_picker.findData(selected)
                self.provider_picker.setCurrentIndex(max(0, index))
            finally:
                self.provider_picker.blockSignals(False)
        self.delivered = bool(result.models) or result.reason is None
        saved = self.saved()
        for model in result.models:
            effort = initial_effort(self.engine, model, saved.get(model.id))
            row = _ModelRow(self, model, effort, self.columns)
            row.switch.setChecked(model.id in saved)
            self.rows_area.addWidget(row)
            self.rows.append(row)
        self.header.setVisible(bool(result.models))
        self._draw_missing(saved)
        if result.reason is not None and result.models:
            at = (
                ""
                if result.fetched_at is None
                else f" It shows the list read {inline(when(result.fetched_at))}."
            )
            self._set_notice(
                Alert(
                    "warning",
                    f"The {NAMES[self.engine]} model list could not be read again.",
                    f"{sentence(result.reason)}.{at}",
                    actions=[("Try again", lambda: self.load(refresh=True))],
                )
            )
        elif result.reason is not None:
            self.show_error(result.reason)
        elif not result.models:
            self.state.show_empty(
                "inbox", "No models", f"{NAMES[self.engine]} reported no models."
            )
        self.apply_filters()
        self.owner.chain_focus()

    def show_error(self, reason: str) -> None:
        """Show that the list could not be read, with Try again."""
        self.state.show_error(
            f"The {NAMES[self.engine]} model list could not be read: {reason}. "
            "Try again; if it keeps failing, check the engine and the network.",
            lambda: self.load(refresh=True),
        )

    def _set_notice(self, alert: Alert | None) -> None:
        discard(self.notice, self.notice_area)
        self.notice = alert
        if alert is not None:
            self.notice_area.addWidget(alert)

    def saved(self) -> dict[str, str | None]:
        """Return the saved shortlist as model to effort."""
        entries = self.owner.setting(f"shortlist.{self.engine}", [])
        if not isinstance(entries, list):
            return {}
        return choices.shortlist_models(self.engine, entries)

    def _absent(self, saved: dict[str, str | None]) -> list[tuple[str, str | None]]:
        """Return the saved entries missing from a delivered model list."""
        if self.models is None or not self.delivered:
            return []
        known = {model.id for model in self.models}
        return [(m, e) for m, e in saved.items() if m not in known]

    def _draw_missing(self, saved: dict[str, str | None]) -> None:
        self._measure_names(saved)
        clear_layout(self.missing_area)
        self.missing = []
        absent = self._absent(saved)
        if absent:
            heading = QLabel("Shortlisted, not in the list")
            heading.setFont(theme.font("label"))
            self.missing_area.addWidget(heading)
        for model, effort in absent:
            row = _MissingRow(self, model, effort)
            self.missing_area.addWidget(row)
            self.missing.append(row)
        self.missing_widget.setVisible(bool(absent))

    def _measure_names(self, saved: dict[str, str | None]) -> None:
        """Fit the header and all model ids, up to 320 logical pixels."""

        def width(label: QLabel, texts: Sequence[str]) -> int:
            label.ensurePolished()
            label.setFixedWidth(320)
            extra = label.width() - label.contentsRect().width()
            metrics = QFontMetricsF(label.font())
            return ceil(max(metrics.horizontalAdvance(text) for text in texts) + extra)

        measured = width(self.name_header, [self.name_header.text()])
        names = [model.id for model in self.models or ()]
        names += [model for model, _effort in self._absent(saved)]
        if names:
            name_label = (
                self.rows[0].name if self.rows else ElidedLabel(parent=self.rows_widget)
            )
            name_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            measured = max(measured, width(name_label, names))
            if not self.rows:
                name_label.deleteLater()
        self.name_width = min(320, measured)
        self.name_header.setFixedWidth(self.name_width)
        for row in self.rows:
            row.name.setFixedWidth(self.name_width)

    def sync(self) -> None:
        """Show the saved shortlist on the rows."""
        saved = self.saved()
        for row in self.rows:
            on = row.model.id in saved
            row.switch.setChecked(on)
            if on and row.picker is not None:
                effort = initial_effort(self.engine, row.model, saved[row.model.id])
                row.picker.set_value(effort)
        missing = {row.model_id for row in self.missing}
        if missing != {m for m, _e in self._absent(saved)}:
            self._draw_missing(saved)
            self.owner.chain_focus()

    def apply_filters(self) -> None:
        """Show the rows that pass the OpenCode search and filters."""
        if not self.columns or self.models is None:
            return
        assert self.search is not None
        assert self.tools_switch is not None
        assert self.price_picker is not None
        assert self.context_picker is not None
        assert self.count_label is not None
        assert self.provider_picker is not None
        text = self.search.text().strip()
        price_value = self.price_picker.value()
        context_value = self.context_picker.value()
        provider_value = self.provider_picker.value()
        kept = models.filter_opencode(
            self.models,
            text=text or None,
            max_prompt_price=per_token(
                price_value if isinstance(price_value, str) else None
            ),
            min_context=context_value if isinstance(context_value, int) else None,
            tools=True if self.tools_switch.isChecked() else None,
            provider=provider_value if isinstance(provider_value, str) else None,
        )
        ids = {model.id for model in kept}
        for row in self.rows:
            row.setVisible(row.model.id in ids)
        self.count_label.setText(f"{len(kept)} of {len(self.models)} models")
        if self.models and not kept:
            self.state.show_empty(
                "search", "No models match", "Change the search or the filters."
            )
        elif self.models:
            self.state.clear()

    def use_changed(self, row: _ModelRow, on: bool) -> None:
        """Save a row's Use switch."""
        effort = row.effort()
        if on and effort is None and self.engine != "opencode":
            row.switch.setChecked(False)
            row.show_note(NO_EFFORT_NOTE)
            return
        row.show_note("")
        self._save(row.model.id, effort, on)

    def effort_changed(self, row: _ModelRow) -> None:
        """Save a row's effort when the model is on the shortlist."""
        if row.model.id in self.saved():
            self._save(row.model.id, row.effort(), True)

    def remove(self, model: str) -> None:
        """Take a model off the shortlist."""
        self._save(model, None, False)

    def _save(self, model: str, effort: str | None, on: bool) -> None:
        entries = self.owner.setting(f"shortlist.{self.engine}", [])
        current = entries if isinstance(entries, list) else []
        updated = shortlist_with(self.engine, current, model, effort, on)
        self.owner.save_shortlist(self.engine, updated)

    def focus_order(self) -> list[QWidget]:
        """Return the panel's controls in reading order."""
        order: list[QWidget] = [self.refresh_button]
        if self.notice is not None:
            order += self.notice.buttons
        order += [
            w
            for w in (
                self.search,
                self.provider_info,
                self.provider_picker,
                self.tools_switch,
                self.price_picker,
                self.context_picker,
            )
            if w is not None
        ]
        for row in self.rows:
            order += row.focus_order()
        order += [row.remove_button for row in self.missing]
        order.append(self.state.retry_button)
        return order


class EnginesScreen(QWidget):
    """The Engines screen: each engine's status, sandbox and models."""

    TITLE = "Engines"

    def __init__(self, context: AppContext) -> None:
        """Create the screen.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        self.checks: dict[str, sandbox.SandboxCheck] = {}
        self.checking: set[str] = set()
        self.open_engine: str | None = None
        self.panel: _ModelPanel | None = None
        self._check_tokens: dict[str, int] = {}
        self._reading_engines = False

        layout = scrolled(self)
        self.content_layout = layout
        self.check_button = button("Check now", "secondary")
        self.check_button.setAccessibleName("Check now")
        self.check_button.clicked.connect(self.check_now)
        self.subtitle = screen_header(layout, self.TITLE, trailing=(self.check_button,))

        self.failure_area = QVBoxLayout()
        layout.addLayout(self.failure_area)
        self.failure: Alert | None = None

        self.cards_grid = QGridLayout()
        self.cards_grid.setHorizontalSpacing(CARD_GAP)
        self.cards_grid.setVerticalSpacing(CARD_GAP)
        self.cards: dict[str, _EngineCard] = {}
        for engine in status.ENGINES:
            self.cards[engine] = _EngineCard(self, engine)
        layout.addLayout(self.cards_grid)
        area = self.findChild(QScrollArea)
        assert area is not None
        self.scroll_area = area
        self.columns = len(self.cards)
        self._place_cards()
        self.scroll_area.installEventFilter(self)

        self.panel_area = QVBoxLayout()
        layout.addLayout(self.panel_area)
        layout.addStretch(1)

        context.engines_changed.connect(self._engines_read)
        context.engines_failed.connect(self._engines_failed)
        context.settings_changed.connect(self._settings_changed)
        context.stale.connect(self._settings_changed)
        context.error.connect(self._settings_changed)
        theme.signals.changed.connect(self._theme_changed)
        self._draw_check_icon()
        self._redraw()
        self._read_checks()

    # Data

    def setting(self, name: str, default: Any) -> Any:  # noqa: ANN401 - typed per key
        """Return a setting's value, or `default` before the first snapshot."""
        value = self.context.setting(name)
        return default if value is None else value

    def shown(self) -> None:
        """Read the engine status again (design D2) and the last checks."""
        self._reading_engines = True
        self._draw_subtitle()
        self.context.refresh_engines()
        self._read_checks()

    def _read_checks(self) -> None:
        env = self.context.env

        def read() -> dict[str, sandbox.SandboxCheck]:
            return sandbox.last_checks(env)

        tokens = dict(self._check_tokens)

        def loaded(result: dict[str, sandbox.SandboxCheck]) -> None:
            for engine, check in result.items():
                # A check started since this read began, still running or
                # finished, is newer than the cached one.
                if engine in self.checking or self._check_tokens.get(
                    engine
                ) != tokens.get(engine):
                    continue
                held = self.checks.get(engine)
                if held is None or held.checked_at < check.checked_at:
                    self.checks[engine] = check
            self._redraw()

        self.context.tasks.run(read, loaded, lambda _error: None)

    def fitting_columns(self) -> int:
        """Return how many engine cards, up to three, fit side by side."""
        tile = max(card.minimumSizeHint().width() for card in self.cards.values())
        margins = self.content_layout.contentsMargins()
        return fitting_columns(
            tile,
            self.scroll_area,
            len(self.cards),
            CARD_GAP,
            margins.left() + margins.right(),
        )

    def _place_cards(self) -> None:
        for card in self.cards.values():
            self.cards_grid.removeWidget(card)
        for index in range(len(self.cards)):
            self.cards_grid.setColumnStretch(index, 1 if index < self.columns else 0)
        for index, card in enumerate(self.cards.values()):
            self.cards_grid.addWidget(card, index // self.columns, index % self.columns)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: D102, N802
        if watched is self.scroll_area and event.type() == QEvent.Type.Resize:
            columns = self.fitting_columns()
            if columns != self.columns:
                self.columns = columns
                self._place_cards()
        return super().eventFilter(watched, event)

    def _engines_read(self, _result: dict) -> None:
        self._reading_engines = False
        self._set_failure(None)
        self._redraw()

    def _engines_failed(self, text: str) -> None:
        self._reading_engines = False
        self._set_failure(
            Alert("danger", text, "", actions=[("Try again", self.check_now)])
        )
        self._draw_subtitle()

    def _settings_changed(self, *_args: object) -> None:
        self._redraw()
        if self.panel is not None:
            self.panel.sync()

    def _theme_changed(self, *_args: object) -> None:
        for card in self.cards.values():
            card.draw_logo()
        self._draw_check_icon()

    def _draw_check_icon(self) -> None:
        set_icon(
            self.check_button, icons.icon("refresh-cw", theme.color("text-primary"), 16)
        )

    def _set_failure(self, alert: Alert | None) -> None:
        discard(self.failure, self.failure_area)
        self.failure = alert
        if alert is not None:
            self.failure_area.addWidget(alert)
        self.chain_focus()

    # Drawing

    def _draw_subtitle(self) -> None:
        engines = self.context.engines()
        if engines:
            at = max(item.checked_at for item in engines.values())
            self.subtitle.setText(f"Checked {inline(when(at))}")
        elif self._reading_engines:
            self.subtitle.setText("Checking the engines")
        else:
            self.subtitle.setText("Not checked yet")

    def _redraw(self) -> None:
        self._draw_subtitle()
        for card in self.cards.values():
            card.update_view()
        self.chain_focus()

    def chain_focus(self) -> None:
        """Set the Tab order of every control in reading order."""
        order: list[QWidget] = [self.check_button]
        if self.failure is not None:
            order += self.failure.buttons
        for card in self.cards.values():
            order += card.focus_order()
        if self.panel is not None:
            order += self.panel.focus_order()
        chain_tab_order(order)

    # Actions

    def check_now(self) -> None:
        """Read the engine status and run the sandbox checks.

        Only engines whose Use and Sandbox switches are both on are checked.
        """
        for card in self.cards.values():
            card.show_check_again(False)
        self._reading_engines = True
        self._draw_subtitle()
        self.context.refresh_engines()
        for engine in status.ENGINES:
            if (
                self.setting(f"engine.{engine}", "on") == "on"
                and self.setting(f"sandbox.{engine}", "off") == "on"
            ):
                self.run_sandbox_check(engine)

    def run_sandbox_check(self, engine: str) -> None:
        """Run an engine's sandbox check in a task and keep its result.

        Args:
            engine: `codex`, `opencode` or `claude`.
        """
        token = self._check_tokens.get(engine, 0) + 1
        self._check_tokens[engine] = token
        self.checking.add(engine)
        env = self.context.env

        def check() -> sandbox.SandboxCheck:
            result = sandbox.check(engine, env)
            sandbox.save_check(result, env)
            return result

        def done(result: sandbox.SandboxCheck) -> None:
            if self._check_tokens.get(engine) != token:
                return
            self.checking.discard(engine)
            self.checks[engine] = result
            self._redraw()

        def failed(error: Exception) -> None:
            if self._check_tokens.get(engine) != token:
                return
            self.checking.discard(engine)
            self.context.error.emit(
                f"The {NAMES[engine]} sandbox check could not run: {error}. "
                "Try Check now again."
            )
            self._redraw()

        self.context.tasks.run(check, done, failed)
        self._redraw()

    def save_switch(self, key: str, on: bool) -> None:
        """Save an on or off setting.

        Args:
            key: The settings key.
            on: The new value.
        """
        self.context.save_settings({key: "on" if on else "off"})

    def save_sandbox(self, engine: str, on: bool) -> None:
        """Save an engine's sandbox switch; after turning it on, run its check.

        Args:
            engine: `codex`, `opencode` or `claude`.
            on: The new value.
        """
        if not on:
            self._drop_check(engine)
            self.context.save_settings({f"sandbox.{engine}": "off"})
            return
        self.context.save_settings(
            {f"sandbox.{engine}": "on"},
            lambda _snapshot: self.run_sandbox_check(engine),
        )

    def _drop_check(self, engine: str) -> None:
        """Forget the result of an engine's check in flight."""
        self._check_tokens[engine] = self._check_tokens.get(engine, 0) + 1
        self.checking.discard(engine)

    def save_shortlist(self, engine: str, entries: list[str]) -> None:
        """Save an engine's shortlist.

        Args:
            engine: `codex`, `opencode` or `claude`.
            entries: The whole new shortlist.
        """
        self.context.save_settings({f"shortlist.{engine}": entries})

    def run_command(self, engine: str, command: str) -> None:
        """Ask to run a setup command; on Run in terminal, ask to check again.

        Args:
            engine: The card's engine.
            command: The command.
        """
        if self.context.run_command(self, command):
            self.cards[engine].show_check_again(True)

    def toggle_models(self, engine: str) -> None:
        """Open an engine's model list below the cards, or close it.

        Args:
            engine: `codex`, `opencode` or `claude`.
        """
        closing = self.open_engine == engine
        if self.panel is not None:
            discard(self.panel, self.panel_area)
            self.panel = None
            self.open_engine = None
        if not closing:
            self.open_engine = engine
            self.panel = _ModelPanel(self, engine)
            self.panel_area.addWidget(self.panel)
            self.panel.load()
        self._redraw()

    def model_list_read(
        self, panel: _ModelPanel, token: int, result: models.ModelList
    ) -> None:
        """Show a model list read in a task, when its panel is still open."""
        if panel is self.panel and panel.is_current(token):
            panel.show_list(result)

    def model_list_failed(
        self, panel: _ModelPanel, token: int, error: Exception
    ) -> None:
        """Show a model list read that raised, when its panel is still open."""
        if panel is self.panel and panel.is_current(token):
            panel.show_error(str(error))
