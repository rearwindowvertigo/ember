"""The Dependencies screen (design J2, K).

One tile per component of the last dependency report, in three groups:
Engines and apps, System tools, Ember's libraries. Each tile shows the
component's status, its installed and newest versions, and the update or
install command when there is one, which runs only after the Run this
command? dialog. Opening the screen shows the last report; Check now reads
installed versions, and newest versions when due.
"""

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ember import deps
from ember.app import icons, theme, widgets
from ember.app.screens.common import (
    SCREEN_MARGIN,
    SCREEN_SPACING,
    discard,
    inline,
    label,
    plural,
    screen_header,
    scroll_area,
    when,
)

if TYPE_CHECKING:
    from ember.app.context import AppContext

GROUP_TITLES = (
    ("apps", "Engines and apps"),
    ("system", "System tools"),
    ("libraries", "Ember's libraries"),
)
"""Each group's key and heading, in screen order."""

STATUS_ORDER = (deps.UPDATE_AVAILABLE, deps.MISSING, deps.CHECK_FAILED, deps.CURRENT)
"""The order of tiles within a group, before their labels."""

BADGES = {
    deps.UPDATE_AVAILABLE: ("Update", "info"),
    deps.MISSING: ("Missing", "warning"),
    deps.CHECK_FAILED: ("Check failed", "danger"),
    deps.CURRENT: ("Current", "success"),
}
"""Each status's badge word and tone."""

COLUMNS = 3
"""Tiles per row within a group."""

NOT_READ = "Not read"
"""Shown in place of a version that was not read."""


def subtitle(report: deps.DepsReport, now: float | None = None) -> str:
    """Return the screen's subtitle for a report.

    Args:
        report: The dependency report.
        now: The current time in seconds since the epoch; `None` reads it.

    Returns:
        What needs the owner, then when newest versions were read, for
        example "3 updates available. Newest versions read today 08:12."
    """
    statuses = [item.status for item in report.components]
    parts = [
        plural(count, one, many)
        for count, one, many in (
            (
                statuses.count(deps.UPDATE_AVAILABLE),
                "update available",
                "updates available",
            ),
            (statuses.count(deps.MISSING), "item missing", "items missing"),
            (statuses.count(deps.CHECK_FAILED), "check failed", "checks failed"),
        )
        if count
    ]
    if not parts:
        summary = "Everything is current."
    elif len(parts) == 1:
        summary = f"{parts[0]}."
    else:
        summary = f"{', '.join(parts[:-1])} and {parts[-1]}."
    if report.components and all(
        item.newest_kind == "none" for item in report.components
    ):
        return summary
    oldest = report.oldest_newest_read()
    if oldest is None:
        read = "Newest versions not read yet."
    else:
        stamp = inline(when(oldest, now))
        if report.newest_read_complete():
            read = f"Newest versions read {stamp}."
        else:
            read = f"Some newest versions not read; the others read {stamp}."
    return f"{summary} {read}"


def ordered(
    report: deps.DepsReport,
) -> list[tuple[str, str, list[deps.Component]]]:
    """Return the report's components by group, in screen order.

    Args:
        report: The dependency report.

    Returns:
        `(group key, heading, components)` for each group that has a
        component; within a group, by status (update available, missing,
        check failed, current), then by label.
    """
    result = []
    for key, title in GROUP_TITLES:
        items = sorted(
            (item for item in report.components if item.group == key),
            key=lambda item: (
                STATUS_ORDER.index(item.status)
                if item.status in STATUS_ORDER
                else len(STATUS_ORDER),
                item.label.casefold(),
            ),
        )
        if items:
            result.append((key, title, items))
    return result


def versions_line(item: deps.Component) -> str:
    """Return a tile's version line.

    Args:
        item: The component.

    Returns:
        "Installed <v> · Newest <v>", with "Not read" for a version that was
        not read; a missing component shows only its newest version, when
        one was read. A skill shows its link state.
    """
    if item.installed_kind == "skill":
        return item.link_state or NOT_READ
    newest = item.newest or NOT_READ
    if item.status == deps.MISSING:
        return "" if item.newest is None else f"Newest {newest}"
    return f"Installed {item.installed or NOT_READ} · Newest {newest}"


class DependencyTile(widgets.Card):
    """One component: label, status badge, versions, and command or note."""

    def __init__(self, item: deps.Component, context: AppContext) -> None:
        """Create the tile.

        Args:
            item: The component.
            context: The app's shared state and actions.
        """
        super().__init__()
        self.item = item
        self.context = context
        self.setAccessibleName(item.label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.label = QLabel(item.label)
        self.label.setFont(theme.font("label"))
        word, tone = BADGES.get(item.status, (item.status, "neutral"))
        self.badge = widgets.Badge(word, tone)
        head.addWidget(self.label, 1)
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)
        self.versions = label(versions_line(item), "secondary")
        layout.addWidget(self.versions)
        self.versions.setVisible(bool(self.versions.text()))
        self.error = label(item.error or "", "muted")
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.error)
        self.error.setVisible(bool(item.error) and item.error != item.link_state)
        self.resolved_path = label(item.resolved_path or "", "muted")
        self.resolved_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.resolved_path)
        self.resolved_path.setVisible(
            item.resolved_path is not None
            and item.command is not None
            and item.command_kind == "update"
        )
        self.command: widgets.CommandLine | None = None
        self.note: QLabel | None = None
        if item.command is not None:
            self.command = widgets.CommandLine(item.command)
            self.command.run_requested.connect(self._run)
            layout.addWidget(self.command)
        if item.note:
            self.note = label(item.note, "secondary")
            layout.addWidget(self.note)
        layout.addStretch(1)

    def _run(self, command: str) -> None:
        self.context.run_command(self, command)


class DependenciesScreen(QWidget):
    """The Dependencies screen: the installed and newest version of each component."""

    TITLE = "Dependencies"

    def __init__(self, context: AppContext) -> None:
        """Create the screen; it reads nothing until shown.

        Args:
            context: The app's shared state and actions.
        """
        super().__init__()
        self.context = context
        self.tiles: dict[str, DependencyTile] = {}
        self._checking = False
        self._dirty = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            SCREEN_MARGIN, SCREEN_MARGIN, SCREEN_MARGIN, SCREEN_MARGIN
        )
        layout.setSpacing(SCREEN_SPACING)

        self.spinner = widgets.Spinner(16)
        self.checking_label = label("Checking versions", "secondary", wrap=False)
        self.spinner.setAccessibleName(self.checking_label.text())
        self.spinner.hide()
        self.checking_label.hide()
        self.check_button = widgets.button("Check now", "secondary")
        self.check_button.setAccessibleName("Check now")
        self.check_button.clicked.connect(self.check_now)
        self.subtitle = screen_header(
            layout,
            self.TITLE,
            trailing=(self.spinner, self.checking_label, self.check_button),
        )

        self.alert_area = QVBoxLayout()
        self.alert_area.setContentsMargins(0, 0, 0, 0)
        self.alert: widgets.Alert | None = None
        layout.addLayout(self.alert_area)

        self.state_view = widgets.StateView()
        layout.addWidget(self.state_view, 1)
        self.scroll_area = scroll_area()
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.hide()
        layout.addWidget(self.scroll_area, 1)
        self.groups: list[tuple[str, QLabel]] = []

        context.deps_changed.connect(self._changed)
        context.deps_failed.connect(self._failed)
        theme.signals.changed.connect(self._draw_icons)
        self._draw_icons()

    def _draw_icons(self, *_args: object) -> None:
        widgets.set_icon(
            self.check_button, icons.icon("refresh-cw", theme.color("text-primary"), 16)
        )

    # Reading

    def shown(self) -> None:
        """Show the last report; read nothing new.

        Before any report exists the cached reads are shown, which runs no
        command and reads no network.
        """
        report = self.context.deps()
        if report is None:
            self._show_state()
            self.state_view.show_loading("Reading dependencies")
            self.context.refresh_deps(check_now=False)
            return
        if self._dirty:
            self.draw(report)

    def check_now(self) -> None:
        """Read installed versions, and newest versions when due."""
        if self.context.deps() is None:
            self._show_state()
            self.state_view.show_loading("Reading dependencies")
        self._set_checking(True)
        self.context.refresh_deps(check_now=True)

    def _set_checking(self, checking: bool) -> None:
        """Show the header spinner while checking and a report is shown.

        Without a report the state view's spinner shows the read instead.
        """
        self._checking = checking
        shown = checking and self.context.deps() is not None
        self.spinner.setVisible(shown)
        self.checking_label.setVisible(shown)

    def _read_ended(self) -> None:
        # A read that ended may be an earlier one; the check goes on while
        # the context still has a read in flight or waiting.
        self._set_checking(self._checking and self.context.deps_refreshing())

    def _changed(self, report: object) -> None:
        self._read_ended()
        self._clear_alert()
        if not isinstance(report, deps.DepsReport):
            return
        if self.isVisible():
            self.draw(report)
        else:
            self._dirty = True

    def _failed(self, text: str) -> None:
        self._read_ended()
        if self.context.deps() is None:
            self._show_state()
            self.subtitle.setText("")
            self.state_view.show_error(text, self.check_now)
            return
        self._clear_alert()
        self.alert = widgets.Alert(
            "danger", text, actions=[("Try again", self.check_now)]
        )
        self.alert_area.addWidget(self.alert)
        QWidget.setTabOrder(self.check_button, self.alert.buttons[0])

    def _clear_alert(self) -> None:
        discard(self.alert, self.alert_area)
        self.alert = None

    # Drawing

    def _show_state(self) -> None:
        """Hide the tiles and give the state view the content area."""
        self.scroll_area.hide()
        self.state_view.show()

    def draw(self, report: deps.DepsReport) -> None:
        """Draw a report: the subtitle and one tile per component.

        Args:
            report: The dependency report.
        """
        self._dirty = False
        self.subtitle.setText(subtitle(report))
        self.tiles = {}
        self.groups = []
        groups = ordered(report)
        if not groups:
            self._show_state()
            self.state_view.show_empty(
                "package", "No components", "Ember lists no components to check."
            )
            return
        self.state_view.clear()
        self.state_view.hide()
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(16)
        for key, title, items in groups:
            heading = QLabel(title)
            heading.setFont(theme.font("heading"))
            column.addWidget(heading)
            self.groups.append((key, heading))
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(16)
            for index in range(COLUMNS):
                grid.setColumnStretch(index, 1)
            for index, item in enumerate(items):
                tile = DependencyTile(item, self.context)
                self.tiles[item.id] = tile
                grid.addWidget(tile, index // COLUMNS, index % COLUMNS)
            column.addLayout(grid)
        column.addStretch(1)
        discard(self.scroll_area.takeWidget())
        self.scroll_area.setWidget(content)
        self.scroll_area.show()

    def order(self) -> list[tuple[str, list[str]]]:
        """Return the shown group keys with their component ids, in order."""
        return [
            (
                key,
                [
                    item_id
                    for item_id, tile in self.tiles.items()
                    if tile.item.group == key
                ],
            )
            for key, _heading in self.groups
        ]
