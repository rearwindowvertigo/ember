import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ember.benchmark_sources import ARTIFICIAL_ANALYSIS
from ember.runs import announcements
from ember.runs.core import WorkerSelection

CLAIM = "announcement-123"
WORKER = WorkerSelection(
    engine="codex",
    model="gpt-6.1-sol",
    effort="low",
    speed="standard",
    row="Single tasks Writer",
    model_source="--model",
    effort_source="--effort",
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_RUNTIME_DIR": str(tmp_path),
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_SESSION_ID": "lead-session",
    }


def test_claude_requires_notice_in_the_transcript(
    env: dict[str, str], tmp_path: Path
) -> None:
    transcript = tmp_path / "conversation.jsonl"
    transcript.touch()
    marker = announcements.prepare(
        CLAIM, "job", WORKER, "Boundary tests", None, {}, env
    )
    with pytest.raises(announcements.AnnouncementError, match="no published"):
        announcements.require_posted(CLAIM, "job", env)
    hook = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "transcript_path": str(transcript),
        "session_id": env["CLAUDE_CODE_SESSION_ID"],
        "tool_use_id": "preview-tool",
        "tool_response": {"stdout": json.dumps({"announcement": marker})},
    }
    notice = announcements.claude_notice(hook, env)
    assert notice == (
        "Ember will use codex gpt-6.1-sol at low effort (standard): Boundary tests."
    )
    event: dict[str, Any] = {
        "type": "attachment",
        "sessionId": hook["session_id"],
        "attachment": {
            "hookEvent": "PostToolUse",
            "type": "hook_success",
            "toolUseID": hook["tool_use_id"],
            "content": notice,
        },
    }
    transcript.write_text(json.dumps(event) + "\n")
    with pytest.raises(announcements.AnnouncementError, match="no published"):
        announcements.require_posted(CLAIM, "job", env)
    event["attachment"]["type"] = "hook_system_message"
    transcript.write_text(json.dumps(event) + "\n")
    with pytest.raises(announcements.AnnouncementError, match="no published"):
        announcements.require_posted(CLAIM, "different brief", env)
    announcements.require_posted(CLAIM, "job", env)
    with pytest.raises(announcements.AnnouncementError, match="no published"):
        announcements.require_posted(CLAIM, "job", env)


def test_opencode_requires_a_delivered_message_for_the_same_session(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"XDG_RUNTIME_DIR": env["XDG_RUNTIME_DIR"], "OPENCODE_SESSION_ID": "ses_one"}
    queued: dict[str, Any] = {}
    delivered: dict[str, Any] = {}

    def api(
        session: str,
        path: str,
        _env: Mapping[str, str],
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert session == "ses_one"
        if body is not None:
            assert path == "/synthetic"
            assert not body["resume"]
            queued.update(body)
            return {"id": body["id"]}
        assert path == f"/message/{queued['id']}"
        return delivered

    monkeypatch.setattr(announcements, "_opencode", api)
    announcements.prepare(CLAIM, "job", WORKER, "Boundary tests", None, {}, env)
    with pytest.raises(announcements.AnnouncementError, match="no published"):
        announcements.require_posted(CLAIM, "job", env)
    delivered.update(queued, type="synthetic")
    with pytest.raises(announcements.AnnouncementError, match="no published"):
        announcements.require_posted(
            CLAIM, "job", {**env, "OPENCODE_SESSION_ID": "ses_two"}
        )
    announcements.require_posted(CLAIM, "job", env)


@pytest.mark.parametrize("stale", [False, True])
def test_announcement_uses_the_saved_cost_for_the_selected_effort(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    series = f"{ARTIFICIAL_ANALYSIS}-5.0"
    report = {
        "sources": {
            series: {
                "stale": stale,
                "title": "Artificial Analysis Intelligence Index",
                "publisher": "Artificial Analysis",
                "edition": "5.0",
            },
        },
        "workers": {
            "codex:gpt-6.1-sol": {
                "effort": "high",
                "model_reference": {
                    "source": series,
                    "efforts": {
                        "high": {"metrics": {"task_usd": 0.8}},
                        "low": {"metrics": {"task_usd": 0.12}},
                    },
                },
            },
        },
    }
    monkeypatch.setattr(announcements.benchmarks, "cached", lambda *_: report)
    citation = f"{series}:task_usd"
    if stale:
        with pytest.raises(announcements.AnnouncementError, match="no current"):
            announcements.prepare(
                CLAIM, "job", WORKER, "Boundary tests", citation, {}, env
            )
        return
    marker = announcements.prepare(
        CLAIM, "job", WORKER, "Boundary tests", citation, {}, env
    )
    transcript = tmp_path / "chat.jsonl"
    transcript.touch()
    text = announcements.claude_notice(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "transcript_path": str(transcript),
            "session_id": env["CLAUDE_CODE_SESSION_ID"],
            "tool_use_id": "preview-tool",
            "tool_response": {"stdout": marker},
        },
        env,
    )
    assert text is not None
    assert text.endswith(
        "Artificial Analysis Intelligence Index 5.0: 0.12 USD per Index task."
    )
