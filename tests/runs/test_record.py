import json
import re
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import paths
from ember.runs import record
from ember.runs.record import Run


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def _run(env: dict[str, str], run_id: str = "codex-20260101-120000-abcdef") -> Run:
    return Run(
        id=run_id,
        engine="codex",
        model="gpt-5",
        effort="high",
        read_only=False,
        repo="/repo",
        worktree=f"/repo/.ember/worktrees/{run_id}",
        branch=f"ember/codex/{run_id}",
        base="a" * 40,
        sandbox="bubblewrap",
        read=("/data",),
        trailers=("Co-Authored-By: X <x@y>",),
        claim=None,
        status=record.STARTING,
        turn=0,
        turn_state=None,
        reply=None,
        expected_head=None,
        started_at=1.5,
        updated_at=1.5,
        ended_at=None,
        last_exit=None,
        last_reason=None,
        commits=(),
        subject="Do the thing",
        folder=record.make_run_dir(run_id, env),
        env=env,
    )


def test_new_id_format() -> None:
    run_id = record.new_id("opencode", now=0)
    assert re.fullmatch(r"opencode-19700101-000000-[0-9a-f]{6}", run_id)
    assert record.new_id("codex") != record.new_id("codex")


@pytest.mark.parametrize("run_id", ["", "../x", "short", "A-upper-case", "a" * 65])
def test_invalid_ids(run_id: str, env: dict[str, str]) -> None:
    with pytest.raises(record.RecordError):
        record.load(run_id, env)


def test_save_and_load_round_trip(env: dict[str, str]) -> None:
    run = _run(env)
    record.save(run)
    folder = paths.runs_dir(env) / run.id
    assert run.folder == folder
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "run.json").stat().st_mode) == 0o600
    loaded = record.load(run.id, env)
    assert loaded == run
    assert loaded.folder == folder
    assert loaded.env is env
    stored = json.loads((folder / "run.json").read_text())
    assert stored["read"] == ["/data"]
    assert stored["commits"] == []
    assert set(stored) == {
        "id", "engine", "model", "effort", "read_only", "repo", "worktree",
        "branch", "base", "sandbox", "read", "trailers", "claim", "status",
        "turn", "turn_state", "reply", "expected_head", "started_at",
        "updated_at", "ended_at", "last_exit", "last_reason", "commits",
        "subject",
    }  # fmt: skip
    assert [entry.name for entry in folder.iterdir()] == ["run.json"]


def test_missing_run(env: dict[str, str]) -> None:
    with pytest.raises(record.RecordMissingError):
        record.load("codex-20260101-120000-000000", env)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": "lost"}, "invalid status"),
        ({"turn_state": "odd"}, "invalid turn_state"),
        ({"turn": True}, "invalid turn"),
        ({"turn": "1"}, "invalid turn"),
        ({"read": ["/a", 1]}, "invalid read"),
        ({"model": None}, "invalid model"),
        ({"id": "codex-20260101-120000-ffffff"}, "names another run"),
    ],
)
def test_invalid_records(
    env: dict[str, str], change: dict[str, Any], message: str
) -> None:
    run = _run(env)
    document = {**run.to_json(), **change}
    (run.folder / "run.json").write_text(json.dumps(document))
    with pytest.raises(record.RecordError, match=message):
        record.load(run.id, env)


def test_record_missing_a_field(env: dict[str, str]) -> None:
    run = _run(env)
    document = run.to_json()
    del document["subject"]
    (run.folder / "run.json").write_text(json.dumps(document))
    with pytest.raises(record.RecordError, match="has no subject"):
        record.load(run.id, env)


def test_integer_times_are_read_as_floats(env: dict[str, str]) -> None:
    run = _run(env)
    (run.folder / "run.json").write_text(json.dumps({**run.to_json(), "started_at": 2}))
    assert record.load(run.id, env).started_at == 2.0


def test_update_keeps_a_close_made_elsewhere(env: dict[str, str]) -> None:
    run = _run(env)
    record.save(run)
    changed = record.update(run, {"turn": 1, "status": record.RUNNING})
    assert (changed.turn, changed.status) == (1, record.RUNNING)
    assert changed.updated_at > run.updated_at
    record.save(replace(changed, status=record.CLOSED))
    with pytest.raises(record.RunClosedError):
        record.update(changed, {"status": record.WAITING})
    assert record.load(run.id, env).status == record.CLOSED
    after = record.update(changed, {"ended_at": 9.0}, allow_closed=True)
    assert (after.status, after.ended_at) == (record.CLOSED, 9.0)


def test_list_runs_skips_broken_records(env: dict[str, str]) -> None:
    assert record.list_runs(env) == ([], [])
    later = replace(_run(env, "codex-20260101-120000-000002"), started_at=5.0)
    earlier = _run(env, "codex-20260101-120000-000003")
    record.save(later)
    record.save(earlier)
    broken = paths.runs_dir(env) / "codex-20260101-120000-000001"
    broken.mkdir()
    (broken / "run.json").write_text("[]")
    (paths.runs_dir(env) / "stray-file").write_text("")
    runs, errors = record.list_runs(env)
    assert [run.id for run in runs] == [earlier.id, later.id]
    assert [str(error) for error in errors] == [
        "codex-20260101-120000-000001: run.json is not an object"
    ]
    assert errors[0].run_id == "codex-20260101-120000-000001"


def test_write_once(tmp_path: Path) -> None:
    path = tmp_path / "turn-1.md"
    assert record.write_once(path, "first")
    assert not record.write_once(path, "second")
    assert path.read_text() == "first"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in tmp_path.iterdir()] == ["turn-1.md"]
