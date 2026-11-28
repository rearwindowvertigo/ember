"""The app's screens, in sidebar order.

Each screen module defines one `QWidget` subclass, constructed with the
`AppContext`, with a method `shown()` the window calls each time the screen
becomes current.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from PySide6.QtWidgets import QWidget

from ember.app.screens.benchmarks import BenchmarksScreen
from ember.app.screens.dependencies import DependenciesScreen
from ember.app.screens.engines import EnginesScreen
from ember.app.screens.menu import MenuScreen
from ember.app.screens.overview import OverviewScreen
from ember.app.screens.settings import SettingsScreen

if TYPE_CHECKING:
    from ember.app.context import AppContext


class Screen(Protocol):
    """What the window needs of a screen besides being a `QWidget`."""

    def shown(self) -> None:
        """Called by the window each time the screen becomes current."""


ScreenClass = Callable[["AppContext"], QWidget]
"""A screen class: called with the context, it returns the screen widget."""

SCREENS: tuple[tuple[str, str, str, ScreenClass], ...] = (
    ("overview", "Overview", "layout-dashboard", OverviewScreen),
    ("menu", "Team", "users", MenuScreen),
    ("engines", "Engines", "cpu", EnginesScreen),
    ("dependencies", "Dependencies", "package", DependenciesScreen),
    ("settings", "Settings", "settings", SettingsScreen),
    ("benchmarks", "Benchmarks", "chart-no-axes-combined", BenchmarksScreen),
)
"""`(key, title, icon name, class)` for each screen, in sidebar order."""

KEYS = tuple(key for key, _title, _icon, _cls in SCREENS)
"""Every screen's key, in sidebar order."""
