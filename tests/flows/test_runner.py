import contextlib
import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember import branches, choices, paths
from ember.flows import briefs, lock, plan, record, runner
from ember.runs import record as runs
from ember.runs.core import Core, StartOptions
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    git,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
)

PASS = json.dumps({"verdict": "pass", "findings": []})
MUST = {
    "severity": "must",
    "file": "a",
    "problem": "wrong value",
    "scenario": "the result is wrong",
    "fix": "use the correct value",
}
FIX = json.dumps({"verdict": "fix", "findings": [MUST]})


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.env = make_env(tmp_path)
        self.repo = make_repo(tmp_path, self.env)
        self.scripts = tmp_path / "scripts.json"
        self.script_data = {"a read-only review;": "@reply " + PASS}
        self.scripts.write_text(json.dumps(self.script_data))
        self.driver = StubDriver("codex", script_path(self.env), scripts=self.scripts)
        self.id = record.new_id()

    def factory(self, env: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        return Core(
            env, load_driver=lambda name: self.driver, poll_seconds=0.01, **kwargs
        )

    def create(self, tasks: list[dict[str, Any]] | None = None, **options: Any) -> None:  # noqa: ANN401
        data = {
            "dir": str(self.repo),
            "tasks": tasks if tasks is not None else [task("a")],
            **options,
        }
        parsed = plan.parse(json.dumps(data), self.tmp / "plan.json")
        branch = branches.create_flow_branch(self.repo, self.id, "HEAD", self.env)
        worker = plan.Worker("codex", "gpt-5", None, "option")
        flow = record.new_flow(
            self.id,
            parsed,
            top=str(branch.top),
            main=str(branch.main),
            worktree=str(branch.worktree),
            base=branch.base,
            writer=worker,
            reviewer=worker,
            stall_minutes=0.03,
            claim=None,
        )
        record.create(flow, parsed.text, self.env)

    def run(self) -> dict[str, Any]:
        code = runner.run(
            self.id, self.env, core_factory=self.factory, poll_seconds=0.01
        )
        assert code == 0, self.flow()
        return self.flow()

    def flow(self) -> dict[str, Any]:
        return record.load(self.id, self.env)

    def scripts_update(self, **items: str) -> None:
        self.script_data.update(items)
        self.scripts.write_text(json.dumps(self.script_data))

    def answer(self, kind: str, **fields: Any) -> None:  # noqa: ANN401
        record.append_answer(self.id, {"task": "a", "kind": kind, **fields}, self.env)

    def instance(self, shutdown: threading.Event | None = None) -> runner._Runner:
        return runner._Runner(
            self.id, self.env, self.factory, 0.01, shutdown or threading.Event()
        )


def task(
    name: str,
    *,
    text: str | None = None,
    files: list[str] | None = None,
    after: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": name,
        "subject": f"feat: {name}",
        "task": text or f"@write {name} value",
        "files": files or [name],
        "after": after or [],
    }


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    try:
        yield harness
    finally:
        kill_sessions(tmp_path)


def test_parallel_wave_merges_cleans_and_checks(h: Harness) -> None:
    h.create([task("a"), task("b")], check=[sys.executable, "-c", "print('checked')"])
    assert h.flow()["plan_waves"] == [["a", "b"]]
    flow = h.run()
    assert flow["plan_waves"] == [["a", "b"]]
    assert flow["status"] == "done"
    assert flow["wave"] == 1
    assert [t["status"] for t in flow["tasks"]] == ["merged", "merged"]
    commits = git(
        h.repo,
        "rev-list",
        "--first-parent",
        f"{flow['base']}..{flow['branch']}",
        env=h.env,
    ).splitlines()
    assert commits == [t["merge_commit"] for t in reversed(flow["tasks"])]
    assert len(flow["checks"]) == 1
    assert Path(flow["checks"][0]["log"]).read_text() == "checked\n"
    assert flow["writer"]["effort"] == "medium"
    for item in flow["tasks"]:
        message = git(
            h.repo, "show", "-s", "--format=%B", item["merge_commit"], env=h.env
        )
        assert message.splitlines()[0] == item["subject"]
        assert "Ember-Verdict: pass" in message
        assert f"Ember-Task-Tip: {item['head']}" in message
        assert (
            len(
                git(
                    h.repo, "show", "-s", "--format=%P", item["merge_commit"], env=h.env
                ).split()
            )
            == 1
        )
        writer = runs.load(item["writer_run"], h.env)
        reviewer = runs.load(item["reviews"][0]["run"], h.env)
        assert writer.status == reviewer.status == "closed"
        assert writer.effort == reviewer.effort == "medium"
        assert writer.claim is reviewer.claim is None
        assert not Path(writer.worktree).exists()
        assert item["branch"] is item["worktree"] is None
    assert record.read_events(h.id, h.env)[-1]["exit"] == 0
    assert not lock.live(h.id, h.env)


@pytest.mark.parametrize("overlap", [False, True])
def test_dependencies_and_overlaps_form_two_waves(h: Harness, overlap: bool) -> None:
    second = task(
        "b",
        text="@write a second" if overlap else None,
        files=["a"] if overlap else None,
        after=[] if overlap else ["a"],
    )
    h.create([task("a"), second])
    assert h.flow()["plan_waves"] == [["a"], ["b"]]
    flow = h.run()
    assert flow["plan_waves"] == [["a"], ["b"]]
    assert flow["wave"] == 2
    a, b = flow["tasks"]
    assert runs.load(b["writer_run"], h.env).base == a["merge_commit"]


def test_fix_round_has_previous_head_and_real_plan_briefs(h: Harness) -> None:
    h.scripts_update(
        **{
            "Review of task a, round 1,": "@reply " + FIX,
            "Fix round 1 of task a": "@write a fixed",
        }
    )
    h.create(rounds=1, read=[str(h.scripts)])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert item["round"] == 1
    assert len(item["reviews"]) == 2
    folder = record.folder(h.id, h.env) / "briefs"
    fix = next(folder.glob("a-fix-*.md")).read_text()
    assert fix.startswith(briefs.FIX_FIRST.format(round=1, task="a", flow=h.id))
    second = sorted(folder.glob("a-review-*.md"))[-1].read_text()
    assert "1. a: wrong value" in second
    assert f"git diff {item['fix_base']} {item['head']}" in second
    assert not any(line.startswith("@") for line in second.splitlines())
    for review in item["reviews"]:
        assert runs.load(review["run"], h.env).read == (str(h.scripts),)


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (FIX, "unresolved after 0 fix rounds"),
        (
            json.dumps(
                {
                    "verdict": "fix",
                    "findings": [{"severity": "lead", "problem": "choose format"}],
                }
            ),
            "needs the lead",
        ),
        ("prose", "the review is unreadable"),
        (
            json.dumps(
                {
                    "verdict": "fix",
                    "findings": [{"severity": "should", "problem": "tidy"}],
                }
            ),
            "the review says fix but names no must or lead finding",
        ),
    ],
)
def test_review_stops_and_blocks_transitively(
    h: Harness, reply: str, reason: str
) -> None:
    h.script_data = {"a read-only review;": "@reply " + reply}
    h.scripts_update()
    h.create([task("c", after=["b"]), task("b", after=["a"]), task("a")], rounds=0)
    flow = h.run()
    assert flow["status"] == "stopped"
    assert [t["status"] for t in flow["tasks"]] == ["blocked", "blocked", "stopped"]
    assert reason in flow["tasks"][2]["reason"]
    events = record.read_events(h.id, h.env)
    assert not any(e["kind"] == "end" for e in events)
    stop = next(e for e in events if e["kind"] == "lead_stop")
    assert len(stop["choices"]) == 5


def test_exhausted_fix_budget(h: Harness) -> None:
    h.script_data = {
        "a read-only review;": "@reply " + FIX,
        "Fix round 1 of task a": "@write a fixed",
    }
    h.scripts_update()
    h.create(rounds=1)
    item = h.run()["tasks"][0]
    assert item["reason"] == "unresolved after 1 fix rounds"
    assert len(item["reviews"]) == 2


@pytest.mark.parametrize("text", ["Do nothing", "@write a value"])
def test_no_change_initial_or_fix(h: Harness, text: str) -> None:
    h.script_data = {"a read-only review;": "@reply " + FIX}
    h.scripts_update()
    h.create([task("a", text=text)], rounds=1)
    item = h.run()["tasks"][0]
    assert item["reason"] == (
        "the writer made no change"
        if text == "Do nothing"
        else "the fix made no change"
    )


def test_same_wave_declaration_violation_stops_before_review(h: Harness) -> None:
    h.create([task("a", text="@write b wrong"), task("b")])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["reason"] == "changed b, which task b in this wave declares"
    assert not item["reviews"]
    assert flow["tasks"][1]["status"] == "merged"


def test_outside_file_and_same_wave_merge_conflict(h: Harness) -> None:
    h.create(
        [
            task("a", text="@write README.md first"),
            task("b", text="@write README.md second"),
        ]
    )
    flow = h.run()
    assert flow["tasks"][0]["status"] == "merged"
    assert flow["tasks"][1]["status"] == "conflict"
    assert all(t["outside_files"] == ["README.md"] for t in flow["tasks"])
    assert flow["tip"] == flow["tasks"][0]["merge_commit"]
    assert (
        h.instance()
        .git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
        .returncode
        != 0
    )


@pytest.mark.parametrize("failure", ["@fail broken", "@provider rate limit"])
def test_writer_failure_and_provider_halt(h: Harness, failure: str) -> None:
    h.create([task("a", text=failure), task("b")], parallel=1)
    flow = h.run()
    assert flow["tasks"][0]["status"] == "stopped"
    assert "next:" not in flow["tasks"][0]["reason"]
    assert "owner:" not in flow["tasks"][0]["reason"]
    if failure.startswith("@provider"):
        assert flow["halted"] == "provider error on codex; new runs halted"
        assert flow["tasks"][1]["writer_run"] is None
        resumed = h.run()
        assert resumed["halted"] is None
        assert resumed["tasks"][0]["status"] == "stopped"
        assert resumed["tasks"][1]["status"] == "merged"
    else:
        assert flow["tasks"][1]["status"] == "merged"


@pytest.mark.parametrize("role", ["writer", "reviewer"])
@pytest.mark.parametrize(
    ("error_output", "reason"),
    [
        (
            "emb run: failed\nnext: retry\n\n  detail stays  \n\towner: lead\n"
            " \t\nlast diagnostic\n  next: inspect\nowner: user\n",
            "emb run: failed;   detail stays  ; last diagnostic",
        ),
        (
            "detail next: retry\n  detail owner: lead\nnext steps\nowner name\n",
            "detail next: retry;   detail owner: lead; next steps; owner name",
        ),
        ("next: retry\n", "the run exited 77"),
        ("\towner: lead\n", "the run exited 77"),
        ("\n  next: retry\n \t\n\towner: lead\n", "the run exited 77"),
        ("\n \t\n", "the run exited 77"),
        ("", "the run exited 77"),
    ],
)
def test_run_error_stop_reason(
    h: Harness, role: str, error_output: str, reason: str
) -> None:
    """Keep diagnostics in order and omit next and owner instruction lines."""
    h.create()
    instance = h.instance()
    item = instance.flow["tasks"][0]

    def fail(core: Core) -> int:
        """Write the run's error output and return its failure code."""
        assert core.err is not None
        core.err.write(error_output)
        return 77

    assert not instance.call(item, role, fail)
    saved = h.flow()["tasks"][0]
    assert saved["status"] == "stopped"
    assert saved["reason"] == reason


def test_failed_check_resumes_with_a_distinct_log(h: Harness) -> None:
    h.create(check=[sys.executable, "-c", "print('bad'); raise SystemExit(3)"])
    flow = h.run()
    assert flow["status"] == "failed" and flow["check_pending"]
    assert record.read_events(h.id, h.env)[-1]["exit"] == 82
    flow["check"] = [sys.executable, "-c", "print('good')"]
    record.save(flow, h.env)
    flow = h.run()
    assert flow["status"] == "done" and not flow["check_pending"]
    assert [c["exit"] for c in flow["checks"]] == [3, 0]
    assert len({c["log"] for c in flow["checks"]}) == 2
    assert flow["attempt"] == 2


def test_check_timeout_kills_group(h: Harness) -> None:
    pidfile = h.tmp / "check.pid"
    h.create(
        check=[
            sys.executable,
            "-c",
            "import os,time,pathlib; "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)",
        ],
        check_timeout=1,
    )
    flow = h.run()
    assert flow["checks"][0]["exit"] == 124
    assert runner._process_start(int(pidfile.read_text())) is None


def test_check_cleans_files_and_environment(h: Harness) -> None:
    h.create(
        [task("a"), task("b", after=["a"])],
        check=[
            sys.executable,
            "-c",
            "import os,pathlib; assert 'GIT_DIR' not in os.environ; "
            "assert 'EMBER_FLOW_RUNNER' not in os.environ; "
            "pathlib.Path('leftover').write_text('x')",
        ],
    )
    h.env.update(GIT_DIR="wrong", EMBER_FLOW_RUNNER=h.id)
    flow = h.run()
    assert flow["status"] == "done"
    assert len(flow["checks"]) == 2
    assert not (Path(flow["worktree"]) / "leftover").exists()


@pytest.mark.parametrize("kind", ["accept", "review", "send", "worker", "drop"])
def test_answers_resume_stopped_tasks(h: Harness, kind: str) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    h.create([task("a"), task("b", after=["a"])], rounds=0)
    assert h.flow()["plan_waves"] == [["a"], ["b"]]
    before = h.run()["tasks"][0]
    h.script_data = {"a read-only review;": "@reply " + PASS}
    h.scripts_update()
    fields: dict[str, Any] = {}
    if kind == "accept":
        fields["commit"] = before["head"]
    elif kind == "send":
        correction = h.tmp / "correction.md"
        correction.write_text("@write a corrected")
        fields["file"] = str(correction)
    elif kind in ("worker", "review"):
        fields["worker"] = "codex:another@low"
    h.answer(kind, **fields)
    flow = h.run()
    assert flow["plan_waves"] == [["a"], ["b"]]
    item = flow["tasks"][0]
    assert flow["answers_seen"] == 1
    assert item["entry"] is None
    if kind == "drop":
        assert [t["status"] for t in flow["tasks"]] == ["dropped", "blocked"]
        assert record.read_events(h.id, h.env)[-1]["exit"] == 82
    else:
        assert all(t["status"] == "merged" for t in flow["tasks"])
    if kind in ("worker", "drop"):
        assert before["branch"] in flow["abandoned"]
        assert runs.load(before["writer_run"], h.env).status == "closed"
    if kind == "accept":
        assert len(item["reviews"]) == 1
    if kind == "worker":
        assert item["writer_run"] != before["writer_run"]
        assert runs.load(item["writer_run"], h.env).model == "another"
    if kind == "review":
        assert runs.load(item["reviews"][-1]["run"], h.env).model == "another"
    if kind == "send":
        assert item["round"] == 0


@pytest.mark.parametrize("rounds", [0, 1])
@pytest.mark.parametrize("retry_succeeds", [False, True])
def test_hook_refusal_send_uses_the_existing_fix_budget(
    h: Harness, rounds: int, retry_succeeds: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create(rounds=rounds)
    if rounds:
        h.scripts_update(
            **{
                "Review of task a, round 1,": "@reply " + FIX,
                "Fix round 1 of task a": "@write a needs-hook-fix",
            }
        )
    hook = h.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        'case "$(git show :a)" in\n'
        + ("value) exit 0;;\n" if rounds else "")
        + "corrected) exit 0;;\nesac\necho hook refused >&2\nexit 1\n"
    )
    hook.chmod(0o755)
    before = h.run()["tasks"][0]
    assert before["status"] == "stopped"
    assert "git commit was refused" in before["reason"]
    assert before["round"] == rounds
    writer = runs.load(before["writer_run"], h.env)
    assert writer.turn_state == runs.COLLECTED
    if retry_succeeds:
        hook.unlink()
    correction = h.tmp / "correction.md"
    correction.write_text("@write a corrected\n")
    sent: list[str] = []
    deliver = h.driver.send

    def capture(run: runs.Run, text: str) -> None:
        if run.id == writer.id:
            sent.append(text)
        deliver(run, text)

    monkeypatch.setattr(h.driver, "send", capture)
    h.answer("send", file=str(correction))
    instance = h.instance()
    instance.answers()
    assert instance.flow["tasks"][0]["round"] == rounds
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert item["round"] == rounds + 1
    assert flow["rounds"] == rounds
    writer = runs.load(item["writer_run"], h.env)
    assert len(sent) == 1
    assert sent[0].endswith("@write a corrected\n")
    assert sent[0].startswith("The commit hook refused") == (not retry_succeeds)
    if not retry_succeeds:
        assert "```\nhook refused\n```\n\n" in sent[0]
    assert len(writer.commits) == rounds + (2 if retry_succeeds else 1)
    assert git(h.repo, "show", f"{writer.commits[-1]}:a", env=h.env) == "corrected"
    subject = git(h.repo, "log", "-1", "--format=%s", writer.commits[-1], env=h.env)
    assert subject == writer.subject[:72] + f" (turn {rounds + 2})"


def test_hook_correction_does_not_grant_more_automatic_fix_rounds(h: Harness) -> None:
    h.create(rounds=0)
    hook = h.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        '#!/bin/sh\n[ "$(git show :a)" = corrected ] && exit 0\n'
        "echo hook refused >&2\nexit 1\n"
    )
    hook.chmod(0o755)
    assert h.run()["tasks"][0]["status"] == "stopped"
    correction = h.tmp / "correction.md"
    correction.write_text("@write a corrected\n")
    h.scripts_update(**{"a read-only review;": "@reply " + FIX})
    h.answer("send", file=str(correction))
    item = h.run()["tasks"][0]
    assert item["round"] == 1
    assert item["reason"] == "unresolved after 1 fix rounds"


def test_stale_and_unreadable_answers_advance_cursor(h: Harness) -> None:
    h.create([task("a", text="@write a value\n@reply done")], rounds=0)
    h.script_data = {"a read-only review;": "@reply " + FIX}
    h.scripts_update()
    flow = h.run()
    h.answer("accept", commit=flow["base"])
    with (record.folder(h.id, h.env) / record.ANSWERS_NAME).open("a") as stream:
        stream.write("not json\n")
    h.answer("drop")
    flow = h.run()
    assert flow["answers_seen"] == 3
    assert flow["tasks"][0]["status"] == "dropped"
    reasons = [e.get("reason", "") or "" for e in record.read_events(h.id, h.env)]
    assert any("the branch moved" in reason for reason in reasons)
    assert any("unreadable answer line 2" in reason for reason in reasons)


def test_dropped_pending_task_never_starts_and_blocks_dependants(h: Harness) -> None:
    h.create([task("a"), task("b", after=["a"]), task("c")])
    h.answer("drop")
    flow = h.run()
    states = {item["id"]: item for item in flow["tasks"]}
    assert states["a"]["status"] == "dropped"
    assert states["a"]["writer_run"] is None
    assert states["b"]["status"] == "blocked"
    assert states["c"]["status"] == "merged"


def test_second_runner_does_not_change_record(
    h: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    h.create()
    before = h.flow()
    with lock.hold(h.id, h.env):
        assert runner.run(h.id, h.env) == 75
    assert h.flow() == before
    assert "next: emb flow --watch" in capsys.readouterr().err


def test_run_id_is_saved_during_active_turn_and_stall_can_be_reviewed(
    h: Harness,
) -> None:
    h.create([task("a", text="@write a value\n@hang")])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        deadline = time.monotonic() + 10
        while not (run_id := h.flow()["tasks"][0]["writer_run"]):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not future.done()
        flow = future.result(timeout=10)
    assert flow["tasks"][0]["reason"] == f"stalled; the turn keeps running in {run_id}"
    (runs.load(run_id, h.env).folder / "stub" / "release").touch()
    h.answer("review")
    assert h.run()["tasks"][0]["status"] == "merged"


def test_stream_handles_split_announcements(h: Harness) -> None:
    captured: list[str] = []
    stream = runner._RunIdStream(captured.append)
    stream.write("noise\nru")
    stream.write("n codex-20261102-000000-abcdef")
    assert not captured
    stream.write("\nrun codex-20261102-000000-123456\n")
    assert captured == ["codex-20261102-000000-abcdef"]


def test_recovery_without_captured_writer(h: Harness) -> None:
    h.create()
    flow = h.flow()
    flow["tasks"][0].update(status="writing", wave=1)
    record.save(flow, h.env)
    assert h.run()["tasks"][0]["status"] == "merged"


def started_writer(
    h: Harness, *, trailers: tuple[str, ...] = ()
) -> tuple[dict[str, Any], runs.Run]:
    flow = h.flow()
    path = h.tmp / "writer.md"
    path.write_text("Implement a\n@write a value")
    out = io.StringIO()
    core = h.factory(h.env, out=out, err=out)
    assert (
        core.execute(
            lambda: core.start(
                StartOptions(
                    "codex",
                    flow["worktree"],
                    str(path),
                    model="gpt-5",
                    effort="low",
                    trailers=trailers,
                )
            )
        )
        == 0
    )
    run_id = out.getvalue().splitlines()[0][4:]
    return flow, runs.load(run_id, h.env)


@pytest.mark.parametrize("merged", [False, True])
def test_recover_writer_or_completed_merge(h: Harness, merged: bool) -> None:
    h.create(check=[sys.executable, "-c", "print('recovered check')"])
    flow, writer = started_writer(h)
    head = git(Path(writer.worktree), "rev-parse", "HEAD", env=h.env)
    item = flow["tasks"][0]
    item.update(
        status="merging" if merged else "writing",
        writer_run=writer.id,
        branch=writer.branch if merged else None,
        worktree=writer.worktree if merged else None,
        task_base=writer.base,
        merge_tip=head if merged else None,
        wave=1,
    )
    if merged:
        assert writer.branch is not None
        repo = branches.open_repository(flow["worktree"], h.env, "merge")
        branches._merge(
            repo,
            writer.branch,
            flow["branch"],
            Path(flow["worktree"]),
            "pass",
            item["subject"],
        )
    record.save(flow, h.env)
    flow = h.run()
    assert flow["tasks"][0]["status"] == "merged"
    assert len(flow["checks"]) == 1
    assert flow["tasks"][0]["merge_commit"] == flow["tip"]


def test_recover_closed_review_reply(h: Harness) -> None:
    h.create()
    flow, writer = started_writer(h)
    path = h.tmp / "review.md"
    path.write_text("Review\n@reply " + PASS)
    out = io.StringIO()
    core = h.factory(h.env, out=out, err=out)
    assert (
        core.execute(
            lambda: core.start(
                StartOptions(
                    "codex", writer.worktree, str(path), model="gpt-5", read_only=True
                )
            )
        )
        == 0
    )
    reviewer = out.getvalue().splitlines()[0][4:]
    assert core.execute(lambda: core.close(reviewer)) == 0
    flow["tasks"][0].update(
        status="reviewing",
        writer_run=writer.id,
        reviewer_run=reviewer,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=writer.base,
        head=writer.expected_head,
        wave=1,
    )
    record.save(flow, h.env)
    item = h.run()["tasks"][0]
    assert item["status"] == "merged"
    assert item["reviews"][0]["run"] == reviewer


def test_recovered_explicit_review_of_a_moved_branch_starts_fresh(
    h: Harness,
) -> None:
    h.create()
    flow, writer = started_writer(h)
    path = h.tmp / "review.md"
    path.write_text("Review\n@reply " + PASS)
    out = io.StringIO()
    core = h.factory(h.env, out=out, err=out)
    assert (
        core.execute(
            lambda: core.start(
                StartOptions(
                    "codex", writer.worktree, str(path), model="gpt-5", read_only=True
                )
            )
        )
        == 0
    )
    reviewer = out.getvalue().splitlines()[0][4:]
    assert core.execute(lambda: core.close(reviewer)) == 0
    moved = Path(writer.worktree) / "moved.txt"
    moved.write_text("after the review started\n")
    git = ["git", "-C", str(writer.worktree)]
    subprocess.run([*git, "add", "moved.txt"], check=True, env=h.env)
    subprocess.run([*git, "commit", "-q", "-m", "moved"], check=True, env=h.env)
    tip = subprocess.run(
        [*git, "rev-parse", "HEAD"],
        check=True,
        env=h.env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    flow["tasks"][0].update(
        status="reviewing",
        writer_run=writer.id,
        reviewer_run=reviewer,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=writer.base,
        head=writer.expected_head,
        explicit_review=True,
        wave=1,
    )
    record.save(flow, h.env)
    item = h.run()["tasks"][0]
    assert item["head"] == tip
    assert all(review["run"] != reviewer for review in item["reviews"])


def test_recover_unlaunched_writer_stops(h: Harness) -> None:
    h.create()
    flow, writer = started_writer(h)
    runs.update(writer, {"turn": 0, "turn_state": None})
    flow["tasks"][0].update(status="writing", writer_run=writer.id)
    record.save(flow, h.env)
    item = h.run()["tasks"][0]
    assert item["status"] == "stopped"
    assert "not launched" in item["reason"] and "--worker" in item["reason"]


def test_menu_does_not_change_frozen_default(h: Harness) -> None:
    h.create([task("a"), task("b", after=["a"])])
    choices.set_settings(
        {"single.writer": "codex:gpt-5", "single.reviewer": "codex:gpt-5"}, env=h.env
    )
    flow = h.run()
    assert all(
        runs.load(t["writer_run"], h.env).effort == "medium" for t in flow["tasks"]
    )


def test_driver_without_default_does_not_read_menu_effort(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(h.driver, "default_effort", lambda model: None)
    choices.set_settings(
        {"single.writer": "codex:gpt-5", "single.reviewer": "codex:gpt-5"}, env=h.env
    )
    h.create()
    item = h.run()["tasks"][0]
    assert runs.load(item["writer_run"], h.env).effort is None
    assert runs.load(item["reviews"][0]["run"], h.env).effort is None


def test_shutdown_during_record_save_is_not_reentrant(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create()
    shutdown = threading.Event()
    original = record.save

    def saving(flow: dict[str, Any], env: Mapping[str, str] | None = None) -> None:
        shutdown.set()
        original(flow, env)

    monkeypatch.setattr(record, "save", saving)
    assert runner._run(h.id, h.env, h.factory, 0.01, shutdown) == 0
    flow = h.flow()
    assert flow["status"] == "failed"
    assert "stopped by a signal" in flow["reason"]
    assert not lock.live(h.id, h.env)


def test_check_cleanup_failure_retains_pending(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create(check=[sys.executable, "-c", "print('ok')"])
    original = runner._Runner.clean
    calls = 0

    def clean(instance: runner._Runner) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise runner.RunnerError("git clean: denied")
        original(instance)

    monkeypatch.setattr(runner._Runner, "clean", clean)
    flow = h.run()
    assert flow["status"] == "failed" and flow["check_pending"]
    assert "git clean: denied" in flow["reason"]
    assert h.run()["status"] == "done"


@pytest.mark.parametrize("delivered", [False, True])
def test_recover_scheduled_correction_once(h: Harness, delivered: bool) -> None:
    h.create()
    flow, writer = started_writer(h)
    path = h.tmp / "send.md"
    path.write_text("Correction\n@write a corrected")
    if delivered:
        stream = io.StringIO()
        core = h.factory(h.env, out=stream, err=stream)
        assert core.execute(lambda: core.send(writer.id, str(path))) == 0
    flow["tasks"][0].update(
        status="fixing",
        writer_run=writer.id,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=writer.base,
        head=writer.expected_head,
        fix_base=writer.expected_head,
        turn_target=2,
        turn_pending=True,
        turn_file=str(path),
        turn_kind="send",
        answer=1,
        wave=1,
    )
    record.save(flow, h.env)
    item = h.run()["tasks"][0]
    assert item["status"] == "merged"
    assert runs.load(writer.id, h.env).turn == 2
    assert item["fix_base"] == writer.expected_head


def test_missing_reviewer_id_restarts_review(h: Harness) -> None:
    h.create()
    flow, writer = started_writer(h)
    flow["tasks"][0].update(
        status="reviewing",
        writer_run=writer.id,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=writer.base,
        head=writer.expected_head,
        wave=1,
    )
    record.save(flow, h.env)
    assert h.run()["tasks"][0]["status"] == "merged"


def test_changed_approved_tip_is_refused_before_merge(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create()
    original = runner._Runner.merge

    def moved(instance: runner._Runner, item: dict[str, Any]) -> bool:
        path = Path(item["worktree"])
        (path / "a").write_text("unreviewed\n")
        git(path, "add", "a", env=h.env)
        git(path, "commit", "-m", "unreviewed", env=h.env)
        return original(instance, item)

    monkeypatch.setattr(runner._Runner, "merge", moved)
    flow = h.run()
    assert flow["tasks"][0]["status"] == "stopped"
    assert "the branch moved" in flow["tasks"][0]["reason"]
    assert flow["tip"] == flow["base"]


def test_successful_merge_with_cleanup_error_still_checks(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create(check=[sys.executable, "-c", "print('checked')"])
    original = branches.merge

    def merge(*args: Any, **kwargs: Any) -> int:  # noqa: ANN401
        assert original(*args, **kwargs) == 0
        kwargs["err"].write("cleanup failed")
        return 70

    monkeypatch.setattr(branches, "merge", merge)
    flow = h.run()
    assert flow["status"] == "done"
    assert "cleanup failed" in flow["tasks"][0]["cleanup_error"]
    assert len(flow["checks"]) == 1


def test_recover_equivalent_containment_without_merge_commit(h: Harness) -> None:
    h.create(check=[sys.executable, "-c", "print('checked')"])
    flow, writer = started_writer(h)
    worktree = Path(flow["worktree"])
    (worktree / "a").write_text("value\n")
    git(worktree, "add", "a", env=h.env)
    git(worktree, "commit", "-m", "equivalent change", env=h.env)
    flow["tasks"][0].update(
        status="merging",
        writer_run=writer.id,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=writer.base,
        head=writer.expected_head,
        merge_tip=writer.expected_head,
        wave=1,
    )
    record.save(flow, h.env)
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert item["merge_commit"] is None
    assert item["contained_before"]
    assert flow["checks"][0]["exit"] == 0


def test_recover_unfinished_merge(h: Harness) -> None:
    h.create()
    flow, writer = started_writer(h)
    git(
        Path(flow["worktree"]),
        "merge",
        "--no-ff",
        "--no-commit",
        str(writer.branch),
        env=h.env,
    )
    flow["tasks"][0].update(
        status="merging",
        writer_run=writer.id,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=writer.base,
        head=writer.expected_head,
        merge_tip=writer.expected_head,
        wave=1,
    )
    record.save(flow, h.env)
    assert h.run()["tasks"][0]["status"] == "merged"


def test_review_close_refusal_is_reported_with_retained_writer(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create()
    monkeypatch.setattr(h.driver, "foreign_input", lambda run: "owner typed")
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert "stays open" in item["warnings"][0]
    assert item["cleanup"][0]["action"] == "kept"
    assert item["branch"] and Path(item["worktree"]).exists()


def test_pending_check_process_is_reconciled_before_cleanup(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create()
    flow = h.flow()
    flow["active_check"] = {"pid": 1234, "start": "ticks"}
    record.save(flow, h.env)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(runner, "_process_start", lambda pid: "ticks")
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert h.run()["status"] == "done"
    assert killed == [(1234, signal.SIGKILL)]


def test_recovery_does_not_kill_reused_check_pid(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(runner, "_process_start", lambda pid: "new")
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    runner._kill_check({"pid": 1234, "start": "old"})
    assert not killed


def test_review_failure_stops_task(h: Harness) -> None:
    h.scripts_update(**{"a read-only review;": "@fail reviewer unavailable"})
    h.create()
    item = h.run()["tasks"][0]
    assert item["status"] == "stopped"
    assert "reviewer unavailable" in item["reason"]


def test_active_writer_recovery(h: Harness) -> None:
    h.create()
    flow = h.flow()
    path = h.tmp / "writer.md"
    path.write_text("Write a\n@write a value\n@hang")
    out = io.StringIO()
    core = h.factory(h.env, stall_minutes=0.01, out=out, err=out)
    assert (
        core.execute(
            lambda: core.start(
                StartOptions("codex", flow["worktree"], str(path), model="gpt-5")
            )
        )
        == 124
    )
    run_id = out.getvalue().splitlines()[0][4:]
    flow["tasks"][0].update(status="writing", writer_run=run_id, wave=1)
    record.save(flow, h.env)
    (runs.load(run_id, h.env).folder / "stub" / "release").touch()
    assert h.run()["tasks"][0]["status"] == "merged"


def test_artifact_numbers_survive_replacement(h: Harness) -> None:
    h.scripts_update(**{"a read-only review;": "@reply " + FIX})
    h.create(rounds=0)
    h.run()
    directory = record.folder(h.id, h.env) / "briefs"
    before = {p.name: p.read_text() for p in directory.iterdir()}
    h.answer("worker", worker="codex:gpt-5")
    h.run()
    assert all((directory / name).read_text() == text for name, text in before.items())
    numbers = [int(p.stem.rsplit("-", 1)[1]) for p in directory.iterdir()]
    assert len(numbers) == len(set(numbers)) == 4


def test_changed_files_include_both_rename_sides(h: Harness) -> None:
    h.create([task("a", text="@git mv README.md renamed.md", files=["renamed.md"])])
    item = h.run()["tasks"][0]
    assert item["status"] == "merged"
    assert item["changed_files"] == ["README.md", "renamed.md"]
    assert item["outside_files"] == ["README.md"]


def test_recovered_turn_checks_original_wave_declarations(h: Harness) -> None:
    h.create([task("a"), task("b")])
    flow, writer = started_writer(h)
    worktree = Path(writer.worktree)
    (worktree / "b").write_text("wrong task\n")
    git(worktree, "add", "b", env=h.env)
    git(worktree, "commit", "-m", "wrong task", env=h.env)
    flow["tasks"][0].update(
        status="writing", writer_run=writer.id, wave=1, wave_declarations={"b": ["b"]}
    )
    flow["tasks"][1].update(status="dropped", wave=2)
    record.save(flow, h.env)
    item = h.run()["tasks"][0]
    assert item["reason"] == "changed b, which task b in this wave declares"
    assert item["reviews"] == []


def test_saved_answer_effect_closes_once_after_recovery(h: Harness) -> None:
    h.create()
    flow, writer = started_writer(h)
    flow["tasks"][0].update(
        status="dropped",
        writer_run=writer.id,
        branch=writer.branch,
        worktree=writer.worktree,
        answer=1,
        answer_effect={"n": 1, "run": writer.id},
    )
    flow["abandoned"] = [writer.branch]
    flow["answers_seen"] = 1
    record.save(flow, h.env)
    flow = h.run()
    assert flow["tasks"][0]["answer_effect"] is None
    assert runs.load(writer.id, h.env).status == "closed"
    assert Path(writer.worktree).exists()


def test_no_check_returns_running_and_clears_pending(h: Harness) -> None:
    h.create()
    instance = h.instance()
    instance.flow["check_pending"] = True
    assert instance.check()
    assert h.flow()["status"] == "running"
    assert not h.flow()["check_pending"]


def test_fresh_review_after_unchanged_fix(h: Harness) -> None:
    h.scripts_update(**{"a read-only review;": "@reply " + FIX})
    h.create(rounds=1)
    item = h.run()["tasks"][0]
    assert item["reason"] == "the fix made no change"
    h.scripts_update(**{"a read-only review;": "@reply " + PASS})
    h.answer("review")
    assert h.run()["tasks"][0]["status"] == "merged"


def test_answer_enqueue_waits_until_lifetime_lock_is_released(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create([task("a", text="Do nothing")])
    folder = record.folder(h.id, h.env)
    reservation = folder / "startup.json"
    reservation.write_text('{"reservation": "owned by command"}\n')
    before = reservation.read_bytes()
    final_read = threading.Event()
    release_runner = threading.Event()
    enqueue_attempted = threading.Event()
    execute = runner._Runner.execute

    def paused_execute(instance: runner._Runner, closing: contextlib.ExitStack) -> int:
        code = execute(instance, closing)
        final_read.set()
        assert release_runner.wait(10)
        return code

    def enqueue() -> bool:
        fd = paths.open_private_file(folder / "lifecycle.lock")
        try:
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                enqueue_attempted.set()
            fcntl.flock(fd, fcntl.LOCK_EX)
            h.answer("drop")
            return lock.live(h.id, h.env)
        finally:
            os.close(fd)

    monkeypatch.setattr(runner._Runner, "execute", paused_execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(h.run)
        try:
            assert final_read.wait(10)
            queued = pool.submit(enqueue)
            assert enqueue_attempted.wait(10)
        finally:
            release_runner.set()
        assert running.result(timeout=10)["status"] == "stopped"
        assert not queued.result(timeout=10)
    answer = record.read_answers(h.id, h.env)[0]
    assert answer is not None and answer["kind"] == "drop"
    assert reservation.read_bytes() == before
    monkeypatch.setattr(runner._Runner, "execute", execute)
    assert h.run()["tasks"][0]["status"] == "dropped"


def test_answer_at_final_read_is_taken_without_locking_the_next_wave(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create([task("a", text="Do nothing")])
    original_read = record.read_answers
    original_jobs = runner._Runner.jobs
    queued = False
    waves = 0

    def read_answers(
        flow_id: str, env: Mapping[str, str] | None = None
    ) -> list[dict[str, Any] | None]:
        nonlocal queued
        fd = paths.open_private_file(record.folder(flow_id, env) / "lifecycle.lock")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Model a command that appended immediately before the final read.
                if not queued:
                    queued = True
                    correction = h.tmp / "correction.md"
                    correction.write_text("@write a corrected")
                    h.answer("send", file=str(correction))
        finally:
            os.close(fd)
        return original_read(flow_id, env)

    def jobs(instance: runner._Runner, wave: list[dict[str, Any]]) -> None:
        nonlocal waves
        waves += 1
        fd = paths.open_private_file(instance.folder / "lifecycle.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original_jobs(instance, wave)
        finally:
            os.close(fd)

    monkeypatch.setattr(record, "read_answers", read_answers)
    monkeypatch.setattr(runner._Runner, "jobs", jobs)
    flow = h.run()
    assert queued and waves == 2
    assert flow["tasks"][0]["status"] == "merged"
    assert flow["answers_seen"] == 1
    assert not (record.folder(h.id, h.env) / "startup.json").exists()


@pytest.mark.parametrize("failed_check", [False, True])
def test_answer_during_check_continues_provider_halted_runner(
    h: Harness, monkeypatch: pytest.MonkeyPatch, failed_check: bool
) -> None:
    started = h.tmp / "check-started"
    release = h.tmp / "check-release"
    h.scripts_update(**{"\nALPHA_WRITE_MARKER\n": "@provider limited"})
    h.create(
        [task("a", text="ALPHA_WRITE_MARKER"), task("b")],
        check=[
            sys.executable,
            "-c",
            "import pathlib,time,sys; "
            f"started=pathlib.Path({str(started)!r}); "
            f"release=pathlib.Path({str(release)!r}); "
            "first=not started.exists(); started.touch(); "
            "deadline=time.monotonic()+15\n"
            "while not release.exists() and time.monotonic()<deadline: "
            "time.sleep(.01)\n"
            f"sys.exit(1 if first and {failed_check!r} else 0)",
        ],
    )
    b_finished = threading.Event()
    pipeline = runner._Runner.pipeline

    def ordered(instance: runner._Runner, item: dict[str, Any]) -> None:
        if item["id"] == "a":
            assert b_finished.wait(10)
        pipeline(instance, item)
        if item["id"] == "b":
            b_finished.set()

    monkeypatch.setattr(runner._Runner, "pipeline", ordered)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        try:
            deadline = time.monotonic() + 10
            while not started.exists():
                assert not future.done(), future.result()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with runner._lifecycle_guard(record.folder(h.id, h.env)):
                flow = h.flow()
                assert lock.live(h.id, h.env) and not flow["runner"]["exiting"]
                assert flow["halted"] and flow["tasks"][0]["status"] == "stopped"
                h.scripts_update(**{"\nALPHA_WRITE_MARKER\n": "@write a replacement"})
                h.answer("worker", worker="codex:gpt-5@low")
        finally:
            release.touch()
        flow = future.result(timeout=15)
    assert flow["status"] == "done"
    assert flow["attempt"] == flow["answers_seen"] == 1
    assert flow["halted"] is None
    assert all(item["status"] == "merged" for item in flow["tasks"])
    assert [entry["exit"] for entry in flow["checks"]] == (
        [1, 0, 0] if failed_check else [0, 0]
    )


@pytest.mark.parametrize(
    "outcome", ["done", "stopped", "halted", "check", "merge", "unexpected", "signal"]
)
def test_every_attempt_outcome_holds_lifecycle_until_lifetime_release(
    h: Harness, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    tasks = [task("a")]
    options: dict[str, Any] = {}
    if outcome == "stopped":
        tasks = [task("a", text="Do nothing")]
    elif outcome == "halted":
        tasks = [task("a", text="@provider limited")]
    elif outcome == "check":
        options["check"] = [sys.executable, "-c", "raise SystemExit(1)"]
    h.create(tasks, **options)
    if outcome == "merge":
        monkeypatch.setattr(branches, "merge", lambda *args, **kwargs: 70)
    elif outcome == "unexpected":

        def broken(instance: runner._Runner) -> None:
            raise RuntimeError("recovery failed")

        monkeypatch.setattr(runner._Runner, "recover", broken)
    elif outcome == "signal":
        begin = runner._Runner.begin

        def interrupted(instance: runner._Runner) -> None:
            begin(instance)
            instance.shutdown.set()

        monkeypatch.setattr(runner._Runner, "begin", interrupted)
    execute = runner._Runner.execute
    guarded: list[bool] = []

    def verify(instance: runner._Runner, closing: contextlib.ExitStack) -> int:
        code = execute(instance, closing)
        assert lock.live(h.id, h.env)
        fd = paths.open_private_file(instance.folder / "lifecycle.lock")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            guarded.append(True)
        finally:
            os.close(fd)
        return code

    monkeypatch.setattr(runner._Runner, "execute", verify)
    code = runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01)
    assert code == (1 if outcome == "unexpected" else 0)
    assert guarded == [True]
    assert not lock.live(h.id, h.env)
    fd = paths.open_private_file(record.folder(h.id, h.env) / "lifecycle.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.parametrize("stale_head", [False, True])
def test_unchanged_lead_correction_compares_actual_tip(
    h: Harness, stale_head: bool
) -> None:
    h.create([task("a", text="Do nothing")])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["head"] is None
    if stale_head:
        extra = h.tmp / "extra.md"
        extra.write_text("@write a saved work")
        stream = io.StringIO()
        core = h.factory(h.env, out=stream, err=stream)
        assert core.execute(lambda: core.send(item["writer_run"], str(extra))) == 0
        item["head"] = item["task_base"]
        record.save(flow, h.env)
    actual_tip = git(Path(item["worktree"]), "rev-parse", "HEAD", env=h.env)
    correction = h.tmp / "correction.md"
    correction.write_text("Do nothing again")
    h.answer("send", file=str(correction))
    flow = h.run()
    item = flow["tasks"][0]
    assert item["reason"] == "the fix made no change"
    assert item["fix_base"] == actual_tip
    assert item["reviews"] == []
    assert flow["tip"] == flow["base"]
    # Recovery uses the same recorded comparison, without sending another turn.
    item["status"] = "fixing"
    record.save(flow, h.env)
    item = h.run()["tasks"][0]
    assert item["reason"] == "the fix made no change"
    assert runs.load(item["writer_run"], h.env).turn == (3 if stale_head else 2)
    assert item["reviews"] == []


@pytest.mark.parametrize(
    ("ending", "exit_code"), [("@empty", 80), ("@fail broken", 71)]
)
@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_review_inspects_committed_work_from_failed_turn(
    h: Harness, ending: str, exit_code: int, explicit: bool
) -> None:
    h.create([task("a", text=f"@write a saved\n{ending}")])
    flow = h.run()
    item = flow["tasks"][0]
    writer = runs.load(item["writer_run"], h.env)
    assert writer.last_exit == exit_code and writer.commits
    assert item["head"] is None
    if explicit:
        h.answer("review")
    else:
        item["status"] = "writing"
        record.save(flow, h.env)
    flow = h.run()
    item = flow["tasks"][0]
    if explicit:
        assert item["status"] == "merged"
        assert item["head"] == writer.commits[-1]
        assert len(item["reviews"]) == 1
        brief = next(
            (record.folder(h.id, h.env) / "briefs").glob("a-review-*.md")
        ).read_text()
        assert f"git diff {writer.base} {writer.commits[-1]}" in brief
    else:
        assert item["status"] == "stopped" and item["reviews"] == []
    assert runs.load(writer.id, h.env).last_exit == exit_code


def test_explicit_review_still_refuses_an_unchanged_failed_turn(h: Harness) -> None:
    h.create([task("a", text="@empty")])
    h.run()
    h.answer("review")
    item = h.run()["tasks"][0]
    assert item["reason"] == "the writer made no change"
    assert item["reviews"] == []


@pytest.mark.parametrize("already_saved", [False, True])
def test_pending_cancel_prevents_startup_wave(h: Harness, already_saved: bool) -> None:
    h.create()
    request = {"at": time.time(), "reason": "cancel before startup"}
    runs.write_private(
        record.folder(h.id, h.env) / record.CANCEL_NAME, json.dumps(request)
    )
    if already_saved:
        record.finish_cancel(h.flow(), request, h.env)
    flow = h.run()
    assert flow["status"] == "cancelled" and flow["reason"] == request["reason"]
    assert flow["wave"] == 0 and flow["tasks"][0]["writer_run"] is None
    assert not lock.live(h.id, h.env)
    events = record.read_events(h.id, h.env)
    assert len([event for event in events if event["kind"] == "end"]) == 1
    assert events[-1]["exit"] == 82


def test_cancel_finish_wins_over_queued_answers(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.create()

    def drive(instance: runner._Runner) -> tuple[str, None]:
        h.answer("drop")
        runs.write_private(
            instance.folder / record.CANCEL_NAME,
            json.dumps({"at": time.time(), "reason": "stop now"}),
        )
        instance.shutdown.set()
        return "failed", None

    monkeypatch.setattr(runner._Runner, "drive", drive)
    flow = h.run()
    assert flow["status"] == "cancelled" and flow["reason"] == "stop now"
    assert flow["answers_seen"] == 0
    assert flow["tasks"][0]["status"] == "pending"


@pytest.mark.parametrize("phase", ["writer", "reviewer", "checking"])
def test_cancel_live_runner_keeps_turns_and_stops_check(
    h: Harness, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from ember.flows import command

    check = [sys.executable, "-c", "import time; time.sleep(60)"]
    h.create(
        [task("a", text="@hang\n@write a value" if phase == "writer" else None)],
        **({"check": check} if phase == "checking" else {}),
    )
    if phase == "reviewer":
        h.scripts_update(**{"a read-only review;": "@hang\n@reply " + PASS})
    shutdown = threading.Event()
    monkeypatch.setattr(command, "POLL_SECONDS", 0.01)
    signalled: list[bool] = []
    pipeline_finished = threading.Event()
    pipeline = runner._Runner.pipeline

    def tracked_pipeline(instance: runner._Runner, item: dict[str, Any]) -> None:
        try:
            pipeline(instance, item)
        finally:
            pipeline_finished.set()

    monkeypatch.setattr(runner._Runner, "pipeline", tracked_pipeline)

    def signal_runner(fd: int, sig: int) -> None:
        assert sig == signal.SIGTERM
        request = record.cancel_request(h.id, h.env)
        assert request is not None and request["reason"] == "scope changed"
        assert command._startup(h.flow(), h.env)["at"] == 0
        signalled.append(True)
        shutdown.set()

    monkeypatch.setattr(command.signal, "pidfd_send_signal", signal_runner)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner._run, h.id, h.env, h.factory, 0.01, shutdown)
        deadline = time.monotonic() + 15
        try:
            while True:
                flow = h.flow()
                item = flow["tasks"][0]
                ready = (
                    flow.get("active_check")
                    if phase == "checking"
                    else item[f"{phase}_run"]
                    and runs.load(item[f"{phase}_run"], h.env).status == runs.RUNNING
                )
                if ready:
                    break
                assert time.monotonic() < deadline, flow
                time.sleep(0.01)
            check_pid = (flow.get("active_check") or {}).get("pid")
            out = io.StringIO()
            command._cancel(h.id, "scope changed", True, h.env, out, io.StringIO())
            assert future.result(timeout=10) == 0
        finally:
            shutdown.set()
    assert signalled == [True]
    flow = h.flow()
    assert flow["status"] == "cancelled" and flow["reason"] == "scope changed"
    assert not lock.live(h.id, h.env)
    report = json.loads(out.getvalue())
    if phase == "checking":
        assert check_pid is not None and runner._process_start(check_pid) is None
        assert flow["check_pending"] and flow["checks"][-1]["exit"] == 124
        assert report["open_runs"] == []
        flow["check"] = [sys.executable, "-c", "pass"]
        record.save(flow, h.env)
    else:
        run_id = flow["tasks"][0][f"{phase}_run"]
        assert {"task": "a", "role": phase, "run": run_id} in report["open_runs"]
        opened = runs.load(run_id, h.env)
        assert opened.status == runs.RUNNING
        assert Path(opened.worktree).is_dir()
        # Release the stub turn and collect it when the flow resumes.
        (opened.folder / "stub" / "release").touch()
        assert pipeline_finished.wait(10)
    (record.folder(h.id, h.env) / record.CANCEL_NAME).unlink()
    resumed = h.instance()
    resumed.begin()
    assert h.flow()["ended_at"] is None and h.flow()["reason"] is None
    assert h.flow()["tasks"] == flow["tasks"]
    h.scripts_update(**{"a read-only review;": "@reply " + PASS})
    final = h.run()
    assert final["status"] == "done", final["tasks"]
    assert final["tasks"][0]["status"] == "merged"


def test_recorded_plan_without_subject_keeps_resolved_subject(h: Harness) -> None:
    h.create()
    stored = record.folder(h.id, h.env) / record.PLAN_NAME
    data = json.loads(stored.read_text())
    del data["tasks"][0]["subject"]
    stored.write_text(json.dumps(data))
    flow = h.run()
    assert flow["status"] == "done"
    item = flow["tasks"][0]
    assert (
        git(h.repo, "show", "-s", "--format=%s", item["merge_commit"], env=h.env)
        == "feat: a"
    )


@pytest.mark.parametrize("subject", ["feat: update API", "feat!: update API"])
def test_breaking_change_warning_is_recorded_in_merge_event(
    h: Harness,
    subject: str,
) -> None:
    item = task("a")
    item["subject"] = subject
    h.create([item], trailers=["BREAKING CHANGE: use the new API"])
    flow = h.run()
    assert flow["status"] == "done"
    saved = flow["tasks"][0]
    writer = runs.load(saved["writer_run"], h.env)
    warning = f"WARNING: {writer.branch} holds a breaking change; the subject has no !"
    expected = [] if "!:" in subject else [warning]
    event = next(
        event
        for event in record.read_events(h.id, h.env)
        if event["kind"] == "task" and event.get("status") == "merged"
    )
    assert event["warnings"] == expected
    assert saved.get("warnings", []) == expected


@pytest.mark.parametrize("separator", ["=", "%"])
def test_recover_squash_after_target_edit_with_noncolon_worker_separator(
    h: Harness,
    separator: str,
) -> None:
    h.create(check=[sys.executable, "-c", "print('recovered check')"])
    git(h.repo, "config", "trailer.separators", separator, env=h.env)
    flow, writer = started_writer(h, trailers=(f"Reviewed-by {separator} A",))
    assert writer.branch is not None
    item = flow["tasks"][0]
    item.update(
        status="merging",
        writer_run=writer.id,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=writer.base,
        head=writer.expected_head,
        merge_tip=writer.expected_head,
        wave=1,
    )
    worktree = Path(flow["worktree"])
    repo = branches.open_repository(worktree, h.env, "merge")
    landed, _ = branches._merge(
        repo,
        writer.branch,
        flow["branch"],
        worktree,
        "pass",
        item["subject"],
    )
    assert landed is not None
    (worktree / "a").write_text("later target edit")
    git(worktree, "commit", "-am", "fix: edit task result", env=h.env)
    later = git(worktree, "rev-parse", "HEAD", env=h.env)
    record.save(flow, h.env)
    recovered = h.run()
    saved = recovered["tasks"][0]
    assert saved["status"] == "merged"
    assert saved["merge_commit"] == landed
    assert recovered["tip"] == later
    assert saved["cleanup"][0]["action"] == "removed"
    assert not Path(writer.worktree).exists()
    assert saved["branch"] is None
    assert recovered["checks"][0]["exit"] == 0


@pytest.mark.parametrize("parallel", [1, 2])
def test_capacity_exhaustion_does_not_halt_other_flow_tasks(
    h: Harness, monkeypatch: pytest.MonkeyPatch, parallel: int
) -> None:
    choices.set_settings({"provider.capacity_retries": "0"}, env=h.env)
    original = h.driver.wait

    def wait(run: runs.Run) -> Any:  # noqa: ANN401
        outcome = original(run)
        if outcome.provider_error and outcome.reason == "capacity":
            from dataclasses import replace

            return replace(outcome, provider_kind="capacity")
        return outcome

    monkeypatch.setattr(h.driver, "wait", wait)
    h.create([task("a", text="@provider capacity"), task("b")], parallel=parallel)
    flow = h.run()
    assert flow["halted"] is None
    assert flow["tasks"][0]["status"] == "stopped"
    assert flow["tasks"][1]["status"] == "merged"


@pytest.mark.parametrize("startup", [False, True])
def test_denied_paths_are_saved_and_emitted_after_successful_turn(
    h: Harness, monkeypatch: pytest.MonkeyPatch, startup: bool
) -> None:
    from ember import sandbox

    folder = Path(h.env["HOME"]) / (".profile" if startup else ".toolchain")
    if startup:
        folder.touch()
    else:
        folder.mkdir()
    denied = folder if startup else folder / "python"
    reason = "startup file" if startup else "hidden"
    choices.set_settings({"sandbox.codex": "on"}, env=h.env)
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    monkeypatch.setattr(Core, "_sandboxed", lambda self, spec, run, common: spec)
    h.create(
        [task("a", text=f"@write a value\n@reply bash: {denied}: Permission denied")]
    )
    flow = h.run()
    saved = flow["tasks"][0]["denied_paths"]
    assert len(saved) == 1
    assert saved[0]["path"] == str(folder)
    assert saved[0]["reason"] == reason
    assert saved[0]["allow_path"] == (None if startup else str(folder))
    assert saved[0]["run"] == flow["tasks"][0]["writer_run"]
    assert saved[0]["turn"] == 1
    events = [
        event
        for event in record.read_events(h.id, h.env)
        if event["kind"] == "denied_paths"
    ]
    assert len(events) == 1
    assert events[0]["task"] == "a"
    assert events[0]["role"] == "writer"
    assert events[0]["denied_paths"] == saved


def _conflicted(h: Harness) -> dict[str, Any]:
    h.create(
        [
            task("a", text="@write README.md first"),
            task("b", text="@write README.md second"),
        ]
    )
    flow = h.run()
    assert [t["status"] for t in flow["tasks"]] == ["merged", "conflict"]
    return flow


def _merge_by_hand(h: Harness, flow: dict[str, Any]) -> str:
    worktree = flow["worktree"]
    tip = flow["tasks"][1]["merge_tip"]

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", worktree, *args],
            env=h.env,
            capture_output=True,
            text=True,
            check=check,
        )

    assert git("merge", "--no-ff", "--no-edit", tip, check=False).returncode
    Path(worktree, "README.md").write_text("resolved\n")
    git("add", "README.md")
    git("commit", "--no-edit")
    return git("rev-parse", "HEAD").stdout.strip()


def test_conflict_reason_names_how_a_hand_merge_is_recorded(h: Harness) -> None:
    flow = _conflicted(h)
    reason = flow["tasks"][1]["reason"]
    assert f"emb flow --resume {h.id}" in reason
    assert f"emb flow --lead {h.id} b --accept" in reason


def test_hand_merged_conflict_is_recorded_as_merged_on_resume(h: Harness) -> None:
    flow = _conflicted(h)
    merge = _merge_by_hand(h, flow)
    resumed = h.run()
    item = resumed["tasks"][1]
    assert item["status"] == "merged" and item["merge_commit"] == merge
    assert resumed["status"] == "done" and resumed["reason"] is None


def test_accept_on_a_conflict_needs_the_tip_in_the_flow_branch(h: Harness) -> None:
    flow = _conflicted(h)
    tip = flow["tasks"][1]["merge_tip"]
    record.append_answer(h.id, {"task": "b", "kind": "accept", "commit": tip}, h.env)
    refused = h.run()
    assert refused["tasks"][1]["status"] == "conflict"
    stops = [
        event
        for event in record.read_events(h.id, h.env)
        if event.get("kind") == "lead_stop" and event.get("task") == "b"
    ]
    assert stops and f"{tip} is not in" in stops[-1]["reason"]
    _merge_by_hand(h, refused)
    record.append_answer(h.id, {"task": "b", "kind": "accept", "commit": tip}, h.env)
    assert h.run()["tasks"][1]["status"] == "merged"


def test_accept_on_a_conflict_takes_only_the_approved_tip(h: Harness) -> None:
    flow = _conflicted(h)
    record.append_answer(
        h.id, {"task": "b", "kind": "accept", "commit": flow["base"]}, h.env
    )
    refused = h.run()
    assert refused["tasks"][1]["status"] == "conflict"
    stops = [
        event
        for event in record.read_events(h.id, h.env)
        if event.get("kind") == "lead_stop" and event.get("task") == "b"
    ]
    assert "is not the approved tip" in stops[-1]["reason"]


def test_squashed_hand_merge_records_a_commit_on_the_flow_branch(
    h: Harness,
) -> None:
    flow = _conflicted(h)
    worktree = flow["worktree"]
    tip = flow["tasks"][1]["merge_tip"]

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", worktree, *args],
            env=h.env,
            capture_output=True,
            text=True,
            check=check,
        )

    git("merge", "--squash", tip, check=False)
    git("checkout", tip, "--", ".")
    git("add", "-A")
    git("commit", "-m", "squash b by hand")
    squash = git("rev-parse", "HEAD").stdout.strip()
    item = h.run()["tasks"][1]
    assert item["status"] == "merged" and item["merge_commit"] == squash
    assert (
        git(
            "merge-base", "--is-ancestor", squash, flow["branch"], check=False
        ).returncode
        == 0
    )


def test_reconcile_runs_before_a_failing_pending_check(h: Harness) -> None:
    flow = _conflicted(h)
    merge = _merge_by_hand(h, flow)
    saved = record.load(h.id, h.env)
    saved.update(check=["false"], check_pending=True)
    record.save(saved, h.env)
    code = runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01)
    item = h.flow()["tasks"][1]
    assert code != 0 or h.flow()["status"] == "failed"
    assert item["status"] == "merged" and item["merge_commit"] == merge


def test_accept_after_reconciliation_is_not_refused(h: Harness) -> None:
    flow = _conflicted(h)
    tip = flow["tasks"][1]["merge_tip"]
    _merge_by_hand(h, flow)
    record.append_answer(h.id, {"task": "b", "kind": "accept", "commit": tip}, h.env)
    done = h.run()
    assert done["tasks"][1]["status"] == "merged" and done["status"] == "done"
    assert done["answers_seen"] == 1
    assert not [
        event
        for event in record.read_events(h.id, h.env)
        if event.get("kind") == "lead_stop" and "refused" in event.get("reason", "")
    ]


def test_recovered_flow_routes_legacy_opencode_workers(h: Harness) -> None:
    h.create()
    h.driver = StubDriver("opencode", script_path(h.env), scripts=h.scripts)
    flow = h.flow()
    for role in ("writer", "reviewer"):
        flow[role] = {
            "engine": "opencode",
            "model": "anthropic/claude-x",
            "effort": "high",
            "source": "option",
        }
    flow.pop("opencode_id_format")
    path = record.folder(h.id, h.env) / record.FLOW_NAME
    path.write_text(json.dumps(flow))
    result = h.run()
    for role in ("writer", "reviewer"):
        assert result[role]["model"] == "openrouter/anthropic/claude-x"
    saved, errors = runs.list_runs(h.env)
    assert not errors and len(saved) >= 2
    assert all(worker.model == "openrouter/anthropic/claude-x" for worker in saved)
