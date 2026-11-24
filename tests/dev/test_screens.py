"""Exercise the screen renderer without a display or the owner's app data."""

import gc
import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont, QFontInfo, QImage, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QScrollBar, QWidget
from pytestqt.qtbot import QtBot

from ember.app import theme
from ember.app.screens import KEYS

_spec = importlib.util.spec_from_file_location(
    "ember_dev_screen_renderer", Path(__file__).resolve().parents[2] / "dev/screens.py"
)
assert _spec is not None and _spec.loader is not None
screens = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screens)
screens.setup()


# The renderer builds a MainWindow with a real task pool.
pytestmark = pytest.mark.usefixtures("collect_on_gui_thread")


@pytest.fixture(autouse=True)
def restore_appearance(qapp: QApplication) -> Iterator[None]:
    """Restore the shared application's appearance and collect on its thread."""
    mode = theme.current_mode()
    font = QFont(qapp.font())
    palette = QPalette(qapp.palette())
    style = qapp.style().objectName()
    sheet = qapp.styleSheet()
    yield
    theme.apply(qapp, mode)
    qapp.setStyleSheet(sheet)
    if qapp.style().objectName() != style:
        qapp.setStyle(style)
    qapp.setPalette(palette)
    qapp.setFont(font)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def test_planted_overflow(qapp: QApplication, qtbot: QtBot) -> None:
    """Report a one-pixel overflow, including a clipped child, but not hidden ones."""
    assert QFontInfo(qapp.font()).family() == "DejaVu Sans"
    assert QFontInfo(theme.font("code")).family() == "DejaVu Sans Mono"
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.resize(200, 100)
    child = QLabel("Planted overflow", widget)
    child.setObjectName("planted")
    child.setGeometry(10, 10, 190, 20)
    widget.show()
    qapp.processEvents()
    assert screens.find_overflow(widget) == []
    child.resize(191, 20)
    assert screens.find_overflow(widget) == [
        "QLabel(planted): right edge exceeds viewport by 1 px"
    ]
    child.hide()
    assert screens.find_overflow(widget) == []


def test_scrollbars_and_viewport(qapp: QApplication, qtbot: QtBot) -> None:
    """Check the scroll viewport rather than the outer screen; ignore height."""
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.resize(300, 100)
    area = QScrollArea(widget)
    area.setGeometry(0, 0, 200, 100)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    content.setObjectName("scroll-content")
    content.resize(250, 500)
    area.setWidget(content)
    widget.show()
    qapp.processEvents()
    findings = screens.find_overflow(widget)
    assert any(
        "scroll-content" in finding and "right edge" in finding for finding in findings
    )
    assert not any("horizontal scroll bar" in finding for finding in findings)
    content.resize(area.viewport().width(), 500)
    qapp.processEvents()
    assert screens.find_overflow(widget) == []
    bar = QScrollBar(Qt.Orientation.Horizontal, widget)
    bar.setGeometry(210, 0, 80, 20)
    bar.setRange(0, 1)
    bar.show()
    assert screens.find_overflow(widget) == ["QScrollBar: horizontal scroll bar max=1"]
    bar.setMaximum(0)
    assert screens.find_overflow(widget) == []


def test_render_all_screens(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render the populated matrix and propagate a planted overflow to the report."""
    env = screens.environment(tmp_path / "fixture")
    out = tmp_path / "images"
    out.mkdir()
    (out / "old.png").write_bytes(b"old")
    find = screens.find_overflow

    def planted(widget: QWidget) -> list[str]:
        """Add one visible overflow to each rendered screen."""
        child = QLabel("Planted", widget)
        child.setObjectName("render-planted")
        child.setGeometry(widget.width(), 0, 2, 2)
        child.show()
        try:
            return find(widget)
        finally:
            child.hide()
            child.deleteLater()

    monkeypatch.setattr(screens, "find_overflow", planted)
    findings = screens.render(qapp, out, env)
    expected = {
        f"{key}-{width}x{height}-{mode}.png": (width, height)
        for key in KEYS
        for width, height in screens.SIZES
        for mode in theme.MODES
    }
    assert {path.name for path in out.iterdir()} == expected.keys()
    assert set(findings) == {
        f"{name}: QLabel(render-planted): right edge exceeds viewport by 2 px"
        for name in expected
    }
    for name, size in expected.items():
        image = QImage(str(out / name))
        assert not image.isNull()
        assert (image.width(), image.height()) == size
    assert not any(window.isVisible() for window in qapp.topLevelWidgets())


@pytest.mark.parametrize(
    "result, code", [([], 0), (["planted overflow"], 1), (None, 2)]
)
def test_exit_codes(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: list[str] | None,
    code: int,
) -> None:
    """Keep the command's clean, overflow and error statuses distinct."""

    def render(app: QApplication, out: Path, env: dict[str, str]) -> list[str]:
        """Return a report or simulate an output failure."""
        if result is None:
            raise OSError("cannot save PNG")
        return result

    monkeypatch.setattr(screens, "render", render)
    monkeypatch.setattr(screens, "QApplication", lambda _args: qapp)
    monkeypatch.setattr(
        screens.sys, "argv", ["screens.py", "--out", str(tmp_path / "out")]
    )
    assert screens.main() == code
    captured = capsys.readouterr()
    if code == 1:
        assert "planted overflow" in captured.out
    if code == 2:
        assert "cannot save PNG" in captured.err


def test_reject_repository_output(tmp_path: Path) -> None:
    """Reject repository paths before replacing any output files."""
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="outside the repository"):
        screens._prepare_output(repo / "screens")
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        screens._prepare_output(link)
    assert sentinel.read_text() == "keep"
