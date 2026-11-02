"""The Engines screen."""

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ember.app import theme

if TYPE_CHECKING:
    from ember.app.context import AppContext


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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)
        title = QLabel(self.TITLE)
        title.setFont(theme.font("display"))
        layout.addWidget(title)
        layout.addStretch(1)

    def shown(self) -> None:
        """Called by the window each time the screen becomes current."""
