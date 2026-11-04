from pathlib import Path

import pytest

from ember import citations


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See #93 and #93 again.", ["#93"]),
        ("Fixed in owner/repo#12, see #7.", ["owner/repo#12", "#7"]),
        ("Read https://example.com/a/b.html.", ["https://example.com/a/b.html"]),
        ("(https://example.com/x)", ["https://example.com/x"]),
        ("An entity &#39; and a word abc#9 and C#1.", []),
        ("UTF-8, SHA-256 and ISO-8601 are words.", []),
        ("", []),
    ],
)
def test_find(text: str, expected: list[str]) -> None:
    assert [item.reference for item in citations.find("task a", text)] == expected
    assert all(item.where == "task a" for item in citations.find("task a", text))


def test_source_files(tmp_path: Path) -> None:
    single = tmp_path / "issue-93.md"
    single.write_text("x")
    folder = tmp_path / "sources"
    (folder / "nested").mkdir(parents=True)
    (folder / "b.md").write_text("b")
    (folder / "a.md").write_text("a")
    found = citations.source_files(
        [str(single), str(folder), str(tmp_path / "missing"), str(single)]
    )
    assert found == [single, folder / "a.md", folder / "b.md"]
    assert citations.source_files([]) == []


@pytest.mark.parametrize(
    ("name", "text", "reference", "expected"),
    [
        ("issue-93.md", "", "#93", True),
        ("issue-930.md", "", "#93", False),
        ("notes.md", "Source: #93\n", "#93", True),
        ("93.txt", "", "owner/repo#93", True),
        ("notes.md", "https://example.com/x\n", "https://example.com/x", True),
        ("example-93.md", "", "https://example.com/93", False),
        ("notes.md", "\n" * citations.HEAD_LINES + "#93\n", "#93", False),
    ],
)
def test_supplies(
    tmp_path: Path, name: str, text: str, reference: str, expected: bool
) -> None:
    path = tmp_path / name
    path.write_text(text)
    assert citations.supplies(path, reference) is expected


def test_uncovered_keeps_the_first_citation_of_each_reference(tmp_path: Path) -> None:
    covered = tmp_path / "issue-1.md"
    covered.write_text("")
    found = [
        *citations.find("the context", "#1 and #2"),
        *citations.find("task b", "#2 and #3"),
    ]
    missing = citations.uncovered(found, [covered])
    assert [(item.where, item.reference) for item in missing] == [
        ("the context", "#2"),
        ("task b", "#3"),
    ]
    assert citations.warning(missing[0]) == (
        "the context cites #2, which no read path supplies"
    )


def test_sources_section(tmp_path: Path) -> None:
    assert citations.sources_section([]) is None
    section = citations.sources_section([tmp_path / "a.md"])
    assert section is not None and section.startswith("## Sources to read")
    assert f"- {tmp_path / 'a.md'}" in section


@pytest.mark.parametrize(
    ("text", "reference"),
    [
        ("Source: #930\n", "#93"),
        ("Source: owner/repo#93\n", "#93"),
        ("https://example.com/x/y\n", "https://example.com/x"),
        ("https://example.com/xy\n", "https://example.com/x"),
    ],
)
def test_a_longer_reference_does_not_supply_a_shorter_one(
    tmp_path: Path, text: str, reference: str
) -> None:
    path = tmp_path / "unrelated.md"
    path.write_text(text)
    assert not citations.supplies(path, reference)


def test_standing_reads_the_sandbox_read_setting() -> None:
    reply = {"answers": {"sandbox.read": {"value": ["/a", "/b"]}}}
    assert citations.standing(reply) == ("/a", "/b")
    assert citations.standing(None) == ()
    assert citations.standing({"answers": {}}) == ()
    assert citations.standing({"answers": {"sandbox.read": {"value": "x"}}}) == ()
