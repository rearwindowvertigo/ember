"""Run worker commands in transient systemd user scopes and end their members."""

import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from ember import processes

TIMEOUT = 10
CGROUP_ROOT = Path("/sys/fs/cgroup")


class ScopeError(RuntimeError):
    """A scope could not be inspected or signalled."""


def unit(run_id: str) -> str:
    """Return the systemd scope unit for a run id."""
    return f"ember-run-{run_id}.scope"


def _run(
    args: Sequence[str], env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScopeError(f"systemctl --user {args[0]}: {error}") from error


def available(env: Mapping[str, str] | None = None) -> bool:
    """Return whether both systemd tools and the user manager are available."""
    env = os.environ if env is None else env
    if any(
        shutil.which(name, path=env.get("PATH", os.defpath)) is None
        for name in ("systemd-run", "systemctl")
    ):
        return False
    try:
        return _run(["show-environment"], env).returncode == 0
    except ScopeError:
        return False


def command(run_id: str, argv: Sequence[str]) -> tuple[str, ...]:
    """Wrap a pane command in a scope, keeping its environment, cwd and pid."""
    return (
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "--unit", unit(run_id), "--", *argv,
    )  # fmt: skip


def _group(run_id: str, env: Mapping[str, str]) -> Path | None:
    result = _run(["show", "--property=LoadState,ControlGroup", unit(run_id)], env)
    values = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    if values.get("LoadState") == "not-found":
        return None
    if result.returncode:
        raise ScopeError(
            result.stderr.strip() or f"systemctl exited {result.returncode}"
        )
    group = values.get("ControlGroup", "")
    if not group and values.get("LoadState") == "loaded":
        return None
    if not group.startswith("/") or group == "/" or ".." in Path(group).parts:
        raise ScopeError(f"invalid ControlGroup for {unit(run_id)}: {group!r}")
    return CGROUP_ROOT / group.lstrip("/")


def _members(group: Path) -> list[int]:
    result: set[int] = set()
    try:
        files = [group / "cgroup.procs", *group.rglob("*/cgroup.procs")]
        for file in files:
            try:
                pids = file.read_text().split()
            except FileNotFoundError:
                continue
            # cgroup.procs omits zombies and includes reparented processes.
            result.update(int(text) for text in pids)
    except (OSError, ValueError) as error:
        raise ScopeError(f"cannot read scope members: {error}") from error
    return sorted(result)


def end(
    run_id: str,
    *,
    env: Mapping[str, str] | None = None,
    wait: float = processes.END_WAIT,
) -> list[int]:
    """Signal all scope members with SIGTERM, then SIGKILL after `wait`.

    Args:
        run_id: The run whose scope is ended; a missing unit is ignored.
        env: The environment; `None` reads `os.environ`.
        wait: Seconds before SIGKILL, as in `processes.end`.

    Returns:
        The ids of members still running after SIGKILL.

    Raises:
        ScopeError: systemctl failed or scope members could not be read.
    """
    env = os.environ if env is None else env
    group = _group(run_id, env)
    if group is None:
        return []
    for signal in ("SIGTERM", "SIGKILL"):
        result = _run(
            ["kill", "--kill-whom=all", f"--signal={signal}", unit(run_id)], env
        )
        if result.returncode:
            if _group(run_id, env) is None:
                return []
            raise ScopeError(
                result.stderr.strip() or f"systemctl exited {result.returncode}"
            )
        deadline = time.monotonic() + (wait if signal == "SIGTERM" else 0.5)
        while True:
            alive = _members(group)
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if not alive:
            return []
    return alive
