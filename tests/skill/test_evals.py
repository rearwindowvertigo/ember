import dataclasses
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ember import __version__, choices, paths, skill
from ember.cli import main
from ember.gapcheck import previous
from ember.runs import exits, record, steps

TOP = Path(__file__).resolve().parents[2]
SKILL = TOP / "skill"
EVALS = SKILL / "evals"
RESULTS = EVALS / "results"
STUBS = EVALS / "_stubs"
RUNNER = EVALS / "_runner" / "run_case.sh"

CASES = (
    "claim-and-menu-writer",
    "small-change-no-worker",
    "connector-work-goes-to-subagent",
    "refuses-owner-setting",
    "claim-lost-then-gone",
    "claim-stale-skill",
    "claim-unknown-skill",
    "send-refused-foreign-messages",
    "owner-line-not-run",
    "merge-and-cleanup-keeps",
    "turn-results-counted",
    "sandbox-denied-paths",
    "provider-capacity",
    "provider-usage-limit",
    "provider-outage",
    "provider-uncertain",
    "flow-gap-check-first",
    "flow-shared-document-task",
    "owner-away-named-workers",
    "flow-lead-stop-waits",
    "panel-named-members-compared",
    "panel-progress-reported",
)
FLOW_CASES = (
    "flow-gap-check-first",
    "owner-away-named-workers",
    "flow-lead-stop-waits",
)

REVIEW_BAR = """\
Review the work against its task and its goals, as a careful second engineer would: is it correct for the cases the task and its failure model cover, is anything it needs missing, is there a clearly simpler way, and is anything important overlooked. Where there is no failure model, judge by the task text. Do not search every combination of inputs and timing; report a case only when it matters by the rules below.

Give each finding a severity:
- must: blocks the work. Write a scenario: a concrete case that can happen in normal use, or any case that can lose work or data, and the harm it causes, such as a wrong result, lost work, a crash, a broken caller or test, a security hole, or a contradiction of what was decided. A must without a scenario is read as should.
- should: an improvement, or a case outside normal use. It does not block; the lead reads it.
- lead: something only the lead or the owner can settle: behaviour the task and the decisions given do not define, or a change outside what the writer may touch. Name the decision in the problem. It does not go to the writer.

Do not ask for behaviour the failure model or the decisions given already settle."""  # noqa: E501

FORBIDDEN = [
    re.compile(r"delegat[a-z]*", re.IGNORECASE),
    *(
        re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)
        for name in (
            "crewlead",
            "crewlead.db",
            "cx-flow",
            "cx-sweep",
            "oc-panel",
            "peers",
            "machines",
            "kitty",
            "MCP tools",
            "seat usage",
            "--owner-said",
            "--trust",
            "--result",
            "--task",
            "--message",
            "--no-search",
            "--prune",
        )
    ),
]

HELP_COMMANDS = (
    "",
    "choices",
    "engines",
    "run",
    "gapcheck",
    "flow",
    "panel",
    "ground",
    "merge",
    "cleanup",
    "deps",
    "app",
    "skill",
    "skill install",
    "skill uninstall",
    "skill status",
)


# The small reader for the cases' YAML: `key: value`, `[a, b]` lists, `key: |`
# blocks and one level of nested keys.


def parse_simple(lines: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        assert not line.startswith(" "), f"unexpected indent: {line!r}"
        key, colon, value = line.partition(":")
        assert colon, f"not key: value: {line!r}"
        value = value.strip()
        if value in ("|", ""):
            block = []
            while index < len(lines) and (
                lines[index].startswith("  ") or not lines[index].strip()
            ):
                block.append(lines[index][2:])
                index += 1
            result[key] = (
                "\n".join(block).strip() + "\n" if value == "|" else parse_simple(block)
            )
        elif value.startswith("[") and value.endswith("]"):
            result[key] = [item.strip() for item in value[1:-1].split(",") if item]
        else:
            result[key] = value.strip("\"'")
    return result


def front_matter(path: Path) -> tuple[dict[str, Any], str]:
    lines = path.read_text().splitlines()
    assert lines[0] == "---", f"{path} has no front matter"
    end = lines.index("---", 1)
    return parse_simple(lines[1:end]), "\n".join(lines[end + 1 :])


def test_parse_simple() -> None:
    assert parse_simple(
        ["a: 1", 'b: "x"', "c: [d, e]", "f: |", "  one", "", "  two", "g:", "  h: i"]
    ) == {"a": "1", "b": "x", "c": ["d", "e"], "f": "one\n\ntwo\n", "g": {"h": "i"}}


# Case files


def test_the_expected_cases_and_no_others() -> None:
    found = {
        p.name
        for p in EVALS.iterdir()
        if p.is_dir() and p.name[0] not in "_." and p != RESULTS
    }
    assert found == set(CASES)


@pytest.mark.parametrize("case", CASES)
def test_case_files_parse(case: str) -> None:
    folder = EVALS / case
    for name in ("prompt.md", "case.yaml", "place-files.sh"):
        assert (folder / name).is_file(), name
    assert os.access(folder / "place-files.sh", os.X_OK)
    settings, body = front_matter(folder / "prompt.md")
    assert set(settings) >= {"tags", "max_turns", "timeout_seconds", "allowed_tools"}
    assert "Skill" in settings["allowed_tools"] and "Bash" in settings["allowed_tools"]
    assert body.strip()
    data = parse_simple((folder / "case.yaml").read_text().splitlines())
    assert data["schema_version"] == "1.1"
    assert data["name"] == case
    assert data["expected_outcome"].strip()
    assert data["context"] == {"scaffold_script": "place-files.sh"}
    graders = sorted((folder / "graders").glob("*.md"))
    assert graders
    for grader in graders:
        meta, text = front_matter(grader)
        assert meta == {"type": "llm", "focus": "trace", "weight": "1"}, grader
        assert text.strip()
    replies = folder / "owner-replies.md"
    if replies.exists():
        assert re.search(r"^1\. [^:\n]+: \S", replies.read_text(), re.MULTILINE)


def test_shell_scripts_parse() -> None:
    scripts = [
        RUNNER,
        STUBS / "install.sh",
        STUBS / "generate.sh",
        STUBS / "bin" / "emb",
    ]
    scripts += [EVALS / case / "place-files.sh" for case in CASES]
    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_help_reply_matches_the_parser(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    generated_eval_fixtures: Path,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    expected = ""
    for command in HELP_COMMANDS:
        with pytest.raises(SystemExit):
            main([*command.split(), "--help"])
        expected += f"==> {' '.join(['emb', *command.split()])} --help <==\n"
        expected += capsys.readouterr().out
    help_file = generated_eval_fixtures / "_stubs/replies/help.out"
    assert help_file.read_text() == expected


# The stub kit, in a case's working directory


@pytest.fixture
def env(tmp_path: Path, generated_eval_fixtures: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "EMBER_EVAL_FIXTURES": str(generated_eval_fixtures),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(runtime),
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C.UTF-8",
    }


class Case:
    def __init__(self, name: str, work: Path, env: dict[str, str]) -> None:
        self.work = work
        self.eval = work / ".ember-eval"
        self.env = {
            **env,
            "EMBER_EVAL_DIR": str(self.eval),
            "PATH": f"{self.eval / 'bin'}:{env['PATH']}",
        }
        work.mkdir(parents=True)
        subprocess.run(
            ["bash", str(EVALS / name / "place-files.sh")],
            cwd=work,
            env=env,
            check=True,
            capture_output=True,
        )

    def emb(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["emb", *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def calls(self) -> list[dict[str, Any]]:
        text = (self.eval / "calls.log").read_text()
        return [json.loads(line) for line in text.splitlines()]

    def brief(self, text: str = "Add --since to the report\n\nDetails.\n") -> str:
        path = self.work.parent / "brief.md"
        path.write_text(text)
        return str(path)


def worktree_path(work: Case, worktree_id: str) -> Path:
    """The path the stub, as Ember, gives a run's or flow's worktree."""
    return paths.worktree_path(worktree_id, work.env)


def skill_script(work: Case) -> str:
    """The quoted `verify_branch.py` path the real command prints."""
    return shlex.quote(str(skill.link_path(work.env) / "scripts" / "verify_branch.py"))


def case(name: str, tmp_path: Path, env: dict[str, str]) -> Case:
    return Case(name, tmp_path / "work", env)


def test_install_sets_up_the_repository(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    assert work.git("branch", "--show-current") == "claude/report-since"
    assert work.git("remote", "get-url", "origin") == (
        "git@github.com:example/order-tools.git"
    )
    assert work.git("config", "core.sshCommand") == "false"
    assert work.git("status", "--porcelain") == ""
    assert work.git("log", "--format=%s", "main") == (
        "Add the order report tool and its tests"
    )
    for name in ("README.md", "AGENTS.md", "CLAUDE.md", "tools/report.py"):
        assert (work.work / name).is_file()
    for name in ("bin/emb", "bin/stub.py", "replies/menu.json", "env.sh"):
        assert (work.eval / name).exists()
    assert (work.eval / "calls.log").read_text() == ""
    assert (work.eval / "replies" / "run-start.reply").exists()


@pytest.mark.parametrize("layout", ["shared", "split", "missing-dependency"])
def test_shared_document_case_checks_submitted_plan(
    tmp_path: Path, env: dict[str, str], layout: str
) -> None:
    work = case("flow-shared-document-task", tmp_path, env)
    names = ["totals", "items", "summary"]
    tasks = [
        {
            "id": name,
            "subject": f"feat: add {name} helper",
            "task": f"Implement and test {name}; reference edits belong to reference.",
            "files": [f"tools/{name}.py", f"tests/test_{name}.py"]
            + (["docs/reference.md"] if layout == "shared" else []),
        }
        for name in names
    ]
    if layout != "shared":
        tasks.append(
            {
                "id": "reference",
                "subject": "docs: describe the order helpers",
                "task": "Describe all three helpers after their implementations.",
                "files": ["docs/reference.md"],
                "after": names if layout == "split" else names[:-1],
            }
        )
    document = {
        "dir": str(work.work),
        "failure_model": "Empty rows return zero totals and no item names.",
        "check": ["python3", "-m", "unittest", "discover", "-s", "tests"],
        "tasks": tasks,
    }
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(document))
    result = work.emb(
        "flow",
        "--plan",
        str(source),
        "--dry-run",
        "--json",
        "--writer",
        "codex:gpt-6.1-sol@high",
        "--reviewer",
        "codex:gpt-6.1-sol-mini@medium",
        "--claim",
        "shared-document-eval",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    expected = (
        [[name] for name in names] if layout == "shared" else [names, ["reference"]]
    )
    assert [set(wave) for wave in report["waves"]] == [set(wave) for wave in expected]
    assert json.loads((work.eval / "flow-plan.json").read_text()) == document
    if layout != "shared":
        assert report["warnings"] == ["reference: no test file in its files"]
        assert "WARNING: reference: no test file" in result.stderr
    # The checker grades the plan the lead submitted, even after its scratch
    # file is gone; it does not grade a fixture or the lead's final report.
    source.unlink()
    checked = subprocess.run(
        [sys.executable, "-I", str(work.eval / "check-plan.py")],
        cwd=work.work,
        env=work.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (checked.returncode == 0) == (layout == "split"), checked.stderr
    assert [call["route"] for call in work.calls()] == ["flow-dry-run"]


@pytest.mark.parametrize(
    ("args", "route", "code", "check"),
    [
        (["choices", "--claim", "--id", "run-abc123"], "choices-claim", 0, '"none"'),
        (["choices", "--json", "--id", "run-abc123"], "choices-read-id", 0, "set"),
        (["choices", "--json"], "choices-read", 0, '"claim": null'),
        (["choices", "--finish", "run-abc123"], "choices-finish", 0, '"gone"'),
        (["choices", "--set", "single.writer=ask"], "choices-owner", 77, ""),
        (["choices", "--clear-once"], "choices-owner", 77, ""),
        (["engines", "--json"], "engines", 0, '"connected": true'),
        (["engines"], "engines", 0, "codex: connected"),
        (["engines", "--models", "codex", "--json"], "engines-models", 0, "sol"),
        (["ground", "--json"], "ground", 0, '"name": "claude/report-since"'),
        (["ground"], "ground", 0, "branch: claude/report-since at "),
        (["deps"], "deps", 0, "Codex: current"),
        (["skill", "status"], "skill", 0, "state: installed"),
        (["run", "--help"], "help", 0, "usage: emb run"),
        (["skill", "install", "--help"], "help", 0, "usage: emb skill install"),
        (["--help"], "help", 0, "usage: emb [-h]"),
        (["cleanup", "--merged-into", "claude/report-since"], "cleanup", 0, ""),
        (["nope"], "unknown", 64, ""),
    ],
)
def test_route_logs_and_replies(
    tmp_path: Path,
    env: dict[str, str],
    args: list[str],
    route: str,
    code: int,
    check: str,
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    result = work.emb(*args)
    assert result.returncode == code, result.stderr
    assert check in result.stdout
    [call] = work.calls()
    assert call["argv"] == args
    assert call["route"] == route
    assert call["exit"] == code
    assert call["seq"] == 1
    assert call["cwd"] == str(work.work)
    assert set(call) == {"seq", "at", "argv", "cwd", "route", "reply", "exit"}
    if route == "choices-claim":
        assert json.loads(result.stdout)["claim"] == {
            "id": "run-abc123",
            "status": "none",
        }
    if route == "choices-owner":
        assert result.stderr == "emb choices: owner options require a terminal\n"
    if route == "ground":
        assert "dirty: no" in result.stdout or '"value": false' in result.stdout


def test_run_start_commits_on_its_branch(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    trailer = "Co-Authored-By: Claude <noreply@anthropic.com>"
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--trailer", trailer, "--claim", "run-abc123",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    run_id = lines[0].removeprefix("run ")
    assert re.fullmatch(r"codex-\d{8}-\d{6}-[0-9a-f]{6}", run_id)
    branch = f"ember/codex/{run_id}"
    worktree = worktree_path(work, run_id)
    commit = work.git("rev-parse", branch)
    worker_line, next_line = result.stderr.splitlines()
    assert worker_line == (
        "Team, Single tasks Writer: codex gpt-6.1-sol · high (single.writer)"
    )
    assert next_line == (
        f"next: python3 {skill_script(work)} --repo {work.work} "
        f"--base claude/report-since --branch {branch} --worktree {worktree} "
        "--expect-files 2, then emb merge "
        f"{branch} --into claude/report-since --verdict keep "
        "--note '<verdict>' --subject '<subject>'"
    )
    reply = work.eval / "runs" / run_id / "turn-1.md"
    assert lines[1] == "codex gpt-6.1-sol · high · 240s · exit 0 · 2 files · turn 1"
    assert lines[2:9] == [
        f"run {run_id}",
        f"worktree {worktree}",
        f"branch {branch}",
        f"commit {commit}",
        "window -",
        f"reply {reply}",
        "--- reply ---",
    ]
    assert "Not covered: nothing." in result.stdout
    assert reply.read_text().startswith("Added `--since YYYY-MM-DD`")
    assert work.git("log", "-1", "--format=%B", branch) == (
        f"Add --since to the report\n\n{trailer}"
    )
    assert work.git("diff", "--name-only", f"claude/report-since...{branch}") == (
        "tests/test_report.py\ntools/report.py"
    )
    assert work.git("branch", "--show-current", cwd=worktree) == branch
    assert work.git("status", "--porcelain") == ""
    assert not (work.work / ".ember").exists()
    assert ".ember/" not in (work.work / ".git" / "info" / "exclude").read_text()
    assert stat.S_IMODE(worktree.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(worktree.parent.parent.stat().st_mode) == 0o700
    state = json.loads((work.eval / "state.json").read_text())
    assert state["runs"][run_id]["claim"] == "run-abc123"
    status = work.emb("run", "--status", run_id, "--json")
    document = json.loads(status.stdout)
    assert document["branch"] == branch
    assert document["worktree"] == str(worktree)
    assert document["main"] == str(work.work)
    assert document["common"] == str(work.work / ".git")
    assert document["last_exit"] == 0
    [turn] = document["turns"]
    assert turn == {
        "turn": 1,
        "exit": 0,
        "state": "done",
        "reason": None,
        "provider_error": False,
        "signed_out": False,
        "incomplete": None,
        "seconds": 240,
    }
    assert json.loads(work.emb("run", "--status", "--json").stdout)[0]["turns"] == [
        turn
    ]


@pytest.mark.parametrize(
    ("options", "row", "model", "effort", "model_source", "effort_source"),
    [
        ([], "Writer", "gpt-6.1-sol", "high", "single.writer", "single.writer"),
        (
            ["--read-only"],
            "Reviewer",
            "gpt-6.1-sol-mini",
            "medium",
            "single.reviewer",
            "single.reviewer",
        ),
        (
            ["--model", "gpt-6.1-sol"],
            "Writer",
            "gpt-6.1-sol",
            "high",
            "--model",
            "single.writer",
        ),
        (
            ["--effort", "low"],
            "Writer",
            "gpt-6.1-sol",
            "low",
            "single.writer",
            "--effort",
        ),
        (
            ["--model", "gpt-6-astra", "--effort", "high"],
            "Writer",
            "gpt-6-astra",
            "high",
            "--model",
            "--effort",
        ),
    ],
)
def test_run_preview_then_start(
    tmp_path: Path,
    env: dict[str, str],
    options: list[str],
    row: str,
    model: str,
    effort: str,
    model_source: str,
    effort_source: str,
    generated_eval_fixtures: Path,
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    args = [
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--claim", "preview-eval", "--trailer", "Co-Authored-By: Eval <eval@invalid>",
        *options,
    ]  # fmt: skip
    branches = work.git("branch", "--list")
    trees = work.git("worktree", "list", "--porcelain")
    result = work.emb(*args, "--dry-run", "--json")
    assert result.returncode == 0 and result.stderr == ""
    expected = {
        "screen": "Team",
        "row": f"Single tasks {row}",
        "engine": "codex",
        "model": model,
        "effort": effort,
        "model_source": model_source,
        "effort_source": effort_source,
    }
    assert json.loads(result.stdout) == expected
    source = (
        model_source
        if model_source == effort_source
        else f"model: {model_source}, effort: {effort_source}"
    )
    line = f"Team, Single tasks {row}: codex {model} · {effort} ({source})\n"
    preview = work.emb(*args, "--dry-run")
    assert preview.returncode == 0 and preview.stderr == ""
    assert preview.stdout == line
    if not options:
        assert (
            preview.stdout
            == (generated_eval_fixtures / "_stubs/replies/run-dry-run.out").read_text()
        )
        assert expected == json.loads(
            (generated_eval_fixtures / "_stubs/replies/run-dry-run.json").read_text()
        )
    assert work.git("branch", "--list") == branches
    assert work.git("worktree", "list", "--porcelain") == trees
    assert not (work.eval / "runs").exists()
    assert not paths.worktrees_dir(env).exists()
    assert not Path(env["XDG_CACHE_HOME"]).exists()
    state = json.loads((work.eval / "state.json").read_text())
    assert state["runs"] == {}
    started = work.emb(*args)
    assert started.returncode == 0
    assert started.stdout.startswith("run codex-")
    assert started.stderr.startswith(line)
    assert (stderr_next(started) or "").startswith("next: ")
    assert [call["route"] for call in work.calls()] == [
        "run-dry-run",
        "run-dry-run",
        "run-start",
    ]


def test_read_only_start_makes_detached_worktree(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--read-only",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "codex gpt-6.1-sol-mini · medium · " in result.stdout
    assert "branch -\n" in result.stdout
    assert work.git("branch", "--list", "ember/*") == ""
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    assert result.stderr == (
        "Team, Single tasks Reviewer: codex gpt-6.1-sol-mini · medium "
        "(single.reviewer)\n"
        f"next: emb run --close {run_id}\n"
    )
    tree = worktree_path(work, run_id)
    assert tree.is_dir() and tree != work.work
    assert work.git("branch", "--show-current", cwd=tree) == ""
    assert work.git("rev-parse", "HEAD", cwd=tree) == work.git("rev-parse", "HEAD")
    document = json.loads(work.emb("run", "--status", run_id, "--json").stdout)
    assert document["detached"] and document["worktree"] == str(tree)
    closed = work.emb("run", "--close", run_id)
    assert closed.returncode == 0 and closed.stderr == ""
    assert not tree.exists()
    assert str(tree) not in work.git("worktree", "list")
    assert work.emb("run", "--close", run_id).returncode == 0


@pytest.mark.parametrize("read_only", [False, True])
def test_run_records_main_checkout_from_linked_source(
    tmp_path: Path, env: dict[str, str], read_only: bool
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    linked = tmp_path / "linked"
    work.git("worktree", "add", "-b", "source", str(linked))
    result = work.emb(
        "run", "--engine", "codex", "--dir", str(linked),
        "--task-file", work.brief(), *(["--read-only"] if read_only else []),
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    document = json.loads(work.emb("run", "--status", run_id, "--json").stdout)
    assert document["repo"] == str(linked)
    assert document["main"] == str(work.work)
    assert document["common"] == str(work.work / ".git")
    assert document["worktree"] == str(worktree_path(work, run_id))
    assert document["detached"] == read_only


@pytest.mark.parametrize("change", ["modified", "deleted", "renamed", "untracked"])
def test_read_only_warns_for_uncommitted_paths_and_keeps_its_snapshot(
    tmp_path: Path, env: dict[str, str], change: str
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    source = work.work / "README.md"
    committed = source.read_text()
    names = ["README.md"]
    if change == "modified":
        source.write_text("Uncommitted owner edit.\n")
    elif change == "deleted":
        source.unlink()
    elif change == "renamed":
        work.git("mv", "README.md", "renamed.md")
        names.append("renamed.md")
    else:
        (work.work / "notes.txt").write_text("Uncommitted owner note.\n")
        names = ["notes.txt"]
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--read-only",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    tree = worktree_path(work, run_id)
    assert set(result.stderr.splitlines()[1:]) == {
        f"WARNING: read-only run {run_id} does not see uncommitted {name}"
        for name in names
    } | {f"next: emb run --close {run_id}"}
    assert (tree / "README.md").read_text() == committed
    assert not (tree / "notes.txt").exists()
    assert not (tree / "renamed.md").exists()
    source.write_text("Later owner edit.\n")
    sent = work.emb("run", "--send", run_id, "--message-file", work.brief())
    assert sent.returncode == 0
    assert sent.stderr == f"next: emb run --close {run_id}\n"
    assert (tree / "README.md").read_text() == committed
    assert source.read_text() == "Later owner edit.\n"


def test_read_only_change_warning_names_its_detached_worktree(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    files = work.eval / "replies" / "run-start.untracked"
    files.mkdir()
    (files / "evidence.txt").write_text("Worker evidence.\n")
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--read-only",
    )  # fmt: skip
    assert result.returncode == 0
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    evidence = worktree_path(work, run_id) / "evidence.txt"
    assert result.stderr == (
        "Team, Single tasks Reviewer: codex gpt-6.1-sol-mini · medium "
        "(single.reviewer)\n"
        f"WARNING: read-only run {run_id} changed {evidence}\n"
        f"next: emb run --close {run_id}\n"
    )
    assert not (work.work / "evidence.txt").exists()
    record = json.loads(work.emb("run", "--status", run_id, "--json").stdout)
    assert record["last_reason"] == f"changed {evidence}"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("changed_head", [False, True])
def test_read_only_close_keeps_changes_and_recorded_path(
    tmp_path: Path, env: dict[str, str], force: bool, changed_head: bool
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--read-only",
    )  # fmt: skip
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    tree = worktree_path(work, run_id)
    (tree / "evidence.txt").write_text("Keep this.\n")
    original = work.git("rev-parse", "HEAD", cwd=tree)
    if changed_head:
        work.git("add", "evidence.txt", cwd=tree)
        work.git("commit", "-m", "test: record evidence", cwd=tree)
    work.env["XDG_DATA_HOME"] = str(tmp_path / "changed-data")
    closed = work.emb("run", "--close", run_id, *(["--force"] if force else []))
    assert closed.returncode == 0
    assert f"git worktree remove --force {tree}" in closed.stderr
    assert (tree / "evidence.txt").read_text() == "Keep this.\n"
    record = json.loads(work.emb("run", "--status", run_id, "--json").stdout)
    assert record["status"] == "closed" and record["worktree"] == str(tree)
    if changed_head:
        work.git("reset", "--hard", original, cwd=tree)
    else:
        (tree / "evidence.txt").unlink()
    assert work.emb("run", "--close", run_id).stderr == ""
    assert not tree.exists() and str(tree) not in work.git("worktree", "list")


@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_read_only_worktrees_respects_branch_scope(
    tmp_path: Path, env: dict[str, str], dry_run: bool
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--read-only",
    )  # fmt: skip
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    tree = worktree_path(work, run_id)
    extra = tree / "evidence.txt"
    extra.write_text("Keep this.\n")
    assert work.emb("run", "--close", run_id).returncode == 0
    cleanup = ["cleanup", "--merged-into", "claude/report-since"]
    kept = work.emb(*cleanup)
    assert f"git worktree remove --force {tree}" in kept.stdout
    assert extra.exists()
    extra.unlink()
    assert work.emb(*cleanup, "--branch", "claude/report-since").returncode == 0
    assert tree.exists()
    cleaned = work.emb(*cleanup, *(["--dry-run"] if dry_run else []))
    assert cleaned.returncode == 0, cleaned.stderr
    assert f"worktree {tree}" in cleaned.stdout
    assert tree.exists() == dry_run
    assert (str(tree) in work.git("worktree", "list")) == dry_run


def test_start_without_a_set_row_exits_64(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    result = work.emb(
        "run", "--engine", "opencode", "--dir", ".", "--task-file", work.brief()
    )
    assert result.returncode == 64
    assert result.stderr.splitlines()[-1] == "next: emb run --help"


def start(work: Case) -> tuple[str, str, Path]:
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief()
    )
    assert result.returncode == 0, result.stderr
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    return run_id, f"ember/codex/{run_id}", worktree_path(work, run_id)


def test_merge_merges_and_cleans_up(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    run_id, branch, worktree = start(work)
    work.env["XDG_DATA_HOME"] = str(tmp_path / "changed-data")
    tip = work.git("rev-parse", branch)
    refused = work.emb("merge", branch, "--into", "claude/report-since")
    assert refused.returncode == 64
    missing = work.emb(
        "merge",
        "nope",
        "--into",
        "claude/report-since",
        "--verdict",
        "keep",
        "--note",
        "Keep",
        "--subject",
        "feat: add report date filtering",
    )
    assert missing.returncode == 65
    unnoted = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep"
    )
    assert unnoted.returncode == 64 and "--note" in unnoted.stderr
    result = work.emb(
        "merge",
        branch,
        "--into",
        "claude/report-since",
        "--verdict",
        "keep",
        "--note",
        "Keep",
        "--subject",
        "feat: add report date filtering",
    )
    assert result.returncode == 0, result.stderr
    merge = work.git("rev-parse", "HEAD")
    assert result.stdout == (
        "Verdict: Keep\n"
        f"merged {branch} at {tip} into claude/report-since: {merge}\n"
        f"removed {branch}: closed {run_id}; removed worktree {worktree}; "
        f"deleted at {tip}\n"
    )
    assert work.git("log", "-1", "--format=%s") == ("feat: add report date filtering")
    assert len(work.git("log", "-1", "--format=%P").split()) == 1
    assert f"Ember-Task-Tip: {tip}" in work.git("log", "-1", "--format=%B")
    assert not worktree.exists()
    assert work.git("branch", "--list", branch) == ""
    again = work.emb("cleanup", "--merged-into", "claude/report-since")
    assert again.stdout == ""
    assert (
        again.stderr == "emb cleanup: no Ember run branch against claude/report-since\n"
    )


@pytest.mark.parametrize("subject", [None, "", " ", "feat: a\nbody", "a\rb"])
def test_merge_requires_a_subject_before_changing_the_target(
    tmp_path: Path, env: dict[str, str], subject: str | None
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    _, branch, tree = start(work)
    head = work.git("rev-parse", "HEAD")
    args = ["--subject", subject] if subject is not None else []
    result = work.emb(
        "merge", branch, "--into", "claude/report-since",
        "--verdict", "keep", "--note", "Keep", *args,
    )  # fmt: skip
    assert result.returncode == 64
    assert "next:" in result.stderr and "--subject" in result.stderr
    assert work.git("rev-parse", "HEAD") == head
    assert tree.exists() and work.git("branch", "--list", branch)


def test_merge_squashes_turns_with_trailers_and_json_warnings(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    trailer = "Co-Authored-By: Claude <noreply@anthropic.com>"
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--trailer", trailer, "--trailer", "Ember-Verdict: forged",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    branch = f"ember/codex/{run_id}"
    tree = worktree_path(work, run_id)
    base = work.git("rev-parse", "HEAD")
    for turn in (2, 3):
        files = work.eval / "replies" / f"run-send.{turn - 1}.files"
        files.mkdir()
        (files / "extra.txt").write_text(f"turn {turn}\n")
        assert (
            work.emb(
                "run", "--send", run_id, "--message-file", work.brief("Correct it\n")
            ).returncode
            == 0
        )
    work.git("commit", "--amend", "-m", f"feat!: worker\n\n{trailer}", cwd=tree)
    tip = work.git("rev-parse", branch)
    assert work.git("rev-list", "--count", f"{base}..{branch}") == "3"
    result = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep",
        "--note", "Keep after review", "--subject", "feat: extend report", "--json",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {
        "branch",
        "target",
        "note",
        "warnings",
        "merge_commit",
        "contained_before",
        "cleanup",
        "next",
    }
    assert report["next"] is None
    assert report["warnings"] == [
        f"WARNING: {branch} holds a breaking change; the subject has no !"
    ]
    assert result.stderr == report["warnings"][0] + "\n"
    assert report["merge_commit"] == work.git("rev-parse", "HEAD")
    assert report["contained_before"] is False
    assert report["cleanup"][0]["action"] == "removed"
    assert work.git("rev-list", "--count", f"{base}..HEAD") == "1"
    assert work.git("log", "-1", "--format=%P") == base
    assert work.git("log", "-1", "--format=%B") == (
        f"feat: extend report\n\n{trailer}\n"
        f"Ember-Verdict: Keep after review\nEmber-Task-Tip: {tip}"
    )


def test_merge_keeps_a_dirty_worktree(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("merge-and-cleanup-keeps", tmp_path, env)
    _, branch, worktree = start(work)
    assert (worktree / "tools" / "__pycache__" / "report.cpython-314.pyc").is_file()
    result = work.emb(
        "merge",
        branch,
        "--into",
        "claude/report-since",
        "--verdict",
        "keep",
        "--note",
        "Keep",
        "--subject",
        "feat: add report date filtering",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[2] == (
        f"kept {branch}: the worktree {worktree} has changes; "
        f"next: git -C {worktree} status --short"
    )
    assert worktree.is_dir()
    assert work.git("branch", "--list", branch) != ""
    assert work.git("log", "-1", "--format=%s") == ("feat: add report date filtering")
    again = work.emb(
        "merge",
        branch,
        "--into",
        "claude/report-since",
        "--verdict",
        "keep",
        "--note",
        "Keep",
        "--subject",
        "feat: add report date filtering",
    )
    assert again.stdout.startswith(f"Verdict: Keep\nalready contained: {branch} at ")
    (work.work / "later.txt").write_text("Later target edit\n")
    work.git("add", "later.txt")
    work.git("commit", "-m", "fix: update target after squash")
    (worktree / "tools" / "__pycache__" / "report.cpython-314.pyc").unlink()
    (worktree / "tests" / "__pycache__" / "test_report.cpython-314.pyc").unlink()
    cleaned = work.emb("cleanup", "--merged-into", "claude/report-since")
    assert cleaned.returncode == 0, cleaned.stderr
    assert f"removed {branch}:" in cleaned.stdout
    assert not worktree.exists() and work.git("branch", "--list", branch) == ""


def test_merge_from_inside_the_run_worktree(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    _, branch, worktree = start(work)
    result = subprocess.run(
        ["emb", "merge", branch, "--into", "claude/report-since"]
        + [
            "--verdict",
            "keep",
            "--note",
            "Keep",
            "--subject",
            "feat: add report date filtering",
        ],
        cwd=worktree,
        env=work.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[2].startswith(f"removed {branch}: ")
    assert not worktree.exists()
    assert work.git("branch", "--list", branch) == ""


def test_merge_refuses_a_dirty_target(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    _, branch, _ = start(work)
    (work.work / "notes.txt").write_text("mine\n")
    result = work.emb(
        "merge",
        branch,
        "--into",
        "claude/report-since",
        "--verdict",
        "keep",
        "--note",
        "Keep",
        "--subject",
        "feat: add report date filtering",
    )
    assert result.returncode == 75
    assert f"next: git -C {work.work} status --short" in result.stderr


# The stub's `next:` lines against ember.runs.steps


def real_step(work: Case, run_id: str, session: str = "live") -> tuple[str, str | None]:
    """Derive a stub run's step with Ember's own code, in the case's environment."""
    document = json.loads((work.eval / "state.json").read_text())["runs"][run_id]
    known = {item.name for item in dataclasses.fields(record.Run)}
    values: dict[str, Any] = {
        "sandbox": "none",
        "network": False,
        "read": (),
        "read_effective": (),
        "expected_head": None,
        "updated_at": 0.0,
        "ended_at": None,
        **{key: value for key, value in document.items() if key in known},
        "env": work.env,
    }
    for key in ("read", "read_effective", "trailers", "commits"):
        values[key] = tuple(values[key])

    def run_git(directory: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", directory, *args],
            capture_output=True,
            text=True,
            env=work.env,
            check=False,
        )

    return steps.derive(record.Run(**values), session, False, run_git)


def stderr_next(result: subprocess.CompletedProcess[str]) -> str | None:
    """The `next:` line a call printed last on standard error, or None."""
    lines = result.stderr.splitlines()
    return lines[-1] if lines and lines[-1].startswith("next: ") else None


def test_stub_next_lines_follow_an_editing_run(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    claim = "run-abc123"
    started = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--claim", claim,
    )  # fmt: skip
    run_id = started.stdout.splitlines()[0].removeprefix("run ")
    branch = f"ember/codex/{run_id}"
    step, line = real_step(work, run_id)
    assert step == "merge" and line is not None and "--expect-files 2" in line
    assert stderr_next(started) == line
    state = json.loads((work.eval / "state.json").read_text())
    assert state["runs"][run_id]["base_branch"] == "claude/report-since"

    # Status: `step:` on standard output, `next:` on standard error.
    text = work.emb("run", "--status", run_id)
    assert "step: merge\n" in text.stdout
    assert not any(row.startswith("next:") for row in text.stdout.splitlines())
    assert text.stderr == line + "\n"
    one = work.emb("run", "--status", run_id, "--json")
    document = json.loads(one.stdout)
    assert (document["step"], document["next"]) == ("merge", line)
    assert one.stderr == line + "\n"
    # The list form carries both fields and prints no line.
    listing = work.emb("run", "--status", "--json")
    assert listing.stderr == ""
    assert [(item["step"], item["next"]) for item in json.loads(listing.stdout)] == [
        ("merge", line)
    ]

    # A correction turn counts no files, so the line drops --expect-files.
    sent = work.emb("run", "--send", run_id, "--message-file", work.brief("Fix\n"))
    step, line = real_step(work, run_id)
    assert step == "merge" and "--expect-files" not in (line or "")
    assert stderr_next(sent) == line
    recovered = work.emb("run", "--recover", run_id)
    assert stderr_next(recovered) == line

    # Closed with uncontained commits on a branch that is still there.
    closed = work.emb("run", "--close", run_id)
    assert real_step(work, run_id) == ("merge", line)
    assert stderr_next(closed) == line
    assert (
        json.loads(work.emb("run", "--status", run_id, "--json").stdout)["next"] == line
    )

    # A merge closes the run; its line is the claim finish.
    merged = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep",
        "--note", "Keep", "--subject", "feat: add report date filtering", "--json",
    )  # fmt: skip
    assert merged.returncode == 0, merged.stderr
    finish = f"next: emb choices --finish {claim}"
    assert real_step(work, run_id) == ("finish", finish)
    assert stderr_next(merged) == finish
    assert json.loads(merged.stdout)["next"] == finish
    assert (
        json.loads(work.emb("run", "--status", run_id, "--json").stdout)["step"]
        == "finish"
    )

    # An exit-0 finish records the claim `gone`; no line is derived.
    assert work.emb("choices", "--finish", claim).returncode == 0
    choices.finish(claim, env=work.env)
    assert real_step(work, run_id) == ("finish", None)
    gone = work.emb("run", "--status", run_id, "--json")
    assert json.loads(gone.stdout)["next"] is None and gone.stderr == ""
    state = json.loads((work.eval / "state.json").read_text())
    assert state["claims"] == {claim: "gone"}


def test_stub_next_lines_follow_a_read_only_run(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    started = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--read-only", "--claim", "review-abc123",
    )  # fmt: skip
    run_id = started.stdout.splitlines()[0].removeprefix("run ")
    assert real_step(work, run_id) == ("close", f"next: emb run --close {run_id}")
    assert stderr_next(started) == f"next: emb run --close {run_id}"
    closed = work.emb("run", "--close", run_id)
    assert real_step(work, run_id) == (
        "finish",
        "next: emb choices --finish review-abc123",
    )
    assert stderr_next(closed) == "next: emb choices --finish review-abc123"
    dry = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--dry-run",
    )  # fmt: skip
    assert dry.returncode == 0 and stderr_next(dry) is None


def test_stub_records_no_base_branch_when_detached(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    work.git("switch", "--detach", "-q")
    started = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief()
    )
    run_id = started.stdout.splitlines()[0].removeprefix("run ")
    state = json.loads((work.eval / "state.json").read_text())
    assert state["runs"][run_id]["base_branch"] is None
    step, line = real_step(work, run_id)
    assert step == "merge"
    assert "--base <session branch>" in (line or "")
    assert "--into <session branch>" in (line or "")
    assert stderr_next(started) == line


def test_stub_merge_prints_no_line_for_a_kept_branch(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("merge-and-cleanup-keeps", tmp_path, env)
    started = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--claim", "keeps-abc123",
    )  # fmt: skip
    run_id = started.stdout.splitlines()[0].removeprefix("run ")
    branch = f"ember/codex/{run_id}"
    merged = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep",
        "--note", "Keep", "--subject", "feat: add report date filtering", "--json",
    )  # fmt: skip
    assert merged.returncode == 0, merged.stderr
    document = json.loads(merged.stdout)
    assert document["cleanup"][0]["action"] == "kept"
    assert document["next"] is None
    assert "next:" not in merged.stderr


def test_stub_cleanup_prints_the_claim_finish_line_by_the_reference_rule(
    tmp_path: Path, env: dict[str, str]
) -> None:
    """The line follows exit 0 with --claim, unless a kept result holds the claim."""
    work = case("claim-and-menu-writer", tmp_path, env)
    claim = "cleanup-abc123"
    started = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--claim", claim,
    )  # fmt: skip
    run_id = started.stdout.splitlines()[0].removeprefix("run ")
    branch = f"ember/codex/{run_id}"
    target = "claude/report-since"
    finish = f"next: emb choices --finish {claim}\n"
    # The branch is not contained: a kept result holds the claim.
    kept = work.emb("cleanup", "--merged-into", target, "--claim", claim)
    assert kept.stdout.startswith(f"kept {branch}: not contained in {target}")
    assert "emb choices --finish" not in kept.stderr
    # Another claim handles nothing, so its finish line is printed.
    other = work.emb("cleanup", "--merged-into", target, "--claim", "other-abc123")
    assert other.stdout == ""
    assert other.stderr.endswith("next: emb choices --finish other-abc123\n")
    # No claim, no line.
    assert (
        "emb choices --finish"
        not in work.emb("cleanup", "--merged-into", target).stderr
    )
    work.git("merge", "--no-edit", "-q", branch)
    # Contained: a dry run prints no line, the real cleanup does.
    dry = work.emb("cleanup", "--merged-into", target, "--claim", claim, "--dry-run")
    assert dry.stdout.startswith(f"would remove {branch}")
    assert "emb choices --finish" not in dry.stderr
    done = work.emb("cleanup", "--merged-into", target, "--claim", claim)
    assert done.stdout.startswith(f"removed {branch}")
    assert done.stderr == finish
    again = work.emb("cleanup", "--merged-into", target, "--claim", claim)
    assert again.stdout == ""
    assert again.stderr == (
        f"emb cleanup: no Ember run branch against {target}\n{finish}"
    )


def test_claim_lost_then_gone(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-lost-then-gone", tmp_path, env)
    first = work.emb("choices", "--claim", "--id", "run-abc123", "--skill", __version__)
    assert (first.returncode, first.stdout, first.stderr) == (75, "", "")
    second = work.emb(
        "choices", "--claim", "--id", "run-abc123", "--skill", __version__
    )
    assert second.returncode == 0
    reply = json.loads(second.stdout)
    assert reply["claim"] == {"id": "run-abc123", "status": "gone"}
    assert reply["skill"] == "current"
    assert reply["loaded_skill_version"] == __version__
    assert reply["installed_skill_version"] == __version__
    assert second.stderr == ""
    assert reply["menu"]["single.writer"]["state"] == "ask"
    assert all(answer["layer"] == "default" for answer in reply["answers"].values())
    read = json.loads(work.emb("choices", "--json", "--id", "run-abc123").stdout)
    assert read["claim"]["status"] == "gone"
    preview = json.loads(work.emb("choices", "--json").stdout)
    assert preview["menu"]["single.writer"]["state"] == "set"
    assert [call["reply"] for call in work.calls()] == [
        "choices-claim.1",
        "choices-claim",
        "choices-read-id",
        "choices-read",
    ]


@pytest.mark.parametrize(
    ("name", "outcome", "claim_status", "installed"),
    [
        ("claim-and-menu-writer", "current", "none", __version__),
        ("claim-stale-skill", "stale", "claimed", "999.0.0"),
        ("claim-unknown-skill", "unknown", "claimed", None),
    ],
)
def test_claim_skill_replies(
    tmp_path: Path,
    env: dict[str, str],
    name: str,
    outcome: str,
    claim_status: str,
    installed: str | None,
) -> None:
    work = case(name, tmp_path, env)
    result = work.emb(
        "choices", "--claim", "--id", "run-abc123", "--skill", __version__
    )
    assert result.returncode == 0
    reply = json.loads(result.stdout)
    assert reply["claim"] == {"id": "run-abc123", "status": claim_status}
    assert reply["skill"] == outcome
    assert reply["loaded_skill_version"] == __version__
    assert reply["installed_skill_version"] == installed
    if outcome == "current":
        assert result.stderr == ""
    else:
        assert reply["answers"]["single.writer"] == {
            "value": "codex:gpt-6.1-sol-mini",
            "layer": "one-time",
        }
        if outcome == "stale":
            assert result.stderr == (
                f"WARNING: the loaded ember skill is {__version__}; "
                "the installed skill is 999.0.0\n"
                "next: reload the ember skill before the next step\n"
            )
        else:
            assert result.stderr == (
                "WARNING: the installed ember skill version is missing, unreadable "
                "or malformed\n"
                "owner: repair or reinstall the ember skill, then reload it before "
                "starting a worker\n"
            )
        # Reclaiming preserves the successful claim and its one-time answer.
        again = work.emb(
            "choices", "--claim", "--id", "run-abc123", "--skill", __version__
        )
        assert json.loads(again.stdout) == reply
        assert again.stderr == result.stderr
    unversioned = work.emb("choices", "--claim", "--id", "run-abc123")
    assert unversioned.returncode == 0
    assert unversioned.stderr == ""
    document = json.loads(unversioned.stdout)
    for key in ("skill", "loaded_skill_version", "installed_skill_version"):
        assert key not in document
    read = json.loads(work.emb("choices", "--json", "--id", "run-abc123").stdout)
    assert read["claim"] == reply["claim"]
    assert "skill" not in read


def test_send_refused_then_forced(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("send-refused-foreign-messages", tmp_path, env)
    assert work.calls() == []
    listing = work.emb("run", "--status")
    run_id = listing.stdout.split()[0]
    assert "Add --since to tools/report.py" in listing.stdout
    message = work.work.parent / "message.md"
    message.write_text("Add a test for a malformed day.\n")
    refused = work.emb("run", "--send", run_id, "--message-file", str(message))
    assert refused.returncode == 75
    assert refused.stderr.splitlines() == [
        f"emb run: the session of {run_id} holds messages Ember did not send "
        "(1 user message in the rollout); Claude asks the owner before going on",
        f"next: ask the owner about the messages in the session of {run_id}; with "
        "their go-ahead, repeat the command with --force",
    ]
    forced = work.emb(
        "run", "--send", run_id, "--message-file", str(message), "--force"
    )
    assert forced.returncode == 0, forced.stderr
    branch = f"ember/codex/{run_id}"
    assert forced.stdout.splitlines()[0].endswith("· 1 files · turn 2")
    assert work.git("log", "-1", "--format=%s", branch) == (
        "Add --since to tools/report.py (turn 2)"
    )
    assert [call["route"] for call in work.calls()] == [
        "run-status",
        "run-send",
        "run-send",
    ]


@pytest.mark.parametrize("dry_run", [False, True])
def test_owner_line_start_makes_nothing(
    tmp_path: Path, env: dict[str, str], dry_run: bool
) -> None:
    work = case("owner-line-not-run", tmp_path, env)
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        *(["--dry-run"] if dry_run else []),
    )  # fmt: skip
    assert result.returncode == 69
    assert result.stderr.splitlines()[-1] == "owner: codex login"
    assert result.stdout == ""
    assert work.git("branch", "--list", "ember/*") == ""


@pytest.mark.parametrize("args", [("--status", "--json"), ("--json", "--status")])
def test_status_without_runs(
    tmp_path: Path, env: dict[str, str], args: tuple[str, ...]
) -> None:
    work = case("small-change-no-worker", tmp_path, env)
    result = work.emb("run", *args)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == []


def test_generators_write_expected_files(generated_eval_fixtures: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(STUBS / "make_replies.py"), "--list-files"],
        cwd=TOP,
        check=True,
        capture_output=True,
        text=True,
    )
    expected = set(result.stdout.splitlines()) | {"_stubs/replies/help.out"}
    actual = {
        str(path.relative_to(generated_eval_fixtures))
        for path in generated_eval_fixtures.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    for name in expected:
        assert not (EVALS / name).exists(), name
        assert (generated_eval_fixtures / name).read_text().strip(), name


def test_generated_status_replies_carry_their_next_line(
    generated_eval_fixtures: Path,
) -> None:
    """Status standard error is saved only when non-empty; state holds no step."""
    state = json.loads(
        (
            generated_eval_fixtures
            / "turn-results-counted/files/.ember-eval/state.json"
        ).read_text()
    )
    for document in state["runs"].values():
        assert "step" not in document and "next" not in document
    for name in (
        "provider-capacity",
        "provider-usage-limit",
        "provider-outage",
        "provider-uncertain",
        "sandbox-denied-paths",
    ):
        replies = generated_eval_fixtures / name / "files/.ember-eval/replies"
        error = (replies / "run-status.err").read_text()
        document = json.loads((replies / "run-status.json").read_text())
        assert error == f"{document['next']}\n"
        assert error.startswith("next: ")


@pytest.mark.parametrize("name", CASES)
def test_generated_case_files_are_installed(
    tmp_path: Path, env: dict[str, str], generated_eval_fixtures: Path, name: str
) -> None:
    work = case(name, tmp_path, env)
    case_files = generated_eval_fixtures / name / "files"
    for source in case_files.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(case_files)
        installed = work.work / relative
        assert installed.is_file(), relative
        # This case replaces path placeholders in its initial run state.
        if name != "turn-results-counted":
            assert installed.read_bytes() == source.read_bytes(), relative
    assert (work.eval / "replies/help.out").is_file()


@pytest.mark.parametrize(
    ("stage", "message"),
    [("help", "help generator"), ("replies", "make_replies.py")],
)
def test_setup_names_a_failed_generator(
    tmp_path: Path, env: dict[str, str], stage: str, message: str
) -> None:
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    uv = fakes / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$FAIL_STAGE" = help ] || [[ "$*" = *make_replies.py* ]]; then\n'
        "    exit 17\n"
        "fi\n"
    )
    uv.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    setup_env = {
        key: value for key, value in env.items() if key != "EMBER_EVAL_FIXTURES"
    }
    setup_env.update(
        PATH=f"{fakes}:{env['PATH']}", FAIL_STAGE=stage, TMPDIR=str(tmp_path)
    )
    result = subprocess.run(
        ["bash", str(EVALS / "claim-and-menu-writer/place-files.sh")],
        cwd=work,
        env=setup_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert list(work.iterdir()) == []
    assert list(tmp_path.glob("ember-eval-fixtures.*")) == []


def test_setup_generates_without_a_fixture_cache(
    tmp_path: Path, env: dict[str, str]
) -> None:
    setup_env = {
        key: value for key, value in env.items() if key != "EMBER_EVAL_FIXTURES"
    }
    setup_env.update(UV_CACHE_DIR=str(tmp_path / "uv-cache"), TMPDIR=str(tmp_path))
    work = case("flow-gap-check-first", tmp_path, setup_env)
    assert (work.eval / "replies/help.out").read_text().startswith("==> emb --help <==")
    assert json.loads((work.eval / "replies/choices-claim.out").read_text())["menu"]
    assert (work.eval / "replies/flow-dry-run.json").is_file()
    assert list(tmp_path.glob("ember-eval-fixtures.*")) == []
    assert list(tmp_path.glob("ember-eval-defaults.*")) == []


def test_setup_preserves_case_files_and_route_precedence(
    tmp_path: Path, env: dict[str, str]
) -> None:
    case_dir = tmp_path / "flow-gap-check-first"
    replies = case_dir / "files/.ember-eval/replies"
    replies.mkdir(parents=True)
    (replies / "choices-claim.out").write_text("case claim\n")
    (replies / "run-start.err").write_text("case error\n")
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        ["bash", str(STUBS / "install.sh"), str(case_dir)],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
    )
    installed = work / ".ember-eval/replies"
    assert (installed / "choices-claim.out").read_text() == "case claim\n"
    assert (installed / "run-start.err").read_text() == "case error\n"
    assert not (installed / "run-start.reply").exists()
    assert (installed / "flow-dry-run.json").is_file()
    assert (installed / "menu.json").is_file()


@pytest.mark.parametrize("args", [("--status", "--json"), ("--json", "--status")])
def test_turn_result_case_matches_run_status(
    tmp_path: Path,
    env: dict[str, str],
    args: tuple[str, ...],
) -> None:
    work = case("turn-results-counted", tmp_path, env)
    listing = work.emb("run", *args)
    assert listing.returncode == 0 and listing.stderr == ""
    documents = json.loads(listing.stdout)
    assert len(documents) == 3
    first, older, unfinished = documents
    assert first["last_exit"] == 0
    assert [turn["exit"] for turn in first["turns"]] == [73, 0]
    assert [turn["provider_error"] for turn in first["turns"]] == [True, False]
    assert [turn["provider_kind"] for turn in first["turns"]] == ["usage_limit", None]
    assert [a["provider_kind"] for a in first["turns"][1]["attempts"]] == [
        "capacity",
        None,
    ]
    assert first["turns"][1]["seconds"] == 45
    assert sum(a["seconds"] for a in first["turns"][1]["attempts"]) == 15
    assert "provider_kind" not in older["turns"][0]
    assert "attempts" not in older["turns"][0]
    assert older["turns"][0]["exit"] is None
    assert older["turns"][0]["seconds"] is None
    assert older["turns"][0]["provider_error"] is False
    assert unfinished["turns"][0]["exit"] is None
    assert unfinished["turns"][0]["state"] is None
    assert unfinished["turns"][0]["provider_error"] is None
    for document in documents:
        assert all(turn["denied_paths"] == [] for turn in document["turns"])
        assert document["last_screen_change_at"] is None
        one = work.emb("run", "--status", document["id"], "--json")
        assert one.returncode == 0 and json.loads(one.stdout) == document


def test_denied_path_case_keeps_every_attempt_and_owner_action(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("sandbox-denied-paths", tmp_path, env)
    result = work.emb(
        "run",
        "--engine",
        "codex",
        "--read-only",
        "--dir",
        ".",
        "--task-file",
        work.brief("Inspect the report"),
    )
    assert result.returncode == 0
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    status = work.emb("run", "--status", run_id, "--json")
    assert status.returncode == 0
    assert status.stderr == f"next: emb run --close {run_id}\n"
    [turn] = json.loads(status.stdout)["turns"]
    assert turn["exit"] == 0 and turn["state"] == "done"
    denials = {item["path"]: item for item in turn["denied_paths"]}
    expected = {
        "/eval/home/.toolchain": ("hidden", "/eval/home/.toolchain/versions/3.14"),
        "/eval/home/.ssh": ("secret location", None),
        "/eval/home/.config/tool": ("hidden", None),
        "/eval/home/.config/ember": ("secret location", None),
        "/eval/state/ember": ("secret location", None),
        "/eval/home/.local/share/ember/worktrees/other-worker": ("protected", None),
        "/eval/runtime": ("protected", None),
    }
    assert set(denials) == set(expected)
    for path, (reason, allow) in expected.items():
        assert denials[path] == {"path": path, "reason": reason, "allow_path": allow}
        warning = f"WARNING: {run_id} was denied {path}"
        if reason != "hidden":
            warning += f" ({reason})"
        assert result.stderr.splitlines().count(warning) == 1
    owner_prefix = (
        "owner: the turn finished (exit 0); read access is needed only if "
        "the worker's report says the work needed the path; "
        "Settings, Sandbox read paths;"
    )
    assert result.stderr.count(owner_prefix) == 2
    assert (
        "owner: /eval/home/.config/tool is hidden; no folder can be offered "
        "because each folder that holds it also holds private files"
    ) in result.stderr
    assert (
        result.stderr.count("it cannot be added to Settings, Sandbox read paths") == 2
    )
    additions = []
    for line in result.stderr.splitlines():
        if not line.startswith(owner_prefix):
            continue
        command = shlex.split(line.rsplit("; ", 1)[1])
        assert command[:3] == ["emb", "choices", "--set"]
        key, value = command[3].split("=", 1)
        assert key == "sandbox.read"
        preserved, added = json.loads(value)
        assert preserved == "/eval/home/shared"
        additions.append(added)
    assert additions == [
        "/eval/home/.toolchain/versions/3.14",
        "/eval/home/.ssh",
    ]
    # The earlier attempt's path is available only through warnings and status.
    assert "/eval/home/.toolchain" not in result.stdout.split("--- reply ---")[1]
    text = work.emb("run", "--status", run_id)
    assert text.returncode == 0 and "denied_paths" in text.stdout
    assert work.emb("choices", "--set", "sandbox.read=/eval/home/.ssh").returncode == 77


@pytest.mark.parametrize("variant", ["capacity", "usage_limit", "outage", "uncertain"])
def test_provider_error_case_streams_and_status(
    tmp_path: Path, env: dict[str, str], variant: str
) -> None:
    work = case(f"provider-{variant.replace('_', '-')}", tmp_path, env)
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief()
    )
    assert result.returncode == 73
    assert "was denied" not in result.stderr
    run_id = result.stdout.splitlines()[0].removeprefix("run ")
    next_line = exits.line(73, variant, run=run_id)
    assert next_line is not None and next_line in result.stderr
    assert result.stderr.count("at capacity; retry") == (
        1 if variant == "uncertain" else 0
    )
    status = work.emb("run", "--status", run_id, "--json")
    assert status.returncode == 0
    # Status derives its line from the record, which holds no provider kind.
    assert status.stderr == f"{exits.line(73, None, run=run_id)}\n"
    document = json.loads(status.stdout)
    assert document["step"] == "failed"
    assert document["last_exit"] == 73
    [turn] = document["turns"]
    assert turn["exit"] == 73 and turn["provider_error"] is True
    assert turn["denied_paths"] == []
    assert turn["provider_kind"] == ("capacity" if variant == "uncertain" else variant)
    if variant == "capacity":
        assert document["capacity_retries"] == 0
        answers = json.loads(work.emb("choices", "--json").stdout)["answers"]
        assert answers["provider.capacity_retries"]["value"] == "0"
    attempts = turn["attempts"]
    assert attempts[0]["sent"] and attempts[0]["result"] == "failed"
    assert len(attempts) == (2 if variant == "uncertain" else 1)
    if variant == "uncertain":
        assert attempts[-1]["sending"] and not attempts[-1]["sent"]
        assert attempts[-1]["result"] is None
        assert "ask the lead before sending again" in result.stderr
        assert "retry 1 of 2 in 30s" in result.stderr
        assert turn["seconds"] == 40
        assert sum(a["seconds"] for a in attempts) == 10


# The runner, with bwrap and claude replaced by fakes


FAKE_BWRAP = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG/bwrap.log"
chdir=.
while [ $# -gt 0 ]; do
    case $1 in
        --setenv) export "$2=$3"; shift 3 ;;
        --chdir) chdir=$2; shift 2 ;;
        --die-with-parent) shift; break ;;
        *) shift ;;
    esac
done
cd "$chdir" && exec "$@"
"""

FAKE_CLAUDE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG/claude.log"
emb choices --claim --id run-abc123 > /dev/null
case " $* " in
    *" --resume sess-1 "*)
        echo '{"type":"result","session_id":"sess-1","result":"Merged."}' ;;
    *)
        echo '{"type":"assistant","message":"working"}'
        echo '{"type":"result","session_id":"sess-1","result":"Shall I remove it?"}' ;;
esac
"""


def test_runner_reply_loop(tmp_path: Path, env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "bwrap").write_text(FAKE_BWRAP)
    (fakes / "bwrap").chmod(0o755)
    (home / ".local" / "bin").mkdir(parents=True)
    claude = home / ".local" / "bin" / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text("{}")
    logs = tmp_path / "logs"
    logs.mkdir()
    owner_folders = [
        Path(env[variable]) / "ember"
        for variable in (
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        )
    ]
    for folder in owner_folders:
        folder.mkdir(parents=True)
    out = tmp_path / "out"
    case_dir = EVALS / "merge-and-cleanup-keeps"
    result = subprocess.run(
        ["bash", str(RUNNER), str(case_dir), str(SKILL), str(out)],
        env={**env, "PATH": f"{fakes}:{env['PATH']}", "FAKE_LOG": str(logs)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("exit 0 in ")
    assert result.stdout.rstrip().endswith("calls 2")
    claude_calls = (logs / "claude.log").read_text().splitlines()
    assert len(claude_calls) == 2
    assert claude_calls[0].startswith("-p Add a `--since YYYY-MM-DD` option")
    assert "--model opus --output-format stream-json --verbose" in claude_calls[0]
    assert "--permission-mode acceptEdits" in claude_calls[0]
    assert (
        "--allowedTools Read,Glob,Grep,Skill,Write,Edit,Bash,Agent" in claude_calls[0]
    )
    assert claude_calls[1].startswith("-p --resume sess-1 Leave it as it is.")
    assert (out / "owner.log").read_text() == "owner reply 1: Leave it as it is.\n"
    trace = (out / "trace.jsonl").read_text().splitlines()
    assert [json.loads(line)["type"] for line in trace] == [
        "assistant",
        "result",
        "result",
    ]
    assert (out / "prompt.txt").read_text().startswith("Add a `--since")
    assert json.loads((out / "run.json").read_text())["exit"] == 0
    assert len((out / "calls.log").read_text().splitlines()) == 2
    for name in ("scaffold.log", "stderr.txt"):
        assert (out / name).exists()
    bwrap = (logs / "bwrap.log").read_text().splitlines()[0]
    eval_dir = out / "work" / ".ember-eval"
    assert f"--ro-bind / / --dev /dev --proc /proc --bind {out}/tmp /tmp" in bwrap
    assert (
        f"--tmpfs {home}/.claude/skills --ro-bind {SKILL} {home}/.claude/skills/ember"
        in bwrap
    )
    assert f"--tmpfs {home}/.claude/skills/ember/evals" in bwrap
    assert "--unsetenv CLAUDE_CONFIG_DIR" in bwrap
    assert f"--setenv EMBER_EVAL_DIR {eval_dir}" in bwrap
    assert f"--setenv PATH {eval_dir}/bin:" in bwrap
    assert not (home / ".claude" / "skills" / "ember").exists()
    xdg = out / "xdg"
    assert f"--bind {xdg} {xdg}" in bwrap
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        assert f"--setenv {variable} {xdg / name}" in bwrap
        assert stat.S_IMODE((xdg / name).stat().st_mode) == 0o700
    # The owner's Ember folder in each base the owner uses is blanked.
    for folder in owner_folders:
        assert f"--tmpfs {folder}" in bwrap
        assert not any(folder.iterdir())
    assert f"--tmpfs {home}/.local/share/ember" not in bwrap


def test_runner_scaffold_uses_the_private_data_folder(
    tmp_path: Path, env: dict[str, str]
) -> None:
    home = Path(env["HOME"])
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "bwrap").write_text(FAKE_BWRAP)
    (fakes / "bwrap").chmod(0o755)
    (home / ".local" / "bin").mkdir(parents=True)
    claude = home / ".local" / "bin" / "claude"
    claude.write_text("#!/usr/bin/env bash\n")
    claude.chmod(0o755)
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text("{}")
    out = tmp_path / "out"
    case_dir = EVALS / "send-refused-foreign-messages"
    result = subprocess.run(
        ["bash", str(RUNNER), str(case_dir), str(SKILL), str(out)],
        env={**env, "PATH": f"{fakes}:{env['PATH']}", "FAKE_LOG": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    made = list((out / "xdg" / "data" / "ember" / "worktrees").iterdir())
    assert len(made) == 1 and made[0].name.startswith("codex-")
    assert not Path(env["XDG_DATA_HOME"]).exists()


def test_runner_needs_its_arguments(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(RUNNER)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 64
    assert result.stderr.startswith("usage: run_case.sh")


REPEAT = EVALS / "_runner" / "run_repeat.sh"
TALLY = EVALS / "_runner" / "tally.py"


def test_repeat_runs_each_side_in_its_own_folder(
    tmp_path: Path, env: dict[str, str]
) -> None:
    home = Path(env["HOME"])
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "bwrap").write_text(FAKE_BWRAP)
    (fakes / "bwrap").chmod(0o755)
    (home / ".local" / "bin").mkdir(parents=True)
    claude = home / ".local" / "bin" / "claude"
    claude.write_text(
        "#!/usr/bin/env bash\n"
        'echo \'{"type":"result","session_id":"s","result":"Done."}\'\n'
    )
    claude.chmod(0o755)
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text("{}")
    logs = tmp_path / "logs"
    logs.mkdir()
    out = tmp_path / "out"
    result = subprocess.run(
        [
            "bash",
            str(REPEAT),
            "--runs",
            "1",
            "--jobs",
            "2",
            "--base",
            "HEAD",
            str(EVALS / "provider-uncertain"),
            str(out),
        ],
        env={**env, "PATH": f"{fakes}:{env['PATH']}", "FAKE_LOG": str(logs)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(line.split(" ", 2)[:2] for line in result.stdout.splitlines()) == [
        ["base", "run-1"],
        ["skill", "run-1"],
    ]
    assert all(" exit 0 in " in line for line in result.stdout.splitlines())
    for side in ("skill", "base"):
        assert json.loads((out / side / "run-1" / "run.json").read_text())["exit"] == 0
    base = out / "base-skill" / "skill"
    assert (base / "SKILL.md").is_file()
    bwrap = (logs / "bwrap.log").read_text()
    assert f"--ro-bind {SKILL} {home}/.claude/skills/ember" in bwrap
    assert f"--ro-bind {base} {home}/.claude/skills/ember" in bwrap


CASE_ARGS = [str(EVALS / "provider-uncertain"), "out"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--runs", "0", *CASE_ARGS], "--runs must be a whole number from 1 to 50"),
        (["--jobs", "9", *CASE_ARGS], "--jobs must be a whole number from 1 to 8"),
        (["--base", "no-such-ref-for-the-test", *CASE_ARGS], "is not a commit"),
        (["--runs"], "--runs needs a value"),
    ],
)
def test_repeat_refuses_bad_options(
    tmp_path: Path, arguments: list[str], message: str
) -> None:
    result = subprocess.run(
        ["bash", str(REPEAT), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert message in result.stderr


def test_repeat_needs_a_case(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(REPEAT), str(tmp_path), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 66
    assert "prompt.md is missing" in result.stderr
    result = subprocess.run(
        ["bash", str(REPEAT), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert result.stderr.startswith("usage: run_repeat.sh")


def write_grading(run: Path, passed: list[Any]) -> None:
    run.mkdir(parents=True)
    expectations = [
        {"text": f"expectation {index}", "passed": value, "evidence": "-"}
        for index, value in enumerate(passed)
    ]
    (run / "grading.json").write_text(json.dumps({"expectations": expectations}))


def run_tally(*arguments: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TALLY), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_tally_counts_each_grader_for_each_side(tmp_path: Path) -> None:
    case = tmp_path / "some-case"
    (case / "graders").mkdir(parents=True)
    for name in ("02-second", "01-first"):
        (case / "graders" / f"{name}.md").write_text("x")
    out = tmp_path / "out"
    write_grading(out / "skill" / "run-1", [True, True])
    write_grading(out / "skill" / "run-2", [True, False])
    write_grading(out / "skill" / "run-10", [False, False])
    write_grading(out / "base" / "run-1", [True, True])
    write_grading(out / "base" / "run-2", [True])
    write_grading(out / "base" / "run-3", [True, "yes"])
    (out / "base" / "run-4").mkdir()
    (out / "base-skill" / "skill").mkdir(parents=True)
    result = run_tally(case, out)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "some-case",
        "grader       skill  base",
        "01-first     2/3    1/1",
        "02-second    1/3    1/1",
        "all graders  1/3    1/1",
    ]
    assert result.stderr.splitlines() == [
        "base run-2 not graded: grading.json has 1 expectations, the case has 2"
        " graders",
        "base run-3 not graded: grading.json has an expectation without a true or"
        " false passed",
        "base run-4 not graded: no grading.json",
    ]
    table = json.loads(run_tally(case, out, "--json").stdout)
    assert table["sides"]["skill"] == {
        "graded": 3,
        "passed": [2, 1],
        "all_passed": 1,
        "not_graded": {},
    }


def test_tally_counts_zero_graded_runs(tmp_path: Path) -> None:
    case = tmp_path / "case"
    (case / "graders").mkdir(parents=True)
    (case / "graders" / "01-only.md").write_text("x")
    out = tmp_path / "out"
    (out / "skill" / "run-1").mkdir(parents=True)
    result = run_tally(case, out)
    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == "all graders  0/0"


def test_tally_needs_graders_and_runs(tmp_path: Path) -> None:
    case = tmp_path / "case"
    (case / "graders").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    assert run_tally(case, out).returncode == 66
    (case / "graders" / "01-only.md").write_text("x")
    result = run_tally(case, out)
    assert result.returncode == 66
    assert "has no run-<i> folders" in result.stderr
    assert run_tally(case, tmp_path / "missing").returncode == 66


# The skill's text


def skill_text() -> str:
    path = SKILL / "SKILL.md"
    if not path.exists():
        pytest.skip("skill/SKILL.md does not exist in this checkout yet")
    return path.read_text(encoding="utf-8")


def test_review_bar_is_exact() -> None:
    blocks = re.findall(r"^```review-bar\n(.*?)\n```$", skill_text(), re.S | re.M)
    assert blocks == [REVIEW_BAR]
    prose = " ".join(skill_text().split())
    assert "Copy its exact text into every review brief you write." in prose


def test_named_bundled_files_exist() -> None:
    names = set(
        re.findall(
            r"(?<![\w/.-])((?:references|assets|scripts)/[\w.-]*\w)", skill_text()
        )
    )
    assert names
    missing = sorted(name for name in names if not (SKILL / name).is_file())
    assert missing == []


def test_no_file_under_skill_names_an_earlier_system() -> None:
    found = []
    for path in sorted(SKILL.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.is_relative_to(RESULTS):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in FORBIDDEN:
            for match in pattern.finditer(f"{path.relative_to(SKILL)}\n{text}"):
                found.append(f"{path.relative_to(SKILL)}: {match.group(0)}")
    assert found == []


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("--task-file x --message-file y", 0),
        ("emb panel --retry panel-20261102-101500-a1b2c3 1", 0),
        ("use --task x", 1),
        ("Delegation and cx-delegate", 2),
        ("one machine", 0),
        ("two machines", 1),
    ],
)
def test_name_check_matches_whole_tokens(text: str, hits: int) -> None:
    assert sum(len(pattern.findall(text)) for pattern in FORBIDDEN) == hits


# Flow, panel and gap-check replies


NEW_ROUTES = (
    (["gapcheck", "--brief"], "gapcheck-brief"),
    (["gapcheck", "--reply", "reply.md"], "gapcheck-reply"),
    (["gapcheck"], "gapcheck"),
    (["flow", "--dry-run"], "flow-dry-run"),
    (["flow", "--start"], "flow-start"),
    (["flow", "--watch", "flow-id"], "flow-watch"),
    (["flow", "--status", "flow-id"], "flow-status"),
    (["flow", "--lead", "flow-id", "task", "--review"], "flow-lead"),
    (["flow", "--resume", "flow-id"], "flow-resume"),
    (["panel", "--dry-run"], "panel-dry-run"),
    (["panel", "--status", "panel-id"], "panel-status"),
    (["panel", "--recover", "panel-id"], "panel-recover"),
    (["panel"], "panel-start"),
)


@pytest.mark.parametrize(("args", "route"), NEW_ROUTES)
def test_new_routes(
    tmp_path: Path, env: dict[str, str], args: list[str], route: str
) -> None:
    work = case("small-change-no-worker", tmp_path, env)
    replies = work.eval / "replies"
    (replies / f"{route}.out").write_text("text {{FLOW}} {{PANEL}}\n")
    (replies / f"{route}.json").write_text('{"route": "' + route + '"}\n')
    result = work.emb(*args)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("text ")
    assert work.calls()[-1]["route"] == route
    result = work.emb(*args, "--json")
    assert json.loads(result.stdout) == {"route": route}
    assert work.calls()[-1]["seq"] == 2


@pytest.mark.parametrize(("args", "route"), NEW_ROUTES)
def test_new_route_needs_reply(
    tmp_path: Path, env: dict[str, str], args: list[str], route: str
) -> None:
    work = case("small-change-no-worker", tmp_path, env)
    result = work.emb(*args)
    assert result.returncode == 69
    assert result.stderr.startswith(f"emb eval stub: no reply for {route} ")
    assert work.calls()[-1]["exit"] == 69


def flow_plan(work: Case, tasks: list[dict[str, str]] | None = None) -> Path:
    plan = work.work.parent / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "dir": str(work.work),
                "tasks": [
                    {
                        "id": "one",
                        "subject": "feat: extend the report",
                        "task": "Extend the report",
                        "files": ["tools/report.py"],
                    }
                ]
                if tasks is None
                else tasks,
                "trailers": ["Co-Authored-By: Claude <noreply@anthropic.com>"],
                "failure_model": "Empty selections print zero totals.",
            }
        )
    )
    return plan


@pytest.mark.parametrize(
    "location", ["unset", "relative", "data-link", "ember-link", "worktrees-link"]
)
def test_worktree_paths_and_templates_follow_data_environment(
    tmp_path: Path, env: dict[str, str], location: str
) -> None:
    if location == "unset":
        del env["XDG_DATA_HOME"]
    elif location == "relative":
        env["XDG_DATA_HOME"] = "relative-data"
    else:
        data = Path(env["XDG_DATA_HOME"])
        link = {
            "data-link": data,
            "ember-link": data / "ember",
            "worktrees-link": data / "ember" / "worktrees",
        }[location]
        target = tmp_path / "private-target"
        target.mkdir(mode=0o700)
        link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
    work = case("flow-lead-stop-waits", tmp_path, env)
    result = work.emb("flow", "--plan", str(flow_plan(work)), "--start")
    assert result.returncode == 0, result.stderr
    flow_id = result.stdout.splitlines()[0].removeprefix("flow ")
    tree = worktree_path(work, flow_id)
    assert tree.is_dir()
    assert f"worktree {tree}\n" in result.stdout
    report = json.loads(work.emb("flow", "--watch", flow_id, "--json").stdout)
    assert report["worktree"] == str(tree)
    task = report["tasks"][1]
    assert task["worktree"] == str(worktree_path(work, task["writer_run"]))
    assert not (work.work / "relative-data").exists()


@pytest.mark.parametrize("name", FLOW_CASES)
def test_flow_start_and_merge(tmp_path: Path, env: dict[str, str], name: str) -> None:
    work = case(name, tmp_path, env)
    plan = flow_plan(work)
    head = work.git("rev-parse", "HEAD")
    result = work.emb("flow", "--plan", str(plan), "--start", "--claim", "run-abc123")
    assert result.returncode == 0, result.stderr
    flow_id = result.stdout.splitlines()[0].removeprefix("flow ")
    assert re.fullmatch(r"flow-\d{8}-\d{6}-[0-9a-f]{6}", flow_id)
    branch = f"ember/flow/{flow_id}"
    tree = worktree_path(work, flow_id)
    assert result.stdout.splitlines() == [
        f"flow {flow_id}",
        f"branch {branch}",
        f"worktree {tree}",
    ]
    assert result.stderr == f"next: emb flow --watch {flow_id} --json\n"
    assert work.git("rev-parse", f"{branch}^") == head
    assert work.git("log", "-1", "--format=%B", branch) == (
        "feat: extend the report\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    )
    assert work.git("diff", "--name-only", f"HEAD...{branch}") == (
        "tests/test_report.py\ntools/report.py"
    )
    assert work.git("status", "--porcelain") == ""
    state = json.loads((work.eval / "state.json").read_text())
    assert state["flows"][flow_id] == {
        "dir": str(work.work),
        "main": str(work.work),
        "branch": branch,
        "worktree": str(tree),
    }
    assert state["runs"] == {}
    check = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", "tests"],
        cwd=tree,
        env={**work.env, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    watch = work.emb("flow", "--watch", flow_id, "--json")
    report = json.loads(watch.stdout)
    assert report["flow"] == flow_id
    assert report["worktree"] == str(tree)
    assert report["tip"] == work.git("rev-parse", branch)
    if name == "flow-lead-stop-waits":
        assert report["event"] == "lead_stop"
        stopped_task = report["tasks"][1]
        assert stopped_task["worktree"] == str(
            worktree_path(work, stopped_task["writer_run"])
        )
        assert report["tasks"][1]["findings"] == {"must": 0, "should": 0, "lead": 1}
        answer = work.emb("flow", "--lead", flow_id, "strict", "--send", work.brief())
        assert answer.stdout == f"queued answer 1: send for strict\nflow {flow_id}\n"
        watch = work.emb("flow", "--watch", flow_id, "--json")
        report = json.loads(watch.stdout)
    assert report["event"] == "end"
    assert all(task["status"] == "merged" for task in report["tasks"])
    assert report["next"] + "\n" == watch.stderr
    tip = work.git("rev-parse", branch)
    merged = work.emb(
        "merge",
        branch,
        "--into",
        f"claude/{name}",
        "--verdict",
        "keep",
        "--note",
        "Keep",
    )
    assert merged.returncode == 0, merged.stderr
    assert (
        f"removed {branch}: removed worktree {tree}; deleted at {tip}" in merged.stdout
    )
    assert work.git("branch", "--list", branch) == ""
    assert not tree.exists()
    assert work.git("rev-parse", "HEAD^2") == tip


@pytest.mark.parametrize(
    "tasks",
    [
        [],
        [{"id": "first", "task": "Extend report", "files": ["tools/report.py"]}],
        [{"id": "first", "subject": " ", "task": "x", "files": ["tools/report.py"]}],
    ],
)
def test_flow_refuses_empty_tasks_or_missing_subject(
    tmp_path: Path, env: dict[str, str], tasks: list[dict[str, Any]]
) -> None:
    work = case("flow-gap-check-first", tmp_path, env)
    plan = flow_plan(work)
    document = json.loads(plan.read_text())
    document["tasks"] = tasks
    plan.write_text(json.dumps(document))
    result = work.emb("flow", "--plan", str(plan), "--start")
    assert result.returncode == 64, result.stderr
    assert "next: emb flow --help" in result.stderr
    assert work.git("branch", "--list", "ember/*") == ""


@pytest.mark.parametrize("content", [None, "", "{bad", "[]"])
def test_flow_unreadable_plan(
    tmp_path: Path, env: dict[str, str], content: str | None
) -> None:
    work = case("flow-gap-check-first", tmp_path, env)
    plan = tmp_path / "plan.json"
    if content is not None:
        plan.write_text(content)
    result = work.emb("flow", "--plan", str(plan), "--start")
    assert result.returncode == 66
    assert result.stderr == f"emb flow: cannot read {plan}\nnext: ls -l {plan}\n"
    assert work.git("branch", "--list", "ember/*") == ""


def test_flow_without_files_keeps_head(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("small-change-no-worker", tmp_path, env)
    (work.eval / "replies/flow-start.code").write_text("0\n")
    plan = flow_plan(work)
    head = work.git("rev-parse", "HEAD")
    result = work.emb("flow", "--plan", str(plan), "--start")
    assert result.returncode == 0, result.stderr
    flow_id = result.stdout.splitlines()[0].split()[1]
    assert work.git("rev-parse", f"ember/flow/{flow_id}") == head
    (work.eval / "replies/flow-status.out").write_text("{{FLOW}}")
    assert work.emb("flow", "--status").stdout == flow_id
    assert work.emb("flow", "--status=another").stdout == "another"


def test_flow_start_canned_failure_builds_nothing(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("flow-gap-check-first", tmp_path, env)
    (work.eval / "replies/flow-start.code").write_text("75\n")
    (work.eval / "replies/flow-start.err").write_text("refused {{PLAN}}\n")
    plan = flow_plan(work)
    result = work.emb("flow", "--plan", str(plan), "--start")
    assert result.returncode == 75
    assert result.stderr == f"refused {plan}\n"
    assert not work.git("branch", "--list", "ember/*")
    assert json.loads((work.eval / "state.json").read_text())["flows"] == {}


def test_flow_cleanup_keeps_dirty_worktree(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("flow-gap-check-first", tmp_path, env)
    result = work.emb("flow", "--plan", str(flow_plan(work)), "--start")
    flow_id = result.stdout.splitlines()[0].split()[1]
    branch = f"ember/flow/{flow_id}"
    tree = worktree_path(work, flow_id)
    extra = tree / "untracked.txt"
    extra.write_text("kept\n")
    merged = work.emb(
        "merge",
        branch,
        "--into",
        "claude/flow-gap-check-first",
        "--verdict",
        "keep",
        "--note",
        "Keep",
    )
    assert merged.returncode == 0, merged.stderr
    assert f"kept {branch}: the worktree {tree} has changes" in merged.stdout
    assert extra.read_text() == "kept\n"
    extra.unlink()
    cleaned = work.emb(
        "cleanup", "--merged-into", "claude/flow-gap-check-first", "--branch", branch
    )
    assert cleaned.returncode == 0, cleaned.stderr
    assert f"removed {branch}: removed worktree {tree}" in cleaned.stdout
    assert not tree.exists()
    assert not work.git("branch", "--list", branch)


@pytest.mark.parametrize("code", [0, 83])
def test_panel_start_writes_summary(
    tmp_path: Path, env: dict[str, str], code: int
) -> None:
    work = case("panel-named-members-compared", tmp_path, env)
    (work.eval / "replies/panel-start.code").write_text(str(code))
    result = work.emb("panel", "--question-file", work.brief(), "--dir", ".")
    assert result.returncode == code, result.stderr
    panel_id = result.stdout.splitlines()[0].removeprefix("panel ")
    assert re.fullmatch(r"panel-\d{8}-\d{6}-[0-9a-f]{6}", panel_id)
    summary = work.eval / "runs" / panel_id / "summary.md"
    assert f"summary {summary}\n" in result.stdout
    assert str(summary) in result.stderr
    assert "member " not in result.stdout
    assert summary.stat().st_mode & 0o777 == 0o600
    text = summary.read_text()
    assert text.startswith(f"# Panel {panel_id}\n\nCheckout: {work.work}\n")
    assert "compute_totals" in text
    assert "Reason: the turn ended with no reply\n\n(no answer)" in text
    state = json.loads((work.eval / "state.json").read_text())
    assert state["panels"] == {
        panel_id: {"summary": str(summary), "base": work.git("rev-parse", "HEAD")}
    }
    for number, engine, model, run_id in (
        (1, "codex", "gpt-6-astra", "codex-20261102-120000-aaaaa1"),
        (2, "claude", "opus", "claude-20261102-120000-aaaaa2"),
        (
            3,
            "opencode",
            "openrouter/deepseek/deepseek-v4.1-flash-1017",
            "opencode-20261102-120000-aaaaa3",
        ),
    ):
        assert (f"member {number} {engine}:{model} · default · ") in result.stderr
        assert (
            f"WARNING: member {number} ({engine}:{model}): read-only run {run_id} "
            "does not see uncommitted review-notes.md\n"
        ) in result.stderr
    assert (work.work / "review-notes.md").read_text() == "Owner review notes.\n"
    (work.eval / "replies/panel-status.out").write_text("{{PANEL}} {{SUMMARY}}")
    assert work.emb("panel", "--status").stdout == f"{panel_id} {summary}"
    assert work.emb("panel", "--status=another").stdout == (
        f"another {work.eval}/runs/another/summary.md"
    )


def test_panel_json_keeps_completion_lines_on_stderr(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("panel-named-members-compared", tmp_path, env)
    result = work.emb("panel", "--question-file", work.brief(), "--dir", ".", "--json")
    assert result.returncode == 83
    document = json.loads(result.stdout)
    assert document["exit"] == 83
    assert len(document["members"]) == 3
    assert document["members"][0]["exit"] == 0
    assert document["members"][2]["bytes"] == 0
    assert document["members"][2]["model"] == (
        "openrouter/deepseek/deepseek-v4.1-flash-1017"
    )
    assert " · answered · 31s · exit 0 · " in result.stderr
    assert " · failed · 33s · exit 80 · 0 bytes · " in result.stderr


@pytest.mark.parametrize("as_json", [False, True])
def test_panel_progress_case(
    tmp_path: Path, env: dict[str, str], as_json: bool
) -> None:
    work = case("panel-progress-reported", tmp_path, env)
    options = ("--json",) if as_json else ()
    listing = work.emb("panel", "--status", *options)
    panel_id = "panel-20261102-120000-aaaaa0"
    first = work.emb("panel", "--status", panel_id, *options)
    second = work.emb("panel", "--status", panel_id, *options)
    for result in (listing, first, second):
        assert result.returncode == 0 and result.stderr == ""
    if as_json:
        item = json.loads(listing.stdout)[0]
        assert item["live"] is True
        assert item["denied_paths"] == []
        assert item["member_count"] == 3 and "members" not in item
        assert item["answered"] == 1
        assert [member["n"] for member in item["running_members"]] == [2, 3]
        assert item["running_members"][1] == {
            "n": 3,
            "seconds": 0.0,
            "last_screen_change_at": None,
        }
        before, after = json.loads(first.stdout), json.loads(second.stdout)
        assert before["status"] == after["status"] == "running"
        assert before["denied_paths"] == after["denied_paths"] == []
        assert all(member["denied_paths"] == [] for member in after["members"])
        assert before["members"][0] == after["members"][0]
        assert before["members"][0]["seconds"] == 31.0
        assert [member["seconds"] for member in before["members"]] == [31, 120, 60]
        assert [member["seconds"] for member in after["members"]] == [31, 180, 120]
        assert (
            before["members"][1]["last_screen_change_at"]
            < after["members"][1]["last_screen_change_at"]
        )
        assert after["members"][2]["last_screen_change_at"] is None
    else:
        assert "1/3 answered" in listing.stdout
        assert " · running · 0s · " in listing.stdout
        assert "last screen change -" in listing.stdout
        assert " · running · 120s · " in first.stdout
        assert " · running · 180s · " in second.stdout
        assert "last screen change 1793620970.0" in second.stdout


def test_plan_and_directory_templates(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("flow-gap-check-first", tmp_path, env)
    plan = flow_plan(work)
    result = work.emb(
        "flow", "--plan", "../plan.json", "--dir", "/unused", "--dry-run", "--json"
    )
    data = json.loads(result.stdout)
    assert data["plan"] == str(plan)
    assert data["dir"] == str(work.work)
    assert str(plan) in data["gapcheck"]
    assert "--json" in data["gapcheck"]
    (work.eval / "replies/gapcheck-brief.out").write_text("{{PLAN}} {{DIR}}")
    plan.write_text("not JSON")
    result = work.emb("gapcheck", "--brief", "--plan", "../plan.json", "--dir", "..")
    assert result.stdout == f"{plan} {tmp_path}"


@pytest.mark.parametrize("name", FLOW_CASES)
def test_flow_case_reports_have_reference_fields(
    tmp_path: Path, env: dict[str, str], name: str
) -> None:
    work = case(name, tmp_path, env)
    gap = json.loads(
        work.emb("gapcheck", "--plan", str(flow_plan(work)), "--json").stdout
    )
    assert set(gap) == {
        "plan",
        "plan_steps",
        "failure_model",
        "previous",
        "previous_plan",
        "next_id",
        "settled",
        "closed",
        "dispositions",
        "status",
        "ask",
        "reviewer",
        "run",
        "steps",
        "ranked",
        "counts",
        "uncovered_steps",
        "unknown_steps",
        "parse_error",
    }
    previous.validate(gap)
    assert set(gap["counts"]) == {"high", "medium", "low"}
    assert gap["uncovered_steps"] == gap["unknown_steps"] == []
    assert gap["parse_error"] is None
    assert gap["counts"] == {
        harm: sum(g["harm"] == harm for g in gap["ranked"])
        for harm in ("high", "medium", "low")
    }
    if name == "flow-lead-stop-waits":
        assert gap["ranked"] == []
        assert gap["counts"] == {"high": 0, "medium": 0, "low": 0}
    for _ in range(2 if name == "flow-lead-stop-waits" else 1):
        report = json.loads(work.emb("flow", "--watch", "flow-id", "--json").stdout)
        assert set(report) == {
            "event",
            "flow",
            "status",
            "reason",
            "exit",
            "branch",
            "worktree",
            "tip",
            "dir",
            "tasks",
            "stops",
            "abandoned",
            "next",
        }
        for task in report["tasks"]:
            assert task["denied_paths"] == []
            assert set(task) == {
                "id",
                "status",
                "reason",
                "round",
                "writer_run",
                "branch",
                "worktree",
                "merge_commit",
                "outside_files",
                "denied_paths",
                "cleanup",
                "choices",
                "findings",
                "finding_details",
            }


def test_default_choices_include_multi_panel_and_provider(
    generated_eval_fixtures: Path,
) -> None:
    for name in (
        "choices-claim.out",
        "choices-read-id.out",
        "choices-read.out",
        "menu.json",
    ):
        data = json.loads(
            (generated_eval_fixtures / "_stubs/replies" / name).read_text()
        )
        if name != "menu.json":
            for key, value in (
                ("provider.capacity_retries", "2"),
                ("provider.capacity_wait", "30"),
            ):
                assert data["answers"][key] == {"value": value, "layer": "default"}
        menu = data if name == "menu.json" else data["menu"]
        assert menu["multi.runs_as"] == {
            "value": "ask",
            "state": "ask",
            "reason": "asks at each run",
        }
        assert menu["panel.members"] == {"value": [], "state": "empty", "members": []}


@pytest.mark.parametrize("name", FLOW_CASES)
def test_gap_answers_and_repeats_carry_settled_history(
    tmp_path: Path, env: dict[str, str], name: str
) -> None:
    work = case(name, tmp_path, env)
    plan = flow_plan(
        work,
        [
            {"id": "first", "task": "First step"},
            {"id": "second", "task": "Second step"},
        ],
    )
    saved = tmp_path / "gapcheck-plan-1.json"
    first = work.emb("gapcheck", "--plan", str(plan), "--json")
    saved.write_text(first.stdout)
    made = json.loads(first.stdout)
    answers = {}
    for step in made["steps"]:
        for gap in step["gaps"]:
            answers[gap["id"]] = f"Chosen answer for {gap['id']}"
            result = work.emb(
                "gapcheck",
                "--answer",
                str(saved),
                "--id",
                gap["id"],
                "--text",
                answers[gap["id"]],
                "--json",
            )
            assert result.returncode == 0, result.stderr
            previous.validate(json.loads(result.stdout))
    brief = work.emb(
        "gapcheck", "--plan", str(plan), "--previous", str(saved), "--brief"
    )
    for identity, answer in answers.items():
        assert identity in brief.stdout and answer in brief.stdout
    for number in (2, 3):
        dry = work.emb(
            "flow", "--plan", str(plan), "--dry-run", "--previous", str(saved), "--json"
        )
        assert "--previous" in json.loads(dry.stdout)["gapcheck"]
        checked = work.emb(
            "gapcheck", "--plan", str(plan), "--previous", str(saved), "--json"
        )
        assert checked.returncode == 0, checked.stderr
        made = previous.validate(json.loads(checked.stdout))
        assert made["ranked"] == []
        assert made["counts"] == dict(high=0, medium=0, low=0)
        assert {gap["id"]: gap["answer"] for gap in made["settled"]} == answers
        assert made["next_id"] == json.loads(first.stdout)["next_id"]
        saved = tmp_path / f"gapcheck-plan-{number}.json"
        saved.write_text(checked.stdout)


def test_repeat_links_invalidated_answer_and_keeps_closed_history(
    tmp_path: Path, env: dict[str, str]
) -> None:
    work = case("flow-gap-check-first", tmp_path, env)
    plan = flow_plan(
        work,
        [
            {"id": "first", "task": "First step"},
            {"id": "second", "task": "Second step"},
        ],
    )
    saved = tmp_path / "gapcheck-plan-1.json"
    saved.write_text(work.emb("gapcheck", "--plan", str(plan), "--json").stdout)
    for identity in ("g1", "g2", "g3"):
        answer = work.emb(
            "gapcheck",
            "--answer",
            str(saved),
            "--id",
            identity,
            "--text",
            f"Answer {identity}",
        )
        assert answer.returncode == 0 and not answer.stdout
    prior = json.loads(saved.read_text())
    gap = {
        key: prior["steps"][0]["gaps"][0][key]
        for key in ("gap", "harm", "why", "default")
    }
    gap.update(
        earlier="g1", gap="The changed plan contradicts the earlier matching answer."
    )
    changed = json.loads(plan.read_text())
    changed["tasks"][0]["task"] = "Match item names ignoring case."
    plan.write_text(json.dumps(changed))
    review = {
        "steps": [{"step": 1, "gaps": [gap]}, {"step": 2, "gaps": []}],
        "dispositions": [
            {"id": "g1", "status": "reopened", "reason": "Matching changed."},
            {"id": "g2", "status": "closed", "reason": "The task was removed."},
            {"id": "g3", "status": "settled", "reason": "Revenue is unchanged."},
        ],
    }
    (work.eval / "replies/gapcheck-repeat.reply").write_text(json.dumps(review))
    checked = work.emb(
        "gapcheck", "--plan", str(plan), "--previous", str(saved), "--json"
    )
    assert checked.returncode == 0, checked.stderr
    made = previous.validate(json.loads(checked.stdout))
    [new] = made["steps"][0]["gaps"]
    assert new["id"] == "g4" and new["earlier"] == "g1" and new["answer"] is None
    assert made["next_id"] == 5
    assert [gap["id"] for gap in made["settled"]] == ["g3"]
    assert made["closed"][0]["reason"] == "The task was removed."
    saved.write_text(checked.stdout)
    (work.eval / "replies/gapcheck-repeat.reply").unlink()
    assert (
        work.emb(
            "gapcheck",
            "--answer",
            str(saved),
            "--id",
            "g4",
            "--text",
            "Changed matching",
        ).returncode
        == 0
    )
    again = work.emb(
        "gapcheck", "--plan", str(plan), "--previous", str(saved), "--json"
    )
    carried = previous.validate(json.loads(again.stdout))
    assert carried["next_id"] == 5 and carried["closed"] == made["closed"]
    assert {gap["id"] for gap in carried["settled"]} == {"g3", "g4"}


@pytest.mark.parametrize("missing", ["answer", "id"])
def test_repeat_accepts_legacy_unanswered_results(
    tmp_path: Path, env: dict[str, str], missing: str
) -> None:
    work = case("owner-away-named-workers", tmp_path, env)
    plan = flow_plan(
        work,
        [
            {"id": "first", "task": "First step"},
            {"id": "second", "task": "Second step"},
        ],
    )
    made = json.loads(work.emb("gapcheck", "--plan", str(plan), "--json").stdout)
    for step in made["steps"]:
        for gap in step["gaps"]:
            del gap["answer"]
            if missing == "id":
                del gap["id"], gap["earlier"]
    if missing == "id":
        made["ranked"] = [
            {"step": step["step"], **gap}
            for step in made["steps"]
            for gap in step["gaps"]
        ]
        for key in (
            "next_id",
            "previous",
            "previous_plan",
            "settled",
            "closed",
            "dispositions",
        ):
            del made[key]
    saved = tmp_path / "gapcheck-plan-1.json"
    saved.write_text(json.dumps(made))
    checked = work.emb(
        "gapcheck", "--plan", str(plan), "--previous", str(saved), "--json"
    )
    assert checked.returncode == 0, checked.stderr
    repeated = previous.validate(json.loads(checked.stdout))
    assert repeated["settled"] == []
    [gap] = repeated["steps"][1]["gaps"]
    assert gap["id"] == "g2" and gap["earlier"] == "g1"
    assert gap["answer"] is None


@pytest.mark.parametrize(
    ("identity", "text", "code"), [("g9", "Answer", 64), ("g1", " ", 64)]
)
def test_answer_refusals_leave_result_unchanged(
    tmp_path: Path, env: dict[str, str], identity: str, text: str, code: int
) -> None:
    work = case("flow-gap-check-first", tmp_path, env)
    saved = tmp_path / "gapcheck-plan-1.json"
    original = work.emb("gapcheck", "--plan", str(flow_plan(work)), "--json").stdout
    saved.write_text(original)
    checked = work.emb(
        "gapcheck", "--answer", str(saved), "--id", identity, "--text", text
    )
    assert checked.returncode == code and saved.read_text() == original
    assert work.calls()[-1]["route"] == "gapcheck-answer"
