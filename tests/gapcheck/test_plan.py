import json
from pathlib import Path

import pytest

from ember.gapcheck.plan import KINDS, PlanError, Step, has_failure_model, load, split
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]


@pytest.fixture(autouse=True)
def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in make_env(tmp_path).items():
        monkeypatch.setenv(key, value)


def test_json_tasks() -> None:
    text = json.dumps(
        {
            "tasks": [
                {"id": "a", "task": "Read\n  files"},
                {"task": "Build"},
                {"id": "c"},
            ]
        }
    )
    assert split(text) == (
        "tasks",
        (
            Step(1, "task a: Read files", None, None),
            Step(2, "task 2: Build", None, None),
            Step(3, "task c", None, None),
        ),
    )


@pytest.mark.parametrize("value", [[], 1, "text", {"tasks": []}, {"tasks": [1]}])
def test_invalid_task_lists_fall_through(value: object) -> None:
    assert split(json.dumps(value)) == (
        "whole",
        (Step(1, "the whole plan", 1, 1),),
    )


def test_numbered_items_take_precedence_and_trim_blank_lines() -> None:
    assert split("# Plan\n1. Read\n\n2) Build\n  details\n\n") == (
        "numbered",
        (Step(1, "1. Read", 2, 2), Step(2, "2) Build", 4, 5)),
    )


def test_one_numbered_item_uses_headings() -> None:
    assert split("## Read\n1. File\n\n## Build\n\n") == (
        "headings",
        (Step(1, "## Read", 1, 2), Step(2, "## Build", 4, 4)),
    )


@pytest.mark.parametrize("marker", ["```", "~~~", "   ```", "  ~~~"])
def test_fences_hide_steps_and_need_matching_marker(marker: str) -> None:
    other = "~~~" if "`" in marker else "```"
    text = f"{marker}\n1. hidden\n{other}\n2. hidden\n{marker}\n## A\n## B"
    assert split(text) == (
        "headings",
        (Step(1, "## A", 6, 6), Step(2, "## B", 7, 7)),
    )


def test_fenced_json_does_not_supply_tasks_or_numbered_steps() -> None:
    assert split('```json\n{"tasks": [{"id": "a"}]}\n```')[0] == "whole"
    assert split("```\n1. A\n2. B\n```")[0] == "whole"


def test_only_repeated_heading_level_and_shallower_boundary() -> None:
    assert split("### A\nbody\n\n## Parent\n### B\nend\n\n") == (
        "headings",
        (Step(1, "### A", 1, 2), Step(2, "### B", 5, 6)),
    )


def test_shallowest_repeated_headings() -> None:
    assert split("## A\n### Child\n## B\n### Other")[1] == (
        Step(1, "## A", 1, 2),
        Step(2, "## B", 3, 4),
    )


@pytest.mark.parametrize(
    ("text", "last"),
    [("", 1), (" \n\n", 2), ("body\n\n", 2), (" 1. A\n 2. B", 2)],
)
def test_whole_plan(text: str, last: int) -> None:
    assert split(text) == ("whole", (Step(1, "the whole plan", 1, last),))


@pytest.mark.parametrize(
    "text",
    [
        "1. " + "x" * 150 + "\n2. B",
        "## " + "x" * 150 + "\n## B",
        json.dumps({"tasks": [{"id": "x" * 150, "task": "summary"}]}),
        json.dumps({"tasks": [{"id": "a\n@b", "task": "x" * 150}]}),
    ],
)
def test_total_title_length_and_one_line(text: str) -> None:
    step = split(text)[1][0]
    assert len(step.title) == 100
    assert len(step.title.splitlines()) == 1


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ('{"tasks": [{}], "failure_model": "accepted"}', True),
        ('{"tasks": [{}], "failure_model": "  "}', False),
        ('{"tasks": [{}]}', False),
        ('{"tasks": [], "failure_model": "accepted"}', True),
        ('{"failure_model": 1}', False),
        ("## Failure model\n", True),
        ("### failure model (accepted)\n", True),
        ("```\n## Failure model\n```", False),
        ("~~~\n## Failure model\n```", False),
        ("The failure model is accepted.", False),
    ],
)
def test_failure_model(text: str, found: bool) -> None:
    assert has_failure_model(text, split(text)[0]) is found


def test_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("plan.md").write_text("## Failure model\n## Work\n", encoding="utf-8")
    plan = load("plan.md")
    assert plan.path == tmp_path / "plan.md"
    assert plan.text == "## Failure model\n## Work\n"
    assert plan.kind == "headings"
    assert plan.kind in KINDS
    assert plan.steps == (Step(1, "## Failure model", 1, 1), Step(2, "## Work", 2, 2))
    assert plan.failure_model is True


@pytest.mark.parametrize("bad_bytes", [None, b"\xff"])
def test_load_errors(tmp_path: Path, bad_bytes: bytes | None) -> None:
    path = tmp_path / "plan.md"
    if bad_bytes is not None:
        path.write_bytes(bad_bytes)
    with pytest.raises(PlanError, match=f"cannot read the plan {path}:") as error:
        load(path)
    assert isinstance(error.value.__cause__, (OSError, UnicodeDecodeError))


@pytest.mark.parametrize("valid", [False, True])
def test_flow_read_paths_survive_other_validation_errors(
    tmp_path: Path, valid: bool
) -> None:
    path = tmp_path / "plan.json"
    source = tmp_path / "issue.md"
    path.write_text(
        json.dumps(
            {
                "dir": str(tmp_path),
                "read": [str(source)],
                "tasks": [
                    {
                        "id": "a",
                        "subject": "feat: build",
                        "task": "Build",
                        "files": ["a.py"],
                    }
                ]
                if valid
                else [],
            }
        )
    )
    document = load(path)
    assert (document.flow is not None) is valid
    assert bool(document.flow_problems) is not valid
    assert document.read == (str(source),)
    assert document.read_paths() == (str(source),)


@pytest.mark.parametrize(
    "read", [None, "file", {}, [1], ["relative"], ["/a", 1], ["/a", "/a"]]
)
def test_invalid_flow_read_list_is_not_used(tmp_path: Path, read: object) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"dir": str(tmp_path), "tasks": [], "read": read}))
    document = load(path)
    assert document.flow is None
    assert any("plan.read" in problem for problem in document.flow_problems)
    assert document.read_paths() == ()


@pytest.mark.parametrize("fields", [{}, {"tasks": []}, {"dir": "/repo"}])
def test_non_flow_json_ignores_read(tmp_path: Path, fields: dict[str, object]) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({**fields, "read": ["/source"]}))
    document = load(path)
    assert document.read == () and document.flow_problems == ()


def test_read_paths_normalize_and_keep_first_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "issue.md"
    source.write_text("source")
    (tmp_path / "alias.md").symlink_to(source)
    (tmp_path / "nested").mkdir()
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps({"dir": str(tmp_path), "tasks": [], "read": [str(source)]})
    )
    assert load(path).read_paths(
        [
            "~/issue.md",
            "./issue.md",
            "nested/../issue.md",
            "alias.md",
            "other.md",
            str(source),
        ]
    ) == (str(source), str(tmp_path / "other.md"))


def test_read_paths_resolve_directory_symlinks_before_parent_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    archive = tmp_path / "archive"
    (archive / "release").mkdir(parents=True)
    (sources / "current").symlink_to(archive / "release", target_is_directory=True)
    target = archive / "source.md"
    target.write_text("Source text")
    source = archive / "issue-291.md"
    source.symlink_to(target)
    (sources / source.name).write_text("Wrong source")
    given = sources / "current" / ".." / source.name
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps({"dir": str(tmp_path), "tasks": [], "read": [str(given)]})
    )
    reads = load(path).read_paths(["sources/current/../issue-291.md", str(source)])
    assert reads == (str(source),)
    assert Path(reads[0]).read_text() == "Source text"
    assert Path(reads[0]).name == "issue-291.md"


def test_read_paths_preserve_an_unresolvable_parent(tmp_path: Path) -> None:
    source = tmp_path / "missing" / ".." / "issue.md"
    (tmp_path / "issue.md").write_text("Wrong source")
    path = tmp_path / "plan.md"
    path.write_text("Review")
    existing = tmp_path / "issue.md"
    assert load(path).read_paths([str(source), str(existing)]) == (
        str(source),
        str(existing),
    )


@pytest.mark.parametrize(
    ("spelling", "kept"),
    [
        ("sources/release/..", "sources"),
        ("sources/release/../", "sources"),
        ("current/..", "sources"),
        ("sources//./", "sources"),
        ("sources//./release", "sources/release"),
        ("current/.", "sources/release"),
        ("current/./", "sources/release"),
        ("current/", "current"),
        ("~/sources/", "sources"),
    ],
)
def test_read_paths_deduplicate_resolved_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str, kept: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    release = tmp_path / "sources" / "release"
    release.mkdir(parents=True)
    (tmp_path / "current").symlink_to(release, target_is_directory=True)
    given = spelling if spelling.startswith("~") else f"{tmp_path}/{spelling}"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"dir": str(tmp_path), "tasks": [], "read": [given]}))
    expected = tmp_path / kept
    assert load(path).read_paths([str(expected.resolve())]) == (str(expected),)


def test_read_paths_keep_first_citation_filename_and_distinct_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("Source text")
    cited = tmp_path / "issue-291.md"
    cited.symlink_to(source)
    other = tmp_path / "other.md"
    other.write_text("Source text")
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps({"dir": str(tmp_path), "tasks": [], "read": [str(cited)]})
    )
    assert load(path).read_paths([str(source), str(other)]) == (str(cited), str(other))
