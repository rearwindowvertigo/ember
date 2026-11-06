"""Check the fonts Qt resolves for app tests."""

import pytest
from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from ember.app import theme


def test_application_font(qapp: QApplication) -> None:
    """Resolve the default application font to DejaVu Sans."""
    assert QFontInfo(qapp.font()).family() == "DejaVu Sans"


@pytest.mark.parametrize("mode", theme.MODES)
def test_widget_fonts(qapp: QApplication, qtbot: QtBot, mode: str) -> None:
    """Keep widget and type scale fonts pinned in both themes."""
    theme.apply(qapp, mode)
    label = QLabel("App test font")
    qtbot.addWidget(label)
    label.ensurePolished()
    assert QFontInfo(label.font()).family() == "DejaVu Sans"
    for step, style in theme.TYPE.items():
        expected = "DejaVu Sans Mono" if style.mono else "DejaVu Sans"
        assert QFontInfo(theme.font(step)).family() == expected, step


def test_code_font(qapp: QApplication) -> None:
    """Resolve the code and terminal font to DejaVu Sans Mono."""
    assert QFontInfo(theme.font("code")).family() == "DejaVu Sans Mono"
