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


def assert_screen_fits(screen: QWidget) -> None:
    """Check the content cap, centring, and every visible child's horizontal bounds."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QScrollArea

    from ember.app import theme, widgets

    area = screen.findChild(QScrollArea)
    assert area is not None
    content = area.widget()
    assert content is not None
    viewport = area.viewport()
    assert not area.horizontalScrollBar().isVisible()
    assert area.horizontalScrollBar().maximum() == 0
    assert content.width() <= theme.CONTENT_MAX_WIDTH + 2 * common.SCREEN_MARGIN
    assert abs(content.x() * 2 + content.width() - viewport.width()) <= 1
    for child in content.findChildren(QWidget):
        if not child.isVisible() or child.isWindow():
            continue
        ancestor = child.parentWidget()
        table_child = False
        while ancestor is not None and ancestor is not content:
            if isinstance(ancestor, QScrollArea):
                table_child = True
                break
            ancestor = ancestor.parentWidget()
        if table_child:
            continue
        left = child.mapTo(viewport, QPoint()).x()
        right = left + child.width()
        detail = (
            type(child).__name__,
            child.accessibleName(),
            left,
            right,
            viewport.width(),
        )
        assert 0 <= left <= right <= viewport.width(), detail
        ancestor = child.parentWidget()
        while ancestor is not None and ancestor is not content:
            if isinstance(ancestor, widgets.Card):
                x = child.mapTo(ancestor, QPoint()).x()
                assert 0 <= x <= x + child.width() <= ancestor.width(), detail
            ancestor = ancestor.parentWidget()


def assert_rows_reflow(
    screen: QWidget, rows: list[common.TooltipRow], qtbot: QtBot
) -> None:
    """Check shrinking, a shared stack, its saved width, and widening again."""
    from ember.app import theme, widgets

    picker = next(
        row.control for row in rows if isinstance(row.control, widgets.Picker)
    )
    picker.add_option("A long closed model value " * 3, "long")
    picker.set_value("long")
    common.align_controls(rows)
    screen.resize(1400, 480)
    screen.show()
    qtbot.wait(30)
    visible = [row for row in rows if row.isVisible()]
    wide = visible[0].slot.width()
    assert not any(row.stacked for row in visible)
    last = wide
    shrank = False
    for width in range(1000, 399, -8):
        screen.resize(width, 480)
        qtbot.wait(5)
        states = {row.stacked for row in visible}
        assert len(states) == 1
        if True in states:
            for row in visible:
                assert row.slot.width() == min(last, row.width())
                assert row.control.x() == 0
                assert row.slot.x() == 0
                assert row.slot.y() - row.name.geometry().bottom() - 1 == theme.SPACE[1]
            break
        current = visible[0].slot.width()
        assert {row.slot.width() for row in visible} == {current}
        assert current >= theme.PICKER_MIN_WIDTH
        shrank |= current < wide
        last = current
    else:
        raise AssertionError("Rows never stacked")
    assert shrank
    screen.resize(1400, 480)
    qtbot.wait(30)
    assert not any(row.stacked for row in visible)
    assert {row.slot.width() for row in visible} == {wide}


def test_hidden_rows_and_replaced_row_sets_refit(qtbot: QtBot) -> None:
    from ember.app import widgets

    screen = QWidget()
    qtbot.addWidget(screen)
    layout = common.scrolled(screen)
    card, column = common.titled_card("Settings")
    layout.addWidget(card)
    short = common.TooltipRow("Short", "Tip", widgets.Picker())
    long = common.TooltipRow("A much longer setting name", "Tip", widgets.Picker())
    column.addWidget(short)
    column.addWidget(long)
    common.align_controls([short, long])
    screen.resize(500, 400)
    screen.show()
    qtbot.waitUntil(lambda: short.stacked and long.stacked)
    long.hide()
    qtbot.waitUntil(lambda: not short.stacked)
    long.show()
    qtbot.waitUntil(lambda: short.stacked and long.stacked)
    common.align_controls([short])
    qtbot.waitUntil(lambda: not short.stacked)
    long.hide()
    long.show()
    qtbot.wait(20)
    assert not short.stacked


def test_scroll_bar_does_not_change_row_fit(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QScrollArea

    from ember.app import widgets

    screen = QWidget()
    qtbot.addWidget(screen)
    layout = common.scrolled(screen)
    card, column = common.titled_card("Settings")
    layout.addWidget(card)
    row = common.TooltipRow("A setting name", "Tip", widgets.Picker())
    column.addWidget(row)
    extra = QWidget()
    extra.setFixedHeight(800)
    layout.addWidget(extra)
    extra.hide()
    common.align_controls([row])
    screen.resize(500, 400)
    screen.show()
    qtbot.wait(20)
    before = (row.stacked, row.slot.width())
    area = screen.findChild(QScrollArea)
    assert area is not None
    assert not area.verticalScrollBar().isVisible()
    extra.show()
    qtbot.waitUntil(lambda: area.verticalScrollBar().isVisible())
    qtbot.wait(20)
    assert (row.stacked, row.slot.width()) == before


def test_picker_column_reaches_floor_before_stacking(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QScrollArea

    from ember.app import theme, widgets

    screen = QWidget()
    qtbot.addWidget(screen)
    layout = common.scrolled(screen)
    card, column = common.titled_card("Settings")
    layout.addWidget(card)
    picker = widgets.Picker()
    picker.add_option("A model with a long name " * 3, "long")
    row = common.TooltipRow("Name", "Tip", picker)
    column.addWidget(row)
    common.align_controls([row])
    screen.resize(800, 400)
    screen.show()
    qtbot.wait(20)
    area = screen.findChild(QScrollArea)
    assert area is not None
    threshold = (
        screen.width()
        - row.width()
        + area.verticalScrollBar().sizeHint().width()
        + row.name.minimumSizeHint().width()
        + 16
        + theme.PICKER_MIN_WIDTH
    )
    screen.resize(threshold, 400)
    qtbot.wait(20)
    assert not row.stacked
    assert picker.width() == theme.PICKER_MIN_WIDTH
    screen.resize(threshold - 1, 400)
    qtbot.waitUntil(lambda: row.stacked)
    assert picker.width() == theme.PICKER_MIN_WIDTH
    screen.resize(800, 400)
    qtbot.waitUntil(lambda: not row.stacked)
    assert picker.width() > theme.PICKER_MIN_WIDTH


def test_screen_header_sidebar_button_precedes_title_and_actions(qtbot: QtBot) -> None:
    from PySide6.QtCore import Qt

    from ember.app import widgets

    screen = QWidget()
    qtbot.addWidget(screen)
    layout = common.scrolled(screen)
    check = QPushButton("Check now")
    common.screen_header(layout, "Engines", "Checked", iter((check,)))
    show = next(
        button
        for button in screen.findChildren(widgets.IconButton)
        if button.property("role") == "show-sidebar"
    )
    assert show.isHidden()
    assert show.toolTip() == show.accessibleName() == "Show sidebar"
    assert not show.icon().isNull()
    screen.show()
    show.show()
    qtbot.waitExposed(screen)
    with qtbot.waitActive(screen):
        screen.activateWindow()
    show.setFocus()
    qtbot.keyClick(show, Qt.Key.Key_Tab)
    assert check.hasFocus()
    heading = next(
        label for label in screen.findChildren(QLabel) if label.text() == "Engines"
    )
    assert show.geometry().right() < heading.geometry().left()
    assert abs(show.geometry().center().y() - heading.geometry().center().y()) <= 1
