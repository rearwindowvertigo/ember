import pytest

from ember.app import sgr
from ember.app.sgr import Cell

E = "\x1b"


def styles(text: str) -> list[Cell]:
    return sgr.parse_line(text)


def test_sample_capture() -> None:
    sample = (
        f"{E}[38;2;10;20;30mtrue{E}[39m {E}[1m{E}[31mbold-red{E}[0m "
        f"{E}[7mrev{E}[0m {E}[44mbg{E}[49m 漢字"
    )
    [line] = sgr.parse(sample)
    text = "".join(cell.char for cell in line)
    assert text == "true bold-red rev bg 漢字"
    assert line[0].fg == ("rgb", 10, 20, 30)
    assert line[4] == Cell(" ")
    assert line[5].bold and line[5].fg == ("index", 1)
    assert line[13] == Cell(" ")
    assert line[14].reverse
    assert line[17] == Cell(" ")
    assert line[18].bg == ("index", 4)
    assert line[20].bg is None
    assert [sgr.width(cell.char) for cell in line[-2:]] == [2, 2]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ("1", {"bold": True}),
        ("2", {"dim": True}),
        ("3", {"italic": True}),
        ("4", {"underline": True}),
        ("4:3", {"underline": True}),
        ("7", {"reverse": True}),
        ("9", {"strike": True}),
        ("21", {"underline": True}),
        ("32", {"fg": ("index", 2)}),
        ("38;5;200", {"fg": ("index", 200)}),
        ("38:5:200", {"fg": ("index", 200)}),
        ("38;2;1;2;3", {"fg": ("rgb", 1, 2, 3)}),
        ("38:2::1:2:3", {"fg": ("rgb", 1, 2, 3)}),
        ("45", {"bg": ("index", 5)}),
        ("48;5;17", {"bg": ("index", 17)}),
        ("48;2;4;5;6", {"bg": ("rgb", 4, 5, 6)}),
        ("93", {"fg": ("index", 11)}),
        ("104", {"bg": ("index", 12)}),
        (
            "1;3;31;42",
            {"bold": True, "italic": True, "fg": ("index", 1), "bg": ("index", 2)},
        ),
    ],
)
def test_set(params: str, expected: dict[str, object]) -> None:
    [cell] = styles(f"{E}[{params}mx")
    assert cell == Cell("x", **expected)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("on", "off", "field"),
    [
        ("1", "22", "bold"),
        ("2", "22", "dim"),
        ("3", "23", "italic"),
        ("4", "24", "underline"),
        ("7", "27", "reverse"),
        ("9", "29", "strike"),
        ("31", "39", "fg"),
        ("41", "49", "bg"),
        ("1;31;41;7", "0", "reverse"),
        ("1;31", "", "bold"),
    ],
)
def test_reset(on: str, off: str, field: str) -> None:
    before, after = styles(f"{E}[{on}ma{E}[{off}mb")
    assert getattr(before, field)
    assert not getattr(after, field)


def test_other_sequences_are_skipped() -> None:
    line = styles(
        f"a{E}[2Kb{E}]8;;https://example.org{E}\\c{E}]0;title\x07d{E}(Be{E}[5mf{E}[?25hg"
    )
    assert "".join(cell.char for cell in line) == "abcdefg"
    assert all(cell == Cell(cell.char) for cell in line)


def test_unfinished_sequence_ends_line() -> None:
    assert [cell.char for cell in styles(f"ab{E}[31")] == ["a", "b"]


def test_combining_marks_join_the_cell_before() -> None:
    line = styles("éx❤️")
    assert [cell.char for cell in line] == ["é", "x", "❤️"]


def test_wide_and_tab() -> None:
    line = styles("漢\tz")
    assert [cell.char for cell in line] == ["漢"] + [" "] * 6 + ["z"]
    assert sgr.width("漢") == 2
    assert sgr.width("Ａ") == 2
    assert sgr.width("a") == 1


def test_each_line_starts_plain() -> None:
    first, second = sgr.parse(f"{E}[1ma\nb\n")
    assert first[0].bold
    assert not second[0].bold
