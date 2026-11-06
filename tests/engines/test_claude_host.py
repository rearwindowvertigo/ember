"""Per-turn environments passed from the driver to the Claude CLI."""

import json
import sys
from pathlib import Path

import pytest

from ember import sandbox
from ember.engines.claude import turn_files
from ember.engines.claude_host import Host


def write_request(folder: Path, turn: int, **extra: object) -> None:
    """Write a turn that prints its subprocess environment as JSON."""
    files = turn_files(folder, turn)
    files["prompt"].write_text("Inspect\n")
    files["json"].write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import json, os; print(json.dumps(dict(os.environ)))",
                ],
                "prompt": str(files["prompt"]),
                **extra,
            }
        )
    )


@pytest.mark.parametrize("sandboxed", [False, True])
def test_host_applies_environment_only_to_requested_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandboxed: bool
) -> None:
    """The CLI receives overrides, which neither leak nor restore hidden keys."""
    outer = {
        "HOME": str(tmp_path),
        "PYTEST_ADDOPTS": "--junitxml=owner.xml",
        "KEEP": "present",
        "CLAUDECODE": "1",
    }
    overrides = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "--junitxml=owner.xml -p no:cacheprovider",
        "RUFF_CACHE_DIR": str(tmp_path / "cache" / "ruff"),
        "UV_FROZEN": "1",
        "CLAUDECODE": "1",
    }
    wrapped: list[dict[str, object]] = []

    def wrap(argv: list[str], **kwargs: object) -> list[str]:
        """Record the sandbox arguments while running the local CLI stub."""
        wrapped.append(kwargs)
        return argv

    monkeypatch.setattr(sandbox, "claude_wrap", wrap)
    box = {
        "worktree": str(tmp_path),
        "common": str(tmp_path / "common"),
        "read": [],
        "read_only": False,
        "network": False,
    }
    host = Host(tmp_path, str(tmp_path), outer)
    write_request(tmp_path, 1, env=overrides, sandbox=box if sandboxed else None)
    assert host.run_turn(1) == 0
    first = json.loads(turn_files(tmp_path, 1)["jsonl"].read_text())
    for key, value in overrides.items():
        if key != "CLAUDECODE":
            assert first[key] == value
    assert first["KEEP"] == "present"
    assert "CLAUDECODE" not in first
    assert "UV_PROJECT_ENVIRONMENT" not in first
    assert len(wrapped) == int(sandboxed)
    if sandboxed:
        assert wrapped[0]["env"] == outer

    write_request(tmp_path, 2)
    assert host.run_turn(2) == 0
    second = json.loads(turn_files(tmp_path, 2)["jsonl"].read_text())
    assert second["PYTEST_ADDOPTS"] == outer["PYTEST_ADDOPTS"]
    assert second["KEEP"] == "present"
    for key in overrides.keys() - {"PYTEST_ADDOPTS"}:
        assert key not in second
    assert host.env["PYTEST_ADDOPTS"] == outer["PYTEST_ADDOPTS"]


@pytest.mark.parametrize("overrides", [None, [], "bad", {"KEY": 1}])
def test_host_refuses_damaged_environment(tmp_path: Path, overrides: object) -> None:
    """Malformed request environments fail before starting the CLI."""
    write_request(tmp_path, 1, env=overrides)
    host = Host(tmp_path, str(tmp_path), {})
    assert host.run_turn(1) == 127
    files = turn_files(tmp_path, 1)
    assert "env is not a string mapping" in files["stderr"].read_text()
    assert not files["pid"].exists()
