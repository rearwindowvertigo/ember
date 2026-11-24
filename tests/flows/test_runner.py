import contextlib
import fcntl
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import (
    branches,
    choices,
    holds,
    paths,
    prepare,
    processes,
    prune,
    sandbox,
    tmux,
)
from ember.cli import build_parser
from ember.engines import Driver, LaunchSpec
from ember.engines.opencode2 import DeliveryUncertainError
from ember.flows import briefs, command, lock, plan, record, runner
from ember.runs import record as runs
from ember.runs import worktrees
from ember.runs.core import Core, ReviewFollowUp, StartOptions
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


def outside_reply(reply: str) -> str:
    value = json.loads(reply)
    value["outside_files"] = [
        {"file": "README.md", "needed": True, "reason": "The task needs this edit"}
    ]
    return json.dumps(value)


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
        check_base = options.pop("check_base", False)
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
            check_base=check_base,
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


def test_flow_preparation_retry_cleans_first_and_keeps_logs(h: Harness) -> None:
    h.create(check=[sys.executable, "-c", "print('checked')"])
    choices.set_settings(
        {
            "worktree.prepare": {
                str(h.repo): "echo first; echo dirty > leftover; "
                "echo dirty > README.md; exit 8"
            }
        },
        env=h.env,
    )
    flow = h.flow()
    with pytest.raises(runner.PreparationFailedError):
        runner.prepare_worktree(flow, h.env)
    first = dict(h.flow()["preparation"])
    log = Path(first["log"])
    assert log.read_text() == "first\n"
    choices.set_settings(
        {
            "worktree.prepare": {
                str(h.repo): "test ! -e leftover && git diff --exit-code && echo second"
            }
        },
        env=h.env,
    )
    flow = h.run()
    assert flow["status"] == "done"
    assert flow["preparation"]["state"] == "ok"
    assert flow["preparation_history"] == [first]
    assert log.read_text() == "first\n"
    assert Path(flow["preparation"]["log"]).name == "prepare-1.log"
    assert Path(flow["preparation"]["log"]).read_text() == "second\n"
    saved = flow["preparation"]
    choices.set_settings({"worktree.prepare": {str(h.repo): "exit 9"}}, env=h.env)
    assert h.run()["preparation"] == saved


def test_preparation_failure_retries_only_blocked_children(h: Harness) -> None:
    h.create([task("a"), task("b"), task("c"), task("d", after=["a"])], parallel=1)
    flow = h.flow()
    flow["tasks"][2].update(status="stopped", reason="owner decision")
    record.save(flow, h.env)
    choices.set_settings(
        {"worktree.prepare": {str(h.repo): "echo failed; exit 8"}}, env=h.env
    )
    assert runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01) == 79
    failed = h.flow()
    assert failed["status"] == "failed" and failed["halted"]
    assert record.read_events(h.id, h.env)[-1]["exit"] == 79
    original = failed["tasks"][0]["writer_run"]
    assert runs.load(original, h.env).turn == 0
    assert failed["tasks"][1]["writer_run"] is None
    choices.set_settings({"worktree.prepare": {str(h.repo): "true"}}, env=h.env)
    resumed = h.run()
    assert resumed["status"] == "stopped"
    assert [t["status"] for t in resumed["tasks"]] == [
        "merged",
        "merged",
        "stopped",
        "merged",
    ]
    assert resumed["tasks"][2]["reason"] == "owner decision"
    assert resumed["tasks"][0]["writer_run"] != original
    assert resumed["tasks"][0]["preparation_runs"] == [
        {"role": "writer", "run": original}
    ]
    closed = runs.load(original, h.env)
    assert closed.status == runs.CLOSED and not Path(closed.worktree).exists()
    assert closed.preparation is not None
    assert Path(closed.preparation["log"]).read_text() == "failed\n"


def test_reviewer_preparation_retry_keeps_writer_work(h: Harness) -> None:
    h.create()
    choices.set_settings(
        {
            "worktree.prepare": {
                str(h.repo): "git symbolic-ref -q HEAD >/dev/null || exit 8"
            }
        },
        env=h.env,
    )
    assert runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01) == 79
    failed = h.flow()["tasks"][0]
    writer_id, reviewer_id, head = (
        failed["writer_run"],
        failed["reviewer_run"],
        failed["head"],
    )
    assert runs.load(writer_id, h.env).turn == 1
    assert runs.load(reviewer_id, h.env).turn == 0
    choices.set_settings({"worktree.prepare": {str(h.repo): "true"}}, env=h.env)
    resumed = h.run()["tasks"][0]
    assert resumed["status"] == "merged"
    assert resumed["writer_run"] == writer_id and resumed["head"] == head
    assert resumed["reviews"][0]["run"] != reviewer_id
    assert runs.load(writer_id, h.env).turn == 1
    assert runs.load(reviewer_id, h.env).status == runs.CLOSED


def test_preparation_retry_applies_worker_answer_first(h: Harness) -> None:
    h.create([task("a"), task("b")], parallel=1)
    choices.set_settings({"worktree.prepare": {str(h.repo): "exit 8"}}, env=h.env)
    assert runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01) == 79
    before = h.flow()["tasks"][0]
    assert before["preparation_blocked"] == "writer"
    h.answer("worker", worker="codex:another@low")
    choices.set_settings({"worktree.prepare": {str(h.repo): "true"}}, env=h.env)
    flow = h.run()
    answered, automatic = flow["tasks"]
    assert flow["status"] == "done" and flow["answers_seen"] == 1
    assert answered["status"] == automatic["status"] == "merged"
    assert answered["writer_run"] != before["writer_run"]
    assert answered["writer"]["model"] == "another"
    replacement = runs.load(answered["writer_run"], h.env)
    assert replacement.model == "another" and replacement.effort == "low"
    assert replacement.preparation is not None
    assert replacement.preparation["state"] == "ok"
    assert runs.load(before["writer_run"], h.env).status == runs.CLOSED
    assert answered["preparation_blocked"] is None
    assert automatic["writer"] is None
    assert not any(
        "answer 1 refused" in (event.get("reason") or "")
        for event in record.read_events(h.id, h.env)
    )


@pytest.mark.parametrize("kind", ["send", "review", "accept", "drop"])
def test_reviewer_preparation_retry_follows_lead_answer(h: Harness, kind: str) -> None:
    h.create()
    choices.set_settings(
        {
            "worktree.prepare": {
                str(h.repo): "git symbolic-ref -q HEAD >/dev/null || exit 8"
            }
        },
        env=h.env,
    )
    assert runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01) == 79
    before = h.flow()["tasks"][0]
    assert before["preparation_blocked"] == "reviewer"
    fields: dict[str, Any] = {}
    if kind == "send":
        correction = h.tmp / "correction.md"
        correction.write_text("@write a corrected")
        fields["file"] = str(correction)
    elif kind == "review":
        fields["worker"] = "codex:another@low"
    elif kind == "accept":
        fields["commit"] = before["head"]
    h.answer(kind, **fields)
    choices.set_settings(
        {
            "worktree.prepare": {
                str(h.repo): "exit 9" if kind in ("accept", "drop") else "true"
            }
        },
        env=h.env,
    )
    flow = h.run()
    item = flow["tasks"][0]
    assert flow["answers_seen"] == item["answer"] == 1
    assert item["status"] == ("dropped" if kind == "drop" else "merged")
    assert item["writer_run"] == before["writer_run"]
    assert item["preparation_blocked"] is None
    writer = runs.load(item["writer_run"], h.env)
    assert writer.turn == (2 if kind == "send" else 1)
    if kind == "send":
        assert git(h.repo, "show", f"{flow['branch']}:a", env=h.env) == "corrected"
        assert item["corrections"] == [
            str(record.folder(h.id, h.env) / "answers" / "1.md")
        ]
    elif kind == "review":
        reviewer = runs.load(item["reviews"][-1]["run"], h.env)
        assert reviewer.model == "another" and reviewer.effort == "low"
    else:
        assert item["reviews"] == []
    assert runs.load(before["reviewer_run"], h.env).status == runs.CLOSED
    assert item["preparation_runs"] == [
        {"role": "reviewer", "run": before["reviewer_run"]}
    ]
    assert not any(
        "answer 1 refused" in (event.get("reason") or "")
        for event in record.read_events(h.id, h.env)
    )


def test_preparation_retry_preserves_answer_refusal_reason(h: Harness) -> None:
    h.create()
    choices.set_settings(
        {
            "worktree.prepare": {
                str(h.repo): "git symbolic-ref -q HEAD >/dev/null || exit 8"
            }
        },
        env=h.env,
    )
    assert runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01) == 79
    before = h.flow()["tasks"][0]
    base = h.flow()["base"]
    h.answer("accept", commit=base)
    choices.set_settings({"worktree.prepare": {str(h.repo): "true"}}, env=h.env)
    flow = h.run()
    item = flow["tasks"][0]
    assert flow["answers_seen"] == 1 and item["status"] == "stopped"
    assert item["writer_run"] == before["writer_run"]
    assert item["reviewer_run"] == before["reviewer_run"]
    refused = [
        event["reason"]
        for event in record.read_events(h.id, h.env)
        if "answer 1 refused" in (event.get("reason") or "")
    ]
    assert refused == [
        f"answer 1 refused: the branch moved from {base} to {before['head']}"
    ]


@pytest.mark.parametrize("peer", ["prepared", "running"])
def test_preparation_halt_at_admission(
    h: Harness, monkeypatch: pytest.MonkeyPatch, peer: str
) -> None:
    h.create([task("a"), task("b", text="@sleep 0.3\n@write b value")], parallel=2)
    choices.set_settings({"worktree.prepare": {str(h.repo): "true"}}, env=h.env)
    original = prepare.run
    ready = threading.Event()
    peer_run: list[str] = []

    def waiting(predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 10
        while not predicate():
            assert time.monotonic() < deadline
            time.sleep(0.005)

    def preparation(
        shell: str,
        worktree: str | Path,
        log: str | Path,
        *,
        started: Callable[[int, int], None],
        stop: threading.Event | None = None,
        env: Mapping[str, str] | None = None,
    ) -> prepare.Result:
        run = runs.load(Path(log).parent.name, h.env)
        if run.subject == "feat: a":
            assert ready.wait(10)
            if peer == "running":
                waiting(lambda: runs.load(peer_run[0], h.env).turn == 1)
            shell = "echo failed; exit 8"
        else:
            peer_run.append(run.id)
            ready.set()
            if peer == "prepared":
                waiting(lambda: bool(h.flow().get("preparation_halt")))
        return original(shell, worktree, log, started=started, stop=stop, env=env)

    with monkeypatch.context() as patch:
        patch.setattr(prepare, "run", preparation)
        assert runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01) == 79
    blocked = h.flow()["tasks"][1]
    run = runs.load(peer_run[0], h.env)
    assert run.preparation is not None and run.preparation["state"] == "ok"
    assert run.turn == int(peer == "running")
    if peer == "running":
        assert blocked["head"] and blocked["preparation_blocked"] == "reviewer"
    else:
        assert blocked["preparation_blocked"] == "writer"
        assert not (run.folder / "launch.sh").exists()
    resumed = h.run()
    assert resumed["status"] == "done"
    current = resumed["tasks"][1]["writer_run"]
    assert (current == run.id) == (peer == "running")


def test_indeterminate_child_preparation_refuses_resume(h: Harness) -> None:
    h.create()
    flow, writer = started_writer(h)
    runs.update(
        writer,
        {
            "turn": 0,
            "turn_state": None,
            "preparation": {
                "command": "true",
                "state": "running",
                "log": str(writer.folder / "prepare.log"),
            },
        },
    )
    flow["tasks"][0].update(status="writing", writer_run=writer.id)
    record.save(flow, h.env)
    assert runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01) == 1
    assert "indeterminate" in h.flow()["reason"]
    assert h.flow()["tasks"][0]["writer_run"] == writer.id


def test_shutdown_waits_for_child_preparation_cleanup(h: Harness) -> None:
    h.create()
    shell = f"{sys.executable} -c 'import time; time.sleep(60)'"
    choices.set_settings({"worktree.prepare": {str(h.repo): shell}}, env=h.env)
    shutdown = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(runner._run, h.id, h.env, h.factory, 0.01, shutdown)
        deadline = time.monotonic() + 10
        while True:
            run_id = h.flow()["tasks"][0]["writer_run"]
            run = runs.load(run_id, h.env) if run_id else None
            if run and run.preparation and run.preparation.get("pid"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.005)
        shutdown.set()
        assert pending.result(timeout=10) == 79
    saved = runs.load(run.id, h.env)
    assert saved.preparation is not None
    assert saved.preparation["state"] == "interrupted"
    assert prepare.start_ticks(saved.preparation["pid"]) is None
    assert saved.turn == 0
    assert record.read_events(h.id, h.env)[-1]["exit"] == 79


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
    folder = record.folder(h.id, h.env) / "briefs"
    review = next(folder.glob("b-review-*.md")).read_text()
    assert "## Merged sibling tasks" not in review


def test_review_lists_merged_siblings_absent_from_task_branch(h: Harness) -> None:
    h.scripts_update(**{"Review of task a, round 1,": "@reply " + FIX})
    h.create([task("a"), task("b"), task("c", after=["a"])], rounds=0, parallel=1)
    flow = h.run()
    a, b, c = flow["tasks"]
    assert [item["status"] for item in (a, b, c)] == ["stopped", "merged", "blocked"]
    folder = record.folder(h.id, h.env) / "briefs"
    first = next(folder.glob("a-review-*.md"))
    assert "## Merged sibling tasks" not in first.read_text()
    h.scripts_update(**{"Review of task a, round 1,": "@reply " + PASS})
    h.answer("review")
    assert h.run()["status"] == "done"
    review = next(path for path in folder.glob("a-review-*.md") if path != first)
    text = review.read_text()
    section = text.split("## Merged sibling tasks\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert f"    b: {b['subject']}\n    commit: {b['merge_commit']}" in section
    assert "on the flow branch and absent from the task branch" in section
    assert f"a: {a['subject']}" not in section
    assert f"c: {c['subject']}" not in section
    for path in (flow["worktree"], a["worktree"], str(h.repo)):
        assert path not in text


@pytest.mark.parametrize("commit", [None, "missing"])
def test_merged_sibling_without_recorded_commit_is_omitted(
    h: Harness, commit: str | None
) -> None:
    h.create([task("a"), task("b")])
    instance = h.instance()
    a, b = instance.flow["tasks"]
    a["head"] = instance.flow["base"]
    b.update(status="merged", merge_commit=commit)
    if commit == "missing":
        del b["merge_commit"]
    assert instance.merged_siblings(a) == []


def test_merged_sibling_unreadable_commit_is_not_reported_absent(h: Harness) -> None:
    h.create([task("a"), task("b")])
    instance = h.instance()
    a, b = instance.flow["tasks"]
    a["head"] = instance.flow["base"]
    b.update(status="merged", merge_commit="0" * 40)
    with pytest.raises(runner.RunnerError, match="git merge-base"):
        instance.merged_siblings(a)


@pytest.mark.parametrize("check", [[], [sys.executable, "-c", "print('plan checked')"]])
def test_fix_round_has_previous_head_and_real_plan_briefs(
    h: Harness, check: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[tuple[str, str, str]] = []
    send = h.driver.send

    def inspect(run: runs.Run, text: str) -> None:
        if run.read_only and run.turn == 2:
            tree = Path(run.worktree)
            assert git(tree, "rev-parse", "--abbrev-ref", "HEAD", env=h.env) == "HEAD"
            read.append(
                (
                    run.id,
                    git(tree, "rev-parse", "HEAD", env=h.env),
                    (tree / "a").read_text(),
                )
            )
        send(run, text)

    monkeypatch.setattr(h.driver, "send", inspect)
    h.scripts_update(
        **{
            "Review of task a, round 1,": "@reply " + FIX,
            "Fix round 1 of task a": "@write a fixed",
        }
    )
    options: dict[str, Any] = {"check": check} if check else {}
    h.create(rounds=1, read=[str(h.scripts)], **options)
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert item["round"] == 1
    assert len(item["reviews"]) == 2
    first, second = item["reviews"]
    assert second["run"] == first["run"]
    assert [first["turn"], second["turn"]] == [1, 2]
    assert [first["head"], second["head"]] == [item["fix_base"], item["head"]]
    assert read == [(second["run"], item["head"], "fixed\n")]
    reviewer = runs.load(second["run"], h.env)
    assert reviewer.base == first["head"] and reviewer.expected_head == second["head"]
    assert reviewer.status == runs.CLOSED and not Path(reviewer.worktree).exists()
    folder = record.folder(h.id, h.env) / "briefs"
    fix = next(folder.glob("a-fix-*.md")).read_text()
    assert fix.startswith(briefs.FIX_FIRST.format(round=1, task="a", flow=h.id))
    if check:
        assert f"Run the plan's check command `{shlex.join(check)}`" in fix
        assert "or say why you could not run it" in fix
    else:
        assert "Run the tests for the files you change and quick static checks." in fix
    second = sorted(folder.glob("a-review-*.md"))[-1].read_text()
    assert "1. a: wrong value" in second
    assert f"git diff {item['fix_base']} {item['head']}" in second
    assert not any(line.startswith("@") for line in second.splitlines())
    for review in item["reviews"]:
        assert runs.load(review["run"], h.env).read == (str(h.scripts),)


@pytest.mark.parametrize("rounds", [0, 1])
def test_clean_prepared_reviewer_is_removed_on_acceptance(
    h: Harness, monkeypatch: pytest.MonkeyPatch, rounds: int
) -> None:
    (h.repo / ".gitignore").write_text(".venv/\n")
    git(h.repo, "add", ".gitignore", env=h.env)
    git(h.repo, "commit", "-m", "Ignore dependencies", env=h.env)
    h.create(rounds=rounds)
    choices.set_settings(
        {
            "worktree.prepare": {
                str(h.repo): (
                    "if ! git symbolic-ref -q HEAD >/dev/null; then "
                    "/bin/mkdir .venv && echo prepared > .venv/marker; fi"
                )
            }
        },
        env=h.env,
    )
    if rounds:
        h.scripts_update(
            **{
                "Review of task a, round 1,": "@reply " + FIX,
                "Fix round 1 of task a": "@write a fixed",
            }
        )
    checked: list[str] = []
    close = runner._Runner.close

    def check_before_close(instance: runner._Runner, run_id: str | None) -> str | None:
        if run_id:
            run = runs.load(run_id, h.env)
            if run.read_only:
                tree = Path(run.worktree)
                assert (tree / ".venv" / "marker").read_text() == "prepared\n"
                assert git(tree, "rev-parse", "HEAD", env=h.env) == (
                    run.expected_head or run.base
                )
                assert worktrees.remove_detached(run, dry_run=True)
                assert tree.exists()
                checked.append(run_id)
        return close(instance, run_id)

    monkeypatch.setattr(runner._Runner, "close", check_before_close)
    item = h.run()["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    first, last = item["reviews"][0], item["reviews"][-1]
    assert first["run"] == last["run"] and last["turn"] == rounds + 1
    reviewer = runs.load(last["run"], h.env)
    assert checked == [reviewer.id]
    assert reviewer.base == first["head"]
    assert reviewer.expected_head == (last["head"] if rounds else None)
    assert not reviewer.preserve_ignored
    assert reviewer.status == runs.CLOSED and not Path(reviewer.worktree).exists()


def test_explicit_review_after_automatic_fixes_starts_fresh(h: Harness) -> None:
    h.scripts_update(
        **{
            "a read-only review;": "@reply " + FIX,
            "Fix round 1 of task a": "@write a fixed",
        }
    )
    h.create(rounds=1)
    stopped = h.run()["tasks"][0]
    first, second = stopped["reviews"]
    assert first["run"] == second["run"] and second["turn"] == 2
    assert stopped["status"] == "stopped"

    h.scripts_update(**{"a read-only review;": "@reply " + PASS})
    h.answer("review")
    accepted = h.run()["tasks"][0]
    assert accepted["status"] == "merged"
    assert accepted["reviews"][:2] == stopped["reviews"]
    fresh = accepted["reviews"][2]
    assert fresh["run"] != second["run"]
    assert (fresh["review_round"], fresh["turn"], fresh["head"]) == (
        1,
        1,
        second["head"],
    )
    assert (
        "## Earlier findings"
        not in (runs.load(fresh["run"], h.env).folder / runs.BRIEF_NAME).read_text()
    )


@pytest.mark.parametrize(
    "problem",
    ["dirty", "ignored file", "ignored directory", "moved", "locked", "closed"],
)
def test_automatic_review_fallback_keeps_the_previous_worktree(
    h: Harness, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    (h.repo / ".gitignore").write_text("collision\n")
    git(h.repo, "add", ".gitignore", env=h.env)
    git(h.repo, "commit", "-m", "Ignore collision", env=h.env)
    fix = "@write a fixed"
    reviewed_file = ""
    if problem.startswith("ignored"):
        reviewed_file = (
            "collision/local" if problem == "ignored directory" else "collision"
        )
        fix += "\n@write collision fixed\n@git add -f collision"
    elif problem == "dirty":
        reviewed_file = "a"
    first = (
        (f"@write {reviewed_file} local\n" if reviewed_file else "") + "@reply " + FIX
    )
    h.scripts_update(
        **{
            "Review of task a, round 1,": first,
            "Fix round 1 of task a": fix,
        }
    )
    h.create(rounds=1, tasks=[task("a", files=["a", "collision"])])
    start = runner._Runner.continue_review

    def disrupt(instance: runner._Runner, item: dict[str, Any]) -> bool:
        old = runs.load(item["reviewer_run"], h.env)
        if problem == "moved":
            git(Path(old.worktree), "checkout", "--detach", "HEAD^", env=h.env)
        if problem == "locked":
            git(h.repo, "worktree", "lock", old.worktree, env=h.env)
        if problem == "closed":
            assert instance.close(old.id) is None
        return start(instance, item)

    monkeypatch.setattr(runner._Runner, "continue_review", disrupt)
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    previous, fresh = item["reviews"]
    assert fresh["run"] != previous["run"] and previous["turn"] == fresh["turn"] == 1
    old = runs.load(previous["run"], h.env)
    new = runs.load(fresh["run"], h.env)
    assert old.status == new.status == runs.CLOSED
    assert fresh["head"] == new.base == item["head"]
    assert not Path(new.worktree).exists()
    if reviewed_file:
        assert (Path(old.worktree) / reviewed_file).read_text() == "local\n"
    elif problem == "moved":
        assert (
            git(Path(old.worktree), "rev-parse", "HEAD", env=h.env) != previous["head"]
        )
    elif problem == "locked":
        assert Path(old.worktree).is_dir()
    if problem.startswith("ignored"):
        assert old.preserve_ignored and not new.preserve_ignored
        assert h.instance().close(old.id) is None
        for dry_run in (True, False):
            result = prune.remove(0, [h.id], env=h.env, dry_run=dry_run)
            assert any(
                old.id in error and old.worktree in error for error in result.errors
            )
            assert runs.load(old.id, h.env).worktree == old.worktree
            assert old.worktree in git(h.repo, "worktree", "list", env=h.env)
            assert (Path(old.worktree) / reviewed_file).read_text() == "local\n"


class _RunnerStopped(BaseException):
    """Simulate runner loss without saving a task failure."""


@pytest.mark.parametrize("fallback", ["dead", "missing", "closed", "legacy", "resume"])
def test_automatic_review_keeps_ignored_evidence_without_advancing(
    h: Harness, monkeypatch: pytest.MonkeyPatch, fallback: str
) -> None:
    (h.repo / ".gitignore").write_text("report.json\n")
    git(h.repo, "add", ".gitignore", env=h.env)
    git(h.repo, "commit", "-m", "Ignore review reports", env=h.env)
    local, fixed = '{"origin":"local"}\n', '{"origin":"fixed"}\n'
    h.scripts_update(
        **{
            "Review of task a, round 1,": f"@write report.json {local}@reply {FIX}",
            "Fix round 1 of task a": f"@write report.json {fixed}"
            "@git add -f report.json",
        }
    )
    h.create(tasks=[task("a", files=["a", "report.json"])], rounds=1)
    original_artifact = runner._Runner.review_artifact
    original_command = h.driver.command
    trees: list[tuple[str, str, str]] = []

    def disrupt(instance: runner._Runner, item: dict[str, Any]) -> str:
        if item["round"] == 1:
            old = runs.load(item["reviewer_run"], h.env)
            if fallback == "missing":
                tmux.kill(old.id, env=h.env)
            elif fallback == "legacy":
                # A retained reviewer with a verdict predating turn attribution.
                item["reviews"][-1].pop("turn")
            else:
                pid = tmux.pane_pid(old.id, env=h.env)
                assert pid is not None
                os.killpg(pid, signal.SIGTERM)
                _wait_for(lambda: tmux.pane_dead(old.id, env=h.env))
                if fallback == "closed":
                    # Close saved its status before removing the worktree.
                    runs.update(old, {"status": runs.CLOSED, "ended_at": time.time()})
        return original_artifact(instance, item)

    def inspect_checkout(run: runs.Run, message: str) -> LaunchSpec:
        if run.read_only and (report := Path(run.worktree) / "report.json").exists():
            sha = git(Path(run.worktree), "rev-parse", "HEAD", env=h.env)
            trees.append((run.id, sha, report.read_text()))
        return original_command(run, message)

    monkeypatch.setattr(h.driver, "command", inspect_checkout)
    with monkeypatch.context() as patch:
        patch.setattr(runner._Runner, "review_artifact", disrupt)
        if fallback == "resume":

            def stop_before_close(
                instance: runner._Runner, run_id: str | None
            ) -> str | None:
                assert run_id is not None and runs.load(run_id, h.env).preserve_ignored
                raise _RunnerStopped

            patch.setattr(runner._Runner, "close", stop_before_close)
            instance = h.instance()
            instance.begin()
            with pytest.raises(_RunnerStopped):
                instance.pipeline(instance.flow["tasks"][0])
        else:
            h.run()
    if fallback == "resume":
        interrupted = h.flow()["tasks"][0]
        assert interrupted["review_send"]["head"] == interrupted["head"]
        assert h.instance().close(interrupted["reviewer_run"]) is None

    item = (h.run() if fallback == "resume" else h.flow())["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    previous, fresh = item["reviews"]
    assert previous["head"] != fresh["head"] == item["head"]
    assert previous["run"] != fresh["run"]
    assert (fresh["verdict"], fresh["turn"]) == ("pass", 1)
    assert trees == [(fresh["run"], item["head"], fixed)]
    retired = runs.load(previous["run"], h.env)
    assert retired.status == runs.CLOSED and retired.preserve_ignored
    report = Path(retired.worktree) / "report.json"
    assert report.read_text() == local
    assert h.instance().close(retired.id) is None
    for dry_run in (True, False):
        result = prune.remove(0, [h.id], env=h.env, dry_run=dry_run)
        assert any(
            retired.id in error and retired.worktree in error for error in result.errors
        )
        assert runs.load(retired.id, h.env).worktree == retired.worktree
        assert retired.worktree in git(h.repo, "worktree", "list", env=h.env)
        assert report.read_text() == local


def test_saved_fix_decision_starts_a_follow_up_on_resume(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.scripts_update(
        **{
            "Review of task a, round 1,": "@reply " + FIX,
            "Fix round 1 of task a": "@write a fixed",
        }
    )
    h.create(rounds=1)

    def stop_before_fix(
        instance: runner._Runner, item: dict[str, Any], path: str, *, fresh: bool
    ) -> bool:
        raise _RunnerStopped

    with monkeypatch.context() as patch:
        patch.setattr(runner._Runner, "send", stop_before_fix)
        instance = h.instance()
        instance.begin()
        with pytest.raises(_RunnerStopped):
            instance.pipeline(instance.flow["tasks"][0])
    interrupted = h.flow()["tasks"][0]
    assert (
        interrupted["status"] == "reviewing" and interrupted["review_action"] == "fix"
    )
    (first,) = interrupted["reviews"]

    item = h.run()["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    assert item["reviews"][0] == first
    assert [(r["verdict"], r["head"], r["turn"]) for r in item["reviews"]] == [
        ("fix", interrupted["head"], 1),
        ("pass", item["head"], 2),
    ]
    assert item["head"] != interrupted["head"]
    assert [r["run"] for r in item["reviews"]] == [first["run"]] * 2


@pytest.mark.parametrize("boundary", ["advanced", "sent", "collected", "closed"])
def test_automatic_review_recovers_one_turn_at_the_send_boundary(
    h: Harness, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    h.scripts_update(
        **{
            "Review of task a, round 1,": "@reply " + FIX,
            "Fix round 1 of task a": "@write a fixed",
        }
    )
    h.create(rounds=1)
    send = h.driver.send
    delivered: list[str] = []

    def track(run: runs.Run, text: str) -> None:
        if run.read_only and run.turn == 2:
            delivered.append(run.id)
        send(run, text)

    monkeypatch.setattr(h.driver, "send", track)
    with monkeypatch.context() as patch:
        original_advance, original_turn = Core._advance_review, Core._turn
        original_send = Core.send

        def advanced(core: Core, run: runs.Run, review: ReviewFollowUp) -> runs.Run:
            updated = original_advance(core, run, review)
            if boundary == "advanced":
                raise _RunnerStopped
            return updated

        def sent(
            core: Core,
            run: runs.Run,
            driver: Driver,
            started: float,
            window: str | None,
        ) -> int:
            if boundary == "sent" and run.read_only and run.turn == 2:
                raise _RunnerStopped
            return original_turn(core, run, driver, started, window)

        def collected(
            core: Core, run_id: str, path: str, *, review: ReviewFollowUp | None = None
        ) -> int:
            result = original_send(core, run_id, path, review=review)
            if review and boundary in ("collected", "closed"):
                if boundary == "closed":
                    assert core.close(run_id) == 0
                raise _RunnerStopped
            return result

        patch.setattr(Core, "_advance_review", advanced)
        patch.setattr(Core, "_turn", sent)
        patch.setattr(Core, "send", collected)
        instance = h.instance()
        instance.begin()
        item = instance.flow["tasks"][0]
        try:
            instance.pipeline(item)
        except _RunnerStopped:
            pass
        else:
            pytest.fail(f"the runner reached {item['status']}: {item['reason']}")

    interrupted = h.flow()["tasks"][0]
    run_id = interrupted["reviewer_run"]
    assert interrupted["reviews"][0]["run"] == run_id
    assert runs.load(run_id, h.env).turn == (1 if boundary == "advanced" else 2)
    result = h.run()["tasks"][0]
    assert result["status"] == "merged", result["reason"]
    assert [(r["run"], r["turn"]) for r in result["reviews"]] == [
        (run_id, 1),
        (run_id, 2),
    ]
    assert delivered == [run_id]


@pytest.mark.parametrize(
    "obstacle", ["foreign input", "owner wait", "uncertain delivery"]
)
def test_automatic_review_stops_for_owner_input_or_uncertain_delivery(
    h: Harness, monkeypatch: pytest.MonkeyPatch, obstacle: str
) -> None:
    h.scripts_update(
        **{
            "Review of task a, round 1,": "@reply " + FIX,
            "Fix round 1 of task a": "@write a fixed",
        }
    )
    h.create(rounds=1)
    send = h.driver.send
    start = runner._Runner.continue_review

    def block(instance: runner._Runner, item: dict[str, Any]) -> bool:
        if obstacle == "owner wait":
            run = runs.load(item["reviewer_run"], h.env)
            runs.update(run, {"owner_wait": ("owner: decide",)})
        return start(instance, item)

    def uncertain(run: runs.Run, text: str) -> None:
        if obstacle == "uncertain delivery" and run.read_only and run.turn == 2:
            raise DeliveryUncertainError("delivery is uncertain")
        send(run, text)

    monkeypatch.setattr(runner._Runner, "continue_review", block)
    monkeypatch.setattr(h.driver, "send", uncertain)
    if obstacle == "foreign input":
        monkeypatch.setattr(
            h.driver,
            "foreign_input",
            lambda run: "owner typed" if run.read_only else None,
        )
    item = h.run()["tasks"][0]
    assert item["status"] == "stopped" and len(item["reviews"]) == 1
    reviewer = runs.load(item["reviewer_run"], h.env)
    assert reviewer.id == item["reviews"][0]["run"] and reviewer.status != runs.CLOSED
    if obstacle == "uncertain delivery":
        assert reviewer.turn == 2 and reviewer.last_variant == "uncertain"
    else:
        assert reviewer.turn == 1 and reviewer.expected_head is None
    assert h.run()["tasks"][0]["reviews"] == item["reviews"]


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
    assert stop["provider_kind"] is None


@pytest.mark.parametrize("latest_should", [False, True])
def test_status_distinguishes_two_reviews(h: Harness, latest_should: bool) -> None:
    retained = [
        {"severity": "should", "problem": "tidy"},
        {"severity": "lead", "problem": "choose format"},
    ]
    latest = (
        [{"severity": "should", "problem": "new suggestion"}] if latest_should else []
    )
    h.scripts_update(
        **{
            "Review of task a, round 1,": "@reply "
            + json.dumps({"verdict": "fix", "findings": [MUST, *retained]}),
            "Fix round 1 of task a": "@write a fixed",
            "Review of task a, round 2,": "@reply "
            + json.dumps({"verdict": "pass", "findings": latest}),
        }
    )
    h.create(rounds=1)
    item = h.run()["tasks"][0]
    assert item["status"] == "merged"
    assert item["round"] == 1
    assert [review["round"] for review in item["reviews"]] == [0, 1]
    first, second = item["reviews"]
    expected = [(1, first["run"]), (1, first["run"])]
    if latest_should:
        expected.append((2, second["run"]))
    assert [(f["round"], f["run"]) for f in item["findings"]] == expected
    assert [f["problem"] for f in item["findings"]] == [
        f["problem"] for f in [*retained, *latest]
    ]
    out, err = io.StringIO(), io.StringIO()
    args = build_parser().parse_args(["flow", "--status", h.id, "--json"])
    assert command.main(args, h.env, out=out, err=err) == 0, err.getvalue()
    reported = json.loads(out.getvalue())["tasks"][0]
    assert reported["findings"] == item["findings"]
    assert reported["latest_review"] == {
        "round": 2,
        "run": second["run"],
        "verdict": "pass",
        "findings": {"must": 0, "should": int(latest_should), "lead": 0},
    }
    watch = command._task_report(h.id, item)
    assert watch["latest_review"] == reported["latest_review"]
    assert watch["findings"] == {"must": 0, "should": 1 + int(latest_should), "lead": 1}


@pytest.mark.parametrize(
    "source", ["plan.rounds", "flow.review_rounds", "default", "unknown"]
)
def test_exhausted_fix_budget(h: Harness, source: str) -> None:
    h.script_data = {
        "a read-only review;": "@reply " + FIX,
        "Fix round 1 of task a": "@write a fixed",
    }
    h.scripts_update()
    h.create(rounds=1)
    flow = h.flow()
    flow["review_budget"] = {
        "value": 2 if source == "flow.review_rounds" else 1,
        "unit": "reviews" if source == "flow.review_rounds" else "fix_rounds",
        "source": source,
    }
    record.save(flow, h.env)
    item = h.run()["tasks"][0]
    assert (
        item["reason"]
        == {
            "plan.rounds": "unresolved after 1 fix round (plan rounds 1)",
            "flow.review_rounds": "unresolved after 2 reviews "
            "(flow.review_rounds 2 allows 1 fix round)",
            "default": "unresolved after 1 fix round (default 1 fix round)",
            "unknown": "unresolved after 1 fix round "
            "(budget 1 fix round; source unknown)",
        }[source]
    )
    assert len(item["reviews"]) == 2
    assert item["round"] == 1


def test_one_review_setting_stops_without_a_fix(h: Harness) -> None:
    h.scripts_update(**{"a read-only review;": "@reply " + FIX})
    h.create(rounds=0)
    flow = h.flow()
    flow["review_budget"] = {
        "value": 1,
        "unit": "reviews",
        "source": "flow.review_rounds",
    }
    record.save(flow, h.env)
    choices.set_settings({"flow.review_rounds": "4"}, env=h.env)
    item = h.run()["tasks"][0]
    assert item["reason"] == (
        "unresolved after 1 review (flow.review_rounds 1 allows no fix round)"
    )
    assert item["round"] == 0 and len(item["reviews"]) == 1
    assert not list((record.folder(h.id, h.env) / "briefs").glob("a-fix-*.md"))


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
    if item["reviews"]:
        reviewer = runs.load(item["reviews"][0]["run"], h.env)
        assert item["reviewer_run"] is None
        assert reviewer.status == runs.CLOSED and not Path(reviewer.worktree).exists()


def shared_reply() -> str:
    value = json.loads(PASS)
    value["outside_files"] = [
        {"file": "b", "needed": True, "reason": "The task needs this edit"}
    ]
    return json.dumps(value)


def _review_brief(h: Harness, name: str) -> str:
    folder = record.folder(h.id, h.env) / "briefs"
    return sorted(folder.glob(f"{name}-review-*.md"))[-1].read_text()


def test_same_wave_declared_file_is_named_in_the_review_brief(h: Harness) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + shared_reply()})
    h.create(
        [
            task("a", text="@write b wrong"),
            task("b", text="@write c value", files=["b", "c"]),
        ]
    )
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged" and len(item["reviews"]) == 1
    assert item["outside_files"] == ["b"]
    assert "    b (task b)" in _review_brief(h, "a")
    assert flow["tasks"][1]["status"] == "merged"
    assert "another task in this wave declares" not in _review_brief(h, "b")


def test_both_writers_change_a_declared_file_and_the_later_merge_conflicts(
    h: Harness,
) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + shared_reply()})
    h.create([task("a", text="@write b first"), task("b", text="@write b second")])
    first, second = h.run()["tasks"]
    assert first["status"] == "merged" and len(first["reviews"]) == 1
    assert "    b (task b)" in _review_brief(h, "a")
    assert second["status"] == "conflict"


def test_outside_file_and_same_wave_merge_conflict(h: Harness) -> None:
    h.scripts_update(**{"a read-only review;": "@reply " + outside_reply(PASS)})
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


@pytest.mark.parametrize("accounted", [False, True])
def test_outside_file_review_is_required_and_saved(h: Harness, accounted: bool) -> None:
    entries = [
        {"file": "README.md", "needed": True, "reason": "Update the interface"},
        {"file": "helper.py", "needed": False, "reason": "Unrelated change"},
    ]
    if accounted:
        reply = json.dumps(
            {
                "verdict": "pass",
                "findings": [],
                "outside_files": entries[::-1],
            }
        )
        h.scripts_update(**{"a read-only review;": "@reply " + reply})
    h.create([task("a", text="@write README.md updated\n@write helper.py value")])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["outside_files"] == ["README.md", "helper.py"]
    review = item["reviews"][-1]
    if accounted:
        assert flow["status"] == "done" and item["status"] == "merged"
        assert review["outside_files"] == entries
        out, err = io.StringIO(), io.StringIO()
        args = build_parser().parse_args(["flow", "--status", h.id, "--json"])
        assert command.main(args, h.env, out=out, err=err) == 0
        assert json.loads(out.getvalue())["tasks"][0]["reviews"][-1] == review
    else:
        assert item["status"] == "stopped" and review["verdict"] == "unreadable"
        assert "the review is unreadable" in item["reason"]
        assert "README.md (missing)" in item["reason"]
        assert "helper.py (missing)" in item["reason"]
        assert review["outside_files"] == []


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


def test_lead_stop_keeps_live_writer_with_flow(
    h: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    h.scripts_update(**{"Review of task a, round 1,": "@reply " + FIX})
    h.create(rounds=0)
    flow = h.run()
    assert flow["status"] == "stopped"
    assert flow["reason"] == "waiting for the lead: a"
    writer = runs.load(flow["tasks"][0]["writer_run"], h.env)
    assert writer.status == runs.WAITING
    assert writer.parent is not None
    # The runner has returned; model its process exiting with a stale start time.
    writer = runs.update(
        writer, {"parent": {**writer.parent, "start": writer.parent["start"] + 1}}
    )
    assert not runs.orphaned(writer)
    assert runs.waiting_for_flow(writer) == h.id
    core = h.factory(h.env)
    capsys.readouterr()
    for single in (True, False):
        for as_json in (True, False):
            assert core.status(writer.id if single else None, as_json=as_json) == 0
            out, err = capsys.readouterr()
            waiting = f"waiting for the lead's answer to flow {h.id}"
            assert f"WARNING: run {writer.id} is {waiting}" in err
            assert "next:" not in err and "orphaned" not in err
            if as_json:
                data = json.loads(out)
                if not single:
                    data = next(item for item in data if item["id"] == writer.id)
                assert data["status"] == runs.WAITING
                assert data["session_state"] == "live"
                assert data["orphaned"] is False
                assert data["waiting_for_flow"] == h.id
            else:
                assert waiting in out
    assert runs.load(writer.id, h.env) == writer
    h.scripts_update(**{"Review of task a, round 1,": "@reply " + PASS})
    h.answer("review")
    resumed = h.run()
    assert resumed["status"] == "done"
    assert resumed["tasks"][0]["writer_run"] == writer.id


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
    temporary = paths.RUN_TMP_ROOT / f"ember-{h.id}"
    temporary.mkdir()
    (temporary / "stale").touch()
    h.create(
        [task("a"), task("b", after=["a"])],
        check_base=True,
        check=[
            sys.executable,
            "-c",
            "import os,pathlib; assert 'GIT_DIR' not in os.environ; "
            "assert 'EMBER_FLOW_RUNNER' not in os.environ; "
            "assert os.environ['INHERITED'] == 'kept'; "
            "folder = pathlib.Path(os.environ['TMPDIR']); "
            "assert folder.is_dir() and not list(folder.iterdir()); "
            "(folder / 'file').touch(); print(folder); "
            "pathlib.Path('leftover').write_text('x')",
        ],
    )
    h.env.update(
        GIT_DIR="wrong", EMBER_FLOW_RUNNER=h.id, TMPDIR="old", INHERITED="kept"
    )
    flow = h.run()
    assert flow["status"] == "done"
    assert [check["wave"] for check in flow["checks"]] == [0, 1, 2]
    assert all(
        Path(c["log"]).read_text().strip() == str(temporary) for c in flow["checks"]
    )
    assert not list(paths.RUN_TMP_ROOT.iterdir())
    assert not (Path(flow["worktree"]) / "leftover").exists()


@pytest.mark.parametrize("acceptance", ["review", "lead", "recovery"])
def test_acceptance_cleans_the_open_writer_tmp(
    h: Harness, monkeypatch: pytest.MonkeyPatch, acceptance: str
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=h.env)
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    monkeypatch.setattr(
        h.driver, "foreign_input", lambda run: None if run.read_only else "owner typed"
    )
    kept = {"config", "data", "state", "codex-session"}

    def launch(core: Core, spec: LaunchSpec, run: runs.Run, common: Path) -> LaunchSpec:
        if not run.read_only:
            folder = paths.run_tmp_dir(run.id)
            for name in kept:
                (folder / name).mkdir(exist_ok=True)
            (folder / "leftover").touch()
        return spec

    monkeypatch.setattr(Core, "_sandboxed", launch)
    if acceptance != "review":
        h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    h.create(rounds=0)
    flow = h.run()
    item = flow["tasks"][0]
    folder = paths.run_tmp_dir(item["writer_run"])
    if acceptance != "review":
        assert item["status"] == "stopped"
        assert (folder / "leftover").exists()
        if acceptance == "lead":
            h.answer("accept", commit=item["head"])
        else:
            item.update(status="merging", merge_tip=item["head"])
            record.save(flow, h.env)
        item = h.run()["tasks"][0]
    assert item["status"] == "merged"
    assert {entry.name for entry in folder.iterdir()} == kept
    followup = h.tmp / "followup.md"
    followup.write_text("@write a later")
    core = h.factory(h.env)
    assert core.send(item["writer_run"], str(followup), force=True) == 0
    assert runs.load(item["writer_run"], h.env).turn == 2


@pytest.mark.parametrize("missing", [False, True])
def test_writer_tmp_removal_error_warns_and_flow_continues(
    h: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    h.create(rounds=0)
    task = h.run()["tasks"][0]
    writer = runs.load(task["writer_run"], h.env)
    runs.save(replace(writer, sandbox=sandbox.CODEX))
    if not missing:

        def denied(run_id: str, keep: list[str], env: Mapping[str, str]) -> None:
            raise PermissionError("cannot unlink blocked")

        monkeypatch.setattr(paths, "clean_run_tmp_dir", denied)
    h.answer("accept", commit=task["head"])
    assert h.run()["status"] == "done"
    warnings = capsys.readouterr().err.count(
        f"WARNING: cannot clean writer {writer.id}"
    )
    assert warnings == (0 if missing else 1)


def test_check_refuses_symlink_at_its_temporary_path(h: Harness) -> None:
    h.create(check_base=True, check=["true"])
    folder = paths.check_tmp_dir(h.id)
    outside = h.tmp / "outside"
    outside.mkdir()
    (outside / "saved").touch()
    folder.symlink_to(outside)
    flow = h.run()
    assert flow["status"] == "failed" and flow["checks"][0]["exit"] != 0
    assert str(folder) in flow["reason"]
    assert folder.is_symlink() and (outside / "saved").is_file()


def test_failed_check_identity_save_reaps_group_before_removing_tmp(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = h.tmp / "ready"
    h.create(
        check_base=True,
        check=[
            sys.executable,
            "-c",
            "import os, pathlib, time\n"
            "child = os.fork()\n"
            f"if child: pathlib.Path({str(ready)!r}).write_text(str(child))\n"
            "time.sleep(60)",
        ],
    )
    instance = h.instance()
    save = instance._save
    group: list[processes.Process] = []

    def fail_identity() -> None:
        if identity := instance.flow.get("active_check"):
            _wait_for(lambda: ready.exists() and ready.read_text())
            group.extend(processes.tree(identity["pid"]))
            assert {pid for pid, _ in group} == {
                identity["pid"],
                int(ready.read_text()),
            }
            raise OSError("cannot save process identity")
        save()

    monkeypatch.setattr(instance, "_save", fail_identity)
    remove = paths.remove_check_tmp_dir

    def inspect(flow_id: str) -> bool:
        leader = instance.flow["active_check"]["pid"]
        with pytest.raises(ChildProcessError):
            os.waitpid(leader, os.WNOHANG)
        _wait_for(lambda: not any(processes.running(process) for process in group))
        return remove(flow_id)

    monkeypatch.setattr(paths, "remove_check_tmp_dir", inspect)
    assert not instance.check()
    assert not paths.check_tmp_dir(h.id).exists()
    saved = h.flow()
    assert saved["active_check"] is None
    assert "cannot save process identity" in Path(saved["checks"][0]["log"]).read_text()


@pytest.mark.parametrize("exit_code", [0, 3])
def test_check_folder_removal_error_keeps_check_exit_and_log(
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
) -> None:
    h.create(
        check=[sys.executable, "-c", f"print('checked'); raise SystemExit({exit_code})"]
    )

    def denied(flow_id: str) -> bool:
        raise PermissionError("removal denied")

    monkeypatch.setattr(paths, "remove_check_tmp_dir", denied)
    flow = h.run()
    assert flow["status"] == ("failed" if exit_code else "done")
    (check,) = flow["checks"]
    assert check["exit"] == exit_code
    assert Path(check["log"]).read_text() == "checked\n"
    warnings = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "removal denied" in line
    ]
    folder = paths.check_tmp_dir(h.id)
    assert warnings == [f"WARNING: cannot remove check folder {folder}: removal denied"]


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


@pytest.mark.parametrize("check", [[], [sys.executable, "-c", "print('plan checked')"]])
@pytest.mark.parametrize("writer_check", [None, [], [["pytest", "some tests"]]])
def test_send_correction_carries_record_check(
    h: Harness, check: list[str], writer_check: list[list[str]] | None
) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    options: dict[str, Any] = {"check": check} if check else {}
    if writer_check is not None:
        options["writer_check"] = writer_check
    h.create(rounds=0, **options)
    folder = record.folder(h.id, h.env)
    saved_plan = folder / record.PLAN_NAME
    document = json.loads(saved_plan.read_text())
    document["tasks"][0]["writer_check"] = [["other-check"]]
    saved_plan.write_text(json.dumps(document))
    assert h.run()["tasks"][0]["status"] == "stopped"
    if check:
        check = [*check, ""]
        flow = h.flow()
        flow["check"] = check
        record.save(flow, h.env)
    correction = h.tmp / "correction.md"
    correction.write_text("@write a corrected")
    h.scripts_update(**{"Review of task a,": "@reply " + PASS})
    h.answer("send", file=str(correction))
    assert h.run()["status"] == "done"
    artifacts = folder / "briefs"
    message = next(artifacts.glob("a-fix-*.md")).read_text()
    assert "\n\n@write a corrected\n\n## Checks" in message
    assert "other-check" not in message
    if writer_check is not None:
        instruction = (
            "    pytest 'some tests'"
            if writer_check
            else "Run no writer check command."
        )
        assert instruction in message
        assert instruction in next(artifacts.glob("a-write-*.md")).read_text()
        for artifact in artifacts.glob("a-review-*.md"):
            review = artifact.read_text()
            assert "other-check" not in review
            assert (
                "    pytest 'some tests'"
                if writer_check
                else "The writer was told to run no check command."
            ) in review
        if check:
            assert (
                f"Do not run the flow's check command `{shlex.join(check)}`" in message
            )
    elif check:
        assert f"Run the plan's check command `{shlex.join(check)}`" in message
        assert "or say why you could not run it" in message
    else:
        assert (
            "Run the tests for the files you change and quick static checks." in message
        )


@pytest.mark.parametrize("final_answer", ["review", "worker"])
def test_reviewer_corrections_survive_resume_and_later_answers(
    h: Harness, final_answer: str
) -> None:
    """Applied corrections reach only this task's later reviews in answer order."""
    h.scripts_update(**{"Review of task a,": "@reply " + outside_reply(FIX)})
    h.create(
        [task("a", text="@write a value\n@write README.md expanded"), task("b")],
        rounds=0,
    )
    assert h.run()["tasks"][0]["status"] == "stopped"
    folder = record.folder(h.id, h.env)
    original_reviews = set((folder / "briefs").glob("*-review-*.md"))
    assert all(
        "## Lead corrections" not in path.read_text() for path in original_reviews
    )
    source = h.tmp / "correction.md"
    first = "README.md is allowed.\n@write a first"
    second = "Keep the interface.\n@write a second"
    for number, correction in ((1, first), (3, second)):
        source.write_text(correction)
        h.answer("send", file=str(source))
        source.write_text("The source was changed after sending.")
        instance = h.instance()
        instance.answers()
        # A fresh runner resumes the already saved answer without appending it again.
        flow = h.run()
        item = flow["tasks"][0]
        assert item["status"] == "stopped"
        expected = [str(folder / "answers" / f"{n}.md") for n in (1, 3) if n <= number]
        assert item["corrections"] == expected
        assert flow["tasks"][1]["corrections"] == []
        if number == 1:
            h.answer("review")
            assert h.run()["tasks"][0]["corrections"] == expected
    h.scripts_update(**{"Review of task a,": "@reply " + outside_reply(PASS)})
    h.answer(final_answer)
    flow = h.run()
    assert flow["status"] == "done"
    assert flow["tasks"][0]["corrections"] == expected
    reviews = sorted(
        (folder / "briefs").glob("a-review-*.md"),
        key=lambda path: int(path.stem.rsplit("-", 1)[1]),
    )
    assert len(reviews) == 5
    for index, path in enumerate(reviews[1:], 1):
        text = path.read_text()
        assert "### Answer 1\n\n    README.md is allowed.\n    @write a first" in text
        assert "Changed outside the declared files:\n    README.md" in text
        assert "The source was changed" not in text
        assert ("### Answer 3" in text) == (index >= 3)
        if index >= 3:
            assert text.index(first.splitlines()[0]) < text.index(
                second.splitlines()[0]
            )
            assert (
                "### Answer 3\n\n    Keep the interface.\n    @write a second" in text
            )
        assert "### Answer 2" not in text
    if final_answer == "worker":
        for path in (folder / "briefs").glob("a-write-*.md"):
            assert "README.md is allowed." not in path.read_text()


def test_refused_send_does_not_enter_correction_history(h: Harness) -> None:
    """A send refused before the task starts leaves no recorded decision."""
    h.create()
    source = h.tmp / "correction.md"
    source.write_text("Allow README.md")
    h.answer("send", file=str(source))
    instance = h.instance()
    instance.answers()
    flow = h.flow()
    assert flow["answers_seen"] == 1
    assert flow["tasks"][0]["corrections"] == []
    assert "answer 1 refused" in record.read_events(h.id, h.env)[-1]["reason"]


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
    assert "\n\n@write a corrected\n\n\n## Checks\n\n" in sent[0]
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
    assert item["reason"] == "unresolved after 1 fix round (plan rounds 0)"


def test_stale_and_unreadable_answers_advance_cursor(h: Harness) -> None:
    h.create([task("a", text="@write a value\n@reply done")], rounds=0)
    h.script_data = {"a read-only review;": "@reply " + FIX}
    h.scripts_update()
    flow = h.run()
    h.answer("accept", commit=flow["base"])
    with (record.folder(h.id, h.env) / record.ANSWERS_NAME).open("a") as stream:
        stream.write("not json\n")
    flow = h.run()
    assert flow["answers_seen"] == 2
    assert flow["tasks"][0]["status"] == "stopped"
    reasons = [e.get("reason", "") or "" for e in record.read_events(h.id, h.env)]
    assert any("the branch moved" in reason for reason in reasons)
    assert any("unreadable answer line 2" in reason for reason in reasons)
    h.answer("drop")
    flow = h.run()
    assert flow["answers_seen"] == 3
    assert flow["tasks"][0]["status"] == "dropped"


def _stopped_for_the_lead(h: Harness, *tasks: dict[str, Any]) -> dict[str, Any]:
    """Run a flow whose task a stops for the lead after a failed review."""
    h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    h.create([task("a"), *tasks], rounds=0)
    flow = h.run()
    assert flow["tasks"][0]["status"] == "stopped"
    h.script_data = {"a read-only review;": "@reply " + PASS}
    h.scripts_update()
    return flow


def _refusals(h: Harness) -> list[str]:
    return [
        reason
        for event in record.read_events(h.id, h.env)
        if "refused" in (reason := event.get("reason") or "")
    ]


def test_a_later_answer_replaces_the_unapplied_answer_of_its_task(h: Harness) -> None:
    _stopped_for_the_lead(h)
    h.answer("accept", commit="0" * 40)
    h.answer("review")
    h.answer("review", worker="codex:another@low")
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert item["answer"] == 3
    assert item["reviewer"]["model"] == "another"
    assert runs.load(item["reviews"][-1]["run"], h.env).model == "another"
    assert (flow["answers_seen"], flow["answers_taken"]) == (3, [])
    assert _refusals(h) == []


def test_a_withdrawn_answer_is_not_applied(h: Harness) -> None:
    _stopped_for_the_lead(h)
    h.answer("drop")
    h.answer("withdraw")
    flow = h.run()
    assert flow["tasks"][0]["status"] == "stopped"
    assert flow["tasks"][0]["answer"] is None
    assert (flow["status"], flow["answers_seen"]) == ("stopped", 2)
    assert _refusals(h) == []
    h.answer("review")
    assert h.run()["tasks"][0]["status"] == "merged"


def test_a_reviewer_answer_changes_the_saved_reviewer_for_later_reviews(
    h: Harness,
) -> None:
    before = _stopped_for_the_lead(h)
    assert before["reviewer"]["model"] == "gpt-5"
    record.append_answer(
        h.id, {"kind": "reviewer", "worker": "codex:another@low"}, h.env
    )
    h.answer("review")
    flow = h.run()
    item = flow["tasks"][0]
    assert flow["reviewer"]["model"] == "another"
    assert item["reviewer"] is None
    assert runs.load(item["reviews"][-1]["run"], h.env).model == "another"
    assert item["status"] == "merged"
    assert flow["answers_seen"] == 2
    events = [e for e in record.read_events(h.id, h.env) if e["kind"] == "reviewer"]
    assert [(e["answer"], e["worker"]["model"]) for e in events] == [(1, "another")]


def _wait_for(condition: Callable[[], object], seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _hold_wave(h: Harness, *tasks: dict[str, Any]) -> None:
    """Create a flow whose task a stops for the lead while task c keeps writing."""
    h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    h.create([task("a"), *tasks], rounds=0)
    flow = h.flow()
    flow["stall_minutes"] = 5
    record.save(flow, h.env)


def _release(h: Harness, name: str) -> None:
    item = next(t for t in h.flow()["tasks"] if t["id"] == name)
    (runs.load(item["writer_run"], h.env).folder / "stub" / "release").touch()


def _status(h: Harness, name: str) -> str:
    return next(t["status"] for t in h.flow()["tasks"] if t["id"] == name)


def test_a_review_answer_starts_while_the_wave_still_runs(h: Harness) -> None:
    _hold_wave(h, task("c", text="@write c value\n@hang"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        _wait_for(lambda: _status(h, "a") == "stopped")
        assert _status(h, "c") == "writing"
        h.script_data = {"a read-only review;": "@reply " + PASS}
        h.scripts_update()
        h.answer("review")
        _wait_for(lambda: _status(h, "a") == "accepted")
        taken = h.flow()
        assert _status(h, "c") == "writing" and not future.done()
        assert (taken["answers_seen"], taken["answers_taken"]) == (0, [0])
        _release(h, "c")
        flow = future.result(timeout=60)
    assert [t["status"] for t in flow["tasks"]] == ["merged", "merged"]
    assert (flow["answers_seen"], flow["answers_taken"]) == (1, [])
    assert _refusals(h) == []


def test_other_answers_wait_for_the_end_of_the_wave(h: Harness) -> None:
    _hold_wave(h, task("c", text="@write c value\n@hang"))
    correction = h.tmp / "correction.md"
    correction.write_text("@write a corrected")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        _wait_for(lambda: _status(h, "a") == "stopped")
        h.script_data = {"a read-only review;": "@reply " + PASS}
        h.scripts_update()
        h.answer("drop")
        h.answer("send", file=str(correction))
        time.sleep(0.5)
        waiting = h.flow()
        assert _status(h, "a") == "stopped" and not future.done()
        assert (waiting["answers_seen"], waiting.get("answers_taken", [])) == (0, [])
        _release(h, "c")
        flow = future.result(timeout=60)
    assert [t["status"] for t in flow["tasks"]] == ["merged", "merged"]
    assert flow["tasks"][0]["answer"] == 2
    assert flow["answers_seen"] == 2
    assert _refusals(h) == []


def test_a_review_answer_waits_while_a_running_task_holds_its_files(
    h: Harness,
) -> None:
    _hold_wave(h, task("c", text="@write c value\n@hang", files=["a", "c"]))
    assert h.flow()["plan_waves"] == [["a"], ["c"]]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        _wait_for(lambda: _status(h, "c") == "writing")
        assert _status(h, "a") == "stopped"
        h.script_data = {"a read-only review;": "@reply " + PASS}
        h.scripts_update()
        h.answer("review")
        time.sleep(0.5)
        waiting = h.flow()
        assert _status(h, "a") == "stopped" and not future.done()
        assert (waiting["answers_seen"], waiting.get("answers_taken", [])) == (0, [])
        _release(h, "c")
        flow = future.result(timeout=60)
    assert flow["tasks"][0]["status"] == "merged"
    assert flow["answers_seen"] == 1


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


def test_lead_accept_after_stall_preserves_active_writer_files(h: Harness) -> None:
    h.create([task("a", text="@write a value\n@hang")])
    item = h.run()["tasks"][0]
    writer = runs.load(item["writer_run"], h.env)
    assert "stalled" in item["reason"] and writer.turn_state == runs.SENT
    runs.save(replace(writer, sandbox=sandbox.CODEX))
    folder = paths.ensure_run_tmp_dir(writer.id, h.env)
    (folder / "active-test").touch()
    instance = h.instance()
    h.answer("accept", commit=instance.tip(item["branch"]))
    instance.answers()
    assert h.flow()["tasks"][0]["status"] == "accepted"
    assert h.run()["tasks"][0]["status"] == "merged"
    assert runs.load(writer.id, h.env).turn_state == runs.SENT
    assert (folder / "active-test").exists()


def test_lead_accept_cleans_leftovers_of_a_later_refused_turn(h: Harness) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    h.create(rounds=0)
    item = h.run()["tasks"][0]
    assert item["status"] == "stopped"
    writer = runs.load(item["writer_run"], h.env)
    runs.save(replace(writer, sandbox=sandbox.CODEX))
    hook = h.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    correction = h.tmp / "fix.md"
    correction.write_text("@write a correction")
    core = h.factory(h.env)
    assert core.execute(lambda: core.send(writer.id, str(correction))) != 0
    later = runs.load(writer.id, h.env)
    assert later.turn > writer.turn and later.turn_state == runs.COLLECTED
    folder = paths.ensure_run_tmp_dir(writer.id)
    (folder / "pytest").mkdir()
    h.answer("accept", commit=item["head"])
    h.instance().answers()
    assert h.flow()["tasks"][0]["status"] == "accepted"
    assert not list(folder.iterdir())


def test_resume_merges_without_retrying_interrupted_accept_cleanup(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + FIX})
    h.create(rounds=0)
    item = h.run()["tasks"][0]
    writer = runs.load(item["writer_run"], h.env)
    runs.save(replace(writer, sandbox=sandbox.CODEX))
    folder = paths.ensure_run_tmp_dir(writer.id)
    (folder / "remaining").touch()

    def interrupt(run_id: str, keep: list[str], env: Mapping[str, str]) -> None:
        raise KeyboardInterrupt

    h.answer("accept", commit=item["head"])
    with monkeypatch.context() as patch:
        patch.setattr(paths, "clean_run_tmp_dir", interrupt)
        with pytest.raises(KeyboardInterrupt):
            h.instance().answers()
    assert h.flow()["tasks"][0]["status"] == "accepted"
    monkeypatch.setattr(h.driver, "foreign_input", lambda run: "owner typed")
    monkeypatch.setattr(
        runner._Runner, "clean_writer_tmp", lambda *args: pytest.fail("cleanup retried")
    )
    assert h.run()["tasks"][0]["status"] == "merged"
    assert (folder / "remaining").exists()


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
    h.scripts_update(**{"a read-only review;": "@reply " + outside_reply(PASS)})
    h.create([task("a", text="@git mv README.md renamed.md", files=["renamed.md"])])
    item = h.run()["tasks"][0]
    assert item["status"] == "merged"
    assert item["changed_files"] == ["README.md", "renamed.md"]
    assert item["outside_files"] == ["README.md"]


def test_recovered_turn_records_original_wave_declarations(h: Harness) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + shared_reply()})
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
    assert "    b (task b)" in _review_brief(h, "a")
    assert len(item["reviews"]) == 1


def test_review_answer_reviews_a_task_stopped_for_a_declared_file(h: Harness) -> None:
    h.scripts_update(**{"Review of task a,": "@reply " + shared_reply()})
    h.create([task("a"), task("b")])
    flow, writer = started_writer(h)
    worktree = Path(writer.worktree)
    (worktree / "b").write_text("shared\n")
    git(worktree, "add", "b", env=h.env)
    git(worktree, "commit", "-m", "shared", env=h.env)
    head = git(worktree, "rev-parse", "HEAD", env=h.env).strip()
    flow["tasks"][0].update(
        status="stopped",
        reason="changed b, which task b in this wave declares",
        writer_run=writer.id,
        branch=writer.branch,
        worktree=writer.worktree,
        task_base=runs.first_base(writer),
        head=head,
        changed_files=["a", "b"],
        outside_files=["b"],
        wave=1,
        wave_declarations={"b": ["b"]},
    )
    flow["tasks"][1].update(status="dropped", wave=1)
    record.save(flow, h.env)
    h.answer("review")
    item = h.run()["tasks"][0]
    assert item["status"] == "merged" and len(item["reviews"]) == 1
    assert "    b (task b)" in _review_brief(h, "a")
    assert _refusals(h) == []


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


@pytest.mark.parametrize("phase", ["writer", "reviewer", "follow-up", "checking"])
def test_cancel_live_runner_keeps_turns_and_stops_check(
    h: Harness, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from ember.flows import command

    check = [sys.executable, "-c", "import time; time.sleep(60)"]
    h.create(
        [task("a", text="@hang\n@write a value" if phase == "writer" else None)],
        rounds=1 if phase == "follow-up" else 0,
        **({"check": check} if phase == "checking" else {}),
    )
    if phase == "reviewer":
        h.scripts_update(**{"a read-only review;": "@hang\n@reply " + PASS})
    if phase == "follow-up":
        h.scripts_update(
            **{
                "Review of task a, round 1,": "@reply " + FIX,
                "Fix round 1 of task a": "@write a fixed",
                "Review of task a, round 2,": "@hang\n@reply " + PASS,
            }
        )
    role = "reviewer" if phase == "follow-up" else phase
    expected_turn = 2 if phase == "follow-up" else 1
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
                    else item[f"{role}_run"]
                    and (run := runs.load(item[f"{role}_run"], h.env)).status
                    == runs.RUNNING
                    and run.turn == expected_turn
                )
                if ready:
                    break
                assert time.monotonic() < deadline, flow
                time.sleep(0.01)
            check_pid = (flow.get("active_check") or {}).get("pid")
            if phase == "checking":
                assert paths.check_tmp_dir(h.id).is_dir()
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
        assert not paths.check_tmp_dir(h.id).exists()
        assert check_pid is not None and runner._process_start(check_pid) is None
        assert flow["check_pending"] and flow["checks"][-1]["exit"] == 124
        assert report["open_runs"] == []
        flow["check"] = [sys.executable, "-c", "pass"]
        record.save(flow, h.env)
    else:
        run_id = flow["tasks"][0][f"{role}_run"]
        assert {"task": "a", "role": role, "run": run_id} in report["open_runs"]
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
    if phase == "follow-up":
        run_id = flow["tasks"][0]["reviewer_run"]
        assert [(r["run"], r["turn"]) for r in final["tasks"][0]["reviews"]] == [
            (run_id, 1),
            (run_id, 2),
        ]


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
@pytest.mark.parametrize("role", ["writer", "reviewer"])
def test_capacity_exhaustion_does_not_halt_other_flow_tasks(
    h: Harness, monkeypatch: pytest.MonkeyPatch, parallel: int, role: str
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
    if role == "reviewer":
        h.scripts_update(**{"Review of task a, round 1,": "@provider capacity"})
    h.create(
        [task("a", text="@provider capacity" if role == "writer" else None), task("b")],
        parallel=parallel,
    )
    flow = h.run()
    assert flow["halted"] is None
    assert flow["tasks"][0]["status"] == "stopped"
    assert flow["tasks"][1]["status"] == "merged"
    stops = [
        event
        for event in record.read_events(h.id, h.env)
        if event["kind"] == "lead_stop"
    ]
    assert len(stops) == 1
    assert stops[0]["task"] == "a"
    assert stops[0]["provider_kind"] == "capacity"
    for _ in range(2):
        out, err = io.StringIO(), io.StringIO()
        args = build_parser().parse_args(["flow", "--watch", h.id, "--json"])
        assert command.main(args, h.env, out=out, err=err) == 0
        reports = ["report: capacity on a; recommend only the same-worker retry"]
        assert json.loads(out.getvalue())["report"] == reports
        assert err.getvalue().splitlines()[1:] == reports


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
    h.scripts_update(**{"a read-only review;": "@reply " + outside_reply(PASS)})
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


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_base_check_watch_reports(h: Harness, timeout: bool) -> None:
    h.create(
        check_base=True,
        check=[
            sys.executable,
            "-c",
            "import os, pathlib, time; "
            "folder = pathlib.Path(os.environ['TMPDIR']); "
            f"assert folder == pathlib.Path({str(paths.check_tmp_dir(h.id))!r}); "
            "(folder / 'leftover').touch(); print('base failed', flush=True); "
            + ("time.sleep(60)" if timeout else "raise SystemExit(3)"),
        ],
        check_timeout=1,
    )
    flow = h.run()
    assert flow["status"] == "failed" and flow["check_pending"]
    assert flow["wave"] == 0
    assert all(t["writer_run"] is None for t in flow["tasks"])
    assert runs.list_runs(h.env) == ([], [])
    (check,) = flow["checks"]
    assert check["wave"] == 0 and check["exit"] == (124 if timeout else 3)
    assert not paths.check_tmp_dir(h.id).exists()
    reason = f"the check failed on the base: exit {check['exit']}, see {check['log']}"
    assert flow["reason"] == reason
    report = (
        "report: the check failed on the base; "
        "correct the check or the flow branch, then resume; "
        "when the check command is wrong, replace it with "
        f"emb flow --resume {h.id} --check '<JSON argv list>' --reason '<text>'"
    )
    next_line = (
        f"next: correct the check {shlex.join(flow['check'])} or "
        f"commit a correction to the flow branch {flow['branch']} "
        f"(an emb run on {shlex.quote(flow['worktree'])}, "
        f"then emb merge <its branch> --into {flow['branch']} --verdict keep "
        f"--note '<verdict line>'), then emb flow --resume {h.id}"
    )
    for as_json in (False, True):
        out, err = io.StringIO(), io.StringIO()
        args = build_parser().parse_args(
            ["flow", "--watch", h.id, *(["--json"] if as_json else [])]
        )
        assert command.main(args, h.env, out=out, err=err) == 82
        assert err.getvalue().splitlines() == [next_line, report]
        if as_json:
            result = json.loads(out.getvalue())
            assert result["reason"] == reason
            assert result["report"] == [report]
            assert result["next"] == next_line
            assert result["check_output"] == "base failed\n"
        else:
            assert reason in out.getvalue()
            assert "check log output:\n  base failed" in out.getvalue()


@pytest.mark.parametrize("metadata", [False, True, None])
def test_passing_base_check_precedes_wave_one(
    h: Harness, monkeypatch: pytest.MonkeyPatch, metadata: bool | None
) -> None:
    h.create(check_base=True, check=[sys.executable, "-c", "print('checked')"])
    path = record.folder(h.id, h.env) / record.FLOW_NAME
    saved = json.loads(path.read_text())
    if metadata is None:
        del saved["check_base"]
    else:
        saved["check_base"] = metadata
    path.write_text(json.dumps(saved))
    pipeline = runner._Runner.pipeline
    started = []

    def after_base(instance: runner._Runner, item: dict[str, Any]) -> None:
        flow = h.flow()
        assert [(c["wave"], c["exit"]) for c in flow["checks"]] == [(0, 0)]
        assert not flow["check_pending"] and flow["wave"] == 1
        assert item["writer_run"] is None
        started.append(item["id"])
        pipeline(instance, item)

    monkeypatch.setattr(runner._Runner, "pipeline", after_base)
    flow = h.run()
    assert started == ["a"]
    assert flow["status"] == "done"
    assert [(c["wave"], c["exit"]) for c in flow["checks"]] == [(0, 0), (1, 0)]
    assert flow["tasks"][0]["wave"] == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_base_check_interruption_resumes_before_any_task(
    h: Harness, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    entered = h.tmp / "check-entered"
    h.create(
        check_base=True,
        check=[
            sys.executable,
            "-c",
            "import pathlib,time; "
            f"p = pathlib.Path({str(entered)!r}); "
            "first = not p.exists(); p.touch(); "
            "time.sleep(60) if first else None",
        ],
    )
    shutdown = threading.Event()
    monkeypatch.setattr(command, "POLL_SECONDS", 0.01)

    def signal_runner(fd: int, sig: int) -> None:
        assert sig == signal.SIGTERM
        shutdown.set()

    monkeypatch.setattr(command.signal, "pidfd_send_signal", signal_runner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner._run, h.id, h.env, h.factory, 0.01, shutdown)
        try:
            deadline = time.monotonic() + 10
            while not entered.exists() or not h.flow().get("active_check"):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert runs.list_runs(h.env) == ([], [])
            if cancel:
                command._cancel(
                    h.id, "stop base check", True, h.env, io.StringIO(), io.StringIO()
                )
            else:
                shutdown.set()
            assert future.result(timeout=10) == 0
        finally:
            shutdown.set()
    flow = h.flow()
    assert flow["status"] == ("cancelled" if cancel else "failed")
    assert flow["check_pending"] and flow["wave"] == 0
    assert flow["checks"][0]["exit"] == 124
    assert runs.list_runs(h.env) == ([], [])
    pipeline = runner._Runner.pipeline

    def after_retry(instance: runner._Runner, item: dict[str, Any]) -> None:
        saved = h.flow()
        assert [(c["wave"], c["exit"]) for c in saved["checks"]] == [(0, 124), (0, 0)]
        assert not saved["check_pending"] and saved["wave"] == 1
        pipeline(instance, item)

    monkeypatch.setattr(runner._Runner, "pipeline", after_retry)
    command._resume(h.id, h.env, lambda _: ["true"], io.StringIO())
    assert record.cancel_request(h.id, h.env) is None
    final = h.run()
    assert final["status"] == "done"
    assert [(c["wave"], c["exit"]) for c in final["checks"]] == [
        (0, 124),
        (0, 0),
        (1, 0),
    ]


def test_passed_base_check_is_not_rerun_on_resume(h: Harness) -> None:
    h.create(
        [task("a", text="Do nothing")],
        check_base=True,
        check=[sys.executable, "-c", "print('checked')"],
    )
    flow = h.run()
    assert flow["status"] == "stopped" and not flow["check_pending"]
    assert [(c["wave"], c["exit"]) for c in flow["checks"]] == [(0, 0)]
    command._resume(h.id, h.env, lambda _: ["true"], io.StringIO())
    resumed = h.run()
    assert resumed["status"] == "stopped"
    assert resumed["checks"] == flow["checks"]


def test_base_check_resume_preserves_flow_branch_correction(h: Harness) -> None:
    h.create(
        check_base=True,
        check=[
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('fix').exists()",
        ],
    )
    flow = h.run()
    assert flow["status"] == "failed"
    worktree = Path(flow["worktree"])
    (worktree / "fix").write_text("correction")
    git(worktree, "add", "fix", env=h.env)
    git(worktree, "commit", "-m", "fix: correct base", env=h.env)
    correction = git(worktree, "rev-parse", "HEAD", env=h.env)
    command._resume(h.id, h.env, lambda _: ["true"], io.StringIO())
    final = h.run()
    assert final["status"] == "done"
    assert final["base"] == flow["base"]
    assert final["branch"] == flow["branch"]
    assert final["tasks"][0]["task_base"] == correction
    assert [(c["wave"], c["exit"]) for c in final["checks"]] == [(0, 1), (0, 0), (1, 0)]


def test_old_record_runs_without_base_check(h: Harness) -> None:
    h.create(check=[sys.executable, "-c", "print('checked')"])
    path = record.folder(h.id, h.env) / record.FLOW_NAME
    old = json.loads(path.read_text())
    del old["check_base"]
    path.write_text(json.dumps(old))
    flow = h.run()
    assert flow["status"] == "done"
    assert [(c["wave"], c["exit"]) for c in flow["checks"]] == [(1, 0)]


@pytest.mark.parametrize("wave", [0, 1])
@pytest.mark.parametrize(
    ("passes", "scoped"), [(False, False), (True, False), (True, True)]
)
def test_replace_failed_check_before_pending_wave(
    h: Harness, monkeypatch: pytest.MonkeyPatch, wave: int, passes: bool, scoped: bool
) -> None:
    old_check = [sys.executable, "-c", 'print("old check"); raise SystemExit(1)']
    writer_check = [["pytest", "tests/test_b.py"]]
    options: dict[str, Any] = {"writer_check": writer_check} if scoped else {}
    first = {**task("a"), "writer_check": []} if scoped else task("a")
    h.create(
        [first, task("b", after=["a"])],
        check=old_check,
        check_base=wave == 0,
        rounds=1,
        **options,
    )
    failed = h.run()
    resolved = [[], writer_check] if scoped else [None, None]
    assert [task["writer_check"] for task in failed["tasks"]] == resolved
    assert failed["status"] == "failed" and failed["wave"] == wave
    assert failed["check_pending"]
    folder = record.folder(h.id, h.env)
    saved_plan = (folder / record.PLAN_NAME).read_bytes()
    new_check = [
        sys.executable,
        "-c",
        f'print("new check"); raise SystemExit({0 if passes else 2})',
    ]
    h.scripts_update(
        **{
            "Review of task b, round 1,": "@reply " + FIX,
            "Fix round 1 of task b": "@write b fixed",
        }
    )
    pipeline = runner._Runner._pipeline
    started = []

    def verify_check(instance: runner._Runner, item: dict[str, Any]) -> None:
        current = h.flow()
        assert current["checks"][-1]["exit"] == 0
        assert not current["check_pending"]
        assert current["check"] == new_check
        started.append(item["id"])
        pipeline(instance, item)

    monkeypatch.setattr(runner._Runner, "_pipeline", verify_check)
    args = build_parser().parse_args(
        [
            "flow",
            "--resume",
            h.id,
            "--check",
            json.dumps(new_check),
            "--reason",
            "correct the command",
        ]
    )
    err = io.StringIO()
    assert (
        command.main(
            args, h.env, runner_argv=lambda _: ["true"], out=io.StringIO(), err=err
        )
        == 0
    ), err.getvalue()
    final = h.run()
    assert [task["writer_check"] for task in final["tasks"]] == resolved
    assert final["status"] == ("done" if passes else "failed")
    assert final["check_pending"] is not passes
    assert final["checks"][1]["wave"] == wave
    assert final["checks"][1]["exit"] == (0 if passes else 2)
    assert (folder / record.PLAN_NAME).read_bytes() == saved_plan
    if passes:
        assert started == (["a", "b"] if wave == 0 else ["b"])
        assert all(t["status"] == "merged" for t in final["tasks"])
        for kind in ("write", "fix"):
            brief = next((folder / "briefs").glob(f"b-{kind}-*.md")).read_text()
            if scoped:
                assert "    pytest tests/test_b.py" in brief
                assert (
                    f"Do not run the flow's check command `{shlex.join(new_check)}`"
                    in brief
                )
            else:
                assert (
                    f"Run the plan's check command `{shlex.join(new_check)}`" in brief
                )
            assert shlex.join(old_check) not in brief
        if scoped:
            review = next((folder / "briefs").glob("b-review-*.md")).read_text()
            assert "    pytest tests/test_b.py" in review
            assert (
                f"The flow runs its check command `{shlex.join(new_check)}`" in review
            )
    else:
        assert started == []
        assert final["tasks"] == failed["tasks"]
        assert final["checks"][-1]["exit"] == 2


def test_an_answer_replaced_after_the_queue_was_read_is_not_applied(
    h: Harness,
) -> None:
    _stopped_for_the_lead(h)
    h.answer("drop")
    instance = h.instance()
    lines = record.read_answers(h.id, h.env)
    h.answer("withdraw")
    instance._apply_answer(0, lines[0], set(), ahead=False)
    assert instance.flow["tasks"][0]["status"] == "stopped"
    assert instance.flow["answers_seen"] == 1
    h.answer("drop")
    read = record.read_answers(h.id, h.env)
    h.answer("review")
    instance._apply_answer(2, read[2], set(), ahead=True)
    assert instance.flow["tasks"][0]["status"] == "stopped"
    assert instance.flow["answers_taken"] == []
    assert h.run()["tasks"][0]["status"] == "merged"
    assert _refusals(h) == []


def test_reviews_ahead_wait_for_a_halt_a_running_job_and_a_free_slot(
    h: Harness,
) -> None:
    _stopped_for_the_lead(h)
    h.answer("review")
    instance = h.instance()
    assert instance.answers_ahead([], 0) == []
    assert instance.answers_ahead(["a"], 1) == []
    instance.flow["halted"] = "provider error on codex; new runs halted"
    assert instance.answers_ahead([], 1) == []
    assert instance.flow["halted"] and instance.flow["tasks"][0]["status"] == "stopped"
    instance.flow["halted"] = None
    (scheduled,) = instance.answers_ahead([], 1)
    assert (scheduled["id"], scheduled["status"], scheduled["entry"]) == (
        "a",
        "pending",
        "review",
    )
    assert instance.flow["answers_taken"] == [0]
    assert instance.answers_ahead([], 1) == []


def test_a_runner_that_stopped_after_taking_an_answer_ahead_applies_it_once(
    h: Harness,
) -> None:
    before = _stopped_for_the_lead(h)
    h.answer("review")
    instance = h.instance()
    assert len(instance.answers_ahead([], 1)) == 1
    saved = h.flow()
    assert (saved["answers_seen"], saved["answers_taken"]) == (0, [0])
    assert not instance.unapplied()
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert len(item["reviews"]) == len(before["tasks"][0]["reviews"]) + 1
    assert (flow["answers_seen"], flow["answers_taken"]) == (1, [])
    assert _refusals(h) == []


def test_a_fix_decided_by_a_review_started_in_a_wave_waits_for_the_next_wave(
    h: Harness,
) -> None:
    h.scripts_update(**{"Review of task a, round 1,": "@reply unreadable"})
    h.create([task("a"), task("c", text="@write c value\n@hang")], rounds=1)
    flow = h.flow()
    flow["stall_minutes"] = 5
    record.save(flow, h.env)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.run)
        _wait_for(lambda: _status(h, "a") == "stopped")
        stopped = h.flow()["tasks"][0]
        turns = runs.load(stopped["writer_run"], h.env).turn
        h.scripts_update(
            **{
                "Review of task a, round 1,": "@reply " + FIX,
                "Fix round 1 of task a": "@write a fixed",
            }
        )
        h.answer("review")
        # The answer itself makes the task pending; the deferred fix is the
        # pending state with the review's saved decision.
        _wait_for(
            lambda: (
                (item := h.flow()["tasks"][0])["status"] == "pending"
                and item.get("recover_review") is True
            )
        )
        waiting = h.flow()["tasks"][0]
        assert (waiting["entry"], waiting["review_action"]) == ("review", "fix")
        assert waiting["recover_review"] is True
        time.sleep(0.3)
        assert _status(h, "a") == "pending" and _status(h, "c") == "writing"
        assert runs.load(stopped["writer_run"], h.env).turn == turns
        assert not future.done()
        _release(h, "c")
        flow = future.result(timeout=60)
    item = flow["tasks"][0]
    assert runs.load(item["writer_run"], h.env).turn == turns + 1
    assert item["status"] == "merged", item["reason"]
    first, follow_up = item["reviews"][-2:]
    assert first == waiting["reviews"][-1]
    assert first["run"] == follow_up["run"]
    assert (first["verdict"], first["head"], first["turn"]) == (
        "fix",
        waiting["head"],
        1,
    )
    assert (follow_up["verdict"], follow_up["head"], follow_up["turn"]) == (
        "pass",
        item["head"],
        2,
    )
    assert first["head"] != follow_up["head"]
    assert flow["tasks"][1]["status"] == "merged"


def _fallback_events(h: Harness) -> list[dict[str, Any]]:
    return [e for e in record.read_events(h.id, h.env) if e["kind"] == "fallback"]


def test_a_review_that_a_provider_error_ends_goes_to_the_fallback_worker(
    h: Harness,
) -> None:
    choices.set_settings({"provider.fallback": ["claude:opus"]}, env=h.env)
    marker = h.tmp / "failed-once"
    script = f"@provider-once {marker} rate limited\n@reply {PASS}"
    h.scripts_update(**{"a read-only review;": script})
    h.create([task("a")])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged" and flow["halted"] is None
    (entry,) = item["fallbacks"]
    assert (entry["role"], entry["from"], entry["to"], entry["cause"]) == (
        "reviewer",
        "codex:gpt-5",
        "claude:opus",
        "outage",
    )
    # The task's saved reviewer stays; the repeat alone ran on the fallback.
    assert item["reviewer"] is None
    assert (item["fallback_pending"], item["fallback_failed"]) == (None, {})
    assert runs.load(item["reviews"][-1]["run"], h.env).engine == "claude"
    failed = runs.load(entry["run"], h.env)
    assert (failed.engine, failed.status) == ("codex", runs.CLOSED)
    assert [(e["task"], e["to"]) for e in _fallback_events(h)] == [("a", "claude:opus")]
    assert [hold.name() for hold in holds.active(h.env)] == ["codex"]
    assert flow["reviewer"]["model"] == "gpt-5"


def test_a_review_never_falls_back_to_the_model_that_wrote_the_work(
    h: Harness,
) -> None:
    choices.set_settings({"provider.fallback": ["codex:gpt-5"]}, env=h.env)
    marker = h.tmp / "failed-once"
    script = f"@provider-once {marker} rate limited\n@reply {PASS}"
    h.scripts_update(**{"a read-only review;": script})
    h.create([task("a")])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "stopped"
    assert "fallbacks" not in item
    assert flow["halted"] == "provider error on codex; new runs halted"


def test_a_writer_that_a_provider_error_ends_is_continued_by_the_fallback_worker(
    h: Harness,
) -> None:
    choices.set_settings({"provider.fallback": ["claude:opus"]}, env=h.env)
    marker = h.tmp / "failed-once"
    text = f"@write a value\n@provider-once {marker} rate limited\n@write a2 more"
    h.create([task("a", text=text, files=["a", "a2"])])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    (entry,) = [e for e in item["fallbacks"] if e["role"] == "writer"]
    assert (entry["from"], entry["to"], entry["cause"]) == (
        "codex:gpt-5",
        "claude:opus",
        "outage",
    )
    first = runs.load(entry["run"], h.env)
    second = runs.load(item["writer_run"], h.env)
    assert (first.engine, second.engine) == ("codex", "claude")
    assert second.continues == first.id
    assert item["task_base"] == first.base
    assert first.branch not in flow["abandoned"]
    assert git(h.repo, "branch", "--list", first.branch, env=h.env) == ""
    assert first.status == runs.CLOSED
    assert (item["writer"], item["fallback_pending"]) == (None, None)
    assert sorted(item["changed_files"]) == ["a", "a2"]
    assert "## Work already done" in (second.folder / "brief.md").read_text()
    # A review does not fall back to either model that wrote the work.
    assert h.instance().fallback_avoid(item, "reviewer") == (
        ("codex", "gpt-5"),
        ("claude", "opus"),
    )
    assert h.instance().fallback_avoid(item, "writer") == ()


def test_automatic_fix_continues_a_reviewer_started_on_a_fallback_worker(
    h: Harness,
) -> None:
    choices.set_settings(
        {"provider.hold_minutes": "0", "provider.fallback": ["claude:opus"]}, env=h.env
    )
    first = f"@provider-once {h.tmp / 'failed-once'} limited\n@reply {FIX}"
    h.scripts_update(
        **{
            "Review of task a, round 1,": first,
            "Fix round 1 of task a": "@write a fixed",
            "Review of task a, round 2,": "@reply " + PASS,
        }
    )
    h.create([task("a")], rounds=1)
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    engines = [runs.load(review["run"], h.env).engine for review in item["reviews"]]
    assert engines == ["claude", "claude"]
    assert item["reviews"][0]["run"] == item["reviews"][1]["run"]
    assert len(item["fallbacks"]) == 1
    assert (item["reviewer"], item["fallback_failed"]) == (None, {})


def test_a_repeat_on_the_fallback_worker_is_made_after_a_resume(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    choices.set_settings({"provider.fallback": ["claude:opus"]}, env=h.env)
    marker = h.tmp / "failed-once"
    script = f"@provider-once {marker} rate limited\n@reply {PASS}"
    h.scripts_update(**{"a read-only review;": script})
    h.create([task("a")])
    fall_back = runner._Runner.fall_back

    def stop_after(self: runner._Runner, *args: Any) -> bool:  # noqa: ANN401
        """Stop the runner between the failed run and its repeat."""
        done = fall_back(self, *args)
        self.shutdown.set()
        return done

    monkeypatch.setattr(runner._Runner, "fall_back", stop_after)
    runner.run(h.id, h.env, core_factory=h.factory, poll_seconds=0.01)
    monkeypatch.setattr(runner._Runner, "fall_back", fall_back)
    item = h.flow()["tasks"][0]
    pending = item["fallback_pending"]
    assert (pending["role"], pending["failed_run"], pending["continue_run"]) == (
        "reviewer",
        item["reviewer_run"],
        None,
    )
    assert (pending["worker"]["engine"], pending["worker"]["model"]) == (
        "claude",
        "opus",
    )
    assert runs.load(pending["failed_run"], h.env).status == runs.CLOSED
    assert item["fallback_failed"] == {"reviewer": [["codex", "gpt-5", "outage"]]}
    assert item["status"] != "merged" and not item["reviews"]
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    assert runs.load(item["reviews"][-1]["run"], h.env).engine == "claude"
    assert (item["fallback_pending"], item["fallback_failed"]) == (None, {})
    assert len(item["fallbacks"]) == 1


def test_a_run_is_not_repeated_when_its_resend_is_uncertain_or_it_stays_open(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    choices.set_settings({"provider.fallback": ["claude:opus"]}, env=h.env)
    h.create([task("a")])
    h.run()
    instance = h.instance()
    item = instance.flow["tasks"][0]
    run = runs.load(item["writer_run"], h.env)
    runs.save(replace(run, last_variant="uncertain"))
    assert instance.fall_back(item, "writer", run.id, "outage") is False
    runs.save(replace(run, last_variant="outage"))
    with monkeypatch.context() as patch:
        patch.setattr(instance, "close", lambda run_id: "refused")
        assert instance.fall_back(item, "writer", run.id, "outage") is False
    assert "fallback_pending" not in item and "fallbacks" not in item
    assert instance.fall_back(item, "writer", run.id, "outage") is True
    assert item["fallback_pending"] == {
        "role": "writer",
        "worker": item["fallback_pending"]["worker"],
        "failed_run": run.id,
        "continue_run": run.id,
    }
    assert h.flow()["tasks"][0]["fallback_pending"] == item["fallback_pending"]
    # A lead answer starts a new attempt without the pending repeat.
    assert item["fallback_failed"] == {"writer": [["codex", "gpt-5", "outage"]]}


def test_a_start_that_a_hold_redirects_is_recorded_on_the_task(h: Harness) -> None:
    choices.set_settings({"provider.fallback": ["claude:opus"]}, env=h.env)
    holds.start("codex", "gpt-5", "outage", 15, env=h.env)
    h.create([task("a")])
    flow = h.run()
    item = flow["tasks"][0]
    assert item["status"] == "merged"
    assert runs.load(item["writer_run"], h.env).engine == "claude"
    (entry,) = item["fallbacks"]
    assert (entry["role"], entry["from"], entry["to"], entry["cause"]) == (
        "writer",
        "codex:gpt-5",
        "claude:opus",
        "held",
    )
    # The review does not go to the model that wrote the work.
    assert runs.load(item["reviews"][-1]["run"], h.env).engine == "codex"
    assert item["writer"] is None


def test_held_flow_reviewer_keeps_pinned_head_and_skips_the_writer(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high", "gpt-mini@low"],
            "provider.fallback": ["claude:opus", "codex:gpt-mini"],
        },
        env=h.env,
    )
    holds.start("codex", "gpt-5", "capacity", 15, env=h.env)
    h.create()
    flow = h.flow()
    flow["writer"] = plan.Worker("claude", "opus", "high", "option").to_json()
    record.save(flow, h.env)
    source_heads: list[str] = []
    start = Core.start

    def move_source(core: Core, options: StartOptions) -> int:
        if not options.read_only:
            return start(core, options)
        tree = Path(options.dir)
        branch = git(tree, "symbolic-ref", "--short", "HEAD", env=h.env)
        git(tree, "checkout", "--detach", "HEAD^", env=h.env)
        try:
            source_heads.append(git(tree, "rev-parse", "HEAD", env=h.env))
            return start(core, options)
        finally:
            git(tree, "checkout", branch, env=h.env)

    monkeypatch.setattr(Core, "start", move_source)
    item = h.run()["tasks"][0]
    assert item["status"] == "merged", item["reason"]
    (review,) = item["reviews"]
    assert source_heads == [item["task_base"]]
    reviewer = runs.load(review["run"], h.env)
    assert reviewer.base == review["head"] == item["head"] != source_heads[0]
    assert (reviewer.engine, reviewer.model, reviewer.effort) == (
        "codex",
        "gpt-mini",
        "low",
    )
    (entry,) = item["fallbacks"]
    assert (entry["role"], entry["cause"], entry["from"], entry["to"]) == (
        "reviewer",
        "held",
        "codex:gpt-5",
        "codex:gpt-mini",
    )
