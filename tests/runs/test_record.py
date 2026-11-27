import json
import re
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import paths
from ember.flows import plan
from ember.flows import record as flows
from ember.panels import members as panel_members
from ember.panels import record as panels
from ember.runs import record
from ember.runs.record import Run


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }


def _run(env: dict[str, str], run_id: str = "codex-20260101-120000-abcdef") -> Run:
    return Run(
        id=run_id,
        engine="codex",
        model="gpt-5",
        effort="high",
        read_only=False,
        repo="/repo",
        main="/main",
        common="/main/.git",
        worktree=f"/repo/.ember/worktrees/{run_id}",
        branch=f"ember/codex/{run_id}",
        base="a" * 40,
        sandbox="bubblewrap",
        network=False,
        read=("/data",),
        read_effective=("/opt/shared", "/data"),
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
        "branch", "base", "base_branch", "sandbox", "network", "read", "read_effective",
        "trailers", "claim", "status",
        "turn", "turn_state", "reply", "expected_head", "started_at",
        "updated_at", "ended_at", "last_exit", "last_reason", "commits",
        "subject", "main", "common", "detached", "last_screen_change_at",
        "capacity_retries", "capacity_wait", "sandbox_paths", "parent",
        "opencode_id_format", "owner_wait", "owner_read", "owner_denied", "answered_at",
        "last_variant",
        "last_start_failure",
        "scoped",
    }  # fmt: skip
    assert [entry.name for entry in folder.iterdir()] == ["run.json"]


def test_missing_run(env: dict[str, str]) -> None:
    with pytest.raises(record.RecordMissingError):
        record.load("codex-20260101-120000-000000", env)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": "lost"}, "invalid status"),
        ({"last_screen_change_at": True}, "invalid last_screen_change_at"),
        ({"last_screen_change_at": "now"}, "invalid last_screen_change_at"),
        ({"turn_state": "odd"}, "invalid turn_state"),
        ({"turn": True}, "invalid turn"),
        ({"turn": "1"}, "invalid turn"),
        ({"read": ["/a", 1]}, "invalid read"),
        ({"model": None}, "invalid model"),
        ({"speed": "slow"}, "invalid speed"),
        ({"speed": True}, "invalid speed"),
        ({"selection_origin": "other"}, "invalid selection_origin"),
        ({"selection_origin": True}, "invalid selection_origin"),
        ({"engine": "opencode", "speed": "fast"}, "invalid speed"),
        ({"main": 3}, "invalid main"),
        ({"common": []}, "invalid common"),
        ({"sandbox": None}, "invalid sandbox"),
        ({"sandbox": "codex-bundled"}, "invalid sandbox"),
        ({"network": "on"}, "invalid network"),
        ({"detached": 1}, "invalid detached"),
        ({"scoped": 1}, "invalid scoped"),
        ({"scoped": None}, "invalid scoped"),
        ({"read_effective": ["/a", 2]}, "invalid read_effective"),
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


@pytest.mark.parametrize("speed", ["standard", "fast", None])
def test_speed_round_trip(env: dict[str, str], speed: str | None) -> None:
    run = replace(_run(env), speed=speed)
    record.save(run)
    assert record.load(run.id, env).speed == speed
    assert ("speed" in run.to_json()) == (speed is not None)


@pytest.mark.parametrize("origin", [None, *record.SELECTION_ORIGINS])
def test_preparation_retry_selection_origin_round_trip(
    env: dict[str, str], origin: str | None
) -> None:
    run = replace(_run(env), selection_origin=origin)
    record.save(run)
    assert record.load(run.id, env).selection_origin == origin
    assert ("selection_origin" in run.to_json()) == (origin is not None)


def test_record_missing_a_field(env: dict[str, str]) -> None:
    run = _run(env)
    document = run.to_json()
    del document["subject"]
    (run.folder / "run.json").write_text(json.dumps(document))
    with pytest.raises(record.RecordError, match="has no subject"):
        record.load(run.id, env)


@pytest.mark.parametrize("scoped", [False, True])
def test_scope_mode_round_trip(env: dict[str, str], scoped: bool) -> None:
    run = replace(_run(env), scoped=scoped)
    record.save(run)
    assert record.load(run.id, env).scoped is scoped


def test_legacy_record_uses_tree_only(env: dict[str, str]) -> None:
    run = _run(env)
    document = run.to_json()
    document.pop("scoped")
    assert record.from_json(document, run.folder, env).scoped is False


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


def test_list_runs_skips_flow_folders(env: dict[str, str]) -> None:
    run = _run(env)
    record.save(run)
    record.make_run_dir("flow-20260101-120000-abcdef", env)
    assert record.list_runs(env) == ([run], [])


def test_list_runs_skips_panel_folders(env: dict[str, str]) -> None:
    run = _run(env)
    record.save(run)
    record.make_run_dir("panel-20260101-120000-abcdef", env)
    assert record.list_runs(env) == ([run], [])


def test_list_runs_skips_unpublished_records(env: dict[str, str]) -> None:
    run = _run(env)
    record.save(run)
    with paths.create_record_dir(
        paths.runs_dir(env) / record.new_id("codex"), root=paths.state_dir(env)
    ) as temporary:
        assert not record.is_run_folder(temporary.name)
        with pytest.raises(record.RecordError, match="not a valid run id"):
            record.check_id(temporary.name)
        (temporary / record.RECORD_NAME).write_text("{}")
        assert record.list_runs(env) == ([run], [])


def test_record_without_base_branch_loads(env: dict[str, str]) -> None:
    run = _run(env)
    document = run.to_json()
    del document["base_branch"]
    (run.folder / "run.json").write_text(json.dumps(document))
    assert record.load(run.id, env).base_branch is None


def test_record_without_checkout_fields_loads(env: dict[str, str]) -> None:
    run = _run(env)
    document = run.to_json()
    del document["main"]
    del document["common"]
    (run.folder / "run.json").write_text(json.dumps(document))
    loaded = record.load(run.id, env)
    assert loaded.main is None and loaded.common is None


def test_legacy_read_only_record_has_no_owned_detached_worktree(
    env: dict[str, str],
) -> None:
    run = replace(_run(env), read_only=True, branch=None, worktree="/repo")
    document = run.to_json()
    del document["detached"]
    (run.folder / "run.json").write_text(json.dumps(document))
    assert not record.load(run.id, env).detached
    record.save(replace(run, detached=True, worktree="/detached"))
    assert record.load(run.id, env).detached


def _outcome() -> dict[str, Any]:
    return {
        "state": "done",
        "reason": None,
        "provider_error": False,
        "signed_out": False,
        "incomplete": None,
    }


def test_turn_results_empty(env: dict[str, str]) -> None:
    assert record.turn_results(_run(env)) == ([], [])


def test_turn_results_legacy_and_number_order(env: dict[str, str]) -> None:
    run = replace(_run(env), turn=12)
    for turn in range(1, 13):
        record.outcome_path(run, turn).write_text(json.dumps(_outcome()))
    turns, errors = record.turn_results(run)
    assert not errors
    assert turns == [
        {"turn": turn, "exit": None, **_outcome(), "seconds": None, "denied_paths": []}
        for turn in range(1, 13)
    ]


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"\xff",
        b"{",
        b"[]",
        b"{}",
        b"[" * 2000,
        json.dumps({**_outcome(), "state": "unknown"}).encode(),
        json.dumps({**_outcome(), "state": None, "exit": 0}).encode(),
        json.dumps({**_outcome(), "reason": 1}).encode(),
        json.dumps({**_outcome(), "provider_error": 1}).encode(),
        json.dumps({**_outcome(), "provider_error": None}).encode(),
        json.dumps({**_outcome(), "signed_out": "false"}).encode(),
        json.dumps({**_outcome(), "signed_out": None}).encode(),
        json.dumps({**_outcome(), "incomplete": []}).encode(),
        json.dumps({**_outcome(), "exit": True}).encode(),
        json.dumps({**_outcome(), "exit": "0"}).encode(),
        json.dumps({**_outcome(), "seconds": True}).encode(),
        json.dumps({**_outcome(), "seconds": -1}).encode(),
        json.dumps({**_outcome(), "seconds": float("nan")}).encode(),
        json.dumps({**_outcome(), "seconds": float("inf")}).encode(),
        json.dumps({**_outcome(), "seconds": 10**500}).encode(),
    ],
)
def test_broken_outcome_preserves_other_turns(
    env: dict[str, str], content: bytes | None
) -> None:
    run = replace(_run(env), turn=3)
    good = {**_outcome(), "exit": 0, "seconds": 0.0, "denied_paths": []}
    for turn in (1, 3):
        record.outcome_path(run, turn).write_text(json.dumps(good))
    broken = record.outcome_path(run, 2)
    if content is not None:
        broken.write_bytes(content)
    turns, errors = record.turn_results(run)
    assert turns == [
        {"turn": 1, **good},
        {"turn": 2, **dict.fromkeys(good)},
        {"turn": 3, **good},
    ]
    assert len(errors) == 1
    assert str(broken) in str(errors[0])


def _open_outcome(**fields: object) -> dict[str, object]:
    return {
        **dict.fromkeys(_outcome()),
        "exit": None,
        "seconds": 1.0,
        "denied_paths": [],
        **fields,
    }


def test_an_open_turn_reports_its_provider_wait(env: dict[str, str]) -> None:
    run = replace(_run(env), turn=1, status=record.RUNNING, turn_state=record.SENT)
    waiting = {"kind": "capacity", "message": "at capacity", "since": 12.5}
    path = record.outcome_path(run, 1)
    path.write_text(json.dumps(_open_outcome(waiting={**waiting, "other": 1})))
    assert record.load_outcome(run, 1)["waiting"] == waiting
    assert record.provider_wait(run) == waiting
    turns, errors = record.turn_results(run)
    assert errors == []
    assert turns[0]["waiting"] == waiting
    path.write_text(json.dumps(_open_outcome()))
    assert "waiting" not in record.load_outcome(run, 1)
    assert "waiting" not in record.turn_results(run)[0][0]
    assert record.provider_wait(run) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"turn": 0},
        {"status": record.FAILED},
        {"status": record.CLOSED},
        {"status": record.WAITING},
        {"turn_state": record.COLLECTED},
        {"turn_state": record.COMMITTED},
    ],
)
def test_a_run_without_an_open_turn_reports_no_provider_wait(
    env: dict[str, str], changes: dict[str, Any]
) -> None:
    run = replace(_run(env), turn=1, status=record.RUNNING, turn_state=record.SENT)
    assert record.provider_wait(run) is None
    waiting = {"kind": "outage", "message": "Reconnecting...", "since": 1}
    record.outcome_path(run, 1).write_text(json.dumps(_open_outcome(waiting=waiting)))
    assert record.provider_wait(run) == waiting
    assert record.provider_wait(replace(run, **changes)) is None


def test_a_resent_attempt_reports_its_provider_wait(env: dict[str, str]) -> None:
    run = replace(_run(env), turn=1, status=record.RUNNING, turn_state=record.SENT)
    waiting = {"kind": "capacity", "message": "m", "since": 1, "interrupted": True}
    earlier = {**_outcome(), "state": "failed", "provider_error": True}
    record.outcome_path(run, 1).write_text(
        json.dumps({**earlier, "exit": None, "seconds": 1.0, "waiting": waiting})
    )
    assert record.provider_wait(run) == waiting


@pytest.mark.parametrize(
    "waiting",
    [
        [],
        "capacity",
        {"kind": "busy", "message": "m", "since": 1},
        {"kind": "capacity", "message": None, "since": 1},
        {"kind": "capacity", "message": "m", "since": -1},
        {"kind": "capacity", "message": "m"},
    ],
)
def test_an_invalid_provider_wait_is_a_broken_outcome(
    env: dict[str, str], waiting: object
) -> None:
    run = replace(_run(env), turn=1)
    record.outcome_path(run, 1).write_text(json.dumps(_open_outcome(waiting=waiting)))
    with pytest.raises(record.RecordError, match="invalid waiting"):
        record.load_outcome(run, 1)
    assert record.provider_wait(run) is None


def test_unreadable_outcome(env: dict[str, str]) -> None:
    run = replace(_run(env), turn=1)
    record.outcome_path(run, 1).mkdir()
    turns, errors = record.turn_results(run)
    assert all(value is None for key, value in turns[0].items() if key != "turn")
    assert len(errors) == 1
    assert str(record.outcome_path(run, 1)) in str(errors[0])


@pytest.mark.parametrize("activity", [None, 0, 1234.5])
def test_screen_activity_round_trip(
    env: dict[str, str], activity: float | None
) -> None:
    run = replace(_run(env), last_screen_change_at=activity)
    record.save(run)
    saved = record.load(run.id, env)
    assert saved.last_screen_change_at == activity
    if activity is not None:
        assert isinstance(saved.last_screen_change_at, float)


def test_older_record_has_no_screen_activity(env: dict[str, str]) -> None:
    run = _run(env)
    document = run.to_json()
    del document["last_screen_change_at"]
    (run.folder / "run.json").write_text(json.dumps(document))
    assert record.load(run.id, env).last_screen_change_at is None


def test_older_records_without_capacity_settings_load(env: dict[str, str]) -> None:
    run = _run(env)
    data = run.to_json()
    data.pop("capacity_retries")
    data.pop("capacity_wait")
    loaded = record.from_json(data, run.folder, env)
    assert (loaded.capacity_retries, loaded.capacity_wait) == (2, 30)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("capacity_retries", -1),
        ("capacity_retries", 11),
        ("capacity_retries", True),
        ("capacity_wait", 4),
        ("capacity_wait", 601),
        ("capacity_wait", "30"),
    ],
)
def test_invalid_capacity_settings_in_record(
    env: dict[str, str], name: str, value: object
) -> None:
    run = _run(env)
    with pytest.raises(record.RecordError, match=name):
        record.from_json({**run.to_json(), name: value}, run.folder, env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("provider_kind", "unknown"),
        ("attempts", {}),
        ("attempts", [{"id": "attempt-1", "sent": False}]),
        ("retry_at", True),
        ("retry_at", 10**500),
        ("retry_clock", float("nan")),
    ],
)
def test_invalid_retry_journal_is_rejected(
    env: dict[str, str], name: str, value: object
) -> None:
    run = _run(env)
    record.outcome_path(run, 1).write_text(json.dumps({**_outcome(), name: value}))
    with pytest.raises(record.RecordError, match=name):
        record.load_outcome(run, 1)


@pytest.mark.parametrize(
    "denials",
    [
        None,
        {},
        ["/home/a"],
        [{"path": "relative", "reason": "hidden"}],
        [{"path": "/a", "reason": "other"}],
        [{"path": "/a", "reason": "startup file", "allow_path": "/a"}],
        [{"path": "/a", "reason": "secret location", "allow_path": "/a"}],
    ],
)
def test_invalid_denials_are_record_errors(
    env: dict[str, str], denials: object
) -> None:
    run = replace(_run(env), turn=1)
    record.outcome_path(run, 1).write_text(
        json.dumps({**_outcome(), "denied_paths": denials})
    )
    with pytest.raises(record.RecordError, match="denied_paths"):
        record.load_outcome(run, 1)


@pytest.mark.parametrize("reason", ["hidden", "startup file"])
def test_run_denials_keep_run_and_turn_and_legacy_default(
    env: dict[str, str], reason: str
) -> None:
    run = replace(_run(env), turn=2)
    record.save(run)
    item = {
        "path": "/home/a/.profile",
        "reason": reason,
        "allow_path": None,
    }
    record.outcome_path(run, 1).write_text(
        json.dumps({**_outcome(), "denied_paths": [item]})
    )
    record.outcome_path(run, 2).write_text(json.dumps(_outcome()))
    assert record.load_outcome(run, 2)["denied_paths"] == []
    assert record.referenced_denials([run.id, run.id, None, "missing-id"], env) == [
        {**item, "run": run.id, "turn": 1}
    ]


def _dead_parent() -> dict[str, Any]:
    parent = record.parent("gapcheck")
    assert parent is not None
    return {**parent, "start": parent["start"] + 1}


def test_record_without_parent_loads(env: dict[str, str]) -> None:
    run = _run(env)
    document = run.to_json()
    del document["parent"]
    (run.folder / "run.json").write_text(json.dumps(document))
    loaded = record.load(run.id, env)
    assert loaded.parent is None and not record.orphaned(loaded)


def test_parent_round_trip(env: dict[str, str]) -> None:
    parent = record.parent("panel", "panel-20260101-120000-abcdef")
    assert parent is not None and parent["command"] == "panel"
    run = replace(_run(env), parent=parent)
    record.save(run)
    assert record.load(run.id, env).parent == parent


@pytest.mark.parametrize(
    "parent",
    [
        {"command": "run", "id": None, "pid": 1, "start": 1},
        {"command": "flow", "id": 7, "pid": 1, "start": 1},
        {"command": "flow", "id": None, "pid": True, "start": 1},
        {"command": "flow", "id": None, "pid": 1},
        {"command": "flow", "id": None, "pid": 1, "start": 1, "extra": 1},
    ],
)
def test_invalid_parent_is_refused(env: dict[str, str], parent: dict[str, Any]) -> None:
    run = _run(env)
    document = {**run.to_json(), "parent": parent}
    (run.folder / "run.json").write_text(json.dumps(document))
    with pytest.raises(record.RecordError, match="invalid parent"):
        record.load(run.id, env)


@pytest.mark.parametrize(
    ("status", "alive", "expected"),
    [
        (record.RUNNING, True, False),
        (record.RUNNING, False, True),
        (record.WAITING, False, True),
        (record.STARTING, False, True),
        (record.CLOSED, False, False),
        (record.FAILED, False, False),
    ],
)
def test_orphaned(
    env: dict[str, str], status: str, alive: bool, expected: bool
) -> None:
    parent = record.parent("flow", "flow-20260101-120000-abcdef") if alive else None
    run = replace(_run(env), status=status, parent=parent or _dead_parent())
    assert record.orphaned(run) is expected


def test_run_without_parent_is_never_orphaned(env: dict[str, str]) -> None:
    run = replace(_run(env), status=record.RUNNING, parent=None)
    assert not record.orphaned(run)
    assert record.waiting_for_flow(run) is None


def _flow(env: dict[str, str], tmp_path: Path, status: str) -> dict[str, Any]:
    source = plan.from_json(
        {
            "dir": str(tmp_path),
            "tasks": [
                {"id": "a", "subject": "feat: a", "task": "Task", "files": ["a"]}
            ],
        },
        tmp_path / "plan.json",
    )
    worker = plan.Worker("codex", "model", None, "option")
    flow = flows.new_flow(
        flows.new_id(),
        source,
        top=str(tmp_path),
        main=str(tmp_path),
        worktree=str(tmp_path / "flow"),
        base="a" * 40,
        writer=worker,
        reviewer=worker,
        stall_minutes=10,
        claim=None,
    )
    flow["status"] = status
    flows.create(flow, source.text, env)
    return flow


def _flow_run(env: dict[str, str], flow: dict[str, Any], status: str) -> Run:
    return replace(
        _run(env),
        status=status,
        parent={**_dead_parent(), "command": "flow", "id": flow["id"]},
    )


@pytest.mark.parametrize("status", record.STATUSES)
@pytest.mark.parametrize("flow_status", flows.FLOW_STATUSES)
@pytest.mark.parametrize("field", ["writer_run", "reviewer_run", None])
def test_flow_parent_state(
    env: dict[str, str],
    tmp_path: Path,
    status: str,
    flow_status: str,
    field: str | None,
) -> None:
    flow = _flow(env, tmp_path, flow_status)
    run = _flow_run(env, flow, status)
    if field:
        flow["tasks"][0][field] = run.id
        flows.save(flow, env)
    active = status in record.OPEN
    listed = active and field is not None
    expected = (
        record.PARENT_NONE
        if not active
        else record.PARENT_ORPHANED
        if not listed or flow_status == "done"
        else record.PARENT_LEAD
        if flow_status == "stopped"
        else record.PARENT_RESUME
    )
    state = record.parent_state(run)
    assert state.state == expected
    assert record.waiting_for_flow(run) == (
        flow["id"] if expected == record.PARENT_LEAD else None
    )
    assert record.orphaned(run) is (expected == record.PARENT_ORPHANED)


@pytest.mark.parametrize("key", ["run", "preparation_run"])
def test_flow_pending_close_holds_the_run(
    env: dict[str, str], tmp_path: Path, key: str
) -> None:
    flow = _flow(env, tmp_path, "failed")
    run = _flow_run(env, flow, record.WAITING)
    effect = {"n": 1, "run": None, key: run.id}
    flow["tasks"][0]["answer_effect"] = effect
    flows.save(flow, env)
    assert record.parent_state(run).state == record.PARENT_RESUME


def test_resumable_flow_waits_with_its_age(env: dict[str, str], tmp_path: Path) -> None:
    flow = _flow(env, tmp_path, "failed")
    run = _flow_run(env, flow, record.WAITING)
    flow["tasks"][0]["writer_run"] = run.id
    flow["ended_at"] = 1000.0
    flows.save(flow, env)
    state = record.parent_state(run)
    assert (state.status, state.ended_at, state.ended) == ("failed", 1000.0, True)
    assert record.parent_wait(state, 1000.0 + 3 * 86400) == (
        f"waiting for flow {flow['id']} to resume (failed 3 days ago)"
    )
    assert record.parent_next(state) == f"emb flow --resume {flow['id']}"
    flow["status"] = "running"
    flow["ended_at"] = None
    flows.save(flow, env)
    state = record.parent_state(run)
    assert not state.ended and state.ended_at is not None
    assert record.parent_wait(state, state.ended_at + 300) == (
        f"waiting for flow {flow['id']} to resume (last updated 5 min ago)"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "1 min"),
        (59 * 60, "59 min"),
        (3600, "1 h"),
        (47 * 3600, "47 h"),
        (48 * 3600, "2 days"),
    ],
)
def test_parent_wait_ages(seconds: float, text: str) -> None:
    state = record.ParentState(
        record.PARENT_RESUME, "flow", "f", "failed", 0.0, ended=True
    )
    assert (
        record.parent_wait(state, seconds)
        == f"waiting for flow f to resume (failed {text} ago)"
    )


def test_live_lock_holds_the_run(env: dict[str, str], tmp_path: Path) -> None:
    from ember.flows import lock

    flow = _flow(env, tmp_path, "running")
    run = _flow_run(env, flow, record.WAITING)
    flow["tasks"][0]["writer_run"] = run.id
    flows.save(flow, env)
    with lock.hold(flow["id"], env):
        assert record.parent_state(run).state == record.PARENT_LIVE
        assert not record.orphaned(run)
    assert record.parent_state(run).state == record.PARENT_RESUME


def test_live_starter_holds_the_run(env: dict[str, str], tmp_path: Path) -> None:
    flow = _flow(env, tmp_path, "done")
    parent = record.parent("flow", flow["id"])
    run = replace(_run(env), status=record.RUNNING, parent=parent)
    assert record.parent_state(run).state == record.PARENT_LIVE


def test_unreadable_lock_table_falls_back_to_the_flow(
    env: dict[str, str], tmp_path: Path
) -> None:
    flow = _flow(env, tmp_path, "failed")
    run = _flow_run(env, flow, record.WAITING)
    flow["tasks"][0]["writer_run"] = run.id
    flows.save(flow, env)
    state = record.parent_state(run, tmp_path / "absent")
    assert state.state == record.PARENT_RESUME


def _panel(
    env: dict[str, str], tmp_path: Path, status: str, member_status: str, run_id: str
) -> dict[str, Any]:
    member = panel_members.Member("codex", "model", None, "option").entry(1)
    member.update(status=member_status, run=run_id)
    panel = panels.new_panel(
        panels.new_id(),
        dir=str(tmp_path),
        question_file=str(tmp_path / "question.md"),
        read=(),
        claim=None,
        stall_minutes=10,
        timeout_minutes=60,
        members=[member],
        left_out=[],
    )
    panel["status"] = status
    panels.create(panel, "Question\n", "Brief\n", env)
    return panel


@pytest.mark.parametrize(
    ("status", "member_status", "expected"),
    [
        ("incomplete", "timed out", record.PARENT_RESUME),
        ("running", "running", record.PARENT_RESUME),
        ("incomplete", "answered", record.PARENT_ORPHANED),
        ("done", "interrupted", record.PARENT_ORPHANED),
    ],
)
def test_panel_parent_state(
    env: dict[str, str],
    tmp_path: Path,
    status: str,
    member_status: str,
    expected: str,
) -> None:
    run = replace(_run(env), status=record.WAITING)
    panel = _panel(env, tmp_path, status, member_status, run.id)
    run = replace(run, parent={**_dead_parent(), "command": "panel", "id": panel["id"]})
    state = record.parent_state(run)
    assert state.state == expected
    if expected == record.PARENT_RESUME:
        assert state.ended_at is not None
        assert record.parent_next(state) == f"emb panel --recover {panel['id']}"
        assert record.parent_wait(state, state.ended_at + 60) == (
            f"waiting for panel {panel['id']} to recover (last updated 1 min ago)"
        )


@pytest.mark.parametrize("state", ["missing", "invalid", "no-id"])
def test_missing_flow_does_not_exempt_orphan(env: dict[str, str], state: str) -> None:
    parent_id = {"invalid": "../flow", "no-id": None}.get(state, flows.new_id())
    run = replace(
        _run(env),
        parent={**_dead_parent(), "command": "flow", "id": parent_id},
    )
    assert record.waiting_for_flow(run) is None
    assert record.orphaned(run)


def test_unreadable_flow_is_unknown(env: dict[str, str]) -> None:
    flow_id = flows.new_id()
    folder = record.make_run_dir(flow_id, env)
    (folder / flows.FLOW_NAME).write_text('{"status": "stopped"}')
    run = replace(
        _run(env),
        parent={**_dead_parent(), "command": "flow", "id": flow_id},
    )
    state = record.parent_state(run)
    assert state.state == record.PARENT_UNKNOWN and state.error
    assert not record.orphaned(run)
    assert record.parent_next(state) == f"emb flow --status {flow_id}"


@pytest.mark.parametrize(
    "model", ["anthropic/claude-x", "openrouter/anthropic/claude-x"]
)
@pytest.mark.parametrize("marked", [False, True])
def test_opencode_id_migration_load_update_reload(
    env: dict[str, str], model: str, marked: bool
) -> None:
    run = replace(_run(env), engine="opencode", model=model)
    value = run.to_json()
    if not marked:
        value.pop("opencode_id_format")
    path = run.folder / record.RECORD_NAME
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    loaded = record.load(run.id, env)
    expected = (
        model if marked or model.startswith("openrouter/") else "openrouter/" + model
    )
    assert loaded.model == expected
    assert path.read_bytes() == before
    record.update(loaded, {"subject": "updated"})
    assert json.loads(path.read_text())["opencode_id_format"] == 1
    assert record.load(run.id, env).model == expected


def test_owner_fields_round_trip_and_older_defaults(env: dict[str, str]) -> None:
    run = replace(
        _run(env),
        owner_wait=("owner: allow /folder",),
        owner_read=("/folder",),
        answered_at=2.5,
        last_variant="uncertain",
    )
    record.save(run)
    assert record.load(run.id, env) == run
    data = run.to_json()
    for key in (
        "owner_wait",
        "owner_read",
        "answered_at",
        "last_variant",
        "last_start_failure",
    ):
        del data[key]
    old = record.from_json(data, run.folder, env)
    assert old.owner_wait == old.owner_read == ()
    assert old.answered_at is None and old.last_variant is None
    assert old.last_start_failure is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("owner_wait", [1]),
        ("owner_read", None),
        ("answered_at", -1),
        ("answered_at", float("inf")),
        ("answered_at", True),
        ("last_variant", 1),
    ],
)
def test_invalid_owner_fields(env: dict[str, str], key: str, value: object) -> None:
    run = _run(env)
    with pytest.raises(record.RecordError, match=key):
        record.from_json({**run.to_json(), key: value}, run.folder, env)


@pytest.mark.parametrize(
    "state", ["running", "ok", "failed", "timeout", "interrupted", "not-started"]
)
def test_preparation_round_trip(env: dict[str, str], state: str) -> None:
    preparation: dict[str, Any] = {
        "command": "npm ci",
        "state": state,
        "log": "/run/prepare.log",
        "pid": 123,
        "start": 456,
    }
    if state != "running":
        preparation.update(exit=0, seconds=1.5, left_behind=[], message=None)
    run = replace(
        _run(env), preparation=preparation, start_argv=("emb", "run", "--read-only")
    )
    record.save(run)
    assert record.load(run.id, env) == run


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "unknown"},
        {"pid": True},
        {"start": -1},
        {"pid": 123},
        {"command": None},
        {"log": 3},
        {"state": "ok", "exit": 0, "seconds": -1, "left_behind": []},
        {"state": "failed", "exit": False, "seconds": 0, "left_behind": []},
        {"state": "failed", "seconds": 0, "left_behind": [3]},
    ],
)
def test_invalid_preparation_refuses_record(
    env: dict[str, str], changes: dict[str, Any]
) -> None:
    run = _run(env)
    preparation = {
        "command": "true",
        "log": "/run/prepare.log",
        "state": "running",
        **changes,
    }
    value = {**run.to_json(), "preparation": preparation}
    with pytest.raises(record.RecordError, match="invalid preparation"):
        record.from_json(value, run.folder, env)
