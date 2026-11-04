import contextlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from ember import branches, engines, paths
from ember.cli import main
from ember.flows import lock as flow_lock
from ember.flows import plan
from ember.flows import record as flow_record
from ember.runs import core, record
from ember.runs.core import Core, StartOptions
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    git,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
    session_folder,
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
def driver(env: dict[str, str]) -> StubDriver:
    return StubDriver("claude", script_path(env))


class Streams:
    def __init__(self) -> None:
        self.out = io.StringIO()
        self.err = io.StringIO()

    def take(self) -> tuple[str, str]:
        out, err = self.out.getvalue(), self.err.getvalue()
        self.out, self.err = io.StringIO(), io.StringIO()
        return out, err


def _factory(env: dict[str, str], driver: StubDriver) -> branches.CoreFactory:
    def make(stream: Any) -> Core:  # noqa: ANN401
        return Core(env, load_driver=lambda name: driver, out=stream, err=stream)

    return make


def _start(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    text: str = "Task\n@write work.txt work\n",
    *,
    read_only: bool = False,
    trailers: tuple[str, ...] = (),
) -> record.Run:
    brief = tmp_path / f"brief-{len(list(tmp_path.glob('brief-*')))}.md"
    brief.write_text(text)
    out = io.StringIO()
    err = io.StringIO()
    starter = Core(
        env,
        load_driver=lambda name: driver,
        poll_seconds=0.02,
        stall_minutes=0.01,
        quiet=True,
        out=out,
        err=err,
    )
    starter.execute(
        lambda: starter.start(
            StartOptions(
                engine="claude",
                dir=str(repo),
                task_file=str(brief),
                read_only=read_only,
                trailers=trailers,
            )
        )
    )
    run_id = out.getvalue().splitlines()[0].removeprefix("run ")
    return record.load(run_id, env)


def _merge(
    env: dict[str, str],
    driver: StubDriver,
    streams: Streams,
    branch: str,
    directory: Path,
    *,
    target: str = "main",
    verdict: str | None = "keep",
    note: str | None = None,
    subject: str | None = "feat: work",
    as_json: bool = False,
) -> int:
    return branches.merge(
        branch,
        target,
        verdict,
        note=note,
        subject=subject,
        directory=directory,
        as_json=as_json,
        env=env,
        core=_factory(env, driver),
        out=streams.out,
        err=streams.err,
    )


def _cleanup(
    env: dict[str, str],
    driver: StubDriver,
    streams: Streams,
    directory: Path,
    *,
    target: str = "main",
    branch: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> int:
    return branches.cleanup(
        target,
        branch=branch,
        directory=directory,
        dry_run=dry_run,
        as_json=as_json,
        env=env,
        core=_factory(env, driver),
        out=streams.out,
        err=streams.err,
    )


def _branch_exists(repo: Path, branch: str, env: dict[str, str]) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        capture_output=True,
        env=env,
        check=False,
    )
    return result.returncode == 0


def _session_live(tmp_path: Path, run_id: str) -> bool:
    return session_folder(tmp_path, run_id).is_dir()


def _squash(repo: Path, branch: str, env: dict[str, str]) -> None:
    git(repo, "merge", "--squash", branch, env=env)
    git(repo, "commit", "-q", "-m", "squashed", env=env)


def _manual_branch(
    repo: Path, env: dict[str, str], run_id: str, *, legacy: bool = True
) -> tuple[str, Path]:
    """A worker branch and worktree made by hand, with no run record.

    The worktree is at `.ember/worktrees/<id>` in the checkout, where Ember
    made worktrees before its data folder, or with `legacy` false at the
    path Ember now gives it.
    """
    branch = f"ember/claude/{run_id}"
    if legacy:
        folder = repo / ".ember" / "worktrees" / run_id
    else:
        folder = paths.worktree_path(run_id, env)
    git(repo, "worktree", "add", "-q", "-b", branch, str(folder), env=env)
    (folder / "made.txt").write_text("made\n")
    git(folder, "add", "made.txt", env=env)
    git(folder, "commit", "-q", "-m", "made", env=env)
    return branch, folder


def _flow_record(env: dict[str, str], repo: Path, worktree: Path) -> dict[str, Any]:
    """An ended flow record for `FLOW_ID` in `repo`, naming `worktree`."""
    source = {
        "dir": str(repo),
        "base": "main",
        "tasks": [{"id": "a", "subject": "feat: a", "task": "Task", "files": ["a"]}],
    }
    document = plan.from_json(source, repo / "plan.json")
    worker = plan.Worker("codex", "model", None, "option")
    data = flow_record.new_flow(
        FLOW_ID,
        document,
        top=str(repo),
        main=str(repo),
        worktree=str(worktree),
        base=git(repo, "rev-parse", "HEAD", env=env),
        writer=worker,
        reviewer=worker,
        stall_minutes=10,
        claim=None,
    )
    data.update(status="done", ended_at=0)
    flow_record.create(data, json.dumps(source), env)
    return data


# Containment


def test_ancestor_and_tree_equal_merges_are_contained(
    repo: Path, env: dict[str, str]
) -> None:
    base = git(repo, "rev-parse", "HEAD", env=env)
    git(repo, "switch", "-q", "-c", "side", env=env)
    (repo / "side.txt").write_text("side\n")
    git(repo, "add", "side.txt", env=env)
    git(repo, "commit", "-q", "-m", "side", env=env)
    side = git(repo, "rev-parse", "HEAD", env=env)
    git(repo, "switch", "-q", "main", env=env)
    assert branches.is_contained(repo, base, "main", env)
    assert not branches.is_contained(repo, side, "main", env)
    _squash(repo, "side", env)
    assert branches.is_contained(repo, side, "main", env)
    assert branches.is_contained(repo, side, "main", env, scratch=True)
    assert not branches.is_contained(repo, "0" * 40, "main", env)


def test_conflicting_branch_is_not_contained(repo: Path, env: dict[str, str]) -> None:
    git(repo, "switch", "-q", "-c", "side", env=env)
    (repo / "README.md").write_text("side\n")
    git(repo, "commit", "-q", "-am", "side", env=env)
    side = git(repo, "rev-parse", "HEAD", env=env)
    git(repo, "switch", "-q", "main", env=env)
    (repo / "README.md").write_text("main\n")
    git(repo, "commit", "-q", "-am", "main", env=env)
    assert not branches.is_contained(repo, side, "main", env)


def test_scratch_containment_writes_no_objects(repo: Path, env: dict[str, str]) -> None:
    git(repo, "switch", "-q", "-c", "side", env=env)
    (repo / "side.txt").write_text("side\n")
    git(repo, "add", "side.txt", env=env)
    git(repo, "commit", "-q", "-m", "side", env=env)
    side = git(repo, "rev-parse", "HEAD", env=env)
    git(repo, "switch", "-q", "main", env=env)
    (repo / "main.txt").write_text("main\n")
    git(repo, "add", "main.txt", env=env)
    git(repo, "commit", "-q", "-m", "main", env=env)
    before = git(repo, "count-objects", "-v", env=env)
    assert not branches.is_contained(repo, side, "main", env, scratch=True)
    assert git(repo, "count-objects", "-v", env=env) == before


# emb merge


def test_merge_with_keep_merges_and_cleans_up(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    assert _session_live(tmp_path, run.id)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 0
    out, err = streams.take()
    head = git(repo, "rev-parse", "HEAD", env=env)
    lines = out.splitlines()
    assert lines[0].startswith(f"merged {run.branch} at ")
    assert lines[0].endswith(f" into main: {head}")
    assert lines[1].startswith(f"removed {run.branch}: contained in main; ")
    assert f"closed {run.id}" in lines[1]
    assert f"removed worktree {run.worktree}" in lines[1]
    assert f"closed {run.id}" in err
    assert git(repo, "log", "-1", "--format=%P", env=env).count(" ") == 0
    assert git(repo, "log", "-1", "--format=%s", env=env) == "feat: work"
    assert (repo / "work.txt").read_text() == "work\n"
    assert not _branch_exists(repo, run.branch, env)
    assert not Path(run.worktree).exists()
    assert record.load(run.id, env).status == record.CLOSED
    assert driver.closed == [run.id]
    assert git(repo, "worktree", "list", "--porcelain", env=env).count("worktree ") == 1


def test_merge_json(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo, as_json=True) == 0
    document = json.loads(streams.take()[0])
    assert document["branch"] == run.branch
    assert document["target"] == "main"
    assert document["merge_commit"] == git(repo, "rev-parse", "HEAD", env=env)
    assert document["contained_before"] is False
    [item] = document["cleanup"]
    assert item["branch"] == run.branch
    assert item["run"] == run.id
    assert item["action"] == "removed"
    assert item["next"] is None


def test_merge_note_prints_first_and_becomes_a_trailer(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    streams = Streams()
    note = "Keep, with a docstring fix"
    assert _merge(env, driver, streams, run.branch, repo, note=note) == 0
    lines = streams.take()[0].splitlines()
    assert lines[0] == f"Verdict: {note}"
    assert lines[1].startswith(f"merged {run.branch} at ")
    body = git(repo, "log", "-1", "--format=%B", env=env)
    assert body.splitlines()[0] == "feat: work"
    assert (
        git(
            repo,
            "log",
            "-1",
            "--format=%(trailers:key=Ember-Verdict,valueonly)",
            env=env,
        )
        == note
    )


def test_merge_note_in_json_and_for_a_contained_branch(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    git(repo, "branch", "kept", run.branch, env=env)
    streams = Streams()
    assert (
        _merge(env, driver, streams, run.branch, repo, note="Keep", as_json=True) == 0
    )
    assert json.loads(streams.take()[0])["note"] == "Keep"
    head = git(repo, "rev-parse", "HEAD", env=env)
    assert _merge(env, driver, streams, "kept", repo, note="Keep again") == 0
    lines = streams.take()[0].splitlines()
    assert lines[0] == "Verdict: Keep again"
    assert lines[1].startswith("already contained: kept at ")
    assert git(repo, "rev-parse", "HEAD", env=env) == head


def test_merge_from_inside_the_run_worktree_cleans_it_up(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    streams = Streams()
    code = _merge(env, driver, streams, run.branch, Path(run.worktree), note="Keep")
    out, err = streams.take()
    assert code == 0, err
    assert out.splitlines()[2].startswith(f"removed {run.branch}: contained in main")
    assert not _branch_exists(repo, run.branch, env)
    assert not Path(run.worktree).exists()


@pytest.mark.parametrize("verdict", [None, "drop", "Keep"])
def test_merge_without_keep_verdict_is_64(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    verdict: str | None,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    head = git(repo, "rev-parse", "HEAD", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo, verdict=verdict) == 64
    out, err = streams.take()
    assert out == ""
    assert "only --verdict keep merges" in err
    assert err.splitlines()[-1] == (
        f"next: review {run.branch}, then emb merge {run.branch} --into main "
        "--verdict keep --note '<verdict line>'"
    )
    assert git(repo, "rev-parse", "HEAD", env=env) == head
    assert _branch_exists(repo, run.branch, env)


def test_merge_of_a_missing_branch_or_outside_a_checkout_is_65(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    streams = Streams()
    assert _merge(env, driver, streams, "nope", repo) == 65
    _, err = streams.take()
    assert "emb merge: no local branch nope" in err
    assert err.splitlines()[-1] == f"next: git -C {repo} branch --list"
    assert _merge(env, driver, streams, "main", repo, target="nope") == 65
    outside = tmp_path / "outside"
    outside.mkdir()
    assert _merge(env, driver, streams, "x", outside) == 65
    assert "is not in a git checkout" in streams.take()[1]


def test_merge_refuses_a_target_checked_out_nowhere(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    git(repo, "branch", "other", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo, target="other") == 75
    _, err = streams.take()
    assert "other is not checked out in any worktree" in err
    assert err.splitlines()[-1].startswith(f"next: git -C {repo} switch other")
    assert _branch_exists(repo, run.branch, env)


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_merge_refuses_a_dirty_target(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    change: str,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    if change == "tracked":
        (repo / "README.md").write_text("changed\n")
    else:
        (repo / "loose.txt").write_text("loose\n")
    head = git(repo, "rev-parse", "HEAD", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 75
    _, err = streams.take()
    assert "is not clean" in err
    assert err.splitlines()[-1].startswith(f"next: git -C {repo} status --short")
    assert git(repo, "rev-parse", "HEAD", env=env) == head
    assert _session_live(tmp_path, run.id)


def test_merge_ignores_ignored_files_in_the_target(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    (repo / ".gitignore").write_text("*.log\n")
    git(repo, "add", ".gitignore", env=env)
    git(repo, "commit", "-q", "-m", "ignore logs", env=env)
    (repo / "debug.log").write_text("log\n")
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 0


def test_merge_conflict_leaves_target_unchanged_and_75(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Task\n@write README.md worker\n")
    assert run.branch is not None
    (repo / "README.md").write_text("lead\n")
    git(repo, "commit", "-q", "-am", "lead", env=env)
    head = git(repo, "rev-parse", "HEAD", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 75
    out, err = streams.take()
    assert out == ""
    assert f"conflict in README.md; main unchanged at {head}" in err
    assert err.splitlines()[-1].startswith(
        f"next: resolve the conflicts on {run.branch}, review"
    )
    assert git(repo, "rev-parse", "HEAD", env=env) == head
    assert git(repo, "status", "--porcelain", env=env) == ""
    assert not (
        Path(git(repo, "rev-parse", "--git-dir", env=env)) / "MERGE_HEAD"
    ).exists()
    assert (repo / "README.md").read_text() == "lead\n"
    assert _branch_exists(repo, run.branch, env)


def test_merge_of_a_contained_branch_makes_no_commit(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    git(repo, "merge", "-q", "--ff-only", run.branch, env=env)
    head = git(repo, "rev-parse", "HEAD", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo, as_json=True) == 0
    document = json.loads(streams.take()[0])
    assert document["merge_commit"] is None
    assert document["contained_before"] is True
    assert document["cleanup"][0]["action"] == "removed"
    assert git(repo, "rev-parse", "HEAD", env=env) == head
    assert not _branch_exists(repo, run.branch, env)


def test_merge_of_a_contained_branch_prints_already_contained(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 0
    out, _ = streams.take()
    assert out.splitlines()[0].startswith(f"already contained: {run.branch} at ")


def test_merge_of_a_branch_that_is_not_a_run_branch_keeps_it(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    git(repo, "switch", "-q", "-c", "feature", env=env)
    (repo / "f.txt").write_text("f\n")
    git(repo, "add", "f.txt", env=env)
    git(repo, "commit", "-q", "-m", "f", env=env)
    git(repo, "switch", "-q", "main", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, "feature", repo) == 0
    out, _ = streams.take()
    assert out.splitlines()[1] == (
        "kept feature: not an Ember run branch; next: git branch -d feature"
    )
    assert _branch_exists(repo, "feature", env)
    assert len(git(repo, "log", "-1", "--format=%P", env=env).split()) == 2


def test_merge_into_a_target_checked_out_in_a_linked_worktree(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    lead = tmp_path / "lead"
    git(repo, "worktree", "add", "-q", "-b", "session", str(lead), env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, lead, target="session") == 0
    assert git(lead, "log", "-1", "--format=%s", env=env) == "feat: work"
    assert (lead / "work.txt").read_text() == "work\n"
    assert not (repo / "work.txt").exists()
    assert not _branch_exists(repo, run.branch, env)
    assert not Path(run.worktree).exists()


@pytest.mark.parametrize(
    ("name", "operation"),
    [
        ("MERGE_HEAD", "merge"),
        ("REBASE_HEAD", "rebase"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ],
)
def test_merge_refuses_a_target_with_an_unfinished_operation(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    name: str,
    operation: str,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    head = git(repo, "rev-parse", "HEAD", env=env)
    marker = Path(git(repo, "rev-parse", "--absolute-git-dir", env=env)) / name
    if name.startswith("rebase-"):
        marker.mkdir()
    else:
        marker.write_text(f"{head}\n")
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 75
    out, err = streams.take()
    assert out == ""
    assert f"the main tree at {repo} has an unfinished {operation}" in err
    assert err.splitlines()[-1] == (
        f"next: git -C {repo} status; finish or abort the {operation}, then "
        f"emb merge {run.branch} --into main --verdict keep --note '<verdict line>'"
    )
    assert marker.exists()
    assert git(repo, "rev-parse", "HEAD", env=env) == head
    assert _branch_exists(repo, run.branch, env)
    assert _session_live(tmp_path, run.id)


def test_merge_takes_the_branch_over_a_tag_of_the_same_name(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    git(repo, "switch", "-q", "-c", "decoy", env=env)
    (repo / "decoy.txt").write_text("decoy\n")
    git(repo, "add", "decoy.txt", env=env)
    git(repo, "commit", "-q", "-m", "decoy", env=env)
    git(repo, "tag", run.branch, env=env)
    git(repo, "switch", "-q", "main", env=env)
    tip = git(repo, "rev-parse", f"refs/heads/{run.branch}", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 0
    assert (
        git(
            repo,
            "log",
            "-1",
            "--format=%(trailers:key=Ember-Task-Tip,valueonly)",
            env=env,
        )
        == tip
    )
    assert git(repo, "log", "-1", "--format=%s", env=env) == "feat: work"
    assert (repo / "work.txt").read_text() == "work\n"
    assert not (repo / "decoy.txt").exists()


# emb cleanup


def test_cleanup_removes_a_squash_merged_branch(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert out.startswith(f"removed {run.branch}: contained in main; closed {run.id}")
    assert not _branch_exists(repo, run.branch, env)
    assert not Path(run.worktree).exists()
    assert not _session_live(tmp_path, run.id)
    assert record.load(run.id, env).status == record.CLOSED


def test_cleanup_from_inside_the_run_worktree_removes_it(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, Path(run.worktree)) == 0
    out, err = streams.take()
    assert out.startswith(f"removed {run.branch}: contained in main"), err
    assert not _branch_exists(repo, run.branch, env)
    assert not Path(run.worktree).exists()


def test_cleanup_keeps_a_branch_not_contained(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert out == (
        f"kept {run.branch}: not contained in main; next: emb merge {run.branch} "
        "--into main --verdict keep --note '<verdict line>' "
        "--subject '<commit subject>' (after a Keep verdict)\n"
    )
    assert _branch_exists(repo, run.branch, env)
    assert _session_live(tmp_path, run.id)
    assert record.load(run.id, env).status != record.CLOSED


def test_cleanup_keeps_a_run_whose_turn_is_running(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Task\n@hang\n")
    assert run.branch is not None
    assert record.load(run.id, env).turn_state == record.SENT
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert out.startswith(f"kept {run.branch}: turn 1 of {run.id} is running; ")
    assert f"next: emb run --recover {run.id}" in out
    assert _branch_exists(repo, run.branch, env)
    assert Path(run.worktree).is_dir()
    (run.folder / "stub" / "release").write_text("")


def test_cleanup_keeps_a_session_holding_messages_ember_did_not_send(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Task\n")
    assert run.branch is not None
    driver.foreign = "1 message(s) in the session were not sent by Ember"
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [item] = json.loads(streams.take()[0])
    assert item["action"] == "kept"
    assert "holds messages Ember did not send" in item["reason"]
    assert item["next"].startswith("next: ask the owner about the messages")
    assert _branch_exists(repo, run.branch, env)
    assert driver.closed == []


def test_cleanup_keeps_a_run_whose_close_fails_with_an_owner_line(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Task\n")
    assert run.branch is not None

    def broken(run_: record.Run) -> engines.IdleState:
        raise engines.DriverError("the session cannot be read")

    driver.idle_state = broken  # type: ignore[method-assign]
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert "the session cannot be read" in out
    assert "; owner: repair what the message names" in out
    assert _branch_exists(repo, run.branch, env)


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_cleanup_keeps_a_worktree_with_changes(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    change: str,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    worktree = Path(run.worktree)
    if change == "tracked":
        (worktree / "work.txt").write_text("more\n")
    else:
        (worktree / "new.txt").write_text("new\n")
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert out == (
        f"kept {run.branch}: the worktree {worktree} has changes; "
        f"next: git -C {worktree} status --short\n"
    )
    assert _branch_exists(repo, run.branch, env)
    assert _session_live(tmp_path, run.id)


def test_cleanup_removes_a_worktree_holding_only_ignored_files(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Task\n@write .gitignore *.log\n")
    assert run.branch is not None
    _squash(repo, run.branch, env)
    (Path(run.worktree) / "build.log").write_text("log\n")
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    assert streams.take()[0].startswith(f"removed {run.branch}")
    assert not Path(run.worktree).exists()


def test_cleanup_prunes_a_worktree_deleted_by_hand(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    subprocess.run(["rm", "-rf", run.worktree], check=True)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert out.startswith(f"removed {run.branch}")
    assert "removed worktree" not in out
    assert not _branch_exists(repo, run.branch, env)
    assert run.worktree not in git(repo, "worktree", "list", env=env)


@pytest.mark.parametrize("legacy", [True, False])
def test_cleanup_removes_a_branch_with_no_run_record(
    env: dict[str, str], repo: Path, driver: StubDriver, legacy: bool
) -> None:
    branch, folder = _manual_branch(
        repo, env, "claude-20260101-000000-abcdef", legacy=legacy
    )
    _squash(repo, branch, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [item] = json.loads(streams.take()[0])
    assert item["action"] == "removed"
    assert item["run"] == "claude-20260101-000000-abcdef"
    assert "closed" not in item["reason"]
    assert f"removed worktree {folder}" in item["reason"]
    assert not folder.exists()
    assert not _branch_exists(repo, branch, env)


def test_cleanup_leaves_an_unrecorded_worktree_not_named_by_its_id(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run_id = "claude-20260101-000000-abcdef"
    branch, folder = _manual_branch(repo, env, run_id, legacy=False)
    _squash(repo, branch, env)
    other = tmp_path / "elsewhere"
    git(repo, "worktree", "move", str(folder), str(other), env=env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert out.startswith(f"kept {branch}: checked out in {other}; ")
    assert other.is_dir() and _branch_exists(repo, branch, env)


@pytest.mark.parametrize("merge", [False, True])
def test_merge_and_cleanup_remove_a_run_worktree_at_the_old_location(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver, merge: bool
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    legacy = repo / ".ember" / "worktrees" / run.id
    legacy.parent.mkdir(parents=True)
    git(repo, "worktree", "move", run.worktree, str(legacy), env=env)
    record.update(record.load(run.id, env), {"worktree": str(legacy)})
    streams = Streams()
    if merge:
        assert _merge(env, driver, streams, run.branch, repo) == 0
    else:
        _squash(repo, run.branch, env)
        assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert f"removed worktree {legacy}" in out
    assert not legacy.exists() and not _branch_exists(repo, run.branch, env)
    assert record.load(run.id, env).status == record.CLOSED


def test_cleanup_removes_the_worktree_its_record_names(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    assert Path(run.worktree) == paths.worktree_path(run.id, env)
    assert not Path(run.worktree).is_relative_to(repo)
    env["XDG_DATA_HOME"] = str(tmp_path / "moved-data")
    _squash(repo, run.branch, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    assert f"removed worktree {run.worktree}" in streams.take()[0]
    assert not Path(run.worktree).exists()


@pytest.mark.parametrize("merge", [False, True])
def test_branch_operations_use_recorded_repository_after_source_is_replaced(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver, merge: bool
) -> None:
    linked = tmp_path / "source"
    git(repo, "worktree", "add", "-q", "-b", "source", str(linked), env=env)
    run = _start(tmp_path, env, linked, driver)
    assert run.branch is not None
    git(repo, "worktree", "remove", str(linked), env=env)
    linked.mkdir()
    git(linked, "init", "-q", env=env)
    streams = Streams()
    if merge:
        assert _merge(env, driver, streams, run.branch, repo) == 0
    else:
        _squash(repo, run.branch, env)
        assert _cleanup(env, driver, streams, repo) == 0
    assert f"removed worktree {run.worktree}" in streams.take()[0]
    assert not Path(run.worktree).exists()


@pytest.mark.parametrize("where", ["main", "unnamed"])
def test_cleanup_keeps_a_branch_whose_record_names_a_foreign_worktree(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver, where: str
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    named = repo if where == "main" else tmp_path / "unnamed"
    record.update(record.load(run.id, env), {"worktree": str(named)})
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert (
        f"the run record {run.id} is not this branch's: its worktree {named} "
        f"is not a worktree of {run.id}"
    ) in out
    assert _branch_exists(repo, run.branch, env)


def test_cleanup_removes_a_flow_worktree_its_record_names_at_the_old_location(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    legacy = repo / ".ember" / "worktrees" / FLOW_ID
    branch = f"ember/flow/{FLOW_ID}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(legacy), env=env)
    _flow_record(env, repo, legacy)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [item] = json.loads(streams.take()[0])
    assert item["action"] == "removed"
    assert f"removed worktree {legacy}" in item["reason"]
    assert not legacy.exists() and not _branch_exists(repo, branch, env)


def test_cleanup_keeps_a_flow_branch_whose_record_names_another_branch(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    data = _flow_record(env, repo, flow.worktree)
    data["branch"] = "ember/flow/other"
    flow_record.save(data, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert f"the flow record {FLOW_ID} is not this branch's" in out
    assert out.rstrip().endswith(f"next: emb flow --status {FLOW_ID}")
    assert flow.worktree.is_dir()


def test_cleanup_keeps_a_flow_branch_whose_record_is_invalid(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    _flow_record(env, repo, flow.worktree)
    path = flow_record.folder(FLOW_ID, env) / flow_record.FLOW_NAME
    path.write_text("{")
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert "cannot read the flow record" in out
    assert out.rstrip().endswith(f"; owner: repair or remove {path}")
    assert flow.worktree.is_dir()


def test_cleanup_keeps_a_branch_checked_out_in_another_worktree(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    branch, folder = _manual_branch(repo, env, "claude-20260101-000000-abcdef")
    _squash(repo, branch, env)
    git(repo, "worktree", "remove", str(folder), env=env)
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", str(other), branch, env=env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert out.startswith(f"kept {branch}: checked out in {other}; ")
    assert _branch_exists(repo, branch, env)


def test_cleanup_keeps_the_target_even_when_it_is_a_run_branch(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    streams = Streams()
    code = _cleanup(env, driver, streams, repo, target=run.branch, branch=run.branch)
    assert code == 0
    out, _ = streams.take()
    assert out.startswith(f"kept {run.branch}: the merge target; ")
    assert _branch_exists(repo, run.branch, env)


def test_cleanup_keeps_a_branch_whose_record_names_another_branch(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    record.update(record.load(run.id, env), {"branch": "ember/claude/other-branch"})
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert f"the run record {run.id} is not this branch's" in out
    assert out.rstrip().endswith(f"next: emb run --status {run.id}")
    assert _branch_exists(repo, run.branch, env)
    assert _session_live(tmp_path, run.id)


@pytest.mark.parametrize("legacy", [False, True])
def test_cleanup_keeps_a_branch_whose_record_is_in_another_repository(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver, legacy: bool
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(elsewhere, "init", "-q", env=env)
    record.update(
        record.load(run.id, env),
        {
            "repo": str(elsewhere),
            "main": None if legacy else str(elsewhere),
            "common": None if legacy else str(elsewhere / ".git"),
        },
    )
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    assert "is in another repository" in streams.take()[0]
    assert _branch_exists(repo, run.branch, env)


def test_cleanup_keeps_a_branch_whose_record_is_invalid_with_an_owner_line(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    (run.folder / "run.json").write_text("{")
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, _ = streams.take()
    assert "cannot read the run record" in out
    assert out.rstrip().endswith(f"; owner: repair or remove {run.folder / 'run.json'}")
    assert _branch_exists(repo, run.branch, env)


def test_cleanup_never_touches_a_read_only_run(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Look\n", read_only=True)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    assert json.loads(streams.take()[0]) == []
    assert _session_live(tmp_path, run.id)
    assert record.load(run.id, env).status != record.CLOSED


def test_cleanup_ignores_branches_that_are_not_run_branches(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    git(repo, "branch", "feature", env=env)
    git(repo, "branch", "ember/notes", env=env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    out, err = streams.take()
    assert out == ""
    assert err == "emb cleanup: no Ember run branch against main\n"
    assert _branch_exists(repo, "feature", env)
    assert _branch_exists(repo, "ember/notes", env)


def test_cleanup_branch_option_acts_on_that_branch_only(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    first = _start(tmp_path, env, repo, driver, "One\n@write one.txt 1\n")
    second = _start(tmp_path, env, repo, driver, "Two\n@write two.txt 2\n")
    assert first.branch is not None and second.branch is not None
    _squash(repo, first.branch, env)
    _squash(repo, second.branch, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, branch=first.branch) == 0
    assert not _branch_exists(repo, first.branch, env)
    assert _branch_exists(repo, second.branch, env)
    git(repo, "branch", "feature", env=env)
    assert _cleanup(env, driver, streams, repo, branch="feature") == 0
    assert streams.take()[0].splitlines()[-1] == (
        "kept feature: not an Ember run branch; next: git branch -d feature"
    )


def test_cleanup_dry_run_changes_nothing(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    before = git(repo, "count-objects", "-v", env=env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, dry_run=True, as_json=True) == 0
    [item] = json.loads(streams.take()[0])
    assert item["action"] == "would remove"
    assert f"closes {run.id}" in item["reason"]
    assert f"removes worktree {run.worktree}" in item["reason"]
    assert _branch_exists(repo, run.branch, env)
    assert Path(run.worktree).is_dir()
    assert _session_live(tmp_path, run.id)
    assert record.load(run.id, env).status != record.CLOSED
    assert git(repo, "count-objects", "-v", env=env) == before
    assert _cleanup(env, driver, streams, repo, dry_run=True) == 0
    assert streams.take()[0].startswith(f"would remove {run.branch}: contained in main")


def test_cleanup_dry_run_keeps_a_run_whose_turn_is_running(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Task\n@hang\n")
    assert run.branch is not None
    assert record.load(run.id, env).turn_state == record.SENT
    streams = Streams()
    try:
        assert _cleanup(env, driver, streams, repo, dry_run=True) == 0
        dry, _ = streams.take()
        assert _cleanup(env, driver, streams, repo) == 0
        real, _ = streams.take()
    finally:
        (run.folder / "stub" / "release").write_text("")
    assert dry == real
    assert dry.startswith(f"kept {run.branch}: turn 1 of {run.id} is running; ")
    assert _branch_exists(repo, run.branch, env)


def test_cleanup_dry_run_keeps_a_collected_turn_of_a_writing_run(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    record.update(record.load(run.id, env), {"turn_state": record.COLLECTED})
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, dry_run=True, as_json=True) == 0
    [dry] = json.loads(streams.take()[0])
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [real] = json.loads(streams.take()[0])
    assert dry == real
    assert dry["action"] == "kept"
    assert dry["reason"] == (
        f"turn {run.turn} of {run.id} is collected but not committed"
    )
    assert dry["next"].startswith(f"next: emb run --recover {run.id}")
    assert _branch_exists(repo, run.branch, env)
    assert _session_live(tmp_path, run.id)


def test_cleanup_reports_the_steps_done_before_a_failed_branch_deletion(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    common = Path(git(repo, "rev-parse", "--git-common-dir", env=env))
    if not common.is_absolute():
        common = repo / common
    lock = common / "refs" / "heads" / f"{run.branch}.lock"
    lock.write_text("")
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 70
    out, err = streams.take()
    [item] = json.loads(out)
    assert item["action"] == "kept"
    assert item["reason"].startswith(
        f"contained in main; closed {run.id}; removed worktree {run.worktree}; "
        f"deleting the branch {run.branch} failed: "
    )
    assert item["next"] == err.splitlines()[-1]
    assert item["next"].startswith(f"next: git -C {repo} branch -d {run.branch}")
    assert not Path(run.worktree).exists()
    assert record.load(run.id, env).status == record.CLOSED
    assert _branch_exists(repo, run.branch, env)
    lock.unlink()
    assert _cleanup(env, driver, streams, repo) == 0
    assert streams.take()[0].startswith(f"removed {run.branch}")
    assert not _branch_exists(repo, run.branch, env)


def test_cleanup_from_a_linked_worktree_finds_run_worktrees_in_the_main_checkout(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    lead = tmp_path / "lead"
    git(repo, "worktree", "add", "-q", "-b", "session", str(lead), env=env)
    _squash(lead, run.branch, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, lead, target="session") == 0
    assert streams.take()[0].startswith(f"removed {run.branch}")
    assert not Path(run.worktree).exists()
    assert lead.is_dir()


def test_cleanup_usage_and_missing_names_are_65(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, target="nope") == 65
    assert "no local branch nope" in streams.take()[1]
    assert _cleanup(env, driver, streams, repo, branch="ember/claude/none-at-all") == 65
    outside = tmp_path / "outside"
    outside.mkdir()
    assert _cleanup(env, driver, streams, outside) == 65
    _, err = streams.take()
    assert err.splitlines()[-1] == (
        "next: emb cleanup ... --dir <a folder in the git checkout>"
    )


def test_cleanup_stops_at_a_git_failure_with_70(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    first, first_folder = _manual_branch(repo, env, "claude-20260101-000000-aaaaaa")
    second, second_folder = _manual_branch(repo, env, "claude-20260101-000000-bbbbbb")
    _squash(repo, first, env)
    (second_folder / "sub").mkdir()
    (second_folder / ".gitignore").write_text("sub/\n")
    git(second_folder, "add", ".gitignore", env=env)
    git(second_folder, "commit", "-q", "-m", "ignore", env=env)
    _squash(repo, second, env)
    (second_folder / "sub" / "kept.txt").write_text("x\n")
    (second_folder / "sub").chmod(0o555)
    streams = Streams()
    try:
        code = _cleanup(env, driver, streams, repo, as_json=True)
    finally:
        (second_folder / "sub").chmod(0o755)
    assert code == 70
    out, err = streams.take()
    [item, failed] = json.loads(out)
    assert item["branch"] == first
    assert item["action"] == "removed"
    assert failed["branch"] == second
    assert failed["action"] == "kept"
    assert failed["reason"].startswith(
        f"contained in main; removing the worktree {second_folder} failed: "
    )
    assert failed["next"] == err.splitlines()[-1]
    assert f"emb cleanup: removing the worktree {second_folder} failed: " in err
    assert err.splitlines()[-1].startswith(
        f"next: git -C {repo} worktree remove {second_folder}, then emb cleanup"
    )
    assert not first_folder.exists()
    assert _branch_exists(repo, second, env)


# The emb command


def test_merge_and_cleanup_help(capsys: pytest.CaptureFixture[str]) -> None:
    for argv, option in (
        (["merge", "--help"], "--verdict"),
        (["cleanup", "--help"], "--dry-run"),
    ):
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 0
        assert option in capsys.readouterr().out


def test_cli_merge_and_cleanup(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _start(tmp_path, env, repo, driver)
    other = _start(tmp_path, env, repo, driver, "Two\n@write two.txt 2\n")
    assert run.branch is not None and other.branch is not None
    monkeypatch.setattr(engines, "load_driver", lambda name, env=None: driver)
    monkeypatch.setattr(core, "POLL_SECONDS", 0.02)
    argv = ["merge", run.branch, "--into", "main", "--dir", str(repo)]
    for note in ([], ["--note", " "], ["--note", "two\nlines"]):
        with pytest.raises(SystemExit) as exit_info:
            main([*argv, "--verdict", "keep", *note], env=env)
        assert exit_info.value.code == 64
    argv += ["--note", "Keep"]
    assert main([*argv, "--verdict", "keep"], env=env) == 64
    assert "--subject" in capsys.readouterr().err
    argv += ["--subject", "feat: work"]
    assert main(argv, env=env) == 64
    capsys.readouterr()
    assert main([*argv, "--verdict", "keep", "--json"], env=env) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["cleanup"][0]["action"] == "removed"
    assert not _branch_exists(repo, run.branch, env)
    _squash(repo, other.branch, env)
    cleanup = ["cleanup", "--merged-into", "main", "--dir", str(repo)]
    assert main([*cleanup, "--dry-run", "--json"], env=env) == 0
    assert json.loads(capsys.readouterr().out)[0]["action"] == "would remove"
    assert main(cleanup, env=env) == 0
    assert capsys.readouterr().out.startswith(f"removed {other.branch}")
    assert not _branch_exists(repo, other.branch, env)
    assert os.path.isdir(repo)


# Flow branches

FLOW_ID = "flow-20261102-090132-015b1e"


@pytest.mark.parametrize("linked", [False, True])
def test_create_flow_branch_at_base_outside_the_repository(
    tmp_path: Path, env: dict[str, str], repo: Path, linked: bool
) -> None:
    base = git(repo, "rev-parse", "HEAD", env=env)
    directory = repo
    if linked:
        directory = tmp_path / "linked"
        git(repo, "worktree", "add", "-q", "-b", "session", str(directory), env=env)
    (directory / "README.md").write_text("dirty contents stay here\n")
    flow = branches.create_flow_branch(directory, FLOW_ID, base, env)
    assert flow == branches.FlowBranch(
        f"ember/flow/{FLOW_ID}",
        paths.worktree_path(FLOW_ID, env),
        base,
        directory,
        repo,
    )
    assert not flow.worktree.is_relative_to(repo)
    assert git(flow.worktree, "rev-parse", "HEAD", env=env) == base
    assert git(flow.worktree, "branch", "--show-current", env=env) == flow.branch
    assert (flow.worktree / "README.md").read_text() == "hello\n"
    assert (directory / "README.md").read_text() == "dirty contents stay here\n"
    assert not (repo / ".ember").exists()
    exclude = repo / ".git" / "info" / "exclude"
    assert not exclude.exists() or ".ember/" not in exclude.read_text()
    assert branches.FLOW_BRANCH.fullmatch(flow.branch)
    match = branches.WORKER_BRANCH.fullmatch(flow.branch)
    assert match is not None and match["engine"] == "flow"


def test_create_flow_branch_at_the_given_worktree(
    tmp_path: Path, env: dict[str, str], repo: Path
) -> None:
    worktree = tmp_path / "chosen" / FLOW_ID
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env, worktree=worktree)
    assert flow.worktree == worktree
    assert git(worktree, "branch", "--show-current", env=env) == flow.branch


@pytest.mark.parametrize("inside", ["checkout", "linked"])
def test_flow_worktree_folder_inside_the_repository_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, inside: str
) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "session", str(linked), env=env)
    folder = repo if inside == "checkout" else linked
    env["XDG_DATA_HOME"] = str(folder / "data")
    with pytest.raises(branches.BranchError) as caught:
        branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    assert caught.value.code == 69
    assert str(caught.value) == (
        f"the worktree folder {folder.resolve() / 'data' / 'ember' / 'worktrees'} "
        f"is inside the repository folder {folder.resolve()}; "
        "XDG_DATA_HOME places it there"
    )
    assert caught.value.line == (
        f"owner: set XDG_DATA_HOME so that {folder / 'data' / 'ember' / 'worktrees'} "
        "is outside every git checkout"
    )
    assert not _branch_exists(repo, f"ember/flow/{FLOW_ID}", env)
    assert not (folder / "data").exists()


@pytest.mark.parametrize("base", ["nope", "--help"])
def test_flow_base_must_be_a_commit(env: dict[str, str], repo: Path, base: str) -> None:
    with pytest.raises(branches.BranchError) as caught:
        branches.resolve_base(repo, base, env)
    assert caught.value.code == 64
    assert str(caught.value) == f"base {base} is not a commit"


def test_flow_needs_a_checkout(tmp_path: Path, env: dict[str, str]) -> None:
    with pytest.raises(branches.BranchError) as caught:
        branches.resolve_base(tmp_path, "HEAD", env)
    assert caught.value.code == 65
    assert "not in a git checkout" in str(caught.value)


def test_flow_worktree_creation_failure(env: dict[str, str], repo: Path) -> None:
    occupied = paths.worktree_path(FLOW_ID, env)
    occupied.mkdir(parents=True)
    (occupied / "left.txt").write_text("left\n")
    with pytest.raises(branches.BranchError) as caught:
        branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    assert caught.value.code == 70
    assert "git worktree add failed" in str(caught.value)
    assert caught.value.line == (
        f"next: git -C {repo} worktree prune, then emb flow --plan <plan> --start"
    )


def test_flow_worktree_folder_that_is_a_file_is_70(
    env: dict[str, str], repo: Path
) -> None:
    folder = paths.worktrees_dir(env)
    folder.parent.mkdir(parents=True)
    folder.write_text("not a folder")
    with pytest.raises(branches.BranchError) as caught:
        branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    assert caught.value.code == 70
    assert str(caught.value).startswith(f"cannot create the worktree folder {folder}: ")
    assert not _branch_exists(repo, f"ember/flow/{FLOW_ID}", env)


def test_merge_run_into_flow_uses_flow_worktree(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    run = _start(tmp_path, env, flow.worktree, driver)
    assert run.branch is not None
    streams = Streams()
    with flow_lock.hold(FLOW_ID, env):
        assert (
            _merge(
                env, driver, streams, run.branch, repo, target=flow.branch, as_json=True
            )
            == 0
        )
    result = json.loads(streams.take()[0])
    assert result["merge_commit"] == git(flow.worktree, "rev-parse", "HEAD", env=env)
    assert result["cleanup"][0]["action"] == "removed"
    assert (flow.worktree / "work.txt").read_text() == "work\n"
    assert not (repo / "work.txt").exists()
    assert not Path(run.worktree).exists() and not _branch_exists(repo, run.branch, env)
    assert record.load(run.id, env).status == record.CLOSED


@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_keeps_live_flow(
    env: dict[str, str], repo: Path, driver: StubDriver, dry_run: bool
) -> None:
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    streams = Streams()
    with flow_lock.hold(FLOW_ID, env):
        assert _cleanup(env, driver, streams, repo, dry_run=dry_run, as_json=True) == 0
    [result] = json.loads(streams.take()[0])
    assert result["action"] == "kept"
    assert result["reason"] == f"the flow {FLOW_ID} is running"
    assert result["next"] == f"next: emb flow --status {FLOW_ID}"
    assert flow.worktree.is_dir() and _branch_exists(repo, flow.branch, env)


def test_cleanup_flow_dry_run_then_remove(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, dry_run=True, as_json=True) == 0
    [result] = json.loads(streams.take()[0])
    assert result["action"] == "would remove"
    assert flow.worktree.is_dir() and _branch_exists(repo, flow.branch, env)
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [result] = json.loads(streams.take()[0])
    assert result["action"] == "removed" and "closed" not in result["reason"]
    assert not flow.worktree.exists() and not _branch_exists(repo, flow.branch, env)


def test_merge_flow_keeps_live_branch_then_cleans_it(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    (flow.worktree / "flow.txt").write_text("flow\n")
    git(flow.worktree, "add", "flow.txt", env=env)
    git(flow.worktree, "commit", "-q", "-m", "feat: flow change", env=env)
    streams = Streams()
    with flow_lock.hold(FLOW_ID, env):
        assert _merge(env, driver, streams, flow.branch, repo, as_json=True) == 0
    result = json.loads(streams.take()[0])
    assert result["cleanup"][0]["action"] == "kept"
    assert len(git(repo, "log", "-1", "--format=%P", env=env).split()) == 2
    assert (repo / "flow.txt").read_text() == "flow\n"
    assert flow.worktree.is_dir()
    assert _cleanup(env, driver, streams, repo) == 0
    assert not flow.worktree.exists() and not _branch_exists(repo, flow.branch, env)


def test_cleanup_flow_with_unreadable_lock_keeps_it(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    (paths.runtime_dir(env) / "locks" / f"{FLOW_ID}.lock").symlink_to(repo / "missing")
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [result] = json.loads(streams.take()[0])
    assert result["action"] == "kept"
    assert "cannot open the lock" in result["reason"]
    assert result["next"] == f"next: emb flow --status {FLOW_ID}"
    assert flow.worktree.is_dir() and _branch_exists(repo, flow.branch, env)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
def test_cleanup_closed_detached_worktrees(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    dry_run: bool,
    dirty: bool,
) -> None:
    run = _start(tmp_path, env, repo, driver, "Review\n", read_only=True)
    folder = Path(run.worktree)
    evidence = folder / "evidence.txt"
    evidence.write_text("keep\n")
    core_ = _factory(env, driver)(io.StringIO())
    assert core_.close(run.id) == 0
    if not dirty:
        evidence.unlink()
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, dry_run=dry_run, as_json=True) == 0
    out, _ = streams.take()
    result = json.loads(out)
    assert len(result) == 1
    assert result[0]["run"] == run.id and result[0]["branch"] is None
    assert result[0]["action"] == (
        "kept" if dirty else "would remove" if dry_run else "removed"
    )
    assert folder.exists() == (dirty or dry_run)
    assert record.load(run.id, env).worktree == str(folder)
    if dirty:
        assert f"git worktree remove --force {folder}" in result[0]["reason"]


def test_cleanup_detached_worktrees_respects_repository_and_branch_scope(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver, "Review\n", read_only=True)
    folder = Path(run.worktree)
    (folder / "evidence").touch()
    assert _factory(env, driver)(io.StringIO()).close(run.id) == 0
    (folder / "evidence").unlink()
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, branch="main") == 0
    assert folder.exists()
    other = tmp_path / "other"
    other.mkdir()
    other_repo = make_repo(other, env)
    assert _cleanup(env, driver, streams, other_repo) == 0
    assert folder.exists()
    assert _cleanup(env, driver, streams, repo) == 0
    assert not folder.exists()


@pytest.mark.parametrize("subject", [None, "", " ", "feat: a\nbody", "a\rb", "a\0b"])
def test_worker_merge_requires_subject(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    subject: str | None,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    before = git(repo, "rev-parse", "HEAD", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo, subject=subject) == 64
    assert "next:" in streams.err.getvalue()
    assert "--subject" in streams.err.getvalue()
    assert git(repo, "rev-parse", "HEAD", env=env) == before
    assert Path(run.worktree).exists()


def test_three_turn_squash_preserves_distinct_worker_trailers(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
) -> None:
    run = _start(
        tmp_path,
        env,
        repo,
        driver,
        trailers=(
            "Co-Authored-By: A <a@example.com>",
            "Ember-Verdict: forged",
            "Ember-Task-Tip: forged",
            "eMbEr-Other: forged",
        ),
    )
    assert run.branch is not None
    worker = Core(
        env,
        load_driver=lambda name: driver,
        poll_seconds=0.02,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    for turn in (2, 3):
        message = tmp_path / f"turn-{turn}.md"
        message.write_text(f"@write work.txt turn-{turn}\n")
        assert (
            worker.execute(lambda message=message: worker.send(run.id, str(message)))
            == 0
        )
    tip = git(repo, "rev-parse", run.branch, env=env)
    base = git(repo, "rev-parse", "HEAD", env=env)
    assert git(repo, "rev-list", "--count", f"{base}..{tip}", env=env) == "3"
    streams = Streams()
    assert (
        _merge(
            env,
            driver,
            streams,
            run.branch,
            repo,
            subject="feat: x",
            note="Keep after review",
        )
        == 0
    )
    assert git(repo, "rev-list", "--count", f"{base}..HEAD", env=env) == "1"
    assert git(repo, "log", "-1", "--format=%P", env=env) == base
    assert git(repo, "log", "-1", "--format=%B", env=env) == (
        "feat: x\n\nCo-Authored-By: A <a@example.com>\n"
        f"Ember-Verdict: Keep after review\nEmber-Task-Tip: {tip}"
    )


def test_squash_collects_trailers_from_each_worker_commit(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
) -> None:
    branch, folder = _manual_branch(repo, env, "claude-trailers")
    for number, trailer in enumerate(
        [
            "Reviewed-by: A <a@example.com>",
            "Reviewed-by: B <b@example.com>",
            "Reviewed-by: A <a@example.com>",
        ]
    ):
        (folder / "made.txt").write_text(str(number))
        git(
            folder,
            "commit",
            "-am",
            f"turn {number}\n\n{trailer}",
            env=env,
        )
    assert _merge(env, driver, Streams(), branch, repo, note="Keep") == 0
    body = git(repo, "log", "-1", "--format=%B", env=env)
    assert body.count("Reviewed-by: A <a@example.com>") == 1
    assert body.count("Reviewed-by: B <b@example.com>") == 1


@pytest.mark.parametrize("refuse", [False, True])
def test_squash_runs_target_commit_msg_hook_without_pre_commit(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    refuse: bool,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    before = git(repo, "rev-parse", "HEAD", env=env)
    hooks = tmp_path / "custom-hooks"
    hooks.mkdir()
    git(repo, "config", "core.hooksPath", str(hooks), env=env)
    hook = hooks / "commit-msg"
    hook.write_text(
        "#!/bin/sh\n"
        'test "$(pwd)" = ' + str(repo) + " || exit 1\n"
        'IFS= read -r subject < "$1"\n'
        'test "$subject" = "feat: work" || exit 1\n'
        + (
            "echo refused >&2; exit 1\n"
            if refuse
            else 'printf "Reviewed-by: Hook <hook@example.com>\\n" >> "$1"\n'
        )
    )
    hook.chmod(0o755)
    pre = hooks / "pre-commit"
    pre.write_text("#!/bin/sh\necho pre-commit must not run >&2\nexit 1\n")
    pre.chmod(0o755)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == (77 if refuse else 0)
    if refuse:
        assert "commit-msg hook refused" in streams.err.getvalue()
        assert git(repo, "rev-parse", "HEAD", env=env) == before
        assert Path(run.worktree).exists()
        assert git(repo, "status", "--porcelain", env=env) == ""
    else:
        assert "Reviewed-by: Hook" in git(repo, "log", "-1", "--format=%B", env=env)


def test_squash_signing_refusal_moves_nothing(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    before = git(repo, "rev-parse", "HEAD", env=env)
    git(repo, "config", "commit.gpgsign", "true", env=env)
    git(repo, "config", "gpg.program", "/bin/false", env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, repo) == 77
    assert "commit-tree refused" in streams.err.getvalue()
    assert git(repo, "rev-parse", "HEAD", env=env) == before
    assert git(repo, "status", "--porcelain", env=env) == ""
    assert Path(run.worktree).exists()


def test_squash_honours_commit_signing(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    signer = tmp_path / "signer"
    signer.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write('-----BEGIN PGP SIGNATURE-----\\nsigned\\n"
        "-----END PGP SIGNATURE-----\\n')\n"
        "sys.stderr.write('[GNUPG:] SIG_CREATED D 1 10 00 0 0\\n')\n"
    )
    signer.chmod(0o755)
    git(repo, "config", "commit.gpgsign", "true", env=env)
    git(repo, "config", "gpg.program", str(signer), env=env)
    assert _merge(env, driver, Streams(), run.branch, repo) == 0
    assert "gpgsig -----BEGIN PGP SIGNATURE-----" in git(
        repo,
        "cat-file",
        "-p",
        "HEAD",
        env=env,
    )


@pytest.mark.parametrize("separators", [":", "=", "=:", "%"])
def test_cleanup_recognises_squashed_tip_after_target_changes(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    separators: str,
) -> None:
    branch, folder = _manual_branch(repo, env, "claude-squashed")
    git(repo, "config", "trailer.separators", separators, env=env)
    footer = "\n".join(f"Review-{n}{separators[0]} reviewer-{n}" for n in range(8))
    git(folder, "commit", "--amend", "-m", f"feat: worker\n\n{footer}", env=env)
    tip = git(repo, "rev-parse", branch, env=env)
    opened = branches.open_repository(repo, env, "merge")
    commit, _ = branches._merge(opened, branch, "main", repo, "Keep", "feat: work")
    assert commit is not None
    body = git(repo, "show", "-s", "--format=%B", commit, env=env)
    assert footer in body
    assert f"Ember-Task-Tip: {tip}" in body
    (repo / "made.txt").write_text("later value")
    git(repo, "commit", "-am", "fix: change value later", env=env)
    assert branches.is_contained(repo, tip, "main", env, scratch=True)
    assert branches.is_contained(repo, branch, "main", env, scratch=True)
    assert branches.landed_commit(repo, tip, "main", env) == commit
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, branch=branch) == 0
    assert not folder.exists()
    assert not _branch_exists(repo, branch, env)


def test_git_trailers_exclude_body_paragraphs_from_containment(
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
) -> None:
    branch, folder = _manual_branch(repo, env, "claude-body-trailer")
    git(
        folder,
        "commit",
        "--amend",
        "-m",
        "feat: worker\n\nBody: source paragraph\n\nReviewed-by: A <a@example.com>",
        env=env,
    )
    tip = git(repo, "rev-parse", branch, env=env)
    (repo / "README.md").write_text("a target change")
    git(
        repo,
        "commit",
        "-am",
        f"fix: target\n\nEmber-Task-Tip: {tip}\n\nReviewed-by: B <b@example.com>",
        env=env,
    )
    assert branches.landed_commit(repo, tip, "main", env) is None
    assert not branches.is_contained(repo, tip, "main", env)
    assert _merge(env, driver, Streams(), branch, repo) == 0
    body = git(repo, "log", "-1", "--format=%B", env=env)
    assert "Body: source paragraph" not in body
    assert "Reviewed-by: A <a@example.com>" in body


@pytest.mark.parametrize("separators", [":=", "=", "=:", "%"])
def test_squash_uses_repository_trailer_separators(
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    separators: str,
) -> None:
    branch, folder = _manual_branch(repo, env, "claude-separators")
    git(repo, "config", "trailer.separators", separators, env=env)
    separator = separators[0]
    git(
        folder,
        "commit",
        "--amend",
        "-m",
        f"feat: worker\n\nReviewed-by {separator} A <a@example.com>\n"
        f"Ember-Verdict {separator} forged",
        env=env,
    )
    (folder / "made.txt").write_text("second turn")
    git(
        folder,
        "commit",
        "-am",
        f"feat: worker again\n\nreviewed-by {separators[-1]} A <a@example.com>\n"
        f"Signed-off-by {separator} B <b@example.com>\n"
        f"eMbEr-Task-Tip {separator} forged",
        env=env,
    )
    streams = Streams()
    assert _merge(env, driver, streams, branch, repo, note="Keep") == 0
    body = git(repo, "log", "-1", "--format=%B", env=env)
    assert body.count(f"Reviewed-by{separator} A <a@example.com>") == 1
    assert f"reviewed-by{separator}" not in body
    assert f"Signed-off-by{separator} B <b@example.com>" in body
    assert "forged" not in body
    assert body.count("Ember-Verdict: Keep") == 1


@pytest.mark.parametrize("subject", ["feat: update API", "feat!: update API"])
@pytest.mark.parametrize(
    ("message", "breaking"),
    [
        ("feat!: change API", True),
        ("feat(api)!: change API", True),
        ("feat: change API\n\nBREAKING CHANGE: use the new API", True),
        ("feat: change API\n\nBREAKING-CHANGE: use the new API", True),
        ("feat: change API\n\nThis mentions BREAKING CHANGE: in prose.", False),
        ("feat: change API", False),
    ],
)
def test_squash_warns_about_breaking_history(
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    subject: str,
    message: str,
    breaking: bool,
) -> None:
    branch, folder = _manual_branch(repo, env, "claude-breaking")
    git(folder, "commit", "--amend", "-m", message, env=env)
    (folder / "made.txt").write_text("second turn without a breaking signal")
    git(folder, "commit", "-am", "fix: complete the work", env=env)
    streams = Streams()
    assert (
        _merge(env, driver, streams, branch, repo, subject=subject, as_json=True) == 0
    )
    warning = f"WARNING: {branch} holds a breaking change; the subject has no !"
    expected = [warning] if breaking and "!:" not in subject else []
    assert [
        line
        for line in streams.err.getvalue().splitlines()
        if line.startswith("WARNING:")
    ] == expected
    assert json.loads(streams.out.getvalue())["warnings"] == expected
    body = git(repo, "log", "-1", "--format=%B", env=env)
    assert body.splitlines()[0] == subject
    assert "BREAKING CHANGE:" not in body
    assert not _branch_exists(repo, branch, env)


def test_flow_creation_and_cleanup_lock_every_worktree_command(
    env: dict[str, str],
    repo: Path,
    driver: StubDriver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = paths.worktree_lock
    git_command = branches._git
    held = False
    commands: set[str] = set()

    @contextlib.contextmanager
    def observe_lock(
        common: Path, env: Mapping[str, str] | None = None
    ) -> Iterator[None]:
        nonlocal held
        assert common.resolve() == (repo / ".git").resolve()
        assert not held
        with lock(common, env):
            held = True
            try:
                yield
            finally:
                held = False

    def observe_git(
        cwd: str | Path,
        *args: str,
        env: Mapping[str, str],
        timeout: float | None = branches.GIT_TIMEOUT,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "worktree":
            assert held
            commands.add(args[1])
        return git_command(cwd, *args, env=env, timeout=timeout, stdin=stdin)

    monkeypatch.setattr(paths, "worktree_lock", observe_lock)
    monkeypatch.setattr(branches, "_git", observe_git)
    flow = branches.create_flow_branch(repo, FLOW_ID, "HEAD", env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo) == 0
    assert not flow.worktree.exists()
    assert commands == {"list", "add", "remove", "prune"}


def test_cleanup_keeps_uncertain_detached_run(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    saved = _start(tmp_path, env, repo, driver, "Review\n", read_only=True)
    record.update(
        saved, {"status": record.FAILED, "turn_state": record.SENT}, allow_closed=True
    )
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [result] = json.loads(streams.take()[0])
    assert result["action"] == "kept"
    assert "delivery is uncertain" in result["reason"]
    assert Path(saved.worktree).exists()
