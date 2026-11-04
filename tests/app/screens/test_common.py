import datetime

from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from ember.app.screens import common


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime.datetime(year, month, day, hour, minute).timestamp()


def test_when_today_yesterday_and_dates() -> None:
    now = _at(2026, 11, 2, 12, 0)
    assert common.when(_at(2026, 11, 2, 10, 42), now) == "Today 10:42"
    assert common.when(_at(2026, 11, 1, 17, 3), now) == "Yesterday 17:03"
    assert common.when(_at(2026, 10, 30, 14, 5), now) == "30 Oct 14:05"
    assert common.when(_at(2026, 1, 3, 9, 7), now) == "3 Jan 09:07"


def test_when_uses_english_month_abbreviations() -> None:
    names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun")
    names += ("Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    now = _at(2027, 6, 1)
    for month, name in enumerate(names, start=1):
        assert common.when(_at(2026, month, 3, 8), now) == f"3 {name} 08:00"


def test_inline_and_sentence() -> None:
    assert common.inline("Today 10:42") == "today 10:42"
    assert common.inline("Yesterday 17:03") == "yesterday 17:03"
    assert common.inline("30 Oct 14:05") == "30 Oct 14:05"
    assert common.sentence("the codex CLI") == "The codex CLI"
    assert common.sentence("") == ""


def test_duration() -> None:
    assert common.duration(5) == "1 min"
    assert common.duration(18 * 60 + 59) == "18 min"
    assert common.duration(65 * 60) == "1 h 5 min"
    assert common.duration(120 * 60) == "2 h"


def test_plural_and_engine_name() -> None:
    assert common.plural(1, "turn") == "1 turn"
    assert common.plural(2, "turn") == "2 turns"
    assert common.plural(0, "check failed", "checks failed") == "0 checks failed"
    assert common.engine_name("opencode") == "OpenCode"
    assert common.engine_name("other") == "other"


def test_tone_line_shows_and_hides(qtbot: QtBot) -> None:
    line = common.ToneLine("danger")
    qtbot.addWidget(line)
    assert line.isHidden()
    line.show_text("Broken.")
    assert not line.isHidden()
    assert line.accessibleName() == "Broken."
    line.show_text(None)
    assert line.isHidden()


def test_screen_header_returns_the_subtitle(qtbot: QtBot) -> None:
    screen = QWidget()
    qtbot.addWidget(screen)
    layout = common.scrolled(screen)
    button = QPushButton("Check now")
    subtitle = common.screen_header(layout, "Engines", "Checked", (button,))
    assert subtitle.text() == "Checked"
    assert subtitle.property("role") == "secondary"
    texts = [label.text() for label in screen.findChildren(QLabel)]
    assert "Engines" in texts
    assert button.parent() is not None


def test_discard_and_clear_layout(qtbot: QtBot) -> None:
    box = QWidget()
    qtbot.addWidget(box)
    layout = QVBoxLayout(box)
    inner = QVBoxLayout()
    first = common.label("one")
    second = common.label("two")
    layout.addWidget(first)
    inner.addWidget(second)
    layout.addLayout(inner)
    common.discard(first, layout)
    assert layout.count() == 1
    assert first.isHidden()
    common.clear_layout(layout)
    assert layout.count() == 0
    assert second.isHidden()
    common.discard(None)


def test_tooltip_row_is_a_name_with_an_info_icon(qtbot: QtBot) -> None:
    control = QPushButton("Always")
    row = common.TooltipRow("Run the gap check", "Full.", control)
    qtbot.addWidget(row)
    assert row.info.toolTip() == "Full."
    assert row.info.accessibleName() == "About Run the gap check"
    assert row.heading.toolTip() == ""
    assert control.toolTip() == ""
    labels = [label.text() for label in row.findChildren(QLabel)]
    assert labels == ["Run the gap check"]


def test_align_controls_gives_rows_one_column(qtbot: QtBot) -> None:
    from ember.app import widgets

    picker = widgets.Picker()
    picker.add_option("gpt-6-astra · high", "x")
    wide = widgets.Segmented((("a", "Ask each time"), ("f", "Flow"), ("w", "Claude")))
    switch = widgets.Switch()
    rows = [
        common.TooltipRow(name, "Tip.", control)
        for name, control in (("A", picker), ("B", wide), ("C", switch))
    ]
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    for row in rows:
        layout.addWidget(row)
    width = common.align_controls(rows)
    assert width >= max(common.CONTROL_COLUMN, wide.sizeHint().width())
    host.resize(width + 400, 300)
    host.show()
    qtbot.waitExposed(host)
    assert {row.slot.width() for row in rows} == {width}
    assert picker.width() == width
    right = {row.control.mapTo(host, row.control.rect().topRight()).x() for row in rows}
    assert len(right) == 1
