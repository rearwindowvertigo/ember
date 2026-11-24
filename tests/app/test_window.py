import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from PySide6.QtCore import QCoreApplication, QPoint, QProcessEnvironment, Qt
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from ember import deps
from ember.app import flows, icons, state, theme
from ember.app import window as window_module
from ember.app.context import STALE_TITLE, AppContext
from ember.app.screens import KEYS, SCREENS
from ember.app.updater import UpdateNotice
from ember.app.window import MINIMUM_HEIGHT, MINIMUM_WIDTH, MainWindow, app_version
from ember.flows import record as flow_record
from ember.panels import record as panel_record

from .test_flows import make_flow, make_panel  # pyrefly: ignore[missing-import]
from .test_runs import make_run, write_run  # pyrefly: ignore[missing-import]


class _Quit:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _window(
    env: dict[str, str], qtbot: QtBot, *, has_tray: bool = True, ready: bool = True
) -> tuple[MainWindow, AppContext, _Quit]:
    context = AppContext(env)
    quit_app = _Quit()
    window = MainWindow(context, has_tray=has_tray, quit_app=quit_app)
    qtbot.addWidget(window)
    if ready:
        with qtbot.waitSignal(context.settings_changed, timeout=5000):
            context.reload_settings()
    return window, context, quit_app


def _component(status: str, index: int) -> deps.Component:
    return deps.Component(
        id=f"c{index}",
        label=f"C{index}",
        group="system",
        status=status,
        installed=None,
        newest=None,
        error=None,
        command=None,
        command_kind=None,
        note=None,
    )


def test_screens_list_in_sidebar_order() -> None:
    assert KEYS == ("overview", "menu", "engines", "dependencies", "settings")
    assert [title for _k, title, _i, _c in SCREENS] == [
        "Overview",
        "Team",
        "Engines",
        "Dependencies",
        "Settings",
    ]


def test_default_size_and_minimum(env: dict[str, str], qtbot: QtBot) -> None:
    window, _context, _quit = _window(env, qtbot)
    assert (window.width(), window.height()) == (1100, 720)
    assert window.minimumSize().width() == MINIMUM_WIDTH
    assert window.minimumSize().height() == MINIMUM_HEIGHT
    assert (MINIMUM_WIDTH, MINIMUM_HEIGHT) == (640, 480)
    assert window.current_key() == "overview"
    assert window.version_label.text() == f"Ember {app_version()}"


def test_size_and_screen_restored(env: dict[str, str], qtbot: QtBot) -> None:
    state.save(state.AppState(1300, 800, "engines"), env)
    window, _context, _quit = _window(env, qtbot)
    assert (window.width(), window.height()) == (1300, 800)
    assert window.current_key() == "engines"
    assert window.stack.currentWidget() is window.screen_widget("engines")


def test_saved_size_below_minimum_and_unknown_screen(
    env: dict[str, str], qtbot: QtBot
) -> None:
    state.save(state.AppState(500, 300, "nowhere"), env)
    window, _context, _quit = _window(env, qtbot)
    assert window.width() >= MINIMUM_WIDTH
    assert window.height() >= MINIMUM_HEIGHT
    assert window.current_key() == "overview"


def test_screens_wait_for_settings(env: dict[str, str], qtbot: QtBot) -> None:
    window, context, _quit = _window(env, qtbot, ready=False)
    assert window.stack.currentWidget() is window.state_view
    window.show_screen("menu")
    assert window.screen_widget("menu") is None
    assert window.current_key() == "menu"
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        context.reload_settings()
    assert window.stack.currentWidget() is window.screen_widget("menu")


def test_settings_error_shows_try_again(env: dict[str, str], qtbot: QtBot) -> None:
    config = Path(env["XDG_CONFIG_HOME"]) / "ember"
    config.mkdir(parents=True)
    (config / "settings.db").write_text("not a database")
    window, context, _quit = _window(env, qtbot, ready=False)
    with qtbot.waitSignal(context.settings_failed, timeout=5000):
        context.reload_settings()
    assert "could not read its settings" in window.state_view.error_text.text()
    (config / "settings.db").unlink()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        window.state_view.retry_button.click()
    assert window.stack.currentWidget() is window.screen_widget("overview")


def test_shortcuts_switch_screens_and_ctrl_w_hides(
    env: dict[str, str], qtbot: QtBot
) -> None:
    window, context, quit_app = _window(env, qtbot)
    window.show()
    qtbot.waitExposed(window)
    with qtbot.waitActive(window):
        window.activateWindow()
    for index, key in enumerate(KEYS, start=1):
        qtbot.keyClick(
            window.windowHandle(),
            getattr(Qt.Key, f"Key_{index}"),
            Qt.KeyboardModifier.ControlModifier,
        )
        assert window.current_key() == key
        assert window.nav[key].isChecked()
        assert window.stack.currentWidget() is window.screen_widget(key)
    assert context.visible()
    qtbot.keyClick(
        window.windowHandle(), Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier
    )
    assert not window.isVisible()
    assert not context.visible()
    assert quit_app.calls == 0
    assert state.load(env).screen == "settings"
    window.show()
    qtbot.keyClick(
        window.windowHandle(), Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier
    )
    assert quit_app.calls == 1


def test_ctrl_w_and_close_quit_without_a_tray(
    env: dict[str, str], qtbot: QtBot
) -> None:
    window, _context, quit_app = _window(env, qtbot, has_tray=False)
    window.show()
    qtbot.waitExposed(window)
    with qtbot.waitActive(window):
        window.activateWindow()
    qtbot.keyClick(
        window.windowHandle(), Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier
    )
    assert quit_app.calls == 1
    window.close()
    assert quit_app.calls == 2


def test_close_hides_with_a_tray(env: dict[str, str], qtbot: QtBot) -> None:
    window, _context, quit_app = _window(env, qtbot)
    window.show()
    qtbot.waitExposed(window)
    window.resize(1200, 750)
    window.close()
    assert not window.isVisible()
    assert quit_app.calls == 0
    saved = state.load(env)
    assert (saved.width, saved.height) == (1200, 750)


def test_screen_shown_is_called(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _context, _quit = _window(env, qtbot)
    window.show()
    qtbot.waitExposed(window)
    calls: list[str] = []
    window.show_screen("engines")
    screen = window.screen_widget("engines")
    assert isinstance(screen, QWidget)
    monkeypatch.setattr(screen, "shown", lambda: calls.append("engines"))
    window.show_screen("menu")
    window.show_screen("engines")
    assert calls == ["engines"]


def test_counts_shown_only_above_zero(env: dict[str, str], qtbot: QtBot) -> None:
    window, context, _quit = _window(env, qtbot)
    assert window.nav["overview"].count() is None
    assert window.nav["dependencies"].count() is None
    write_run(make_run(env))
    write_run(make_run(env, "codex-20260101-130000-bbbbbb", status="closed"))
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        context.runs.start()
    assert window.nav["overview"].count() == 1
    report = deps.DepsReport(
        components=(
            _component(deps.UPDATE_AVAILABLE, 1),
            _component(deps.MISSING, 2),
            _component(deps.CURRENT, 3),
            _component(deps.CHECK_FAILED, 4),
        ),
        installed_read_at={},
        newest_read_at={},
    )
    context._deps = report
    context.deps_changed.emit(report)
    assert window.nav["dependencies"].count() == 2
    empty = deps.DepsReport(components=(), installed_read_at={}, newest_read_at={})
    context._deps = empty
    context.deps_changed.emit(empty)
    assert window.nav["dependencies"].count() is None
    context.runs.stop()


@pytest.mark.parametrize(
    ("kind", "status", "count"),
    [
        ("flow", "starting", 1),
        ("flow", "running", 1),
        ("flow", "checking", 1),
        ("flow", "stopped", 1),
        ("flow", "done", None),
        ("flow", "failed", None),
        ("flow", "cancelled", None),
        ("panel", "running", 1),
        ("panel", "done", None),
        ("panel", "incomplete", None),
    ],
)
@pytest.mark.parametrize("now", [100, 1000])
def test_counts_parents_without_workers_on_another_screen(
    env: dict[str, str],
    qtbot: QtBot,
    tmp_path: Path,
    kind: str,
    status: str,
    count: int | None,
    now: float,
) -> None:
    state.save(state.AppState(1100, 720, "menu"), env)
    window, context, _quit = _window(env, qtbot)
    assert window.screen_widget("overview") is None
    assert window.nav["overview"].count() is None
    assert window.nav["overview"].accessibleDescription() == ""
    (make_flow if kind == "flow" else make_panel)(env, status=status)
    proc_locks = tmp_path / "proc-locks"
    proc_locks.write_text("")
    context.flows._loaded_records(flows.read(env, proc_locks=proc_locks, now=now))
    assert window.nav["overview"].count() == count
    assert window.nav["overview"].accessibleDescription() == (
        "" if count is None else f"{count} live"
    )
    assert window.current_key() == "menu"
    assert window.screen_widget("overview") is None


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_count_groups_workers_and_keeps_live_workers_of_finished_parents(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str
) -> None:
    state.save(state.AppState(1100, 720, "menu"), env)
    window, context, _quit = _window(env, qtbot)
    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        workers = [
            make_run(env, f"codex-20260101-120000-{index:06x}", status=status)
            for index, status in enumerate(
                ["starting", "running", "waiting", "running", "closed", "failed"]
            )
        ]
        context.runs._loaded_records(((workers, 0, []), None))
        assert window.nav["overview"].count() == 4
        flow = make_flow(env, status="stopped")
        flow["tasks"][0]["writer_run"] = workers[1].id
        flow["tasks"][1]["writer_run"] = workers[2].id
        flow_record.save(flow, env)
        panel = make_panel(env)
        panel["members"][0]["run"] = workers[3].id
        panel_record.save(panel, env)
        context.flows._loaded_records(flows.read(env))
        assert window.nav["overview"].count() == 3
        assert window.nav["overview"].accessibleDescription() == "3 live"

        flow.update(status="cancelled", ended_at=200, updated_at=200)
        flow_record.save(flow, env)
        context.flows._loaded_records(flows.read(env))
        assert window.nav["overview"].count() == 4
        panel.update(status="incomplete", ended_at=200, updated_at=200)
        panel_record.save(panel, env)
        context.flows._loaded_records(flows.read(env))
        assert window.nav["overview"].count() == 4
        assert window.nav["overview"].accessibleDescription() == "4 live"

        closed = [replace(run, status="closed", ended_at=200) for run in workers]
        context.runs._loaded_records(((closed, 0, []), None))
        assert window.nav["overview"].count() is None
        assert window.nav["overview"].accessibleDescription() == ""
    finally:
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


def test_stale_and_error_notices(env: dict[str, str], qtbot: QtBot) -> None:
    window, context, _quit = _window(env, qtbot)
    context.stale.emit()
    assert window.notice is not None
    assert window.notice.tone() == "info"
    assert window.notice.title_label.text() == STALE_TITLE
    context.error.emit("The terminal could not start.")
    assert window.notice.tone() == "danger"
    assert window.notice.title_label.text() == "The terminal could not start."
    window.notice.buttons[0].click()
    assert window.notice is None


def test_screen_requested_switches(env: dict[str, str], qtbot: QtBot) -> None:
    window, context, _quit = _window(env, qtbot)
    context.show_screen("dependencies")
    assert window.current_key() == "dependencies"
    context.show_screen("nowhere")
    assert window.current_key() == "dependencies"


def test_bring_forward_puts_a_token_where_qt_reads_it(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _context, _quit_app = _window(env, qtbot)
    seen: list[str] = []

    def activate() -> None:
        environment = QProcessEnvironment.systemEnvironment()
        seen.append(environment.value("XDG_ACTIVATION_TOKEN"))

    monkeypatch.setattr(window, "activateWindow", activate)
    monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)
    window.bring_forward("dock-token-2")
    assert window.isVisible()
    monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)
    window.bring_forward()
    assert seen == ["dock-token-2", ""]


@pytest.mark.parametrize("shown", [True, False])
@pytest.mark.parametrize("token", ["tray-token", ""])
def test_quit_uses_a_tray_token_without_showing_the_main_window(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    shown: bool,
    token: str,
) -> None:
    """Use the token on a shown window, or on a helper once the desktop shows it."""
    window, _context, quit_app = _window(env, qtbot)
    if shown:
        window.show()
    helper = QWidget()
    qtbot.addWidget(helper)
    calls = Mock()

    def token_now() -> str:
        return QProcessEnvironment.systemEnvironment().value("XDG_ACTIVATION_TOKEN")

    def record(name: str, wrapped: Callable[[], None]) -> Callable[[], None]:
        def recorded() -> None:
            getattr(calls, name)(token_now())
            wrapped()

        return recorded

    def wait_for(condition: Callable[[], bool], msecs: int) -> bool:
        calls.wait(msecs, condition())
        return True

    calls.attach_mock(Mock(return_value=helper), "create")
    calls.attach_mock(Mock(wraps=QCoreApplication.processEvents), "process_events")
    calls.attach_mock(Mock(wraps=quit_app), "quit")
    monkeypatch.setattr(window_module, "QWidget", calls.create)
    monkeypatch.setattr(window_module, "wait_for", wait_for)
    monkeypatch.setattr(window_module, "exposed", lambda widget: widget is helper)
    monkeypatch.setattr(helper, "show", record("show", helper.show))
    monkeypatch.setattr(
        helper, "activateWindow", record("activate_helper", helper.activateWindow)
    )
    monkeypatch.setattr(helper, "hide", record("hide", helper.hide))
    monkeypatch.setattr(
        window, "activateWindow", record("activate_main", window.activateWindow)
    )
    monkeypatch.setattr(QCoreApplication, "processEvents", calls.process_events)
    monkeypatch.setattr(window, "_quit", calls.quit)
    show_main = Mock(wraps=window.show)
    show_normal = Mock(wraps=window.showNormal)
    monkeypatch.setattr(window, "show", show_main)
    monkeypatch.setattr(window, "showNormal", show_normal)
    if token:
        monkeypatch.setenv("XDG_ACTIVATION_TOKEN", token)
    else:
        monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)

    window.quit_app()

    assert quit_app.calls == 1
    show_main.assert_not_called()
    show_normal.assert_not_called()
    assert window.isVisible() == shown
    assert not helper.isVisible()
    if token and not shown:
        assert calls.mock_calls == [
            call.create(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint),
            call.show(""),
            call.wait(window_module.EXPOSE_WAIT_MS, True),
            call.activate_helper(token),
            call.wait(window_module.ACTIVATE_SETTLE_MS, False),
            call.hide(token),
            call.quit(),
        ]
        assert helper.size().width() == helper.size().height() == 1
        assert helper.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert helper.windowOpacity() == 0
    elif token:
        assert calls.mock_calls == [
            call.activate_main(token),
            call.process_events(),
            call.quit(),
        ]
    else:
        assert calls.mock_calls == [call.quit()]


def test_wait_for_returns_when_the_condition_holds_or_time_runs_out(
    qtbot: QtBot,
) -> None:
    checks: list[int] = []

    def third_check() -> bool:
        checks.append(1)
        return len(checks) == 3

    assert window_module.wait_for(third_check, 5_000)
    assert len(checks) == 3
    clock = time.monotonic()
    assert not window_module.wait_for(lambda: False, 50)
    assert 0.04 <= time.monotonic() - clock < 2


def test_exposed_reads_the_native_window(qtbot: QtBot) -> None:
    widget = QWidget()
    qtbot.addWidget(widget)
    assert not window_module.exposed(widget)
    widget.show()
    qtbot.waitUntil(lambda: window_module.exposed(widget))


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_sidebar_collapses_below_960_and_expands_at_960(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str
) -> None:
    theme.apply(qapp, mode)
    window, _context, _quit = _window(env, qtbot)
    window.show()
    assert window.mark.pixmap().toImage() == icons.mark().toImage()
    other = next(name for name in theme.MODES if name != mode)
    theme.apply(qapp, other)
    assert window.mark.pixmap().toImage() == icons.mark().toImage()
    assert window.mark.pixmap().cacheKey() == icons.mark().cacheKey()
    theme.apply(qapp, mode)
    for width, collapsed in [(959, True), (960, False), (640, True), (1100, False)]:
        window.resize(width, 480)
        qtbot.waitUntil(lambda width=width: window.width() == width)
        QCoreApplication.processEvents()
        assert window.height() == 480
        assert window.sidebar.width() == (
            theme.SIDEBAR_COLLAPSED_WIDTH if collapsed else theme.SIDEBAR_WIDTH
        )
        assert window.brand_name.isVisible() is not collapsed
        assert window.version_label.isVisible() is not collapsed
        assert window.mark.toolTip() == (f"Ember {app_version()}" if collapsed else "")
        for item in window.nav.values():
            assert item.text_label.isVisible() is not collapsed
            assert item.accessibleName() == item.title
            assert item.toolTip() == (item.title if collapsed else "")
            if collapsed:
                assert (item.width(), item.height()) == (40, 36)
        mark = window.mark.mapTo(window.sidebar, window.mark.rect().center())
        hide = window.hide_sidebar_button.mapTo(
            window.sidebar, window.hide_sidebar_button.rect().center()
        )
        if collapsed:
            assert abs(mark.x() - hide.x()) <= 1
            assert hide.y() > mark.y()
        else:
            assert abs(mark.y() - hide.y()) <= 1
            assert hide.x() > mark.x()


@pytest.mark.parametrize("mode", theme.MODES)
def test_collapsed_count_pill_and_tooltip(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str
) -> None:
    theme.apply(qapp, mode)
    window, _context, _quit = _window(env, qtbot)
    window.resize(959, 600)
    window.show()
    item = window.nav["overview"]
    QCoreApplication.processEvents()
    for count in (1, 2, 0, -1, 12):
        item.set_count(count, f"{count} live")
        assert item.count_label.isVisible() is (count > 0)
        assert item.toolTip() == (
            f"Overview · {count} live" if count > 0 else "Overview"
        )
        assert item.accessibleName() == "Overview"
        assert item.accessibleDescription() == (f"{count} live" if count > 0 else "")
        if count > 0:
            assert item.count_label.property("role") == "nav-count"
            assert item.count_label.font() == theme.font("caption")
            size = window_module.NAV_COUNT_SIZE
            pill = item.count_label.geometry()
            assert pill.height() == size
            assert item.count_label.minimumWidth() == size
            assert pill.width() >= size
            assert pill.topRight() == item.rect().topRight()
            inside = item.count_label.contentsRect()
            margins = item.count_label.contentsMargins()
            assert min(margins.left(), margins.right()) >= 4
            assert min(margins.top(), margins.bottom()) >= 2
            assert inside.width() >= item.count_label.fontMetrics().horizontalAdvance(
                str(count)
            )
            assert inside.height() >= item.count_label.fontMetrics().height()
    window.resize(960, 600)
    assert item.count() == 12
    QCoreApplication.processEvents()
    assert item.count_label.property("role") == "nav-count"
    assert item.count_label.height() == window_module.NAV_COUNT_SIZE
    assert item.count_label.minimumWidth() == window_module.NAV_COUNT_SIZE
    margins = item.count_label.contentsMargins()
    assert min(margins.left(), margins.right()) >= 4
    assert min(margins.top(), margins.bottom()) >= 2
    pill = item.count_label.geometry()
    assert pill.right() == item.width() - 11
    assert pill.center().y() == item.rect().center().y()
    assert item.count_label.contentsRect().width() >= (
        item.count_label.fontMetrics().horizontalAdvance("12")
    )
    assert item.toolTip() == ""


@pytest.mark.parametrize("mode", theme.MODES)
def test_collapsed_count_ring_matches_row_background(
    env: dict[str, str], qtbot: QtBot, qapp: QApplication, mode: str
) -> None:
    theme.apply(qapp, "dark" if mode == "light" else "light")
    window, _context, _quit = _window(env, qtbot)
    window.resize(959, 600)
    window.show()
    qtbot.waitExposed(window)
    theme.apply(qapp, mode)
    item = window.nav["dependencies"]
    item.set_count(1, "1 to update or install")
    QCoreApplication.processEvents()
    backgrounds = {}
    for selected, hovered in (
        (False, False),
        (False, True),
        (True, True),
        (True, False),
        (False, False),
    ):
        window.nav["dependencies" if selected else "overview"].setChecked(True)
        if hovered:
            qtbot.mouseMove(item, item.rect().center())
        else:
            qtbot.mouseMove(window.sidebar, QPoint(1, 1))
        QCoreApplication.processEvents()
        image = window.sidebar.grab().toImage()
        pill = item.count_label.geometry()
        assert pill.height() == window_module.NAV_COUNT_SIZE
        assert pill.width() >= window_module.NAV_COUNT_SIZE
        assert item.rect().contains(pill)
        origin = item.mapTo(window.sidebar, QPoint(0, 0))
        pill.translate(origin)
        background = image.pixelColor(origin + QPoint(item.width() // 2, 30))
        x = pill.center().x()
        assert image.pixelColor(x, pill.top()) == background
        assert image.pixelColor(x, pill.top() + 1) == background
        assert image.pixelColor(x, pill.bottom()) == background
        assert image.pixelColor(x, pill.bottom() - 1) == background
        assert image.pixelColor(x, pill.bottom() - 2) != background
        for token in ("accent", "on-accent"):
            assert any(
                image.pixelColor(x, y) == theme.color(token)
                for x in range(pill.left(), pill.right() + 1)
                for y in range(pill.top(), pill.bottom() + 1)
            )
        backgrounds[selected, hovered] = background.name()
    assert len(set(backgrounds.values())) == 3
    assert backgrounds[True, True] == backgrounds[True, False]


def test_restart_offer_updates_both_controls(env: dict[str, str], qtbot: QtBot) -> None:
    window, _context, _quit = _window(env, qtbot)
    window.show()
    qtbot.waitExposed(window)
    window.resize(959, 600)
    qtbot.waitUntil(lambda: window.sidebar.width() == theme.SIDEBAR_COLLAPSED_WIDTH)
    first = Mock()
    second = Mock()
    window.show_restart("1.0.0", first)
    compact = window.update_button
    box = window.update_box
    assert compact is not None and box is not None
    assert compact.isVisible()
    assert box.isHidden()
    assert compact.toolTip() == "Ember 1.0.0 is installed. Restart"
    assert compact.accessibleName() == compact.toolTip()
    assert compact.width() == compact.height() == theme.CONTROL_SM
    assert not compact.icon().isNull()
    compact.click()
    first.assert_called_once_with()
    window.show_restart("1.1.0", second)
    assert compact.toolTip() == "Ember 1.1.0 is installed. Restart"
    assert box.label.text() == "Ember 1.1.0 is installed."
    compact.click()
    window.resize(960, 600)
    assert box.isVisible()
    assert compact.isHidden()
    box.restart_button.click()
    assert second.call_count == 2
    window.clear_restart()
    window.resize(959, 600)
    assert box.isHidden() and compact.isHidden()
    compact.click()
    box.restart_button.click()
    assert second.call_count == 2


def test_update_notice_shows_both_versions_and_its_action(
    env: dict[str, str], qtbot: QtBot
) -> None:
    window, _context, _quit = _window(env, qtbot)
    action = Mock()
    window.show_update(
        UpdateNotice(
            "Ember update available", "Installed 1.0.0. Available 2.0.0.", update=action
        )
    )
    window.show_notice("info", "Settings changed")
    notice = window.update_notice
    assert notice is not None
    assert "1.0.0" in notice.text_label.text() and "2.0.0" in notice.text_label.text()
    assert [button.text() for button in notice.buttons] == ["Update", "Dismiss"]
    notice.buttons[0].click()
    action.assert_called_once_with()
    notice.buttons[1].click()
    assert window.update_notice is None
    assert window.notice is not None


@pytest.mark.parametrize("width", [640, 959, 960, 1100])
def test_hide_and_show_from_sidebar_and_each_screen(
    env: dict[str, str], qtbot: QtBot, width: int
) -> None:
    from ember.app import widgets

    window, _context, _quit = _window(env, qtbot)
    window.resize(width, 600)
    window.show()
    qtbot.waitExposed(window)
    with qtbot.waitActive(window):
        window.activateWindow()
    hide = window.hide_sidebar_button
    assert hide.toolTip() == hide.accessibleName() == "Hide sidebar"
    assert not hide.icon().isNull()
    hide.setFocus()
    qtbot.keyClick(hide, Qt.Key.Key_Tab)
    assert window.nav["overview"].hasFocus()
    for key in KEYS:
        hide.click()
        assert window.sidebar.isHidden()
        assert state.load(env).sidebar_hidden is True
        window.show_screen(key)
        screen = window.screen_widget(key)
        assert screen is not None
        show = next(
            button
            for button in screen.findChildren(widgets.IconButton)
            if button.property("role") == "show-sidebar"
        )
        assert show.isVisible()
        assert show.toolTip() == show.accessibleName() == "Show sidebar"
        first = screen.nextInFocusChain()
        assert first is not None
        while not (
            screen.isAncestorOf(first)
            and first.isVisible()
            and first.focusPolicy() & Qt.FocusPolicy.TabFocus
        ):
            first = first.nextInFocusChain()
            assert first is not None
            assert first is not screen
        assert first is show
        show.setFocus()
        qtbot.keyClick(show, Qt.Key.Key_Tab)
        assert QApplication.focusWidget() is not show
        qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_Backtab)
        assert show.hasFocus()
        qtbot.keyClick(show, Qt.Key.Key_Space)
        assert not window.sidebar.isHidden()
        assert state.load(env).sidebar_hidden is False
        assert show.isHidden()
        assert hide.hasFocus()
        assert window.sidebar.width() == (
            theme.SIDEBAR_COLLAPSED_WIDTH if width < 960 else theme.SIDEBAR_WIDTH
        )


def test_hidden_sidebar_restored_in_new_window(
    env: dict[str, str], qtbot: QtBot
) -> None:
    window, _context, _quit = _window(env, qtbot)
    window.resize(800, 600)
    window.show()
    window.hide_sidebar_button.click()
    saved = state.load(env)
    assert (saved.width, saved.height, saved.sidebar_hidden) == (800, 600, True)
    restored, _context, _quit = _window(env, qtbot)
    restored.show()
    assert restored.sidebar.isHidden()
    assert restored.sidebar_collapsed
    assert restored.width() == 800


def test_saved_hidden_sidebar_shown_until_settings_succeed(
    env: dict[str, str], qtbot: QtBot
) -> None:
    state.save(state.AppState(sidebar_hidden=True), env)
    config = Path(env["XDG_CONFIG_HOME"]) / "ember"
    config.mkdir(parents=True)
    database = config / "settings.db"
    database.write_text("not a database")
    window, context, _quit = _window(env, qtbot, ready=False)
    window.show()
    assert window.sidebar.isVisible()
    assert not window.hide_sidebar_button.isEnabled()
    window.hide_sidebar_button.click()
    assert window.sidebar.isVisible()
    with qtbot.waitSignal(context.settings_failed, timeout=5000):
        context.reload_settings()
    assert window.sidebar.isVisible()
    assert not window.hide_sidebar_button.isEnabled()
    assert window.stack.currentWidget() is window.state_view
    assert state.load(env).sidebar_hidden is True
    database.unlink()
    with qtbot.waitSignal(context.settings_changed, timeout=5000):
        window.state_view.retry_button.click()
    assert window.sidebar.isHidden()
    assert window.hide_sidebar_button.isEnabled()
    assert window.stack.currentWidget() is window.screen_widget("overview")


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("width", [640, 1800])
def test_notices_use_capped_column_beside_scrollbar(
    env: dict[str, str], qtbot: QtBot, hidden: bool, width: int
) -> None:
    from PySide6.QtWidgets import QScrollArea

    from ember.app.screens.common import SCREEN_MARGIN

    window, _context, _quit = _window(env, qtbot)
    window.resize(width, 600)
    window.show()
    if hidden:
        window.hide_sidebar_button.click()
    window.show_notice("info", "Settings changed", "Read them again.")
    QCoreApplication.processEvents()
    screen = window.stack.currentWidget()
    assert screen is not None
    area = screen.findChild(QScrollArea)
    assert area is not None
    available = window.content.width() - area.verticalScrollBar().sizeHint().width()
    assert window.notice_column.width() == min(
        available, theme.CONTENT_MAX_WIDTH + 2 * SCREEN_MARGIN
    )
    assert (
        abs(2 * window.notice_column.x() + window.notice_column.width() - available)
        <= 1
    )
    assert window.notice is not None
    assert window.notice.width() <= theme.CONTENT_MAX_WIDTH


def test_sidebar_only_refits_when_resize_crosses_breakpoint(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _context, _quit = _window(env, qtbot)
    window.show()
    qtbot.waitExposed(window)
    fit = Mock(wraps=window._fit_sidebar)
    monkeypatch.setattr(window, "_fit_sidebar", fit)
    for width, height in ((1000, 720), (960, 600)):
        window.resize(width, height)
        QCoreApplication.processEvents()
    fit.assert_not_called()
    window.resize(959, 600)
    QCoreApplication.processEvents()
    fit.assert_called_once_with()
    fit.reset_mock()
    for width, height in ((800, 600), (640, 480), (959, 720)):
        window.resize(width, height)
        QCoreApplication.processEvents()
    fit.assert_not_called()
    window.resize(960, 720)
    QCoreApplication.processEvents()
    fit.assert_called_once_with()


def test_put_first_in_tab_order_keeps_the_other_stops_in_order(
    qtbot: QtBot,
) -> None:
    from PySide6.QtWidgets import QPushButton, QVBoxLayout

    container = QWidget()
    qtbot.addWidget(container)
    layout = QVBoxLayout(container)
    earlier = [QPushButton(f"b{index}") for index in range(3)]
    for item in earlier:
        layout.addWidget(item)
    button = QPushButton("show")
    layout.addWidget(button)
    later = QPushButton("after")
    layout.addWidget(later)
    window_module.put_first_in_tab_order(container, button)
    order = []
    item = container.nextInFocusChain()
    while item is not None and item is not container:
        order.append(item)
        item = item.nextInFocusChain()
    assert order == [button, *earlier, later]
