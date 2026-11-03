import time
from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from pytestqt.qtbot import QtBot

from ember.app import flows, theme
from ember.app import tray as tray_module
from ember.app.context import AppContext
from ember.app.tray import Tray
from ember.flows import record as flow_record
from ember.panels import record as panel_record
from ember.runs import record

from .test_flows import FLOW, PANEL, make_flow, make_panel  # pyrefly: ignore[missing-import]
from .test_runs import make_run, write_run  # pyrefly: ignore[missing-import]


def _tray(env: dict[str, str], qtbot: QtBot) -> tuple[Tray, AppContext, list[str]]:
    context = AppContext(env)
    context.flows._loaded_records(flows.read(env))
    calls: list[str] = []
    tray = Tray(
        context,
        lambda: calls.append("open"),
        lambda: calls.append("overview"),
        lambda: calls.append("quit"),
    )
    context.runs.start()
    qtbot.waitUntil(context.runs.loaded)
    tray.menu.aboutToShow.emit()
    return tray, context, calls


def _hhmm(stamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(stamp))


def test_status_lines() -> None:
    assert tray_module.status_line(0) == "No runs live"
    assert tray_module.status_line(1) == "1 run live"
    assert tray_module.status_line(2) == "2 runs live"
    assert tray_module.tooltip(2) == "Ember · 2 runs live"
    assert tray_module.tooltip(0) == "Ember · No runs live"


def test_menu_is_populated_at_construction(env: dict[str, str]) -> None:
    context = AppContext(env)
    context.flows._loaded_records(flows.read(env))
    calls: list[str] = []
    tray = Tray(
        context,
        lambda: calls.append("open"),
        lambda: calls.append("overview"),
        lambda: calls.append("quit"),
    )

    assert tray.lines() == ["No runs live", "-", "Open Ember", "Quit Ember"]
    actions = tray.menu.actions()
    assert not actions[0].isEnabled()
    actions[2].trigger()
    actions[3].trigger()
    assert calls == ["open", "quit"]


def test_menu_with_no_runs_live(env: dict[str, str], qtbot: QtBot) -> None:
    write_run(make_run(env, status=record.CLOSED, ended_at=1_800_000_100.0))
    tray, context, _calls = _tray(env, qtbot)
    assert tray.lines() == ["No runs live", "-", "Open Ember", "Quit Ember"]
    assert not tray.menu.actions()[0].isEnabled()
    assert tray.icon.toolTip() == "Ember · No runs live"
    assert not tray.icon.icon().isNull()
    context.runs.stop()


def test_menu_with_one_and_two_runs_live(env: dict[str, str], qtbot: QtBot) -> None:
    first = write_run(make_run(env, repo="/home/owner/alpha"))
    tray, context, _calls = _tray(env, qtbot)
    assert tray.lines() == [
        "1 run live",
        f"Codex · alpha · {_hhmm(first.started_at)}",
        "-",
        "Open Ember",
        "Quit Ember",
    ]
    second = make_run(
        env,
        "claude-20260101-130000-bbbbbb",
        engine="claude",
        repo="/home/owner/beta",
        status=record.WAITING,
        started_at=first.started_at + 3600,
    )
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        write_run(second)
    tray.menu.aboutToHide.emit()
    tray.menu.aboutToShow.emit()
    assert tray.lines() == [
        "2 runs live",
        f"Codex · alpha · {_hhmm(first.started_at)}",
        f"Claude · beta · {_hhmm(second.started_at)}",
        "-",
        "Open Ember",
        "Quit Ember",
    ]
    assert tray.icon.toolTip() == "Ember · 2 runs live"
    context.runs.stop()


def test_actions(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = write_run(make_run(env))
    tray, context, calls = _tray(env, qtbot)
    opened: list[str] = []
    monkeypatch.setattr(context, "open_run_terminal", opened.append)
    actions = tray.menu.actions()
    actions[1].trigger()
    assert opened == [run.id]
    actions[3].trigger()
    actions[4].trigger()
    assert calls == ["open", "quit"]
    tray._activated(QSystemTrayIcon.ActivationReason.Trigger)
    tray._activated(QSystemTrayIcon.ActivationReason.Context)
    assert calls == ["open", "quit", "open"]
    context.runs.stop()


def test_menu_is_not_rebuilt_while_open(env: dict[str, str], qtbot: QtBot) -> None:
    first = write_run(make_run(env, repo="/home/owner/alpha"))
    tray, context, _calls = _tray(env, qtbot)
    tray.menu.aboutToShow.emit()
    shown = tray.menu.actions()
    second = make_run(
        env,
        "claude-20260101-130000-bbbbbb",
        engine="claude",
        repo="/home/owner/beta",
        started_at=first.started_at + 3600,
    )
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        write_run(second)
    qtbot.waitUntil(lambda: tray.icon.toolTip() == "Ember · 2 runs live")
    assert tray.menu.actions() == shown
    assert tray.lines()[0] == "1 run live"
    tray.menu.aboutToHide.emit()
    QCoreApplication.processEvents()
    assert tray.menu.actions() == shown
    tray.menu.aboutToShow.emit()
    assert tray.lines()[0] == "2 runs live"
    assert len(tray.lines()) == 6
    context.runs.stop()


@pytest.mark.parametrize("change_after_hide", [False, True])
def test_stale_menu_actions_run_after_hide(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    change_after_hide: bool,
) -> None:
    run = write_run(make_run(env))
    tray, context, calls = _tray(env, qtbot)
    opened: list[str] = []
    monkeypatch.setattr(context, "open_run_terminal", opened.append)
    shown = tray.menu.actions()
    live_icon = tray.icon.icon().cacheKey()
    if change_after_hide:
        tray.menu.aboutToHide.emit()
    with qtbot.waitSignal(context.runs.changed, timeout=5000):
        write_run(replace(run, status=record.CLOSED, ended_at=run.started_at + 1))
    assert tray.icon.toolTip() == "Ember · No runs live"
    assert tray.icon.icon().cacheKey() != live_icon
    if not change_after_hide:
        tray.menu.aboutToHide.emit()
    QCoreApplication.processEvents()
    assert tray.menu.actions() == shown
    shown[1].trigger()
    shown[3].trigger()
    shown[4].trigger()
    assert opened == [run.id]
    assert calls == ["open", "quit"]
    tray.menu.aboutToShow.emit()
    assert tray.lines() == ["No runs live", "-", "Open Ember", "Quit Ember"]
    unchanged = tray.menu.actions()
    tray.menu.aboutToHide.emit()
    tray.menu.aboutToShow.emit()
    assert tray.menu.actions() == unchanged
    context.runs.stop()


@pytest.mark.parametrize(
    ("runs", "flow_count", "panels", "expected"),
    [
        (1, 1, 0, "1 run, 1 flow live"),
        (0, 2, 1, "2 flows, 1 panel live"),
        (2, 0, 2, "2 runs, 2 panels live"),
        (0, 0, 1, "1 panel live"),
        (1, 1, 1, "1 run, 1 flow, 1 panel live"),
    ],
)
def test_mixed_status_lines(
    runs: int, flow_count: int, panels: int, expected: str
) -> None:
    assert tray_module.status_line(runs, flow_count, panels) == expected
    assert tray_module.tooltip(runs, flow_count, panels) == f"Ember · {expected}"


@pytest.mark.parametrize("kind", ["flow", "panel"])
@pytest.mark.parametrize("length", [39, 40, 41])
def test_parent_line_uses_elided_record_id(kind: str, length: int) -> None:
    data = {"id": "x" * length, "started_at": 100}
    item = (
        flows.FlowItem(data, None, False)
        if kind == "flow"
        else flows.PanelItem(data, "Question", None, False)
    )
    ident = "x" * length if length <= 40 else "x" * 39 + "…"
    assert tray_module.parent_line(item) == f"{kind.title()} · {ident} · {_hhmm(100)}"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_parents_and_nested_workers(
    env: dict[str, str],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
    mode: str,
) -> None:
    panel = make_panel(env, started_at=10)
    flow = make_flow(env, status="stopped", started_at=30)
    panel_run = write_run(make_run(env, started_at=50))
    flow_run = write_run(
        make_run(env, "claude-20260101-130000-bbbbbb", engine="claude", started_at=40)
    )
    standalone = write_run(make_run(env, "codex-20260101-130000-cccccc", started_at=20))
    closed = write_run(
        make_run(env, "codex-20260101-130000-dddddd", status=record.CLOSED, ended_at=60)
    )
    panel["members"][0]["run"] = panel_run.id
    panel_record.save(panel, env)
    flow["tasks"][0]["writer_run"] = flow_run.id
    flow["tasks"][1]["writer_run"] = closed.id
    flow_record.save(flow, env)
    tray, context, calls = _tray(env, qtbot)
    previous = theme.current_mode()
    try:
        theme.apply(qapp, mode)
        assert tray.lines() == [
            "1 run, 1 flow, 1 panel live",
            f"Panel · {PANEL} · {_hhmm(10)}",
            "    " + tray_module.run_line(panel_run),
            tray_module.run_line(standalone),
            f"Flow · {FLOW} · {_hhmm(30)}",
            "    " + tray_module.run_line(flow_run),
            "-",
            "Open Ember",
            "Quit Ember",
        ]
        assert tray.icon.toolTip() == "Ember · 1 run, 1 flow, 1 panel live"
        opened: list[str] = []
        monkeypatch.setattr(context, "open_run_terminal", opened.append)
        for action in tray.menu.actions()[1:6]:
            action.trigger()
        assert opened == [panel_run.id, standalone.id, flow_run.id]
        assert calls == ["overview", "overview"]
    finally:
        context.runs.stop()
        theme.apply(qapp, previous)
        qapp.setStyleSheet("")


@pytest.mark.parametrize("status", ["starting", "running", "checking", "stopped"])
def test_live_flows_without_workers(
    env: dict[str, str], qtbot: QtBot, status: str
) -> None:
    tray, context, _calls = _tray(env, qtbot)
    idle_icon = tray.icon.icon().cacheKey()
    make_flow(env, status=status)
    context.flows._loaded_records(flows.read(env))
    try:
        assert tray.icon.toolTip() == "Ember · 1 flow live"
        assert tray.icon.icon().cacheKey() != idle_icon
        assert context.flows.flows()[0].gone == (status != "stopped")
        tray.menu.aboutToShow.emit()
        assert tray.lines()[1] == f"Flow · {FLOW} · {_hhmm(100)}"
    finally:
        context.runs.stop()


@pytest.mark.parametrize("kind", ["flow", "panel"])
@pytest.mark.parametrize("change_after_hide", [False, True])
def test_parent_change_preserves_stale_actions(
    env: dict[str, str], qtbot: QtBot, kind: str, change_after_hide: bool
) -> None:
    data = make_flow(env) if kind == "flow" else make_panel(env)
    tray, context, calls = _tray(env, qtbot)
    shown = tray.menu.actions()
    live_icon = tray.icon.icon().cacheKey()
    try:
        assert tray.icon.toolTip() == f"Ember · 1 {kind} live"
        if kind == "panel":
            assert context.flows.panels()[0].gone
        if change_after_hide:
            tray.menu.aboutToHide.emit()
        data.update(status="done", ended_at=200, updated_at=200)
        (flow_record if kind == "flow" else panel_record).save(data, env)
        context.flows._loaded_records(flows.read(env))
        assert tray.icon.toolTip() == "Ember · No runs live"
        assert tray.icon.icon().cacheKey() != live_icon
        if not change_after_hide:
            tray.menu.aboutToHide.emit()
        QCoreApplication.processEvents()
        assert tray.menu.actions() == shown
        shown[1].trigger()
        shown[-2].trigger()
        shown[-1].trigger()
        assert calls == ["overview", "open", "quit"]
        tray.menu.aboutToShow.emit()
        assert tray.lines() == ["No runs live", "-", "Open Ember", "Quit Ember"]
    finally:
        context.runs.stop()


@pytest.mark.parametrize("kind", ["flow", "panel"])
def test_finished_parent_leaves_live_worker_at_tray_top_level(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from ember.app.screens.overview import item_id, sections
    from ember.app.window import MainWindow

    worker = write_run(make_run(env))
    if kind == "flow":
        data = make_flow(env, status="cancelled", ended_at=200)
        data["tasks"][0]["writer_run"] = worker.id
        flow_record.save(data, env)
    else:
        data = make_panel(env, status="incomplete", ended_at=200)
        data["members"][0]["run"] = worker.id
        panel_record.save(data, env)
    tray, context, _calls = _tray(env, qtbot)
    try:
        live, finished, children = sections(
            context.runs.runs(), [*context.flows.flows(), *context.flows.panels()]
        )
        assert live == []
        assert [item_id(item) for item in finished] == [data["id"]]
        assert children[data["id"]] == [worker]
        assert tray.lines() == [
            "1 run live",
            tray_module.run_line(worker),
            "-",
            "Open Ember",
            "Quit Ember",
        ]
        assert tray.icon.toolTip() == "Ember · 1 run live"
        assert tray._live
        opened: list[str] = []
        monkeypatch.setattr(context, "open_run_terminal", opened.append)
        tray.menu.actions()[1].trigger()
        assert opened == [worker.id]
        window = MainWindow(context, has_tray=True, quit_app=lambda: None)
        qtbot.addWidget(window)
        window.update_counts()
        assert window.nav["overview"].count() == 1
    finally:
        context.runs.stop()


@pytest.mark.parametrize("ended", [False, True])
def test_shared_workers_follow_oldest_parent_and_sort_by_start_and_id(
    env: dict[str, str], ended: bool
) -> None:
    first = make_run(env, started_at=30)
    second = make_run(env, "codex-20260101-130000-bbbbbb", started_at=30)
    third = make_run(env, "codex-20260101-130000-cccccc", started_at=40)
    run_ids = frozenset([first.id, second.id, third.id])
    flow = flows.FlowItem(
        {"id": FLOW, "started_at": 20, "status": "cancelled" if ended else "stopped"},
        None,
        False,
        run_ids,
    )
    panel = flows.PanelItem(
        {"id": PANEL, "started_at": 20, "status": "running"},
        "",
        None,
        False,
        run_ids,
    )
    items, children = tray_module.live_items([third, second, first], [panel, flow])
    assert children[PANEL] == []
    if ended:
        assert items == [panel, first, second, third]
        assert FLOW not in children
    else:
        assert items == [flow, panel]
        assert children[FLOW] == [first, second, third]


def test_flow_ending_promotes_worker_without_losing_stale_terminal_action(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = write_run(make_run(env))
    flow = make_flow(env, status="running")
    flow["tasks"][0]["writer_run"] = worker.id
    flow_record.save(flow, env)
    tray, context, calls = _tray(env, qtbot)
    try:
        assert tray.lines()[0] == "1 flow live"
        assert tray.lines()[2] == "    " + tray_module.run_line(worker)
        shown = tray.menu.actions()
        flow.update(status="cancelled", ended_at=200, updated_at=200)
        flow_record.save(flow, env)
        context.flows._loaded_records(flows.read(env))
        assert tray.icon.toolTip() == "Ember · 1 run live"
        tray.menu.aboutToHide.emit()
        QCoreApplication.processEvents()
        opened: list[str] = []
        monkeypatch.setattr(context, "open_run_terminal", opened.append)
        shown[1].trigger()
        shown[2].trigger()
        assert calls == ["overview"]
        assert opened == [worker.id]
        tray.menu.aboutToShow.emit()
        assert tray.lines()[1] == tray_module.run_line(worker)
        assert len(tray.menu.actions()) == 5
    finally:
        context.runs.stop()
