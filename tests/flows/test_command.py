import fcntl
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ember import branches, choices, paths, prepare, tmux
from ember.cli import build_parser
from ember.flows import command, lock, plan, record
from ember.gapcheck import command as gap_command
from ember.gapcheck import plan as gap_plan
from ember.gapcheck import previous as gap_previous
from ember.runs.core import Core
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    git,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
    tmux_log,
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, str]]:
    environment = make_env(tmp_path)
    yield environment
    kill_sessions(tmp_path)


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    return make_repo(tmp_path, env)


@pytest.fixture
def plan_file(tmp_path: Path, repo: Path) -> Path:
    path = tmp_path / "plan with spaces.json"
    path.write_text(
        json.dumps(
            {
                "dir": str(repo),
                "writer": {"engine": "codex", "model": "gpt-5", "effort": "high"},
                "reviewer": {"engine": "codex", "model": "gpt-5", "effort": "low"},
                "tasks": [
                    {
                        "id": "a",
                        "subject": "feat: a",
                        "task": "First",
                        "files": ["a.py"],
                    },
                    {
                        "id": "b",
                        "subject": "feat: b",
                        "task": "Second",
                        "files": ["b.py"],
                        "after": ["a"],
                    },
                ],
            }
        )
    )
    return path


def invoke(
    env: Mapping[str, str],
    *options: str,
    factory: Callable[..., Core] | None = None,
    argv: Callable[[str], list[str]] | None = None,
) -> tuple[int, str, str]:
    def make(environment: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        return Core(
            environment,
            load_driver=lambda name: StubDriver(name, script_path(env)),
            **kwargs,
        )

    args = build_parser().parse_args(["flow", *options])
    out, err = io.StringIO(), io.StringIO()
    code = command.main(
        args,
        env,
        core_factory=factory or make,
        runner_argv=argv or (lambda _: ["true"]),
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def flow(plan_file: Path, env: dict[str, str]) -> dict[str, Any]:
    document = plan.load(plan_file)
    flow_id = record.new_id()
    branch = branches.create_flow_branch(document.dir, flow_id, document.base, env)
    worker = plan.Worker("codex", "gpt-5", "high", "plan")
    result = record.new_flow(
        flow_id,
        document,
        top=str(branch.top),
        main=str(branch.main),
        worktree=str(branch.worktree),
        base=branch.base,
        writer=worker,
        reviewer=worker,
        stall_minutes=10,
        claim=None,
    )
    record.create(result, document.text, env)
    return result


def stopped(flow: dict[str, Any], env: Mapping[str, str]) -> None:
    flow["status"] = "stopped"
    task = flow["tasks"][0]
    task.update(
        status="stopped",
        reason="needs the lead",
        branch=flow["branch"],
        worktree=flow["worktree"],
    )
    record.save(flow, env)


def end(flow: dict[str, Any], env: Mapping[str, str], code: int = 0) -> None:
    flow["status"] = "done" if code == 0 else "failed"
    if code == 0:
        for task in flow["tasks"]:
            task["status"] = "merged"
    flow["ended_at"] = time.time()
    record.save(flow, env)
    record.append_event(
        flow["id"],
        {
            "kind": "end",
            "status": flow["status"],
            "reason": flow["reason"],
            "exit": code,
        },
        env,
    )


def checked_result(plan_file: Path) -> dict[str, Any]:
    document = gap_plan.load(plan_file)
    made = gap_command._result(document, None)
    made.update(
        gap_command._read_reply(
            json.dumps(
                {
                    "steps": [
                        {"step": step.number, "gaps": []} for step in document.steps
                    ]
                }
            ),
            document,
            None,
        )
    )
    gap_command._mark_asks(made, None)
    return gap_previous.validate(made)


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("with_previous", [False, True])
def test_dry_run(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    repo: Path,
    as_json: bool,
    with_previous: bool,
) -> None:
    choices.claim("flow-claim", env=env)
    before = git(repo, "branch", "--list", env=env)
    previous = str(tmp_path / "owner's previous result.json")
    if with_previous:
        Path(previous).write_text(json.dumps(checked_result(plan_file)))
    code, out, err = invoke(
        env,
        "--plan",
        str(plan_file),
        "--dry-run",
        "--claim",
        "flow-claim",
        *(["--previous", previous] if with_previous else []),
        *(["--json"] if as_json else []),
    )
    assert code == 0, err
    if as_json:
        result = json.loads(out)
        assert result["waves"] == [["a"], ["b"]]
        assert result["writer"] == {
            "engine": "codex",
            "model": "gpt-5",
            "effort": "high",
            "source": "plan",
            "speed": "standard",
        }
        assert result["reviewer"] == {
            "engine": "codex",
            "model": "gpt-5",
            "effort": "low",
            "source": "plan",
            "speed": "standard",
        }
        assert result["same_model"] is True
        assert result["check"] == []
        assert result["writer_check"] is None
        assert result["tasks"] == [
            {"id": "a", "writer_check": None},
            {"id": "b", "writer_check": None},
        ]
        gapcheck = result["gapcheck"]
    else:
        assert out.splitlines()[:3] == [
            "plan, writer: codex gpt-5 · high · standard (plan)",
            "plan, reviewer: codex gpt-5 · low · standard (plan)",
            "same model: writer and reviewer both resolve to codex gpt-5",
        ]
        assert out.count("same model:") == 1
        assert "plan, writer:" not in err
        assert "plan, reviewer:" not in err
        assert "wave 1:\n  a: a.py\nwave 2:\n  b: b.py" in out
        assert "\nwriter check " not in out
        gapcheck = out.split("gap check: ")[1].strip()
    assert shlex.split(gapcheck) == [
        "emb",
        "gapcheck",
        "--plan",
        str(plan_file),
        "--dir",
        str(repo),
        "--json",
        *(["--previous", previous] if with_previous else []),
        "--engine",
        "codex",
        "--model",
        "gpt-5",
        "--effort",
        "low",
        "--claim",
        "flow-claim",
    ]
    assert "same model:" not in err
    assert "WARNING: no failure_model" in err and "WARNING: no check" in err
    assert not paths.runs_dir(env).exists()
    assert not tmux_log(tmp_path)
    assert git(repo, "branch", "--list", env=env) == before


@pytest.mark.parametrize(
    "default", [None, [], [["pytest", "tests/a b.py"], ["printf", "%s", "a\nb"]]]
)
def test_dry_run_writer_checks(
    env: dict[str, str], plan_file: Path, default: list[list[str]] | None
) -> None:
    data = json.loads(plan_file.read_text())
    if default is not None:
        data["writer_check"] = default
        data["check"] = ["make", "check"]
    data["tasks"][1]["writer_check"] = []
    plan_file.write_text(json.dumps(data))
    options = ("--plan", str(plan_file), "--dry-run")
    code, out, err = invoke(env, *options, "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["writer_check"] == default
    assert result["tasks"] == [
        {"id": "a", "writer_check": default},
        {"id": "b", "writer_check": []},
    ]
    code, out, err = invoke(env, *options)
    assert code == 0, err
    lines = out.splitlines()
    if default is None:
        assert lines[lines.index("check none") + 1] == "writer check b: none"
    else:
        first = "pytest 'tests/a b.py', printf %s $'a\\nb'" if default else "none"
        assert lines[lines.index("check make check") + 1] == (
            f"writer check a: {first} | b: none"
        )


@pytest.mark.parametrize(
    ("setting", "plan_rounds", "expected"),
    [
        (None, None, (2, "setting")),
        ("1", None, (0, "setting")),
        ("4", None, (3, "setting")),
        ("1", 3, (3, "plan")),
    ],
)
def test_dry_run_review_rounds(
    env: dict[str, str],
    plan_file: Path,
    setting: str | None,
    plan_rounds: int | None,
    expected: tuple[int, str],
) -> None:
    if setting is not None:
        choices.set_settings({"flow.review_rounds": setting}, env=env)
    if plan_rounds is not None:
        value = json.loads(plan_file.read_text())
        value["rounds"] = plan_rounds
        plan_file.write_text(json.dumps(value))
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert (result["rounds"], result["rounds_source"]) == expected
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run")
    assert code == 0, err
    rounds, source = expected
    assert (
        f"rounds {rounds} fix rounds, at most {rounds + 1} reviews per task ({source})"
        in out
    )


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--dry-run"],
        ["--start"],
        ["--dry-run", "--start", "--plan", "x"],
        ["--status", "--plan", "x"],
        ["--watch", "x", "--writer", "codex:m"],
        ["--resume", "x", "--claim", "x"],
        ["--status", "--reviewer", "codex:m"],
        ["--dry-run", "--plan", "x", "--stall", "1"],
        ["--start", "--plan", "x", "--json"],
        ["--resume", "x", "--json"],
        ["--lead", "x", "a", "--drop", "--json"],
        ["--status", "--accept"],
        ["--lead", "x", "a"],
        ["--lead", "x", "a", "--accept", "--drop"],
        ["--status", "--commit", "abc"],
        ["--lead", "x", "a", "--drop", "--commit", "abc"],
        ["--lead", "x", "a", "--drop", "--reviewer", "codex:m"],
        ["--watch", "x", "--status"],
        ["--status", "--send", "x"],
    ],
)
def test_usage(env: dict[str, str], options: list[str]) -> None:
    code, out, err = invoke(env, *options)
    assert code == 64 and not out
    assert err.endswith("next: emb flow --help\n")
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize(
    "options",
    [
        ["--start", "--plan", "missing-plan.json"],
        ["--watch", "missing-flow"],
        ["--status"],
        ["--lead", "missing-flow", "a", "--drop"],
        ["--resume", "missing-flow"],
        ["--cancel", "missing-flow"],
    ],
)
def test_previous_requires_dry_run(env: dict[str, str], options: list[str]) -> None:
    for flag in (["--previous", "missing-result.json"], ["--gapcheck-use-team"]):
        code, out, err = invoke(env, *options, *flag)
        assert code == 64 and not out
        assert f"{flag[0]} needs --dry-run" in err
        assert err.endswith("next: emb flow --help\n")
        assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing", 66),
        ("blank", 64),
        ("problems", 64),
        ("missing dir", 66),
        ("not checkout", 65),
        ("bad base", 64),
    ],
)
def test_plan_errors(
    tmp_path: Path, env: dict[str, str], plan_file: Path, case: str, code: int
) -> None:
    data = json.loads(plan_file.read_text())
    if case == "missing":
        plan_file.unlink()
    elif case == "blank":
        plan_file.write_text(" \n")
    elif case == "problems":
        data.update(rounds=True, parallel=9, extra="unknown")
        plan_file.write_text(json.dumps(data))
    else:
        data.update(
            {"dir": str(tmp_path / "missing")}
            if case == "missing dir"
            else {"dir": str(tmp_path)}
            if case == "not checkout"
            else {"base": "no-such-commit"}
        )
        plan_file.write_text(json.dumps(data))
    actual, out, err = invoke(env, "--plan", str(plan_file), "--dry-run")
    assert actual == code and not out, err
    if case == "problems":
        assert err.count("emb flow:") == 3
    assert "next:" in err
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_ask_rows(env: dict[str, str], plan_file: Path, explicit: bool) -> None:
    choices.set_settings({"single.writer": "ask", "single.reviewer": "ask"}, env=env)
    data = json.loads(plan_file.read_text())
    del data["writer"], data["reviewer"]
    plan_file.write_text(json.dumps(data))
    options = ["--writer", "codex:m", "--reviewer", "codex:m@low"] if explicit else []
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", *options)
    assert code == (0 if explicit else 64), err
    assert "(option)" in out if explicit else "owner: name the flow's writer" in err


def test_auto_flow_pair_keeps_the_gap_reviewer_policy_separate(
    env: dict[str, str], plan_file: Path
) -> None:
    choices.set_settings(
        {"single.writer": "auto", "single.reviewer": "auto", "gapcheck.when": "ask"},
        env=env,
    )
    data = json.loads(plan_file.read_text())
    del data["writer"], data["reviewer"]
    plan_file.write_text(json.dumps(data))
    preview = ["--plan", str(plan_file), "--dry-run", "--json"]
    code, out, err = invoke(env, *preview)
    assert code == 64 and not out
    assert "next: choose a model for the flow's writer and pass --writer" in err

    pair = ["--writer", "codex:writer@low", "--reviewer", "codex:reviewer@high"]
    code, out, err = invoke(env, *preview, *pair)
    assert code == 0, err
    default_gap = shlex.split(json.loads(out)["gapcheck"])
    assert default_gap[default_gap.index("--model") + 1] == "reviewer"
    assert "--confirmed" in default_gap

    code, out, err = invoke(env, *preview, *pair, "--gapcheck-use-team")
    assert code == 0, err
    result = json.loads(out)
    gap = shlex.split(result["gapcheck"])
    assert "--confirmed" in gap and "--model" not in gap and "--engine" not in gap
    assert (result["writer"]["model"], result["writer"]["effort"]) == ("writer", "low")
    assert (result["reviewer"]["model"], result["reviewer"]["effort"]) == (
        "reviewer", "high"
    )  # fmt: skip


@pytest.mark.parametrize("claim_state", ["gone", "unknown"])
def test_claim_refused(env: dict[str, str], plan_file: Path, claim_state: str) -> None:
    if claim_state == "gone":
        choices.finish("claim-123", env=env)
    code, out, err = invoke(
        env, "--plan", str(plan_file), "--start", "--claim", "claim-123"
    )
    assert code == 64 and not out
    assert f"claim claim-123 is {claim_state}" in err
    assert "next: emb choices --claim" in err
    assert not paths.runs_dir(env).exists()


def test_start(
    tmp_path: Path, env: dict[str, str], plan_file: Path, repo: Path
) -> None:
    title = "Refine Overview rows"
    source = json.loads(plan_file.read_text())
    source["title"] = title
    plan_file.write_text(json.dumps(source))
    code, out, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == 0, err
    flow_id = out.splitlines()[0].split()[1]
    current = record.load(flow_id, env)
    assert out.splitlines() == [
        f"flow {flow_id}",
        f"branch {current['branch']}",
        f"worktree {current['worktree']}",
    ]
    assert current["status"] == "starting"
    assert current["title"] == title
    assert current["common"] == str((repo / ".git").resolve())
    assert current["base_branch"] == git(repo, "branch", "--show-current", env=env)
    assert current["writer"]["effort_frozen"] is True
    assert git(repo, "rev-parse", current["branch"], env=env) == current["base"]
    assert current["worktree"] == str(paths.worktree_path(flow_id, env))
    assert not Path(current["worktree"]).is_relative_to(repo)
    assert (
        git(current["worktree"], "branch", "--show-current", env=env)
        == (current["branch"])
    )
    assert not (repo / ".ember").exists()
    calls = tmux_log(tmp_path)
    launch = next(call for call in calls if "new-session" in call)
    assert launch[launch.index("-s") + 1] == flow_id
    assert f"EMBER_FLOW_RUNNER={flow_id}" in launch and launch[-1] == "true"
    assert err.splitlines() == [
        "plan, writer: codex gpt-5 · high · standard (plan)",
        "plan, reviewer: codex gpt-5 · low · standard (plan)",
        "same model: writer and reviewer both resolve to codex gpt-5",
        f"next: emb flow --watch {flow_id} --json",
    ]
    plan_file.unlink()
    (record.folder(flow_id, env) / record.PLAN_NAME).unlink()
    assert record.load(flow_id, env)["title"] == title
    code, out, err = invoke(env, "--status", flow_id, "--json")
    assert code == 0, err
    assert "title" not in json.loads(out)
    code, out, err = invoke(env, "--status", flow_id)
    assert code == 0, err
    assert "title:" not in out


@pytest.mark.parametrize("reviewer", ["codex:other@low", "claude:gpt-5@low"])
@pytest.mark.parametrize("mode", ["dry-run", "json", "start"])
def test_different_workers(
    env: dict[str, str], plan_file: Path, reviewer: str, mode: str
) -> None:
    options = ["--dry-run", "--json"] if mode == "json" else [f"--{mode}"]
    code, out, err = invoke(
        env, "--plan", str(plan_file), "--reviewer", reviewer, *options
    )
    assert code == 0, err
    assert "same model:" not in out + err
    if mode == "json":
        assert json.loads(out)["same_model"] is False
    else:
        engine, model = reviewer.removesuffix("@low").split(":")
        lines = [
            "plan, writer: codex gpt-5 · high · standard (plan)",
            f"option, reviewer: {engine} {model} · low"
            f"{' · standard' if engine == 'codex' else ''} (option)",
        ]
        if mode == "start":
            flow_id = out.splitlines()[0].split()[1]
            assert err.splitlines() == [
                *lines,
                f"next: emb flow --watch {flow_id} --json",
            ]
            assert len(out.splitlines()) == 3
        else:
            assert out.splitlines()[:2] == lines


@pytest.mark.parametrize("setting", [None, ""])
def test_start_without_preparation(
    env: dict[str, str], plan_file: Path, repo: Path, setting: str | None
) -> None:
    if setting is not None:
        choices.set_settings({"worktree.prepare": {str(repo): setting}}, env=env)
    code, out, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == 0, err
    flow_id = out.splitlines()[0].split()[1]
    current = record.load(flow_id, env)
    assert "preparation" not in current
    assert not (record.folder(flow_id, env) / "prepare.log").exists()


def test_start_prepares_before_runner(
    env: dict[str, str], plan_file: Path, repo: Path
) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): "echo prepared"}}, env=env)

    def launch(flow_id: str) -> list[str]:
        current = record.load(flow_id, env)
        saved = current["preparation"]
        assert saved["state"] == "ok" and saved["exit"] == 0
        assert saved["pid"] > 1 and saved["start"] > 0
        log = Path(saved["log"])
        assert log == record.folder(flow_id, env) / "prepare.log"
        assert log.read_text() == "prepared\n"
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        return ["true"]

    code, _, err = invoke(env, "--plan", str(plan_file), "--start", argv=launch)
    assert code == 0, err


@pytest.mark.parametrize(
    ("shell", "reason"),
    [("echo failed; exit 9", "exited 9"), ("echo dirty > leftover", "leftover")],
)
def test_start_preparation_failure_keeps_flow(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    repo: Path,
    shell: str,
    reason: str,
) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): shell}}, env=env)
    code, _, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == 79 and reason in err
    (current,), errors = record.list_flows(env)
    assert not errors
    flow_id = current["id"]
    assert current["status"] == "failed"
    assert current["preparation"]["state"] == "failed"
    assert all(task["writer_run"] is None for task in current["tasks"])
    assert Path(current["worktree"]).is_dir()
    assert f"next: emb flow --resume {flow_id}" in err
    assert str(record.folder(flow_id, env) / "prepare.log") in err
    assert not any("new-session" in call for call in tmux_log(tmp_path))
    code, out, err = invoke(env, "--watch", flow_id, "--json")
    assert code == 79, err
    assert json.loads(out)["next"] == f"next: emb flow --resume {flow_id}"


def test_resume_refuses_indeterminate_preparation(
    tmp_path: Path,
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow.update(status="failed", updated_at=0)
    flow["preparation"] = {
        "state": "running",
        "command": "true",
        "log": "prepare.log",
        "pid": 123,
        "start": 456,
    }
    record.save(flow, env)
    ended: list[tuple[int, int]] = []
    monkeypatch.setattr(
        prepare, "end_group", lambda pid, start: ended.append((pid, start))
    )
    code, _, err = invoke(env, "--resume", flow["id"])
    assert code == 75 and "indeterminate" in err
    assert ended == [(123, 456)]
    assert not any("new-session" in call for call in tmux_log(tmp_path))
    assert record.load(flow["id"], env)["preparation"]["state"] == "running"


def test_cancel_during_flow_start_preparation(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = shlex.join([sys.executable, "-c", "import time; time.sleep(60)"])
    choices.set_settings({"worktree.prepare": {str(repo): shell}}, env=env)
    monkeypatch.setattr(command, "POLL_SECONDS", 0.01)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(invoke, env, "--plan", str(plan_file), "--start")
        deadline = time.monotonic() + 10
        while True:
            found, errors = record.list_flows(env)
            assert not errors
            if found and (found[0].get("preparation") or {}).get("pid"):
                flow_id = found[0]["id"]
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        code, _, err = invoke(env, "--cancel", flow_id, "--reason", "stop setup")
        assert code == 0, err
        code, _, err = pending.result(timeout=10)
    assert code == 79 and f"next: emb flow --resume {flow_id}" in err
    current = record.load(flow_id, env)
    saved = current["preparation"]
    assert current["status"] == "cancelled" and saved["state"] == "interrupted"
    assert prepare.start_ticks(saved["pid"]) is None
    assert record.read_events(flow_id, env)[-1]["exit"] == 79
    assert not any("new-session" in call for call in tmux_log(tmp_path))


@pytest.mark.parametrize("failure", ["timeout", "interrupted", "failed"])
def test_start_preparation_exception(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    shell = shlex.join([sys.executable, "-c", "import time; time.sleep(60)"])
    choices.set_settings({"worktree.prepare": {str(repo): shell}}, env=env)
    original = prepare.run

    def fail(*args: Any, **kwargs: Any) -> prepare.Result:  # noqa: ANN401
        if failure == "interrupted":
            raise KeyboardInterrupt
        if failure == "failed":
            raise OSError("cannot start")
        return original(*args, **kwargs, timeout=0.05)

    monkeypatch.setattr(prepare, "run", fail)
    code, _, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == 79, err
    (current,), errors = record.list_flows(env)
    assert not errors
    saved = current["preparation"]
    assert current["status"] == "failed" and saved["state"] == failure
    assert Path(saved["log"]).is_file()
    assert f"next: emb flow --resume {current['id']}" in err
    assert not any("new-session" in call for call in tmux_log(tmp_path))


@pytest.mark.parametrize(
    ("setting", "plan_rounds", "value", "unit", "source"),
    [
        ("1", None, 1, "reviews", "flow.review_rounds"),
        ("4", None, 4, "reviews", "flow.review_rounds"),
        ("1", 0, 0, "fix_rounds", "plan.rounds"),
        ("1", 3, 3, "fix_rounds", "plan.rounds"),
        (None, None, 2, "fix_rounds", "default"),
    ],
)
def test_start_freezes_review_budget(
    env: dict[str, str],
    plan_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str | None,
    plan_rounds: int | None,
    value: int,
    unit: str,
    source: str,
) -> None:
    if setting is None:
        settings = command._settings(None, env)
        settings["answers"].pop("flow.review_rounds", None)
        monkeypatch.setattr(command, "_settings", lambda claim, env: settings)
    else:
        choices.set_settings({"flow.review_rounds": setting}, env=env)
    if plan_rounds is not None:
        document = json.loads(plan_file.read_text())
        document["rounds"] = plan_rounds
        plan_file.write_text(json.dumps(document))
    code, out, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == 0, err
    flow_id = out.splitlines()[0].split()[1]
    directory = record.folder(flow_id, env)
    saved = json.loads((directory / record.FLOW_NAME).read_text())
    expected = {"value": value, "unit": unit, "source": source}
    assert saved["review_budget"] == expected
    assert saved["rounds"] == value - int(unit == "reviews")
    choices.set_settings({"flow.review_rounds": "2"}, env=env)
    plan_file.write_text("{}")
    (directory / record.PLAN_NAME).write_text("{}")
    code, out, err = invoke(env, "--status", flow_id, "--json")
    assert code == 0, err
    assert json.loads(out)["review_budget"] == expected


@pytest.mark.parametrize(
    ("case", "expected"),
    [("tmux", 69), ("branch", 70), ("inside", 69), ("runtime", 69)],
)
def test_start_failure(
    env: dict[str, str], plan_file: Path, repo: Path, case: str, expected: int
) -> None:
    if case == "tmux":
        (Path(env["PATH"].split(os.pathsep)[0]) / "tmux").unlink()
    elif case == "branch":
        paths.worktrees_dir(env).parent.mkdir(parents=True)
        paths.worktrees_dir(env).write_text("file")
    elif case == "inside":
        env["XDG_DATA_HOME"] = str(repo / "data")
    else:
        Path(env["XDG_RUNTIME_DIR"]).write_text("file")
    code, out, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == expected and not out, err
    if case in ("branch", "inside"):
        flows, errors = record.list_flows(env)
        assert not errors and flows[0]["status"] == "failed"
        assert not (repo / "data").exists()
    else:
        assert not paths.runs_dir(env).exists()


def test_session_failure(
    env: dict[str, str], plan_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise tmux.TmuxError("cannot start")

    monkeypatch.setattr(tmux, "new_session", fail)
    code, out, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == 71 and not out and "next: emb flow --resume" in err
    flows, _ = record.list_flows(env)
    assert flows[0]["status"] == "failed"


def test_watch_stop_then_end(
    env: dict[str, str], flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command, "POLL_SECONDS", 0.01)
    stopped(flow, env)
    flow["status"] = "running"
    flow["tasks"][0]["findings"] = [
        {"severity": "must", "problem": "bug", "scenario": "bad input"}
    ]
    flow["tasks"][0]["cleanup"] = [
        {"action": "kept", "branch": flow["branch"], "reason": "owner input"}
    ]
    record.save(flow, env)
    event = record.append_event(
        flow["id"],
        {"kind": "lead_stop", "task": "a", "reason": "bug", "choices": []},
        env,
    )
    with lock.hold(flow["id"], env):
        code, out, err = invoke(env, "--watch", flow["id"], "--json")
        report = json.loads(out)
        assert code == 0 and report["exit"] is None and report["event"] == "lead_stop"
        assert report["stops"][0]["seq"] == event["seq"]
        task = report["tasks"][0]
        assert task["findings"] == {"must": 1, "should": 0, "lead": 0}
        assert task["finding_details"][0]["scenario"] == "bad input"
        assert len(task["choices"]) == 5
        assert task["cleanup"][0]["reason"] == "owner input"
        assert "then emb flow --watch" in err
        assert record.read_cursor(flow["id"], env) == event["seq"]
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(invoke, env, "--watch", flow["id"], "--json")
            time.sleep(0.05)
            assert not pending.done()
            end(flow, env)
            code, out, err = pending.result(timeout=5)
        assert code == 0 and json.loads(out)["event"] == "end"
        assert "then emb merge" in err


@pytest.mark.parametrize(
    ("status", "expected"),
    [("done", 0), ("failed", 82), ("running", 72), ("stopped", 0)],
)
def test_watch_outcomes(
    env: dict[str, str], flow: dict[str, Any], status: str, expected: int
) -> None:
    if status in record.ENDED:
        flow["wave"] = 1
        flow["check_pending"] = status == "failed"
        end(flow, env, 82 if status == "failed" else 0)
    else:
        flow["status"] = status
        record.save(flow, env)
    code, out, err = invoke(env, "--watch", flow["id"], "--json")
    assert code == expected, err
    report = json.loads(out)
    assert report["event"] == (
        "runner_lost"
        if status == "running"
        else "lead_stop"
        if status == "stopped"
        else "end"
    )
    assert report["next"] in err
    expected_reports = {
        82: [
            f"report: the check failed after wave {flow['wave']}; "
            "fix the flow branch, then resume; "
            "when the check command is wrong, replace it with "
            f"emb flow --resume {flow['id']} --check '<JSON argv list>' "
            "--reason '<text>'"
        ],
        72: ["report: the runner is gone; work may still be running"],
    }.get(expected, [])
    assert report["report"] == expected_reports
    assert err.splitlines() == [report["next"], *expected_reports]
    if status == "failed":
        assert "fix the flow branch" in err
    # Already consumed terminal events still give the terminal outcome.
    if status in record.ENDED:
        again, output, _ = invoke(env, "--watch", flow["id"], "--json")
        assert again == expected and json.loads(output)["event"] == "end"


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("with_event", [False, True])
@pytest.mark.parametrize("status", ["failed", "done", "cancelled"])
@pytest.mark.parametrize("check_pending", [False, True])
def test_watch_exit_82_report(
    env: dict[str, str],
    flow: dict[str, Any],
    as_json: bool,
    with_event: bool,
    status: str,
    check_pending: bool,
) -> None:
    flow.update(status=status, check_pending=check_pending, wave=3)
    flow["tasks"][0]["status"] = "merged"
    record.save(flow, env)
    if with_event:
        record.append_event(
            flow["id"], {"kind": "end", "status": status, "exit": 82}, env
        )
    expected = (
        "report: the check failed after wave 3; fix the flow branch, then resume; "
        "when the check command is wrong, replace it with "
        f"emb flow --resume {flow['id']} --check '<JSON argv list>' --reason '<text>'"
        if status == "failed" and check_pending
        else "report: tasks unmerged; report every task's status and reason; "
        "leave the unmerged work and the flow branch to the owner"
    )
    for _ in range(2):
        code, out, err = invoke(
            env, "--watch", flow["id"], *(["--json"] if as_json else [])
        )
        assert code == 82, err
        assert err.splitlines()[1:] == [expected]
        if as_json:
            result = json.loads(out)
            assert result["exit"] == 82
            assert result["report"] == [expected]
            assert [task["status"] for task in result["tasks"]] == ["merged", "pending"]
        else:
            assert "a merged" in out and "b pending" in out
            assert "report:" not in out


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize(("lead", "should"), [(0, 0), (1, 0), (0, 1), (2, 1)])
def test_watch_done_reports_findings(
    env: dict[str, str],
    flow: dict[str, Any],
    as_json: bool,
    retained: bool,
    lead: int,
    should: int,
) -> None:
    for index, severity in enumerate([*(["lead"] * lead), *(["should"] * should)]):
        flow["tasks"][index % 2]["findings"].append(
            {"severity": severity, "problem": f"finding {index}", "round": 1}
        )
    # A must finding alone does not trigger this report.
    flow["tasks"][0]["findings"].append({"severity": "must", "problem": "must"})
    for task in flow["tasks"]:
        task["reviews"] = [
            {
                "review_round": 1,
                "verdict": "pass",
                **{
                    severity: sum(
                        finding["severity"] == severity for finding in task["findings"]
                    )
                    for severity in ("must", "should", "lead")
                },
            }
        ]
        if retained:
            task["reviews"].append(
                {
                    "review_round": 2,
                    "verdict": "pass",
                    "must": 0,
                    "should": 0,
                    "lead": 0,
                }
            )
    end(flow, env)
    reports = (
        [
            f"report: {lead} lead and {should} should findings remain on merged tasks; "
            "inspect each before merging"
        ]
        if lead or should
        else []
    )
    options = ["--watch", flow["id"], *(["--json"] if as_json else [])]
    # The report also appears when the terminal event was already consumed.
    for _ in range(2):
        code, out, err = invoke(env, *options)
        assert code == 0, err
        assert err.splitlines()[0].startswith("next: review ")
        assert err.splitlines()[1:] == reports
        if as_json:
            assert json.loads(out)["report"] == reports
        else:
            assert "report:" not in out


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_done_outside_reports_in_watch_and_status(
    env: dict[str, str], flow: dict[str, Any], as_json: bool, count: int
) -> None:
    flow["tasks"][0]["findings"] = [{"severity": "should", "problem": "Check wording"}]
    for index, task in enumerate(flow["tasks"][:count]):
        task["outside_files"] = [f"extra-{n}.py" for n in range(index + 1)]
    end(flow, env)
    reports = [
        "report: 0 lead and 1 should findings remain on merged tasks; "
        "inspect each before merging"
    ]
    if count:
        names = "a (1)" if count == 1 else "a (1), b (2)"
        reports.append(
            f"report: {count} merged task{'' if count == 1 else 's'} changed files "
            f"outside their declared files: {names}; inspect each before merging"
        )
    options = ["--json"] if as_json else []
    for _ in range(2):
        code, out, err = invoke(env, "--watch", flow["id"], *options)
        assert code == 0
        assert err.splitlines()[1:] == reports
        if as_json:
            assert json.loads(out)["report"] == reports
    code, out, err = invoke(env, "--status", flow["id"], *options)
    assert code == 0 and err == ""
    if as_json:
        assert json.loads(out)["reports"] == reports
    else:
        assert [
            line for line in out.splitlines() if line.startswith("report:")
        ] == reports


def test_done_outside_report_without_findings_counts_only_merged_tasks(
    env: dict[str, str], flow: dict[str, Any]
) -> None:
    end(flow, env)
    for task in flow["tasks"]:
        task["outside_files"] = ["extra.py"]
    flow["tasks"][1]["status"] = "dropped"
    assert command._done_reports(flow) == [
        "report: 1 merged task changed files outside their declared files: a (1); "
        "inspect each before merging"
    ]
    flow["tasks"][0]["outside_files"] = []
    assert command._done_reports(flow) == []


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("status", ["failed", "cancelled", "stopped", "running"])
def test_watch_other_states_omit_done_findings_report(
    env: dict[str, str], flow: dict[str, Any], as_json: bool, status: str
) -> None:
    flow["tasks"][0]["findings"] = [
        {"severity": "lead", "problem": "lead finding"},
        {"severity": "should", "problem": "should finding"},
    ]
    flow["tasks"][0].update(status="merged", outside_files=["extra.py"])
    flow["status"] = status
    record.save(flow, env)
    code, out, err = invoke(
        env, "--watch", flow["id"], *(["--json"] if as_json else [])
    )
    assert code == {"failed": 82, "cancelled": 82, "stopped": 0, "running": 72}[status]
    assert "findings remain on merged tasks" not in err
    assert "changed files outside their declared files" not in err
    if as_json:
        assert all(
            "findings remain on merged tasks" not in line
            for line in json.loads(out)["report"]
        )
    code, out, err = invoke(env, "--status", flow["id"], "--json")
    assert code == 0 and err == ""
    assert json.loads(out)["reports"] == []


def test_done_unmerged(env: dict[str, str], flow: dict[str, Any]) -> None:
    flow["status"] = "done"
    flow["tasks"][0]["status"] = "dropped"
    record.save(flow, env)
    code, _, err = invoke(env, "--watch", flow["id"])
    assert code == 82 and "report the unmerged tasks to the owner" in err
    assert err.splitlines()[-1] == (
        "report: tasks unmerged; report every task's status and reason; "
        "leave the unmerged work and the flow branch to the owner"
    )


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("capacity", [False, True])
def test_watch_reports_each_stop(
    env: dict[str, str],
    flow: dict[str, Any],
    live: bool,
    as_json: bool,
    capacity: bool,
) -> None:
    """Print one report per event and retain capacity on a repeated watch."""
    stopped(flow, env)
    flow["tasks"][1].update(status="conflict", reason="merge conflict")
    record.save(flow, env)
    state = (
        "the runner is still live for the other tasks"
        if live
        else "no runner is live and the owner's answer starts it"
    )
    reports = []
    for task_id in ("a", "a", "b", None):
        event: dict[str, Any] = {
            "kind": "lead_stop",
            "task": task_id,
            "reason": "needs the lead",
        }
        if task_id == "a" and capacity:
            event["provider_kind"] = "capacity"
            reports.append(
                "report: capacity on a; recommend only the same-worker retry"
            )
        else:
            reports.append(
                f"report: lead stop on {task_id or flow['id']}; {state}; "
                "give the reason as printed, list every printed choice by its "
                "option with what it does, and recommend one"
            )
        record.append_event(flow["id"], event, env)
    options = ["--watch", flow["id"], *(["--json"] if as_json else [])]
    with ExitStack() as stack:
        if live:
            stack.enter_context(lock.hold(flow["id"], env))
        code, out, err = invoke(env, *options)
    assert code == 0
    assert err.splitlines()[0].startswith("next:")
    assert err.splitlines()[1:] == reports
    if as_json:
        assert json.loads(out)["report"] == reports
    else:
        assert "report:" not in out
    # Consumed events are absent; the current stopped/conflict tasks still report.
    code, out, err = invoke(env, "--watch", flow["id"], "--json")
    expected = [reports[0], reports[2]]
    expected = [
        line.replace(
            "the runner is still live for the other tasks",
            "no runner is live and the owner's answer starts it",
        )
        for line in expected
    ]
    assert code == 0 and json.loads(out)["stops"] == []
    assert json.loads(out)["report"] == expected
    assert err.splitlines()[1:] == expected


@pytest.mark.parametrize("code", [72, 82])
@pytest.mark.parametrize(
    "mode", ["start", "watch", "status", "resume", "lead", "cancel"]
)
@pytest.mark.parametrize("check_pending", [False, True])
def test_flow_errors_print_exit_report(
    env: dict[str, str],
    flow: dict[str, Any],
    plan_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    mode: str,
    check_pending: bool,
) -> None:
    """All command error paths put the exit report after the next action."""
    flow.update(status="failed", check_pending=check_pending, wave=3)
    record.save(flow, env)

    def fail(*args: object, **kwargs: object) -> None:
        """Raise a flow error from the selected command."""
        raise command.CommandError(code, "stopped", "next: emb flow --status")

    monkeypatch.setattr(command, f"_{mode}", fail)
    options = (
        ["--start", "--plan", str(plan_file)]
        if mode == "start"
        else ["--lead", flow["id"], "a", "--drop"]
        if mode == "lead"
        else [f"--{mode}", flow["id"]]
    )
    actual, out, err = invoke(env, *options)
    report = (
        "report: the runner is gone; work may still be running"
        if code == 72
        else "report: the check failed after wave 3; fix the flow branch, then resume; "
        "when the check command is wrong, replace it with "
        f"emb flow --resume {flow['id']} --check '<JSON argv list>' --reason '<text>'"
        if check_pending and mode != "start"
        else "report: tasks unmerged; report every task's status and reason; "
        "leave the unmerged work and the flow branch to the owner"
    )
    assert actual == code and not out
    assert err.splitlines() == ["emb flow: stopped", "next: emb flow --status", report]


@pytest.mark.parametrize("record_state", ["missing", "invalid"])
def test_flow_error_exit_report_without_record(
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    record_state: str,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise command.CommandError(82, "stopped", "next: emb flow --status")

    monkeypatch.setattr(command, "_resume", fail)
    path = record.folder(flow["id"], env) / record.FLOW_NAME
    if record_state == "missing":
        path.unlink()
    else:
        path.write_text("not JSON")
    code, out, err = invoke(env, "--resume", flow["id"])
    assert code == 82 and not out
    assert err.splitlines() == [
        "emb flow: stopped",
        "next: emb flow --status",
        "report: tasks unmerged; report every task's status and reason; "
        "leave the unmerged work and the flow branch to the owner",
    ]


def test_starting_grace(
    env: dict[str, str], flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def elapse(_: float) -> None:
        monkeypatch.setattr(command, "START_SECONDS", 0)

    monkeypatch.setattr(command.time, "sleep", elapse)
    code, out, _ = invoke(env, "--watch", flow["id"], "--json")
    assert code == 72 and json.loads(out)["event"] == "runner_lost"


def test_watch_interrupted_keeps_cursor(
    env: dict[str, str], flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    record.write_cursor(flow["id"], 1, env)
    monkeypatch.setattr(command.time, "sleep", interrupt)
    with lock.hold(flow["id"], env):
        code, out, err = invoke(env, "--watch", flow["id"])
    assert code == 74 and not out and "next: emb flow --watch" in err
    assert record.read_cursor(flow["id"], env) == 1


def test_old_end_not_current_attempt(
    env: dict[str, str], flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    end(flow, env, 82)
    flow["status"] = "running"
    flow["ended_at"] = None
    record.save(flow, env)
    record.append_event(flow["id"], {"kind": "flow", "status": "running"}, env)
    code, out, _ = invoke(env, "--watch", flow["id"], "--json")
    assert code == 72 and json.loads(out)["event"] == "runner_lost"


def test_concurrent_watch_delivers_stop_once(
    env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    event = record.append_event(
        flow["id"], {"kind": "lead_stop", "task": "a", "reason": "bug"}, env
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(invoke, env, "--watch", flow["id"], "--json") for _ in range(2)
        ]
        reports = [json.loads(future.result(timeout=5)[1]) for future in futures]
    assert sum(len(report["stops"]) for report in reports) == 1
    assert record.read_cursor(flow["id"], env) == event["seq"]


@pytest.mark.parametrize("mode", ["--watch", "--status"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("ended", [False, True])
@pytest.mark.parametrize("with_outside_files", [False, True])
def test_task_outside_files(
    env: dict[str, str],
    flow: dict[str, Any],
    mode: str,
    as_json: bool,
    ended: bool,
    with_outside_files: bool,
) -> None:
    paths = [
        ["docs/extra.md", "tests/path with spaces.py", "src/extra.py"],
        ["another/path.txt"],
    ]
    for task, outside_files in zip(flow["tasks"], paths, strict=True):
        task["outside_files"] = outside_files if with_outside_files else []
    if ended:
        end(flow, env)
    else:
        stopped(flow, env)
    code, out, err = invoke(env, mode, flow["id"], *(["--json"] if as_json else []))
    assert code == 0, err
    if as_json:
        tasks = json.loads(out)["tasks"]
        assert [task["outside_files"] for task in tasks] == (
            paths if with_outside_files else [[], []]
        )
    else:
        for task in flow["tasks"]:
            task_block = f"{task['id']} {task['status']}" + (
                f": {task['reason']}" if task["reason"] else ""
            )
            for path in task["outside_files"]:
                task_block += f"\n  outside file: {path}"
            assert f"{task_block}\n  latest review:" in out
        assert out.count("  outside file:") == (4 if with_outside_files else 0)
    assert "outside file:" not in err


@pytest.mark.parametrize("mode", ["--watch", "--status"])
@pytest.mark.parametrize("as_json", [False, True])
def test_failed_check_output(
    env: dict[str, str], flow: dict[str, Any], mode: str, as_json: bool
) -> None:
    log = record.folder(flow["id"], env) / "check-1-1.log"
    lines = [f"check line {number}" for number in range(25)]
    output = "\n".join(lines) + "\n"
    flow.update(
        check_pending=True,
        checks=[{"wave": 1, "exit": 1, "log": str(log), "at": time.time()}],
        reason=f"the check failed after wave 1: exit 1, see {log}",
    )
    record.append_event(
        flow["id"],
        {"kind": "check", "wave": 1, "exit": 1, "log": str(log), "output": output},
        env,
    )
    end(flow, env, 82)
    # The event's stored tail remains available without the original log file.
    assert not log.exists()
    options = [mode, flow["id"], *(["--json"] if as_json else [])]
    for _ in range(2):
        code, out, err = invoke(env, *options)
        assert code == (82 if mode == "--watch" else 0), err
        if as_json:
            result = json.loads(out)
            assert result["check_output"] == "\n".join(lines[-20:]) + "\n"
            assert str(log) in result["reason"]
        else:
            assert str(log) in out
            assert "check log output:\n" in out
            assert out.split("check log output:\n", 1)[1].splitlines()[:20] == [
                f"  {line}" for line in lines[-20:]
            ]
            assert "  check line 4\n" not in out
        assert "check log output:" not in err


@pytest.mark.parametrize("mode", ["--watch", "--status"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("output", [None, "", " \t\n  \n"])
def test_check_output_absent(
    env: dict[str, str],
    flow: dict[str, Any],
    mode: str,
    as_json: bool,
    output: str | None,
) -> None:
    if output is not None:
        record.append_event(
            flow["id"], {"kind": "check", "exit": 1, "output": output}, env
        )
    end(flow, env, 82)
    code, out, err = invoke(env, mode, flow["id"], *(["--json"] if as_json else []))
    assert code == (82 if mode == "--watch" else 0), err
    if as_json:
        assert json.loads(out)["check_output"] is None
    else:
        assert "check log output:" not in out
        assert "check_output:" not in out


@pytest.mark.parametrize("mode", ["--watch", "--status"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("failed_before", [False, True])
def test_passing_check_clears_output(
    env: dict[str, str],
    flow: dict[str, Any],
    mode: str,
    as_json: bool,
    failed_before: bool,
) -> None:
    if failed_before:
        record.append_event(
            flow["id"],
            {"kind": "check", "exit": 1, "output": "previous failure\n"},
            env,
        )
        end(flow, env, 82)
    # A resumed attempt starts a new watch event boundary.
    record.append_event(flow["id"], {"kind": "flow", "status": "running"}, env)
    record.append_event(
        flow["id"], {"kind": "check", "exit": 0, "output": "checks passed\n"}, env
    )
    flow.update(check_pending=False, reason=None)
    end(flow, env)
    code, out, err = invoke(env, mode, flow["id"], *(["--json"] if as_json else []))
    assert code == 0, err
    if as_json:
        assert json.loads(out)["check_output"] is None
    else:
        assert "check log output:" not in out
        assert "check_output:" not in out
        assert "previous failure" not in out and "checks passed" not in out


def test_status(env: dict[str, str], flow: dict[str, Any]) -> None:
    with lock.hold(flow["id"], env):
        code, out, _ = invoke(env, "--status", flow["id"], "--json")
        assert code == 0 and json.loads(out)["runner_live"]
        assert json.loads(out)["tasks"][0]["latest_review"] is None
    code, out, _ = invoke(env, "--status", flow["id"])
    assert code == 0 and "runner_live: False" in out and "a pending" in out
    broken = record.folder(record.new_id(), env)
    broken.mkdir()
    (broken / record.FLOW_NAME).write_text("not JSON")
    code, out, err = invoke(env, "--status", "--json")
    assert code == 0 and json.loads(out)[0]["tasks"] == 2
    assert "emb flow: skipped" in err
    code, out, _ = invoke(env, "--status")
    assert code == 0 and f"{flow['id']}  starting  0/2 merged" in out


@pytest.mark.parametrize(
    ("value", "unit", "source", "description"),
    [
        (
            1,
            "reviews",
            "flow.review_rounds",
            "1 review (flow.review_rounds 1 allows no fix round)",
        ),
        (
            4,
            "reviews",
            "flow.review_rounds",
            "4 reviews (flow.review_rounds 4 allows 3 fix rounds)",
        ),
        (0, "fix_rounds", "plan.rounds", "0 fix rounds (plan rounds 0)"),
        (1, "fix_rounds", "plan.rounds", "1 fix round (plan rounds 1)"),
        (2, "fix_rounds", "default", "2 fix rounds (default 2 fix rounds)"),
        (
            2,
            "fix_rounds",
            "unknown",
            "2 fix rounds (budget 2 fix rounds; source unknown)",
        ),
    ],
)
def test_status_review_budget(
    env: dict[str, str],
    flow: dict[str, Any],
    value: int,
    unit: str,
    source: str,
    description: str,
) -> None:
    flow["rounds"] = value - int(unit == "reviews")
    budget = {"value": value, "unit": unit, "source": source}
    flow["review_budget"] = budget
    record.save(flow, env)
    if source == "unknown":
        del flow["review_budget"]
        (record.folder(flow["id"], env) / record.FLOW_NAME).write_text(json.dumps(flow))
    for single in (True, False):
        args = ["--status", *([flow["id"]] if single else [])]
        code, out, err = invoke(env, *args, "--json")
        assert code == 0, err
        status = json.loads(out)
        item = status if single else status[0]
        assert item["review_budget"] == budget
        if single:
            assert item["rounds"] == flow["rounds"]
        code, out, err = invoke(env, *args)
        assert code == 0, err
        assert f"review_budget: {description}" in out


@pytest.mark.parametrize("legacy", [False, True])
def test_status_review_provenance(
    env: dict[str, str], flow: dict[str, Any], legacy: bool
) -> None:
    task = flow["tasks"][0]
    task["reviews"] = [
        {
            "round": 0,
            "run": "first",
            "verdict": "fix",
            "must": 0,
            "should": 1,
            "lead": 0,
        },
        {
            "round": 0,
            "run": "second",
            "verdict": "pass",
            "must": 0,
            "should": 0,
            "lead": 0,
        },
    ]
    task["findings"] = [{"severity": "should", "problem": "retained suggestion"}]
    if not legacy:
        for review in task["reviews"]:
            review["review_round"] = 1
        task["findings"][0].update(round=1, run="first")
    record.save(flow, env)
    path = record.folder(flow["id"], env) / record.FLOW_NAME
    before = path.read_bytes()
    code, out, err = invoke(env, "--status", flow["id"], "--json")
    assert code == 0, err
    report = json.loads(out)["tasks"][0]
    expected_finding = {
        **task["findings"][0],
        "round": None if legacy else 1,
        "run": None if legacy else "first",
    }
    assert report["findings"] == [expected_finding]
    assert report["reviews"] == task["reviews"]
    assert report["latest_review"] == {
        "round": None if legacy else 1,
        "run": "second",
        "verdict": "pass",
        "findings": {"must": 0, "should": 0, "lead": 0},
    }
    code, out, err = invoke(env, "--status", flow["id"])
    assert code == 0, err
    assert f"latest review: {json.dumps(report['latest_review'])}" in out
    # Field order is not part of the status contract.
    printed = next(line.strip() for line in out.splitlines() if "finding:" in line)
    assert json.loads(printed.removeprefix("finding: ")) == expected_finding
    watch = command._task_report(flow["id"], task)
    assert watch["finding_details"] == [expected_finding]
    assert watch["latest_review"] == report["latest_review"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", record.ANSWER_KINDS)
def test_lead_answers(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any], kind: str
) -> None:
    stopped(flow, env)
    message = tmp_path / "correction.md"
    message.write_text("@write intentional command\n")
    options = {
        "send": ["--send", str(message)],
        "accept": ["--accept"],
        "review": ["--review", "--reviewer", "codex:m@low"],
        "worker": ["--worker", "codex:m@high"],
        "drop": ["--drop"],
    }[kind]
    with lock.hold(flow["id"], env):
        code, out, err = invoke(env, "--lead", flow["id"], "a", *options)
    assert code == 0 and f"queued answer 1: {kind} for a" in out, err
    answer = record.read_answers(flow["id"], env)[0]
    assert answer is not None and answer["kind"] == kind
    if kind == "send":
        assert Path(answer["file"]).read_text() == message.read_text()
        assert Path(answer["file"]).name == "1.md"
    if kind == "accept":
        assert answer["commit"] == flow["tip"]
    if kind in ("worker", "review"):
        assert answer["worker"].startswith("codex:m@")
    assert not tmux_log(tmp_path)
    assert record.load(flow["id"], env)["tasks"][0]["status"] == "stopped"


def test_accept_resolves_short_commit(
    env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    with lock.hold(flow["id"], env):
        code, _, err = invoke(
            env, "--lead", flow["id"], "a", "--accept", "--commit", flow["tip"][:8]
        )
        assert code == 0, err
        code, _, _ = invoke(
            env, "--lead", flow["id"], "a", "--accept", "--commit", "invalid-commit"
        )
        assert code == 64
    answers = record.read_answers(flow["id"], env)
    assert len(answers) == 1 and answers[0] is not None
    assert answers[0]["commit"] == flow["tip"]


@pytest.mark.parametrize(
    ("case", "expected"),
    [("unknown", 64), ("writing", 75), ("done", 75), ("empty", 64), ("missing", 66)],
)
def test_lead_refusal(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any], case: str, expected: int
) -> None:
    stopped(flow, env)
    task = "a"
    options = ["--drop"]
    if case == "unknown":
        task = "unknown"
    elif case == "writing":
        flow["tasks"][0]["status"] = "writing"
        record.save(flow, env)
    elif case == "done":
        end(flow, env)
    else:
        path = tmp_path / "correction"
        if case == "empty":
            path.write_text(" \n")
        options = ["--send", str(path)]
    code, out, err = invoke(env, "--lead", flow["id"], task, *options)
    assert code == expected and not out, err
    assert not record.read_answers(flow["id"], env)


def test_lead_queues_a_drop_for_a_pending_task(
    env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    flow["tasks"][0]["status"] = "pending"
    record.save(flow, env)
    code, _out, err = invoke(env, "--lead", flow["id"], "a", "--drop")
    assert code == 0, err
    answers = record.read_answers(flow["id"], env)
    assert len(answers) == 1 and answers[0] is not None
    assert answers[0]["kind"] == "drop"


def test_lead_starts_runner(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    code, out, err = invoke(env, "--lead", flow["id"], "a", "--drop")
    assert code == 0 and f"flow {flow['id']}" in out, err
    assert sum("new-session" in call for call in tmux_log(tmp_path)) == 1


def test_resume_live_and_done(env: dict[str, str], flow: dict[str, Any]) -> None:
    with lock.hold(flow["id"], env):
        code, _, err = invoke(env, "--resume", flow["id"])
        assert code == 75 and "next: emb flow --watch" in err
    end(flow, env)
    code, _, err = invoke(env, "--resume", flow["id"])
    assert code == 75 and "is done" in err


def test_resume_dead_session(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    tmux.new_session(
        flow["id"], cwd=Path(flow["worktree"]), argv=["true"], env_vars={}, env=env
    )
    deadline = time.monotonic() + 5
    while not tmux.pane_dead(flow["id"], env=env):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    code, out, err = invoke(env, "--resume", flow["id"])
    assert code == 0 and out == f"flow {flow['id']}\n", err
    calls = tmux_log(tmp_path)
    assert sum("kill-session" in call for call in calls) == 1
    assert sum("new-session" in call for call in calls) == 2


def test_concurrent_resume_launches_once(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, env, "--resume", flow["id"]) for _ in range(2)]
        codes = sorted(future.result(timeout=5)[0] for future in futures)
    assert codes == [0, 75]
    assert sum("new-session" in call for call in tmux_log(tmp_path)) == 1


def test_runner_loader_ignores_conflicting_module(
    tmp_path: Path, env: dict[str, str]
) -> None:
    (tmp_path / "ember.py").write_text(
        "raise RuntimeError('untrusted checkout module')"
    )
    argv = command.runner_command("unused")
    assert argv[1] == "-I"
    # Exercise the same bootstrap with the available CLI; runner is task B's file.
    argv[3] = argv[3].replace("ember.flows.runner", "ember.cli")
    argv[-1] = "--help"
    result = subprocess.run(
        argv,
        cwd=tmp_path,
        env={**env, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: emb" in result.stdout


def test_missing_flow(env: dict[str, str]) -> None:
    for options in (
        ("--status", "invalid"),
        ("--resume", record.new_id()),
        ("--watch", record.new_id()),
    ):
        code, out, err = invoke(env, *options)
        assert code == 64 and not out and "next: emb flow --status" in err


@pytest.mark.parametrize(("case", "expected"), [("off", 64), ("effort", 64)])
def test_worker_gates(
    env: dict[str, str],
    plan_file: Path,
    case: str,
    expected: int,
) -> None:
    options = []
    if case == "off":
        choices.set_settings({"engine.codex": "off"}, env=env)
    else:
        options = ["--writer", "codex:m@impossible"]
    code, out, err = invoke(env, "--plan", str(plan_file), "--start", *options)
    assert code == expected and not out
    assert "owner:" in err if case == "off" else "does not take the effort" in err
    assert not paths.runs_dir(env).exists()


def test_freezes_engine_default(
    env: dict[str, str],
    plan_file: Path,
) -> None:
    class DefaultDriver(StubDriver):
        def default_effort(self, model: str) -> str | None:
            return None

    def factory(environment: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        return Core(
            environment,
            load_driver=lambda name: DefaultDriver(name, script_path(environment)),
            **kwargs,
        )

    choices.set_settings({"single.writer": "ask", "single.reviewer": "ask"}, env=env)
    code, out, err = invoke(
        env,
        "--plan",
        str(plan_file),
        "--start",
        "--writer",
        "codex:m",
        "--reviewer",
        "codex:m",
        factory=factory,
    )
    assert code == 0, err
    flow_id = out.splitlines()[0].split()[1]
    flow = record.load(flow_id, env)
    assert flow["writer"]["effort"] is None
    assert flow["writer"]["effort_frozen"] is True
    choices.set_settings({"single.writer": "codex:gpt-5"}, env=env)
    assert record.load(flow_id, env)["writer"] == flow["writer"]


def test_watch_refreshes_after_runner_exits(
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow["status"] = "running"
    record.save(flow, env)

    def finish(flow_id: str, environment: Mapping[str, str]) -> bool:
        end(flow, environment)
        return False

    monkeypatch.setattr(command.lock, "live", finish)
    code, out, _ = invoke(env, "--watch", flow["id"], "--json")
    assert code == 0 and json.loads(out)["event"] == "end"


def test_resume_reservation_hides_old_end(
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    end(flow, env, 82)
    code, _, err = invoke(env, "--resume", flow["id"])
    assert code == 0, err

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(command.time, "sleep", interrupt)
    code, out, _ = invoke(env, "--watch", flow["id"], "--json")
    assert code == 74 and not out


@pytest.mark.parametrize("outcome", ["dead", "gone", "gone during probe", "timeout"])
def test_lead_waits_for_exiting_runner_session(
    tmp_path: Path,
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    stopped(flow, env)
    elapsed = 0.0
    session_state = "live"
    killed: list[str] = []

    def exists(name: str, *, env: Mapping[str, str]) -> bool:
        return session_state != "gone"

    def pane_dead(name: str, *, env: Mapping[str, str]) -> bool:
        nonlocal session_state
        if outcome == "gone during probe" and elapsed:
            session_state = "gone"
            raise tmux.TmuxError("session ended")
        return session_state == "dead"

    def kill(name: str, *, env: Mapping[str, str]) -> None:
        nonlocal session_state
        assert session_state == "dead"
        killed.append(name)
        session_state = "gone"

    def sleep(seconds: float) -> None:
        nonlocal elapsed, session_state
        # The answer must stay queued while the old runner's pane is closing.
        answers = record.read_answers(flow["id"], env)
        assert len(answers) == 1 and answers[0] is not None
        assert answers[0]["kind"] == "drop"
        elapsed += seconds
        if outcome in ("dead", "gone"):
            session_state = outcome

    monkeypatch.setattr(tmux, "exists", exists)
    monkeypatch.setattr(tmux, "pane_dead", pane_dead)
    monkeypatch.setattr(tmux, "kill", kill)
    from types import SimpleNamespace

    # Only the flow command sees the fake clock; subprocess keeps the real one.
    fake_time = SimpleNamespace(time=time.time, monotonic=lambda: elapsed, sleep=sleep)
    monkeypatch.setattr(command, "time", fake_time)
    code, out, err = invoke(env, "--lead", flow["id"], "a", "--drop")
    assert "queued answer 1: drop for a" in out
    assert err.endswith(f"next: emb flow --watch {flow['id']} --json\n")
    launches = sum("new-session" in call for call in tmux_log(tmp_path))
    if outcome == "timeout":
        assert code == 75 and elapsed == 10.0
        assert launches == 0 and "is still running" in err
        assert not killed
    else:
        assert code == 0 and 0 < elapsed <= 10.0, err
        assert launches == 1 and f"flow {flow['id']}\n" in out
        assert killed == ([flow["id"]] if outcome == "dead" else [])


@pytest.mark.parametrize("status", ["failed", "stopped"])
@pytest.mark.parametrize("current_attempt", [False, True])
def test_expired_resume_reservation_requires_current_record(
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    current_attempt: bool,
) -> None:
    flow["runner"] = {"pid": 1, "started_at": time.time() - 60}
    if status == "failed":
        end(flow, env, 82)
    else:
        stopped(flow, env)
        record.append_event(
            flow["id"], {"kind": "lead_stop", "task": "a", "reason": "old stop"}, env
        )
    code, _, err = invoke(env, "--resume", flow["id"])
    assert code == 0, err
    reservation = json.loads(
        (record.folder(flow["id"], env) / "startup.json").read_text()
    )
    if current_attempt:
        # Equality is sufficient: this is the new attempt's stored outcome.
        flow["runner"]["started_at"] = reservation["at"]
        record.save(flow, env)
    monkeypatch.setattr(
        command.time, "time", lambda: reservation["at"] + command.START_SECONDS + 1
    )
    code, out, err = invoke(env, "--watch", flow["id"], "--json")
    result = json.loads(out)
    assert result["stops"] == []
    if current_attempt:
        assert code == (82 if status == "failed" else 0)
        assert result["event"] == ("end" if status == "failed" else "lead_stop")
    else:
        assert code == 72 and result["exit"] == 72
        assert result["event"] == "runner_lost"
        assert result["next"] == f"next: emb flow --resume {flow['id']}"
        assert err == (
            result["next"] + "\nreport: the runner is gone; work may still be running\n"
        )


@pytest.mark.parametrize("status", ["starting", "running", "checking", "stopped"])
@pytest.mark.parametrize("reason", [None, "scope changed"])
def test_cancel_idle_flow(
    flow: dict[str, Any], env: dict[str, str], status: str, reason: str | None
) -> None:
    flow["status"] = status
    flow["tasks"][0]["branch"] = "ember/codex/kept"
    flow["abandoned"] = ["ember/codex/previous"]
    record.save(flow, env)
    options = ["--reason", reason] if reason is not None else []
    code, out, err = invoke(env, "--cancel", flow["id"], *options, "--json")
    assert code == 0, err
    assert json.loads(out) == {
        "flow": flow["id"],
        "status": "cancelled",
        "reason": reason,
        "branch": flow["branch"],
        "task_branches": [
            {"task": "a", "branch": "ember/codex/kept"},
            {"task": None, "branch": "ember/codex/previous"},
        ],
        "open_runs": [],
    }
    current = record.load(flow["id"], env)
    assert current["ended_at"] is not None
    assert current["tasks"] == flow["tasks"]
    assert Path(current["worktree"]).is_dir()
    assert not lock.live(flow["id"], env)
    assert not command._starting(current, env)
    code, out, err = invoke(env, "--watch", flow["id"], "--json")
    assert code == 82 and json.loads(out)["status"] == "cancelled"
    assert err.splitlines() == [
        f"next: emb flow --resume {flow['id']}",
        "report: tasks unmerged; report every task's status and reason; "
        "leave the unmerged work and the flow branch to the owner",
    ]
    code, out, err = invoke(env, "--status", flow["id"])
    assert code == 0 and "status: cancelled\n" in out
    assert f"reason: {reason}\n" in out
    code, out, err = invoke(env, "--status")
    suffix = f": {reason}" if reason else ""
    assert code == 0 and f"cancelled{suffix}  0/2 merged" in out


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_cancel_refuses_ended_unchanged(
    flow: dict[str, Any], env: dict[str, str], status: str
) -> None:
    flow.update(status=status, reason="original", ended_at=1.0)
    record.save(flow, env)
    directory = record.folder(flow["id"], env)
    before = (directory / record.FLOW_NAME).read_bytes()
    events = record.read_events(flow["id"], env)
    code, out, err = invoke(env, "--cancel", flow["id"], "--reason", "new")
    assert code == 75 and not out and status in err
    assert (directory / record.FLOW_NAME).read_bytes() == before
    assert record.read_events(flow["id"], env) == events
    assert not (directory / record.CANCEL_NAME).exists()


@pytest.mark.parametrize("mode", ["cancel", "resume", "watch", "status", "lead"])
def test_cancel_missing_code_is_mode_specific(env: dict[str, str], mode: str) -> None:
    options = ["a", "--drop"] if mode == "lead" else []
    code, out, err = invoke(env, f"--{mode}", record.new_id(), *options)
    assert code == (65 if mode == "cancel" else 64)
    assert not out and "no such flow" in err


@pytest.mark.parametrize(
    "options",
    [
        ["--status", "--reason", "why"],
        ["--cancel", "FLOW", "--resume", "FLOW"],
        ["--cancel", "FLOW", "--writer", "codex:m"],
        ["--cancel", "FLOW", "--drop"],
        ["--cancel", "FLOW", "--plan", "plan.json"],
    ],
)
def test_cancel_option_validation(env: dict[str, str], options: list[str]) -> None:
    code, out, err = invoke(env, *options)
    assert code == 64 and not out and "next: emb flow --help" in err


def test_cancelled_lead_refused_and_resume_clears_request(
    flow: dict[str, Any], env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped(flow, env)
    assert invoke(env, "--cancel", flow["id"])[0] == 0
    code, out, err = invoke(env, "--lead", flow["id"], "a", "--drop")
    assert code == 75 and not out
    assert err.splitlines()[-1] == f"next: emb flow --resume {flow['id']}"
    assert not record.read_answers(flow["id"], env)
    monkeypatch.setattr(command, "_require_tool", lambda *args: None)
    monkeypatch.setattr(command, "_wait_session_exit", lambda *args: None)
    monkeypatch.setattr(tmux, "new_session", lambda *args, **kwargs: None)
    code, out, err = invoke(env, "--resume", flow["id"])
    assert code == 0, err
    assert record.cancel_request(flow["id"], env) is None
    assert command._starting(record.load(flow["id"], env), env)


def test_cancel_lists_stored_open_run_statuses(
    flow: dict[str, Any], env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    item = flow["tasks"][0]
    item.update(writer_run="writer", reviewer_run="reviewer", branch="kept")
    item["reviews"] = [{"run": "older"}, {"run": "closed"}, {"run": "failed"}]
    item["answer_effect"] = {"run": "previous", "n": 1}
    item["cleanup"] = [
        {"run": "writer", "branch": "kept", "action": "kept"},
        {"run": "cleanup-only", "branch": "also-kept", "action": "kept"},
    ]
    record.save(flow, env)
    statuses = {
        "writer": "running",
        "reviewer": "starting",
        "older": "waiting",
        "closed": "closed",
        "failed": "failed",
        "previous": "waiting",
        "cleanup-only": "waiting",
    }
    loaded = []

    def load(run_id: str, environment: Mapping[str, str]) -> SimpleNamespace:
        assert environment == env
        loaded.append(run_id)
        return SimpleNamespace(status=statuses[run_id], branch=None)

    monkeypatch.setattr(command.run_record, "load", load)
    code, out, err = invoke(env, "--cancel", flow["id"], "--reason", "changed")
    assert code == 0, err
    assert set(loaded) == set(statuses)
    assert len(loaded) == len(statuses)
    assert out.splitlines() == [
        f"flow {flow['id']} cancelled",
        "reason changed",
        f"branch {flow['branch']}",
        "task a branch kept",
        "task a branch also-kept",
        "task a writer run writer",
        "task a reviewer run reviewer",
        "task a reviewer run older",
        "task a writer run cleanup-only",
        "task a writer run previous",
    ]


def test_cancel_text_leaves_out_missing_reason(
    flow: dict[str, Any], env: dict[str, str]
) -> None:
    flow["status"] = "stopped"
    record.save(flow, env)
    code, out, err = invoke(env, "--cancel", flow["id"])
    assert code == 0, err
    assert out.splitlines() == [
        f"flow {flow['id']} cancelled",
        f"branch {flow['branch']}",
    ]


def test_cancel_reports_with_unreadable_run_record(
    flow: dict[str, Any], env: dict[str, str]
) -> None:
    flow["status"] = "stopped"
    flow["tasks"][0].update(writer_run="codex-20261102-000000-abcdef", branch="kept")
    record.save(flow, env)
    code, out, err = invoke(env, "--cancel", flow["id"], "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["status"] == "cancelled"
    assert result["task_branches"] == [{"task": "a", "branch": "kept"}]
    assert result["open_runs"] == []
    assert "WARNING: task a writer run codex-20261102-000000-abcdef" in err
    assert record.load(flow["id"], env)["status"] == "cancelled"


@pytest.mark.parametrize("runner_finishes", [False, True])
def test_cancel_signals_runner_and_records_after_lock_release(
    flow: dict[str, Any],
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    runner_finishes: bool,
) -> None:
    import sys

    flow_id = flow["id"]
    script = """
import os, signal, sys, time
from ember.flows import lock, record
from ember.flows.command import lifecycle
from ember.flows.runner import _process_start
flow_id, finish = sys.argv[1:]
stopped = False
def stop(signum, frame):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
with lock.hold(flow_id):
    with lifecycle(flow_id, os.environ):
        flow = record.load(flow_id)
        flow.update(status="running", runner={
            "pid": os.getpid(), "started_at": time.time(),
            "start": _process_start(os.getpid()), "exiting": False,
        })
        record.save(flow)
    while not stopped:
        time.sleep(0.01)
    if finish == "yes":
        with lifecycle(flow_id, os.environ):
            request = record.cancel_request(flow_id)
            record.finish_cancel(flow, request, os.environ)
"""
    monkeypatch.setattr(command, "POLL_SECONDS", 0.01)
    process = subprocess.Popen(
        [sys.executable, "-c", script, flow_id, "yes" if runner_finishes else "no"],
        env=dict(env),
    )
    try:
        deadline = time.monotonic() + 10
        while record.load(flow_id, env)["runner"] is None:
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        code, out, err = invoke(env, "--cancel", flow_id, "--reason", "stop", "--json")
        assert code == 0, err
        assert json.loads(out)["status"] == "cancelled"
        assert process.wait(timeout=5) == 0
        assert not lock.live(flow_id, env)
        ends = [e for e in record.read_events(flow_id, env) if e["kind"] == "end"]
        assert len(ends) == 1
        assert ends[0]["reason"] == "stop" and ends[0]["exit"] == 82
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("busy_lock", ["runner", "lifecycle"])
@pytest.mark.parametrize("deadline_seconds", [60.0, 2.5])
def test_cancel_deadline_and_retry(
    flow: dict[str, Any],
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
    busy_lock: str,
    deadline_seconds: float,
) -> None:
    flow_id = flow["id"]
    elapsed = 0.0
    polling = False
    lifecycle_held = False
    original_flock = fcntl.flock
    requests: list[dict[str, Any]] = []

    def flock(fd: int, operation: int) -> None:
        if polling and os.readlink(f"/proc/self/fd/{fd}").endswith("lifecycle.lock"):
            assert operation & fcntl.LOCK_NB
        original_flock(fd, operation)

    def signal_cancel(value: Mapping[str, Any], environment: Mapping[str, str]) -> None:
        nonlocal elapsed, polling
        request = record.cancel_request(flow_id, environment)
        assert request is not None
        requests.append(request)
        elapsed += 1.0
        polling = True

    with ExitStack() as runner, ExitStack() as lifecycle:
        runner.enter_context(lock.hold(flow_id, env))

        def sleep(seconds: float) -> None:
            nonlocal elapsed, lifecycle_held
            elapsed += seconds
            if busy_lock == "lifecycle" and not lifecycle_held:
                lifecycle.enter_context(command.lifecycle(flow_id, env, blocking=False))
                lifecycle_held = True
                runner.close()

        assert command.CANCEL_SECONDS == 60.0
        monkeypatch.setattr(command, "CANCEL_SECONDS", deadline_seconds)
        monkeypatch.setattr(command, "_signal_cancel", signal_cancel)
        monkeypatch.setattr(command.fcntl, "flock", flock)
        monkeypatch.setattr(
            command,
            "time",
            SimpleNamespace(time=time.time, monotonic=lambda: elapsed, sleep=sleep),
        )
        options = ["--json"] if as_json else []
        before = record.load(flow_id, env)
        for attempt in range(2):
            polling = False
            code, out, err = invoke(
                env,
                "--cancel",
                flow_id,
                "--reason",
                "first" if not attempt else "new",
                *options,
            )
            assert code == 75 and not out
            assert err == (
                f"emb flow: the runner for {flow_id} is still running after "
                f"{deadline_seconds:g} seconds\n"
                f"next: emb flow --status {flow_id}\n"
            )
            assert elapsed == (attempt + 1) * deadline_seconds
            assert record.load(flow_id, env) == before
            assert record.cancel_request(flow_id, env) == requests[0]
            assert requests[-1] == requests[0]
            assert requests[0]["reason"] == "first"
            lifecycle.close()
            lifecycle_held = False
            if busy_lock == "lifecycle":
                runner.enter_context(lock.hold(flow_id, env))
        runner.close()

    polling = False
    code, out, err = invoke(env, "--cancel", flow_id, "--reason", "new", "--json")
    assert code == 0, err
    assert json.loads(out)["reason"] == "first"
    assert json.loads(out)["status"] == "cancelled"
    assert record.cancel_request(flow_id, env) == requests[0]


def test_cancel_runner_releases_during_last_sleep(
    flow: dict[str, Any], env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0
    with ExitStack() as runner:
        runner.enter_context(lock.hold(flow["id"], env))

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            assert record.cancel_request(flow["id"], env) is not None
            elapsed += seconds
            runner.close()

        monkeypatch.setattr(command, "CANCEL_SECONDS", 0.5)
        monkeypatch.setattr(
            command,
            "time",
            SimpleNamespace(time=time.time, monotonic=lambda: elapsed, sleep=sleep),
        )
        code, out, err = invoke(env, "--cancel", flow["id"], "--json")

    assert code == 0, err
    assert elapsed == 0.5
    assert json.loads(out)["status"] == "cancelled"
    assert err == f"next: emb flow --resume {flow['id']}\n"


def test_cancel_loses_race_to_completed_flow(
    flow: dict[str, Any], env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = command.lifecycle
    completed: list[dict[str, Any]] = []

    def lifecycle(flow_id: str, environment: Mapping[str, str]) -> Any:  # noqa: ANN401
        end(flow, environment)
        completed.append(record.load(flow_id, environment))
        return original(flow_id, environment)

    monkeypatch.setattr(command, "lifecycle", lifecycle)
    code, out, err = invoke(env, "--cancel", flow["id"])
    assert code == 75 and not out and "is done" in err
    assert record.load(flow["id"], env) == completed[0]
    assert record.cancel_request(flow["id"], env) is None


def _menu_plan(plan_file: Path) -> Path:
    """Drop the plan's own workers so the dry run reads the Team rows."""
    data = json.loads(plan_file.read_text())
    del data["writer"], data["reviewer"]
    plan_file.write_text(json.dumps(data))
    return plan_file


def test_dry_run_reads_the_flow_settings(env: dict[str, str], plan_file: Path) -> None:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high", "gpt-5-mini@low"],
            "single.writer": "codex:gpt-5",
            "single.reviewer": "codex:gpt-5",
            "flow.reviewer": "codex:gpt-5-mini",
            "flow.parallel": "2",
            "flow.stall": "20",
            "flow.whole_review": "off",
        },
        env=env,
    )
    code, out, err = invoke(
        env, "--plan", str(_menu_plan(plan_file)), "--dry-run", "--json"
    )
    assert code == 0, err
    result = json.loads(out)
    assert result["writer"]["model"] == "gpt-5"
    assert (result["reviewer"]["model"], result["reviewer"]["effort"]) == (
        "gpt-5-mini",
        "low",
    )
    assert (result["parallel"], result["parallel_source"]) == (2, "setting")
    assert result["stall_minutes"] == 20
    assert result["whole_review"] is False
    assert result["gapcheck_status"] == "run"
    assert "--engine" not in result["gapcheck"]

    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run")
    assert code == 0, err
    assert out.splitlines()[:2] == [
        "Team, Single tasks Writer: codex gpt-5 · high · standard (single.writer)",
        "Team, Flow reviewer: codex gpt-5-mini · low · standard (flow.reviewer)",
    ]
    # A model the owner set to Fast is announced as fast, in text and JSON.
    choices.set_settings({"fast.codex": ["gpt-5-mini"]}, env=env)
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run")
    assert code == 0, err
    assert out.splitlines()[1] == (
        "Team, Flow reviewer: codex gpt-5-mini · low · fast (flow.reviewer)"
    )
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert (result["writer"]["speed"], result["reviewer"]["speed"]) == (
        "standard",
        "fast",
    )
    assert "Team," not in err


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("default_effort", [None, "medium"])
def test_dry_run_announces_worker_options(
    env: dict[str, str],
    plan_file: Path,
    as_json: bool,
    default_effort: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Options replace plan workers and retain their source and default effort."""
    monkeypatch.setattr(
        StubDriver, "default_effort", lambda self, model: default_effort
    )
    code, out, err = invoke(
        env,
        "--plan",
        str(plan_file),
        "--dry-run",
        "--writer",
        "claude:opus@high",
        "--reviewer",
        "codex:other",
        *(["--json"] if as_json else []),
    )
    assert code == 0, err
    if as_json:
        result = json.loads(out)
        assert result["writer"] == {
            "engine": "claude",
            "model": "opus",
            "effort": "high",
            "source": "option",
            "speed": None,
        }
        assert result["reviewer"] == {
            "engine": "codex",
            "model": "other",
            "effort": default_effort,
            "source": "option",
            "speed": "standard",
        }
    else:
        assert out.splitlines()[:2] == [
            "option, writer: claude opus · high (option)",
            "option, reviewer: codex other · "
            f"{default_effort or 'default'} · standard (option)",
        ]
    assert "option, writer:" not in err
    assert "option, reviewer:" not in err


@pytest.mark.parametrize("source", ["option", "plan"])
@pytest.mark.parametrize("same", [False, True])
def test_dry_run_announces_separate_effort_source(
    env: dict[str, str], plan_file: Path, source: str, same: bool
) -> None:
    """A matching menu supplies effort when an explicit worker omits it."""
    choices.set_settings(
        {
            "single.writer": "codex:gpt-5",
            "single.reviewer": "codex:gpt-5",
            "flow.writer": "same" if same else "codex:gpt-5",
            "flow.reviewer": "same" if same else "codex:gpt-5",
        },
        env=env,
    )
    options = []
    if source == "option":
        options = ["--writer", "codex:gpt-5", "--reviewer", "codex:gpt-5"]
    else:
        data = json.loads(plan_file.read_text())
        del data["writer"]["effort"], data["reviewer"]["effort"]
        plan_file.write_text(json.dumps(data))
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", *options)
    assert code == 0, err
    row = "single" if same else "flow"
    assert out.splitlines()[:2] == [
        f"{source}, {role}: codex gpt-5 · high · standard "
        f"(model: {source}, effort: {row}.{role})"
        for role in plan.ROLES
    ]


def test_plan_parallel_wins_over_the_setting(
    env: dict[str, str], plan_file: Path
) -> None:
    data = json.loads(plan_file.read_text())
    data["parallel"] = 3
    plan_file.write_text(json.dumps(data))
    choices.set_settings({"flow.parallel": "6"}, env=env)
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert (result["parallel"], result["parallel_source"]) == (3, "plan")


@pytest.mark.parametrize(
    ("when", "status", "has_command"),
    [("off", "skipped", False), ("3", "skipped", False), ("ask", "ask", True)],
)
def test_dry_run_reports_the_gap_check_decision(
    env: dict[str, str], plan_file: Path, when: str, status: str, has_command: bool
) -> None:
    choices.set_settings({"gapcheck.when": when}, env=env)
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["gapcheck_status"] == status
    assert result["gapcheck_reason"]
    assert (result["gapcheck"] is not None) == has_command
    if has_command:
        assert result["gapcheck"].endswith("--confirmed")
        assert "--json" in shlex.split(result["gapcheck"])
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run")
    assert code == 0, err
    line = out.split("gap check: ")[1]
    assert line.startswith("skipped, " if status == "skipped" else "ask the owner")


@pytest.mark.parametrize("reason", ["hidden", "startup file"])
def test_status_keeps_denial_provenance_from_completed_workers(
    env: dict[str, str], flow: dict[str, Any], reason: str
) -> None:
    item = {
        "path": "/home/owner/.profile",
        "reason": reason,
        "allow_path": None,
        "run": "codex-20260101-000000-abcdef",
        "turn": 2,
    }
    flow["tasks"][0]["denied_paths"] = [item]
    record.save(flow, env)
    code, out, _ = invoke(env, "--status", flow["id"], "--json")
    assert code == 0
    report = json.loads(out)
    assert report["tasks"][0]["denied_paths"] == [item]
    assert report["denied_paths"] == [{**item, "task": "a"}]
    assert json.loads(invoke(env, "--status", "--json")[1])[0]["denied_paths"] == [
        {**item, "task": "a"}
    ]
    code, out, _ = invoke(env, "--status", flow["id"])
    assert code == 0
    assert f"denied {item['path']} ({reason}) run {item['run']} turn 2" in out


@pytest.mark.parametrize("held", [True, False])
def test_accept_on_a_conflict_checks_the_flow_branch(
    env: dict[str, str], flow: dict[str, Any], held: bool
) -> None:
    stopped(flow, env)
    tip = flow["tip"]
    if not held:
        blob = subprocess.run(
            ["git", "-C", flow["dir"], "hash-object", "-w", "--stdin"],
            input="not merged\n",
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", flow["dir"], "mktree"],
            input=f"100644 blob {blob}\tother.txt\n",
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tip = subprocess.run(
            ["git", "-C", flow["dir"], "commit-tree", tree, "-p", tip, "-m", "x"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    flow["tasks"][0].update(status="conflict", merge_tip=tip)
    record.save(flow, env)
    with lock.hold(flow["id"], env):
        code, out, err = invoke(env, "--lead", flow["id"], "a", "--accept")
    answers = record.read_answers(flow["id"], env)
    if held:
        assert code == 0 and "queued answer 1: accept for a" in out, err
        assert answers[0] is not None and answers[0]["commit"] == tip
    else:
        assert code == 75 and not out
        assert f"does not hold {tip}" in err
        assert err.splitlines()[-1].endswith(f"emb flow --lead {flow['id']} a --accept")
        assert not answers


def test_accept_on_a_conflict_refuses_another_commit(
    env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    tree = subprocess.run(
        ["git", "-C", flow["dir"], "rev-parse", f"{flow['tip']}^{{tree}}"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    approved = subprocess.run(
        ["git", "-C", flow["dir"], "commit-tree", tree, "-p", flow["tip"], "-m", "a"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    flow["tasks"][0].update(status="conflict", merge_tip=approved)
    record.save(flow, env)
    with lock.hold(flow["id"], env):
        code, out, err = invoke(
            env, "--lead", flow["id"], "a", "--accept", "--commit", flow["tip"]
        )
    assert code == 64 and not out and "approved tip" in err
    assert not record.read_answers(flow["id"], env)


def test_dry_run_omits_plan_read_options_and_counts_sandbox_read(
    env: dict[str, str], plan_file: Path, tmp_path: Path
) -> None:
    issue = tmp_path / "issue 93.md"
    issue.write_text("#93\n")
    spec = tmp_path / "spec.md"
    spec.write_text("https://example.com/spec\n")
    raw = json.loads(plan_file.read_text())
    raw["tasks"][0]["task"] = "Build #93 from https://example.com/spec"
    raw["read"] = [str(issue)]
    plan_file.write_text(json.dumps(raw))
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", "--json")
    assert code == 0, err
    result = json.loads(out)
    words = shlex.split(result["gapcheck"])
    assert "--read" not in words
    assert words[words.index("--plan") + 1] == str(plan_file)
    code, text, err = invoke(env, "--plan", str(plan_file), "--dry-run")
    assert code == 0, err
    line = next(line for line in text.splitlines() if line.startswith("gap check:"))
    assert shlex.split(line.removeprefix("gap check: ")) == words
    cites = [w for w in result["warnings"] if " cites " in w]
    assert cites == [
        "task a cites https://example.com/spec, which no read path supplies"
    ]
    choices.set_settings({"sandbox.read": [str(spec)]}, env=env)
    code, out, err = invoke(env, "--plan", str(plan_file), "--dry-run", "--json")
    assert code == 0, err
    assert not [w for w in json.loads(out)["warnings"] if " cites " in w]


@pytest.mark.parametrize("mode", ["--dry-run", "--start"])
@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "whitespace",
        "nonempty",
        "folder",
        "symlink",
        "missing",
        "broken symlink",
        "fifo",
        "device",
        "undecodable",
        "unreadable",
    ],
)
def test_read_file_validation(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    kind: str,
) -> None:
    source = tmp_path / "read file.md"
    if kind == "folder":
        source.mkdir()
        (source / "empty.md").touch()
    elif kind == "symlink":
        target = tmp_path / "empty.md"
        target.touch()
        source.symlink_to(target)
    elif kind == "broken symlink":
        source.symlink_to(tmp_path / "missing.md")
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "device":
        source = Path("/dev/null")
    elif kind == "undecodable":
        source.write_bytes(b"\xff")
    elif kind != "missing":
        source.write_text(
            " \t\n\r\u2003"
            if kind == "whitespace"
            else "source text"
            if kind == "nonempty"
            else ""
        )
    if kind == "unreadable":
        original = Path.read_text

        def read_text(
            path: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            if path == source:
                raise PermissionError("read denied")
            return original(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", read_text)
    raw = json.loads(plan_file.read_text())
    raw["read"] = [str(source)]
    plan_file.write_text(json.dumps(raw))
    code, out, err = invoke(env, "--plan", str(plan_file), mode)
    if kind in ("empty", "whitespace", "symlink"):
        assert code == 64 and not out
        assert err == (
            f"emb flow: the read file {source} is empty\nnext: emb flow --help\n"
        )
        assert not paths.runs_dir(env).exists()
        assert not tmux_log(tmp_path)
    else:
        assert code == 0, err
        assert "the read file" not in err
        assert out


@pytest.mark.parametrize("mode", ["--dry-run", "--start"])
@pytest.mark.parametrize("directory", ["missing", "not-checkout"])
def test_empty_reads_report_in_order_before_directory_and_git_checks(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    mode: str,
    directory: str,
) -> None:
    first, second = tmp_path / "first.md", tmp_path / "second.md"
    first.touch()
    second.write_text(" \n")
    raw = json.loads(plan_file.read_text())
    raw["read"] = [str(second), str(first)]
    raw["dir"] = str(tmp_path / "missing" if directory == "missing" else tmp_path)
    plan_file.write_text(json.dumps(raw))
    code, out, err = invoke(env, "--plan", str(plan_file), mode)
    assert code == 64 and not out
    assert err.splitlines() == [
        f"emb flow: the read file {second} is empty",
        f"emb flow: the read file {first} is empty",
        "next: emb flow --help",
    ]
    assert not paths.runs_dir(env).exists()
    assert not tmux_log(tmp_path)


@pytest.mark.parametrize("mode", ["--dry-run", "--start"])
def test_empty_sandbox_read_is_accepted(
    tmp_path: Path, env: dict[str, str], plan_file: Path, mode: str
) -> None:
    source = tmp_path / "empty.md"
    source.touch()
    choices.set_settings({"sandbox.read": [str(source)]}, env=env)
    code, out, err = invoke(env, "--plan", str(plan_file), mode)
    assert code == 0, err
    assert out


@pytest.mark.parametrize("when", ["off", "ask", "2", "3", "always"])
@pytest.mark.parametrize("as_json", [False, True])
def test_repeat_report_is_independent_of_gap_check_settings(
    tmp_path: Path, env: dict[str, str], plan_file: Path, when: str, as_json: bool
) -> None:
    choices.set_settings({"gapcheck.when": when}, env=env)
    earlier = tmp_path / "earlier result.json"
    earlier.write_text(json.dumps(checked_result(plan_file)))
    document = json.loads(plan_file.read_text())
    document["tasks"][1]["after"] = []
    document["tasks"][0]["task"] += " Changed scope."
    document["context"] = "New context"
    document["failure_model"] = "New cases"
    plan_file.write_text(json.dumps(document))
    code, out, err = invoke(
        env,
        "--plan",
        str(plan_file),
        "--dry-run",
        "--previous",
        str(earlier),
        *(["--json"] if as_json else []),
    )
    assert code == 0, err
    skipped = when in ("off", "3")
    if as_json:
        data = json.loads(out)
        comparison = data["gapcheck_comparison"]
        assert comparison["repeat_needed"]
        assert comparison["reasons"] == ["b: changed after", "changed waves"]
        assert comparison["judge"] == [
            "a: changed task text",
            "changed context for tasks: a, b",
            "changed failure_model for tasks: a, b",
        ]
        assert comparison["recovery"] is None
        assert (data["gapcheck"] is None) == skipped
        assert data["gapcheck_status"] == (
            "skipped" if skipped else "ask" if when == "ask" else "run"
        )
        if when == "ask":
            assert data["gapcheck"].endswith("--confirmed")
    else:
        assert "repeat needed: b: changed after" in out
        assert "repeat needed: changed waves" in out
        assert "lead judgement: a: changed task text" in out
        assert "lead judgement: changed context for tasks: a, b" in out
        assert "lead judgement: changed failure_model for tasks: a, b" in out
        assert ("gap check: skipped" in out) == skipped
        assert ("gap check: ask the owner" in out) == (when == "ask")
    assert "no repeat" not in out.lower()
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize(
    "state", ["old", "parse_error", "uncovered_steps", "unknown_steps"]
)
@pytest.mark.parametrize("as_json", [False, True])
def test_dry_run_reports_repeat_for_old_or_incomplete_results(
    tmp_path: Path, env: dict[str, str], plan_file: Path, state: str, as_json: bool
) -> None:
    made = checked_result(plan_file)
    if state == "old":
        del made["checked_plan"], made["schema_version"]
    else:
        made[state] = "bad reply" if state == "parse_error" else [1]
    earlier = tmp_path / "earlier.json"
    earlier.write_text(json.dumps(made))
    code, out, err = invoke(
        env,
        "--plan",
        str(plan_file),
        "--dry-run",
        "--previous",
        str(earlier),
        *(["--json"] if as_json else []),
    )
    assert code == 0, err
    if as_json:
        data = json.loads(out)
        report = data["gapcheck_comparison"]
        assert report["repeat_needed"]
        check_command = data["gapcheck"]
        recovery = report["recovery"]
    else:
        assert "repeat needed:" in out
        check_command = out.split("gap check: ")[1].strip()
        recovery = out
    if state != "old":
        assert "--previous" not in shlex.split(check_command)
        assert "complete the earlier check" in recovery
        assert "without passing the incomplete result as --previous" in recovery
    assert "no repeat" not in out.lower()


@pytest.mark.parametrize("kind", ["missing", "malformed", "not a result"])
def test_dry_run_validates_previous_input(
    tmp_path: Path, env: dict[str, str], plan_file: Path, kind: str
) -> None:
    earlier = tmp_path / "earlier.json"
    if kind != "missing":
        earlier.write_text("{" if kind == "malformed" else "{}")
    code, out, err = invoke(
        env, "--plan", str(plan_file), "--dry-run", "--previous", str(earlier)
    )
    assert code == (66 if kind == "missing" else 65)
    assert not out and "next:" in err


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--dry-run", "--plan", "missing.json"],
        ["--status"],
        ["--watch", "missing"],
        ["--resume", "missing"],
        ["--cancel", "missing"],
        ["--lead", "missing", "task", "--drop"],
    ],
)
def test_check_base_requires_start(env: dict[str, str], options: list[str]) -> None:
    code, out, err = invoke(env, *options, "--check-base")
    assert code == 64 and not out
    assert "--check-base needs --start" in err
    assert not paths.runs_dir(env).exists()


def test_check_base_requires_check(env: dict[str, str], plan_file: Path) -> None:
    code, out, err = invoke(env, "--plan", str(plan_file), "--start", "--check-base")
    assert code == 64 and not out
    assert "--check-base needs a plan with a check" in err
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize("check_base", [False, True])
def test_start_records_pending_base_check_before_launch(
    env: dict[str, str], plan_file: Path, check_base: bool
) -> None:
    data = json.loads(plan_file.read_text())
    choices.set_settings({"worktree.prepare": {data["dir"]: "true"}}, env=env)
    data["check"] = [sys.executable, "-c", "raise SystemExit(3)"]
    plan_file.write_text(json.dumps(data))
    launched = []

    def launch(flow_id: str) -> list[str]:
        flow = record.load(flow_id, env)
        assert flow["wave"] == 0
        assert flow["check_pending"] is check_base
        assert flow["check_base"] is check_base
        assert flow["checks"] == []
        assert flow["preparation"]["state"] == "ok"
        assert all(task["writer_run"] is None for task in flow["tasks"])
        launched.append(flow_id)
        return ["true"]

    code, _, err = invoke(
        env,
        "--plan",
        str(plan_file),
        "--start",
        *(["--check-base"] if check_base else []),
        argv=launch,
    )
    assert code == 0, err
    assert len(launched) == 1


def test_dry_run_does_not_execute_check(env: dict[str, str], plan_file: Path) -> None:
    marker = plan_file.parent / "check-ran"
    data = json.loads(plan_file.read_text())
    data["check"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    ]
    data["writer_check"] = [data["check"]]
    plan_file.write_text(json.dumps(data))
    code, _, err = invoke(env, "--plan", str(plan_file), "--dry-run")
    assert code == 0, err
    assert not marker.exists()
    assert not paths.runs_dir(env).exists()


def flow_files(flow_id: str, env: Mapping[str, str]) -> dict[str, bytes]:
    """Read the record, saved plan and event log for refusal comparisons."""
    folder = record.folder(flow_id, env)
    return {
        name: (folder / name).read_bytes()
        for name in (record.FLOW_NAME, record.PLAN_NAME, record.EVENTS_NAME)
    }


@pytest.mark.parametrize(
    "options",
    [
        ["--check", "[]", "--reason", "correct argv"],
        ["--check", '[" \\t"]', "--reason", "correct argv"],
        ["--check", '["true\\u0000"]', "--reason", "correct argv"],
        ["--check", '["true", "\\u0000"]', "--reason", "correct argv"],
        ["--check", '"true"', "--reason", "correct argv"],
        ["--check", "{}", "--reason", "correct argv"],
        ["--check", "null", "--reason", "correct argv"],
        ["--check", '["true", 1]', "--reason", "correct argv"],
        ["--check", "{", "--reason", "correct argv"],
        ["--check", '["true"]'],
        ["--check", '["true"]', "--reason", " \t\n"],
        ["--reason", "correct argv"],
    ],
)
def test_check_replacement_usage_preserves_files(
    env: dict[str, str], flow: dict[str, Any], options: list[str]
) -> None:
    flow.update(status="failed", check_pending=True, check=["false"])
    record.save(flow, env)
    before = flow_files(flow["id"], env)
    code, out, err = invoke(env, "--resume", flow["id"], *options)
    assert code == 64 and not out, err
    assert "next: emb flow --help" in err
    assert flow_files(flow["id"], env) == before


@pytest.mark.parametrize("mode", ["--status", "--watch", "--cancel"])
def test_check_requires_resume(
    env: dict[str, str], flow: dict[str, Any], mode: str
) -> None:
    before = flow_files(flow["id"], env)
    code, _, err = invoke(
        env, mode, flow["id"], "--check", '["true"]', "--reason", "correct argv"
    )
    assert code == 64 and "--check needs --resume" in err
    assert flow_files(flow["id"], env) == before


@pytest.mark.parametrize("status", record.FLOW_STATUSES)
@pytest.mark.parametrize("pending", [True, False])
def test_check_replacement_state_refused(
    env: dict[str, str], flow: dict[str, Any], status: str, pending: bool
) -> None:
    if status == "failed" and pending:
        return
    flow.update(status=status, check_pending=pending)
    record.save(flow, env)
    before = flow_files(flow["id"], env)
    code, _, err = invoke(
        env, "--resume", flow["id"], "--check", '["true"]', "--reason", "correct argv"
    )
    assert code == 75 and f"is {status}" in err
    assert f"next: emb flow --status {flow['id']}" in err
    assert flow_files(flow["id"], env) == before


@pytest.mark.parametrize("blocker", ["cancel", "live", "startup", "session"])
def test_check_replacement_busy_preserves_files(
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    flow.update(status="failed", check_pending=True, check=["false"])
    record.save(flow, env)
    folder = record.folder(flow["id"], env)
    with ExitStack() as stack:
        if blocker == "cancel":
            (folder / record.CANCEL_NAME).write_text(
                json.dumps({"at": time.time(), "reason": "stop"})
            )
        elif blocker == "live":
            stack.enter_context(lock.hold(flow["id"], env))
        elif blocker == "startup":
            (folder / "startup.json").write_text(json.dumps({"at": time.time()}))
        else:
            monkeypatch.setattr(command, "SESSION_EXIT_SECONDS", 0)
            monkeypatch.setattr(tmux, "exists", lambda *a, **kw: True)
            monkeypatch.setattr(tmux, "pane_dead", lambda *a, **kw: False)
        before = flow_files(flow["id"], env)
        code, _, err = invoke(
            env,
            "--resume",
            flow["id"],
            "--check",
            '["true"]',
            "--reason",
            "correct argv",
        )
        assert code == 75, err
        assert flow_files(flow["id"], env) == before


@pytest.mark.parametrize("wave", [0, 1, 3])
def test_check_replacement_record_event_status(
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    wave: int,
) -> None:
    flow.update(status="failed", check_pending=True, check=["false"], wave=wave)
    writer_check = [["pytest", "tests/test_a.py"]]
    flow["tasks"][0]["writer_check"] = writer_check
    record.save(flow, env)
    before = flow_files(flow["id"], env)
    saves = []
    save = record.save

    def capture(value: dict[str, Any], environment: Mapping[str, str]) -> None:
        saves.append(json.loads(json.dumps(value)))
        save(value, environment)

    monkeypatch.setattr(record, "save", capture)
    code, _, err = invoke(
        env,
        "--resume",
        flow["id"],
        "--check",
        '["true", ""]',
        "--reason",
        "empty argument is intentional",
    )
    assert code == 0, err
    assert len(saves) == 1
    saved = record.load(flow["id"], env)
    assert saved["check"] == ["true", ""] and saved["check_pending"]
    assert [task["writer_check"] for task in saved["tasks"]] == [writer_check, None]
    (change,) = saved["check_changes"]
    assert change == {
        "at": change["at"],
        "old": ["false"],
        "new": ["true", ""],
        "reason": "empty argument is intentional",
    }
    assert isinstance(change["at"], float)
    assert saves[0]["check_changes"] == [change]
    events = record.read_events(flow["id"], env)
    assert events[-1] == {"kind": "check_replaced", "seq": 2, **change}
    assert flow_files(flow["id"], env)[record.PLAN_NAME] == before[record.PLAN_NAME]
    for as_json in (False, True):
        code, out, err = invoke(
            env, "--status", flow["id"], *(["--json"] if as_json else [])
        )
        assert code == 0, err
        if as_json:
            status = json.loads(out)
            assert status["check_changes"] == [change]
            assert [task["writer_check"] for task in status["tasks"]] == [
                writer_check,
                None,
            ]
        else:
            assert f"latest check replacement: {json.dumps(change)}" in out


@pytest.mark.parametrize("failure", ["missing tool", "launch"])
def test_check_replacement_survives_launch_failure(
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    flow.update(status="failed", check_pending=True, check=["false"])
    record.save(flow, env)
    before = flow_files(flow["id"], env)

    def fail(*args: object, **kwargs: object) -> None:
        raise tmux.TmuxError("launch failed")

    with monkeypatch.context() as patch:
        if failure == "missing tool":
            patch.setattr(command.shutil, "which", lambda *a, **kw: None)
        else:
            patch.setattr(tmux, "new_session", fail)
        code, _, err = invoke(
            env,
            "--resume",
            flow["id"],
            "--check",
            '["true"]',
            "--reason",
            "correct argv",
        )
    assert code == (69 if failure == "missing tool" else 71), err
    assert f"emb flow --resume {flow['id']} uses the new check" in err
    assert err.splitlines().count(f"next: emb flow --resume {flow['id']}") == 1
    saved = record.load(flow["id"], env)
    assert saved["check"] == ["true"] and saved["check_pending"]
    assert len(saved["check_changes"]) == 1
    assert flow_files(flow["id"], env)[record.PLAN_NAME] == before[record.PLAN_NAME]
    assert (
        len(
            [
                e
                for e in record.read_events(flow["id"], env)
                if e["kind"] == "check_replaced"
            ]
        )
        == 1
    )
    code, _, err = invoke(env, "--resume", flow["id"])
    assert code == 0, err
    assert record.load(flow["id"], env)["check_changes"] == saved["check_changes"]


def test_status_names_a_task_whose_run_waits_for_the_provider(
    tmp_path: Path,
    env: dict[str, str],
    flow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "codex-20260101-000000-abcdef"
    waiting = {
        "kind": "capacity",
        "message": "Selected model is at capacity.",
        "since": 5,
    }
    outcome = {
        **dict.fromkeys(
            ("state", "reason", "provider_error", "signed_out", "incomplete", "exit")
        ),
        "seconds": 1.0,
        "waiting": waiting,
    }
    (tmp_path / "turn-1.outcome.json").write_text(json.dumps(outcome))
    loaded = SimpleNamespace(
        id=run_id, turn=1, folder=tmp_path, status="running", turn_state="sent"
    )
    monkeypatch.setattr(command.run_record, "load", lambda name, env: loaded)
    flow["tasks"][0].update(status="reviewing", reviewer_run=run_id)
    record.save(flow, env)
    code, out, _ = invoke(env, "--status", flow["id"], "--json")
    assert code == 0
    first, second = json.loads(out)["tasks"]
    assert first["provider_wait"] == {"run": run_id, **waiting}
    assert first["reason"] == (
        f"{run_id} waits for the provider (capacity): Selected model is at capacity."
    )
    assert second["provider_wait"] is None and second["reason"] is None
    code, out, _ = invoke(env, "--status", flow["id"])
    assert code == 0
    assert f"a reviewing: {run_id} waits for the provider (capacity)" in out
    flow["tasks"][0].update(status="stopped", reason="stalled")
    record.save(flow, env)
    stopped_task = json.loads(invoke(env, "--status", flow["id"], "--json")[1])[
        "tasks"
    ][0]
    assert stopped_task["reason"] == "stalled"
    assert stopped_task["provider_wait"] == {"run": run_id, **waiting}
    loaded.status = "failed"
    gone = json.loads(invoke(env, "--status", flow["id"], "--json")[1])["tasks"][0]
    assert gone["provider_wait"] is None


def test_a_new_lead_answer_names_the_answer_it_replaces(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    with lock.hold(flow["id"], env):
        code, out, err = invoke(env, "--lead", flow["id"], "a", "--review")
        assert code == 0 and out.strip() == "queued answer 1: review for a", err
        code, out, err = invoke(env, "--lead", flow["id"], "a", "--drop")
        assert code == 0, err
        assert out.strip() == "queued answer 2: drop for a (replaces answer 1)"
        code, out, err = invoke(
            env, "--lead", flow["id"], "a", "--review", "--reviewer", "codex:m@low"
        )
        assert code == 0, err
        assert out.strip() == "queued answer 3: review for a (replaces answers 1, 2)"
        code, out, _ = invoke(env, "--status", flow["id"], "--json")
        assert json.loads(out)["queued_answers"] == [
            {
                "n": 3,
                "task": "a",
                "kind": "review",
                "worker": "codex:m@low",
                "replaces": [1, 2],
            }
        ]
        code, out, _ = invoke(env, "--status", flow["id"])
        assert "queued answer 3: review for a codex:m@low" in out.splitlines()
    assert not tmux_log(tmp_path)


def test_withdraw_removes_a_queued_answer(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    with lock.hold(flow["id"], env):
        code, out, err = invoke(env, "--lead", flow["id"], "a", "--withdraw")
        assert code == 75 and not out
        assert err.splitlines() == [
            "emb flow: task a has no queued answer",
            f"next: emb flow --status {flow['id']}",
        ]
        assert not record.read_answers(flow["id"], env)
        assert invoke(env, "--lead", flow["id"], "a", "--drop")[0] == 0
        code, out, err = invoke(env, "--lead", flow["id"], "a", "--withdraw")
        assert code == 0, err
        assert out.strip() == "queued answer 2: withdraw for a (withdraws answer 1)"
        assert (
            json.loads(invoke(env, "--status", flow["id"], "--json")[1])[
                "queued_answers"
            ]
            == []
        )
        assert invoke(env, "--lead", flow["id"], "a", "--withdraw")[0] == 75
    assert not tmux_log(tmp_path)
    assert record.load(flow["id"], env)["tasks"][0]["status"] == "stopped"


def test_lead_without_a_task_queues_a_reviewer_change(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any]
) -> None:
    stopped(flow, env)
    code, out, err = invoke(env, "--lead", flow["id"], "--reviewer", "codex:m@low")
    assert code == 0, err
    assert out.splitlines() == [
        "queued answer 1: reviewer codex:m@low for the flow's later reviews",
        "no runner is live; the next runner takes this line",
    ]
    answer = record.read_answers(flow["id"], env)[0]
    assert answer is not None
    assert (answer["task"], answer["kind"]) == (None, "reviewer")
    assert answer["worker"] == "codex:m@low"
    assert not tmux_log(tmp_path)
    code, out, err = invoke(env, "--lead", flow["id"], "--reviewer", "codex:n@low")
    assert code == 0, err
    assert out.splitlines()[0].endswith("(replaces answer 1)")
    queued = json.loads(invoke(env, "--status", flow["id"], "--json")[1])[
        "queued_answers"
    ]
    assert [(item["n"], item["task"], item["worker"]) for item in queued] == [
        (2, None, "codex:n@low")
    ]
    code, out, _ = invoke(env, "--status", flow["id"])
    assert "queued answer 2: reviewer codex:n@low" in out.splitlines()


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        (["x", "a", "b", "--drop"], "--lead takes a flow and at most one task"),
        (["x"], "--lead without a task needs --reviewer and no answer"),
        (["x", "--drop"], "--lead without a task needs --reviewer and no answer"),
        (
            ["x", "--reviewer", "codex:m", "--withdraw"],
            "--lead without a task needs --reviewer and no answer",
        ),
        (["x", "a", "--drop", "--withdraw"], "--lead needs exactly one of"),
        (["x", "a", "--withdraw", "--reviewer", "codex:m"], "--reviewer needs"),
    ],
)
def test_lead_usage_with_the_queue_options(
    env: dict[str, str], options: list[str], problem: str
) -> None:
    code, out, err = invoke(env, "--lead", *options)
    assert code == 64 and not out
    assert problem in err
