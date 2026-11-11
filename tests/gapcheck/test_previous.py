import fcntl
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember.gapcheck import previous


def gap(name: str, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "id": "g1",
        "gap": name,
        "harm": "high",
        "why": "Data can be lost",
        "default": "Keep it",
        "answer": None,
        "ask_owner": True,
        "earlier": None,
        **extra,
    }


def result(*steps: dict[str, Any], **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    active = [{**gap, "step": step["step"]} for step in steps for gap in step["gaps"]]
    return {
        "plan": "/plan.md",
        "plan_steps": "numbered",
        "failure_model": True,
        "status": "run",
        "ask": "all",
        "reviewer": None,
        "run": None,
        "steps": list(steps),
        "previous": None,
        "previous_plan": None,
        "settled": [],
        "closed": [],
        "next_id": 20,
        "dispositions": [],
        "ranked": [{key: gap[key] for key in ("id", "step", "harm")} for gap in active],
        "counts": {
            harm: sum(gap["harm"] == harm for gap in active)
            for harm in ("high", "medium", "low")
        },
        "parse_error": None,
        "uncovered_steps": [],
        "unknown_steps": [],
        **extra,
    }


def write(tmp_path: Path, document: object) -> str:
    path = tmp_path / "earlier.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def history_gap(name: str, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    made = gap(name, **extra)
    del made["ask_owner"], made["earlier"]
    return {"step": 3, "title": "Old", **made}


def test_result_gaps_with_ids_and_answers(tmp_path: Path) -> None:
    document = result(
        {"step": 1, "title": "Read", "gaps": [gap("a", answer="Keep")]},
        {"step": 2, "title": "Build", "gaps": [gap("b", id="g2")]},
        settled=[history_gap("c", id="g7", answer="Drop")],
        closed=[{**history_gap("d", id="g9"), "reason": "Removed"}],
    )
    loaded = previous.load(write(tmp_path, document))
    assert loaded.path == tmp_path / "earlier.json"
    assert [(item.id, item.step, item.answer) for item in loaded.gaps] == [
        ("g1", 1, "Keep"),
        ("g2", 2, None),
        ("g7", 3, "Drop"),
    ]
    assert loaded.ids() == {"g1", "g2", "g7"}
    assert loaded.next_id == 20
    settled, closed = previous.history(
        loaded,
        [
            {"id": "g1", "status": "reopened", "reason": "Changed"},
            {"id": "g2", "status": "closed", "reason": "Removed too"},
            {"id": "g7", "status": "settled", "reason": "Unchanged"},
        ],
    )
    assert settled == document["settled"]
    assert [entry["id"] for entry in closed] == ["g9", "g2"]
    assert closed[-1]["reason"] == "Removed too"


def test_result_without_ids_or_answers_counts_as_unanswered(tmp_path: Path) -> None:
    document = result(
        {"step": 1, "title": "Read", "gaps": [gap("a"), gap("b", id="g2")]}
    )
    for entry in document["steps"][0]["gaps"]:
        del entry["id"], entry["answer"], entry["earlier"]
    document["ranked"] = [
        {"step": 1, **entry} for entry in document["steps"][0]["gaps"]
    ]
    for key in (
        "next_id",
        "previous",
        "previous_plan",
        "settled",
        "closed",
        "dispositions",
    ):
        del document[key]
    loaded = previous.load(write(tmp_path, document))
    assert [item.id for item in loaded.gaps] == ["g1", "g2"]
    assert all(item.answer is None for item in loaded.gaps)
    assert loaded.next_id == 3


def test_missing_answer_is_accepted(tmp_path: Path) -> None:
    item = gap("a")
    del item["answer"]
    loaded = previous.load(
        write(tmp_path, result({"step": 1, "title": "Read", "gaps": [item]}))
    )
    assert loaded.gaps[0].answer is None


def test_empty_result_keeps_next_id(tmp_path: Path) -> None:
    loaded = previous.load(write(tmp_path, result(next_id=41)))
    assert loaded.gaps == ()
    assert loaded.next_id == 41
    assert previous.history(loaded, ()) == ([], [])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "x", "expected a gap id"),
        ("id", "", "expected a non-empty string"),
        ("harm", "x", "expected one of high, medium, low"),
        ("answer", "", "expected a non-empty string"),
        ("answer", 3, "expected string or null"),
        ("earlier", 3, "expected string or null"),
        ("ask_owner", "yes", "expected boolean"),
    ],
)
def test_invalid_gap_fields(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    item = gap("a")
    item[field] = value
    document = result({"step": 1, "title": "Read", "gaps": [item]})
    with pytest.raises(previous.PreviousError, match=message):
        previous.load(write(tmp_path, document))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"next_id": 1}, "must exceed every gap id"),
        ({"next_id": True}, "expected integer"),
        ({"next_id": 0}, "expected an integer at least 1"),
        ({"ranked": []}, "must refer to each active gap"),
        ({"ranked": [{"id": "g1", "step": 2, "harm": "high"}]}, "must refer"),
        ({"counts": {"high": 0, "medium": 0, "low": 0}}, "must count active gaps"),
        ({"settled": [history_gap("dup", answer="a")]}, "gap ids must be unique"),
        ({"settled": [history_gap("a", id="g3")]}, "expected string"),
        ({"closed": [history_gap("a", id="g3")]}, "missing required key 'reason'"),
        ({"plan": None}, "expected string"),
        ({"failure_model": "yes"}, "expected boolean"),
        ({"extra": 1}, "unexpected key 'extra'"),
        (
            {"ranked": [{"id": "g1", "step": 1, "harm": "high", "answer": "x"}]},
            "unexpected key 'answer'",
        ),
    ],
)
def test_inconsistent_results_are_refused(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    document = result({"step": 1, "title": "Read", "gaps": [gap("a")]}, **change)
    with pytest.raises(previous.PreviousError, match=message):
        previous.load(write(tmp_path, document))


@pytest.mark.parametrize(
    "document", [[], {}, {"steps": []}, {"steps": [{"step": 1, "gaps": []}]}]
)
def test_replies_and_partial_objects_are_not_results(
    tmp_path: Path, document: object
) -> None:
    with pytest.raises(previous.PreviousError):
        previous.load(write(tmp_path, document))


def test_required_result_fields_are_checked() -> None:
    for field in json.loads(previous.schema_text())["required"]:
        document = result({"step": 1, "title": "Read", "gaps": [gap("a")]})
        del document[field]
        with pytest.raises(previous.PreviousError):
            previous.validate(document)


def test_unreadable_and_non_json_files(tmp_path: Path) -> None:
    with pytest.raises(previous.PreviousError) as raised:
        previous.load(str(tmp_path / "missing.json"))
    assert raised.value.unreadable
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(previous.PreviousError) as raised:
        previous.load(str(path))
    assert not raised.value.unreadable
    assert str(raised.value).startswith(f"the previous result {path} is not JSON")


def test_canned_eval_results_match_the_result_schema(
    generated_eval_fixtures: Path,
) -> None:
    generated = sorted(generated_eval_fixtures.rglob("gapcheck*.json"))
    assert generated, "the fixture generator produced no gap check results"
    evals = Path(__file__).resolve().parents[2] / "skill/evals"
    paths = [*generated, *sorted(evals.rglob("gapcheck*.json"))]
    for path in paths:
        previous.validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "fields",
    [
        {"parse_error": "the reply misses earlier gap g1"},
        {"uncovered_steps": [1]},
        {"unknown_steps": [3]},
    ],
)
def test_incomplete_checks_cannot_discard_earlier_context(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    document = result(**fields)
    previous.validate(document)
    with pytest.raises(previous.PreviousError, match="check is incomplete"):
        previous.load(write(tmp_path, document))


def test_legacy_ranked_copies_are_checked_before_normalizing(tmp_path: Path) -> None:
    document = result({"step": 1, "title": "Read", "gaps": [gap("a")]})
    item = document["steps"][0]["gaps"][0]
    del item["id"], item["answer"], item["earlier"]
    document["ranked"] = [{**item, "step": 1, "gap": "different"}]
    for key in ("next_id", "previous_plan", "settled", "closed", "dispositions"):
        del document[key]
    with pytest.raises(previous.PreviousError, match="must match the gaps in steps"):
        previous.load(write(tmp_path, document))


@pytest.mark.parametrize("defaults", [False, True])
def test_overlapping_answers_reload_under_a_lock_that_survives_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defaults: bool
) -> None:
    document = result(
        {"step": 1, "title": "Read", "gaps": [gap("a"), gap("b", id="g2")]}
    )
    saved = write(tmp_path, document)
    first_snapshot = previous.load(saved)
    second_snapshot = previous.load(saved)
    lock_path = Path(saved + ".lock")
    first_replaced = threading.Event()
    release_first = threading.Event()
    second_waiting = threading.Event()
    real_replace = os.replace
    real_flock = fcntl.flock

    def replace(source: Path, target: Path) -> None:
        real_replace(source, target)
        if not first_replaced.is_set():
            first_replaced.set()
            assert release_first.wait(timeout=10)

    def flock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX and first_replaced.is_set():
            second_waiting.set()
        real_flock(fd, operation)

    monkeypatch.setattr(previous.os, "replace", replace)
    monkeypatch.setattr(previous.fcntl, "flock", flock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(previous.answer, first_snapshot, "g1", "Keep")
        try:
            assert first_replaced.wait(timeout=10)
            # Replacement has happened, but the original lock still excludes writers.
            fd = os.open(lock_path, os.O_RDWR)
            try:
                inode = os.fstat(fd).st_ino
                with pytest.raises(BlockingIOError):
                    real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            second = (
                pool.submit(previous.take_defaults, second_snapshot, ["g2"])
                if defaults
                else pool.submit(previous.answer, second_snapshot, "g2", "Stop")
            )
            assert second_waiting.wait(timeout=10)
        finally:
            release_first.set()
        first.result(timeout=10)
        second.result(timeout=10)
    assert {gap.id: gap.answer for gap in previous.load(saved).gaps} == {
        "g1": "Keep",
        "g2": "Default taken: Keep it" if defaults else "Stop",
    }
    assert lock_path.stat().st_ino == inode


@pytest.mark.parametrize("defaults", [False, True])
def test_answer_revalidates_the_current_document(
    tmp_path: Path, defaults: bool
) -> None:
    saved = write(tmp_path, result({"step": 1, "title": "Read", "gaps": [gap("a")]}))
    snapshot = previous.load(saved)
    Path(saved).write_text("{}", encoding="utf-8")
    with pytest.raises(previous.PreviousError):
        if defaults:
            previous.take_defaults(snapshot, ["g1"])
        else:
            previous.answer(snapshot, "g1", "Keep")
    assert Path(saved).read_text(encoding="utf-8") == "{}"


def test_defaults_use_one_replacement_under_the_result_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = result(
        {"step": 1, "title": "Read", "gaps": [gap("a", default="Keep é\nThen delete")]},
        {"step": 2, "title": "Build", "gaps": [gap("b", id="g2", default="Stop")]},
    )
    saved = write(tmp_path, document)
    real_replace = os.replace
    replacements = []

    def replace(source: Path, target: Path) -> None:
        fd = os.open(saved + ".lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        replacements.append(json.loads(source.read_text()))
        real_replace(source, target)

    monkeypatch.setattr(previous.os, "replace", replace)
    made = previous.take_defaults(previous.load(saved), ["g2", "g1"])
    expected = json.loads(json.dumps(document))
    expected["steps"][0]["gaps"][0]["answer"] = "Default taken: Keep é\nThen delete"
    expected["steps"][1]["gaps"][0]["answer"] = "Default taken: Stop"
    assert made == expected == json.loads(Path(saved).read_text())
    assert replacements == [expected]
    assert not list(tmp_path.glob(".earlier.json.*"))
    previous.load(saved)


@pytest.mark.parametrize(
    "identities",
    [
        [],
        ["g1", ""],
        ["g1", "g1"],
        ["g1", "g9"],
        ["g1", "g2"],
        ["g1", "g3"],
        ["g1", "g4"],
    ],
)
def test_invalid_defaults_leave_the_result_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identities: list[str]
) -> None:
    document = result(
        {"step": 1, "title": "Read", "gaps": [gap("a"), gap("b", id="g2", answer="X")]},
        settled=[history_gap("c", id="g3", answer="Y")],
        closed=[{**history_gap("d", id="g4"), "reason": "Removed"}],
    )
    saved = write(tmp_path, document)
    snapshot = previous.load(saved)
    before = Path(saved).read_bytes()

    def refuse(*args: object) -> None:
        raise AssertionError("invalid batches must not create a temporary result")

    monkeypatch.setattr(previous.tempfile, "NamedTemporaryFile", refuse)
    with pytest.raises(ValueError):
        previous.take_defaults(snapshot, identities)
    assert Path(saved).read_bytes() == before
    assert snapshot.document == document


def test_defaults_revalidate_answers_after_acquiring_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = write(
        tmp_path,
        result({"step": 1, "title": "Read", "gaps": [gap("a"), gap("b", id="g2")]}),
    )
    snapshot = previous.load(saved)
    real_flock = fcntl.flock
    current = json.loads(Path(saved).read_text())
    current["steps"][0]["gaps"][1]["answer"] = "Owner's answer"
    updated = json.dumps(current).encode()

    def flock(fd: int, operation: int) -> None:
        Path(saved).write_bytes(updated)
        real_flock(fd, operation)

    monkeypatch.setattr(previous.fcntl, "flock", flock)
    with pytest.raises(ValueError, match="g2.*already answered"):
        previous.take_defaults(snapshot, ["g1", "g2"])
    assert Path(saved).read_bytes() == updated
    assert all(gap.answer is None for gap in snapshot.gaps)


def test_defaults_use_the_current_default_and_accept_a_missing_answer(
    tmp_path: Path,
) -> None:
    item = gap("a")
    del item["answer"]
    document = result({"step": 1, "title": "Read", "gaps": [item]})
    saved = write(tmp_path, document)
    snapshot = previous.load(saved)
    item["default"] = "Stop"
    write(tmp_path, document)
    previous.take_defaults(snapshot, ["g1"])
    assert previous.load(saved).gaps[0].answer == "Default taken: Stop"


@pytest.mark.parametrize("identity", ["g1", "g2"])
def test_single_answer_can_overwrite_active_and_settled_answers(
    tmp_path: Path, identity: str
) -> None:
    saved = write(
        tmp_path,
        result(
            {"step": 1, "title": "Read", "gaps": [gap("a", answer="Old")]},
            settled=[history_gap("b", id="g2", answer="Old")],
        ),
    )
    previous.answer(previous.load(saved), identity, "New")
    assert {gap.id: gap.answer for gap in previous.load(saved).gaps}[identity] == "New"


def checked_flow(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "plan.json"
    document: dict[str, Any] = {
        "dir": str(tmp_path),
        "context": "Context\n",
        "failure_model": "Keep data\n",
        "tasks": [
            {
                "id": ident,
                "subject": f"feat: {ident}",
                "task": f"Build {ident}\n",
                "review": "Check it",
                "files": [f"{ident}.py", f"tests/test_{ident}.py"],
                "after": [],
            }
            for ident in ("a", "b", "c")
        ],
    }
    path.write_text(json.dumps(document))
    return path, document


@pytest.mark.parametrize(
    ("change", "reasons", "judge"),
    [
        ("add", ["task added: d", "changed waves"], []),
        ("remove", ["task removed: c", "changed waves"], []),
        ("files", ["a: changed files", "changed waves"], []),
        ("after", ["a: changed after", "changed waves"], []),
        ("task", [], ["a: changed task text"]),
        ("subject", [], ["a: changed task text"]),
        ("review", [], ["a: changed task text"]),
        ("context", [], ["changed context for tasks: a, b, c"]),
        ("failure_model", [], ["changed failure_model for tasks: a, b, c"]),
        ("file order", [], []),
        ("after order", [], []),
        ("unchanged", [], []),
    ],
)
def test_compare_saved_plan_after_editing_in_place(
    tmp_path: Path, change: str, reasons: list[str], judge: list[str]
) -> None:
    from ember.flows import plan as flow_plan
    from ember.gapcheck import plan

    path, document = checked_flow(tmp_path)
    if change == "after order":
        document["tasks"][0]["after"] = ["b", "c"]
        path.write_text(json.dumps(document))
    saved = previous.snapshot(plan.load(path))
    earlier = previous.load(
        write(tmp_path, result(schema_version=3, checked_plan=saved))
    )
    tasks = document["tasks"]
    if change == "add":
        tasks.append({**tasks[-1], "id": "d", "files": ["d.py"]})
    elif change == "remove":
        tasks.pop()
    elif change == "files":
        tasks[0]["files"].append("b.py")
    elif change == "after":
        tasks[0]["after"] = ["b"]
    elif change in ("task", "subject", "review"):
        tasks[0][change] += " "
    elif change in ("context", "failure_model"):
        document[change] += " "
    elif change == "file order":
        tasks[0]["files"].reverse()
    elif change == "after order":
        tasks[0]["after"].reverse()
    path.write_text(json.dumps(document))
    report = previous.comparison(flow_plan.load(path), earlier)
    assert report == {
        "previous": str(earlier.path),
        "repeat_needed": True if reasons else None,
        "reasons": reasons,
        "judge": judge,
        "recovery": None,
    }
    assert earlier.document["checked_plan"] == saved


def test_comparison_keeps_task_order_when_recomputing_overlap_waves(
    tmp_path: Path,
) -> None:
    from ember.flows import plan as flow_plan
    from ember.gapcheck import plan

    path, document = checked_flow(tmp_path)
    document["tasks"][0]["files"] = ["src"]
    document["tasks"][1]["files"] = ["src/module.py"]
    path.write_text(json.dumps(document))
    earlier = previous.load(
        write(
            tmp_path,
            result(schema_version=3, checked_plan=previous.snapshot(plan.load(path))),
        )
    )
    document["tasks"].reverse()
    path.write_text(json.dumps(document))
    report = previous.comparison(flow_plan.load(path), earlier)
    assert report["reasons"] == ["changed waves"]
    assert report["repeat_needed"]


@pytest.mark.parametrize("field", ["parse_error", "uncovered_steps", "unknown_steps"])
def test_incomplete_results_are_comparison_only(tmp_path: Path, field: str) -> None:
    from ember.flows import plan as flow_plan
    from ember.gapcheck import plan

    path, _ = checked_flow(tmp_path)
    document = result(schema_version=3, checked_plan=previous.snapshot(plan.load(path)))
    document[field] = "bad reply" if field == "parse_error" else [1]
    saved = write(tmp_path, document)
    with pytest.raises(previous.PreviousError, match="incomplete"):
        previous.load(saved)
    earlier = previous.load(saved, allow_incomplete=True)
    report = previous.comparison(flow_plan.load(path), earlier)
    assert report["repeat_needed"]
    assert report["reasons"] == ["the earlier check is incomplete"]
    assert "without passing the incomplete result as --previous" in report["recovery"]
    # Comparison mode still applies identity validation.
    document["ranked"] = [{"id": "g1", "step": 1, "harm": "high"}]
    with pytest.raises(previous.PreviousError, match="ranked"):
        previous.load(write(tmp_path, document), allow_incomplete=True)


@pytest.mark.parametrize("kind", ["old", "document"])
def test_older_and_nonflow_results_are_not_structural_baselines(
    tmp_path: Path, kind: str
) -> None:
    from ember.flows import plan as flow_plan

    path, _ = checked_flow(tmp_path)
    document = result()
    if kind == "document":
        document.update(
            schema_version=3,
            checked_plan={"version": 1, "kind": "document", "text": "Workflow\n"},
        )
    earlier = previous.load(write(tmp_path, document))
    report = previous.comparison(flow_plan.load(path), earlier)
    assert report["repeat_needed"] and len(report["reasons"]) == 1
    assert report["recovery"] is None


@pytest.mark.parametrize("change", ["version", "kind", "missing", "cycle", "duplicate"])
def test_snapshot_schema_and_graph_are_validated(tmp_path: Path, change: str) -> None:
    from ember.gapcheck import plan

    path, _ = checked_flow(tmp_path)
    saved = previous.snapshot(plan.load(path))
    if change == "version":
        saved["version"] = 2
    elif change == "kind":
        saved["kind"] = "unknown"
    elif change == "missing":
        del saved["context"]
    elif change == "cycle":
        saved["tasks"][0]["after"] = ["a"]
    else:
        saved["tasks"][1]["id"] = "a"
    with pytest.raises(previous.PreviousError):
        previous.load(write(tmp_path, result(schema_version=3, checked_plan=saved)))
