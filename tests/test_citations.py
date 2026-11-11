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


def test_append_sources_lists_absolute_paths_without_expanding_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "sources"
    nested = folder / "nested" / "issue.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("source")
    single = tmp_path / "notes.md"
    single.write_text("notes")
    brief = "Task\n\nRead the sources.\n"
    rendered = citations.append_sources(
        brief, ["sources", str(single), str(folder), "./notes.md"]
    )
    assert rendered.startswith(brief + "\n## Sources to read\n")
    assert [line for line in rendered.splitlines() if line.startswith("- ")] == [
        f"- {folder}",
        f"- {single}",
    ]
    assert str(nested) not in rendered
    assert citations.append_sources(brief, []) == brief


@pytest.mark.parametrize("ending", ["\n", "  \n", "\r\n"])
def test_append_sources_preserves_an_existing_section(ending: str) -> None:
    brief = f"Task\n\n## Sources to read{ending}\nExisting instructions.\n"
    assert citations.append_sources(brief, ["/another/source"]) == brief


def test_append_sources_does_not_treat_a_heading_mention_as_a_section() -> None:
    brief = 'Task\n\nUse a "## Sources to read" section.\n'
    assert "- /source" in citations.append_sources(brief, ["/source"])


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
