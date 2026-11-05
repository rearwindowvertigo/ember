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


def test_codex_trust_requires_owner_action() -> None:
    assert exits.line(69, "trust", dir="/repo") == (
        "owner: run codex in /repo once and choose to trust the folder"
    )


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


REPORTS = [  # noqa: E501
    (
        69,
        None,
        "no worker started; do not retry the start or start another engine "
        "before the owner answers",
    ),
    (
        73,
        "capacity",
        "capacity; retries exhausted; offer only a retry with the same model "
        "and effort once capacity returns; offer no other model, engine, "
        "Claude subagent or Claude's own work",
    ),
    (
        73,
        "outage",
        "outage; offer only a retry with the same model and effort once it "
        "clears; offer no other model, engine, Claude subagent or Claude's "
        "own work",
    ),
    (
        73,
        "usage_limit",
        "usage_limit; a retry on the same worker needs no question; another "
        "worker or model needs the owner's answer; OpenCode may be offered",
    ),
    (
        73,
        "uncertain",
        "retry delivery is uncertain; do not send again before inspecting the session",
    ),
    (
        74,
        "uncertain",
        "retry delivery is uncertain; do not send again before inspecting the session",
    ),
    (74, None, "interrupted; partial edits may be committed on worker"),
    (74, "retry", "interrupted; partial edits may be committed on worker"),
    (74, "complete", "interrupted; partial edits may be committed on worker"),
    (
        75,
        "conflict",
        "merge conflict; do not retry or force it; ask the owner: a new run "
        "cut from the updated target, or the owner resolves it",
    ),
    (
        76,
        None,
        "branch or HEAD moved; nothing committed; recover only when the "
        "branch and HEAD are as recorded, otherwise leave the work to the "
        "owner",
    ),
    (77, None, "the commit hook refused; the work is uncommitted in /worktree"),
    (80, None, "the turn ended with no reply"),
]


@pytest.mark.parametrize(("code", "variant", "expected"), REPORTS)
def test_report_lines(code: int, variant: str | None, expected: str) -> None:
    assert exits.report(code, variant, branch="worker", worktree="/worktree") == (
        "report: " + expected
    )


@pytest.mark.parametrize("variant", exits.LINES[69])
def test_unavailable_reports_only_before_a_turn(variant: str) -> None:
    assert exits.report(69, variant) == exits.report(69)
    assert exits.report(69, variant, turn=1) is None


@pytest.mark.parametrize(
    ("code", "variant", "read_only"),
    [
        (0, None, False),
        (64, None, False),
        (69, "fix", False),
        (74, "starting", False),
        (74, "send", False),
        (74, None, True),
        (74, "retry", True),
        (74, "complete", True),
        (75, "busy", False),
        (77, "add", False),
        (124, None, False),
    ],
)
def test_no_report(code: int, variant: str | None, read_only: bool) -> None:
    assert exits.report(code, variant, read_only=read_only, turn=1) is None


def test_start_failure_reports_before_a_recorded_turn_was_launched() -> None:
    assert exits.report(69, "runtime", turn=1, last_start_failure=True) == exits.report(
        69
    )
