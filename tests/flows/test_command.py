import fcntl
import io
import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ember import branches, choices, paths, tmux
from ember.cli import build_parser
from ember.flows import command, lock, plan, record
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
        assert result["writer"]["source"] == "plan"
        assert result["check"] == []
        gapcheck = result["gapcheck"]
    else:
        assert "writer codex:gpt-5@high (plan)" in out
        assert "wave 1:\n  a: a.py\nwave 2:\n  b: b.py" in out
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
    assert "WARNING: no failure_model" in err and "WARNING: no check" in err
    assert not paths.runs_dir(env).exists()
    assert not tmux_log(tmp_path)
    assert git(repo, "branch", "--list", env=env) == before


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
    code, out, err = invoke(env, *options, "--previous", "missing-result.json")
    assert code == 64 and not out
    assert "--previous needs --dry-run" in err
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
    assert err == f"next: emb flow --watch {flow_id} --json\n"


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
    if status == "failed":
        assert "fix the flow branch" in err
    # Already consumed terminal events still give the terminal outcome.
    if status in record.ENDED:
        again, output, _ = invoke(env, "--watch", flow["id"], "--json")
        assert again == expected and json.loads(output)["event"] == "end"


def test_done_unmerged(env: dict[str, str], flow: dict[str, Any]) -> None:
    flow["status"] = "done"
    flow["tasks"][0]["status"] = "dropped"
    record.save(flow, env)
    code, _, err = invoke(env, "--watch", flow["id"])
    assert code == 82 and "report the unmerged tasks to the owner" in err


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


def test_status(env: dict[str, str], flow: dict[str, Any]) -> None:
    with lock.hold(flow["id"], env):
        code, out, _ = invoke(env, "--status", flow["id"], "--json")
        assert code == 0 and json.loads(out)["runner_live"]
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
        assert err == result["next"] + "\n"


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
    assert err.strip() == f"next: emb flow --resume {flow['id']}"
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


def test_status_keeps_denial_provenance_from_completed_workers(
    env: dict[str, str], flow: dict[str, Any]
) -> None:
    item = {
        "path": "/home/owner/tools",
        "reason": "hidden",
        "allow_path": "/home/owner/tools/bin",
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
    assert f"denied {item['path']} (hidden) run {item['run']} turn 2" in out


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
