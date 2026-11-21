import contextlib
import errno
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from ember import processes, scopes


@pytest.mark.parametrize("missing", ["systemd-run", "systemctl", None])
@pytest.mark.parametrize("status", [0, 1])
def test_availability_checks_tools_and_manager(
    scope_probe: Callable[[Mapping[str, str] | None], bool],
    monkeypatch: pytest.MonkeyPatch,
    missing: str | None,
    status: int,
) -> None:
    seen = []
    environment = {"PATH": "/test/bin", "XDG_RUNTIME_DIR": "/test/runtime"}

    def which(name: str, *, path: str) -> str | None:
        assert path == environment["PATH"]
        return None if name == missing else f"{path}/{name}"

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        assert kwargs["env"] is environment
        return subprocess.CompletedProcess(argv, status, "", "")

    monkeypatch.setattr(scopes.shutil, "which", which)
    monkeypatch.setattr(scopes.subprocess, "run", run)
    assert scope_probe(environment) is (missing is None and status == 0)
    assert seen == ([] if missing else [["systemctl", "--user", "show-environment"]])


def test_command_doubles_dollar_signs() -> None:
    assert scopes.command("r1", ["/bin/sh", "/state/$HOME/${X}/launch"]) == (
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "--unit", "ember-run-r1.scope", "--",
        "/bin/sh", "/state/$$HOME/$${X}/launch",
    )  # fmt: skip


def test_real_scope_passes_dollar_signs_unchanged(
    tmp_path: Path,
    scope_probe: Callable[[Mapping[str, str] | None], bool],
) -> None:
    if not scope_probe(os.environ):
        pytest.skip("no usable systemd user session")
    argument = "a$b/${HOME}/$$c"
    result = subprocess.run(
        scopes.command(
            f"scope-test-{uuid.uuid4().hex}", ["/usr/bin/printf", "%s", argument]
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout == argument


@pytest.mark.parametrize(
    "error", [OSError("unavailable"), subprocess.TimeoutExpired("systemctl", 10)]
)
def test_probe_failure_uses_fallback(
    scope_probe: Callable[[Mapping[str, str] | None], bool],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(scopes.shutil, "which", lambda name, path: f"/bin/{name}")

    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(scopes.subprocess, "run", fail)
    assert not scope_probe({})


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    group = tmp_path / "user.slice" / "test.scope"
    group.mkdir(parents=True)
    members = group / "cgroup.procs"
    members.write_text("123\n")
    state: dict[str, Any] = {
        "group": group,
        "members": members,
        "signals": [],
        "gone": False,
        "failure": False,
        "term_ends": True,
        "survivors": False,
    }
    monkeypatch.setattr(scopes, "CGROUP_ROOT", tmp_path)

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["systemctl", "--user"]
        assert argv[-1] == "ember-run-test-run.scope"
        assert kwargs["env"] == {"TEST": "session"}
        if argv[2] == "show":
            text = (
                "LoadState=not-found\n"
                if state["gone"]
                else ("LoadState=loaded\nControlGroup=/user.slice/test.scope\n")
            )
            return subprocess.CompletedProcess(
                argv, 4 if state["gone"] else 0, text, ""
            )
        assert argv[2:4] == ["kill", "--kill-whom=all"]
        state["signals"].append(argv[4])
        if state["failure"]:
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")
        if state.get("disappear"):
            state["gone"] = True
            return subprocess.CompletedProcess(argv, 1, "", "unit not loaded")
        if not state["survivors"] and (
            state["term_ends"] or argv[4] == "--signal=SIGKILL"
        ):
            for file in group.rglob("cgroup.procs"):
                file.write_text("")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(scopes.subprocess, "run", run)
    return state


def test_end_uses_term_then_kill(manager: dict[str, Any]) -> None:
    manager["term_ends"] = False
    assert scopes.end("test-run", env={"TEST": "session"}, wait=0) == []
    assert manager["signals"] == ["--signal=SIGTERM", "--signal=SIGKILL"]


def test_end_stops_after_term_when_empty(manager: dict[str, Any]) -> None:
    assert scopes.end("test-run", env={"TEST": "session"}, wait=0) == []
    assert manager["signals"] == ["--signal=SIGTERM"]


def test_end_reports_members_of_nested_groups(manager: dict[str, Any]) -> None:
    nested = manager["group"] / "nested" / "deeper"
    nested.mkdir(parents=True)
    (nested / "cgroup.procs").write_text("456\n123\n")
    manager["survivors"] = True
    assert scopes.end("test-run", env={"TEST": "session"}, wait=0) == [123, 456]


@pytest.mark.parametrize("disappear", [False, True])
def test_missing_scope_is_not_an_error(
    manager: dict[str, Any], disappear: bool
) -> None:
    manager["gone"] = not disappear
    manager["disappear"] = disappear
    assert scopes.end("test-run", env={"TEST": "session"}, wait=0) == []


def test_signal_failure_is_reported(manager: dict[str, Any]) -> None:
    manager["failure"] = True
    with pytest.raises(scopes.ScopeError, match="permission denied"):
        scopes.end("test-run", env={"TEST": "session"}, wait=0)


@pytest.mark.parametrize(
    "error", [OSError("no manager"), subprocess.TimeoutExpired("systemctl", 10)]
)
def test_close_command_failure_is_a_scope_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(scopes.subprocess, "run", fail)
    with pytest.raises(scopes.ScopeError, match="systemctl --user show"):
        scopes.end("test-run", env={})


def test_real_scope_ends_reparented_process_and_preserves_launch_context(
    tmp_path: Path,
    scope_probe: Callable[[Mapping[str, str] | None], bool],
) -> None:
    environment = {**os.environ, "EMBER_SCOPE_TEST": "kept value"}
    if not scope_probe(environment):
        pytest.skip("no usable systemd user session")
    run_id = f"scope-test-{uuid.uuid4().hex}"
    pane_marker = tmp_path / "pane.json"
    child_marker = tmp_path / "child.pid"
    script = r"""
import json, os, signal, sys, time
from pathlib import Path
pane, child = map(Path, sys.argv[1:])
pane.write_text(json.dumps([os.getpid(), os.getcwd(), os.environ['EMBER_SCOPE_TEST']]))
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child.write_text(str(os.getpid()))
    while True:
        time.sleep(1)
while True:
    time.sleep(1)
"""
    process = subprocess.Popen(
        scopes.command(
            run_id, [sys.executable, "-c", script, str(pane_marker), str(child_marker)]
        ),
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    child: processes.Process | None = None
    try:
        deadline = time.monotonic() + 10
        while not child_marker.exists() or not child_marker.read_text():
            assert process.poll() is None, process.communicate()[1]
            assert time.monotonic() < deadline
            time.sleep(0.02)
        child_pid = int(child_marker.read_text())
        found = processes.tree(child_pid)
        assert found
        child = found[0]
        assert json.loads(pane_marker.read_text()) == [
            process.pid,
            str(tmp_path),
            "kept value",
        ]
        while child_pid in [pid for pid, _start in processes.tree(process.pid)]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert scopes.end(run_id, env=environment, wait=0.1) == []
        deadline = time.monotonic() + 2
        while processes.running(child):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        process.wait(timeout=10)
        assert scopes.end(run_id, env=environment) == []
    finally:
        with contextlib.suppress(scopes.ScopeError):
            scopes.end(run_id, env=environment, wait=0)
        if child is not None and processes.running(child):
            with contextlib.suppress(OSError):
                os.kill(child[0], signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_members_skip_a_group_removed_while_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    (tmp_path / "cgroup.procs").write_text("123\n")
    (gone / "cgroup.procs").write_text("456\n")
    read_text = Path.read_text

    def read(self: Path) -> str:
        if self.parent == gone:
            raise OSError(errno.ENODEV, os.strerror(errno.ENODEV))
        return read_text(self)

    monkeypatch.setattr(Path, "read_text", read)
    assert scopes._members(tmp_path) == [123]


def test_members_report_other_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cgroup.procs").write_text("123\n")

    def read(self: Path) -> str:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(scopes.ScopeError, match="cannot read scope members"):
        scopes._members(tmp_path)
