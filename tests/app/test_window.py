import gc
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, Qt, QThreadPool
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from ember import deps
from ember.app import state
from ember.app.context import STALE_TITLE, AppContext
from ember.app.screens import KEYS, SCREENS
from ember.app.window import MINIMUM_HEIGHT, MINIMUM_WIDTH, MainWindow, app_version

from .test_runs import make_run, write_run  # pyrefly: ignore[missing-import]


@pytest.fixture(autouse=True)
def _collect_on_gui_thread() -> Iterator[None]:
    # Qt objects left in reference cycles must be freed on the GUI thread,
    # not by a garbage collection that happens to run on a pool thread.
    yield
    QThreadPool.globalInstance().waitForDone(10_000)
    QCoreApplication.processEvents()
    gc.collect()


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
        "Menu",
        "Engines",
        "Dependencies",
        "Settings",
    ]


def test_default_size_and_minimum(env: dict[str, str], qtbot: QtBot) -> None:
    window, _context, _quit = _window(env, qtbot)
    assert (window.width(), window.height()) == (1100, 720)
    assert window.minimumSize().width() == MINIMUM_WIDTH
    assert window.minimumSize().height() == MINIMUM_HEIGHT
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
