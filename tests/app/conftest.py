import atexit
import gc
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThreadPool
from PySide6.QtGui import QFont, QFontInfo, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from ember import paths
from ember.app import main, theme

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_fontconfig_dir = Path(tempfile.mkdtemp(prefix="ember-test-fonts-"))
atexit.register(shutil.rmtree, _fontconfig_dir, ignore_errors=True)
_fontconfig_file = _fontconfig_dir / "fonts.conf"
_fontconfig_file.write_text(
    f"""<?xml version="1.0"?>
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <dir>/usr/local/share/fonts</dir>
  <cachedir>{escape(str(_fontconfig_dir / "cache"))}</cachedir>
  <alias>
    <family>sans-serif</family>
    <prefer><family>DejaVu Sans</family></prefer>
  </alias>
  <!-- Qt's offscreen platform requests this spelling of sans-serif. -->
  <alias>
    <family>Sans Serif</family>
    <prefer><family>DejaVu Sans</family></prefer>
  </alias>
  <alias>
    <family>monospace</family>
    <prefer><family>DejaVu Sans Mono</family></prefer>
  </alias>
</fontconfig>
""",
    encoding="utf-8",
)
os.environ["FONTCONFIG_FILE"] = str(_fontconfig_file)


@pytest.fixture(scope="session", autouse=True)
def _require_test_fonts(qapp: QApplication) -> None:
    """Stop the session if Qt substitutes either pinned test font."""
    families = (
        QFontInfo(qapp.font()).family(),
        QFontInfo(theme.font("code")).family(),
    )
    if families != ("DejaVu Sans", "DejaVu Sans Mono"):
        pytest.exit(
            "App tests require DejaVu Sans and DejaVu Sans Mono; "
            "install fonts-dejavu-core. "
            f"Qt resolved {families!r}.",
            returncode=1,
        )


@pytest.fixture(autouse=True)
def _restore_appearance(qapp: QApplication) -> Iterator[None]:
    """Restore the shared application's appearance after each test.

    Reapplying a theme restores its mode, but also changes the application
    font, palette and style. Keep their original values as well as the sheet.
    """
    mode = theme.current_mode()
    font = QFont(qapp.font())
    palette = QPalette(qapp.palette())
    style = qapp.style().objectName()
    sheet = qapp.styleSheet()
    yield
    if theme.current_mode() != mode:
        theme.apply(qapp, mode)
    qapp.setStyleSheet(sheet)
    if qapp.style().objectName() != style:
        qapp.setStyle(style)
    qapp.setPalette(palette)
    qapp.setFont(font)


@pytest.fixture(autouse=True)
def _healthy_tmp_volume() -> Iterator[None]:
    """Supply stable capacity for background reads in app tests."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            paths, "run_tmp_volume", lambda: paths.RunTmpVolume(100_000, 1024**3, False)
        )
        yield


@pytest.fixture(autouse=True)
def _no_package_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the app from watching the shared environment's package folder.

    Other tests change that folder while they run; a test that needs the
    watch points `package_folder` at its own folder.
    """
    monkeypatch.setattr(main, "package_folder", lambda: None)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A private environment for one app test.

    XDG folders live under `tmp_path`; `PATH` holds a stub `bin/` folder
    plus `/usr/bin:/bin`.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(runtime),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }


@pytest.fixture(autouse=True)
def _collect_on_gui_thread() -> Iterator[None]:
    """Wait for pool tasks, deliver their events, then collect garbage.

    Qt objects left in reference cycles must be freed on the GUI thread, not
    by a garbage collection that happens to run on a pool thread.
    """
    yield
    QThreadPool.globalInstance().waitForDone(10_000)
    QCoreApplication.processEvents()
    gc.collect()


class _WindowShows(QObject):
    """Record every widget shown as a window of its own."""

    def __init__(self) -> None:
        super().__init__()
        self.shown: list[tuple[QWidget, str]] = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.Show
            and isinstance(watched, QWidget)
            and watched.isWindow()
        ):
            self.shown.append((watched, type(watched).__name__))
        return False


@pytest.fixture(autouse=True)
def _no_stray_windows() -> Iterator[None]:
    """Fail a test in which a widget is shown before it has a parent.

    Showing a parentless widget makes it a window, which takes activation
    from the app's window until the widget joins a layout; the window's
    shortcuts then stop working.
    """
    app = QApplication.instance() or QApplication([])
    spy = _WindowShows()
    app.installEventFilter(spy)
    yield
    app.removeEventFilter(spy)
    stray = []
    for widget, name in spy.shown:
        try:
            if not widget.isWindow():
                stray.append(name)
        except RuntimeError:
            continue
    assert not stray, f"shown as a window before getting a parent: {stray}"
