import contextlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import paths, prune
from ember.cli import main
from ember.flows import lock as flow_lock
from ember.flows import plan
from ember.flows import record as flows
from ember.panels import record as panels
from ember.runs import record as runs

NOW = 1_000_000.0


@pytest.mark.parametrize("age", [0, 3600, 3601])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_prune_sweeps_creation_folders_after_one_hour(
    env: dict[str, str], age: int, dry_run: bool, scoped: bool
) -> None:
    root = paths.runs_dir(env)
    folder = paths.ensure_private_dir(
        root / f"{paths.CREATION_TMP_PREFIX}abandoned", root=paths.state_dir(env)
    )
    (folder / runs.RECORD_NAME).write_text("{}")
    os.utime(folder, (NOW - age, NOW - age))
    assert prune.select(0, env=env, now=NOW).errors == []
    assert runs.list_runs(env) == ([], [])
    assert flows.list_flows(env) == ([], [])
    assert panels.list_panels(env) == ([], [])
    result = prune.remove(30, [] if scoped else None, env=env, now=NOW, dry_run=dry_run)
    assert not result.errors
    candidate = age > 3600 and not scoped
    assert result.removed == ([dict(id=str(folder), kind="tmp")] if candidate else [])
    assert folder.exists() == (not candidate or dry_run)


def test_prune_creation_sweep_keeps_symlinks(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = paths.ensure_private_dir(paths.runs_dir(env), root=paths.state_dir(env))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    link = root / f"{paths.CREATION_TMP_PREFIX}link"
    link.symlink_to(outside, target_is_directory=True)
    os.utime(link, (0, 0), follow_symlinks=False)
    result = prune.remove(0, env=env, now=NOW)
    assert not result.errors and not result.removed
    assert link.is_symlink() and (outside / "keep").read_text() == "keep"


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
        "XDG_DATA_HOME": str(tmp_path / "data"),
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
        turn_state="committed",
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
        "tasks": [{"id": "a", "subject": "feat: a", "task": "Task", "files": ["a"]}],
    }
    document = plan.from_json(source, Path("/plan.json"))
    worker = plan.Worker("codex", "model", None, "option")
    record = flows.new_flow(
        ident,
        document,
        top="/checkout",
        main="/checkout",
        worktree="/data/ember/worktrees/" + ident,
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


def test_stopped_flow_keeps_its_open_run(env: dict[str, str]) -> None:
    parent = flow(env, status="stopped", ended_at=None)
    child = run(
        env,
        status=runs.WAITING,
        ended_at=None,
        parent={"command": "flow", "id": parent["id"], "pid": 0, "start": 0},
    )
    parent["tasks"][0].update(status="stopped", writer_run=child.id)
    flows.save(parent, env)
    result = prune.remove(0, env=env)
    assert result.removed == []
    assert flows.load(parent["id"], env)["status"] == "stopped"
    assert runs.load(child.id, env) == child


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


def orphan(env: dict[str, str], name: str = "orphan tree") -> Path:
    folder = paths.worktrees_dir(env) / name
    folder.mkdir(parents=True)
    (folder / ".git").write_text("gitdir: ../../removed clone/.git/worktrees/task\n")
    (folder / "work.txt").write_text("work\n")
    return folder


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("kind", ["unrecorded", "run", "flow", "task"])
def test_prune_removes_orphan_worktrees(
    env: dict[str, str], tmp_path: Path, kind: str, dry_run: bool
) -> None:
    folder = orphan(env)
    if kind == "run":
        child = run(
            env,
            repo=str(tmp_path / "gone source"),
            main=str(tmp_path / "gone clone"),
            common=str(tmp_path / "gone clone" / ".git"),
            worktree=str(folder),
            branch="ember/codex/task",
            read_only=False,
        )
        record_folder = child.folder
    elif kind in ("flow", "task"):
        parent = flow(
            env, dir=str(tmp_path / "gone clone"), main=str(tmp_path / "gone clone")
        )
        if kind == "flow":
            parent["worktree"] = str(folder)
        else:
            parent["tasks"][0].update(branch="ember/codex/task", worktree=str(folder))
        flows.save(parent, env)
        record_folder = flows.folder(parent["id"], env)
    else:
        record_folder = None
    result = prune.remove(0, dry_run=dry_run, env=env)
    assert not result.errors
    assert dict(id=str(folder), kind="worktree") in result.removed
    assert folder.exists() == dry_run
    if record_folder is not None:
        assert record_folder.exists() == dry_run
    assert not result.worktrees and not result.branches


@pytest.mark.parametrize("dry_run", [False, True])
def test_scoped_prune_does_not_sweep_unrecorded_worktrees(
    env: dict[str, str], dry_run: bool
) -> None:
    folder = orphan(env)
    child = run(env)
    result = prune.remove(0, [child.id], dry_run=dry_run, env=env)
    assert str(folder) not in ids(result)
    assert folder.is_dir()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_prune_lists_unrecorded_orphans(
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    as_json: bool,
) -> None:
    folder = orphan(env)
    assert prune.main(0, dry_run=dry_run, as_json=as_json, env=env) == 0
    captured = capsys.readouterr()
    assert not captured.err
    if as_json:
        assert json.loads(captured.out)["removed"] == [
            dict(id=str(folder), kind="worktree")
        ]
    else:
        verb = "would remove" if dry_run else "removed"
        assert captured.out == f"{verb} worktree {folder}\n"
    assert folder.exists() == dry_run


@pytest.mark.parametrize("offset, removed", [(86400, True), (86399, False)])
def test_unrecorded_worktree_age_is_its_modification_time(
    env: dict[str, str], offset: int, removed: bool
) -> None:
    folder = orphan(env)
    os.utime(folder, (NOW - offset, NOW - offset))
    preview = prune.remove(1, dry_run=True, env=env, now=NOW)
    result = prune.remove(1, env=env, now=NOW)
    assert preview.to_json() == result.to_json()
    assert (str(folder) in ids(result)) == removed
    assert folder.exists() != removed


@pytest.mark.parametrize("kind", ["run", "flow", "task"])
def test_sweep_keeps_folders_named_by_unselected_records(
    env: dict[str, str], kind: str
) -> None:
    folder = orphan(env)
    if kind == "run":
        run(env, worktree=str(folder), status="running")
    else:
        parent = flow(env, status="running")
        if kind == "flow":
            parent["worktree"] = str(folder)
        else:
            parent["tasks"][0]["worktree"] = str(folder)
        flows.save(parent, env)
    result = prune.remove(0, env=env)
    assert not result.errors and not result.removed
    assert folder.is_dir()


@pytest.mark.parametrize("kind", ["run", "flow", "panel", "invalid-id"])
def test_sweep_keeps_folders_when_any_record_is_unreadable(
    env: dict[str, str], kind: str
) -> None:
    folder = orphan(env)
    if kind == "run":
        broken = run(env).folder / runs.RECORD_NAME
    elif kind == "flow":
        broken = flows.folder(flow(env)["id"], env) / flows.FLOW_NAME
    elif kind == "panel":
        broken = panels.folder(panel(env)["id"], env) / "panel.json"
    else:
        broken = paths.runs_dir(env) / "unknown" / "run.json"
        broken.parent.mkdir(parents=True)
    broken.write_text("{")
    result = prune.remove(0, env=env)
    assert result.errors and not result.removed
    assert folder.is_dir()
    assert (
        dict(id=str(folder), reason="cannot establish ownership: unreadable records")
        in result.skipped
    )


@pytest.mark.parametrize(
    "pointer",
    [
        "",
        "garbage",
        "gitdir: \n",
        "gitdir: gone\nextra",
        "gitdir: gone\rextra",
        "gitdir: x\0y",
    ],
)
def test_sweep_keeps_invalid_git_files(env: dict[str, str], pointer: str) -> None:
    folder = orphan(env)
    (folder / ".git").write_text(pointer)
    result = prune.remove(0, env=env)
    assert not result.removed and folder.is_dir()
    assert dict(id=str(folder), reason="invalid .git file") in result.skipped


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("ending", ["", "\n", "\r\n", "\r"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_keeps_metadata_paths_with_trailing_spaces(
    env: dict[str, str],
    tmp_path: Path,
    recorded: bool,
    relative: bool,
    ending: str,
    dry_run: bool,
) -> None:
    folder = orphan(env)
    metadata = tmp_path / "metadata "
    metadata.mkdir()
    name = os.path.relpath(metadata, folder) if relative else str(metadata)
    (folder / ".git").write_bytes(os.fsencode(f"gitdir: {name}{ending}"))
    if recorded:
        run(
            env,
            worktree=str(folder),
            common=str(metadata),
            branch="ember/codex/task",
            read_only=False,
        )
    result = prune.remove(0, env=env, dry_run=dry_run)
    assert not result.errors
    assert not any(item["kind"] == "worktree" for item in result.removed)
    assert (folder / "work.txt").read_text() == "work\n"
    if not recorded:
        assert any(
            item["id"] == str(folder)
            and item["reason"].startswith("git folder still exists:")
            for item in result.skipped
        )


@pytest.mark.parametrize("pointer", ["missing", "directory", "symlink", "live"])
def test_sweep_keeps_folders_without_a_missing_git_folder(
    env: dict[str, str], tmp_path: Path, pointer: str
) -> None:
    folder = orphan(env)
    dot_git = folder / ".git"
    dot_git.unlink()
    if pointer == "directory":
        dot_git.mkdir()
    elif pointer in ("symlink", "live"):
        metadata = tmp_path / "metadata"
        metadata.mkdir()
        if pointer == "live":
            dot_git.write_text(f"gitdir: {metadata}\n")
        else:
            dot_git.symlink_to(metadata)
    result = prune.remove(0, env=env)
    assert not result.removed and folder.is_dir()
    assert any(item["id"] == str(folder) and item["reason"] for item in result.skipped)


def test_sweep_keeps_symlinks_and_external_recorded_worktrees(
    env: dict[str, str], tmp_path: Path
) -> None:
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / ".git").write_text(f"gitdir: {tmp_path / 'gone'}\n")
    root = paths.worktrees_dir(env)
    root.mkdir(parents=True)
    link = root / "linked"
    link.symlink_to(outside, target_is_directory=True)
    run(
        env,
        worktree=str(outside),
        main=str(tmp_path / "gone clone"),
        common=str(tmp_path / "gone clone" / ".git"),
        branch="ember/codex/task",
    )
    result = prune.remove(0, env=env)
    assert outside.is_dir() and link.is_symlink()
    assert not any(item["kind"] == "worktree" for item in result.removed)
    assert any(item["id"] == str(outside) for item in result.skipped)


@pytest.mark.parametrize("main_exists", [False, True])
@pytest.mark.parametrize("kind", ["run", "flow"])
@pytest.mark.parametrize("source_exists", [False, True])
def test_prune_uses_recorded_checkout_without_discovering_from_worktree(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    main_exists: bool,
    kind: str,
    source_exists: bool,
) -> None:
    main = tmp_path / "main"
    common = tmp_path / "metadata"
    common.mkdir()
    if main_exists:
        main.mkdir()
    source = tmp_path / "reused source"
    if source_exists:
        source.mkdir()
    folder = orphan(env)
    (folder / ".git").write_text(f"gitdir: {common}\n")
    changes = dict(
        main=str(main),
        common=str(common),
        worktree=str(folder),
        branch="ember/codex/task",
    )
    if kind == "run":
        run(env, repo=str(source), **changes)
    else:
        flow(env, dir=str(source), **changes)
    queried: list[str] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] != "worktree"
        queried.append(directory)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, dry_run=True, env=env)
    assert not result.errors
    assert set(queried) == {str(main if main_exists else common)}


@pytest.mark.parametrize("status", runs.STATUSES)
def test_only_ended_or_abandoned_runs(env: dict[str, str], status: str) -> None:
    record = run(env, status=status)
    result = prune.remove(0, env=env, now=NOW)
    ended = status in ("closed", "failed", "starting")
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
        removed=[], skipped=[], branches=[], worktrees=[]
    )
    root = paths.runs_dir(env)
    (root / "not-a-record").mkdir(parents=True)
    (root / "settings.db").write_text("untouched")
    assert prune.remove(0, env=env).to_json() == dict(
        removed=[], skipped=[], branches=[], worktrees=[]
    )
    assert (root / "not-a-record").is_dir()


@pytest.mark.parametrize(
    "lock_kind", ["run", "run_command", "flow_lifecycle", "flow_lifetime", "panel"]
)
def test_busy_records_and_children_are_kept(
    env: dict[str, str], lock_kind: str
) -> None:
    child = run(env)
    if lock_kind == "run":
        ident = child.id
        held = runs.record_lock(child)
    elif lock_kind == "run_command":
        ident = child.id
        root = paths.runtime_dir(env)
        folder = paths.ensure_private_dir(root / "locks", root=root)
        held = prune._file_lock(folder / f"{ident}.lock")
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
    assert set(data) == {"removed", "skipped", "branches", "worktrees"}
    assert data["worktrees"] == []


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


@pytest.mark.parametrize("legacy", [True, False])
def test_run_whose_repo_folder_is_gone_uses_the_main_checkout(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    main = tmp_path / "main"
    main.mkdir()
    child = run(env, base_branch="main")
    if legacy:
        worktree = main / ".ember" / "worktrees" / child.id
    else:
        worktree = paths.worktree_path(child.id, env)
    worktree.mkdir(parents=True)
    runs.save(
        replace(
            child,
            repo=str(tmp_path / "removed-flow-worktree"),
            worktree=str(worktree),
            branch=f"ember/codex/{child.id}",
            read_only=False,
        )
    )
    folders: list[str] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "rev-parse":
            assert directory == str(worktree)
            return subprocess.CompletedProcess(args, 0, str(main / ".git"), "")
        if args[0] == "worktree":
            assert directory == str(worktree)
            listing = f"worktree {main}\0HEAD {'a' * 40}\0branch refs/heads/main\0\0"
            return subprocess.CompletedProcess(args, 0, listing, "")
        folders.append(directory)
        return subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, dry_run=True, env=env)
    assert not result.errors
    assert set(folders) == {str(main)}
    assert (
        f"git -C {main} branch -D ember/codex/{child.id}"
        in (result.branches[0]["command"])
    )


def test_task_run_whose_flow_worktree_is_gone_uses_the_flow_main_checkout(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    main.mkdir()
    gone = str(tmp_path / "removed-flow-worktree")
    child = run(env)
    branch = f"ember/codex/{child.id}"
    runs.save(replace(child, repo=gone, worktree=gone, branch=branch, read_only=False))
    parent = flow(env, dir=str(main), main=str(main))
    parent["tasks"][0].update(branch=branch, writer_run=child.id, worktree=gone)
    flows.save(parent, env)
    folders: list[str] = []

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] != "worktree"
        folders.append(directory)
        return subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", "")

    monkeypatch.setattr(prune, "_git", git)
    result = prune.remove(0, dry_run=True, env=env)
    assert not result.errors
    assert set(folders) == {str(main)}


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
        if args[0] in ("rev-parse", "worktree"):
            return subprocess.CompletedProcess(args, 128, "", "not a repository")
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
    root = paths.runtime_dir(env)
    locks = paths.ensure_private_dir(root / "locks", root=root)
    held = (
        prune._file_lock(locks / f"{child.id}.lock")
        if status == runs.STARTING
        else contextlib.nullcontext()
    )
    with held:
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
    command = shlex.join(
        ["git", "-C", "/checkout", "worktree", "remove", str(worktree)]
    )
    if as_json:
        data = json.loads(captured.out)
        assert data["branches"] == []
        assert (
            dict(branch=branch, worktree=str(worktree), command=command)
            in data["worktrees"]
        )
    else:
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


@pytest.mark.parametrize("dry_run", [False, True])
def test_json_worktrees_match_text(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    expected: set[tuple[str, str, str]] = set()
    records: list[runs.Run] = []
    for state in ("merged", "absent", "unmerged"):
        worktree = tmp_path / f"{state} worktree"
        worktree.mkdir()
        branch = f"ember/{state}"
        records.append(
            run(
                env,
                branch=branch,
                base_branch="main",
                read_only=False,
                worktree=str(worktree),
            )
        )
        command = shlex.join(
            ["git", "-C", "/checkout", "worktree", "remove", str(worktree)]
        )
        expected.add((branch, str(worktree), command))

    def git(
        directory: str, *args: str, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        if args[0] in ("rev-parse", "worktree"):
            code = 128
        elif args[0] == "show-ref":
            code = int(args[-1] == "refs/heads/ember/absent")
        else:
            assert args[0] == "merge-base"
            code = int(args[-2] == "refs/heads/ember/unmerged")
        return subprocess.CompletedProcess(args, code, "", "")

    monkeypatch.setattr(prune, "_git", git)
    assert prune.main(0, dry_run=True, env=env) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    text_worktrees: set[tuple[str, str, str]] = set()
    branch = ""
    for line in captured.out.splitlines():
        if line.startswith("branch "):
            branch = line.removeprefix("branch ").split(": ", 1)[0]
        elif line.startswith("  worktree "):
            worktree_path, command = line.removeprefix("  worktree ").split(": ", 1)
            text_worktrees.add((branch, worktree_path, command))
    assert text_worktrees == expected

    assert prune.main(0, dry_run=dry_run, as_json=True, env=env) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    data = json.loads(captured.out)
    assert {
        (item["branch"], item["worktree"], item["command"])
        for item in data["worktrees"]
    } == text_worktrees
    assert len(data["worktrees"]) == len(expected)
    assert len(data["branches"]) == 1
    assert data["branches"][0]["branch"] == "ember/unmerged"
    for record in records:
        assert record.folder.exists() == dry_run
        assert Path(record.worktree).is_dir()


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


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
def test_prune_detached_worktrees(tmp_path: Path, dry_run: bool, dirty: bool) -> None:
    from runs.stub_engine import (  # pyrefly: ignore[missing-import]
        git,
        make_env,
        make_repo,
    )

    environment = make_env(tmp_path)
    checkout = make_repo(tmp_path, environment)
    child = run(
        environment,
        detached=True,
        repo=str(checkout),
        main=str(checkout),
        common=str(checkout / ".git"),
        base=git(checkout, "rev-parse", "HEAD", env=environment),
    )
    worktree = paths.worktree_path(child.id, environment)
    paths.ensure_worktrees_dir(environment)
    git(checkout, "worktree", "add", "--detach", str(worktree), env=environment)
    child = replace(child, worktree=str(worktree))
    runs.save(child)
    if dirty:
        (worktree / "evidence.txt").write_text("keep\n")
    result = prune.remove(0, env=environment, dry_run=dry_run)
    assert worktree.exists() == (dry_run or dirty)
    assert child.folder.exists() == (dry_run or dirty)
    listing = git(checkout, "worktree", "list", env=environment)
    assert (str(worktree) in listing) == (dry_run or dirty)
    if dirty:
        assert child.id not in ids(result)
        assert any(
            f"git worktree remove --force {worktree}" in e for e in result.errors
        )
    else:
        assert not result.errors
        assert {child.id, str(worktree)} <= ids(result)


@pytest.mark.parametrize("parent_kind", [None, "panel", "flow"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_keeps_uncertain_delivery_worktree(
    tmp_path: Path, parent_kind: str | None, dry_run: bool
) -> None:
    from runs.stub_engine import (  # pyrefly: ignore[missing-import]
        git,
        make_env,
        make_repo,
    )

    environment = make_env(tmp_path)
    checkout = make_repo(tmp_path, environment)
    child = run(
        environment,
        status=runs.FAILED,
        turn_state=runs.SENT,
        detached=True,
        last_reason="capacity retry delivery is uncertain; ask the lead",
        repo=str(checkout),
        main=str(checkout),
        common=str(checkout / ".git"),
        base=git(checkout, "rev-parse", "HEAD", env=environment),
    )
    worktree = paths.worktree_path(child.id, environment)
    paths.ensure_worktrees_dir(environment)
    git(checkout, "worktree", "add", "--detach", str(worktree), env=environment)
    child = replace(child, worktree=str(worktree))
    runs.save(child)
    parent_id = None
    if parent_kind == "panel":
        parent = panel(environment, child)
        parent_id = parent["id"]
    elif parent_kind == "flow":
        parent = flow(environment)
        parent.update(status="done", ended_at=0.0)
        parent["tasks"][0]["writer_run"] = child.id
        flows.save(parent, environment)
        parent_id = parent["id"]
    result = prune.remove(0, env=environment, dry_run=dry_run)
    assert not result.errors
    assert child.folder.exists() and worktree.exists()
    assert child.id not in ids(result)
    assert any("delivery is uncertain" in item["reason"] for item in result.skipped)
    if parent_id:
        assert runs.run_dir(parent_id, environment).exists()
        assert parent_id not in ids(result)
    assert str(worktree) in git(checkout, "worktree", "list", env=environment)


@pytest.mark.parametrize("status", [runs.FAILED, runs.CLOSED])
@pytest.mark.parametrize("pending", ["sent", "collected", "session"])
def test_prune_keeps_pending_runs(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, status: str, pending: str
) -> None:
    from ember import tmux

    child = run(
        env,
        status=status,
        turn_state=runs.SENT if pending == "sent" else runs.COLLECTED,
        read_only=pending == "session",
    )
    monkeypatch.setattr(tmux, "exists", lambda *args, **kwargs: pending == "session")
    monkeypatch.setattr(tmux, "pane_dead", lambda *args, **kwargs: False)
    result = prune.remove(0, env=env)
    assert not result.removed and not result.errors
    assert child.folder.exists()
    assert result.skipped[0]["id"] == child.id
    expected = pending if pending != "session" else "still running"
    assert expected in result.skipped[0]["reason"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_ends_recorded_preparation_group(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    from ember import prepare

    child = run(
        env,
        preparation={
            "state": "running",
            "command": "sleep 60",
            "log": "/run/prepare.log",
            "pid": 123,
            "start": 456,
        },
    )
    calls = []
    monkeypatch.setattr(prepare, "end_group", lambda *identity: calls.append(identity))
    result = prune.remove(0, [child.id], env=env, now=NOW, dry_run=dry_run)
    assert not result.errors
    assert {"id": child.id, "kind": "run"} in result.removed
    assert calls == ([] if dry_run else [(123, 456)])


@contextlib.contextmanager
def live_preparation() -> Iterator[subprocess.Popen[bytes]]:
    with subprocess.Popen(["/bin/sleep", "60"], start_new_session=True) as process:
        try:
            yield process
        finally:
            process.kill()
            process.wait(timeout=5)


def starting_run(
    env: dict[str, str], process: subprocess.Popen[bytes], kind: str
) -> runs.Run:
    from ember import prepare

    ticks = prepare.start_ticks(process.pid)
    assert ticks is not None
    return run(
        env,
        status=runs.STARTING,
        turn=0,
        turn_state=None,
        ended_at=None,
        updated_at=0.0,
        last_exit=None,
        read_only=kind != "editing",
        parent={"command": "gapcheck", "id": None, "pid": 0, "start": 0}
        if kind == "gapcheck"
        else None,
        preparation={
            "state": "running",
            "command": "/bin/sleep 60",
            "log": "/run/prepare.log",
            "pid": process.pid,
            "start": ticks,
        },
    )


@contextlib.contextmanager
def held_command_lock(child: runs.Run) -> Iterator[None]:
    root = paths.runtime_dir(child.env)
    locks = paths.ensure_private_dir(root / "locks", root=root)
    script = (
        "import fcntl, sys; "
        "stream = open(sys.argv[1], 'a'); "
        "fcntl.flock(stream, fcntl.LOCK_EX); "
        "print('locked', flush=True); sys.stdin.read()"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(locks / f"{child.id}.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline() == "locked\n"
            yield
        finally:
            holder.kill()
            holder.wait(timeout=5)


@pytest.mark.parametrize("kind", ["editing", "read-only", "gapcheck"])
@pytest.mark.parametrize("scoped", [False, True])
def test_prune_fails_abandoned_preparation_under_command_lock(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, kind: str, scoped: bool
) -> None:
    with live_preparation() as process:
        child = starting_run(env, process, kind)
        saved = []
        original = runs.save

        def save(failed: runs.Run) -> None:
            root = paths.runtime_dir(env)
            with (
                pytest.raises(BlockingIOError),
                prune._file_lock(root / "locks" / f"{child.id}.lock"),
            ):
                pass
            with (
                pytest.raises(BlockingIOError),
                prune._file_lock(child.folder / runs.LOCK_NAME),
            ):
                pass
            assert process.wait(timeout=5) == -9
            saved.append(failed)
            original(failed)

        monkeypatch.setattr(runs, "save", save)
        result = prune.remove(1, [child.id] if scoped else None, env=env, now=NOW)
        assert not result.errors and not result.skipped
        assert len(saved) == 1
        failed = saved[0]
        assert failed.status == runs.FAILED and failed.last_exit == 79
        assert "abandoned" in (failed.last_reason or "")
        assert "preparation" in (failed.last_reason or "")
        assert failed.last_start_failure and failed.ended_at is not None
        assert failed.preparation is not None
        assert failed.preparation["state"] == "interrupted"
        assert {"id": child.id, "kind": "run"} in result.removed
        assert result.failed == [{"id": child.id, "reason": failed.last_reason}]
        assert not child.folder.exists()


@pytest.mark.parametrize("kind", ["editing", "read-only", "gapcheck"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_keeps_starting_preparation_with_command_lock_in_another_process(
    env: dict[str, str], kind: str, dry_run: bool
) -> None:
    with live_preparation() as process:
        child = starting_run(env, process, kind)
        before = (child.folder / runs.RECORD_NAME).read_bytes()
        with held_command_lock(child):
            result = prune.remove(0, [child.id], env=env, now=NOW, dry_run=dry_run)
        assert not result.errors and not result.removed and not result.failed
        assert process.poll() is None
        assert (child.folder / runs.RECORD_NAME).read_bytes() == before
        assert runs.load(child.id, env).status == runs.STARTING


@pytest.mark.parametrize("kind", ["editing", "read-only", "gapcheck"])
@pytest.mark.parametrize("as_json", [False, True])
def test_prune_previews_abandoned_preparation_without_ending_it(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], kind: str, as_json: bool
) -> None:
    with live_preparation() as process:
        child = starting_run(env, process, kind)
        before = (child.folder / runs.RECORD_NAME).read_bytes()
        assert prune.main(0, dry_run=True, as_json=as_json, env=env) == 0
        output = capsys.readouterr()
        assert not output.err and process.poll() is None
        assert (child.folder / runs.RECORD_NAME).read_bytes() == before
        if as_json:
            data = json.loads(output.out)
            assert data["failed"][0]["id"] == child.id
            assert "abandoned" in data["failed"][0]["reason"]
            assert {"id": child.id, "kind": "run"} in data["removed"]
        else:
            assert (
                f"would fail {child.id}: abandoned worktree preparation" in output.out
            )
            assert f"would remove run {child.id}" in output.out


def test_prune_abandons_start_but_keeps_a_live_worker_session(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ember import tmux

    with live_preparation() as process:
        child = starting_run(env, process, "read-only")
        monkeypatch.setattr(tmux, "exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(tmux, "pane_dead", lambda *args, **kwargs: False)
        result = prune.remove(0, [child.id], env=env)
        assert not result.errors and not result.removed
        assert process.wait(timeout=5) == -9
        failed = runs.load(child.id, env)
        assert failed.status == runs.FAILED and failed.last_exit == 79
        assert "still running" in result.skipped[0]["reason"]


def test_prune_abandoned_start_checks_process_identity(env: dict[str, str]) -> None:
    with live_preparation() as process:
        child = starting_run(env, process, "editing")
        assert child.preparation is not None
        runs.save(
            replace(
                child,
                preparation={
                    **child.preparation,
                    "start": child.preparation["start"] + 1,
                },
            )
        )
        result = prune.remove(0, [child.id], env=env)
        assert not result.errors and child.id in ids(result)
        assert process.poll() is None


@pytest.mark.parametrize("kind", ["flow", "panel"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_ended_parent_with_abandoned_preparation(
    env: dict[str, str], kind: str, dry_run: bool
) -> None:
    with live_preparation() as process:
        child = starting_run(env, process, "read-only")
        if kind == "panel":
            parent_id = panel(env, child)["id"]
        else:
            parent = flow(env, status="cancelled")
            parent["tasks"][0]["writer_run"] = child.id
            flows.save(parent, env)
            parent_id = parent["id"]
        result = prune.remove(0, [parent_id], env=env, dry_run=dry_run)
        assert not result.errors and not result.skipped
        assert child.id in ids(result) and parent_id in ids(result)
        if dry_run:
            assert process.poll() is None
            assert runs.load(child.id, env).status == runs.STARTING
        else:
            assert process.wait(timeout=5) == -9


@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_removes_run_temporary_folder_with_record(
    env: dict[str, str], tmp_path: Path, dry_run: bool
) -> None:
    child = run(env)
    folder = paths.ensure_run_tmp_dir(child.id, env)
    (folder / "file").write_text("temporary")
    outside = tmp_path / "keep"
    outside.write_text("keep")
    (folder / "link").symlink_to(outside)
    result = prune.remove(0, [child.id], env=env, dry_run=dry_run)
    assert not result.errors
    assert dict(id=str(folder), kind="tmp") in result.removed
    assert folder.exists() == dry_run
    assert child.folder.exists() == dry_run
    assert outside.read_text() == "keep"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_prune_removes_flow_check_folder_with_record(
    env: dict[str, str], dry_run: bool, scoped: bool
) -> None:
    ident = flow(env)["id"]
    folder = paths.ensure_check_tmp_dir(ident)
    orphan = paths.ensure_check_tmp_dir("flow-20260101-000000-123abc")
    result = prune.remove(0, [ident] if scoped else None, env=env, dry_run=dry_run)
    assert not result.errors
    assert dict(id=str(folder), kind="tmp") in result.removed
    assert folder.exists() == dry_run
    assert flows.folder(ident, env).exists() == dry_run
    assert orphan.is_dir() and str(orphan) not in ids(result)


def test_prune_refuses_check_symlink_before_removing_orphan_worktree(
    env: dict[str, str], tmp_path: Path
) -> None:
    tree = orphan(env)
    ident = flow(env, worktree=str(tree))["id"]
    target = tmp_path / "outside"
    target.mkdir()
    check = paths.check_tmp_dir(ident)
    check.symlink_to(target)
    for dry_run in (True, False):
        result = prune.remove(0, [ident], env=env, dry_run=dry_run)
        assert not result.removed
        assert [item["id"] for item in result.skipped] == [ident]
        assert len(result.errors) == 1 and str(check) in result.errors[0]
        assert flows.folder(ident, env).is_dir()
        assert (tree / "work.txt").read_text() == "work\n"
        assert check.is_symlink() and target.is_dir()


def test_prune_reports_removed_artifacts_when_flow_record_removal_fails(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = orphan(env)
    ident = flow(env, worktree=str(tree))["id"]
    folder = flows.folder(ident, env)
    check = paths.ensure_check_tmp_dir(ident)
    rmtree = prune.shutil.rmtree

    def refuse_record(path: Path | str, *, dir_fd: int | None = None) -> None:
        if path == folder:
            raise PermissionError("record removal refused")
        rmtree(path, dir_fd=dir_fd)

    monkeypatch.setattr(prune.shutil, "rmtree", refuse_record)
    result = prune.remove(0, [ident], env=env)
    assert result.errors == [f"{ident}: record removal refused"]
    assert ids(result) == {str(tree), str(check)}
    assert not tree.exists() and not check.exists() and folder.is_dir()


@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_sweeps_unrecorded_temporary_entries(
    env: dict[str, str], tmp_path: Path, dry_run: bool
) -> None:
    live = run(env, status=runs.WAITING, ended_at=None)
    kept = paths.ensure_run_tmp_dir(live.id, env)
    orphaned = paths.ensure_run_tmp_dir("codex-20260101-000000-aabbcc", env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    link = orphaned.parent / "ember-112233"
    link.symlink_to(outside, target_is_directory=True)
    (orphaned / "link").symlink_to(outside, target_is_directory=True)
    result = prune.remove(0, env=env, dry_run=dry_run)
    assert not result.errors
    assert kept.is_dir()
    assert orphaned.exists() == dry_run
    assert link.is_symlink()
    assert (outside / "keep").read_text() == "keep"
    assert str(orphaned) in ids(result)
    assert str(link) not in ids(result)


@pytest.mark.parametrize("unreadable", [False, True])
def test_prune_keeps_unrecorded_temporary_folders_when_scoped_or_unreadable(
    env: dict[str, str], unreadable: bool
) -> None:
    child = run(env)
    folder = paths.ensure_run_tmp_dir("codex-20260101-000000-aabbcc", env)
    if unreadable:
        (child.folder / runs.RECORD_NAME).write_text("{")
    result = prune.remove(0, None if unreadable else [child.id], env=env)
    assert folder.is_dir()
    assert str(folder) not in ids(result)
    assert bool(result.errors) == unreadable


def test_prune_temporary_sweep_respects_age(env: dict[str, str]) -> None:
    old = paths.ensure_run_tmp_dir("codex-20260101-000000-aabbcc", env)
    young = paths.ensure_run_tmp_dir("codex-20260101-000000-ddeeff", env)
    os.utime(old, (0, 0))
    os.utime(young, (NOW, NOW))
    result = prune.remove(1, env=env, now=NOW)
    assert not result.errors
    assert not old.exists()
    assert young.is_dir()


def test_prune_temporary_sweep_refuses_symlinked_parent(
    env: dict[str, str], tmp_path: Path
) -> None:
    parent = paths.RUN_TMP_ROOT
    parent.rmdir()
    outside = tmp_path / "outside"
    (outside / "orphan").mkdir(parents=True)
    parent.symlink_to(outside, target_is_directory=True)
    result = prune.remove(0, env=env)
    assert result.errors
    assert (outside / "orphan").is_dir()


@pytest.mark.parametrize("dry_run", [False, True])
def test_prune_temporary_sweep_keeps_nonmatching_and_foreign_entries(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    root = paths.RUN_TMP_ROOT
    for name in (
        "ember-aabbcc",
        "ember-12345",
        "ember-1234567",
        "ember-ABCDEF",
        "other",
    ):
        (root / name).mkdir()
    (root / "ember-112233").write_text("keep")
    # All files belong to the real uid; simulate a different pruning uid.
    monkeypatch.setattr(os, "getuid", lambda: 987654)
    result = prune.remove(0, env=env, dry_run=dry_run)
    assert not result.errors
    assert not ids(result)
    assert (root / "ember-aabbcc").is_dir()


def test_prune_temporary_sweep_only_removes_matching_folders(
    env: dict[str, str],
) -> None:
    root = paths.RUN_TMP_ROOT
    for name in (
        "ember-aabbcc",
        "ember-12345",
        "ember-1234567",
        "ember-ABCDEF",
        "other",
    ):
        (root / name).mkdir()
    (root / "ember-112233").write_text("keep")
    result = prune.remove(0, env=env)
    assert not result.errors
    assert ids(result) == {str(root / "ember-aabbcc")}
    assert {entry.name for entry in root.iterdir()} == {
        "ember-12345",
        "ember-1234567",
        "ember-ABCDEF",
        "other",
        "ember-112233",
    }


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("mode", ["unscoped", "scoped", "unreadable"])
def test_prune_sweeps_020_cache_temporary_entries(
    env: dict[str, str], tmp_path: Path, dry_run: bool, mode: str
) -> None:
    child = run(env, status=runs.WAITING, ended_at=None)
    root = paths.cache_dir(env) / "tmp"
    root.mkdir(parents=True)
    kept = root / child.id
    kept.mkdir()
    orphan = root / "codex-20260101-000000-aabbcc"
    orphan.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    link = root / "codex-20260101-000000-ddeeff"
    link.symlink_to(outside, target_is_directory=True)
    if mode == "unreadable":
        (child.folder / runs.RECORD_NAME).write_text("{")
    result = prune.remove(
        0, [child.id] if mode == "scoped" else None, env=env, dry_run=dry_run
    )
    assert bool(result.errors) == (mode == "unreadable")
    assert kept.is_dir()
    removed = mode == "unscoped" and not dry_run
    assert orphan.exists() is not removed
    assert link.is_symlink() is not removed
    assert (outside / "keep").read_text() == "keep"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_claim_prunes_records_and_children(
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    as_json: bool,
) -> None:
    child = run(env)
    parent = flow(env, claim="mine")
    parent["tasks"][0]["writer_run"] = child.id
    flows.save(parent, env)
    member = run(env)
    group = panel(env, member, claim="mine")
    standalone = run(env, claim="mine")
    opened = run(env, claim="mine", status=runs.WAITING, ended_at=None)
    other_child = run(env)
    other_flow = flow(env, claim="other")
    other_flow["tasks"][0]["writer_run"] = other_child.id
    flows.save(other_flow, env)
    other_panel = panel(env, claim="other")
    other_run = run(env, claim="other")
    unclaimed = run(env)
    arguments = ["prune", "--claim", "mine"]
    if dry_run:
        arguments.append("--dry-run")
    if as_json:
        arguments.append("--json")
    assert main(arguments, env=env) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    removed = {child.id, parent["id"], member.id, group["id"], standalone.id}
    if as_json:
        result = json.loads(captured.out)
        assert {item["id"] for item in result["removed"]} == removed
        assert result["skipped"] == [dict(id=opened.id, reason="not ended")]
    else:
        verb = "would remove" if dry_run else "removed"
        for ident in removed:
            assert any(
                verb in line and ident in line for line in captured.out.splitlines()
            )
        assert f"skipped {opened.id}: not ended" in captured.out
    for ident in removed:
        assert runs.run_dir(ident, env).exists() == dry_run
    for ident in (
        opened.id,
        other_child.id,
        other_flow["id"],
        other_panel["id"],
        other_run.id,
        unclaimed.id,
    ):
        assert runs.run_dir(ident, env).exists()


@pytest.mark.parametrize("parent_kind", ["flow", "panel"])
@pytest.mark.parametrize("age", [86400, 86399])
def test_claim_age_filters_parents_before_expanding_children(
    env: dict[str, str], parent_kind: str, age: int
) -> None:
    child = run(env, ended_at=NOW, claim="mine")
    if parent_kind == "flow":
        parent = flow(env, claim="mine", ended_at=NOW - age)
        parent["tasks"][0]["writer_run"] = child.id
        flows.save(parent, env)
    else:
        parent = panel(env, child, claim="mine", ended_at=NOW - age)
    old = run(env, claim="mine", ended_at=NOW - 86400)
    young = run(env, claim="mine", ended_at=NOW - 86399)
    result = prune.remove(1, claim="mine", env=env, now=NOW)
    expected = {old.id, parent["id"], child.id} if age == 86400 else {old.id}
    assert ids(result) == expected
    assert not result.errors
    assert not any(item["id"] in expected for item in result.skipped)
    assert dict(id=young.id, reason="younger than the requested age") in result.skipped
    assert young.folder.exists()
    assert child.folder.exists() == (age < 86400)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("has_record", [False, True])
def test_claim_never_sweeps_orphans(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    has_record: bool,
) -> None:
    tree = orphan(env)
    record = run(env, claim="mine") if has_record else None

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("claim pruning ran an orphan sweep")

    monkeypatch.setattr(prune, "_sweep_orphans", unexpected)
    monkeypatch.setattr(prune, "_sweep_tmp", unexpected)
    result = prune.remove(0, claim="mine", env=env, dry_run=dry_run)
    assert ids(result) == ({record.id} if record else set())
    assert not result.errors
    assert tree.exists()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("parent_kind", [None, "flow", "panel"])
def test_claim_keeps_unlocked_starting_records(
    env: dict[str, str], dry_run: bool, parent_kind: str | None
) -> None:
    with live_preparation() as process:
        child = replace(starting_run(env, process, "editing"), claim="mine")
        runs.save(child)
        parent_id = None
        if parent_kind == "flow":
            parent = flow(env, claim="mine")
            parent["tasks"][0]["writer_run"] = child.id
            flows.save(parent, env)
            parent_id = parent["id"]
        elif parent_kind == "panel":
            parent_id = panel(env, child, claim="mine")["id"]
        before = (child.folder / runs.RECORD_NAME).read_bytes()
        result = prune.remove(0, claim="mine", env=env, dry_run=dry_run)
        assert not result.removed and not result.failed and not result.errors
        assert dict(id=child.id, reason="not ended") in result.skipped
        assert (child.folder / runs.RECORD_NAME).read_bytes() == before
        assert process.poll() is None
        if parent_id:
            assert runs.run_dir(parent_id, env).exists()


@pytest.mark.parametrize("busy", [False, True])
def test_claim_keeps_busy_or_unreadable_children(
    env: dict[str, str], busy: bool
) -> None:
    child = run(env)
    parent = panel(env, child, claim="mine")
    with contextlib.ExitStack() as stack:
        if busy:
            stack.enter_context(held_command_lock(child))
        else:
            (child.folder / runs.RECORD_NAME).write_text("{")
        result = prune.remove(0, claim="mine", env=env)
    assert not result.removed
    assert bool(result.errors) != busy
    assert child.folder.exists()
    assert runs.run_dir(parent["id"], env).exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_claim_rechecks_starting_state_under_lock(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    child = run(env, claim="mine")
    original = prune._hold
    starting = replace(child, status=runs.STARTING, ended_at=None)

    def hold(
        entry: prune.Entry, stack: contextlib.ExitStack, env: Mapping[str, str]
    ) -> None:
        original(entry, stack, env)
        if entry.id == child.id:
            runs.save(starting)

    monkeypatch.setattr(prune, "_hold", hold)
    result = prune.remove(0, claim="mine", env=env, dry_run=dry_run)
    assert not result.removed and not result.failed and not result.errors
    assert result.skipped == [dict(id=child.id, reason="not ended")]
    assert runs.load(child.id, env) == starting
