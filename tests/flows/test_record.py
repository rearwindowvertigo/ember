import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember import paths
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
                {"id": "a", "task": "Implement a", "files": ["a"]},
                {"id": "b", "task": "Implement b", "files": ["b"]},
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
    assert flow["status"] == "starting" and flow["tip"] == flow["base"]
    assert record.task_entry(flow, "a")["status"] == "pending"
    record.task_entry(flow, "a")["findings"].append({"problem": "a"})
    assert record.task_entry(flow, "b")["findings"] == []
    with pytest.raises(KeyError):
        record.task_entry(flow, "unknown")


def test_create_does_not_overwrite(
    flow: dict[str, Any], created: Path, env: dict[str, str]
) -> None:
    with pytest.raises(record.FlowError, match="already exists"):
        record.create(flow, "changed", env)
    assert "example" in (created / record.PLAN_NAME).read_text()


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
        ("wave", True),
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
        ("conflict", "branch", ("worker", "drop")),
        ("blocked", None, ("drop",)),
        *[
            (state, "branch", ())
            for state in record.TASK_STATUSES
            if state not in ("stopped", "conflict", "blocked")
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
