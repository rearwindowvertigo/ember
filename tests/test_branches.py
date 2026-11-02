import io
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ember import branches, engines
from ember.cli import main
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
    as_json: bool = False,
) -> int:
    return branches.merge(
        branch,
        target,
        verdict,
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


def _manual_branch(repo: Path, env: dict[str, str], run_id: str) -> tuple[str, Path]:
    """A worker branch and worktree made by hand, with no run record."""
    branch = f"ember/claude/{run_id}"
    folder = repo / ".ember" / "worktrees" / run_id
    git(repo, "worktree", "add", "-q", "-b", branch, str(folder), env=env)
    (folder / "made.txt").write_text("made\n")
    git(folder, "add", "made.txt", env=env)
    git(folder, "commit", "-q", "-m", "made", env=env)
    return branch, folder


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
    assert git(repo, "log", "-1", "--format=%P", env=env).count(" ") == 1
    assert git(repo, "log", "-1", "--format=%s", env=env) == (
        f"Merge branch '{run.branch}'"
    )
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
        "--verdict keep"
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


def test_merge_conflict_is_aborted_and_75(
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
    assert f"conflict in README.md; merge aborted, main unchanged at {head}" in err
    assert err.splitlines()[-1].startswith(
        f"next: git -C {repo} merge --no-ff --no-edit {run.branch}, resolve"
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


def test_merge_into_a_target_checked_out_in_a_linked_worktree(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    lead = tmp_path / "lead"
    git(repo, "worktree", "add", "-q", "-b", "session", str(lead), env=env)
    streams = Streams()
    assert _merge(env, driver, streams, run.branch, lead, target="session") == 0
    assert git(lead, "log", "-1", "--format=%s", env=env) == (
        f"Merge branch '{run.branch}' into session"
    )
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
        f"emb merge {run.branch} --into main --verdict keep"
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
    assert git(repo, "rev-parse", "HEAD^2", env=env) == tip
    assert git(repo, "log", "-1", "--format=%s", env=env) == (
        f"Merge branch '{run.branch}'"
    )
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
        "--into main --verdict keep (after a Keep verdict)\n"
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


def test_cleanup_removes_a_branch_with_no_run_record(
    env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    branch, folder = _manual_branch(repo, env, "claude-20260101-000000-abcdef")
    _squash(repo, branch, env)
    streams = Streams()
    assert _cleanup(env, driver, streams, repo, as_json=True) == 0
    [item] = json.loads(streams.take()[0])
    assert item["action"] == "removed"
    assert item["run"] == "claude-20260101-000000-abcdef"
    assert "closed" not in item["reason"]
    assert not folder.exists()
    assert not _branch_exists(repo, branch, env)


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


def test_cleanup_keeps_a_branch_whose_record_is_in_another_repository(
    tmp_path: Path, env: dict[str, str], repo: Path, driver: StubDriver
) -> None:
    run = _start(tmp_path, env, repo, driver)
    assert run.branch is not None
    _squash(repo, run.branch, env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(elsewhere, "init", "-q", env=env)
    record.update(record.load(run.id, env), {"repo": str(elsewhere)})
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
