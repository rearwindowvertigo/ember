import gc
import threading
import weakref

from PySide6.QtCore import QThread
from pytestqt.qtbot import QtBot

from ember.app.tasks import TaskRunner


def test_delivers_the_result_on_the_gui_thread(qtbot: QtBot) -> None:
    runner = TaskRunner()
    gui = QThread.currentThread()
    worker: list[int] = []
    delivered: list[tuple[object, bool]] = []

    def work() -> str:
        worker.append(threading.get_ident())
        return "done"

    runner.run(
        work,
        lambda result: delivered.append((result, QThread.currentThread() is gui)),
        lambda error: delivered.append((error, False)),
    )
    qtbot.waitUntil(lambda: bool(delivered))
    assert delivered == [("done", True)]
    assert worker[0] != threading.get_ident()
    assert runner.pending() == 0


def test_reports_exceptions_on_the_gui_thread(qtbot: QtBot) -> None:
    runner = TaskRunner()
    gui = QThread.currentThread()
    failures: list[tuple[Exception, bool]] = []

    def work() -> None:
        raise OSError("disk gone")

    runner.run(
        work,
        lambda _result: None,
        lambda error: failures.append((error, QThread.currentThread() is gui)),
    )
    qtbot.waitUntil(lambda: bool(failures))
    error, on_gui = failures[0]
    assert isinstance(error, OSError)
    assert str(error) == "disk gone"
    assert on_gui


def test_many_tasks_each_deliver_once(qtbot: QtBot) -> None:
    runner = TaskRunner()
    results: list[int] = []
    for index in range(20):
        runner.run(lambda index=index: index * 2, results.append, lambda _e: None)
    qtbot.waitUntil(lambda: len(results) == 20)
    assert sorted(results) == [index * 2 for index in range(20)]
    assert runner.wait(1000)
    assert runner.pending() == 0


def test_callbacks_and_work_are_released_on_the_gui_thread(qtbot: QtBot) -> None:
    runner = TaskRunner()
    gui = threading.get_ident()
    released: list[int] = []
    delivered: list[int] = []

    class Held:
        pass

    for index in range(50):
        work_held, done_held, failed_held = Held(), Held(), Held()
        for held in (work_held, done_held, failed_held):
            weakref.finalize(held, lambda: released.append(threading.get_ident()))

        def work(held: Held = work_held, index: int = index) -> int:
            assert held
            return index

        def done(result: int, held: Held = done_held) -> None:
            assert held
            delivered.append(result)

        def failed(_error: Exception, held: Held = failed_held) -> None:
            assert held

        runner.run(work, done, failed)
        del work, done, failed, work_held, done_held, failed_held, held
    qtbot.waitUntil(lambda: len(delivered) == 50)
    assert runner.wait(1000)
    gc.collect()
    assert len(released) == 150
    assert set(released) == {gui}
