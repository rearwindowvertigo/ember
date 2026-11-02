import json
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ember.panels import record
from ember.runs import record as runs
from runs.stub_engine import make_env  # pyrefly: ignore[missing-import]


def member(n: int = 1, engine: str = "codex", **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "n": n,
        "engine": engine,
        "model": "model",
        "effort": "low",
        "source": "option",
        "status": "pending",
        "run": None,
        "exit": None,
        "reason": None,
        "reply": None,
        "bytes": 0,
        "changed": [],
        "seconds": None,
        "closed": False,
        "close_error": None,
        "attempts": [],
        **changes,
    }


def panel(panel_id: str | None = None, **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return record.new_panel(
        panel_id or record.new_id(),
        dir="/checkout",
        question_file="/question.md",
        read=[],
        claim=None,
        stall_minutes=10,
        timeout_minutes=30,
        members=[member()],
        left_out=[],
        **changes,
    )


def test_id_and_private_artifacts(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    ident = record.new_id(0)
    assert ident.startswith("panel-19700101-000000-")
    assert record.is_panel_id(ident)
    assert runs.check_id(ident) == ident
    assert not record.is_panel_id(ident + "\n")
    assert not record.is_panel_id(None)
    data = panel(ident)
    with record.hold(ident, env):
        folder = record.create(data, "question\n", "brief\n", env)
        record.append_log(ident, 1, "first\n", env)
        record.append_log(ident, 1, "second\n", env)
        with pytest.raises(record.PanelError, match="already exists"):
            record.create(data, "replacement", "replacement", env)
    for path in [folder, folder / "members"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in [
        folder / name
        for name in (
            "panel.json",
            "question.md",
            "brief.md",
            "summary.md",
            "members/1.log",
        )
    ]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (folder / "question.md").read_text() == "question\n"
    assert (folder / "brief.md").read_text() == "brief\n"
    assert (folder / "members/1.log").read_text() == "first\nsecond\n"
    assert record.load(ident, env) == data


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"read": [1]},
        {"started_at": True},
        {"updated_at": float("nan")},
        {"members": [member(status="unknown")]},
        {"members": [member(extra=1)]},
        {"members": [member(attempts=[{"kind": "send", "exit": None, "at": 1}])]},
        {"left_out": [{"value": "a", "reason": 1}]},
        {"schema_version": 2},
    ],
)
def test_invalid_records(tmp_path: Path, change: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    data = panel()
    with record.hold(data["id"], env):
        folder = record.create(data, "q", "b", env)
        (folder / "panel.json").write_text(json.dumps({**data, **change}))
        with pytest.raises(record.PanelError):
            record.load(data["id"], env)


def test_missing_and_listing(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert record.list_panels(env) == ([], [])
    earlier, later = panel(now=1), panel(now=2)
    for data in (later, earlier):
        with record.hold(data["id"], env):
            record.create(data, "q", "b", env)
    broken = record.new_id()
    with pytest.raises(record.PanelMissingError):
        record.load(broken, env)
    runs.make_run_dir(broken, env)
    runs.make_run_dir("flow-20260101-120000-abcdef", env)
    runs.make_run_dir("codex-20260101-120000-abcdef", env)
    panels, errors = record.list_panels(env)
    assert panels == [earlier, later]
    assert len(errors) == 1 and errors[0].panel_id == broken


def test_save_replaces_and_lock_lifetime(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    data = panel(now=1)
    ident = data["id"]
    assert not record.live(ident, env)
    with record.hold(ident, env):
        assert record.live(ident, env)
        with pytest.raises(record.Busy), record.hold(ident, env):
            pass
        folder = record.create(data, "q", "b", env)
        with (folder / "panel.json").open() as original:
            data["status"] = "done"
            record.save(data, env)
            assert json.load(original)["status"] == "running"
        assert record.load(ident, env) == data
        assert data["updated_at"] > 1
    assert not record.live(ident, env)


@pytest.mark.parametrize("left_out", [[], [{"value": "claude:model", "reason": "off"}]])
def test_summary_golden(left_out: list[dict[str, str]]) -> None:
    data = panel("panel-20260101-120000-abcdef", now=0)
    data.update(
        status="done",
        left_out=left_out,
        members=[
            member(
                1,
                status="answered",
                exit=0,
                seconds=2.4,
                bytes=3,
                run="codex-000001",
                effort=None,
            ),
            member(2, status="failed", exit=71, reason="failed turn"),
            member(
                3, status="answered", exit=0, seconds=0, bytes=5, changed=["a", "b"]
            ),
        ],
    )
    stamp = datetime.fromtimestamp(0).astimezone().isoformat(timespec="seconds")
    expected = (
        "# Panel panel-20260101-120000-abcdef\n\nCheckout: /checkout\n"
        f"Started: {stamp}\nMembers: 3; answered: 2; status: done\n\n"
        "## Question\n\nWhat?\n\n"
        "## Member 1: codex:model · default\n\n"
        "Status: answered · exit 0 · 2s · run codex-000001 · 3 bytes\n\nOne\n\n"
        "## Member 2: codex:model · low\n\n"
        "Status: failed · exit 71 · - · run - · 0 bytes\nReason: failed turn\n\n"
        "(no answer)\n\n## Member 3: codex:model · low\n\n"
        "Status: answered · exit 0 · 0s · run - · 5 bytes\n"
        "Changed paths: a, b\n\nThree\n"
    )
    if left_out:
        expected += "\n## Left out\n\n- claude:model: off\n"
    assert record.summary_text(data, "What?\n", {1: "One", 3: "Three"}) == expected


def test_summary_keeps_failed_reply_and_unreadable_file(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    data = panel()
    reply = tmp_path / "reply"
    reply.write_text("partial reply")
    data["members"] = [member(status="failed", reply=str(reply))]
    with record.hold(data["id"], env):
        folder = record.create(data, "q", "b", env)
        assert "partial reply" in (folder / "summary.md").read_text()
        reply.unlink()
        record.write_summary(data, env)
    assert "the reply file cannot be read:" in (folder / "summary.md").read_text()


@pytest.mark.parametrize("retained", [{}, {"run": None}, {"run": "codex-old"}])
def test_retry_attempt_compatible_with_existing_attempts(
    tmp_path: Path, retained: dict[str, Any]
) -> None:
    env = make_env(tmp_path)
    data = panel()
    attempts = [
        {"kind": "start", "at": 1, "exit": 71},
        {"kind": "retry", "at": 2, "exit": 71, **retained},
        {"kind": "start", "at": 3, "exit": 0},
    ]
    data["members"][0]["attempts"] = attempts
    with record.hold(data["id"], env):
        record.create(data, "q", "b", env)
    assert record.load(data["id"], env)["members"][0]["attempts"] == attempts


@pytest.mark.parametrize("retained", [{"run": 42}, {"run": "old", "unknown": 0}])
def test_retry_attempt_rejects_invalid_fields(
    tmp_path: Path, retained: dict[str, Any]
) -> None:
    env = make_env(tmp_path)
    data = panel()
    data["members"][0]["attempts"] = [
        {"kind": "retry", "at": 1, "exit": None, **retained}
    ]
    with record.hold(data["id"], env), pytest.raises(record.PanelError):
        record.create(data, "q", "b", env)
