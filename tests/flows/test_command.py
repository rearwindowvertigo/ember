import io
import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
                    {"id": "a", "task": "First", "files": ["a.py"]},
                    {"id": "b", "task": "Second", "files": ["b.py"], "after": ["a"]},
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
def test_dry_run(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    repo: Path,
    as_json: bool,
) -> None:
    choices.claim("flow-claim", env=env)
    before = git(repo, "branch", "--list", env=env)
    code, out, err = invoke(
        env,
        "--plan",
        str(plan_file),
        "--dry-run",
        "--claim",
        "flow-claim",
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
    assert current["writer"]["effort_frozen"] is True
    assert git(repo, "rev-parse", current["branch"], env=env) == current["base"]
    assert Path(current["worktree"]).is_dir()
    calls = tmux_log(tmp_path)
    launch = next(call for call in calls if "new-session" in call)
    assert launch[launch.index("-s") + 1] == flow_id
    assert f"EMBER_FLOW_RUNNER={flow_id}" in launch and launch[-1] == "true"
    assert err == f"next: emb flow --watch {flow_id} --json\n"


@pytest.mark.parametrize(
    ("case", "expected"), [("tmux", 69), ("branch", 70), ("runtime", 69)]
)
def test_start_failure(
    env: dict[str, str], plan_file: Path, repo: Path, case: str, expected: int
) -> None:
    if case == "tmux":
        (Path(env["PATH"].split(os.pathsep)[0]) / "tmux").unlink()
    elif case == "branch":
        (repo / ".ember").mkdir()
        (repo / ".ember" / "worktrees").write_text("file")
    else:
        Path(env["XDG_RUNTIME_DIR"]).write_text("file")
    code, out, err = invoke(env, "--plan", str(plan_file), "--start")
    assert code == expected and not out, err
    if case == "branch":
        flows, errors = record.list_flows(env)
        assert not errors and flows[0]["status"] == "failed"
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
    [("unknown", 64), ("pending", 75), ("done", 75), ("empty", 64), ("missing", 66)],
)
def test_lead_refusal(
    tmp_path: Path, env: dict[str, str], flow: dict[str, Any], case: str, expected: int
) -> None:
    stopped(flow, env)
    task = "a"
    options = ["--drop"]
    if case == "unknown":
        task = "unknown"
    elif case == "pending":
        flow["tasks"][0]["status"] = "pending"
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
    monkeypatch.setattr(command.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(command.time, "sleep", sleep)
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
