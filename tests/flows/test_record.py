import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember import paths, prepare
from ember.flows import plan, record
from ember.runs import record as runs
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return make_env(tmp_path)


@pytest.fixture
def flow(tmp_path: Path, env: dict[str, str]) -> dict[str, Any]:
    source = plan.from_json(
        {
            "dir": str(tmp_path),
            "tasks": [
                {
                    "id": "a",
                    "subject": "feat: a",
                    "task": "Implement a",
                    "files": ["a"],
                },
                {
                    "id": "b",
                    "subject": "feat: b",
                    "task": "Implement b",
                    "files": ["b"],
                },
            ],
        },
        tmp_path / "plan.json",
    )
    worker = plan.Worker("codex", "gpt-5", "high", "option")
    return record.new_flow(
        record.new_id(0),
        source,
        top=str(tmp_path),
        main=str(tmp_path),
        common=str(tmp_path / ".git"),
        worktree=str(tmp_path / "worktrees" / "flow"),
        base="a" * 40,
        writer=worker,
        reviewer=worker,
        stall_minutes=10,
        claim=None,
        now=0,
    )


@pytest.fixture
def created(flow: dict[str, Any], env: dict[str, str]) -> Path:
    return record.create(flow, '{"dir": "/example", "tasks": []}\n', env)


def test_creation(flow: dict[str, Any], created: Path, env: dict[str, str]) -> None:
    assert created == paths.runs_dir(env) / flow["id"]
    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    for name in (record.FLOW_NAME, record.PLAN_NAME, record.EVENTS_NAME):
        assert stat.S_IMODE((created / name).stat().st_mode) == 0o600
    assert (
        created / record.PLAN_NAME
    ).read_text() == '{"dir": "/example", "tasks": []}\n'
    assert record.load(flow["id"], env) == flow
    (event,) = record.read_events(flow["id"], env)
    assert (
        event["seq"] == 1 and event["kind"] == "flow" and event["status"] == "starting"
    )
    assert flow["wave"] == flow["answers_seen"] == 0
    assert flow["plan_waves"] == [["a", "b"]]
    assert flow["status"] == "starting" and flow["tip"] == flow["base"]
    assert flow["review_budget"] == {
        "value": 2,
        "unit": "fix_rounds",
        "source": "default",
    }
    assert record.task_entry(flow, "a")["status"] == "pending"
    record.task_entry(flow, "a")["findings"].append({"problem": "a"})
    assert record.task_entry(flow, "b")["findings"] == []
    assert record.task_entry(flow, "a")["corrections"] == []
    record.task_entry(flow, "a")["corrections"].append("answers/1.md")
    assert record.task_entry(flow, "b")["corrections"] == []
    with pytest.raises(KeyError):
        record.task_entry(flow, "unknown")


def test_preparation_round_trip(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    attempt = {
        "command": "true",
        "pid": 123,
        "start": 456,
        **prepare.Result(
            "failed", 8, 0.1, str(created / "prepare.log"), [], "exited 8"
        ).as_dict(),
    }
    flow["preparation_history"] = [attempt]
    flow["preparation"] = {**attempt, "state": "ok", "exit": 0, "message": None}
    flow["preparation_halt"] = "child preparation failed"
    flow["tasks"][0]["preparation_blocked"] = "reviewer"
    record.save(flow, env)
    assert record.load(flow["id"], env) == flow


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("preparation", None),
        ("preparation", {}),
        ("preparation", {"command": "true", "state": "unknown", "log": "log"}),
        (
            "preparation",
            {"command": "true", "state": "running", "log": "log", "pid": 1},
        ),
        ("preparation_history", None),
        ("preparation_history", {}),
        ("preparation_history", [{}]),
        ("preparation_halt", 79),
    ],
)
def test_invalid_preparation(
    flow: dict[str, Any], created: Path, env: dict[str, str], field: str, value: object
) -> None:
    flow[field] = value
    (created / record.FLOW_NAME).write_text(json.dumps(flow))
    with pytest.raises(record.FlowError, match="preparation"):
        record.load(flow["id"], env)


def test_create_does_not_overwrite(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    with pytest.raises(record.FlowError, match="already exists"):
        record.create(flow, "changed", env)
    assert "example" in (created / record.PLAN_NAME).read_text()
    assert list(created.parent.iterdir()) == [created]


def test_creation_is_invisible_until_complete(
    flow: dict[str, Any], env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = runs.write_private
    observed: list[str] = []

    def write(path: Path, text: str) -> None:
        original(path, text)
        observed.append(path.name)
        assert path.parent.parent == paths.runs_dir(env)
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert not record.folder(flow["id"], env).exists()
        assert record.list_flows(env) == ([], [])
        assert runs.list_runs(env) == ([], [])

    monkeypatch.setattr(runs, "write_private", write)
    directory = record.create(flow, "{}\n", env)
    assert observed == [record.PLAN_NAME, record.FLOW_NAME]
    assert {item.name for item in directory.iterdir()} == {
        record.PLAN_NAME,
        record.FLOW_NAME,
        record.EVENTS_NAME,
    }
    assert record.list_flows(env) == ([flow], [])
    assert list(directory.parent.iterdir()) == [directory]


@pytest.mark.parametrize(
    "name", [record.PLAN_NAME, record.FLOW_NAME, record.EVENTS_NAME]
)
def test_creation_failure_removes_unpublished_files(
    flow: dict[str, Any],
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    original = paths.open_private_file

    def open_file(path: Path, flags: int = os.O_RDWR) -> int:
        if path.name == name or path.name.startswith(f".{name}."):
            raise OSError("write failed")
        return original(path, flags)

    monkeypatch.setattr(paths, "open_private_file", open_file)
    with pytest.raises(OSError, match="write failed"):
        record.create(flow, "{}", env)
    assert list(paths.runs_dir(env).iterdir()) == []


def test_save_atomic(flow: dict[str, Any], created: Path, env: dict[str, str]) -> None:
    path = created / record.FLOW_NAME
    with path.open() as previous:
        flow["status"] = "running"
        record.save(flow, env)
        assert json.load(previous)["status"] == "starting"
    current = record.load(flow["id"], env)
    assert current["status"] == "running" and current["updated_at"] > 0
    assert not list(created.glob(".flow.json.*"))


def test_missing(flow: dict[str, Any], env: dict[str, str]) -> None:
    with pytest.raises(record.FlowMissingError):
        record.load(flow["id"], env)
    runs.make_run_dir(flow["id"], env)
    with pytest.raises(record.FlowMissingError):
        record.load(flow["id"], env)


@pytest.mark.parametrize("status", record.FLOW_STATUSES)
def test_status_round_trip(
    flow: dict[str, Any], created: Path, env: dict[str, str], status: str
) -> None:
    flow["status"] = status
    record.save(flow, env)
    assert record.load(flow["id"], env)["status"] == status
    assert record.cancel_request(flow["id"], env) is None


@pytest.mark.parametrize("reason", [None, "the scope changed"])
def test_cancel_request_and_end(
    flow: dict[str, Any], created: Path, env: dict[str, str], reason: str | None
) -> None:
    request = {"at": 123.0, "reason": reason}
    runs.write_private(created / record.CANCEL_NAME, json.dumps(request))
    assert record.cancel_request(flow["id"], env) == request
    record.finish_cancel(flow, request, env)
    loaded = record.load(flow["id"], env)
    assert loaded["status"] == "cancelled"
    assert loaded["status"] in record.ENDED
    assert loaded["reason"] == reason and loaded["ended_at"] == 123.0
    event = record.read_events(flow["id"], env)[-1]
    assert (event["kind"], event["status"], event["reason"], event["exit"]) == (
        "end",
        "cancelled",
        reason,
        82,
    )


def test_load_without_plan_waves(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    """A record without plan waves loads with an empty list and can be saved."""
    del flow["plan_waves"]
    path = created / record.FLOW_NAME
    text = json.dumps(flow)
    path.write_text(text)
    loaded = record.load(flow["id"], env)
    assert loaded["plan_waves"] == []
    assert path.read_text() == text
    record.save(loaded, env)
    assert record.load(flow["id"], env)["plan_waves"] == []


def test_save_requires_plan_waves(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    """Saving requires the plan waves field."""
    del flow["plan_waves"]
    with pytest.raises(record.FlowError, match="has no plan_waves"):
        record.save(flow, env)


@pytest.mark.parametrize(
    ("plan_text", "source"),
    [
        ('{"rounds": 2}', "plan.rounds"),
        ('{"rounds": 1}', "unknown"),
        ('{"rounds": true}', "unknown"),
        ('{"rounds": "2"}', "unknown"),
        ("{}", "unknown"),
        ("[]", "unknown"),
        ("invalid", "unknown"),
        (None, "unknown"),
    ],
)
def test_load_without_review_budget(
    flow: dict[str, Any],
    created: Path,
    env: dict[str, str],
    plan_text: str | None,
    source: str,
) -> None:
    del flow["review_budget"]
    path = created / record.FLOW_NAME
    text = json.dumps(flow)
    path.write_text(text)
    saved_plan = created / record.PLAN_NAME
    if plan_text is None:
        saved_plan.unlink()
    else:
        saved_plan.write_text(plan_text)
    loaded = record.load(flow["id"], env)
    assert loaded["review_budget"] == {
        "value": 2,
        "unit": "fix_rounds",
        "source": source,
    }
    assert path.read_text() == text
    record.save(loaded, env)
    assert record.load(flow["id"], env)["review_budget"] == loaded["review_budget"]


@pytest.mark.parametrize(
    "budget",
    [
        None,
        {},
        {"value": True, "unit": "fix_rounds", "source": "default"},
        {"value": 1, "unit": "fix_rounds", "source": "plan.rounds"},
        {"value": 2, "unit": "reviews", "source": "default"},
        {"value": 3, "unit": "reviews", "source": "setting"},
    ],
)
def test_invalid_review_budget(
    flow: dict[str, Any], created: Path, env: dict[str, str], budget: object
) -> None:
    flow["review_budget"] = budget
    (created / record.FLOW_NAME).write_text(json.dumps(flow))
    with pytest.raises(record.FlowError, match="review_budget"):
        record.load(flow["id"], env)


def test_corrections_round_trip(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    """A task retains the private correction paths in answer order."""
    corrections = [str(created / "answers" / f"{n}.md") for n in (2, 5)]
    flow["tasks"][0]["corrections"] = corrections
    record.save(flow, env)
    loaded = record.load(flow["id"], env)
    assert loaded["tasks"][0]["corrections"] == corrections
    assert loaded["tasks"][1]["corrections"] == []


def test_load_without_corrections(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    """Older task records default to independent empty correction histories."""
    for task in flow["tasks"]:
        del task["corrections"]
    path = created / record.FLOW_NAME
    text = json.dumps(flow)
    path.write_text(text)
    loaded = record.load(flow["id"], env)
    assert all(task["corrections"] == [] for task in loaded["tasks"])
    assert path.read_text() == text
    loaded["tasks"][0]["corrections"].append("answers/1.md")
    assert loaded["tasks"][1]["corrections"] == []
    record.save(loaded, env)
    assert record.load(flow["id"], env)["tasks"][0]["corrections"] == ["answers/1.md"]


@pytest.mark.parametrize("bad", [b"invalid", b"\xff", b"[]", b"{}"])
def test_broken_json(
    flow: dict[str, Any], created: Path, env: dict[str, str], bad: bytes
) -> None:
    (created / record.FLOW_NAME).write_bytes(bad)
    with pytest.raises(record.FlowError):
        record.load(flow["id"], env)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status", "unknown"),
        ("common", 42),
        ("wave", True),
        ("plan_waves", None),
        ("plan_waves", ["a"]),
        ("plan_waves", [[1]]),
        ("rounds", -1),
        ("parallel", 0),
        ("check_timeout", 0),
        ("id", "another-flow"),
        ("tasks", [None]),
        ("writer", {}),
        ("checks", [{}]),
        ("runner", {}),
        ("started_at", True),
        ("stall_minutes", 0),
        ("read", [1]),
        ("ended_at", float("nan")),
    ],
)
def test_invalid_field(
    flow: dict[str, Any], created: Path, env: dict[str, str], key: str, value: object
) -> None:
    flow[key] = value
    (created / record.FLOW_NAME).write_text(json.dumps(flow))
    with pytest.raises(record.FlowError):
        record.load(created.name, env)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status", "unknown"),
        ("round", -1),
        ("entry", "merge"),
        ("writer_run", 1),
        ("corrections", None),
        ("corrections", "answers/1.md"),
        ("corrections", [1]),
        ("files", None),
    ],
)
def test_invalid_task(
    flow: dict[str, Any], created: Path, env: dict[str, str], key: str, value: object
) -> None:
    task = record.task_entry(flow, "a")
    if key == "files":
        del task["changed_files"]
    else:
        task[key] = value
    (created / record.FLOW_NAME).write_text(json.dumps(flow))
    with pytest.raises(record.FlowError):
        record.load(created.name, env)


def test_events(flow: dict[str, Any], created: Path, env: dict[str, str]) -> None:
    second = record.append_event(
        flow["id"], {"kind": "task", "task": "a", "seq": 99}, env
    )
    assert second["seq"] == 2
    assert record.read_events(flow["id"], env, after=1) == [second]
    assert record.read_events(flow["id"], env, after=2) == []


@pytest.mark.parametrize("name", [record.ANSWERS_NAME, record.EVENTS_NAME])
def test_incomplete_tail_preserved(
    flow: dict[str, Any], created: Path, env: dict[str, str], name: str
) -> None:
    path = created / name
    tail = b'{"interrupted": "\xff'
    with path.open("ab") as stream:
        stream.write(tail)
    if name == record.ANSWERS_NAME:
        assert record.read_answers(flow["id"], env) == []
        item = record.append_answer(flow["id"], {"kind": "drop", "task": "a"}, env)
        assert item["n"] == 1
        assert record.read_answers(flow["id"], env) == [item]
    else:
        assert len(record.read_events(flow["id"], env)) == 1
        item = record.append_event(flow["id"], {"kind": "end"}, env)
        assert item["seq"] == 2
        assert record.read_events(flow["id"], env, after=1) == [item]
    (backup,) = created.glob(name + ".incomplete-*")
    assert backup.read_bytes() == tail
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert path.read_bytes().endswith(b"\n")


def test_unreadable_answer_keeps_position(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    path = created / record.ANSWERS_NAME
    path.write_bytes(b"bad\n[]\n\xff\n")
    assert record.read_answers(flow["id"], env) == [None, None, None]
    item = record.append_answer(flow["id"], {"kind": "drop", "task": "a"}, env)
    assert item["n"] == 4
    assert record.read_answers(flow["id"], env) == [None, None, None, item]


def test_concurrent_answers_and_copies(
    flow: dict[str, Any], created: Path, env: dict[str, str], tmp_path: Path
) -> None:
    sources = [tmp_path / f"source-{i}.md" for i in range(16)]
    for index, source in enumerate(sources):
        source.write_text(f"correction {index}\n@intentional\n")

    def append(source: Path) -> dict[str, Any]:
        return record.append_answer(
            flow["id"], {"kind": "send", "task": "a", "file": str(source)}, env
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(append, sources))
    assert sorted(answer["n"] for answer in answers) == list(range(1, 17))
    for answer, source in zip(answers, sources, strict=True):
        path = Path(answer["file"])
        assert path == created / "answers" / f"{answer['n']}.md"
        assert path.read_text() == source.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((created / "answers").stat().st_mode) == 0o700
    assert [item["n"] for item in record.read_answers(flow["id"], env) if item] == list(
        range(1, 17)
    )


def test_failed_copy_does_not_append(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    with pytest.raises(OSError):
        record.append_answer(
            flow["id"],
            {"kind": "send", "task": "a", "file": str(created / "missing")},
            env,
        )
    assert record.read_answers(flow["id"], env) == []
    assert (
        record.append_answer(flow["id"], {"kind": "drop", "task": "a"}, env)["n"] == 1
    )


def test_cursor_never_regresses(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    assert record.read_cursor(flow["id"], env) == 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda seq: record.write_cursor(flow["id"], seq, env),
                [4, 1, 8, 3, 7, 2],
            )
        )
    assert record.read_cursor(flow["id"], env) == 8
    record.write_cursor(flow["id"], 0, env)
    assert record.read_cursor(flow["id"], env) == 8
    with pytest.raises(record.FlowError):
        record.write_cursor(flow["id"], -1, env)


def test_listing(flow: dict[str, Any], created: Path, env: dict[str, str]) -> None:
    broken = runs.make_run_dir(record.new_id(), env)
    (broken / record.FLOW_NAME).write_text("not json")
    runs.make_run_dir(runs.new_id("codex"), env)
    older = {**flow, "id": record.new_id(0), "started_at": -10}
    record.create(older, "{}", env)
    found, errors = record.list_flows(env)
    assert [item["id"] for item in found] == [older["id"], flow["id"]]
    assert len(errors) == 1 and errors[0].flow_id == broken.name


def test_no_flows(env: dict[str, str]) -> None:
    assert record.list_flows(env) == ([], [])


@pytest.mark.parametrize(
    ("status", "branch", "expected"),
    [
        ("stopped", "branch", record.ANSWER_KINDS),
        ("stopped", None, ("worker", "drop")),
        ("conflict", "branch", ("accept", "worker", "drop")),
        ("blocked", None, ("drop",)),
        ("pending", None, ("drop",)),
        *[
            (state, "branch", ())
            for state in record.TASK_STATUSES
            if state not in ("stopped", "conflict", "blocked", "pending")
        ],
    ],
)
def test_allowed_answers(
    env: dict[str, str], status: str, branch: str | None, expected: tuple[str, ...]
) -> None:
    assert record.allowed_answers({"status": status, "branch": branch}) == expected


def test_flow_id(env: dict[str, str]) -> None:
    assert record.is_flow_id(record.new_id(0))
    assert record.new_id(0).startswith("flow-19700101-000000-")
    assert record.FLOW_ID.pattern == runs._FLOW_ID.pattern
    for bad in (
        None,
        2,
        "../flow",
        runs.new_id("codex"),
        "flow-20260101-000000-ABCDEF",
        "flow-20260101-000000-abcdef\n",
    ):
        assert not record.is_flow_id(bad)
    with pytest.raises(record.FlowError):
        record.folder("../outside", env)


def test_interrupted_answer_keeps_correction(
    flow: dict[str, Any],
    created: Path,
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "correction.md"
    source.write_text("correction A")
    answer = {"kind": "send", "task": "a", "file": str(source)}
    write = record.os.write

    def interrupted_write(fd: int, data: bytes) -> int:
        return write(fd, data[:-1])

    with monkeypatch.context() as patch:
        patch.setattr(record.os, "write", interrupted_write)
        with pytest.raises(OSError, match="short write"):
            record.append_answer(flow["id"], answer, env)
    original = created / "answers" / "1.md"
    assert original.read_text() == "correction A"
    assert record.read_answers(flow["id"], env) == []

    source.write_text("correction B")
    appended = record.append_answer(flow["id"], answer, env)
    assert appended["n"] == 2
    assert Path(appended["file"]) == created / "answers" / "2.md"
    assert Path(appended["file"]).read_text() == "correction B"
    assert original.read_text() == "correction A"
    (backup,) = created.glob(record.ANSWERS_NAME + ".incomplete-*")
    assert Path(json.loads(backup.read_text())["file"]) == original
    assert record.read_answers(flow["id"], env) == [appended]


@pytest.mark.parametrize(
    ("line_count", "stored_number", "file_number", "expected"),
    [(5, 2, 3, 6), (2, 8, 3, 9), (2, 3, 11, 12)],
)
def test_answer_number_exceeds_logs_and_files(
    flow: dict[str, Any],
    created: Path,
    env: dict[str, str],
    line_count: int,
    stored_number: int,
    file_number: int,
    expected: int,
) -> None:
    answers = created / "answers"
    answers.mkdir()
    original = answers / f"{file_number}.md"
    original.write_text("retained correction")
    (created / record.ANSWERS_NAME).write_text(
        "unreadable\n" * (line_count - 1) + json.dumps({"n": stored_number}) + "\n"
    )
    appended = record.append_answer(flow["id"], {"kind": "drop", "task": "a"}, env)
    assert appended["n"] == expected
    assert original.read_text() == "retained correction"


def test_correction_creation_refuses_existing_file(
    flow: dict[str, Any],
    created: Path,
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "correction.md"
    source.write_text("new correction")
    target = created / "answers" / "1.md"
    open_private_file = paths.open_private_file

    def collide(path: Path, flags: int = record.os.O_RDWR) -> int:
        if path == target:
            path.write_text("existing correction")
        return open_private_file(path, flags)

    monkeypatch.setattr(paths, "open_private_file", collide)
    with pytest.raises(FileExistsError):
        record.append_answer(
            flow["id"], {"kind": "send", "task": "a", "file": str(source)}, env
        )
    assert target.read_text() == "existing correction"
    assert record.read_answers(flow["id"], env) == []


def test_record_without_base_branch_loads(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    del flow["base_branch"]
    (created / record.FLOW_NAME).write_text(json.dumps(flow))
    assert record.load(flow["id"], env)["base_branch"] is None


def test_record_without_common_loads(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    del flow["common"]
    (created / record.FLOW_NAME).write_text(json.dumps(flow))
    loaded = record.load(flow["id"], env)
    assert loaded["common"] is None
    record.save(loaded, env)
    assert record.load(flow["id"], env)["common"] is None


@pytest.mark.parametrize(
    "model", ["anthropic/claude-x", "openrouter/anthropic/claude-x"]
)
@pytest.mark.parametrize("marked", [False, True])
def test_opencode_workers_migrate_at_load(
    flow: dict[str, Any], created: Path, env: dict[str, str], model: str, marked: bool
) -> None:
    worker = {
        "engine": "opencode",
        "model": model,
        "effort": "high",
        "source": "setting",
    }
    flow["writer"] = dict(worker)
    flow["reviewer"] = dict(worker)
    for task in flow["tasks"]:
        task["writer"] = dict(worker)
        task["reviewer"] = dict(worker)
    if not marked:
        flow.pop("opencode_id_format")
    path = created / record.FLOW_NAME
    path.write_text(json.dumps(flow))
    before = path.read_bytes()
    loaded = record.load(flow["id"], env)
    expected = (
        model if marked or model.startswith("openrouter/") else "openrouter/" + model
    )
    for row in (loaded, *loaded["tasks"]):
        for name in ("writer", "reviewer"):
            assert row[name] == {**worker, "model": expected}
    assert path.read_bytes() == before
    record.save(loaded, env)
    assert record.load(flow["id"], env) == loaded
    assert json.loads(path.read_text())["opencode_id_format"] == 1


def test_pending_worker_answers_migrate_without_rewriting_log(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    path = created / record.ANSWERS_NAME
    path.write_text(
        json.dumps(
            {
                "kind": "worker",
                "task": "a",
                "worker": "opencode:anthropic/claude-x@high",
                "n": 1,
            }
        )
        + "\n"
    )
    before = path.read_bytes()
    old = record.read_answers(flow["id"], env)[0]
    assert old is not None
    assert old["worker"] == "opencode:openrouter/anthropic/claude-x@high"
    assert path.read_bytes() == before
    current = record.append_answer(
        flow["id"],
        {"kind": "worker", "task": "a", "worker": "opencode:anthropic/claude-x@high"},
        env,
    )
    assert current["opencode_id_format"] == 1
    assert record.read_answers(flow["id"], env)[1] == current


def test_record_without_check_base(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    path = created / record.FLOW_NAME
    old = json.loads(path.read_text())
    del old["check_base"]
    path.write_text(json.dumps(old))
    loaded = record.load(flow["id"], env)
    assert loaded["check_base"] is False
    assert loaded["check_pending"] is False
    record.save(loaded, env)
    assert record.load(flow["id"], env) == loaded
