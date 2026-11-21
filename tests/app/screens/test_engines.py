import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFontMetricsF, QImage
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QWidget
from pytestqt.qtbot import QtBot

from ember import choices, holds, sandbox
from ember.app import theme
from ember.app.context import AppContext
from ember.app.screens.common import inline, when
from ember.app.screens.engines import (
    NO_EFFORT_NOTE,
    PRICE_CHOICES,
    EnginesScreen,
    badge_for,
    cause_advice,
    context_text,
    initial_effort,
    per_token,
    price_text,
    shortlist_with,
)
from ember.app.widgets import HINT_ROLE, CommandLine, InfoTip
from ember.engines import models, status

NOW = time.time()


@pytest.fixture(params=theme.MODES)
def engine_theme(qapp: QApplication, request: pytest.FixtureRequest) -> Iterator[None]:
    previous = theme.current_mode()
    sheet = qapp.styleSheet()
    theme.apply(qapp, request.param)
    try:
        yield
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet(sheet)


def _status(engine: str, state: str, reason: str | None = None) -> status.EngineStatus:
    installed = state != status.NOT_INSTALLED
    if state == status.NOT_INSTALLED:
        requirements = (
            status.Requirement(f"the {engine} CLI", False, status.INSTALL[engine]),
            status.Requirement(status.SIGN_IN_LABEL[engine], False, None),
        )
        reason = reason or f"the {engine} CLI is not installed"
    elif state == status.NOT_SIGNED_IN:
        fix = " ".join([engine, *status.SIGN_IN_ARGS[engine]])
        requirements = (
            status.Requirement(f"the {engine} CLI", True, None),
            status.Requirement(status.SIGN_IN_LABEL[engine], False, fix),
        )
    elif state == status.CHECK_FAILED:
        requirements = (
            status.Requirement(f"the {engine} CLI", True, None),
            status.Requirement(status.SIGN_IN_LABEL[engine], False, None),
        )
    else:
        requirements = (
            status.Requirement(f"the {engine} CLI", True, None),
            status.Requirement(status.SIGN_IN_LABEL[engine], True, None),
        )
    return status.EngineStatus(
        engine=engine,
        state=state,
        reason=reason,
        installed=installed,
        binary=f"/usr/bin/{engine}" if installed else None,
        version=f"{engine} 1.0.0" if installed else None,
        signed_in=state == status.CONNECTED if installed else None,
        connected=state == status.CONNECTED,
        requirements=requirements,
        checked_at=NOW,
    )


def _all(**states: str) -> dict[str, status.EngineStatus]:
    return {
        engine: _status(engine, states.get(engine, status.CONNECTED))
        for engine in status.ENGINES
    }


def _check(
    engine: str, ok: bool = True, detail: str = "", causes: tuple[str, ...] = ()
) -> sandbox.SandboxCheck:
    return sandbox.SandboxCheck(
        engine=engine,
        provider=sandbox.BUBBLEWRAP,
        bwrap_path="/usr/bin/bwrap",
        bwrap_version="0.11.1",
        ok=ok,
        detail=detail or "/bin/true ran under bubblewrap",
        causes=causes,
        checked_at=NOW,
    )


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub engine status, sandbox checks and model lists."""
    held: dict[str, Any] = {
        "engines": _all(),
        "checks": [],
        "lists": {},
        "list_calls": [],
    }

    def read(_env: object, *, refresh: bool = False) -> dict[str, status.EngineStatus]:
        return held["engines"]

    def check(engine: str, _env: object = None) -> sandbox.SandboxCheck:
        held["checks"].append(engine)
        return held.get("check_result", {}).get(engine) or _check(engine)

    def list_models(
        engine: str, _env: object = None, *, refresh: bool = False
    ) -> models.ModelList:
        held["list_calls"].append((engine, refresh))
        return held["lists"][engine]

    monkeypatch.setattr(status, "read", read)
    monkeypatch.setattr(sandbox, "check", check)
    monkeypatch.setattr(models, "list_models", list_models)
    return held


def _screen(env: dict[str, str], qtbot: QtBot) -> tuple[EnginesScreen, AppContext]:
    context = AppContext(env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    screen = EnginesScreen(context)
    qtbot.addWidget(screen)
    return screen, context


def _shown(screen: EnginesScreen, context: AppContext, qtbot: QtBot) -> None:
    with qtbot.waitSignal(context.engines_changed, timeout=5000):
        screen.shown()
    context.tasks.wait(5000)
    QApplication.processEvents()


def _save(context: AppContext, qtbot: QtBot, action: Callable[[], None]) -> None:
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        action()


def _physical_rect(rect: QRect, ratio: float) -> QRect:
    """Enclose a logical rectangle in device pixels."""
    left = math.floor(rect.x() * ratio)
    top = math.floor(rect.y() * ratio)
    right = math.ceil((rect.x() + rect.width()) * ratio)
    bottom = math.ceil((rect.y() + rect.height()) * ratio)
    return QRect(left, top, right - left, bottom - top)


_ANTIALIAS_TOLERANCE = 8


def _assert_label_background(image: QImage, screen: QWidget, name: QLabel) -> None:
    """Assert the card background outside a label's tight text bounds."""
    origin = name.mapTo(screen, QPoint())
    metrics = name.fontMetrics()
    line = metrics.boundingRect(name.contentsRect(), int(name.alignment()), name.text())
    text = metrics.tightBoundingRect(name.text()).translated(
        origin.x() + line.x(), origin.y() + line.y() + metrics.ascent()
    )
    ratio = image.devicePixelRatio()
    text_pixels = _physical_rect(text, ratio)
    region = _physical_rect(QRect(origin, name.size()).adjusted(-3, -3, 3, 3), ratio)
    background = theme.color("bg-card")
    checked = 0
    for y in range(region.top(), region.bottom() + 1):
        for x in range(region.left(), region.right() + 1):
            if text_pixels.contains(x, y):
                continue
            checked += 1
            pixel = image.pixelColor(x, y)
            # Font antialiasing may tint a pixel next to the glyphs by a step
            # or two; a border line differs by about 25 on every channel.
            difference = max(
                abs(pixel.red() - background.red()),
                abs(pixel.green() - background.green()),
                abs(pixel.blue() - background.blue()),
            )
            assert difference <= _ANTIALIAS_TOLERANCE, (
                name.text(),
                x,
                y,
                image.pixelColor(x, y).name(),
                background.name(),
            )
    assert checked > 0


def _capture_label_backgrounds(
    env: dict[str, str], mode: str, ratio: float, captures: Path
) -> None:
    """Show Engines and check captures with mouse selection on, off and on."""
    app = QApplication([])
    theme.apply(app, mode)
    captures.mkdir()
    choices.set_settings(
        {f"sandbox.{engine}": "on" for engine in status.ENGINES}, env=env
    )
    context = AppContext(env)
    context.reload_settings()
    assert context.tasks.wait(5000)
    app.processEvents()
    with (
        patch.object(status, "read", return_value=_all()),
        patch.object(models, "list_models", return_value=_router_list()),
    ):
        screen = EnginesScreen(context)
        try:
            screen.resize(1100, 900)
            screen.show()
            screen.activateWindow()
            app.processEvents()
            handle = screen.windowHandle()
            assert handle is not None and handle.isExposed()
            screen.shown()
            assert context.tasks.wait(5000)
            app.processEvents()
            screen.toggle_models("opencode")
            assert context.tasks.wait(5000)
            app.processEvents()
            panel = screen.panel
            assert panel is not None
            assert panel.tools_switch is not None
            panel.tools_switch.setChecked(False)
            panel.apply_filters()
            app.processEvents()
            labels = [row.name for row in panel.rows]
            labels += [card.check_label for card in screen.cards.values()]
            assert len(labels) == 6
            for step, selectable in enumerate((True, False, True)):
                for name in labels:
                    name.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextSelectableByMouse
                        if selectable
                        else Qt.TextInteractionFlag.NoTextInteraction
                    )
                screen.check_button.setFocus()
                app.processEvents()
                image = screen.grab().toImage()
                assert image.devicePixelRatio() == ratio
                assert image.save(str(captures / f"{step}-screen.png"))
                for index, name in enumerate(labels):
                    assert name.isVisible()
                    assert not name.hasFocus()
                    area = QRect(name.mapTo(screen, QPoint()), name.size())
                    pixels = _physical_rect(area.adjusted(-3, -3, 3, 3), ratio)
                    assert image.copy(pixels).save(
                        str(captures / f"{step}-label-{index}.png")
                    )
                    _assert_label_background(image, screen, name)
        finally:
            screen.close()
            assert context.tasks.wait(5000)
            app.processEvents()


@pytest.mark.parametrize("mode", theme.MODES)
@pytest.mark.parametrize("ratio", (1, 1.25))
def test_shown_selectable_labels_have_only_text_pixels(
    env: dict[str, str], tmp_path: Path, mode: str, ratio: float
) -> None:
    test_file = Path(__file__).resolve()
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, runpy, sys; from pathlib import Path; "
            "runpy.run_path(sys.argv[1])['_capture_label_backgrounds']("
            "json.loads(sys.argv[2]), sys.argv[3], float(sys.argv[4]), "
            "Path(sys.argv[5]))",
            str(test_file),
            json.dumps(env),
            mode,
            str(ratio),
            str(tmp_path / "captures"),
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(test_file.parents[3] / "src"),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_SCALE_FACTOR": str(ratio),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr


# Pure functions


def test_badge_order() -> None:
    connected = _status("codex", status.CONNECTED)
    assert badge_for(False, None, "none") == ("Off", "neutral")
    assert badge_for(False, _status("codex", status.NOT_INSTALLED), "codex") == (
        "Off",
        "neutral",
    )
    assert badge_for(True, None, "codex") == ("Not checked", "neutral")
    assert badge_for(True, _status("codex", status.NOT_INSTALLED), "none") == (
        "Not installed",
        "danger",
    )
    assert badge_for(True, _status("codex", status.NOT_SIGNED_IN), "none") == (
        "Not signed in",
        "warning",
    )
    assert badge_for(True, _status("codex", status.CHECK_FAILED, "x"), "none") == (
        "Check failed",
        "danger",
    )
    assert badge_for(True, connected, "none") == ("No sandbox", "warning")
    assert badge_for(True, connected, "codex") == ("Connected", "success")


def test_shortlist_entries_per_engine() -> None:
    assert shortlist_with("codex", [], "gpt-5", "high", True) == ["gpt-5@high"]
    assert shortlist_with("claude", ["opus@max"], "sonnet", "low", True) == [
        "opus@max",
        "sonnet@low",
    ]
    assert shortlist_with("opencode", [], "qwen/coder", None, True) == ["qwen/coder"]
    assert shortlist_with("opencode", [], "qwen/coder", "high", True) == [
        "qwen/coder@high"
    ]
    entries = ["a@low", "b@high", "c@low"]
    assert shortlist_with("codex", entries, "b", "max", True) == [
        "a@low",
        "b@max",
        "c@low",
    ]
    assert shortlist_with("codex", entries, "b", None, False) == ["a@low", "c@low"]


def test_initial_effort() -> None:
    codex = models.Model("gpt", "GPT", ("low", "high"), "high")
    assert initial_effort("codex", codex, "low") == "low"
    assert initial_effort("codex", codex, "bogus") == "high"
    assert initial_effort("codex", codex, None) == "high"
    claude = models.Model("opus", "opus", models.CLAUDE_EFFORTS)
    assert initial_effort("claude", claude, None) == "low"
    assert initial_effort("claude", claude, "max") == "max"
    assert initial_effort("codex", models.Model("x", "x"), None) is None
    router = models.Model("a/b", "B", ("low", "high"), "low")
    assert initial_effort("opencode", router, None) is None
    assert initial_effort("opencode", router, "high") == "high"


def test_number_formats() -> None:
    assert context_text(131_072) == "131k"
    assert context_text(1_000_000) == "1M"
    assert context_text(None) == "—"
    assert price_text(0.0000005) == "$0.50"
    assert price_text(None) == "—"
    assert per_token("0.5") == 0.0000005
    assert per_token(None) is None


@pytest.mark.parametrize("engine", status.ENGINES)
def test_model_rows_share_columns_and_centres(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    engine_theme: None,
    engine: str,
) -> None:
    prefix = "provider/" if engine == "opencode" else ""
    long_id = prefix + "model-" + "long-" * 30
    missing_id = prefix + "missing-" + "long-" * 40
    short_id = prefix + "short"
    choices.set_settings({f"shortlist.{engine}": [f"{missing_id}@high"]}, env=env)
    stubs["lists"] = {
        engine: models.ModelList(
            (
                models.Model(
                    long_id, "Long model", ("low", "high"), supports_tools=True
                ),
                models.Model(short_id, short_id, supports_tools=True),
            ),
            models.CLI,
            NOW,
            None,
        )
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, engine)
    screen.resize(1300, 1000)
    screen.show()
    QApplication.processEvents()
    first, second = panel.rows
    missing = panel.missing[0]
    assert first.note.isHidden() and second.note.isHidden()
    assert first.height() == second.height() == missing.height()
    # The column fits the room the 960 px content cap leaves, up to 320 px.
    assert 140 <= panel.name_width <= 320
    assert panel.name_header.width() == first.name.width() == missing.name.width()
    assert first.name.display_text().endswith("…")
    assert first.name.text() == long_id
    assert first.name.toolTip() == second.name.toolTip() == ""
    assert missing.name.display_text().endswith("…")
    assert missing.name.toolTip() == ""
    assert isinstance(second.effort_cell, QLabel)
    assert second.effort_cell.text() == ""
    for row in panel.rows:
        cells = [row.name, *row.figures, row.effort_cell, row.switch]
        centres = [cell.mapTo(row, cell.rect().center()).y() for cell in cells]
        assert max(centres) - min(centres) <= 1
        assert row.effort_cell.width() == 130
    headers = panel.header.findChildren(QLabel)
    for row in panel.rows:
        cells = [row.name, *row.figures, row.effort_cell]
        for cell, heading in zip(cells, headers, strict=False):
            assert cell.mapTo(panel, QPoint()).x() == heading.mapTo(panel, QPoint()).x()
    adjacent = [first.name, *first.figures, first.effort_cell]
    for left, right in zip(adjacent, adjacent[1:], strict=False):
        assert right.x() == left.x() + left.width() + 12
    assert (
        missing.info.mapTo(panel, QPoint()).x()
        == adjacent[1].mapTo(panel, QPoint()).x()
    )
    assert missing.info.width() == sum(cell.width() for cell in adjacent[1:]) + 12 * (
        len(adjacent) - 2
    )
    use_header = headers[-1]
    assert (
        missing.remove_button.mapTo(panel, QPoint()).x()
        == use_header.mapTo(panel, QPoint()).x()
    )
    assert missing.remove_button.width() == use_header.width()
    assert (
        abs(
            first.switch.mapTo(panel, first.switch.rect().center()).x()
            - use_header.mapTo(panel, use_header.rect().center()).x()
        )
        <= 1
    )
    assert first.switch.mapTo(panel, QPoint()).x() > (
        first.effort_cell.mapTo(panel, QPoint()).x() + first.effort_cell.width() + 12
    )
    height = second.height()
    second.show_note(NO_EFFORT_NOTE)
    QApplication.processEvents()
    assert second.height() > height
    assert not screen.grab().isNull()


def _name_width(label: QLabel, text: str) -> int:
    return math.ceil(
        QFontMetricsF(label.font()).horizontalAdvance(text)
        + label.width()
        - label.contentsRect().width()
    )


@pytest.mark.parametrize("engine", status.ENGINES)
@pytest.mark.parametrize(
    "ids",
    (
        ("fable", "opus", "sonnet", "haiku"),
        ("gpt-5", "gpt-5-codex", "gpt-5-mini"),
        ("i",),
        (),
    ),
)
def test_name_column_fits_ids_and_header(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    engine_theme: None,
    engine: str,
    ids: tuple[str, ...],
) -> None:
    stubs["lists"] = {
        engine: models.ModelList(
            tuple(models.Model(name, "A different display name") for name in ids),
            models.CLI,
            NOW,
            None,
        )
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, engine)
    screen.resize(1300, 1000)
    screen.show()
    QApplication.processEvents()
    header_width = _name_width(panel.name_header, "Model")
    expected = max(
        [header_width, *(_name_width(row.name, row.model.id) for row in panel.rows)]
    )
    assert panel.name_width == expected < 320
    assert panel.name_header.width() >= header_width
    for row in panel.rows:
        assert row.name.width() == expected
        assert row.name.display_text() == row.model.id
    if engine == "opencode":
        assert panel.search is not None
        panel.search.setText("no matching model")
        panel._measure_names(panel.saved())
        assert panel.name_width == expected
        assert all(row.isHidden() for row in panel.rows)


@pytest.mark.parametrize("widest", ("header", "row"))
def test_name_width_uses_polished_fonts_margins_and_frames(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    engine_theme: None,
    widest: str,
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    panel.name_header.setStyleSheet(
        f"font-size: {30 if widest == 'header' else 10}px; "
        "padding: 3px 11px; border: 2px solid red;"
    )
    for row in panel.rows:
        row.name.setStyleSheet(
            f"font-size: {30 if widest == 'row' else 10}px; "
            "padding: 3px 7px; border: 3px solid red;"
        )
    panel._measure_names(panel.saved())
    screen.resize(1300, 1000)
    screen.show()
    QApplication.processEvents()
    header_width = _name_width(panel.name_header, "Model")
    row_width = max(_name_width(row.name, row.model.id) for row in panel.rows)
    assert (header_width > row_width) == (widest == "header")
    assert panel.name_width == max(header_width, row_width)
    assert panel.name_header.width() >= header_width
    for row in panel.rows:
        assert row.name.display_text() == row.model.id


def test_name_width_measures_missing_ids_and_rebuilds(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    engine_theme: None,
) -> None:
    choices.set_settings({"shortlist.codex": ["a-longer-missing-name@high"]}, env=env)
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    assert panel.name_width == _name_width(panel.rows[0].name, "a-longer-missing-name")
    _save(context, qtbot, lambda: panel.remove("a-longer-missing-name"))
    assert not panel.missing
    assert panel.name_width == _name_width(panel.rows[0].name, "gpt-mini")
    assert all(row.name.width() == panel.name_width for row in panel.rows)
    panel.show_list(models.ModelList((), models.CLI, NOW, None))
    assert panel.rows == [] and panel.missing == []
    assert panel.header.isHidden()
    assert panel.name_width == _name_width(panel.name_header, "Model")
    assert not screen.grab().isNull()


def test_card_rows_stay_level_with_long_version(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    engine_theme: None,
) -> None:
    long_version = "2.1.316 (Claude Code) " + "build " * 25
    stubs["engines"]["claude"] = replace(
        stubs["engines"]["claude"], version=long_version
    )
    choices.set_settings(
        {f"sandbox.{engine}": "on" for engine in status.ENGINES}, env=env
    )
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    screen.resize(1000, 900)
    screen.show()
    QApplication.processEvents()
    version = screen.cards["claude"].version_label
    assert version.text() == long_version
    assert version.toolTip() == ""
    assert version.display_text().endswith("…")
    assert not version.wordWrap()
    for attribute in (
        "version_label",
        "use_switch",
        "sandbox_switch",
        "provider_label",
        "bwrap_label",
        "check_label",
        "models_button",
    ):
        cells = [getattr(card, attribute) for card in screen.cards.values()]
        centres = [cell.mapTo(screen, cell.rect().center()).y() for cell in cells]
        assert max(centres) - min(centres) <= 1, attribute
    assert not screen.grab().isNull()


def test_sandbox_check_line_wording(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    screen, context = _screen(env, qtbot)
    context.tasks.wait(5000)
    QApplication.processEvents()
    card = screen.cards["codex"]
    card._draw_check(None, False, True)
    assert card.check_label.text() == "Sandbox check: not run"
    card._draw_check(None, True, True)
    assert card.check_label.text() == "Sandbox check: checking now"
    card._draw_check(_check("codex"), False, True)
    assert card.check_label.text() == f"Sandbox check: passed, {inline(when(NOW))}"
    card._draw_check(_check("codex", ok=False, detail="blocked"), False, True)
    assert (
        card.check_label.text()
        == f"Sandbox check: failed, {inline(when(NOW))}: blocked"
    )


def test_cause_advice_names_what_to_do() -> None:
    assert "sandbox_mode" in cause_advice(
        "/etc/codex/config.toml sets sandbox_mode or cannot be read"
    )
    assert "AppArmor" not in cause_advice("Ubuntu's AppArmor profile is present")
    assert "~/.local" in cause_advice(
        "Codex is installed at /x, which the sandbox does not keep"
    )


# Cards


def test_cards_show_badges_and_versions(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["engines"] = _all(codex=status.NOT_SIGNED_IN, opencode=status.NOT_INSTALLED)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert screen.cards["codex"].badge.text() == "Not signed in"
    assert screen.cards["opencode"].badge.text() == "Not installed"
    assert screen.cards["opencode"].badge.tone() == "danger"
    claude = screen.cards["claude"]
    expected = (
        "Connected" if context.setting("sandbox.claude") == "on" else "No sandbox"
    )
    assert claude.badge.text() == expected
    assert claude.version_label.text() == "claude 1.0.0"
    assert screen.subtitle.text().startswith("Checked today ")
    assert claude.logo.pixmap() is not None


def test_cards_refresh_and_release_only_the_selected_hold(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert all(card.holds_widget.isHidden() for card in screen.cards.values())

    now = time.time()
    until = time.strftime("%H:%M", time.localtime(now + 900))
    holds.start("codex", "gpt-5", "capacity", 15, env=env, now=now)
    holds.start("codex", "other", "outage", 15, env=env, now=now)
    holds.start("claude", "opus", "usage_limit", 15, env=env, now=now)
    _shown(screen, context, qtbot)
    codex, claude = screen.cards["codex"], screen.cards["claude"]
    qtbot.waitUntil(lambda: len(codex.retry_buttons) == 2)
    assert not codex.holds_widget.isHidden()
    assert screen.cards["opencode"].holds_widget.isHidden()
    assert [line.text() for line in codex.holds_widget.findChildren(QLabel)] == [
        f"gpt-5: held after capacity until {until}",
        f"Held after outage until {until}",
    ]
    assert [line.text() for line in claude.holds_widget.findChildren(QLabel)] == [
        f"Held after usage_limit until {until}",
    ]
    retry = codex.retry_buttons[0]
    assert retry.text() == "Try again now"
    assert retry.accessibleName() == "Try again now: codex gpt-5"
    retry.click()
    qtbot.waitUntil(lambda: len(codex.retry_buttons) == 1)
    assert [(h.engine, h.model) for h in holds.active(env)] == [
        ("codex", None),
        ("claude", None),
    ]
    assert codex.retry_buttons[0].accessibleName() == "Try again now: codex"
    codex.retry_buttons[0].click()
    qtbot.waitUntil(codex.holds_widget.isHidden)
    assert [(h.engine, h.model) for h in holds.active(env)] == [("claude", None)]
    assert len(claude.retry_buttons) == 1


def test_engine_off_shows_off(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"engine.codex": "off"}, env=env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert screen.cards["codex"].badge.text() == "Off"
    assert not screen.cards["codex"].use_switch.isChecked()


def test_no_sandbox_badge_and_warning(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"sandbox.claude": "off"}, env=env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    card = screen.cards["claude"]
    assert card.badge.text() == "No sandbox"
    assert card.provider_label.text() == "Provider: None"
    assert not card.none_label.isHidden()


def test_setup_steps_per_state(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["engines"] = {
        "codex": _status("codex", status.NOT_SIGNED_IN),
        "opencode": _status("opencode", status.NOT_INSTALLED),
        "claude": _status("claude", status.CHECK_FAILED, "claude auth status exited 2"),
    }
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    codex = screen.cards["codex"]
    assert [line.command() for line in codex.commands] == ["codex login"]
    opencode = screen.cards["opencode"]
    assert [line.command() for line in opencode.commands] == [
        "curl -fsSL https://opencode.ai/install | bash"
    ]
    claude = screen.cards["claude"]
    assert claude.commands == []
    assert not claude.setup_widget.isHidden()
    assert claude.reason_label.text() == "Claude auth status exited 2"
    texts = [w.text() for w in claude.setup_widget.findChildren(QLabel)]
    assert any("Claude auth status exited 2" in text for text in texts)


def test_signed_in_engine_has_no_setup(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert screen.cards["codex"].setup_widget.isHidden()
    assert screen.cards["codex"].findChildren(CommandLine) == []


@pytest.mark.parametrize("accepted", [True, False])
def test_run_goes_through_the_context(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    accepted: bool,
) -> None:
    stubs["engines"] = _all(codex=status.NOT_SIGNED_IN)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    asked: list[str] = []

    def run_command(_parent: QWidget, command: str) -> bool:
        asked.append(command)
        return accepted

    monkeypatch.setattr(context, "run_command", run_command)
    card = screen.cards["codex"]
    qtbot.mouseClick(card.commands[0].run_button, Qt.MouseButton.LeftButton)
    assert asked == ["codex login"]
    assert card.again_widget.isHidden() is not accepted


def test_check_now_checks_engines_with_use_and_sandbox_on(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings(
        {
            "engine.opencode": "off",
            "sandbox.opencode": "on",
            "sandbox.codex": "on",
            "sandbox.claude": "off",
        },
        env=env,
    )
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert stubs["checks"] == []
    with qtbot.waitSignal(context.engines_changed, timeout=5000):
        qtbot.mouseClick(screen.check_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not screen.checking, timeout=5000)
    assert stubs["checks"] == ["codex"]
    assert (
        screen.cards["codex"]
        .check_label.text()
        .startswith("Sandbox check: passed, today ")
    )
    assert screen.cards["opencode"].check_label.text() == "Sandbox check: not run"
    assert screen.cards["claude"].check_label.text() == "Sandbox check: not run"
    assert set(sandbox.last_checks(env)) == {"codex"}


def test_sandbox_switch_on_saves_then_runs_the_check(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"sandbox.opencode": "off"}, env=env)
    stubs["check_result"] = {
        "opencode": _check("opencode", ok=False, detail="the sandbox test failed")
    }
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    card = screen.cards["opencode"]
    assert not card.sandbox_switch.isChecked()
    assert card.check_label.text() == "Sandbox check: not run"
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(card.sandbox_switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("sandbox.opencode") == "on"
    qtbot.waitUntil(lambda: stubs["checks"] == ["opencode"], timeout=5000)
    qtbot.waitUntil(lambda: not screen.checking, timeout=5000)
    assert "Sandbox check: failed, today" in card.check_label.text()
    assert card.check_label.text().endswith(": the sandbox test failed")
    assert card.bwrap_label.text().startswith("bwrap: <span style=")
    assert ">/usr/bin/bwrap</span>, 0.11.1" in card.bwrap_label.text()
    assert sandbox.last_checks(env)["opencode"].ok is False


def test_sandbox_switch_off_shows_not_run_and_runs_nothing(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    sandbox.save_check(_check("claude"), env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    card = screen.cards["claude"]
    assert card.check_label.text().startswith("Sandbox check: passed, today ")
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(card.sandbox_switch, Qt.MouseButton.LeftButton),
    )
    context.tasks.wait(5000)
    QApplication.processEvents()
    assert context.setting("sandbox.claude") == "off"
    assert stubs["checks"] == []
    assert card.check_label.text() == "Sandbox check: not run"
    assert "passed" not in card.check_label.text()


def test_a_check_without_a_sandbox_never_shows_passed(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    unsandboxed = replace(_check("codex"), provider=sandbox.NONE, detail="no sandbox")
    sandbox.save_check(unsandboxed, env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    assert screen.cards["codex"].check_label.text() == "Sandbox check: not run"


def test_cached_checks_never_overwrite_a_newer_check(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    old = replace(_check("codex", ok=False, detail="old"), checked_at=NOW - 600)
    release = threading.Event()

    def last_checks(_env: object = None) -> dict[str, sandbox.SandboxCheck]:
        release.wait(5)
        return {"codex": old}

    monkeypatch.setattr(sandbox, "last_checks", last_checks)
    stubs["check_result"] = {"codex": replace(_check("codex"), checked_at=NOW + 1)}
    screen.shown()
    screen.run_sandbox_check("codex")
    qtbot.waitUntil(lambda: not screen.checking, timeout=5000)
    release.set()
    context.tasks.wait(5000)
    QApplication.processEvents()
    assert screen.checks["codex"].ok
    assert screen.cards["codex"].check_label.text().startswith("Sandbox check: passed")

    held = screen.checks["codex"]
    release.clear()
    screen.shown()
    screen.run_sandbox_check("codex")
    release.set()
    qtbot.waitUntil(lambda: not screen.checking, timeout=5000)
    context.tasks.wait(5000)
    QApplication.processEvents()
    assert screen.checks["codex"].checked_at >= held.checked_at
    assert screen.checks["codex"].ok


def test_failed_version_probe_shows_the_reason_without_setup(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    met = replace(
        _status("codex", status.CONNECTED),
        state=status.CHECK_FAILED,
        connected=False,
        version=None,
        reason="codex --version exited 1",
    )
    stubs["engines"] = {**_all(), "codex": met}
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    card = screen.cards["codex"]
    assert card.setup_widget.isHidden()
    assert not card.reason_label.isHidden()
    assert card.reason_label.text() == "Codex --version exited 1"
    assert card.badge.text() == "Check failed"


def test_use_switch_saves(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    screen, context = _screen(env, qtbot)
    card = screen.cards["claude"]
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(card.use_switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("engine.claude") == "off"
    assert card.badge.text() == "Off"


def test_blocking_causes_show_an_alert(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    cause = "/etc/codex/config.toml sets sandbox_mode or cannot be read"
    sandbox.save_check(_check("codex", ok=False, causes=(cause,)), env)
    screen, context = _screen(env, qtbot)
    _shown(screen, context, qtbot)
    alert = screen.cards["codex"].cause_alert
    assert alert is not None
    assert alert.tone() == "danger"
    assert "Remove the sandbox_mode key" in alert.text_label.text()


def test_failed_status_read_offers_try_again(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read(_env: object, *, refresh: bool = False) -> dict[str, status.EngineStatus]:
        raise OSError("disk gone")

    monkeypatch.setattr(status, "read", read)
    screen, context = _screen(env, qtbot)
    with qtbot.waitSignal(context.engines_failed, timeout=5000):
        screen.shown()
    assert screen.failure is not None
    assert [b.text() for b in screen.failure.buttons] == ["Try again"]


# Model lists


def _codex_list() -> models.ModelList:
    return models.ModelList(
        models=(
            models.Model("gpt-5", "GPT-5", ("low", "medium", "high"), "medium"),
            models.Model("gpt-mini", "GPT mini", (), None),
        ),
        source=models.CLI,
        fetched_at=NOW,
        reason=None,
    )


def _router_list() -> models.ModelList:
    return models.ModelList(
        models=(
            models.Model(
                "openrouter/acme/big",
                "Big",
                ("low", "high"),
                None,
                1_000_000,
                0.000002,
                0.000008,
                True,
                provider="openrouter",
                provider_name="OpenRouter",
                login="api",
            ),
            models.Model(
                "openrouter/acme/small",
                "Small",
                (),
                None,
                32_768,
                0.0000001,
                0,
                True,
                provider="openrouter",
                provider_name="OpenRouter",
                login="api",
            ),
            models.Model(
                "openrouter/other/chat",
                "Chat",
                (),
                None,
                128_000,
                0.0000004,
                0.000001,
                False,
                provider="openrouter",
                provider_name="OpenRouter",
                login="api",
            ),
        ),
        source=models.CLI,
        fetched_at=NOW,
        reason=None,
        integrations=(models.Integration("openrouter", "OpenRouter", "api"),),
    )


def _integrations_list() -> models.ModelList:
    router = _router_list()
    return replace(
        router,
        models=(
            models.Model(
                "anthropic/claude",
                "Claude",
                context_length=200_000,
                prompt_price=0.00002,
                completion_price=0.00003,
                supports_tools=True,
                provider="anthropic",
                provider_name="Anthropic",
                login="oauth",
            ),
            models.Model(
                "openai/gpt",
                "GPT",
                supports_tools=True,
                provider="openai",
                provider_name="OpenAI",
                login="oauth",
            ),
            *router.models,
            models.Model(
                "opencode/unknown",
                "Unknown",
                provider="opencode",
                provider_name="OpenCode",
            ),
        ),
        integrations=(
            models.Integration("anthropic", "Anthropic", "oauth"),
            models.Integration("openai", "OpenAI", "oauth"),
            *router.integrations,
            models.Integration("opencode", "OpenCode"),
            models.Integration("empty", "Empty provider", "api"),
        ),
    )


def _open(screen: EnginesScreen, context: AppContext, qtbot: QtBot, engine: str) -> Any:  # noqa: ANN401
    qtbot.mouseClick(screen.cards[engine].models_button, Qt.MouseButton.LeftButton)
    panel = screen.panel
    assert panel is not None
    qtbot.waitUntil(lambda: panel.models is not None or panel.state.error.isVisible())
    return panel


def test_models_open_one_at_a_time(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list(), "opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    _open(screen, context, qtbot, "codex")
    assert screen.open_engine == "codex"
    assert screen.cards["codex"].models_button.isChecked()
    _open(screen, context, qtbot, "opencode")
    assert screen.open_engine == "opencode"
    assert not screen.cards["codex"].models_button.isChecked()
    qtbot.mouseClick(screen.cards["opencode"].models_button, Qt.MouseButton.LeftButton)
    assert screen.panel is None
    assert screen.open_engine is None


def test_codex_shortlist_and_effort(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    row = panel.rows[0]
    assert row.effort() == "medium"
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.codex") == ["gpt-5@medium"]
    assert screen.cards["codex"].shortlisted_label.text() == "1 shortlisted"
    index = row.picker.findData("high")

    def pick() -> None:
        row.picker.setCurrentIndex(index)
        row.picker.activated.emit(index)

    _save(context, qtbot, pick)
    assert context.setting("shortlist.codex") == ["gpt-5@high"]
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.codex") == []


def test_effort_change_off_the_shortlist_saves_nothing(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    row = panel.rows[0]
    generation = context.settings()["generation"]
    index = row.picker.findData("low")
    row.picker.setCurrentIndex(index)
    row.picker.activated.emit(index)
    context.tasks.wait(5000)
    QApplication.processEvents()
    assert context.settings()["generation"] == generation
    assert row.effort() == "low"


def test_model_without_effort_cannot_be_shortlisted(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    row = panel.rows[1]
    assert row.picker is None
    qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton)
    assert not row.switch.isChecked()
    assert row.note.text() == NO_EFFORT_NOTE
    assert context.setting("shortlist.codex") == []


def test_claude_shortlist_entry(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {
        "claude": models.ModelList(
            models=tuple(
                models.Model(name, name, models.CLAUDE_EFFORTS)
                for name in models.CLAUDE_MODELS
            ),
            source=models.CLI,
            fetched_at=NOW,
            reason=None,
        )
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "claude")
    row = next(r for r in panel.rows if r.model.id == "opus")
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.claude") == ["opus@low"]


def test_opencode_entries_with_and_without_effort(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    big, small = panel.rows[0], panel.rows[1]
    assert big.effort() is None
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(small.switch, Qt.MouseButton.LeftButton),
    )
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(big.switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.opencode") == [
        "openrouter/acme/small",
        "openrouter/acme/big",
    ]
    index = big.picker.findData("high")

    def pick() -> None:
        big.picker.setCurrentIndex(index)
        big.picker.activated.emit(index)

    _save(context, qtbot, pick)
    assert context.setting("shortlist.opencode") == [
        "openrouter/acme/small",
        "openrouter/acme/big@high",
    ]


def test_opencode_filters_and_search(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")

    def shown() -> list[str]:
        return [row.model.id for row in panel.rows if not row.isHidden()]

    assert shown() == ["openrouter/acme/big", "openrouter/acme/small"]
    assert panel.count_label.text() == "2 of 3 models"
    qtbot.mouseClick(panel.tools_switch, Qt.MouseButton.LeftButton)
    assert shown() == [
        "openrouter/acme/big",
        "openrouter/acme/small",
        "openrouter/other/chat",
    ]
    panel.price_picker.set_value("0.5")
    assert shown() == ["openrouter/acme/small", "openrouter/other/chat"]
    panel.context_picker.set_value(64_000)
    assert shown() == ["openrouter/other/chat"]
    panel.context_picker.set_value(None)
    panel.search.setText("SMALL")
    assert shown() == ["openrouter/acme/small"]
    assert panel.count_label.text() == "1 of 3 models"
    panel.search.setText("nothing")
    assert shown() == []
    assert not panel.state.empty.isHidden()
    assert panel.state.empty_title.text() == "No models match"


def test_provider_picker_counts_and_filters(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    result = _integrations_list()
    stubs["lists"] = {"opencode": result}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    picker = panel.provider_picker
    assert picker.accessibleName() == "Provider"
    assert [picker.itemText(i) for i in range(picker.count())] == [
        "All providers",
        "Anthropic",
        "OpenAI",
        "OpenRouter",
        "OpenCode",
        "Empty provider",
    ]
    assert [picker.itemData(i, HINT_ROLE) for i in range(picker.count())] == [
        "6 models",
        "1 model",
        "1 model",
        "3 models",
        "1 model",
        "0 models",
    ]
    assert panel.provider_info.toolTip() == (
        "The OpenCode integration that serves the model. Ember lists the "
        "integrations you connected with /connect in OpenCode."
    )
    assert panel.provider_info.nextInFocusChain() is picker
    panel.tools_switch.click()
    for integration in result.integrations:
        picker.set_value(integration.id)
        assert [row.model.id for row in panel.rows if not row.isHidden()] == [
            model.id for model in result.models if model.provider == integration.id
        ]
    assert panel.state.empty_title.text() == "No models match"
    picker.set_value("openrouter")
    panel.search.setText("small")
    panel.price_picker.set_value("0.5")
    panel.context_picker.set_value(32_000)
    assert [row.model.id for row in panel.rows if not row.isHidden()] == [
        "openrouter/acme/small"
    ]
    assert picker.itemData(picker.findData("openrouter"), HINT_ROLE) == "3 models"
    picker.set_value("anthropic")
    assert all(row.isHidden() for row in panel.rows)


def test_provider_columns_and_subscription_prices(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any], engine_theme: None
) -> None:
    stubs["lists"] = {"opencode": _integrations_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    screen.resize(1500, 1000)
    screen.show()
    QApplication.processEvents()
    assert [cell.text() for cell in panel.header.findChildren(QLabel)] == [
        "Model",
        "Provider",
        "Context",
        "Input $/M",
        "Output $/M",
        "Effort",
        "Use",
    ]
    for row in panel.rows:
        assert row.figures[0].text() == row.model.provider_name
        if row.model.login == "oauth":
            for cell in row.figures[2:]:
                assert cell.text() == "Subscription"
                assert (
                    cell.contentsRect().width()
                    >= cell.fontMetrics().horizontalAdvance(cell.text())
                )
        else:
            assert [cell.text() for cell in row.figures[2:]] == [
                price_text(row.model.prompt_price),
                price_text(row.model.completion_price),
            ]
    for _text, value in PRICE_CHOICES:
        panel.price_picker.set_value(value)
        assert all(not row.isHidden() for row in panel.rows[:2])
    panel.context_picker.set_value(128_000)
    assert not panel.rows[0].isHidden()
    assert panel.rows[1].isHidden()
    panel.context_picker.set_value(None)
    panel.provider_picker.set_value("opencode")
    panel.price_picker.set_value(None)
    panel.tools_switch.click()
    assert not panel.rows[-1].isHidden()
    panel.price_picker.set_value("0.5")
    assert panel.rows[-1].isHidden()
    panel.price_picker.set_value(None)
    panel.context_picker.set_value(32_000)
    assert panel.rows[-1].isHidden()


def test_provider_refresh_preserves_id_and_resets_when_removed(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    result = _integrations_list()
    stubs["lists"] = {"opencode": result}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    panel.provider_picker.set_value("anthropic")
    refreshed = replace(
        result,
        models=result.models[1:],
        integrations=(
            *result.integrations[1:],
            models.Integration("anthropic", "Renamed integration", "oauth"),
        ),
    )
    stubs["lists"]["opencode"] = refreshed
    panel.refresh_button.click()
    qtbot.waitUntil(lambda: panel.models is refreshed.models)
    assert panel.provider_picker.value() == "anthropic"
    assert panel.provider_picker.currentText() == "Renamed integration"
    assert panel.provider_picker.currentData(HINT_ROLE) == "0 models"
    assert all(row.isHidden() for row in panel.rows)
    removed = replace(refreshed, integrations=refreshed.integrations[:-1])
    stubs["lists"]["opencode"] = removed
    panel.refresh_button.click()
    qtbot.waitUntil(lambda: panel.provider_picker.value() is None)
    assert panel.provider_picker.currentText() == "All providers"
    assert any(not row.isHidden() for row in panel.rows)


@pytest.mark.parametrize("connected", (False, True))
def test_provider_picker_with_no_models(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any], connected: bool
) -> None:
    integrations = (models.Integration("empty", "Empty"),) if connected else ()
    stubs["lists"] = {
        "opencode": models.ModelList((), models.CLI, NOW, None, integrations)
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    assert panel.provider_picker.count() == 1 + int(connected)
    for index in range(panel.provider_picker.count()):
        panel.provider_picker.setCurrentIndex(index)
        assert panel.provider_picker.currentData(HINT_ROLE) == "0 models"
        assert panel.state.empty_title.text() == "No models"
        assert panel.count_label.text() == "0 of 0 models"


def test_shortlist_keeps_selected_integration(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"opencode": _integrations_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    panel.provider_picker.set_value("anthropic")
    _save(context, qtbot, panel.rows[0].switch.click)
    assert context.setting("shortlist.opencode") == ["anthropic/claude"]


def test_saved_entry_missing_from_the_list_can_be_removed(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings({"shortlist.codex": ["gone@high", "gpt-5@low"]}, env=env)
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    assert panel.rows[0].switch.isChecked()
    assert panel.rows[0].effort() == "low"
    assert [row.model_id for row in panel.missing] == ["gone"]
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(
            panel.missing[0].remove_button, Qt.MouseButton.LeftButton
        ),
    )
    assert context.setting("shortlist.codex") == ["gpt-5@low"]
    assert panel.missing == []


def test_removing_a_menu_model_keeps_the_menu_value(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings(
        {"shortlist.codex": ["gpt-5@low"], "single.writer": "codex:gpt-5"}, env=env
    )
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    _save(
        context,
        qtbot,
        lambda: qtbot.mouseClick(panel.rows[0].switch, Qt.MouseButton.LeftButton),
    )
    assert context.setting("shortlist.codex") == []
    assert context.setting("single.writer") == "codex:gpt-5"


def test_list_with_a_reason_keeps_cached_rows(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    held = _codex_list()
    stubs["lists"] = {
        "codex": models.ModelList(held.models, models.CACHE, NOW, "codex exited 1")
    }
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    assert len(panel.rows) == 2
    assert panel.notice is not None
    assert [b.text() for b in panel.notice.buttons] == ["Try again"]
    stubs["lists"] = {"codex": held}
    qtbot.mouseClick(panel.notice.buttons[0], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(stubs["list_calls"]) == 2, timeout=5000)
    context.tasks.wait(5000)
    QApplication.processEvents()
    assert stubs["list_calls"][-1] == ("codex", True)
    assert panel.notice is None


def test_unreadable_list_shows_error_with_try_again(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"opencode": models.ModelList((), models.CLI, None, "no network")}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    assert "no network" in panel.state.error_text.text()
    assert not panel.state.retry_button.isHidden()
    stubs["lists"] = {"opencode": _router_list()}
    qtbot.mouseClick(panel.state.retry_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(panel.rows) == 3, timeout=5000)
    assert stubs["list_calls"][-1] == ("opencode", True)


def test_unreadable_list_lists_no_shortlist_entry_as_missing(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    choices.set_settings(
        {"shortlist.opencode": ["openai/gpt-5@high", "anthropic/claude-x"]}, env=env
    )
    stubs["lists"] = {"opencode": models.ModelList((), models.CLI, None, "no network")}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    assert "no network" in panel.state.error_text.text()
    assert panel.missing == []
    assert panel.missing_widget.isHidden()
    stubs["lists"] = {"opencode": _router_list()}
    qtbot.mouseClick(panel.state.retry_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(panel.rows) == 3, timeout=5000)
    assert sorted(row.model_id for row in panel.missing) == [
        "anthropic/claude-x",
        "openai/gpt-5",
    ]


def test_refresh_list_reads_again(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    assert stubs["list_calls"] == [("codex", False)]
    qtbot.mouseClick(panel.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(stubs["list_calls"]) == 2, timeout=5000)
    assert stubs["list_calls"][-1] == ("codex", True)


def test_stale_save_reloads_the_shortlist(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")
    choices.set_settings({"shortlist.codex": ["gpt-5@high"]}, env=env)
    row = panel.rows[0]
    assert not row.switch.isChecked()
    with qtbot.waitSignal(context.stale, timeout=5000):
        qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton)
    assert context.setting("shortlist.codex") == ["gpt-5@high"]
    assert row.switch.isChecked()
    assert row.effort() == "high"


def test_refused_save_leaves_the_switch_as_saved(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stubs["lists"] = {"codex": _codex_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "codex")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise choices.InvalidChoiceError("refused")

    monkeypatch.setattr(choices, "set_settings", refuse)
    row = panel.rows[0]
    with qtbot.waitSignal(context.error, timeout=5000) as blocker:
        qtbot.mouseClick(row.switch, Qt.MouseButton.LeftButton)
    assert blocker.args == ["The setting was not saved: refused"]
    assert not row.switch.isChecked()


# Keyboard


def test_every_control_is_reached_with_tab(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["engines"] = _all(codex=status.NOT_SIGNED_IN)
    stubs["lists"] = {"opencode": _router_list()}
    screen, context = _screen(env, qtbot)
    screen.resize(1100, 720)
    screen.show()
    qtbot.waitExposed(screen)
    _shown(screen, context, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    expected: list[QWidget] = [screen.check_button]
    for card in screen.cards.values():
        expected += [card.use_switch, card.sandbox_switch]
        for line in card.commands:
            expected += [line.copy_button, line.run_button]
        expected.append(card.models_button)
    expected += [
        panel.refresh_button,
        panel.search,
        panel.tools_switch,
        panel.price_picker,
        panel.context_picker,
    ]
    for row in panel.rows:
        if not row.isHidden():
            expected += row.focus_order()
    with qtbot.waitActive(screen):
        screen.activateWindow()
    screen.check_button.setFocus(Qt.FocusReason.TabFocusReason)
    reached: list[QWidget] = []
    for _ in range(len(expected) + 5):
        widget = QApplication.focusWidget()
        if widget is not None and widget not in reached:
            reached.append(widget)
        screen.focusNextChild()
    assert [w for w in reached if w in expected] == expected


def test_check_now_has_the_refresh_icon(env: dict[str, str], qtbot: QtBot) -> None:
    screen, _context = _screen(env, qtbot)
    assert not screen.check_button.icon().isNull()
    assert screen.check_button.text().strip() == "Check now"
    assert screen.check_button.accessibleName() == "Check now"
    before = screen.check_button.icon().cacheKey()
    theme.signals.changed.emit(theme.current_mode())
    assert screen.check_button.icon().cacheKey() != before


def test_engine_cards_fit_the_window_width(env: dict[str, str], qtbot: QtBot) -> None:
    screen, _context = _screen(env, qtbot)
    screen.resize(1400, 700)
    screen.show()
    qtbot.waitUntil(lambda: screen.columns == 3, timeout=2000)
    screen.resize(500, 700)
    qtbot.waitUntil(lambda: screen.columns < 3, timeout=2000)
    assert screen.scroll_area.horizontalScrollBar().maximum() == 0
    positions = [
        (card.mapTo(screen, card.rect().topLeft()).y())
        for card in screen.cards.values()
    ]
    assert len(set(positions)) > 1
    screen.resize(1400, 700)
    qtbot.waitUntil(lambda: screen.columns == 3, timeout=2000)


def test_engine_cards_never_scroll_sideways_while_resizing(
    env: dict[str, str], qtbot: QtBot
) -> None:
    screen, _context = _screen(env, qtbot)
    screen.show()
    for width in range(560, 1300, 2):
        screen.resize(width, 700)
        QApplication.processEvents()
        assert screen.scroll_area.horizontalScrollBar().maximum() == 0, width
        for card in screen.cards.values():
            assert card.width() >= card.minimumSizeHint().width(), width


def test_opencode_filters_fit_the_default_window(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    stubs["lists"] = {"opencode": _integrations_list()}
    screen, context = _screen(env, qtbot)
    panel = _open(screen, context, qtbot, "opencode")
    names = {"Provider", "Tools", "Max price", "Min context"}
    assert names <= {label.text() for label in panel.findChildren(QLabel)}
    tips = {tip.accessibleName() for tip in panel.findChildren(InfoTip)}
    assert {f"About {name}" for name in names} <= tips
    from ember.app.screens.common import FittingRow

    rows = panel.findChildren(FittingRow)
    assert len(rows) == 2
    screen.resize(880, 720)
    screen.show()
    qtbot.wait(20)
    assert all(row.width() <= 880 - 2 * 24 for row in rows)


@pytest.mark.parametrize(("width", "scrolls"), [(880, False), (680, True)])
def test_opencode_table_stays_inside_the_window(
    env: dict[str, str],
    qtbot: QtBot,
    stubs: dict[str, Any],
    width: int,
    scrolls: bool,
) -> None:
    listed = _integrations_list()
    long_id = replace(
        listed.models[0],
        id="openrouter/~anthropic/claude-fable-latest-with-a-long-name-1017",
    )
    stubs["lists"] = {
        "opencode": replace(listed, models=(long_id, *listed.models)),
        "codex": _codex_list(),
    }
    screen, context = _screen(env, qtbot)
    screen.resize(width, 720)
    screen.show()
    page = screen.findChildren(QScrollArea)[0]
    content = page.widget()
    assert content is not None
    for engine in ("codex", "opencode", "codex", "opencode"):
        panel = _open(screen, context, qtbot, engine)
        qtbot.waitUntil(
            lambda p=panel: p.table_area.height() >= p.table.sizeHint().height()
        )
        assert content.width() <= page.viewport().width()
        assert panel.width() <= page.viewport().width()
    area = panel.table_area
    qtbot.waitUntil(lambda: area.horizontalScrollBar().isVisible() is scrolls)
    assert panel.name_width < panel.measured_name
    assert all(row.name.width() == panel.name_width for row in panel.rows)
    assert panel.name_header.width() == panel.name_width


def test_opencode_table_height_follows_its_scroll_bar(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any]
) -> None:
    listed = _integrations_list()
    long_id = replace(
        listed.models[0],
        id="openrouter/~anthropic/claude-fable-latest-with-a-long-name-1017",
    )
    stubs["lists"] = {"opencode": replace(listed, models=(long_id, *listed.models))}
    choices.set_settings(
        {"shortlist.opencode": ["openrouter/acme/gone-from-the-list"]}, env=env
    )
    screen, context = _screen(env, qtbot)
    screen.resize(900, 720)
    screen.show()
    panel = _open(screen, context, qtbot, "opencode")
    assert panel.missing
    area = panel.table_area
    for width in range(850, 835, -1):
        screen.resize(width, 720)
        QApplication.processEvents()
        qtbot.waitUntil(
            lambda: (
                area.viewport().height() >= panel.table.sizeHint().height()
                and area.horizontalScrollBar().isVisible()
                == (panel.table.minimumSizeHint().width() > area.viewport().width())
            ),
            timeout=2000,
        )


@pytest.mark.parametrize("width", [576, 1400])
def test_screen_fits_width(
    env: dict[str, str], qtbot: QtBot, stubs: dict[str, Any], width: int
) -> None:
    from .test_common import assert_screen_fits  # pyrefly: ignore[missing-import]

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    theme.apply(app, "light")
    stubs["lists"] = {"codex": _codex_list(), "opencode": _integrations_list()}
    screen, context = _screen(env, qtbot)
    screen.resize(width, 480)
    screen.show()
    _shown(screen, context, qtbot)
    for engine in ("codex", "opencode"):
        _open(screen, context, qtbot, engine)
        qtbot.wait(50)
        assert screen.width() == width
        assert_screen_fits(screen)
