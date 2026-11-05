"""Step precedence, read-only derivation and missing git history."""

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import branches, choices, skill
from ember.runs import core, exits, record, steps

from .stub_engine import git, make_env, make_repo  # pyrefly: ignore[missing-import]


@pytest.fixture
def run(tmp_path: Path) -> record.Run:
    env = make_env(tmp_path)
    return record.Run(
        id="claude-20261105-120000-abcdef",
        engine="claude",
        model="opus",
        effort="high",
        read_only=False,
        repo=str(tmp_path / "repo"),
        main=str(tmp_path / "main"),
        worktree=str(tmp_path / "worktree"),
        branch="ember/claude/claude-20261105-120000-abcdef",
        base="a" * 40,
        base_branch="main",
        sandbox="none",
        network=False,
        read=(),
        read_effective=(),
        trailers=(),
        claim="claim-a",
        status=record.WAITING,
        turn=1,
        turn_state=record.COMMITTED,
        reply=None,
        expected_head=None,
        started_at=1,
        updated_at=1,
        ended_at=None,
        last_exit=0,
        last_reason=None,
        commits=(),
        subject="Task",
        env=env,
    )


def _git(directory: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, "one\0two\0", "")


@pytest.mark.parametrize(
    ("changes", "session", "collect", "merged", "step", "command"),
    [
        ({"parent": {"command": "gapcheck"}, "status": record.CLOSED},
         core.LIVE, False, True, "parent", None),
        ({"status": record.CLOSED}, core.MISSING, False, False, "finish", "finish"),
        ({"status": record.CLOSED, "read_only": True, "commits": ("tip",)},
         core.MISSING, False, False, "finish", "finish"),
        ({"status": record.CLOSED, "commits": ("tip",), "last_exit": 71},
         core.MISSING, True, True, "finish", "finish"),
        ({"status": record.CLOSED, "commits": ("tip",), "last_exit": 71},
         core.MISSING, True, False, "merge", "verify"),
        ({"status": record.FAILED, "last_exit": 71, "turn": 0},
         core.MISSING, True, False, "failed", "failure"),
        ({"last_exit": 76}, core.LIVE, True, True, "failed", "failure"),
        ({"status": record.STARTING, "turn_state": record.SENT},
         core.LIVE, True, False, "starting", "status"),
        ({"turn": 0}, core.MISSING, True, False, "starting", "status"),
        ({"turn_state": record.SENT, "last_exit": 124},
         core.LIVE, True, True, "running", "status"),
        ({"status": record.FAILED, "turn_state": record.SENT, "last_exit": 71},
         core.LIVE, True, False, "failed", "failure"),
        ({"status": record.FAILED, "turn_state": record.SENT, "last_exit": 72},
         core.MISSING, True, False, "failed", "failure"),
        ({"turn_state": record.SENT, "last_exit": 124},
         core.MISSING, False, True, "recover", "recover"),
        ({"turn_state": record.SENT},
         core.DEAD, False, True, "recover", "recover"),
        ({"commits": ("tip",)}, core.LIVE, True, True, "recover", "recover"),
        ({"commits": ("tip",)}, core.LIVE, False, True, "close", "close"),
        ({"commits": ("tip",)}, core.LIVE, False, False, "merge", "verify"),
        ({}, core.LIVE, False, False, "close", "close"),
        ({"read_only": True, "commits": ("tip",)},
         core.LIVE, False, False, "close", "close"),
    ],
)  # fmt: skip
def test_order(
    run: record.Run,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    session: str,
    collect: bool,
    merged: bool,
    step: str,
    command: str | None,
) -> None:
    run = replace(run, **changes)
    monkeypatch.setattr(branches, "is_contained", lambda *a, **kw: merged)
    actual, line = steps.derive(run, session, collect, _git)
    assert actual == step
    if command is None:
        assert line is None
    elif command == "finish":
        assert line == "next: emb choices --finish claim-a"
    elif command == "failure":
        assert line == exits.line(
            run.last_exit or 0,
            None,
            run=run.id,
            worktree=run.worktree,
            engine=run.engine,
        )
    elif command == "verify":
        assert line is not None and line.startswith("next: python3 ")
    else:
        assert line == f"next: emb run --{command} {run.id}"


@pytest.mark.parametrize("claim", [None, "claim-a"])
def test_finished_claim_has_no_line(run: record.Run, claim: str | None) -> None:
    if claim is not None:
        choices.finish(claim, env=run.env)
    run = replace(run, status=record.CLOSED, claim=claim)
    assert steps.derive(run, core.MISSING, False, _git) == ("finish", None)


@pytest.mark.parametrize("base_branch", [None, "main"])
@pytest.mark.parametrize("main", [None, "/main with spaces"])
@pytest.mark.parametrize("turn", [1, 2])
def test_verify_arguments(
    run: record.Run,
    monkeypatch: pytest.MonkeyPatch,
    base_branch: str | None,
    main: str | None,
    turn: int,
) -> None:
    run = replace(
        run, commits=("first", "last"), main=main, base_branch=base_branch, turn=turn
    )
    contained_calls = []
    git_calls = []

    def contained(*args: object, **kwargs: object) -> bool:
        contained_calls.append((args, kwargs))
        return False

    def diff(directory: str, *args: str) -> subprocess.CompletedProcess[str]:
        git_calls.append((directory, *args))
        return _git(directory, *args)

    monkeypatch.setattr(branches, "is_contained", contained)
    _, line = steps.derive(run, core.LIVE, False, diff)
    assert line is not None
    assert str(skill.link_path(run.env) / "scripts/verify_branch.py") in line
    assert not skill.link_path(run.env).exists()
    assert f"--base {base_branch or '<session branch>'}" in line
    assert f"--into {base_branch or '<session branch>'}" in line
    assert f"--branch {run.branch} --worktree {run.worktree}" in line
    assert ("--expect-files 2" in line) == (turn == 1)
    assert line.endswith("--verdict keep --note '<verdict>' --subject '<subject>'")
    assert ("--repo '/main with spaces'" if main else f"--repo {run.repo}") in line
    assert git_calls == (
        [(main or run.repo, "diff", "--name-only", "-z", run.base, "last")]
        if turn == 1
        else []
    )
    assert contained_calls == (
        [((main or run.repo, "last", base_branch, run.env), {"scratch": True})]
        if base_branch
        else []
    )


@pytest.mark.parametrize("failure", ["exit", "os", "timeout", "core"])
def test_git_failure_omits_count(
    run: record.Run, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run = replace(run, commits=("tip",))
    monkeypatch.setattr(branches, "is_contained", lambda *a, **kw: False)

    def failed(directory: str, *args: str) -> subprocess.CompletedProcess[str]:
        if failure == "os":
            raise OSError("missing git")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("git", 120)
        if failure == "core":
            raise core.RunError(1, "git timed out")
        return subprocess.CompletedProcess(args, 1, "", "missing commit")

    step, line = steps.derive(run, core.LIVE, False, failed)
    assert step == "merge" and line is not None
    assert "--expect-files" not in line


def test_null_base_counts_any_commit(run: record.Run) -> None:
    run = replace(run, base=None, base_branch=None, commits=("tip",))
    step, line = steps.derive(run, core.MISSING, False, _git)
    assert step == "merge" and line is not None
    assert "--expect-files" not in line


@pytest.mark.parametrize("squash", [False, True])
def test_merged_after_branch_deleted(
    tmp_path: Path, run: record.Run, squash: bool
) -> None:
    repo = make_repo(tmp_path, run.env)
    base = git(repo, "rev-parse", "HEAD", env=run.env)
    git(repo, "switch", "-c", "worker", env=run.env)
    (repo / "work").write_text("work")
    git(repo, "add", "work", env=run.env)
    git(repo, "commit", "-qm", "work", env=run.env)
    tip = git(repo, "rev-parse", "HEAD", env=run.env)
    git(repo, "switch", "main", env=run.env)
    if squash:
        git(repo, "merge", "--squash", "worker", env=run.env)
        git(repo, "commit", "-qm", f"landed\n\nEmber-Task-Tip: {tip}", env=run.env)
    else:
        git(repo, "merge", "--ff-only", "worker", env=run.env)
    git(repo, "branch", "-D", "worker", env=run.env)
    run = replace(run, main=str(repo), branch="worker", base=base, commits=(tip,))
    assert steps.derive(run, core.MISSING, False, _git) == (
        "close",
        f"next: emb run --close {run.id}",
    )
    run = replace(run, status=record.CLOSED)
    assert steps.derive(run, core.MISSING, False, _git) == (
        "finish",
        "next: emb choices --finish claim-a",
    )


def test_pruned_tip_counts_as_not_merged(tmp_path: Path, run: record.Run) -> None:
    """A tip absent from the object store, including a pruned tip, is not merged."""
    repo = make_repo(tmp_path, run.env)
    run = replace(run, main=str(repo), commits=("f" * 40,))
    step, line = steps.derive(run, core.MISSING, False, core.Core(run.env)._git)
    assert step == "merge" and line is not None
    assert "--expect-files" not in line


@pytest.mark.parametrize("base_branch", [None, "main"])
def test_closed_run_with_missing_branch_finishes(
    tmp_path: Path, run: record.Run, base_branch: str | None
) -> None:
    """A closed run whose branch is gone has nothing to merge, merged or not."""
    repo = make_repo(tmp_path, run.env)
    run = replace(
        run,
        status=record.CLOSED,
        main=str(repo),
        base_branch=base_branch,
        commits=("f" * 40,),
    )
    assert steps.derive(run, core.MISSING, False, core.Core(run.env)._git) == (
        "finish",
        "next: emb choices --finish claim-a",
    )


def test_closed_run_in_a_removed_checkout_finishes(run: record.Run) -> None:
    run = replace(run, status=record.CLOSED, commits=("f" * 40,))
    assert steps.derive(run, core.MISSING, False, core.Core(run.env)._git) == (
        "finish",
        "next: emb choices --finish claim-a",
    )


def test_closed_run_with_its_branch_keeps_the_merge_line(
    tmp_path: Path, run: record.Run
) -> None:
    repo = make_repo(tmp_path, run.env)
    git(repo, "branch", "worker", env=run.env)
    run = replace(
        run,
        status=record.CLOSED,
        main=str(repo),
        branch="worker",
        base_branch=None,
        commits=("f" * 40,),
    )
    step, line = steps.derive(run, core.MISSING, False, core.Core(run.env)._git)
    assert step == "merge" and line is not None
