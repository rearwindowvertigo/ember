"""The main window: the sidebar and the current screen (design J1, J2).

The full sidebar holds the Ember mark and name, one row per screen with its
icon and title, and the version at the bottom. Below a window width of 960 px,
it collapses to icon-only rows and hides the name and version line. Overview
and Dependencies show counts only above zero, as pills on the icons when
collapsed. A button hides the sidebar; a button before the current screen's
title shows it again. The hidden state is saved with the window size.

Screens are created the first time they are shown, once the first settings
snapshot is read; until then the content shows the read in progress, or its
error with Try again. The sidebar stays shown and its hide button disabled
until the first screen exists, when the saved hidden state takes effect.
"""

import dataclasses
import importlib.metadata
import os
from collections.abc import Callable
from typing import cast

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QProcessEnvironment,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QCloseEvent,
    QHideEvent,
    QKeySequence,
    QResizeEvent,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ember import __version__
from ember.app import icons, state, theme, tray, widgets
from ember.app.context import STALE_TEXT, STALE_TITLE, AppContext
from ember.app.screens import KEYS, SCREENS, Screen
from ember.app.screens.common import SCREEN_MARGIN

MINIMUM_WIDTH = 640
"""The narrowest the window can be."""

MINIMUM_HEIGHT = 480
"""The shortest the window can be."""

ICON_SIZE = 18
"""The size of a sidebar row's icon."""

BADGE_HEIGHT = 22
"""The height of a full sidebar row's count, in pixels."""

SIDEBAR_BREAKPOINT = 960
"""Window widths below this show the sidebar's icon-only rows."""

RESTART_TITLE = "Ember {version} is installed."
"""The restart alert's text, after an upgrade while Ember runs."""

_NAV_STYLE = """
QPushButton[role="nav"] {{
    text-align: left;
    border: 2px solid transparent;
    border-radius: {radius}px;
    background: transparent;
    min-height: {height}px;
    max-height: {height}px;
    padding: 0;
}}
QPushButton[role="nav"]:hover {{ background: {hover}; }}
QPushButton[role="nav"]:checked {{ background: {selected}; }}
QPushButton[role="nav"][keyfocus="true"]:focus {{ border: 2px solid {focus}; }}
QLabel[role="nav-count"] {{
    background: {subtle};
    color: {primary};
    border-radius: 8px;
    padding: 0 3px;
}}
QPushButton[role="restart-sidebar"] {{
    background: {accent_bg};
    border-radius: {round_radius}px;
}}
"""


def _rgba(token: str) -> str:
    color = theme.color(token)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


def app_version() -> str:
    """Return the installed Ember version."""
    try:
        return importlib.metadata.version("ember-app")
    except importlib.metadata.PackageNotFoundError:
        return __version__


class NavButton(QPushButton):
    """A screen's icon, title and count, or an icon and count pill when collapsed."""

    def __init__(self, key: str, title: str, icon_name: str) -> None:
        """Create the row.

        Args:
            key: The screen's key.
            title: The screen's title.
            icon_name: The Lucide icon's name.
        """
        super().__init__()
        self.key = key
        self.title = title
        self.icon_name = icon_name
        self.collapsed = False
        self.setProperty("show-tooltip", True)
        self.setProperty("role", "nav")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        layout = QHBoxLayout(self)
        self.row = layout
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(10)
        self.icon_label = QLabel()
        self.text_label = QLabel(title)
        self.text_label.setFont(theme.font("label"))
        self.count_label = QLabel()
        self.count_label.setProperty("role", "badge")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label.setFixedHeight(BADGE_HEIGHT)
        self.count_label.setFont(theme.font("caption"))
        self.count_label.hide()
        for part in (self.icon_label, self.text_label, self.count_label):
            part.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.text_label, 1)
        layout.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.icon_label.installEventFilter(self)
        self.draw_icon()

    def draw_icon(self) -> None:
        """Draw the icon in the current theme's colour."""
        self.icon_label.setPixmap(
            icons.pixmap(self.icon_name, theme.color("text-secondary"), ICON_SIZE)
        )

    def set_count(self, count: int, description: str) -> None:
        """Show a count above zero, or hide it.

        Args:
            count: The number to show.
            description: What the number counts, for assistive technology.
        """
        self.count_label.setText(str(count))
        self.count_label.setVisible(count > 0)
        self.setAccessibleDescription(description if count > 0 else "")
        self._count_tooltip()
        self._place_count()

    def set_collapsed(self, collapsed: bool) -> None:
        """Show only the icon and its count pill when collapsed."""
        self.collapsed = collapsed
        self.text_label.setVisible(not collapsed)
        self.row.removeWidget(self.count_label)
        margin = 0 if collapsed else 10
        self.row.setContentsMargins(margin, 0, margin, 0)
        self.row.setAlignment(self.icon_label, Qt.AlignmentFlag.AlignCenter)
        self.count_label.setProperty("role", "nav-count" if collapsed else "badge")
        self.count_label.setFixedHeight(16 if collapsed else BADGE_HEIGHT)
        widgets.repolish(self.count_label)
        if not collapsed:
            self.row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedSize(
            40 if collapsed else theme.SIDEBAR_WIDTH - 24, theme.CONTROL_MD
        )
        self._count_tooltip()
        self._place_count()

    def _count_tooltip(self) -> None:
        text = self.title
        if self.accessibleDescription():
            text += f" · {self.accessibleDescription()}"
        self.setToolTip(text if self.collapsed else "")

    def _place_count(self) -> None:
        if self.collapsed:
            self.count_label.adjustSize()
            icon = self.icon_label.geometry()
            self.count_label.move(
                max(
                    0,
                    min(
                        icon.right() + 1 - self.count_label.width() // 2,
                        self.width() - self.count_label.width(),
                    ),
                ),
                max(
                    0,
                    min(
                        icon.top() - self.count_label.height() // 2,
                        self.height() - self.count_label.height(),
                    ),
                ),
            )
            self.count_label.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Keep the pill on the icon when its layout moves or resizes it."""
        if watched is self.icon_label and event.type() in (
            QEvent.Type.Move,
            QEvent.Type.Resize,
        ):
            self._place_count()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: D102
        super().resizeEvent(event)
        self._place_count()

    def count(self) -> int | None:
        """Return the shown count, or `None` when it is hidden."""
        if self.count_label.isHidden():
            return None
        return int(self.count_label.text())

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(
            40 if self.collapsed else theme.SIDEBAR_WIDTH - 24, theme.CONTROL_MD
        )


@dataclasses.dataclass
class RestartOffer:
    """The installed version and the action shared by both restart controls."""

    version: str
    restart: Callable[[], None]


class UpdateBox(QFrame):
    """The full sidebar's offer to restart into a newer installed Ember."""

    def __init__(self, restart: Callable[[], None]) -> None:
        """Build the box with its Restart button.

        Args:
            restart: Quits this Ember and starts the installed one.
        """
        super().__init__()
        self.setProperty("role", "update")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self.label = QLabel()
        self.label.setFont(theme.font("caption"))
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.restart_button = widgets.button("Restart", "secondary", size="sm")
        self.restart_button.clicked.connect(restart)
        layout.addWidget(self.restart_button, 0, Qt.AlignmentFlag.AlignLeft)

    def set_text(self, text: str) -> None:
        """Show which version is installed."""
        self.label.setText(text)
        self.setAccessibleName(text)
        self.restart_button.setAccessibleDescription(text)


class MainWindow(QMainWindow):
    """The window: sidebar, notices and the current screen."""

    def __init__(
        self,
        context: AppContext,
        *,
        has_tray: bool,
        quit_app: Callable[[], None] | None = None,
    ) -> None:
        """Create the window at its saved size; it opens on the last screen.

        Args:
            context: The app's shared state and actions.
            has_tray: A tray icon is shown, so closing hides the window.
            quit_app: Quits Ember; `None` quits the application.
        """
        super().__init__()
        self.context = context
        self.has_tray = has_tray
        self._quit = QApplication.quit if quit_app is None else quit_app
        self._screens: dict[str, QWidget] = {}
        saved = state.load(context.env)
        self.sidebar_hidden = saved.sidebar_hidden
        self.sidebar_collapsed = self.width() < SIDEBAR_BREAKPOINT
        self.restart_offer: RestartOffer | None = None
        self.update_box: UpdateBox | None = None
        self.update_button: widgets.IconButton | None = None
        self._show_sidebar_buttons: list[widgets.IconButton] = []
        self._current = saved.screen if saved.screen in KEYS else KEYS[0]
        self.setWindowTitle("Ember")
        self.setWindowIcon(icons.ember_icon())
        self.setMinimumSize(MINIMUM_WIDTH, MINIMUM_HEIGHT)
        self.resize(max(saved.width, MINIMUM_WIDTH), max(saved.height, MINIMUM_HEIGHT))

        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        self.content = QWidget()
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.notice_column = QWidget()
        self.notice_column.setMaximumWidth(theme.CONTENT_MAX_WIDTH + 2 * SCREEN_MARGIN)
        self.notice_area = QVBoxLayout(self.notice_column)
        self.notice_area.setContentsMargins(SCREEN_MARGIN, 16, SCREEN_MARGIN, 0)
        self.notice: widgets.Alert | None = None
        self.notice_row = QHBoxLayout()
        self.notice_row.addWidget(self.notice_column, 1)
        content.addLayout(self.notice_row)
        self.stack = QStackedWidget()
        self.state_view = widgets.StateView()
        self.stack.addWidget(self.state_view)
        content.addWidget(self.stack, 1)
        row.addWidget(self.content, 1)
        self.setCentralWidget(central)
        self._fit_sidebar()

        for index, key in enumerate(KEYS, start=1):
            QShortcut(
                QKeySequence(f"Ctrl+{index}"),
                self,
                lambda key=key: self.show_screen(key),
            )
        QShortcut(QKeySequence("Ctrl+W"), self, self.close_to_tray)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.quit_app)

        context.settings_changed.connect(self._settings_read)
        context.settings_failed.connect(self._settings_failed)
        context.stale.connect(self._stale)
        context.error.connect(self._error)
        context.screen_requested.connect(self.show_screen)
        context.runs.changed.connect(self.update_counts)
        context.flows.changed.connect(self.update_counts)
        context.deps_changed.connect(self.update_counts)
        theme.signals.changed.connect(self._theme_changed)
        self._theme_changed()
        self.update_counts()
        if context.settings_ready():
            self.show_screen(self._current)
        else:
            self.state_view.show_loading("Reading settings")
            self._select(self._current)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setProperty("role", "sidebar")
        sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        self.sidebar = sidebar
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 20, 12, 16)
        layout.setSpacing(4)
        brand = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.brand = brand
        brand.setContentsMargins(10, 0, 10, 16)
        brand.setSpacing(10)
        mark = QLabel()
        self.mark = mark
        mark.setFixedSize(24, 24)
        mark.setProperty("show-tooltip", True)
        mark.setPixmap(icons.ember_icon().pixmap(24, 24))
        mark.setAccessibleName("Ember mark")
        name = QLabel("Ember")
        self.brand_name = name
        name.setFont(theme.font("heading"))
        brand.addWidget(mark)
        brand.addWidget(name, 1)
        self.hide_sidebar_button = widgets.IconButton("panel-left", "Hide sidebar")
        self.hide_sidebar_button.setEnabled(False)
        self.hide_sidebar_button.clicked.connect(self._toggle_sidebar)
        brand.addWidget(self.hide_sidebar_button)
        layout.addLayout(brand)
        self.nav: dict[str, NavButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, title, icon_name, _cls in SCREENS:
            item = NavButton(key, title, icon_name)
            item.clicked.connect(lambda _checked=False, key=key: self.show_screen(key))
            self._group.addButton(item)
            self.nav[key] = item
            layout.addWidget(item)
        layout.addStretch(1)
        self.update_area = QVBoxLayout()
        self.update_area.setContentsMargins(0, 0, 0, 8)
        layout.addLayout(self.update_area)
        self.version_label = QLabel(f"Ember {app_version()}")
        self.version_label.setProperty("role", "muted")
        self.version_label.setContentsMargins(10, 0, 10, 0)
        layout.addWidget(self.version_label)
        return sidebar

    def _fit_sidebar(self) -> None:
        collapsed = self.width() < SIDEBAR_BREAKPOINT
        self.sidebar_collapsed = collapsed
        self.sidebar.setFixedWidth(
            theme.SIDEBAR_COLLAPSED_WIDTH if collapsed else theme.SIDEBAR_WIDTH
        )
        self.brand.setDirection(
            QBoxLayout.Direction.TopToBottom
            if collapsed
            else QBoxLayout.Direction.LeftToRight
        )
        self.brand.setContentsMargins(0 if collapsed else 10, 0, 0, 16)
        self.brand.setAlignment(self.mark, Qt.AlignmentFlag.AlignCenter)
        self.brand.setAlignment(self.hide_sidebar_button, Qt.AlignmentFlag.AlignCenter)
        self.brand_name.setVisible(not collapsed)
        self.version_label.setVisible(not collapsed)
        self.mark.setToolTip(f"Ember {app_version()}" if collapsed else "")
        for item in self.nav.values():
            item.set_collapsed(collapsed)
        self._show_restart_control()

    def _sync_sidebar(self) -> None:
        self.hide_sidebar_button.setEnabled(bool(self._screens))
        hidden = self.sidebar_hidden and bool(self._screens)
        self.sidebar.setVisible(not hidden)
        for button in self._show_sidebar_buttons:
            button.setVisible(hidden)
        self._fit_notice()

    def _toggle_sidebar(self) -> None:
        if not self._screens:
            self._save_state()
            return
        self.sidebar_hidden = not self.sidebar_hidden
        self._sync_sidebar()
        self._save_state()
        if self.sidebar_hidden:
            screen = self.stack.currentWidget()
            for button in self._show_sidebar_buttons:
                if screen is not None and screen.isAncestorOf(button):
                    button.setFocus(Qt.FocusReason.OtherFocusReason)
                    break
        else:
            self.hide_sidebar_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _fit_notice(self) -> None:
        screen = self.stack.currentWidget()
        area = screen.findChild(QScrollArea) if screen is not None else None
        scrollbar = (
            area.verticalScrollBar().sizeHint().width()
            if area is not None
            else self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        )
        self.notice_row.setContentsMargins(0, 0, scrollbar, 0)

    # Screens

    def current_key(self) -> str:
        """Return the key of the current screen."""
        return self._current

    def screen_widget(self, key: str) -> QWidget | None:
        """Return a screen that was created, or `None`."""
        return self._screens.get(key)

    def _select(self, key: str) -> None:
        self._current = key
        self.nav[key].setChecked(True)
        if isinstance(QApplication.focusWidget(), NavButton):
            self.nav[key].setFocus(Qt.FocusReason.OtherFocusReason)

    def show_screen(self, key: str) -> None:
        """Make a screen current, creating it on first show.

        Before the first settings snapshot the choice is kept and shown once
        the settings are read.

        Args:
            key: A key of `SCREENS`.
        """
        if key not in KEYS:
            return
        self._select(key)
        if not self.context.settings_ready():
            return
        widget = self._screens.get(key)
        if widget is None:
            factory = next(cls for k, _t, _i, cls in SCREENS if k == key)
            widget = factory(self.context)
            self._screens[key] = widget
            self.stack.addWidget(widget)
            for button in widget.findChildren(widgets.IconButton):
                if button.property("role") == "show-sidebar":
                    button.clicked.connect(self._toggle_sidebar)
                    self._show_sidebar_buttons.append(button)
                    first = widget.nextInFocusChain()
                    while first is not None and first is not widget:
                        if (
                            widget.isAncestorOf(first)
                            and first.focusPolicy() & Qt.FocusPolicy.TabFocus
                        ):
                            if first is not button:
                                QWidget.setTabOrder(button, first)
                            break
                        first = first.nextInFocusChain()
        self.stack.setCurrentWidget(widget)
        self._sync_sidebar()
        self._save_state()
        if self.isVisible():
            cast(Screen, widget).shown()

    def _settings_read(self, _snapshot: dict) -> None:
        if self.stack.currentWidget() is self.state_view:
            self.state_view.clear()
            self.show_screen(self._current)

    def _settings_failed(self, text: str) -> None:
        if self.context.settings_ready():
            self.show_notice("danger", text)
        else:
            self.state_view.show_error(text, self._retry_settings)

    def _retry_settings(self) -> None:
        self.state_view.show_loading("Reading settings")
        self.context.reload_settings()

    # Counts

    def update_counts(self, *_args: object) -> None:
        """Show top-level live items and dependencies that need the owner."""
        items, _children = tray.live_items(
            self.context.runs.live(),
            [*self.context.flows.flows(), *self.context.flows.panels()],
        )
        live = len(items)
        self.nav["overview"].set_count(live, f"{live} live")
        report = self.context.deps()
        needs = 0 if report is None else report.counts()
        self.nav["dependencies"].set_count(needs, f"{needs} to update or install")

    # Notices

    def show_notice(self, tone: str, title: str, text: str = "") -> None:
        """Show an alert above the current screen, replacing the last one.

        Args:
            tone: One of `widgets.TONES`.
            title: The first line.
            text: The lines under it.
        """
        self.clear_notice()
        self.notice = widgets.Alert(
            tone, title, text, actions=[("Dismiss", self.clear_notice)]
        )
        self.notice_area.addWidget(self.notice)

    def clear_notice(self) -> None:
        """Remove the alert, if any."""
        if self.notice is not None:
            self.notice_area.removeWidget(self.notice)
            self.notice.deleteLater()
            self.notice = None

    def show_restart(self, version: str, restart: Callable[[], None]) -> None:
        """Offer a restart into the installed version until Ember restarts.

        The full sidebar shows a small box above the version line. The
        collapsed sidebar shows a round refresh-cw button whose tooltip
        names the installed version and offers Restart. Both controls use
        one stored offer. There is no Dismiss button; each call updates the
        version, the restart action and both controls' text.

        Args:
            version: The installed version.
            restart: Quits this Ember and starts the installed one.
        """
        previous = self.restart_offer
        self.restart_offer = RestartOffer(version, restart)
        text = RESTART_TITLE.format(version=self.restart_offer.version)
        if self.update_box is None:
            self.update_box = UpdateBox(self._restart)
            self.update_area.addWidget(self.update_box)
            self.update_button = widgets.IconButton(
                "refresh-cw", "", color="text-accent"
            )
            self.update_button.setProperty("role", "restart-sidebar")
            self.update_button.clicked.connect(self._restart)
            self.update_area.addWidget(
                self.update_button, 0, Qt.AlignmentFlag.AlignHCenter
            )
        self.update_box.set_text(text)
        assert self.update_button is not None
        self.update_button.setToolTip(f"{text} Restart")
        self.update_button.setAccessibleName(f"{text} Restart")
        self._show_restart_control()
        if previous is None or previous.version != version:
            self.bring_forward()

    def _restart(self) -> None:
        if self.restart_offer is not None:
            self.restart_offer.restart()

    def _show_restart_control(self) -> None:
        if self.update_box is not None and self.update_button is not None:
            self.update_box.setVisible(not self.sidebar_collapsed)
            self.update_button.setVisible(self.sidebar_collapsed)

    def _stale(self) -> None:
        self.show_notice("info", STALE_TITLE, STALE_TEXT)

    def _error(self, text: str) -> None:
        self.show_notice("danger", text)

    # Theme

    def _theme_changed(self, *_args: object) -> None:
        self.sidebar.setStyleSheet(
            _NAV_STYLE.format(
                radius=theme.RADIUS["sm"],
                height=theme.CONTROL_MD - 4,
                hover=_rgba("bg-hover"),
                selected=_rgba("bg-selected"),
                focus=_rgba("accent"),
                subtle=_rgba("bg-subtle"),
                primary=_rgba("text-primary"),
                accent_bg=_rgba("bg-accent"),
                round_radius=theme.CONTROL_SM // 2,
            )
        )
        for item in self.nav.values():
            item.draw_icon()

    # Showing, hiding and quitting

    def bring_forward(self, token: str = "") -> None:
        """Show the window, raise it and give it focus.

        Args:
            token: An activation token from another launch of Ember, or "".
                Qt's Wayland activation reads it from `XDG_ACTIVATION_TOKEN`
                and unsets it once used, which ends the desktop's busy cursor.
        """
        if token:
            os.environ["XDG_ACTIVATION_TOKEN"] = token
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def close_to_tray(self) -> None:
        """Hide the window to the tray, or quit when there is no tray."""
        if self.has_tray:
            self._save_state()
            self.hide()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        """Keep the window's state and quit Ember.

        A tray menu click leaves an activation token in the environment, and
        the desktop shows a busy cursor until a window uses it. A shown
        window uses it before Ember quits; when hidden, a separate invisible
        window uses it instead.
        """
        self._save_state()
        token = QProcessEnvironment.systemEnvironment().value("XDG_ACTIVATION_TOKEN")
        if token:
            if self.isVisible():
                self.activateWindow()
                QCoreApplication.processEvents()
            else:
                helper = QWidget(
                    None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                )
                helper.setFixedSize(1, 1)
                helper.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                helper.setWindowOpacity(0)
                helper.show()
                helper.activateWindow()
                QCoreApplication.processEvents()
                helper.hide()
        self._quit()

    def _save_state(self) -> None:
        size = self.normalGeometry().size() if self.isMaximized() else self.size()
        saved = state.load(self.context.env)
        state.save(
            dataclasses.replace(
                saved,
                width=size.width(),
                height=size.height(),
                screen=self._current,
                sidebar_hidden=self.sidebar_hidden,
            ),
            self.context.env,
        )

    def showEvent(self, event: QShowEvent) -> None:  # noqa: D102
        super().showEvent(event)
        self.context.set_visible(True)
        widget = self._screens.get(self._current)
        if widget is not None and self.stack.currentWidget() is widget:
            cast(Screen, widget).shown()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: D102
        super().resizeEvent(event)
        if (self.width() < SIDEBAR_BREAKPOINT) != self.sidebar_collapsed:
            self._fit_sidebar()
        self._fit_notice()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: D102
        super().hideEvent(event)
        self.context.set_visible(False)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: D102
        self._save_state()
        if self.has_tray:
            event.ignore()
            self.hide()
        else:
            event.accept()
            self._quit()
