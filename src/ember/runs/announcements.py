"""Publish and verify the conversation notice for an automatic single worker."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember import benchmarks, choices, ground, paths
from ember.benchmark_sources import Observation
from ember.engines import status

if TYPE_CHECKING:
    from ember.runs.core import WorkerSelection

MARKER = "ember announcement: "
"""The preview line that lets Claude Code's skill hook find the pending notice."""

_UNITS = {
    "task_usd": "USD per Index task",
    "pass_usd": "USD per pass",
    "attempt_usd": "USD per attempt",
}


class AnnouncementError(ValueError):
    """Ember could not publish or verify an automatic worker announcement."""


def _claude(env: Mapping[str, str]) -> bool:
    return bool(env.get("CLAUDECODE") or env.get(ground.ENTRYPOINT_VARIABLE))


def _opencode_session(env: Mapping[str, str]) -> str | None:
    session = env.get("OPENCODE_SESSION_ID", "")
    return session if re.fullmatch(r"ses_[A-Za-z0-9_-]+", session) else None


def _opencode(
    session: str,
    path: str,
    env: Mapping[str, str],
    *,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binary = status.locate("opencode", env)
    if binary is None:
        raise AnnouncementError(
            "the OpenCode CLI is needed to post to this conversation"
        )
    command = [binary, "api"]
    if server := env.get("EMBER_OPENCODE_SERVER"):
        command.extend(("--server", server))
    if body is not None:
        command.extend(("--data", json.dumps(body)))
    method = "post" if body is not None else "get"
    command.extend((method, f"/api/session/{session}{path}"))
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        result.check_returncode()
        value = json.loads(result.stdout)
        data = value.get("data", value) if isinstance(value, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise AnnouncementError(
            "OpenCode could not confirm the announcement in this session"
        ) from error
    if not isinstance(data, dict):
        raise AnnouncementError("OpenCode returned no announcement in this session")
    return data


@contextlib.contextmanager
def _locked(claim: str, env: Mapping[str, str]) -> Iterator[tuple[Path, Path]]:
    claim = choices.check_id(claim)
    root = paths.runtime_dir(env)
    folder = paths.ensure_private_dir(root / "announcements", root=root)
    fd = paths.open_private_file(folder / "lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield folder / f"{claim}.json", root
    finally:
        os.close(fd)


def _load(file: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(file.read_bytes())
    except FileNotFoundError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def _save(file: Path, root: Path, value: dict[str, Any]) -> None:
    paths.replace_private_file(file, json.dumps(value).encode(), root=root)


def pending(claim: str, env: Mapping[str, str]) -> bool:
    """Return whether this claim has an unused automatic preview."""
    with contextlib.suppress(paths.RuntimeDirError):
        folder = paths.runtime_dir(env) / "announcements"
        return (folder / f"{choices.check_id(claim)}.json").is_file()
    return False


def fingerprint(worker: WorkerSelection, checkout: str, base: str, brief: str) -> str:
    """Bind a notice to the resolved worker, job and checkout commit."""
    material = [asdict(worker), checkout, base, brief]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def _benchmark(
    citation: str,
    worker: WorkerSelection,
    answers: Mapping[str, Mapping[str, Any]],
    env: Mapping[str, str],
) -> str:
    series, _, metric = citation.rpartition(":")
    if not series or metric not in _UNITS:
        raise AnnouncementError(
            "--benchmark-cost needs a benchmark series and cost metric"
        )
    report = benchmarks.cached(answers, env)
    source = report["sources"].get(series)
    evidence = report["workers"].get(f"{worker.engine}:{worker.model}")
    row: Observation | None = None
    if evidence is not None and worker.effort is not None:
        reference = evidence.get("model_reference")
        if reference is not None and reference["source"] == series:
            row = reference["efforts"].get(worker.effort)
        else:
            row = evidence["matches_by_effort"].get(worker.effort, {}).get(series)
    if not source or source["stale"] or row is None or metric not in row["metrics"]:
        raise AnnouncementError(
            f"no current {citation} cost is saved for {worker.model} at {worker.effort}"
        )
    title = source["title"]
    edition = source["edition"]
    if edition and edition not in title:
        title = f"{title} {edition}"
    if source["publisher"].casefold() not in title.casefold():
        title = f"{title} ({source['publisher']})"
    return f"{title}: {row['metrics'][metric]:g} {_UNITS[metric]}."


def prepare(
    claim: str,
    decision: str,
    worker: WorkerSelection,
    reason: str,
    citation: str | None,
    answers: Mapping[str, Mapping[str, Any]],
    env: Mapping[str, str],
) -> str:
    """Stage the notice and ask the lead's harness to post it to the conversation."""
    reason = " ".join(reason.split()).rstrip(".")
    if not reason:
        raise AnnouncementError("--announce needs a task-specific reason")
    effort = worker.effort or "default"
    speed = f" ({worker.speed})" if worker.speed else ""
    text = (
        f"Ember will use {worker.engine} {worker.model} at {effort} effort{speed}: "
        f"{reason}."
    )
    if citation is not None:
        text += " " + _benchmark(citation, worker, answers, env)
    claude = _claude(env)
    session = None if claude else _opencode_session(env)
    if session is None and not claude:
        raise AnnouncementError("no Claude Code or OpenCode conversation is available")
    notice_id = "msg_" + secrets.token_hex(13)
    payload = {
        "id": notice_id,
        "decision": decision,
        "text": text,
        "harness": "claude" if session is None else "opencode",
        "session": session,
    }
    with _locked(claim, env) as (file, root):
        _save(file, root, payload)
    if session is not None:
        reply = _opencode(
            session,
            "/synthetic",
            env,
            body={
                "id": notice_id,
                "text": text,
                "description": text,
                "delivery": "steer",
                "resume": False,
            },
        )
        if reply.get("id") != notice_id:
            raise AnnouncementError(
                "OpenCode did not accept the announcement in this session"
            )
    return MARKER + f"{claim} {notice_id}"


def claude_notice(hook: object, env: Mapping[str, str]) -> str | None:
    """Return a native Claude Code notice for an Ember announcement preview."""
    if (
        not isinstance(hook, dict)
        or hook.get("hook_event_name") != "PostToolUse"
        or hook.get("tool_name") != "Bash"
    ):
        return None
    result = hook.get("tool_response")
    output = result.get("stdout", "") if isinstance(result, dict) else ""
    if not isinstance(output, str):
        return None
    if output.lstrip().startswith("{"):
        value = json.loads(output)
        output = value.get("announcement", "") if isinstance(value, dict) else ""
        if not isinstance(output, str):
            return None
    marker = re.search(
        rf"(?m)^{MARKER}([a-z0-9][a-z0-9-]{{5,63}}) (msg_[a-f0-9]{{26}})$",
        output,
    )
    if marker is None:
        return None
    transcript, session, tool = (
        hook.get("transcript_path"),
        hook.get("session_id"),
        hook.get("tool_use_id"),
    )
    if (
        not isinstance(transcript, str)
        or not isinstance(session, str)
        or not isinstance(tool, str)
        or not session
        or not tool
    ):
        return None
    path = Path(transcript)
    if not path.is_absolute():
        return None
    with _locked(marker[1], env) as (file, root):
        value = _load(file)
        if (
            not value
            or value.get("id") != marker[2]
            or value.get("harness") != "claude"
            or not isinstance(text := value.get("text"), str)
        ):
            return None
        value.update(
            transcript=transcript,
            offset=path.stat().st_size,
            session=session,
            tool=tool,
        )
        _save(file, root, value)
    return text


def _claude_posted(value: dict[str, Any]) -> bool:
    path, offset = value.get("transcript"), value.get("offset")
    if not isinstance(path, str) or type(offset) is not int or offset < 0:
        return False
    try:
        with Path(path).open("rb") as stream:
            stream.seek(offset)
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (
                    not isinstance(entry, dict)
                    or entry.get("isSidechain")
                    or entry.get("sessionId") != value.get("session")
                ):
                    continue
                notice = entry.get("attachment")
                if (
                    entry.get("type") == "attachment"
                    and isinstance(notice, dict)
                    and notice.get("type") == "hook_system_message"
                    and notice.get("hookEvent") == "PostToolUse"
                    and notice.get("toolUseID") == value.get("tool")
                    and notice.get("content") == value["text"]
                ):
                    return True
    except OSError:
        return False
    return False


def _posted(value: dict[str, Any], env: Mapping[str, str]) -> bool:
    if _claude(env):
        return value.get("harness") == "claude" and _claude_posted(value)
    session = _opencode_session(env)
    if (
        session is None
        or value.get("harness") != "opencode"
        or value.get("session") != session
    ):
        return False
    notice_id = value.get("id")
    if not isinstance(notice_id, str) or not re.fullmatch(
        r"msg_[a-f0-9]{26}", notice_id
    ):
        return False
    reply = _opencode(session, f"/message/{notice_id}", env)
    return (
        reply.get("id") == notice_id
        and reply.get("type") == "synthetic"
        and reply.get("description") == value["text"]
        and reply.get("text") == value["text"]
    )


def require_posted(claim: str, decision: str, env: Mapping[str, str]) -> None:
    """Consume a matching published notice before the worker can start."""
    with _locked(claim, env) as (file, _):
        value = _load(file)
    if (
        value is None
        or value.get("decision") != decision
        or not isinstance(value.get("text"), str)
        or not _posted(value, env)
    ):
        raise AnnouncementError(
            "no published announcement matches this worker and brief"
            " in the lead's conversation"
        )
    with _locked(claim, env) as (file, _):
        if _load(file) != value:
            raise AnnouncementError(
                "the matching announcement changed or was already used"
            )
        file.unlink()
