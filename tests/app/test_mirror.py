import json
import os
import signal
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QPushButton, QScrollArea, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from ember.app import mirror, sgr, theme
from ember.app.mirror import MirrorClient, MirrorView, Screen

RUN_ID = "codex-20260101-120000-abcdef"

STUB = """#!{python}
import json, os, signal, sys

CONFIG = json.loads({config!r})
log = open(CONFIG["log"], "a", buffering=1)
log.write("ARGV " + json.dumps(sys.argv[1:]) + "\\n")
log.write("ENV " + json.dumps(sorted(os.environ)) + "\\n")
state = {{"during": False}}


def out(text):
    os.write(1, (text + "\\n").encode())


def output(*_args):
    out("%output %0 hello\\\\015\\\\012")


def output_and_arm(*_args):
    state["during"] = True
    output()


def leave(*_args):
    out("%exit")
    os._exit(0)


signal.signal(signal.SIGUSR1, output)
signal.signal(signal.SIGUSR2, output_and_arm)
signal.signal(signal.SIGTERM, leave)
if CONFIG["attach_error"]:
    out("%begin 100 1 0")
    out("can't find session: x")
    out("%error 100 1 0")
    out("%exit")
    sys.exit(1)
out("%begin 100 1 0")
out("%end 100 1 0")
out("%session-changed $0 x")
number = 1
for line in sys.stdin:
    log.write("IN " + line)
    number += 1
    body = []
    if line.startswith("capture-pane"):
        if CONFIG["capture_error"]:
            out(f"%begin 100 {{number}} 1")
            out("can't find pane")
            out(f"%error 100 {{number}} 1")
            continue
        if state["during"]:
            state["during"] = False
            output()
        body = CONFIG["screen"]
    elif "pane_id" in line:
        body = ["%0"]
    elif "pane_width" in line:
        body = [CONFIG["size"]]
    out(f"%begin 100 {{number}} 1")
    for item in body:
        out(item)
    out(f"%end 100 {{number}} 1")
log.write("EOF\\n")
"""

SCREEN = ["%end 100 2 1 is text, not a frame", "\x1b[31mred\x1b[0m 漢字"]


def stub_tmux(
    env: dict[str, str],
    *,
    attach_error: bool = False,
    capture_error: bool = False,
    screen: list[str] | None = None,
    size: str = "40 3 2 1 1 0",
) -> Path:
    """Write a stub tmux control client; return its log file."""
    bin_dir = Path(env["PATH"].split(":")[0])
    log = bin_dir.parent / "tmux.log"
    config = {
        "log": str(log),
        "attach_error": attach_error,
        "capture_error": capture_error,
        "screen": SCREEN if screen is None else screen,
        "size": size,
    }
    script = bin_dir / "tmux"
    script.write_text(STUB.format(python=sys.executable, config=json.dumps(config)))
    script.chmod(0o755)
    return log


def log_lines(log: Path, prefix: str) -> list[str]:
    if not log.exists():
        return []
    return [
        line[len(prefix) + 1 :]
        for line in log.read_text().splitlines()
        if line.startswith(prefix + " ")
    ]


class Recorder:
    def __init__(self, client: MirrorClient) -> None:
        self.screens: list[Screen] = []
        self.ended: list[str] = []
        client.updated.connect(self.screens.append)
        client.ended.connect(self.ended.append)


def idle(client: MirrorClient) -> bool:
    return not client._pending and not client._dirty  # noqa: SLF001


def started(env: dict[str, str], qtbot: QtBot) -> tuple[MirrorClient, Recorder]:
    client = MirrorClient(RUN_ID, env)
    recorder = Recorder(client)
    client.start()
    qtbot.waitUntil(lambda: bool(recorder.screens) and idle(client))
    return client, recorder


def finish(client: MirrorClient, qtbot: QtBot) -> None:
    process = client._process  # noqa: SLF001
    client.stop()
    if process is not None:
        qtbot.waitUntil(lambda: process not in mirror._STOPPING, timeout=3000)  # noqa: SLF001


def pid(client: MirrorClient) -> int:
    process = client._process  # noqa: SLF001
    assert process is not None
    return process.processId()


def test_argv_and_empty_environment(
    env: dict[str, str], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_TEST_MARKER", "1")
    log = stub_tmux(env)
    client, _recorder = started(env, qtbot)
    [argv] = log_lines(log, "ARGV")
    args = json.loads(argv)
    assert args[2:] == [
        "-C",
        "attach-session",
        "-f",
        "read-only,ignore-size",
        "-t",
        f"={RUN_ID}",
    ]
    assert args[0] == "-S"
    names = json.loads(log_lines(log, "ENV")[0])
    assert "EMBER_TEST_MARKER" not in names
    assert "HOME" not in names
    assert "PATH" not in names
    finish(client, qtbot)


def test_attach_captures_the_screen(env: dict[str, str], qtbot: QtBot) -> None:
    log = stub_tmux(env)
    client, recorder = started(env, qtbot)
    screen = recorder.screens[-1]
    assert screen.lines == tuple(SCREEN)
    assert (screen.width, screen.height) == (40, 3)
    assert (screen.cursor_x, screen.cursor_y, screen.cursor_on) == (2, 1, True)
    assert client.pane() == "%0"
    sent = log_lines(log, "IN")
    assert sent[0] == f"display-message -p -t ={RUN_ID}: '#{{pane_id}}'"
    assert f"capture-pane -p -e -t ={RUN_ID}:" in sent
    assert "capture-pane -p -e -t %0" in sent
    assert f"display-message -p -t %0 '{mirror.SIZE_FORMAT}'" in sent
    finish(client, qtbot)


def test_output_triggers_exactly_one_capture(env: dict[str, str], qtbot: QtBot) -> None:
    stub_tmux(env)
    client, _recorder = started(env, qtbot)
    base = client.captures
    qtbot.wait(300)
    assert client.captures == base
    os.kill(pid(client), signal.SIGUSR1)
    qtbot.waitUntil(lambda: client.captures == base + 1 and idle(client))
    qtbot.wait(300)
    assert client.captures == base + 1
    finish(client, qtbot)


def test_output_during_a_capture_triggers_one_more(
    env: dict[str, str], qtbot: QtBot
) -> None:
    log = stub_tmux(env)
    client, recorder = started(env, qtbot)
    base = client.captures
    screens = len(recorder.screens)
    os.kill(pid(client), signal.SIGUSR2)
    qtbot.waitUntil(lambda: client.captures == base + 2 and idle(client))
    qtbot.wait(300)
    assert client.captures == base + 2
    assert len(recorder.screens) == screens + 2
    captures = [line for line in log_lines(log, "IN") if line.startswith("capture")]
    assert len(captures) == client.captures
    finish(client, qtbot)


def test_exit_ends_the_mirror(env: dict[str, str], qtbot: QtBot) -> None:
    stub_tmux(env)
    client, recorder = started(env, qtbot)
    os.kill(pid(client), signal.SIGTERM)
    qtbot.waitUntil(lambda: bool(recorder.ended))
    assert recorder.ended == [mirror.ENDED_REASON]
    assert not client.running()
    finish(client, qtbot)


def test_process_end_without_exit(env: dict[str, str], qtbot: QtBot) -> None:
    stub_tmux(env)
    client, recorder = started(env, qtbot)
    os.kill(pid(client), signal.SIGKILL)
    qtbot.waitUntil(lambda: bool(recorder.ended))
    assert recorder.ended[0].startswith("The session ended")
    finish(client, qtbot)


def test_session_gone_at_attach(env: dict[str, str], qtbot: QtBot) -> None:
    stub_tmux(env, attach_error=True)
    client = MirrorClient(RUN_ID, env)
    recorder = Recorder(client)
    client.start()
    qtbot.waitUntil(lambda: bool(recorder.ended))
    assert "can't find session" in recorder.ended[0]
    assert not recorder.screens
    finish(client, qtbot)


def test_failed_capture_ends_without_retry(env: dict[str, str], qtbot: QtBot) -> None:
    log = stub_tmux(env, capture_error=True)
    client = MirrorClient(RUN_ID, env)
    recorder = Recorder(client)
    client.start()
    qtbot.waitUntil(lambda: bool(recorder.ended))
    assert recorder.ended == ["tmux refused a command: can't find pane"]
    qtbot.wait(200)
    assert len([x for x in log_lines(log, "IN") if x.startswith("capture")]) == 1
    finish(client, qtbot)


def test_tmux_missing(env: dict[str, str]) -> None:
    env = {**env, "PATH": env["PATH"].split(":")[0]}
    client = MirrorClient(RUN_ID, env)
    recorder = Recorder(client)
    client.start()
    assert len(recorder.ended) == 1
    assert recorder.ended[0].startswith("The screen cannot be shown")
    assert not client.running()


def test_stop_closes_standard_input(env: dict[str, str], qtbot: QtBot) -> None:
    log = stub_tmux(env)
    client, recorder = started(env, qtbot)
    finish(client, qtbot)
    qtbot.waitUntil(lambda: "EOF" in log.read_text())
    assert recorder.ended == []


def test_resolve() -> None:
    palette = theme.mirror_palette("dark")
    assert mirror.resolve(sgr.Cell("a"), palette) == (palette.fg.name(), None)
    assert mirror.resolve(sgr.Cell("a", fg=("index", 9)), palette)[0] == (
        palette.colors[9].name()
    )
    dark_rgb = sgr.Cell("a", fg=("rgb", 10, 10, 10), bg=("rgb", 200, 0, 0))
    fg, bg = mirror.resolve(dark_rgb, palette)
    assert bg is None
    assert theme.contrast(QColor(fg), palette.bg) >= 4.5
    cube = sgr.Cell("a", fg=("index", 17))
    assert theme.contrast(QColor(mirror.resolve(cube, palette)[0]), palette.bg) >= 4.5
    gray = sgr.Cell("a", fg=("index", 255))
    assert mirror.resolve(gray, palette)[0] == "#eeeeee"
    reverse = sgr.Cell("a", fg=("index", 2), reverse=True)
    assert mirror.resolve(reverse, palette) == (
        palette.bg.name(),
        palette.colors[2].name(),
    )


def test_view_paints_and_ends(env: dict[str, str], qtbot: QtBot) -> None:
    stub_tmux(env)
    view = MirrorView(RUN_ID, env)
    qtbot.addWidget(view)
    view.show()
    assert view.accessibleName() == f"Screen of run {RUN_ID}"
    view.start()
    qtbot.waitUntil(lambda: view.last_screen() is not None and idle(view.client))
    cell = view.canvas.cell_size()
    assert view.canvas.height() == 3 * cell.height() + 2 * mirror.PADDING
    assert view.canvas.width() == 40 * cell.width() + 2 * mirror.PADDING
    image = view.canvas.grab()
    assert not image.isNull()
    assert not view.has_ended()
    os.kill(pid(view.client), signal.SIGTERM)
    qtbot.waitUntil(view.has_ended)
    view.stop()


def test_view_wider_than_its_pane_list(env: dict[str, str], qtbot: QtBot) -> None:
    stub_tmux(env, screen=["x" * 200], size="200 1 0 0 0 0")
    view = MirrorView(RUN_ID, env)
    qtbot.addWidget(view)
    view.resize(300, 200)
    view.show()
    view.start()
    qtbot.waitUntil(lambda: view.last_screen() is not None)
    assert view.canvas.width() > view.scroller.viewport().width()
    qtbot.waitUntil(lambda: view.scroller.horizontalScrollBar().maximum() > 0)
    finish(view.client, qtbot)


def test_keys_scroll_the_view_and_never_reach_tmux(
    env: dict[str, str], qtbot: QtBot
) -> None:
    log = stub_tmux(env, screen=["x" * 200], size="200 1 0 0 0 0")
    outer = QScrollArea()
    qtbot.addWidget(outer)
    outer.setWidgetResizable(True)
    outer.resize(400, 300)
    content = QWidget()
    column = QVBoxLayout(content)
    before = QPushButton("Before")
    column.addWidget(before)
    view = MirrorView(RUN_ID, env)
    column.addWidget(view)
    spacer = QWidget()
    spacer.setFixedHeight(2000)
    column.addWidget(spacer)
    outer.setWidget(content)
    outer.show()
    qtbot.waitExposed(outer)
    with qtbot.waitActive(outer):
        outer.activateWindow()
    view.start()
    qtbot.waitUntil(lambda: view.last_screen() is not None and idle(view.client))
    bar = view.scroller.horizontalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0)
    sent = len(log_lines(log, "IN"))

    before.setFocus()
    qtbot.keyClick(before, Qt.Key.Key_Tab)
    assert view.scroller.hasFocus()
    qtbot.keyClick(view.scroller, Qt.Key.Key_Right)
    assert bar.value() > 0
    qtbot.keyClick(view.scroller, Qt.Key.Key_End)
    assert bar.value() == bar.maximum()
    qtbot.keyClick(view.scroller, Qt.Key.Key_Home)
    assert bar.value() == 0
    page = outer.verticalScrollBar()
    qtbot.keyClick(view.scroller, Qt.Key.Key_PageDown)
    assert page.value() > 0
    qtbot.keyClick(view.scroller, Qt.Key.Key_Up)
    qtbot.keyClick(view.scroller, Qt.Key.Key_PageUp)
    assert page.value() == 0
    qtbot.keyClicks(view.scroller, "ls\r")
    assert len(log_lines(log, "IN")) == sent
    finish(view.client, qtbot)


def test_focus_ring_style() -> None:
    sheet = theme.style_sheet("light")
    rule = sheet.split('QScrollArea[role="mirror"]:focus')[1].split("}")[0]
    assert theme.color("focus-ring", "light").name() in rule.lower()
    assert 'QScrollArea[role="mirror"] {' in sheet
