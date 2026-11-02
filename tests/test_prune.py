import contextlib
import json
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import paths, prune
from ember.flows import lock as flow_lock
from ember.flows import plan
from ember.flows import record as flows
from ember.panels import record as panels
from ember.runs import record as runs

NOW = 1_000_000.0


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(prune, "_git", git)
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }


def run(env: dict[str, str], **changes: Any) -> runs.Run:  # noqa: ANN401
    ident = runs.new_id("codex", 0)
    data: dict[str, Any] = dict(
        id=ident,
        engine="codex",
        model="model",
        effort=None,
        read_only=True,
        repo="/checkout",
        worktree="/checkout",
        branch=None,
        base="a" * 40,
        sandbox="none",
        network=False,
        read=(),
        read_effective=(),
        trailers=(),
        claim=None,
        status="closed",
        turn=1,
        turn_state="collected",
        reply=None,
        expected_head=None,
        started_at=0.0,
        updated_at=NOW,
        ended_at=0.0,
        last_exit=0,
        last_reason=None,
        commits=(),
        subject="Task",
    )
    data.update(changes)
    record = runs.Run(**data, folder=runs.make_run_dir(ident, env), env=env)
    runs.save(record)
    (record.folder / "turn-1.md").write_text("reply")
    return record


def flow(env: dict[str, str], **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    ident = flows.new_id(0)
    source = {
        "dir": "/checkout",
        "base": "main",
        "tasks": [{"id": "a", "task": "Task", "files": ["a"]}],
    }
    document = plan.from_json(source, Path("/plan.json"))
    worker = plan.Worker("codex", "model", None, "option")
    record = flows.new_flow(
        ident,
        document,
        top="/checkout",
        main="/checkout",
        base="a" * 40,
        writer=worker,
        reviewer=worker,
        stall_minutes=10,
        claim=None,
        now=0,
    )
    record.update(status="done", ended_at=0)
    record.update(changes)
    flows.create(record, json.dumps(source), env)
    return record


def panel(env: dict[str, str], *members: runs.Run, **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    entries = [
        dict(
            n=n,
            engine="codex",
            model="model",
            effort=None,
            source="option",
            status="answered",
            run=member.id,
            exit=0,
            reason=None,
            reply=None,
            bytes=0,
            changed=[],
            seconds=1,
            closed=True,
            close_error=None,
            attempts=[],
        )
        for n, member in enumerate(members, 1)
    ]
    record = panels.new_panel(
        panels.new_id(0),
        dir="/checkout",
        question_file="/question.md",
        read=[],
        claim=None,
        stall_minutes=10,
        timeout_minutes=20,
        members=entries,
        left_out=[],
        now=0,
    )
    record.update(status="done", ended_at=0)
    record.update(changes)
    panels.create(record, "question", "brief", env)
    return record


def ids(result: prune.Result) -> set[str]:
    return {item["id"] for item in result.removed}


@pytest.mark.parametrize("status", runs.STATUSES)
def test_only_ended_runs(env: dict[str, str], status: str) -> None:
    record = run(env, status=status)
    result = prune.remove(0, env=env, now=NOW)
    ended = status in ("closed", "failed")
    assert (record.id in ids(result)) == ended
    assert record.folder.exists() != ended
    assert result.errors == []


@pytest.mark.parametrize("status", flows.FLOW_STATUSES)
def test_only_ended_flows(env: dict[str, str], status: str) -> None:
    record = flow(env)
    record["status"] = status
    flows.save(record, env)
    result = prune.remove(0, env=env, now=NOW)
    assert (record["id"] in ids(result)) == (status in flows.ENDED)
    assert result.errors == []


@pytest.mark.parametrize("offset, expected", [(86400, True), (86399.99, False)])
@pytest.mark.parametrize("use_end", [True, False])
def test_inclusive_age_and_update_fallback(
    env: dict[str, str], offset: float, expected: bool, use_end: bool
) -> None:
    record = run(
        env, ended_at=NOW - offset if use_end else None, updated_at=NOW - offset
    )
    if use_end:
        record = replace(record, updated_at=NOW)
        runs.save(record)
    result = prune.remove(1, env=env, now=NOW)
    assert (record.id in ids(result)) == expected


def test_explicit_ids_ignore_age_and_report_refusals(env: dict[str, str]) -> None:
    young = run(env, ended_at=NOW)
    opened = run(env, status="waiting")
    missing = runs.new_id("codex", 0)
    broken = run(env)
    (broken.folder / "run.json").write_text("{")
    result = prune.remove(
        999, [young.id, opened.id, missing, broken.id, "../bad"], env=env
    )
    assert ids(result) == {young.id}
    assert {item["id"] for item in result.skipped} == {
        opened.id,
        missing,
        broken.id,
        "../bad",
    }
    assert all(item["reason"] for item in result.skipped)
    assert opened.folder.exists() and broken.folder.exists()


def test_parent_collects_record_events_check_and_fix_runs(env: dict[str, str]) -> None:
    children = [run(env, ended_at=NOW) for _ in range(5)]
    record = flow(env)
    record["tasks"][0].update(writer_run=children[0].id)
    record["checks"] = [
        {"wave": 1, "exit": 0, "log": "check.log", "at": 1, "run": children[1].id}
    ]
    record["fix_run"] = children[2].id
    flows.save(record, env)
    for child in children[3:]:
        flows.append_event(record["id"], {"kind": "anything", "run": child.id}, env)
    selection = prune.select(1, env=env, now=NOW)
    assert set(selection.records) == {record["id"], *(child.id for child in children)}
    assert all(child.folder.exists() for child in children)
    result = prune.remove(1, env=env, now=NOW)
    assert ids(result) == set(selection.records)
    assert not result.skipped


@pytest.mark.parametrize("parent_kind", ["flow", "panel"])
def test_child_waits_for_qualifying_selected_parent(
    env: dict[str, str], parent_kind: str
) -> None:
    child = run(env)
    if parent_kind == "flow":
        parent = flow(env)
        parent["tasks"][0]["writer_run"] = child.id
        parent["ended_at"] = NOW
        flows.save(parent, env)
    else:
        parent = panel(env, child, ended_at=NOW)
    assert ids(prune.remove(1, env=env, now=NOW)) == set()
    result = prune.remove(0, [child.id], env=env)
    assert not result.removed
    assert "parent not selected" in result.skipped[0]["reason"]
    assert ids(prune.remove(999, [parent["id"]], env=env)) == {child.id, parent["id"]}


def test_shared_child_requires_every_parent(env: dict[str, str]) -> None:
    child = run(env)
    first, second = panel(env, child), panel(env, child)
    result = prune.remove(0, [first["id"]], env=env)
    assert ids(result) == {first["id"]}
    assert child.folder.exists()
    assert ids(prune.remove(0, [second["id"]], env=env)) == {second["id"], child.id}


@pytest.mark.parametrize("status", ["done", "incomplete"])
def test_panel_requires_every_current_and_old_member_run_ended(
    env: dict[str, str], status: str
) -> None:
    child = run(env)
    old = run(env, status="waiting")
    record = panel(env, child, status=status)
    record["members"][0]["attempts"].append(
        dict(kind="retry", at=1, exit=0, run=old.id)
    )
    panels.save(record, env)
    assert not prune.remove(0, env=env).removed
    runs.save(replace(old, status="failed"))
    assert ids(prune.remove(0, env=env)) == {record["id"], child.id, old.id}


def test_dry_run_retains_files_and_external_logs(
    env: dict[str, str], tmp_path: Path
) -> None:
    external = tmp_path / "external.log"
    external.write_text("kept")
    record = run(env, reply=str(external))
    (record.folder / "external-link").symlink_to(external)
    result = prune.remove(0, dry_run=True, env=env)
    assert ids(result) == {record.id}
    assert (record.folder / "turn-1.md").read_text() == "reply"
    assert ids(prune.remove(0, env=env)) == {record.id}
    assert external.read_text() == "kept"


def test_invalid_folder_names_and_empty_folder(env: dict[str, str]) -> None:
    assert prune.remove(0, env=env).to_json() == dict(
        removed=[], skipped=[], branches=[]
    )
    root = paths.runs_dir(env)
    (root / "not-a-record").mkdir(parents=True)
    (root / "settings.db").write_text("untouched")
    assert prune.remove(0, env=env).to_json() == dict(
        removed=[], skipped=[], branches=[]
    )
    assert (root / "not-a-record").is_dir()


@pytest.mark.parametrize(
    "lock_kind", ["run", "flow_lifecycle", "flow_lifetime", "panel"]
)
def test_busy_records_and_children_are_kept(
    env: dict[str, str], lock_kind: str
) -> None:
    child = run(env)
    if lock_kind == "run":
        ident = child.id
        held = runs.record_lock(child)
    elif lock_kind == "panel":
        ident = panel(env, child)["id"]
        held = panels.hold(ident, env)
    else:
        record = flow(env)
        record["tasks"][0]["writer_run"] = child.id
        flows.save(record, env)
        ident = record["id"]
        held = (
            flow_lock.hold(ident, env)
            if lock_kind == "flow_lifetime"
            else prune._file_lock(flows.folder(ident, env) / "lifecycle.lock")
        )
    with held:
        result = prune.remove(0, env=env)
    assert not result.removed
    assert {item["id"]: item["reason"] for item in result.skipped}[ident] == "busy"
    assert child.folder.exists()
    assert not result.errors


def test_reopened_after_selection_is_kept(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = run(env)
    original = prune._hold

    def hold(
        entry: prune.Entry, stack: contextlib.ExitStack, env: Mapping[str, str]
    ) -> None:
        runs.save(replace(record, status="running"))
        original(entry, stack, env)

    monkeypatch.setattr(prune, "_hold", hold)
    result = prune.remove(0, env=env)
    assert not result.removed
    assert result.skipped == [dict(id=record.id, reason="not ended")]
    assert record.folder.exists()


def test_parent_reopened_before_lock_preserves_child(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    child = run(env)
    parent = panel(env, child)
    original = prune._hold

    def hold(
        entry: prune.Entry, stack: contextlib.ExitStack, env: Mapping[str, str]
    ) -> None:
        if entry.kind == "panel":
            parent["status"] = "running"
            panels.save(parent, env)
        original(entry, stack, env)

    monkeypatch.setattr(prune, "_hold", hold)
    result = prune.remove(0, env=env)
    assert not result.removed
    assert {item["id"] for item in result.skipped} == {parent["id"], child.id}
    assert child.folder.exists()


def test_delete_failure_keeps_children_and_continues(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    child, independent = run(env), run(env)
    parent = panel(env, child)
    original = prune.shutil.rmtree

    def fail(path: Path) -> None:
        if path.name == parent["id"]:
            raise PermissionError("refused")
        original(path)

    monkeypatch.setattr(prune.shutil, "rmtree", fail)
    result = prune.remove(0, env=env)
    assert ids(result) == {independent.id}
    assert len(result.errors) == 1 and "refused" in result.errors[0]
    assert child.folder.exists()


def test_broken_parent_keeps_potential_children(env: dict[str, str]) -> None:
    child = run(env)
    parent = panel(env, child)
    (panels.folder(parent["id"], env) / "panel.json").write_text("{")
    result = prune.remove(0, env=env)
    assert not result.removed and result.errors
    assert child.folder.exists()


def test_symlink_record_is_never_followed(env: dict[str, str], tmp_path: Path) -> None:
    child = run(env)
    moved = tmp_path / "moved"
    child.folder.rename(moved)
    child.folder.symlink_to(moved, target_is_directory=True)
    result = prune.remove(0, env=env)
    assert not result.removed and result.errors
    assert (moved / "run.json").exists()


@pytest.mark.parametrize("merged", [True, False])
def test_branch_commands_worktrees_and_ancestry(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, merged: bool
) -> None:
    checkout = tmp_path / "repo with spaces"
    checkout.mkdir()
    worktree = tmp_path / "run tree"
    worktree.mkdir()
    record = run(env, read_only=False, repo=str(checkout), worktree=str(worktree))
    branch = f"ember/codex/{record.id}"
    runs.save(replace(record, branch=branch, base_branch="main"))
    calls: list[tuple[str, ...]] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert directory == str(checkout)
        calls.append(args)
        code = int(not merged) if args[0] == "merge-base" else 0
        return subprocess.CompletedProcess(
            args, code, "main\n" if args[0] == "symbolic-ref" else "", ""
        )

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, env=env)
    assert ids(result) == {record.id} and not result.errors
    assert (
        "merge-base",
        "--is-ancestor",
        f"refs/heads/{branch}",
        "refs/heads/main",
    ) in calls
    assert worktree.is_dir() and checkout.is_dir()
    if merged:
        assert result.branches == []
    else:
        command = result.branches[0]["command"]
        assert shlex.quote(str(worktree)) in command
        assert command.endswith(
            shlex.join(["git", "-C", str(checkout), "branch", "-D", branch])
        )
        assert " && " in command and "emb cleanup" not in command
    assert result.worktrees == [
        dict(
            branch=branch,
            worktree=str(worktree),
            command=shlex.join(
                ["git", "-C", str(checkout), "worktree", "remove", str(worktree)]
            ),
        )
    ]


def test_flow_and_task_branch_targets(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = flow(env, base_branch="main")
    record["tasks"][0].update(branch="ember/task/a", worktree="/absent")
    flows.save(record, env)
    calls: list[tuple[str, ...]] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, env=env)
    assert not result.errors
    assert (
        "merge-base",
        "--is-ancestor",
        f"refs/heads/{record['branch']}",
        "refs/heads/main",
    ) in calls
    assert (
        "merge-base",
        "--is-ancestor",
        "refs/heads/ember/task/a",
        f"refs/heads/{record['branch']}",
    ) in calls
    assert result.branches == [
        dict(branch=branch, command=f"git -C /checkout branch -D {branch}")
        for branch in (record["branch"], "ember/task/a")
    ]


def test_json_failure_continues_and_reports_stderr(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    bad = run(env)
    good = run(env)
    (bad.folder / "run.json").write_text("{")
    assert prune.main(0, as_json=True, env=env) == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["removed"] == [dict(id=good.id, kind="run")]
    assert data["skipped"][0]["id"] == bad.id
    assert "emb prune:" in captured.err and bad.id in captured.err
    assert set(data) == {"removed", "skipped", "branches"}


def test_parent_locks_held_until_child_removal(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    child = run(env)
    parent = panel(env, child)
    original = prune.shutil.rmtree

    def remove(path: Path) -> None:
        assert panels.live(parent["id"], env)
        if path.name == child.id:
            with (
                pytest.raises(BlockingIOError),
                prune._file_lock(child.folder / runs.LOCK_NAME),
            ):
                pass
        original(path)

    monkeypatch.setattr(prune.shutil, "rmtree", remove)
    assert ids(prune.remove(0, env=env)) == {child.id, parent["id"]}


@pytest.mark.parametrize("kind", ["run", "flow"])
@pytest.mark.parametrize("saved_branch", [None, "original"])
def test_ancestry_uses_saved_base_after_checkout_switches(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    saved_branch: str | None,
) -> None:
    if kind == "run":
        child = run(env, base_branch=saved_branch)
        runs.save(replace(child, branch=f"ember/codex/{child.id}", read_only=False))
        ident, base = child.id, child.base
    else:
        parent = flow(env, base_branch=saved_branch)
        ident, base = parent["id"], parent["base"]
        source = json.loads((flows.folder(ident, env) / flows.PLAN_NAME).read_text())
        source["base"] = "HEAD"
        (flows.folder(ident, env) / flows.PLAN_NAME).write_text(json.dumps(source))
    queries: list[tuple[str, ...]] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        queries.append(args)
        assert args[0] != "symbolic-ref"
        return subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, env=env)
    assert ids(result) == {ident} and not result.errors
    ancestry = [args for args in queries if args[0] == "merge-base"]
    if saved_branch:
        assert ancestry[0][-1] == f"refs/heads/{saved_branch}"
    else:
        assert not ancestry
        assert base is not None
        assert base not in result.branches[0]["command"]
        assert "--merged-into" not in result.branches[0]["command"]
        assert "branch -D" in result.branches[0]["command"]


def test_busy_member_prevents_panel_removal(env: dict[str, str]) -> None:
    child = run(env)
    parent = panel(env, child)
    with runs.record_lock(child):
        result = prune.remove(0, env=env)
    assert not result.removed
    assert {item["id"] for item in result.skipped} == {child.id, parent["id"]}
    assert panels.load(parent["id"], env)["status"] == "done"


def test_repository_failure_omits_command_and_continues(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    child = run(env)
    runs.save(replace(child, branch=f"ember/codex/{child.id}", read_only=False))
    other = run(env)

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(prune, "_git", git)
    assert prune.main(0, as_json=True, env=env) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert {item["id"] for item in result["removed"]} == {child.id, other.id}
    assert "git unavailable" in captured.err
    assert result["branches"] == []


def test_unmerged_task_using_run_branch_lists_deletion_once(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = flow(env, base_branch="main")
    child = run(env, base_branch=parent["branch"], repo="/flow-checkout")
    branch = f"ember/codex/{child.id}"
    runs.save(replace(child, branch=branch, read_only=False))
    parent["tasks"][0].update(branch=branch, writer_run=child.id)
    flows.save(parent, env)

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, env=env)
    assert ids(result) == {parent["id"], child.id} and not result.errors
    commands = [item["command"] for item in result.branches if item["branch"] == branch]
    assert len(commands) == 1
    assert commands == [f"git -C /checkout branch -D {branch}"]


@pytest.mark.parametrize(
    "base_exists, contained", [(True, True), (True, False), (False, False)]
)
def test_deleted_target_uses_flow_base_without_error(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    base_exists: bool,
    contained: bool,
) -> None:
    parent = flow(env, base_branch="main")
    worktree = tmp_path / "kept tree"
    worktree.mkdir()
    child = run(env, base_branch=parent["branch"], worktree=str(worktree))
    branch = f"ember/codex/{child.id}"
    runs.save(replace(child, branch=branch, read_only=False))
    parent["tasks"][0].update(
        branch=branch, writer_run=child.id, worktree=str(worktree)
    )
    flows.save(parent, env)
    queries: list[tuple[str, ...]] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        queries.append(args)
        if args[0] == "show-ref":
            present = args[-1] == f"refs/heads/{branch}" or (
                args[-1] == "refs/heads/main" and base_exists
            )
            return subprocess.CompletedProcess(args, int(not present), "", "")
        assert args[0] == "merge-base" and args[-1] == "refs/heads/main"
        return subprocess.CompletedProcess(args, int(not contained), "", "")

    monkeypatch.setattr(prune, "_git", git)
    assert prune.main(0, as_json=True, env=env) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert {item["id"] for item in result["removed"]} == {parent["id"], child.id}
    if contained:
        assert result["branches"] == []
    else:
        command = next(
            item["command"] for item in result["branches"] if item["branch"] == branch
        )
        assert "branch -D" in command and "emb cleanup" not in command
    assert all(
        args[-1] == "refs/heads/main" for args in queries if args[0] == "merge-base"
    )
    assert any(args[-1] == "refs/heads/main" for args in queries)
    assert worktree.exists()


@pytest.mark.parametrize("kind", ["flow", "panel"])
@pytest.mark.parametrize("source", ["record", "events.jsonl", "members/old.log"])
def test_broken_parent_only_protects_ids_in_its_files(
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    kind: str,
    source: str,
) -> None:
    owned, historical, unrelated = run(env), run(env), run(env)
    parent = flow(env) if kind == "flow" else panel(env)
    folder = runs.run_dir(parent["id"], env)
    record_file = folder / f"{kind}.json"
    record_file.write_text("{")
    file = record_file if source == "record" else folder / source
    file.parent.mkdir(exist_ok=True)
    file.write_bytes(
        f'broken JSON: "run": "{owned.id}", old run {historical.id}'.encode() + b"\xff"
    )
    assert prune.main(0, as_json=True, env=env) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["removed"] == [dict(id=unrelated.id, kind="run")]
    assert {item["id"] for item in result["skipped"]} == {
        owned.id,
        historical.id,
        parent["id"],
    }
    assert parent["id"] in captured.err
    assert folder.exists() and owned.folder.exists() and historical.folder.exists()


@pytest.mark.parametrize("status", ["starting", "running", "waiting"])
@pytest.mark.parametrize("role", ["writer_run", "reviewer_run"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cancelled_flow_with_open_task_has_no_worktree_command(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: str,
    role: str,
    dry_run: bool,
) -> None:
    worktree = tmp_path / "live task tree"
    worktree.mkdir()
    child = run(env, status=status, worktree=str(worktree))
    branch = f"ember/codex/{child.id}"
    runs.save(replace(child, branch=branch, read_only=False))
    parent = flow(env, status="cancelled", base_branch="main")
    parent["tasks"][0].update(
        {role: child.id, "branch": branch, "worktree": str(worktree)}
    )
    flows.save(parent, env)
    independent = run(env)
    assert prune.main(0, dry_run=dry_run, as_json=True, env=env) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["removed"] == [dict(id=independent.id, kind="run")]
    reason = next(
        item["reason"] for item in result["skipped"] if item["id"] == parent["id"]
    )
    assert child.id in reason and status in reason
    assert result["branches"] == [] and str(worktree) not in captured.out
    assert captured.err == ""
    assert runs.run_dir(parent["id"], env).exists() and child.folder.exists()


@pytest.mark.parametrize("child_state", ["busy", "unreadable"])
def test_flow_with_unavailable_child_is_kept(
    env: dict[str, str],
    tmp_path: Path,
    child_state: str,
) -> None:
    worktree = tmp_path / "task tree"
    worktree.mkdir()
    child = run(env, worktree=str(worktree))
    parent = flow(env, status="cancelled", base_branch="main")
    parent["tasks"][0].update(
        writer_run=child.id, branch=f"ember/codex/{child.id}", worktree=str(worktree)
    )
    flows.save(parent, env)
    independent = run(env)
    if child_state == "unreadable":
        (child.folder / "run.json").write_text("{")
    lock = (
        runs.record_lock(child) if child_state == "busy" else contextlib.nullcontext()
    )
    with lock:
        result = prune.remove(0, env=env)
    assert ids(result) == {independent.id}
    reason = next(
        item["reason"] for item in result.skipped if item["id"] == parent["id"]
    )
    assert child.id in reason and child_state in reason
    assert not result.branches and not result.worktrees
    assert child.folder.exists() and flows.folder(parent["id"], env).exists()
    assert bool(result.errors) == (child_state == "unreadable")


def test_flow_child_reopens_before_run_lock(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = run(env)
    parent = flow(env, status="cancelled")
    parent["tasks"][0]["writer_run"] = child.id
    flows.save(parent, env)
    original = prune._hold

    def hold(
        entry: prune.Entry, stack: contextlib.ExitStack, env: Mapping[str, str]
    ) -> None:
        if entry.id == child.id:
            runs.save(replace(child, status="running"))
        original(entry, stack, env)

    monkeypatch.setattr(prune, "_hold", hold)
    result = prune.remove(0, env=env)
    assert not result.removed and not result.branches and not result.worktrees
    reason = next(
        item["reason"] for item in result.skipped if item["id"] == parent["id"]
    )
    assert child.id in reason and "running" in reason


def test_unrelated_live_run_keeps_branch_command_without_worktree_removal(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "shared tree"
    worktree.mkdir()
    live = run(env, status="running", worktree=str(worktree))
    ended = run(env, worktree=str(worktree), base_branch="main")
    runs.save(replace(ended, branch=f"ember/codex/{ended.id}", read_only=False))

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, env=env)
    assert ids(result) == {ended.id}
    branch = f"ember/codex/{ended.id}"
    assert result.branches == [
        dict(branch=branch, command=f"git -C /checkout branch -D {branch}")
    ]
    assert not result.worktrees
    assert live.folder.exists() and worktree.exists()


def test_reopened_run_protects_shared_worktree_before_any_removal(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "shared tree"
    worktree.mkdir()
    reopening = run(env, worktree=str(worktree))
    other = run(env, worktree=str(worktree), base_branch="main")
    runs.save(replace(other, branch=f"ember/codex/{other.id}", read_only=False))
    original = prune._hold

    def hold(
        entry: prune.Entry, stack: contextlib.ExitStack, env: Mapping[str, str]
    ) -> None:
        if entry.id == reopening.id:
            runs.save(replace(reopening, status="running"))
        original(entry, stack, env)

    monkeypatch.setattr(prune, "_hold", hold)

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, env=env)
    assert ids(result) == {other.id}
    branch = f"ember/codex/{other.id}"
    assert result.branches == [
        dict(branch=branch, command=f"git -C /checkout branch -D {branch}")
    ]
    assert not result.worktrees
    assert reopening.folder.exists() and worktree.exists()


@pytest.mark.parametrize("kind", ["run", "task", "flow"])
@pytest.mark.parametrize("branch_state", ["merged", "absent"])
@pytest.mark.parametrize("as_json", [False, True])
def test_merged_and_absent_branches_are_never_listed(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    branch_state: str,
    as_json: bool,
) -> None:
    worktree = tmp_path / "kept worktree"
    worktree.mkdir()
    if kind == "run":
        record = run(env, base_branch="main", worktree=str(worktree))
        branch = f"ember/codex/{record.id}"
        runs.save(replace(record, branch=branch, read_only=False))
        ident = record.id
        target = "main"
    else:
        parent = flow(env, base_branch="main", worktree=str(worktree))
        ident = parent["id"]
        if kind == "task":
            branch = "ember/task/task-branch"
            target = parent["branch"]
            parent["tasks"][0].update(branch=branch, worktree=str(worktree))
            flows.save(parent, env)
        else:
            branch, target = parent["branch"], "main"
    queries: list[tuple[str, ...]] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        queries.append(args)
        return subprocess.CompletedProcess(
            args, int(branch_state == "absent" and args[0] == "show-ref"), "", ""
        )

    monkeypatch.setattr(prune, "_git", git)
    assert prune.main(0, as_json=as_json, env=env) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if as_json:
        assert json.loads(captured.out)["branches"] == []
        assert str(worktree) not in captured.out
    else:
        command = shlex.join(
            ["git", "-C", "/checkout", "worktree", "remove", str(worktree)]
        )
        assert f"branch {branch}\n  worktree {worktree}: {command}\n" in captured.out
        assert "branch -D" not in captured.out
    assert "emb cleanup" not in captured.out
    if branch_state == "merged":
        assert (
            "merge-base",
            "--is-ancestor",
            f"refs/heads/{branch}",
            f"refs/heads/{target}",
        ) in queries
    else:
        assert all(args[0] != "merge-base" for args in queries)
    assert not runs.run_dir(ident, env).exists()
    assert worktree.exists()


def test_flow_without_base_lists_task_as_unmerged(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = flow(env, base_branch=None)
    parent["tasks"][0]["branch"] = "ember/task/old-task"
    flows.save(parent, env)

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] == "show-ref"
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, env=env)
    assert not result.errors
    assert result.branches == [
        dict(branch=branch, command=f"git -C /checkout branch -D {branch}")
        for branch in (parent["branch"], "ember/task/old-task")
    ]


def test_removed_flow_reports_merged_task_worktree(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worktree = tmp_path / "merged task tree"
    worktree.mkdir()
    (worktree / "kept.txt").write_text("task changes")
    parent = flow(env, status="done", base_branch="main")
    child = run(env, worktree=str(worktree), base_branch=parent["branch"])
    branch = f"ember/codex/{child.id}"
    runs.save(replace(child, branch=branch, read_only=False))
    parent["tasks"][0].update(
        status="merged", branch=branch, worktree=str(worktree), writer_run=child.id
    )
    flows.save(parent, env)

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(prune, "_git", git)
    command = shlex.join(
        ["git", "-C", "/checkout", "worktree", "remove", str(worktree)]
    )
    preview = prune.remove(0, dry_run=True, env=env)
    assert ids(preview) == {parent["id"], child.id}
    assert preview.branches == []
    assert preview.worktrees == [
        dict(branch=branch, worktree=str(worktree), command=command)
    ]
    assert child.folder.exists() and flows.folder(parent["id"], env).exists()
    assert prune.main(0, env=env) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"branch {branch}\n  worktree {worktree}: {command}\n" in captured.out
    assert "branch -D" not in captured.out and "emb cleanup" not in captured.out
    assert not child.folder.exists() and not flows.folder(parent["id"], env).exists()
    assert (worktree / "kept.txt").read_text() == "task changes"
