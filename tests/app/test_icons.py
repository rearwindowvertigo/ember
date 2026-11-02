import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from ember.app import icons, theme


def _painted(image: QImage) -> bool:
    return any(
        image.pixelColor(x, y).alpha() > 0
        for x in range(image.width())
        for y in range(image.height())
    )


def test_asset_files_exist() -> None:
    root = icons.assets()
    for name in icons.ICON_NAMES:
        assert (root / "icons" / f"{name}.svg").is_file(), name
    assert (root / "icons" / "LICENSE").is_file()
    for engine in icons.LOGOS:
        assert (root / "logos" / f"{engine}.svg").is_file(), engine
    assert (root / "logos" / "NOTICE").is_file()
    for file in (icons.APP_ICON, icons.APP_ICON_SMALL, *icons.TRAY_ICONS.values()):
        assert (root / "ember" / file).is_file(), file


@pytest.mark.parametrize("name", icons.ICON_NAMES)
def test_icon_renders(qapp: QApplication, name: str) -> None:  # noqa: ARG001
    result = icons.pixmap(name, QColor("#ff0000"), 18, ratio=1.0)
    assert result.width() == 18
    image = result.toImage()
    assert _painted(image)
    colours = {
        image.pixelColor(x, y).name()
        for x in range(18)
        for y in range(18)
        if image.pixelColor(x, y).alpha() == 255
    }
    assert colours <= {"#ff0000"}


def test_icon_cache_and_ratio(qapp: QApplication) -> None:  # noqa: ARG001
    first = icons.pixmap("x", QColor("#000000"), 16, ratio=2.0)
    assert first.width() == 32
    assert first.devicePixelRatio() == 2.0
    assert icons.pixmap("x", QColor("#000000"), 16, ratio=2.0).cacheKey() == (
        first.cacheKey()
    )
    assert icons.pixmap("x", QColor("#ffffff"), 16, ratio=2.0).cacheKey() != (
        first.cacheKey()
    )
    assert not icons.icon("x", QColor("#000000"), 16).isNull()


@pytest.mark.parametrize("engine", icons.LOGOS)
def test_logo_renders(qapp: QApplication, engine: str) -> None:  # noqa: ARG001
    result = icons.logo(engine, 32)
    assert not result.isNull()
    assert _painted(result.toImage())


def test_logo_colours(qapp: QApplication) -> None:
    try:
        theme.apply(qapp, "dark")
        codex = icons.logo("codex", 32).toImage()
        claude = icons.logo("claude", 32).toImage()
    finally:
        theme.apply(qapp, "light")
        qapp.setStyleSheet("")
    opaque = {
        codex.pixelColor(x, y).name()
        for x in range(codex.width())
        for y in range(codex.height())
        if codex.pixelColor(x, y).alpha() == 255
    }
    assert opaque <= {"#ededed"}
    orange = {
        claude.pixelColor(x, y).name()
        for x in range(claude.width())
        for y in range(claude.height())
        if claude.pixelColor(x, y).alpha() == 255
    }
    assert "#d97757" in orange


def test_ember_and_tray_icons(qapp: QApplication) -> None:  # noqa: ARG001
    app_icon = icons.ember_icon()
    assert not app_icon.isNull()
    assert {s.width() for s in app_icon.availableSizes()} >= {16, 24, 32, 256}
    for live in (False, True):
        tray = icons.tray_icon(live)
        assert not tray.isNull()
        assert _painted(tray.pixmap(22).toImage())
    idle = icons.tray_icon(False).pixmap(16).toImage()
    live_image = icons.tray_icon(True).pixmap(16).toImage()
    assert idle != live_image
