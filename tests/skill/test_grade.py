"""Tests for the eval kit's blind per-expectation grading, `_runner/grade.py`."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

EVALS = Path(__file__).resolve().parents[2] / "skill" / "evals"
GRADE = EVALS / "_runner" / "grade.py"
TALLY = EVALS / "_runner" / "tally.py"

LLM = "---\ntype: llm\nfocus: trace\nweight: 1\n---\n\nClaude reports the keep.\n"
SCRIPT = (
    "---\ntype: script\nfocus: trace\nweight: 1\ncheck: order\n"
    "first: 'git log'\nthen: 'emb merge .*--verdict'\n---\n\n"
    "A `git log` call runs before the merge.\n"
)
SCRIPT_GRADERS = [
    "merge-and-cleanup-keeps/graders/02b-reads-convention-first.md",
    "claim-and-menu-writer/graders/06b-reads-convention-first.md",
    "flow-gap-check-first/graders/03b-reads-convention-first.md",
]


def load_grade() -> ModuleType:
    """Import grade.py as a module, with its folder on the path for tally."""
    spec = importlib.util.spec_from_file_location("eval_grade", GRADE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_grade(
    *arguments: Path | str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run grade.py and return its result."""
    return subprocess.run(
        [sys.executable, str(GRADE), *(str(item) for item in arguments)],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def bash(command: str) -> str:
    """Return one stream-json line for an assistant Bash call."""
    part = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return json.dumps({"type": "assistant", "message": {"content": [part]}})


def tool_result(text: str) -> str:
    """Return one stream-json line for a tool result."""
    part = {"type": "tool_result", "content": text}
    return json.dumps({"type": "user", "message": {"content": [part]}})


def make(
    tmp_path: Path, extra: dict[str, list[str]] | None = None
) -> tuple[Path, Path]:
    """Build a case with one llm and one script grader, and three runs."""
    case = tmp_path / "case"
    (case / "graders").mkdir(parents=True)
    (case / "graders" / "01-reports.md").write_text(LLM, encoding="utf-8")
    (case / "graders" / "01b-reads-first.md").write_text(SCRIPT, encoding="utf-8")
    out = tmp_path / "out"
    orders = {
        "skill/run-1": ["git log --oneline", "emb merge x --verdict keep"],
        "skill/run-2": ["emb merge x --verdict keep", "git log --oneline"],
        "base/run-1": ["emb merge x --verdict keep"],
    }
    for name, called in orders.items():
        run = out / name
        (run / "work").mkdir(parents=True)
        lines = [bash(command) for command in called]
        lines += (extra or {}).get(name, [])
        lines.append(json.dumps({"type": "result", "result": "started, no pause"}))
        lines.append(
            json.dumps({"type": "result", "result": f"done in {run}/work, go on?"})
        )
        lines.append(json.dumps({"type": "result", "result": "second turn done"}))
        (run / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (run / "calls.log").write_text(f"1 emb ground --dir {run}/work\n", "utf-8")
        (run / "owner.log").write_text(
            "owner reply 1: go on without a pause\n", encoding="utf-8"
        )
    return case, out


def verdict(out: Path, who: str, passed: dict[str, bool]) -> None:
    """Write one grader's verdict file for the llm grader, by real run name."""
    folder = out / "grading" / "01-reports"
    key = json.loads((out / "grading" / "keys" / "01-reports.json").read_text("utf-8"))
    graded = {
        ident: {"passed": passed[name], "evidence": f"{who} saw it"}
        for ident, name in key.items()
    }
    (folder / f"verdict-{who}.json").write_text(
        json.dumps({"reading": f"{who} reads it", "ambiguities": [], "runs": graded}),
        encoding="utf-8",
    )


ALL = {"skill/run-1": True, "skill/run-2": True, "base/run-1": True}


def test_batch_decides_script_graders_from_the_call_order(tmp_path: Path) -> None:
    """A script grader passes only when the first call precedes the then call."""
    case, out = make(tmp_path)
    result = run_grade("batch", case, out)
    assert result.returncode == 0, result.stderr
    decided = json.loads(
        (out / "grading" / "01b-reads-first" / "script.json").read_text("utf-8")
    )
    assert (decided["first"], decided["then"]) == ("git log", "emb merge .*--verdict")
    assert {name: item["passed"] for name, item in decided["runs"].items()} == {
        "skill/run-1": True,
        "skill/run-2": False,
        "base/run-1": False,
    }
    assert "no Bash call matches 'git log'" in decided["runs"]["base/run-1"]["evidence"]


def test_batch_hides_the_side_of_every_run(tmp_path: Path) -> None:
    """Run folders are shuffled and their files name neither side nor path."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out, "--seed", "7").returncode == 0
    folder = out / "grading" / "01-reports"
    runs = sorted(path.name for path in (folder / "runs").iterdir())
    assert runs == ["r01", "r02", "r03"]
    key = json.loads((out / "grading" / "keys" / "01-reports.json").read_text("utf-8"))
    assert sorted(key.values()) == ["base/run-1", "skill/run-1", "skill/run-2"]
    assert (folder / "expectation.md").read_text("utf-8") == (
        "Claude reports the keep.\n"
    )
    task = (folder / "task.md").read_text("utf-8")
    assert "verdict-<your name>.json" in task
    assert "Open\nnothing outside this folder" in task
    for path in (folder / "runs").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert str(out) not in text
            assert "skill/run-" not in text
            assert "base/run-" not in text
    trace = (folder / "runs" / "r01" / "trace.txt").read_text("utf-8")
    assert "TOOL CALL Bash" in trace
    assert "done in <RUN>/work, go on?" in trace


def test_a_path_cut_inside_a_tool_result_does_not_name_the_side(
    tmp_path: Path,
) -> None:
    """Paths are replaced before a long tool result is cut."""
    grade = load_grade()
    out = tmp_path / "out"
    extra: dict[str, list[str]] = {}
    for name in ALL:
        path = f"{out / name}/work/file.py"
        for cut in range(len(path)):
            pad = "x" * (grade.RESULT_LIMIT - cut)
            extra.setdefault(name, []).append(tool_result(pad + path + " tail"))
    case, out = make(tmp_path, extra)
    assert run_grade("batch", case, out).returncode == 0
    for path in (out / "grading" / "01-reports" / "runs").rglob("trace.txt"):
        text = path.read_text(encoding="utf-8")
        assert "[cut, " in text
        assert "/skill/" not in text
        assert "/base/" not in text
        assert "<OUT>" not in text


def test_a_relative_out_dir_replaces_no_part_of_a_word(tmp_path: Path) -> None:
    """Only absolute paths are replaced, so `out` in a word stays."""
    case, out = make(tmp_path)
    assert run_grade("batch", "case", "out", cwd=tmp_path).returncode == 0
    run = out / "grading" / "01-reports" / "runs" / "r01"
    assert (run / "owner.log").read_text("utf-8") == (
        "owner reply 1: go on without a pause\n"
    )
    assert "<RUN>/work" in (run / "calls.log").read_text("utf-8")
    trace = (run / "trace.txt").read_text("utf-8")
    assert "=== OWNER REPLY 1:\nowner reply 1: go on without a pause" in trace
    assert trace.index("started, no pause") < trace.index("done in <RUN>/work")
    assert trace.index("go on?") < trace.index("=== OWNER REPLY 1")
    assert trace.index("=== OWNER REPLY 1") < trace.index("second turn done")
    assert trace.count("=== OWNER REPLY") == 1


def test_the_dashed_form_of_a_run_path_is_replaced(tmp_path: Path) -> None:
    """A scratch folder named after the run's work folder does not name the side."""
    out = tmp_path / "out"
    extra = {
        name: [
            tool_result(
                "/tmp/claude-1000/"
                + f"{out / name}/work".replace("/", "-")
                .replace("_", "-")
                .replace(".", "-")
                + "/abc/scratchpad/plan.json"
            )
        ]
        for name in ALL
    }
    case, out = make(tmp_path, extra)
    assert run_grade("batch", case, out).returncode == 0
    for path in (out / "grading" / "01-reports" / "runs").rglob("trace.txt"):
        text = path.read_text(encoding="utf-8")
        assert "/tmp/claude-1000/<RUN>-work/abc/scratchpad/plan.json" in text
        assert "skill-run" not in text
        assert "base-run" not in text


def test_apply_writes_grading_when_two_graders_agree(tmp_path: Path) -> None:
    """Agreed verdicts become each run's grading.json, in grader file order."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    passed = {"skill/run-1": True, "skill/run-2": True, "base/run-1": False}
    verdict(out, "one", passed)
    verdict(out, "two", passed)
    result = run_grade("apply", case, out)
    assert result.returncode == 0, result.stderr
    assert "01-reports reading, one: one reads it" in result.stdout
    assert "01-reports reading, two: two reads it" in result.stdout
    graded = json.loads((out / "skill" / "run-2" / "grading.json").read_text("utf-8"))
    first, second = graded["expectations"]
    assert first["text"] == "Claude reports the keep."
    assert first["passed"] is True
    assert first["evidence"] == "one: one saw it | two: two saw it"
    assert second["text"] == "A `git log` call runs before the merge."
    assert second["passed"] is False
    table = subprocess.run(
        [sys.executable, str(TALLY), str(case), str(out)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = [line.split() for line in table.splitlines()]
    assert ["01-reports", "2/2", "0/1"] in rows
    assert ["01b-reads-first", "1/2", "0/1"] in rows
    assert ["all", "graders", "1/2", "0/1"] in rows


def test_apply_refuses_disagreement_and_writes_nothing(tmp_path: Path) -> None:
    """A run two graders judge differently is listed, with exit 65."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    verdict(out, "one", ALL)
    verdict(out, "two", {**ALL, "skill/run-2": False})
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "01-reports: graders disagree on skill/run-2" in result.stderr
    assert "one pass, two fail" in result.stderr
    assert "no grading.json written" in result.stderr
    assert not list(out.rglob("grading.json"))


def test_a_new_batch_removes_earlier_grades(tmp_path: Path) -> None:
    """Grades from an earlier round cannot be tallied after a new batch."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    verdict(out, "one", ALL)
    verdict(out, "two", ALL)
    assert run_grade("apply", case, out).returncode == 0
    assert len(list(out.rglob("grading.json"))) == 3
    assert run_grade("batch", case, out).returncode == 0
    assert not list(out.rglob("grading.json"))
    assert not list(out.rglob("verdict-*.json"))


def test_apply_refuses_a_grader_changed_after_batch(tmp_path: Path) -> None:
    """Verdicts on an earlier text or pattern are not applied to a new one."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    verdict(out, "one", ALL)
    verdict(out, "two", ALL)
    (case / "graders" / "01-reports.md").write_text(
        LLM.replace("the keep", "the keep and the tip"), encoding="utf-8"
    )
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "01-reports: its text changed after batch" in result.stderr
    (case / "graders" / "01-reports.md").write_text(LLM, encoding="utf-8")
    (case / "graders" / "01b-reads-first.md").write_text(
        SCRIPT.replace("'git log'", "'git show'"), encoding="utf-8"
    )
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "01b-reads-first: its patterns changed after batch" in result.stderr
    assert not list(out.rglob("grading.json"))


def test_apply_needs_the_asked_number_of_verdict_files(tmp_path: Path) -> None:
    """One verdict file is not enough by default, and is with --graders 1."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    verdict(out, "one", ALL)
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "01-reports: has 1 of 2 verdict files" in result.stderr
    assert run_grade("apply", case, out, "--graders", "1").returncode == 0


@pytest.mark.parametrize(
    "document",
    [
        {"reading": "r", "ambiguities": [], "runs": {"r01": {"passed": True}}},
        {"ambiguities": [], "runs": "all"},
        {"reading": "  ", "ambiguities": [], "runs": "all"},
        {"reading": "r", "runs": "all"},
        {"reading": "r", "ambiguities": [], "runs": "one"},
    ],
)
def test_apply_refuses_an_incomplete_verdict_file(
    tmp_path: Path, document: dict
) -> None:
    """A verdict needs a reading, ambiguities and a true or false for every run."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    folder = out / "grading" / "01-reports"
    full = {f"r0{n}": {"passed": True, "evidence": "e"} for n in (1, 2, 3)}
    if document["runs"] == "all":
        document = {**document, "runs": full}
    elif document["runs"] == "one":
        document = {**document, "runs": {**full, "r02": {"passed": 1}}}
    for who in ("one", "two"):
        (folder / f"verdict-{who}.json").write_text(json.dumps(document), "utf-8")
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "unreadable or incomplete" in result.stderr
    assert not list(out.rglob("grading.json"))


def test_grade_exit_codes_for_usage_graders_and_runs(tmp_path: Path) -> None:
    """Usage errors exit 64; no graders or no runs exits 66."""
    case, out = make(tmp_path)
    empty = tmp_path / "empty"
    (empty / "graders").mkdir(parents=True)
    assert run_grade("batch", empty, out).returncode == 66
    assert run_grade("batch", case, tmp_path / "missing").returncode == 66
    assert run_grade("apply", case, out).returncode == 65
    assert run_grade("grade", case, out).returncode == 64
    assert run_grade("batch", case).returncode == 64
    assert run_grade("apply", case, out, "--graders", "0").returncode == 64
    assert run_grade("--help").returncode == 0


def test_a_script_grader_needs_its_patterns(tmp_path: Path) -> None:
    """A script grader without first and then is a usage error."""
    case, out = make(tmp_path)
    (case / "graders" / "01b-reads-first.md").write_text(
        "---\ntype: script\ncheck: order\n---\n\nNo patterns.\n", encoding="utf-8"
    )
    result = run_grade("batch", case, out)
    assert result.returncode == 64
    assert "needs check: order with first and then" in result.stderr


def test_a_run_without_a_trace_is_batched_and_fails_the_script(
    tmp_path: Path,
) -> None:
    """A run that never wrote trace.jsonl does not stop the batch."""
    case, out = make(tmp_path)
    (out / "skill" / "run-1" / "trace.jsonl").unlink()
    result = run_grade("batch", case, out)
    assert result.returncode == 0, result.stderr
    decided = json.loads(
        (out / "grading" / "01b-reads-first" / "script.json").read_text("utf-8")
    )
    assert decided["runs"]["skill/run-1"]["passed"] is False


def test_batch_copies_named_files_and_a_command_result(tmp_path: Path) -> None:
    """An llm grader's files and command reach each blind run folder."""
    case, out = make(tmp_path)
    (case / "graders" / "01-reports.md").write_text(
        LLM.replace(
            "weight: 1\n",
            "weight: 1\nfiles: .eval/plan.json, .eval/none.json\n"
            "command: python3 -I .eval/check.py\n",
        ),
        encoding="utf-8",
    )
    for name in ALL:
        work = out / name / "work" / ".eval"
        work.mkdir()
        (work / "plan.json").write_text(f'{{"dir": "{out / name}/work"}}\n', "utf-8")
        (work / "check.py").write_text(
            "import pathlib, sys\nprint('checked', pathlib.Path.cwd())\nsys.exit(3)\n",
            encoding="utf-8",
        )
    assert run_grade("batch", case, out).returncode == 0
    run = out / "grading" / "01-reports" / "runs" / "r02"
    assert (run / "files" / "plan.json").read_text("utf-8") == (
        '{"dir": "<RUN>/work"}\n'
    )
    assert "(the run has no .eval/none.json)" in (
        run / "files" / "none.json"
    ).read_text("utf-8")
    assert (run / "command.txt").read_text("utf-8") == (
        "$ python3 -I .eval/check.py\nexit 3\nchecked <RUN>/work\n"
    )


@pytest.mark.parametrize("grader", SCRIPT_GRADERS)
def test_the_convention_checks_follow_the_command_not_its_spelling(
    grader: str,
) -> None:
    """The three read-first graders accept real reads and refuse mentions."""
    grade = load_grade()
    meta, _ = grade.front(EVALS / grader)
    first, then = meta["first"], meta["then"]
    target = (
        "emb flow --plan p.json \\\n  --dry-run --claim c"
        if "flow" in grader
        else "emb merge ember/codex/x --into main \\\n  --verdict keep --note Keep"
    )
    for read in (
        "git log --oneline -10 claude/report-since",
        "git -C /repo log --oneline -10",
        "git --no-pager log -5",
        "cd /repo && git log --oneline",
        "ls data; git log --oneline",
    ):
        assert grade.order(first, then, [read, target])[0], read
    assert grade.order(first, then, ["git log", f"/usr/bin/{target}"])[0]
    for not_a_read in (
        "cat > brief.md <<'B'\nRun no git command such as `git log` here.\nB",
        "printf '%s\\n' 'read git log first'",
        "git status --short",
        "git show --stat HEAD",
    ):
        assert not grade.order(first, then, [not_a_read, target])[0], not_a_read
    assert not grade.order(first, then, [target, "git log --oneline"])[0]
    helped = target.split(" \\\n")[0] + " --help"
    passed, evidence = grade.order(first, then, [helped, "git log", target])
    assert passed, evidence


def test_the_fixture_gives_the_convention_only_in_its_history() -> None:
    """The read-first graders rest on these three facts of the fixture."""
    fixture = EVALS / "_stubs" / "fixture"
    rules = (fixture / "AGENTS.md").read_text("utf-8").lower()
    for word in ("commit message", "convention", "subject"):
        assert word not in rules, word
    assert not list(fixture.glob("CONTRIBUTING*"))
    install = (EVALS / "_stubs" / "install.sh").read_text("utf-8")
    assert "Add the order report tool and its tests" in install
