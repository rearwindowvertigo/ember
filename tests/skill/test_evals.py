import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ember.cli import main

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
    "send-refused-foreign-messages",
    "owner-line-not-run",
    "merge-and-cleanup-keeps",
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
            "--retry",
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


def test_the_eight_cases_and_no_others() -> None:
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
    scripts = [RUNNER, STUBS / "install.sh", STUBS / "bin" / "emb"]
    scripts += [EVALS / case / "place-files.sh" for case in CASES]
    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_help_reply_matches_the_parser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    assert (STUBS / "replies" / "help.out").read_text() == expected, (
        "regenerate skill/evals/_stubs/replies/help.out as skill/evals/README.md says"
    )


# The stub kit, in a case's working directory


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
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
    worktree = work.work / ".ember" / "worktrees" / run_id
    commit = work.git("rev-parse", branch)
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
    state = json.loads((work.eval / "state.json").read_text())
    assert state["runs"][run_id]["claim"] == "run-abc123"
    status = work.emb("run", "--status", run_id, "--json")
    assert json.loads(status.stdout)["branch"] == branch


def test_read_only_start_makes_no_branch(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief(),
        "--read-only",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "codex gpt-6.1-sol-mini · medium · " in result.stdout
    assert "branch -\n" in result.stdout
    assert work.git("branch", "--list", "ember/*") == ""


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
    return run_id, f"ember/codex/{run_id}", work.work / ".ember" / "worktrees" / run_id


def test_merge_merges_and_cleans_up(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    run_id, branch, worktree = start(work)
    tip = work.git("rev-parse", branch)
    refused = work.emb("merge", branch, "--into", "claude/report-since")
    assert refused.returncode == 64
    missing = work.emb(
        "merge", "nope", "--into", "claude/report-since", "--verdict", "keep"
    )
    assert missing.returncode == 65
    result = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep"
    )
    assert result.returncode == 0, result.stderr
    merge = work.git("rev-parse", "HEAD")
    assert result.stdout == (
        f"merged {branch} at {tip} into claude/report-since: {merge}\n"
        f"removed {branch}: closed {run_id}; removed worktree {worktree}; "
        f"deleted at {tip}\n"
    )
    assert work.git("log", "-1", "--format=%s") == (
        f"Merge branch '{branch}' into claude/report-since"
    )
    assert work.git("rev-parse", "HEAD^2") == tip
    assert not worktree.exists()
    assert work.git("branch", "--list", branch) == ""
    again = work.emb("cleanup", "--merged-into", "claude/report-since")
    assert again.stdout == ""
    assert (
        again.stderr == "emb cleanup: no Ember run branch against claude/report-since\n"
    )


def test_merge_keeps_a_dirty_worktree(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("merge-and-cleanup-keeps", tmp_path, env)
    _, branch, worktree = start(work)
    assert (worktree / "tools" / "__pycache__" / "report.cpython-314.pyc").is_file()
    result = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[1] == (
        f"kept {branch}: the worktree {worktree} has changes; "
        f"next: git -C {worktree} status --short"
    )
    assert worktree.is_dir()
    assert work.git("branch", "--list", branch) != ""
    assert work.git("log", "-1", "--format=%s") == (
        f"Merge branch '{branch}' into claude/report-since"
    )
    again = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep"
    )
    assert again.stdout.startswith(f"already contained: {branch} at ")


def test_merge_refuses_a_dirty_target(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-and-menu-writer", tmp_path, env)
    _, branch, _ = start(work)
    (work.work / "notes.txt").write_text("mine\n")
    result = work.emb(
        "merge", branch, "--into", "claude/report-since", "--verdict", "keep"
    )
    assert result.returncode == 75
    assert result.stderr.splitlines()[-1] == f"next: git -C {work.work} status --short"


def test_claim_lost_then_gone(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("claim-lost-then-gone", tmp_path, env)
    first = work.emb("choices", "--claim", "--id", "run-abc123")
    assert (first.returncode, first.stdout, first.stderr) == (75, "", "")
    second = work.emb("choices", "--claim", "--id", "run-abc123")
    assert second.returncode == 0
    reply = json.loads(second.stdout)
    assert reply["claim"] == {"id": "run-abc123", "status": "gone"}
    assert {row["state"] for row in reply["menu"].values()} == {"ask"}
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


def test_owner_line_start_makes_nothing(tmp_path: Path, env: dict[str, str]) -> None:
    work = case("owner-line-not-run", tmp_path, env)
    result = work.emb(
        "run", "--engine", "codex", "--dir", ".", "--task-file", work.brief()
    )
    assert result.returncode == 69
    assert result.stderr.splitlines()[-1] == "owner: sign in to Codex: codex login"
    assert result.stdout == ""
    assert work.git("branch", "--list", "ember/*") == ""


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


def test_runner_needs_its_arguments(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(RUNNER)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 64
    assert result.stderr.startswith("usage: run_case.sh")


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
        ("use --task x", 1),
        ("Delegation and cx-delegate", 2),
        ("one machine", 0),
        ("two machines", 1),
    ],
)
def test_name_check_matches_whole_tokens(text: str, hits: int) -> None:
    assert sum(len(pattern.findall(text)) for pattern in FORBIDDEN) == hits
