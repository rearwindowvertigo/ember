import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QLineEdit,
    QTableWidget,
    QWidget,
)
from pytestqt.qtbot import QtBot

from ember import benchmark_recommendations, benchmarks, choices, db
from ember.app import theme, widgets
from ember.app.context import AppContext
from ember.app.screens.benchmarks import BenchmarksScreen, _badge_position
from ember.app.screens.menu import MenuScreen
from ember.app.window import MainWindow
from ember.benchmark_sources import SOURCES, Observation, SourceError
from ember.engines import models, status


def _context(env: dict[str, str], qtbot: QtBot) -> AppContext:
    context = AppContext(env)
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    return context


def _screen(env: dict[str, str], qtbot: QtBot) -> BenchmarksScreen:
    screen = BenchmarksScreen(_context(env, qtbot))
    qtbot.addWidget(screen)
    qtbot.waitUntil(lambda: screen.chart.report is not None, timeout=5000)
    return screen


def _texts(widget: QWidget) -> list[str]:
    return [
        line.text() for line in widget.findChildren(QLabel) if line.isVisibleTo(widget)
    ]


def _pick_margin(screen: BenchmarksScreen, value: str) -> None:
    assert screen.margin.set_value(value)
    screen.margin.activated.emit(screen.margin.currentIndex())


def _wait_for_report(screen: BenchmarksScreen, qtbot: QtBot) -> None:
    expected = benchmark_recommendations.read(screen.context.env)
    qtbot.waitUntil(
        lambda: (
            screen.chart.report == expected
            and screen.rank_status.text() != "Saving lead settings…"
            and screen.rank_status.text() != "Reading saved lead recommendations…"
        ),
        timeout=5000,
    )


def _connections(env: dict[str, str], *connected: str) -> None:
    entries = {
        app: status.EngineStatus(
            engine=app,
            state=status.CONNECTED if app in connected else status.NOT_SIGNED_IN,
            reason=None,
            installed=True,
            binary=None,
            version=None,
            signed_in=app in connected,
            connected=app in connected,
            requirements=(),
            checked_at=10.0,
        ).to_json()
        for app in status.ENGINES
    }
    status.save_cache_file(status.cache_path(env), {"engines": entries}, env)


def _catalogue(env: dict[str, str], app: str, *entries: models.Model) -> None:
    catalogue = models.ModelList(entries, models.CACHE, 10.0, None)
    status.save_cache_file(
        models.cache_path(app, env),
        {**catalogue.to_json(), "format_version": models.CACHE_VERSION},
        env,
    )


def _row(model: str, effort: str, score: float, cost: float) -> Observation:
    return Observation(
        model=model,
        model_creator="OpenAI",
        model_release_date="2026-06-01",
        harness=None,
        effort=effort,
        metrics={"intelligence_index": score, "task_usd": cost},
    )


def _unavailable() -> list[Observation]:
    raise SourceError("fixture unavailable")


@pytest.fixture
def no_refresh(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def unexpected(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("Unexpected benchmark download or model discovery")

    monkeypatch.setattr(benchmarks, "read", unexpected)
    monkeypatch.setattr(benchmarks, "read_artificial_analysis", unexpected)
    monkeypatch.setattr(models, "_read_codex", unexpected)
    monkeypatch.setattr(models, "_read_opencode", unexpected)
    monkeypatch.setattr(status, "_check_all", unexpected)
    return calls


def _index(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, *rows: Observation
) -> None:
    benchmarks.configure_artificial_analysis("fixture-key", env)
    with monkeypatch.context() as patch:
        patch.setattr(
            benchmarks, "read_artificial_analysis", lambda _key: ("5.0", list(rows))
        )
        assert benchmarks.refresh(env)["status"] == "ok"


@pytest.fixture
def comparable(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, no_refresh: list[object]
) -> list[object]:
    _connections(env, "codex", "opencode")
    _catalogue(
        env,
        "codex",
        models.Model("gpt-6.2-sol", "Sol", ("low", "high")),
        models.Model("gpt-6.3", "GPT-6.3", ("high",)),
    )
    _catalogue(
        env,
        "opencode",
        models.Model("openrouter/openai/gpt-6.2-sol", "Sol", ("low",)),
    )
    _index(
        env,
        monkeypatch,
        _row("GPT-6.2 Sol", "low", 59.8, 0.01),
        _row("GPT-6.2 Sol", "high", 61.2, 0.15),
        _row("GPT-6.3", "high", 64.8, 0.8),
    )
    return no_refresh


def test_saved_lead_settings_use_the_same_report_in_a_new_screen(
    env: dict[str, str], qtbot: QtBot, comparable: list[object]
) -> None:
    screen = _screen(env, qtbot)
    _wait_for_report(screen, qtbot)
    assert screen.margin.closed_text() == "5"
    assert screen.lead_apps["all"].isChecked()
    assert not screen.one_per_model.isChecked()
    assert screen.rank_status.text() == "Best comparable score 64.8 · Cutoff 59.8"
    assert "1. GPT-6.2 Sol" in _texts(screen)
    assert "3. GPT-6.3" in _texts(screen)
    assert "low · 2026-06-01" in _texts(screen)
    assert "Codex · gpt-6.2-sol @ low" in _texts(screen)
    assert "OpenCode · openrouter/openai/gpt-6.2-sol @ low" in _texts(screen)

    with qtbot.waitSignal(screen.context.settings_changed):
        screen.one_per_model.click()
    _wait_for_report(screen, qtbot)
    assert "2. GPT-6.3" in _texts(screen)
    assert "3. GPT-6.3" not in _texts(screen)

    with qtbot.waitSignal(screen.context.settings_changed):
        screen.lead_apps["codex"].click()
    _wait_for_report(screen, qtbot)
    assert screen.rank_status.text() == "Best comparable score 59.8 · Cutoff 54.8"
    assert screen.lead_apps["opencode"].isChecked()
    assert not screen.lead_apps["all"].isChecked()

    with qtbot.waitSignal(screen.context.settings_changed):
        screen.lead_apps["codex"].click()
    with qtbot.waitSignal(screen.context.settings_changed):
        _pick_margin(screen, "0")
    _wait_for_report(screen, qtbot)
    assert not screen.lead_apps["all"].isChecked()
    assert screen.rank_status.text() == "Best comparable score 64.8 · Cutoff 64.8"
    assert "1. GPT-6.3" in _texts(screen)
    assert "1. GPT-6.2 Sol" not in _texts(screen)

    new_screen = _screen(env, qtbot)
    assert new_screen.margin.closed_text() == "0"
    assert new_screen.one_per_model.isChecked()
    assert new_screen.lead_apps["codex"].isChecked()
    assert new_screen.lead_apps["opencode"].isChecked()
    assert not new_screen.lead_apps["all"].isChecked()
    assert new_screen.chart.report == screen.chart.report
    assert comparable == []


def test_cache_creation_dynamic_all_and_unavailable_explicit_choices(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    no_refresh: list[object],
) -> None:
    _index(env, monkeypatch, _row("GPT-6.3", "high", 64.8, 0.8))
    screen = _screen(env, qtbot)
    assert screen.lead_apps["codex"].isHidden()

    _connections(env, "codex")
    qtbot.waitUntil(lambda: not screen.lead_apps["codex"].isHidden())
    assert screen.rank_status.text() == "Refresh model catalogues in Engines for Codex."
    assert not screen.open_engines.isHidden()

    _catalogue(env, "codex", models.Model("gpt-6.3", "GPT-6.3", ("high",)))
    qtbot.waitUntil(lambda: "1. GPT-6.3" in _texts(screen))
    _connections(env, "codex", "opencode")
    qtbot.waitUntil(lambda: not screen.lead_apps["opencode"].isHidden())
    assert screen.lead_apps["all"].isChecked()
    assert screen.lead_apps["opencode"].isChecked()
    with qtbot.waitSignal(screen.context.settings_changed):
        screen.lead_apps["opencode"].click()

    _connections(env, "opencode", "claude")
    qtbot.waitUntil(lambda: screen.lead_apps["codex"].text() == "Codex (unavailable)")
    assert screen.lead_apps["codex"].isChecked()
    assert not screen.lead_apps["claude"].isChecked()
    with qtbot.waitSignal(screen.context.settings_changed):
        screen.lead_apps["codex"].click()
    _wait_for_report(screen, qtbot)
    assert screen.lead_apps["codex"].isHidden()
    assert "Select at least one lead app" in screen.rank_status.text()
    assert screen.context.setting(choices.BENCHMARK_LEAD_APPS) == []
    with qtbot.waitSignal(screen.context.settings_changed):
        screen.lead_apps["all"].click()
    _wait_for_report(screen, qtbot)
    assert screen.lead_apps["opencode"].isChecked()
    assert screen.lead_apps["claude"].isChecked()
    assert no_refresh == []


def test_shown_and_engine_signal_read_saved_inputs(
    env: dict[str, str],
    qtbot: QtBot,
    comparable: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = _screen(env, qtbot)
    choices.set_settings({choices.BENCHMARK_SCORE_MARGIN: "12"}, env=env)
    screen.shown()
    _wait_for_report(screen, qtbot)
    assert screen.margin.closed_text() == "12"

    cached = status.cached(env)
    assert cached is not None
    cached["codex"] = replace(cached["codex"], connected=False)
    monkeypatch.setattr(status, "cached", lambda _env: cached)
    screen.context.engines_changed.emit(cached)
    _wait_for_report(screen, qtbot)
    assert screen.lead_apps["codex"].isHidden()
    assert screen.rank_status.text() == "Best comparable score 59.8 · Cutoff 47.8"
    assert comparable == []


@pytest.mark.parametrize("failure", ["busy", "stale"])
def test_rapid_saves_render_only_confirmed_settings_after_a_failure(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    comparable: list[object],
    failure: str,
) -> None:
    screen = _screen(env, qtbot)
    set_settings = choices.set_settings
    entered, release = threading.Event(), threading.Event()

    if failure == "stale":
        set_settings({choices.BENCHMARK_SCORE_MARGIN: "3"}, env=env)

    def delayed(
        values: dict[str, object],
        *,
        expect_generation: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        if values == {choices.BENCHMARK_SCORE_MARGIN: "0"}:
            entered.set()
            assert release.wait(5)
            if failure == "busy":
                raise db.DatabaseBusyError("fixture")
        return set_settings(values, expect_generation=expect_generation, env=env)

    monkeypatch.setattr(choices, "set_settings", delayed)
    try:
        _pick_margin(screen, "0")
        qtbot.waitUntil(entered.is_set)
        screen.one_per_model.click()
        _pick_margin(screen, "8")
        assert screen.rank_status.text() == "Saving lead settings…"
        assert "1. GPT-6.2 Sol" not in _texts(screen)
    finally:
        release.set()
    expected = "8" if failure == "busy" else "3"
    qtbot.waitUntil(
        lambda: screen.context.setting(choices.BENCHMARK_SCORE_MARGIN) == expected
    )
    _wait_for_report(screen, qtbot)
    assert screen.margin.closed_text() == expected
    assert screen.one_per_model.isChecked() is (failure == "busy")
    assert ("1. GPT-6.2 Sol" in _texts(screen)) is (failure == "busy")
    assert comparable == []


def test_benchmarks_show_external_refresh_and_collapse_related_results(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high"],
            "routing.pool": {"codex:gpt-5": {"scores": {}, "notes": "Owner evidence"}},
        },
        env=env,
    )
    for key, source in SOURCES.items():
        monkeypatch.setitem(SOURCES, key, replace(source, read=_unavailable))
    screen = _screen(env, qtbot)
    assert "No matching benchmark for this setup." in _texts(screen)
    assert screen.related.isHidden()

    key = "terminal-bench-4.1"
    sample = Observation(
        model="GPT-5",
        harness="Codex",
        effort="high",
        metrics={"resolution": 63.25, "attempt_usd": 1.25, "pass_usd": 2},
    )
    monkeypatch.setitem(SOURCES, key, replace(SOURCES[key], read=lambda: [sample]))
    benchmarks.refresh(env)
    exact = (
        "Terminal-Bench 4.1 · Resolved % 63.25 · USD / attempt 1.25 · USD / pass 2\n"
        "GPT-5 · high · Codex"
    )
    qtbot.waitUntil(lambda: exact in _texts(screen), timeout=5000)
    assert "1/1 pool workers" in screen.benchmark_summary.text()
    assert "Owner evidence" not in _texts(screen)
    assert any("last checked" in text for text in _texts(screen))
    assert any("fixture unavailable" in text for text in _texts(screen))

    sample["metrics"]["resolution"] = 64.5
    benchmarks.refresh(env)
    updated = exact.replace("63.25", "64.5")
    qtbot.waitUntil(lambda: updated in _texts(screen), timeout=5000)
    assert exact not in _texts(screen)

    choices.set_settings({"shortlist.codex": ["gpt-5@low"]}, env=env)
    with qtbot.waitSignal(screen.context.settings_changed, timeout=5000):
        screen.context.reload_settings()
    qtbot.waitUntil(lambda: not screen.related.isHidden())
    assert updated not in _texts(screen)
    assert "No matching benchmark for this setup." in _texts(screen)
    screen.related.click()
    assert updated in _texts(screen)
    assert "Different effort." in _texts(screen)


def test_masked_key_cancellation_and_effort_matrix(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, source in SOURCES.items():
        monkeypatch.setitem(SOURCES, key, replace(source, read=_unavailable))
    calls: list[str] = []

    def reference(key: str) -> tuple[str, list[Observation]]:
        calls.append(key)
        return "5.0", [
            Observation(
                model="Claude Opus 5.5",
                model_creator="Anthropic",
                harness=None,
                effort="medium",
                configuration="Default Fallback",
                metrics={"intelligence_index": 55, "task_usd": 1.25},
            )
        ]

    monkeypatch.setattr(benchmarks, "read_artificial_analysis", reference)
    choices.set_settings(
        {
            "shortlist.claude": ["opus@medium", "sonnet@low"],
            "routing.pool": {"claude:opus": {"scores": {}}},
        },
        env=env,
    )
    screen = _screen(env, qtbot)
    assert screen.aa_matrix.isHidden()
    assert screen.aa_remove.isHidden()

    def enter(dialog: QDialog) -> int:
        field = dialog.findChild(QLineEdit)
        assert field is not None and field.echoMode() == QLineEdit.EchoMode.Password
        assert field.text() == ""
        field.setText("fixture-key")
        return QDialog.DialogCode.Accepted

    with monkeypatch.context() as patch:
        patch.setattr(QDialog, "exec", enter)
        screen.aa_key.click()
    qtbot.waitUntil(lambda: screen.aa_status.text().startswith("API key saved."))
    assert not calls

    screen = _screen(env, qtbot)
    assert screen.aa_key.text() == "Replace API key"
    assert not screen.aa_remove.isHidden()

    def cancel(dialog: QDialog) -> int:
        enter(dialog)
        field = dialog.findChild(QLineEdit)
        assert field is not None
        field.setText("discarded-key")
        return QDialog.DialogCode.Rejected

    with monkeypatch.context() as patch:
        patch.setattr(QDialog, "exec", cancel)
        screen.aa_key.click()

    screen.refresh_button.click()
    qtbot.waitUntil(lambda: "last checked" in screen.aa_status.text())
    assert calls == ["fixture-key"]
    assert screen.aa_matrix.isEnabled()
    assert "Artificial Analysis model reference" in _texts(screen)

    def matrix(dialog: QDialog) -> int:
        table = dialog.findChild(QTableWidget)
        assert table is not None

        def cell(row: int, column: int) -> str:
            item = table.item(row, column)
            assert item is not None
            return item.text()

        assert table.rowCount() == 10
        assert cell(0, 2) == "low"
        assert cell(0, 3) == cell(0, 4) == ""
        assert cell(1, 1) == "Claude Opus 5.5 · Default Fallback"
        assert cell(1, 2) == "medium (preferred)"
        assert cell(1, 3) == "55"
        assert cell(1, 4) == "1.2500"
        assert cell(5, 0) == "claude:sonnet"
        assert cell(5, 2) == "low (preferred)"
        assert cell(5, 3) == cell(5, 4) == ""
        return QDialog.DialogCode.Rejected

    with monkeypatch.context() as patch:
        patch.setattr(QDialog, "exec", matrix)
        screen.aa_matrix.click()
    screen.aa_remove.click()
    qtbot.waitUntil(screen.aa_matrix.isHidden)
    assert screen.aa_key.text() == "Add API key"
    assert calls == ["fixture-key"]


def test_explicit_index_only_refresh_retains_due_data_and_ignores_double_click(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    no_refresh: list[object],
) -> None:
    _connections(env, "codex")
    _catalogue(env, "codex", models.Model("gpt-6.3", "GPT-6.3", ("high",)))
    _index(env, monkeypatch, _row("GPT-6.3", "high", 64.8, 0.8))
    original_time = time.time()
    monkeypatch.setattr(
        benchmarks,
        "time",
        SimpleNamespace(
            time=lambda: original_time + benchmarks.MAX_CHECK_AGE + 60,
            strftime=time.strftime,
            gmtime=time.gmtime,
        ),
    )
    screen = _screen(env, qtbot)
    assert "refresh due" in screen.aa_status.text()
    assert "1. GPT-6.3" in _texts(screen)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def fail(key: str) -> tuple[str, list[Observation]]:
        calls.append(key)
        entered.set()
        assert release.wait(5)
        raise SourceError("fixture Index error")

    monkeypatch.setattr(benchmarks, "read_artificial_analysis", fail)
    try:
        screen.refresh_button.click()
        qtbot.waitUntil(entered.is_set)
        assert screen.refresh_status.text() == "Refreshing benchmarks…"
        assert not screen.refresh_button.isEnabled()
        screen.refresh_button.click()
    finally:
        release.set()
    qtbot.waitUntil(lambda: "fixture Index error" in screen.aa_status.text())
    assert "retained" in screen.aa_status.text()
    assert "refresh due" in screen.aa_status.text()
    assert "Kept the saved" in screen.refresh_status.text()
    assert "1. GPT-6.3" in _texts(screen)
    assert calls == ["fixture-key"]
    assert no_refresh == []


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_chart_and_table_render_at_minimum_width_with_zero_and_ties(
    env: dict[str, str],
    qtbot: QtBot,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    no_refresh: list[object],
    mode: str,
) -> None:
    from .test_common import assert_screen_fits  # pyrefly: ignore[missing-import]

    previous_mode = theme.current_mode()
    theme.apply(qapp, mode)
    try:
        _connections(env, "codex")
        _catalogue(
            env,
            "codex",
            models.Model("gpt-6-alpha", "Alpha", ("high", "max")),
            models.Model("gpt-6-beta", "Beta", ("max",)),
        )
        choices.set_settings({choices.BENCHMARK_SCORE_MARGIN: "0"}, env=env)
        screen = _screen(env, qtbot)
        screen.resize(576, 480)
        screen.show()
        assert not screen.chart.isVisible()
        assert screen.chart.report is not None and not screen.chart.report["points"]
        assert not screen.chart.grab().isNull()
        _index(
            env,
            monkeypatch,
            _row("GPT-6 Alpha", "high", 0, 0),
            _row("GPT-6 Alpha", "max", 0, 0),
            _row("GPT-6 Beta", "max", 0, 0),
        )
        qtbot.waitUntil(screen.chart.isVisible)
        qtbot.waitUntil(lambda: assert_screen_fits(screen))
        image = screen.chart.grab().toImage()
        assert not image.isNull()
        assert image.pixelColor(image.width() // 2, image.height() // 3) == theme.color(
            "bg-accent"
        )
        assert "1. GPT-6 Alpha" in _texts(screen)
        assert "2. GPT-6 Alpha" in _texts(screen)
        assert "3. GPT-6 Beta" in _texts(screen)
        assert screen.rank_status.text() == "Best comparable score 0 · Cutoff 0"
    finally:
        theme.apply(qapp, previous_mode)
    assert no_refresh == []


def test_recommendation_badges_separate_three_points_at_the_same_corner() -> None:
    plot = QRectF(40, 20, 320, 190)
    badges: list[QPointF] = []
    for _ in range(3):
        badge = _badge_position(plot.topLeft(), plot, badges)
        assert plot.adjusted(9, 9, -9, -9).contains(badge)
        assert all(
            abs(badge.x() - other.x()) >= 18 or abs(badge.y() - other.y()) >= 18
            for other in badges
        )
        badges.append(badge)


def test_benchmarks_navigation_and_keyboard_order(
    env: dict[str, str],
    qtbot: QtBot,
    qapp: QApplication,
    comparable: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(env, qtbot)
    window = MainWindow(context, has_tray=False, quit_app=lambda: None)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    with qtbot.waitActive(window):
        window.activateWindow()
    window.nav["menu"].click()
    team = window.screen_widget("menu")
    assert isinstance(team, MenuScreen)
    team.view_benchmarks.click()
    screen = window.screen_widget("benchmarks")
    assert isinstance(screen, BenchmarksScreen)
    _wait_for_report(screen, qtbot)
    assert window.current_key() == "benchmarks"

    widgets.install_focus_tracking(qapp)
    screen.margin.setFocus(Qt.FocusReason.TabFocusReason)
    assert widgets.focus_visible(screen.margin)
    steps: list[QWidget] = [
        screen._lead_rows[1].info,
        screen.lead_apps["all"],
        screen.lead_apps["codex"],
        screen.lead_apps["opencode"],
        screen._lead_rows[2].info,
        screen.one_per_model,
        screen.aa_key,
    ]
    for target in steps:
        qtbot.keyClick(window, Qt.Key.Key_Tab)
        assert qapp.focusWidget() is target

    window.nav["menu"].click()
    window.nav["benchmarks"].click()
    assert window.stack.currentWidget() is screen
    _connections(env)
    qtbot.waitUntil(lambda: screen.open_engines.isVisible())
    # Engines' own connection check is outside this navigation test.
    monkeypatch.setattr(context, "refresh_engines", lambda: None)
    screen.open_engines.click()
    assert window.current_key() == "engines"
    assert comparable == []
