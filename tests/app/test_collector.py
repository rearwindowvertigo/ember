import gc
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from ember.app.collector import Collector


@pytest.fixture
def collector(qapp: QApplication) -> Iterator[Collector]:
    """Stop the collector and restore the caller's GC state after each test."""
    enabled = gc.isenabled()
    instance = Collector(qapp)
    try:
        yield instance
    finally:
        instance.stop()
        instance.deleteLater()
        if enabled:
            gc.enable()
        else:
            gc.disable()


def test_disables_automatic_collection(collector: Collector) -> None:
    """The active timer replaces automatic collection."""
    assert not gc.isenabled()
    assert collector.timer.isActive()
    assert collector.timer.interval() == 500


@pytest.mark.parametrize("count", [9, 10, 11])
def test_timer_collects_only_above_first_threshold(
    collector: Collector,
    count: int,
    qtbot: QtBot,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the first count triggers a full collection, on the GUI thread."""
    threads: list[QThread] = []
    monkeypatch.setattr(gc, "get_count", lambda: (count, 1000, 1000))
    monkeypatch.setattr(gc, "get_threshold", lambda: (10, 1, 1))
    monkeypatch.setattr(gc, "collect", lambda: threads.append(QThread.currentThread()))
    with qtbot.waitSignal(collector.timer.timeout):
        pass
    assert threads == ([qapp.thread()] if count > 10 else [])


@pytest.mark.parametrize("enabled", [False, True])
def test_stop_restores_previous_state(qapp: QApplication, enabled: bool) -> None:
    """Stopping restores enabled or disabled GC and is idempotent."""
    original = gc.isenabled()
    instance = None
    try:
        if enabled:
            gc.enable()
        else:
            gc.disable()
        instance = Collector(qapp)
        instance.stop()
        instance.stop()
        assert gc.isenabled() == enabled
        assert not instance.timer.isActive()
    finally:
        if instance is not None:
            instance.stop()
            instance.deleteLater()
        if original:
            gc.enable()
        else:
            gc.disable()


def test_app_tests_run_without_automatic_collection() -> None:
    """The app test fixture keeps collection on the GUI thread, as the app does."""
    assert not gc.isenabled()
