import io

import pytest

from ember.runs import exits

CODES = (1, 64, 65, 66, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 124)


def test_table_has_every_code() -> None:
    assert tuple(sorted(exits.LINES)) == CODES


@pytest.mark.parametrize("code", CODES)
def test_every_line_is_next_or_owner(code: int) -> None:
    entry = exits.LINES[code]
    lines = [entry] if isinstance(entry, str) else list(entry.values())
    for text in lines:
        assert text.startswith(("next: ", "owner: "))
        assert "\n" not in text


def test_zero_has_no_line() -> None:
    assert exits.line(0) is None
    assert exits.emit(0) is None


def test_placeholders_and_blanks() -> None:
    assert exits.line(124, run="codex-1") == (
        "next: emb run --recover codex-1 "
        "(waits for the turn still running in its session, then collects it)"
    )
    assert exits.line(71, run=None) == "next: emb run --status <run>"
    assert exits.line(71, run="") == "next: emb run --status <run>"


def test_variants_fall_back_to_the_default() -> None:
    assert exits.line(69, "fix", fix="codex login") == "owner: codex login"
    assert exits.line(69, "nope", engine="codex") == exits.line(69, engine="codex")
    assert exits.line(65, "anything") == exits.line(65)


def test_unknown_code_uses_exit_1_line() -> None:
    assert exits.line(99) == "next: emb run --status"


def test_emit_prints_the_line() -> None:
    stream = io.StringIO()
    text = exits.emit(75, "close", stream=stream, run="r1")
    assert text is not None
    assert stream.getvalue() == text + "\n"
    assert "emb run --close r1 --force" in text


def test_no_reply_points_at_screen_then_send() -> None:
    assert exits.line(80, run="r1") == (
        "next: emb run --screen r1, then emb run --send r1 --message-file <file>"
    )


@pytest.mark.parametrize("kind", ["capacity", "usage_limit", "outage"])
def test_provider_next_line_names_the_kind(kind: str) -> None:
    text = exits.line(73, kind, run="codex-1")
    assert text is not None and text.startswith("next:")
    assert kind in text
