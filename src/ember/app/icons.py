"""Icons, engine logos and the Ember drawings, rendered from SVG files.

Interface icons use the Lucide style in `assets/icons/`; each is drawn in a
colour by replacing `currentColor`. Engine logos are in `assets/logos/`. The
Ember app and tray drawings are in `assets/ember/`. Rendered pixmaps are
cached per name, colour, size and device pixel ratio.
"""

from importlib.resources import files
from importlib.resources.abc import Traversable

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ember.app import theme

ICON_NAMES = (
    "layout-dashboard",
    "user",
    "users",
    "users-three",
    "cpu",
    "package",
    "settings",
    "chart-no-axes-combined",
    "terminal",
    "copy",
    "refresh-cw",
    "rotate-ccw",
    "chevron-down",
    "chevron-right",
    "triangle-alert",
    "circle-alert",
    "circle-check",
    "info",
    "inbox",
    "search",
    "folder-plus",
    "x",
    "check",
    "trash-2",
    "panel-left",
)
"""The interface icons shipped in `assets/icons/`."""

LOGOS = ("codex", "opencode", "claude")
"""The engines whose logos are shipped in `assets/logos/`."""

APP_ICON = "io.github.rearwindowvertigo.Ember.svg"
"""The scalable app icon in `assets/ember/`."""

APP_ICON_PLAIN = "io.github.rearwindowvertigo.Ember-plain.svg"
"""The app icon drawn as filled outlines, without SVG masks or clip paths.

The `.deb` installs it as the desktop's scalable icon.
"""

APP_ICON_SMALL = "ember-app-small.svg"
"""The bolder app drawing for 32 pixels and below, in `assets/ember/`."""

MARKS = {"dark": "ember-mark-on-dark.svg", "light": "ember-mark-on-light.svg"}
"""The mark without its tile for each theme mode, drawn for 24 pixels."""

MARK_SIZE = 24
"""The size the mark without its tile is drawn at, in pixels."""

TRAY_ICONS = {False: "ember-tray-idle-on-dark.svg", True: "ember-tray-live-on-dark.svg"}
"""The tray drawings for idle and a run live, both for a dark top bar."""

_SMALL_SIZES = (16, 24, 32)
_LARGE_SIZES = (48, 64, 128, 256)
_TRAY_SIZES = (16, 22, 24, 32, 48)

_cache: dict[tuple[str, str, int, float], QPixmap] = {}


def assets() -> Traversable:
    """Return the folder holding the app's assets."""
    return files("ember.app") / "assets"


def _ratio() -> float:
    app = QGuiApplication.instance()
    if isinstance(app, QGuiApplication):
        return app.devicePixelRatio()
    return 1.0


def _render(data: bytes, size: int, ratio: float) -> QPixmap:
    side = max(1, round(size * ratio))
    result = QPixmap(side, side)
    result.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(data))
    painter = QPainter(result)
    try:
        renderer.render(painter, QRectF(0, 0, side, side))
    finally:
        painter.end()
    result.setDevicePixelRatio(ratio)
    return result


def _cached(
    key: tuple[str, str, int, float], path: Traversable, fill: QColor | None
) -> QPixmap:
    found = _cache.get(key)
    if found is not None:
        return found
    data = path.read_bytes()
    if fill is not None:
        data = data.replace(b"currentColor", fill.name().encode())
    result = _render(data, key[2], key[3])
    _cache[key] = result
    return result


def pixmap(name: str, color: QColor, size: int, ratio: float | None = None) -> QPixmap:
    """Return an interface icon drawn in a colour.

    Args:
        name: One of `ICON_NAMES`.
        color: The stroke colour.
        size: The size in device-independent pixels.
        ratio: The device pixel ratio; the application's by default.

    Returns:
        A square pixmap carrying the device pixel ratio.
    """
    scale = _ratio() if ratio is None else ratio
    key = (f"icon:{name}", color.name(QColor.NameFormat.HexArgb), size, scale)
    return _cached(key, assets() / "icons" / f"{name}.svg", color)


def icon(name: str, color: QColor, size: int) -> QIcon:
    """Return an interface icon drawn in a colour, as a `QIcon`."""
    return QIcon(pixmap(name, color, size))


def logo(engine: str, size: int) -> QPixmap:
    """Return an engine maker's logo.

    The Codex and OpenCode logos are drawn in `text-primary` of the current
    theme mode; the Claude logo keeps its own colour.

    Args:
        engine: One of `LOGOS`.
        size: The size in device-independent pixels.

    Returns:
        A square pixmap carrying the application's device pixel ratio.
    """
    scale = _ratio()
    fill = None if engine == "claude" else theme.color("text-primary")
    shade = "" if fill is None else fill.name()
    key = (f"logo:{engine}", shade, size, scale)
    return _cached(key, assets() / "logos" / f"{engine}.svg", fill)


def _drawing(file: str, sizes: tuple[int, ...], into: QIcon) -> None:
    path = assets() / "ember" / file
    for size in sizes:
        into.addPixmap(_cached((f"ember:{file}", "", size, 1.0), path, None))


def ember_icon() -> QIcon:
    """Return the app icon: the small drawing at 16, 24 and 32 pixels."""
    result = QIcon()
    _drawing(APP_ICON_SMALL, _SMALL_SIZES, result)
    _drawing(APP_ICON, _LARGE_SIZES, result)
    return result


def mark() -> QPixmap:
    """Return the mark without its tile, for the current theme mode."""
    file = MARKS[theme.current_mode()]
    path = assets() / "ember" / file
    return _cached((f"ember:{file}", "", MARK_SIZE, _ratio()), path, None)


def tray_icon(live: bool) -> QIcon:
    """Return the tray icon for idle or a run live, for a dark top bar."""
    result = QIcon()
    _drawing(TRAY_ICONS[live], _TRAY_SIZES, result)
    return result
