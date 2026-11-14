"""Tests for the eval kit's blind per-expectation grading, `_runner/grade.py`."""

import json
import subprocess
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[2] / "skill" / "evals"
GRADE = EVALS / "_runner" / "grade.py"
TALLY = EVALS / "_runner" / "tally.py"

LLM = "---\ntype: llm\nfocus: trace\nweight: 1\n---\n\nClaude reports the keep.\n"
SCRIPT = (
    "---\ntype: script\nfocus: trace\nweight: 1\ncheck: order\n"
    "first: 'git log'\nthen: 'emb merge .*--verdict'\n---\n\n"
    "A `git log` call runs before the merge.\n"
)


def run_grade(*arguments: Path | str) -> subprocess.CompletedProcess[str]:
    """Run grade.py and return its result."""
    return subprocess.run(
        [sys.executable, str(GRADE), *(str(item) for item in arguments)],
        capture_output=True,
        text=True,
        check=False,
    )


def bash(command: str) -> str:
    """Return one stream-json line for an assistant Bash call."""
    part = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return json.dumps({"type": "assistant", "message": {"content": [part]}})


def make(tmp_path: Path) -> tuple[Path, Path]:
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
        run.mkdir(parents=True)
        lines = [bash(command) for command in called]
        lines.append(json.dumps({"type": "result", "result": f"done in {run}/work"}))
        (run / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (run / "calls.log").write_text(f"1 emb ground --dir {run}/work\n", "utf-8")
        (run / "owner.log").write_text("", encoding="utf-8")
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
        json.dumps({"reading": "one reading", "ambiguities": [], "runs": graded}),
        encoding="utf-8",
    )


def test_batch_decides_script_graders_from_the_call_order(tmp_path: Path) -> None:
    """A script grader passes only when the first call precedes the then call."""
    case, out = make(tmp_path)
    result = run_grade("batch", case, out)
    assert result.returncode == 0, result.stderr
    decided = json.loads(
        (out / "grading" / "01b-reads-first" / "script.json").read_text("utf-8")
    )
    assert {name: item["passed"] for name, item in decided.items()} == {
        "skill/run-1": True,
        "skill/run-2": False,
        "base/run-1": False,
    }
    assert "no Bash call matches 'git log'" in decided["base/run-1"]["evidence"]


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
    assert "verdict-<your name>.json" in (folder / "task.md").read_text("utf-8")
    for path in (folder / "runs").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert str(out) not in text
            assert "skill/run-" not in text
            assert "base/run-" not in text
    trace = (folder / "runs" / "r01" / "trace.txt").read_text("utf-8")
    assert "TOOL CALL Bash" in trace
    assert "done in <RUN>/work" in trace


def test_apply_writes_grading_when_two_graders_agree(tmp_path: Path) -> None:
    """Agreed verdicts become each run's grading.json, in grader file order."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    passed = {"skill/run-1": True, "skill/run-2": True, "base/run-1": False}
    verdict(out, "one", passed)
    verdict(out, "two", passed)
    result = run_grade("apply", case, out)
    assert result.returncode == 0, result.stderr
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
    verdict(out, "one", {"skill/run-1": True, "skill/run-2": True, "base/run-1": True})
    verdict(out, "two", {"skill/run-1": True, "skill/run-2": False, "base/run-1": True})
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "01-reports: graders disagree on skill/run-2" in result.stderr
    assert "one pass, two fail" in result.stderr
    assert "no grading.json written" in result.stderr
    assert not list(out.rglob("grading.json"))


def test_apply_needs_the_asked_number_of_verdict_files(tmp_path: Path) -> None:
    """One verdict file is not enough by default, and is with --graders 1."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    verdict(out, "one", {"skill/run-1": True, "skill/run-2": True, "base/run-1": True})
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "01-reports: has 1 of 2 verdict files" in result.stderr
    assert run_grade("apply", case, out, "--graders", "1").returncode == 0


def test_apply_refuses_an_incomplete_verdict_file(tmp_path: Path) -> None:
    """A verdict file that leaves a run out does not count."""
    case, out = make(tmp_path)
    assert run_grade("batch", case, out).returncode == 0
    folder = out / "grading" / "01-reports"
    for who in ("one", "two"):
        (folder / f"verdict-{who}.json").write_text(
            json.dumps({"reading": "r", "runs": {"r01": {"passed": True}}}),
            encoding="utf-8",
        )
    result = run_grade("apply", case, out)
    assert result.returncode == 65
    assert "unreadable or incomplete" in result.stderr


def test_grade_needs_graders_and_runs(tmp_path: Path) -> None:
    """A case without graders or an out dir without runs exits 66."""
    case, out = make(tmp_path)
    empty = tmp_path / "empty"
    (empty / "graders").mkdir(parents=True)
    assert run_grade("batch", empty, out).returncode == 66
    assert run_grade("batch", case, tmp_path / "missing").returncode == 66
    assert run_grade("apply", case, out).returncode == 65


def test_a_script_grader_needs_its_patterns(tmp_path: Path) -> None:
    """A script grader without first and then is a usage error."""
    case, out = make(tmp_path)
    (case / "graders" / "01b-reads-first.md").write_text(
        "---\ntype: script\ncheck: order\n---\n\nNo patterns.\n", encoding="utf-8"
    )
    result = run_grade("batch", case, out)
    assert result.returncode == 64
    assert "needs check: order with first and then" in result.stderr
