"""Parse the text `tmux capture-pane -p -e` prints into styled cells.

`capture-pane -e` writes the pane's text with SGR sequences (`ESC [ … m`)
for its colours and attributes. `parse` turns each line into cells. Any
other escape sequence is skipped without breaking the line. This module does
not use Qt.
"""

import unicodedata
from dataclasses import dataclass, replace

Color = tuple[str, int] | tuple[str, int, int, int]
"""`("index", 0-255)` or `("rgb", r, g, b)`."""

TAB_WIDTH = 8
"""The columns between tab stops."""

ESC = "\x1b"


@dataclass(frozen=True)
class Cell:
    """One displayed character and its style."""

    char: str
    """The character, followed by any combining marks or variation selectors."""

    fg: Color | None = None
    """The foreground colour; `None` is the default."""

    bg: Color | None = None
    """The background colour; `None` is the default."""

    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    reverse: bool = False


_PLAIN = Cell(" ")


def width(char: str) -> int:
    """Return the columns a cell's character takes: 2 for wide, else 1.

    Args:
        char: A cell's text; its first character decides.

    Returns:
        2 when the character is East Asian wide or full-width, else 1.
    """
    if not char:
        return 1
    return 2 if unicodedata.east_asian_width(char[0]) in ("W", "F") else 1


def _joins(char: str) -> bool:
    """Return whether a character attaches to the cell before it."""
    code = ord(char)
    return (
        unicodedata.combining(char) != 0
        or unicodedata.category(char) in ("Mn", "Me")
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
        or code == 0x200D
    )


def _extended(parts: list[list[int | None]], index: int) -> tuple[Color | None, int]:
    """Read a `38`/`48` colour starting at `parts[index]`.

    Returns:
        The colour (or `None` when it is not complete) and how many
        parameters it used.
    """
    head = parts[index]
    if len(head) > 1:
        # Colon form: 38:5:n or 38:2:[colourspace:]r:g:b in one parameter.
        sub = head[1:]
        if sub and sub[0] == 5 and len(sub) >= 2 and sub[1] is not None:
            return ("index", min(sub[1], 255)), 1
        if sub and sub[0] == 2 and len(sub) >= 4:
            r, g, b = (min(value or 0, 255) for value in sub[-3:])
            return ("rgb", r, g, b), 1
        return None, 1
    rest = [part[0] for part in parts[index + 1 :]]
    if rest and rest[0] == 5 and len(rest) >= 2:
        return ("index", min(rest[1] or 0, 255)), 3
    if rest and rest[0] == 2 and len(rest) >= 4:
        r, g, b = (min(value or 0, 255) for value in rest[1:4])
        return ("rgb", r, g, b), 5
    return None, len(parts) - index


def _apply(style: Cell, params: str) -> Cell:
    """Return the style after one SGR sequence's parameters."""
    parts: list[list[int | None]] = []
    for token in params.split(";"):
        sub: list[int | None] = []
        for piece in token.split(":"):
            sub.append(int(piece) if piece.isdigit() else None)
        parts.append(sub)
    index = 0
    while index < len(parts):
        code = parts[index][0]
        code = 0 if code is None else code
        step = 1
        if code == 0:
            style = _PLAIN
        elif code == 1:
            style = replace(style, bold=True)
        elif code == 2:
            style = replace(style, dim=True)
        elif code == 3:
            style = replace(style, italic=True)
        elif code == 4:
            styled = len(parts[index]) < 2 or parts[index][1] != 0
            style = replace(style, underline=styled)
        elif code == 7:
            style = replace(style, reverse=True)
        elif code == 9:
            style = replace(style, strike=True)
        elif code == 21:
            style = replace(style, underline=True)
        elif code == 22:
            style = replace(style, bold=False, dim=False)
        elif code == 23:
            style = replace(style, italic=False)
        elif code == 24:
            style = replace(style, underline=False)
        elif code == 27:
            style = replace(style, reverse=False)
        elif code == 29:
            style = replace(style, strike=False)
        elif 30 <= code <= 37:
            style = replace(style, fg=("index", code - 30))
        elif code in (38, 48):
            color, step = _extended(parts, index)
            if color is not None:
                style = (
                    replace(style, fg=color) if code == 38 else replace(style, bg=color)
                )
        elif code == 39:
            style = replace(style, fg=None)
        elif 40 <= code <= 47:
            style = replace(style, bg=("index", code - 40))
        elif code == 49:
            style = replace(style, bg=None)
        elif 90 <= code <= 97:
            style = replace(style, fg=("index", code - 90 + 8))
        elif 100 <= code <= 107:
            style = replace(style, bg=("index", code - 100 + 8))
        index += step
    return style


def _skip_escape(line: str, start: int) -> tuple[int, str | None]:
    """Skip the escape sequence at `line[start]`.

    Returns:
        The index after it, and the SGR parameters when it was an SGR
        sequence, else `None`.
    """
    end = len(line)
    if start + 1 >= end:
        return end, None
    kind = line[start + 1]
    if kind == "[":
        index = start + 2
        while index < end and not 0x40 <= ord(line[index]) <= 0x7E:
            index += 1
        if index >= end:
            return end, None
        params = line[start + 2 : index]
        if line[index] == "m" and all(c.isdigit() or c in ";:" for c in params):
            return index + 1, params
        return index + 1, None
    if kind in "]PX^_":
        # A string sequence, ended by BEL or ESC \.
        index = start + 2
        while index < end:
            if line[index] == "\x07":
                return index + 1, None
            if line[index] == ESC and index + 1 < end and line[index + 1] == "\\":
                return index + 2, None
            index += 1
        return end, None
    # Any other sequence: intermediate bytes, then one final byte.
    index = start + 1
    while index < end and 0x20 <= ord(line[index]) <= 0x2F:
        index += 1
    return min(index + 1, end), None


def parse_line(line: str) -> list[Cell]:
    """Return the cells of one captured line, starting from the default style.

    Args:
        line: One line of `capture-pane -p -e` output, without its newline.

    Returns:
        One cell per displayed character. A wide character is one cell that
        takes two columns (see `width`); combining marks and variation
        selectors join the cell before them.
    """
    cells: list[Cell] = []
    style = _PLAIN
    columns = 0
    index = 0
    while index < len(line):
        char = line[index]
        if char == ESC:
            index, params = _skip_escape(line, index)
            if params is not None:
                style = _apply(style, params)
            continue
        index += 1
        if char == "\t":
            spaces = TAB_WIDTH - columns % TAB_WIDTH
            cells.extend(replace(style, char=" ") for _ in range(spaces))
            columns += spaces
            continue
        if ord(char) < 0x20 or ord(char) == 0x7F:
            continue
        if _joins(char):
            if cells:
                cells[-1] = replace(cells[-1], char=cells[-1].char + char)
            else:
                cells.append(replace(style, char=char))
                columns += 1
            continue
        cell = replace(style, char=char)
        cells.append(cell)
        columns += width(char)
    return cells


def parse(text: str) -> list[list[Cell]]:
    """Return the cells of every line of `capture-pane -p -e` output.

    Each line starts from the default style, as tmux writes each line's
    sequences against the default.

    Args:
        text: The captured text, lines separated by newlines.

    Returns:
        One list of cells per line.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [parse_line(line.rstrip("\r")) for line in lines]
