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


def test_overlapping_answers_reload_under_a_lock_that_survives_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            second = pool.submit(previous.answer, second_snapshot, "g2", "Stop")
            assert second_waiting.wait(timeout=10)
        finally:
            release_first.set()
        first.result(timeout=10)
        second.result(timeout=10)
    assert {gap.id: gap.answer for gap in previous.load(saved).gaps} == {
        "g1": "Keep",
        "g2": "Stop",
    }
    assert lock_path.stat().st_ino == inode


def test_answer_revalidates_the_current_document(tmp_path: Path) -> None:
    saved = write(tmp_path, result({"step": 1, "title": "Read", "gaps": [gap("a")]}))
    snapshot = previous.load(saved)
    Path(saved).write_text("{}", encoding="utf-8")
    with pytest.raises(previous.PreviousError):
        previous.answer(snapshot, "g1", "Keep")
    assert Path(saved).read_text(encoding="utf-8") == "{}"
