"""The main window: the sidebar and the current screen (design J1, J2).

The sidebar holds the Ember mark and name, one row per screen with its icon
and, for Overview and Dependencies, a count shown only above zero, and the
version at the bottom. Screens are created the first time they are shown,
once the first settings snapshot is read; until then the content shows the
read in progress, or its error with Try again.
"""

import importlib.metadata
from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QHideEvent, QKeySequence, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ember import __version__
from ember.app import icons, state, theme, tray, widgets
from ember.app.context import STALE_TEXT, STALE_TITLE, AppContext
from ember.app.screens import KEYS, SCREENS, Screen

MINIMUM_WIDTH = 900
"""The narrowest the window can be."""

MINIMUM_HEIGHT = 600
"""The shortest the window can be."""

ICON_SIZE = 18
"""The size of a sidebar row's icon."""

BADGE_HEIGHT = 22
"""The height of a sidebar row's count, in pixels."""

_NAV_STYLE = """
QPushButton[role="nav"] {{
    text-align: left;
    border: 2px solid transparent;
    border-radius: {radius}px;
    background: transparent;
    min-height: {height}px;
    padding: 0;
}}
QPushButton[role="nav"]:hover {{ background: {hover}; }}
QPushButton[role="nav"]:checked {{ background: {selected}; }}
QPushButton[role="nav"][keyfocus="true"]:focus {{ border: 2px solid {focus}; }}
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
    """A sidebar row: the screen's icon, its title and an optional count."""

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
        self.setProperty("role", "nav")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(10)
        self.icon_label = QLabel()
        self.text_label = QLabel(title)
        self.text_label.setFont(theme.font("label"))
        self.count_label = QLabel()
        self.count_label.setProperty("role", "badge")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label.setFixedHeight(BADGE_HEIGHT)
        self.count_label.hide()
        for part in (self.icon_label, self.text_label, self.count_label):
            part.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.text_label, 1)
        layout.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)
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

    def count(self) -> int | None:
        """Return the shown count, or `None` when it is hidden."""
        if self.count_label.isHidden():
            return None
        return int(self.count_label.text())

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(theme.SIDEBAR_WIDTH - 24, theme.CONTROL_MD)


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
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.notice_area = QVBoxLayout()
        self.notice_area.setContentsMargins(24, 16, 24, 0)
        self.notice: widgets.Alert | None = None
        content.addLayout(self.notice_area)
        self.stack = QStackedWidget()
        self.state_view = widgets.StateView()
        self.stack.addWidget(self.state_view)
        content.addWidget(self.stack, 1)
        row.addLayout(content, 1)
        self.setCentralWidget(central)

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
        brand = QHBoxLayout()
        brand.setContentsMargins(10, 0, 10, 16)
        brand.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(icons.ember_icon().pixmap(24, 24))
        mark.setAccessibleName("Ember mark")
        name = QLabel("Ember")
        name.setFont(theme.font("heading"))
        brand.addWidget(mark)
        brand.addWidget(name, 1)
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
        self.version_label = QLabel(f"Ember {app_version()}")
        self.version_label.setProperty("role", "muted")
        self.version_label.setContentsMargins(10, 0, 10, 0)
        layout.addWidget(self.version_label)
        return sidebar

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
        self.stack.setCurrentWidget(widget)
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

    def _stale(self) -> None:
        self.show_notice("info", STALE_TITLE, STALE_TEXT)

    def _error(self, text: str) -> None:
        self.show_notice("danger", text)

    # Theme

    def _theme_changed(self, *_args: object) -> None:
        self.sidebar.setStyleSheet(
            _NAV_STYLE.format(
                radius=theme.RADIUS["sm"],
                height=theme.CONTROL_MD,
                hover=_rgba("bg-hover"),
                selected=_rgba("bg-selected"),
                focus=_rgba("accent"),
            )
        )
        for item in self.nav.values():
            item.draw_icon()

    # Showing, hiding and quitting

    def bring_forward(self) -> None:
        """Show the window, raise it and give it focus."""
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
        """Keep the window's state and quit Ember."""
        self._save_state()
        self._quit()

    def _save_state(self) -> None:
        size = self.normalGeometry().size() if self.isMaximized() else self.size()
        state.save(
            state.AppState(size.width(), size.height(), self._current),
            self.context.env,
        )

    def showEvent(self, event: QShowEvent) -> None:  # noqa: D102
        super().showEvent(event)
        self.context.set_visible(True)
        widget = self._screens.get(self._current)
        if widget is not None and self.stack.currentWidget() is widget:
            cast(Screen, widget).shown()

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
