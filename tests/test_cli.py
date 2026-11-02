import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ember import __version__, choices, deps, paths, skill
from ember.cli import main
from ember.engines import models, status


def test_version_prints_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"emb {__version__}\n"


def test_no_command_prints_help_to_stderr_and_exits_64(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 64
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: emb" in captured.err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert "usage: emb" in capsys.readouterr().out


def test_choices_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["choices", "--help"])
    assert exit_info.value.code == 0
    assert "--expect-generation" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--nope"],
        ["nope"],
        ["choices", "--nope"],
        ["choices", "--expect-generation", "x", "--clear-once"],
    ],
)
def test_argparse_usage_errors_exit_64(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 64
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: emb" in captured.err


# emb choices


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": str(empty_bin),
    }


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


@pytest.fixture
def no_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


def _run(
    argv: list[str], env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, Any, str]:
    code = main(["choices", *argv], env=env)
    captured = capsys.readouterr()
    data = json.loads(captured.out) if captured.out else None
    return code, data, captured.err


def test_read_prints_reply(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, data, err = _run([], env, capsys)
    assert code == 0
    assert err == ""
    assert set(data) == {
        "answers",
        "claim",
        "generation",
        "errors",
        "source_error",
        "menu",
    }
    assert data["claim"] is None
    assert not paths.settings_db_path(env).exists()


def test_json_changes_nothing(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["choices"], env=env)
    plain = capsys.readouterr().out
    main(["choices", "--json"], env=env)
    assert capsys.readouterr().out == plain
    assert plain.startswith('{\n  "answers"')


def test_owner_writes_and_claim(
    env: dict[str, str], terminal: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, data, _ = _run(
        [
            "--set",
            "shortlist.codex=gpt-5@high,o3@low",
            "--set",
            "single.writer=codex:o3",
        ],
        env,
        capsys,
    )
    assert code == 0
    assert data == {
        "settings": {
            "shortlist.codex": ["gpt-5@high", "o3@low"],
            "single.writer": "codex:o3",
        },
        "generation": 1,
        "errors": [],
    }
    code, data, _ = _run(
        ["--once", "single.writer=ask", "--expect-generation", "1"], env, capsys
    )
    assert code == 0
    assert data == {"once": {"single.writer": "ask"}, "generation": 2, "errors": []}
    code, data, _ = _run(
        ["--claim", "--id", "run-000001", "--request", "single.writer_as=subagent"],
        env,
        capsys,
    )
    assert code == 0
    assert data["claim"] == {"id": "run-000001", "status": "claimed"}
    assert data["answers"]["single.writer"] == {"value": "ask", "layer": "one-time"}
    assert data["answers"]["single.writer_as"] == {
        "value": "subagent",
        "layer": "request",
    }
    code, data, _ = _run(["--id", "run-000001"], env, capsys)
    assert data["claim"]["status"] == "claimed"
    code, data, _ = _run(["--finish", "run-000001"], env, capsys)
    assert code == 0
    assert data == {
        "claim": {"id": "run-000001", "status": "gone"},
        "generation": 4,
        "errors": [],
    }
    code, data, _ = _run(["--clear", "single.writer"], env, capsys)
    assert data["settings"] == {"shortlist.codex": ["gpt-5@high", "o3@low"]}
    code, data, _ = _run(["--clear-once"], env, capsys)
    assert data == {"cleared": "one-time", "generation": 5, "errors": []}


def test_empty_list_value(
    env: dict[str, str], terminal: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, data, _ = _run(["--set", "sandbox.read="], env, capsys)
    assert code == 0
    assert data["settings"] == {"sandbox.read": []}


@pytest.mark.parametrize(
    "argv",
    [
        ["--set", "app.theme=dark"],
        ["--clear", "app.theme"],
        ["--once", "single.writer=ask"],
        ["--clear-once"],
    ],
)
def test_owner_options_without_terminal_exit_77(
    env: dict[str, str],
    no_terminal: None,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, data, err = _run(argv, env, capsys)
    assert code == 77
    assert data is None
    assert err == "emb choices: owner options require a terminal\n"
    assert not paths.settings_db_path(env).exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--claim"],
        ["--claim", "--id", "run-000001", "--finish", "run-000001"],
        ["--set", "app.theme=dark", "--clear", "app.theme"],
        ["--once", "single.writer=ask", "--clear-once"],
        ["--finish", "run-000001", "--id", "run-000001"],
        ["--set", "app.theme=dark", "--request", "single.writer=ask"],
        ["--expect-generation", "0"],
        ["--claim", "--id", "run-000001", "--expect-generation", "0"],
        ["--set", "app.theme"],
        ["--set", "app.theme=dark", "--set", "app.theme=light"],
        ["--set", "nope=1"],
        ["--set", "app.theme=purple"],
        ["--once", "terminal=xterm"],
        ["--request", "worker.network=on"],
        ["--request", "single.writer=bad"],
        ["--id", "../escape"],
        ["--claim", "--id", "short"],
        ["--finish", "UPPER-CASE"],
    ],
)
def test_usage_errors_and_invalid_values_exit_64(
    env: dict[str, str],
    terminal: None,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, data, err = _run(argv, env, capsys)
    assert code == 64
    assert data is None
    assert err.startswith("emb choices: ")
    assert not paths.settings_db_path(env).exists()


def test_stale_generation_exits_75(
    env: dict[str, str], terminal: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(["--set", "app.theme=dark"], env, capsys)
    code, data, err = _run(
        ["--set", "app.theme=light", "--expect-generation", "0"], env, capsys
    )
    assert code == 75
    assert data is None
    assert err.startswith("emb choices: ")
    assert choices.read(env=env)["answers"]["app.theme"]["value"] == "dark"


def test_unreadable_database_exits_74(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = paths.settings_db_path(env)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database" * 200)
    code, data, err = _run([], env, capsys)
    assert code == 74
    assert data is None
    assert err.startswith("emb choices: ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files at mode 000")
def test_database_without_read_permission_exits_74(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"app.theme": "dark"}, env=env)
    path = paths.settings_db_path(env)
    for entry in path.parent.iterdir():
        entry.chmod(0)
    try:
        code, _, _ = _run([], env, capsys)
    finally:
        for entry in path.parent.iterdir():
            entry.chmod(0o600)
    assert code == 74


def test_newer_schema_exits_78(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:

    choices.set_settings({"app.theme": "dark"}, env=env)
    conn = sqlite3.connect(paths.settings_db_path(env))
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema'")
    conn.commit()
    conn.close()
    code, data, err = _run([], env, capsys)
    assert code == 78
    assert data is None
    assert "schema" in err


def test_menu_value_not_shortlisted_exits_64(
    env: dict[str, str], terminal: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, data, err = _run(["--set", "single.writer=codex:gpt-5"], env, capsys)
    assert code == 64
    assert data is None
    assert "shortlist" in err
    assert choices.read(env=env)["generation"] == 0


# emb engines


def _engines(
    argv: list[str], env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = main(["engines", *argv], env=env)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _stub(env: dict[str, str], name: str, body: str) -> None:
    path = Path(env["PATH"]) / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


CODEX_OK = """case "$1" in
--version) echo "codex-cli 1.2.3";;
login) echo "Logged in using ChatGPT";;
esac
"""


def test_engines_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["engines", "--help"])
    assert exit_info.value.code == 0
    assert "--models" in capsys.readouterr().out


def test_engines_json(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    _stub(env, "codex", CODEX_OK)
    code, out, err = _engines(["--json"], env, capsys)
    assert code == 0
    assert err == ""
    data = json.loads(out)
    assert list(data) == ["codex", "opencode", "claude"]
    assert data["codex"]["connected"] is True
    assert data["codex"]["state"] == "connected"
    assert data["opencode"]["state"] == "not installed"
    assert data["opencode"]["requirements"][0] == {
        "label": "the opencode CLI",
        "met": False,
        "fix": "curl -fsSL https://opencode.ai/install | bash",
    }


def test_engines_text(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    _stub(env, "codex", CODEX_OK)
    code, out, err = _engines([], env, capsys)
    assert code == 0
    assert err == ""
    lines = out.splitlines()
    assert lines[0] == "codex: connected, codex-cli 1.2.3"
    assert lines[1].startswith("opencode: not installed: ")
    assert (
        lines[2] == "  the opencode CLI: curl -fsSL https://opencode.ai/install | bash"
    )
    assert lines[3] == "  an OpenRouter credential for OpenCode"
    assert lines[4].startswith("claude: not installed: ")


def test_engines_reads_cache_until_refresh(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(env, "codex", CODEX_OK)
    _engines(["--json"], env, capsys)
    (Path(env["PATH"]) / "codex").unlink()
    _, out, _ = _engines(["--json"], env, capsys)
    assert json.loads(out)["codex"]["connected"] is True
    _, out, _ = _engines(["--json", "--refresh"], env, capsys)
    assert json.loads(out)["codex"]["state"] == "not installed"


def test_engines_models_json(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _engines(["--models", "claude", "--json"], env, capsys)
    assert code == 0
    assert err == ""
    data = json.loads(out)
    assert data["source"] == "cli"
    assert data["reason"] is None
    assert data["models"][1]["id"] == "opus"


def test_engines_models_text_from_cache(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "fetched_at": time.time() - 120,
        "models": [
            models.Model(
                "openai/gpt-4o",
                "GPT-4o",
                ("low", "high"),
                "low",
                128000,
                0.0000025,
                0.00001,
                True,
            ).to_json()
        ],
    }
    status.save_cache_file(models.cache_path("opencode", env), payload, env)
    code, out, err = _engines(["--models", "opencode"], env, capsys)
    assert code == 0
    assert err == ""
    assert out.splitlines() == [
        "opencode models from cache, read 2 min ago",
        "openai/gpt-4o  GPT-4o  efforts low, high (default low)  context 128000  "
        "$2.50/M in  $10.00/M out  tools",
    ]


def test_engines_models_failure_reason_on_stderr(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _engines(["--models", "codex"], env, capsys)
    assert code == 0
    assert out == "codex models from cli, never read\n"
    assert err.startswith("emb engines: ")
    assert "not installed" in err
    code, out, err = _engines(["--models", "codex", "--json"], env, capsys)
    assert code == 0
    data = json.loads(out)
    assert data == {
        "models": [],
        "source": "cli",
        "fetched_at": None,
        "reason": data["reason"],
    }
    assert data["reason"] in err


def test_engines_models_invalid_engine_exits_64(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["engines", "--models", "gpt"], env=env)
    assert exit_info.value.code == 64
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--models" in captured.err


# emb deps


class _DepsFetch:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> str:
        self.calls.append(url)
        if url == "https://registry.npmjs.org/@openai/codex/latest":
            return '{"version": "1.0.0"}'
        raise OSError("no network")


@pytest.fixture
def deps_fetch(monkeypatch: pytest.MonkeyPatch) -> _DepsFetch:
    fetch = _DepsFetch()
    monkeypatch.setattr(deps, "fetch_text", fetch)
    return fetch


def _deps_stubs(env: dict[str, str]) -> None:
    _stub(env, "dpkg-query", 'printf "installed\\t1.0"\n')
    _stub(env, "apt-cache", 'printf "  Candidate: 1.1\\n"\n')
    _stub(env, "dpkg", "exit 0\n")
    _stub(env, "codex", "echo codex-cli 1.0.0\n")


def test_deps_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["deps", "--help"])
    assert exit_info.value.code == 0
    assert "--refresh" in capsys.readouterr().out


def test_deps_usage_error_exits_64(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["deps", "--nope"], env=env)
    assert exit_info.value.code == 64
    assert capsys.readouterr().out == ""


def test_deps_text(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], deps_fetch: _DepsFetch
) -> None:
    _deps_stubs(env)
    assert main(["deps"], env=env) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[:8] == [
        "Codex: current, installed 1.0.0, newest 1.0.0",
        "OpenCode: missing",
        "  install: curl -fsSL https://opencode.ai/install | bash",
        "Claude Code: missing",
        "  install: curl -fsSL https://claude.ai/install.sh | bash",
        "Claude Desktop: update available, installed 1.0, newest 1.1",
        "  update: sudo apt install --only-upgrade claude-desktop",
        "tmux: update available, installed 1.0, newest 1.1",
    ]
    assert lines[-1].startswith("PySide6: ")


def test_deps_json_and_refresh(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], deps_fetch: _DepsFetch
) -> None:
    _deps_stubs(env)
    codex = "https://registry.npmjs.org/@openai/codex/latest"
    assert main(["deps", "--json"], env=env) == 0
    data = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in data["components"]][:3] == [
        "codex",
        "opencode",
        "claude",
    ]
    assert data["components"][0]["status"] == "current"
    assert deps_fetch.calls.count(codex) == 1
    assert main(["deps", "--json"], env=env) == 0
    capsys.readouterr()
    assert deps_fetch.calls.count(codex) == 1
    assert main(["deps", "--refresh"], env=env) == 0
    capsys.readouterr()
    assert deps_fetch.calls.count(codex) == 2


def test_deps_invalid_component_list_exits_70(
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "components_text", lambda: "[[components]\n")
    assert main(["deps"], env=env) == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("emb deps: components.toml is not valid TOML")


# emb run


@pytest.fixture
def run_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    environment = _stub_engine().make_env(tmp_path)
    yield environment
    _stub_engine().kill_sessions(tmp_path)


def _stub_engine() -> types.ModuleType:
    """Return the run tests' stub helpers, from `tests/runs/stub_engine.py`."""
    return importlib.import_module("runs.stub_engine")


def test_run_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for option in ("--engine", "--task-file", "--recover", "--stall", "--trailer"):
        assert option in out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--dir", "."], "a new run needs --engine, --task-file"),
        (["--screen", "r", "--status"], "--screen and --status cannot be combined"),
        (["--screen", "r", "--model", "m"], "--model cannot be used with --screen"),
        (["--send", "r"], "--send and --message-file go together"),
        (["--status", "--message-file", "f"], "--send and --message-file go together"),
        (["--status", "--force"], "--force needs --close or --send"),
        (["--close", "r", "--json"], "--json needs --status"),
    ],
)
def test_run_usage_errors_exit_64(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", *argv], env={}) == 64
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"emb run: {message}\nnext: emb run --help\n"


@pytest.mark.parametrize("value", ["0", "-1", "x", "inf"])
def test_run_stall_must_be_positive(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "--status", "--stall", value])
    assert exit_info.value.code == 64
    assert "--stall" in capsys.readouterr().err


def test_run_status_with_no_runs(
    run_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--status", "--json"], env=run_env) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_run_start_status_and_close(
    tmp_path: Path,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ember import engines
    from ember.runs import core

    stub = _stub_engine()

    monkeypatch.setattr(core, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(
        engines,
        "load_driver",
        lambda name, env=None: stub.StubDriver(name, stub.script_path(run_env)),
    )
    repo = stub.make_repo(tmp_path, run_env)
    brief = tmp_path / "brief.md"
    brief.write_text("Write a file\n@write out.txt hello\n")
    argv = [
        "run", "--engine", "claude", "--dir", str(repo), "--task-file", str(brief),
        "--trailer", "Co-Authored-By: X <x@y>", "--quiet",
    ]  # fmt: skip
    assert main(argv, env=run_env) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    run_id = out[0].removeprefix("run ")
    assert out[1].endswith(" · exit 0 · 1 files · turn 1")
    assert main(["run", "--status", run_id, "--json"], env=run_env) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["trailers"] == ["Co-Authored-By: X <x@y>"]
    assert data["session_state"] == "live"
    assert main(["run", "--close", run_id], env=run_env) == 0
    assert capsys.readouterr().out == f"closed {run_id}\n"


def test_run_without_driver_exits_69(
    tmp_path: Path,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "ember.engines.codex", None)
    repo = _stub_engine().make_repo(tmp_path, run_env)
    brief = tmp_path / "brief.md"
    brief.write_text("Task\n")
    argv = ["run", "--engine", "codex", "--model", "gpt-5", "--dir", str(repo)]
    code = main([*argv, "--task-file", str(brief)], env=run_env)
    captured = capsys.readouterr()
    assert code == 69
    assert captured.out == ""
    assert "emb run: Ember has no driver for codex\n" in captured.err
    assert captured.err.splitlines()[-1] == (
        "owner: this Ember build has no driver for codex; update Ember"
    )


# emb skill


@pytest.fixture
def skill_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "checkout" / "skill"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: ember\n---\n")
    monkeypatch.setattr(skill, "source_candidates", lambda: (folder,))
    return folder.resolve()


def test_skill_help_names_link_and_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["skill", "--help"])
    assert exit_info.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "$CLAUDE_CONFIG_DIR/skills/ember" in out
    assert "~/.claude/skills/ember" in out
    assert "/usr/share/ember/skill" in out
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "install or remove the ember skill for Claude Code" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "argv",
    [["skill"], ["skill", "nope"], ["skill", "status", "--force"]],
)
def test_skill_usage_errors_exit_64(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 64
    assert capsys.readouterr().out == ""


def test_skill_install_status_uninstall(
    env: dict[str, str], skill_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link = Path(env["HOME"]) / ".claude" / "skills" / "ember"
    assert main(["skill", "install"], env=env) == 0
    assert capsys.readouterr().out == f"installed: {link} -> {skill_source}\n"
    assert main(["skill", "install", "--json"], env=env) == 0
    assert json.loads(capsys.readouterr().out) == {
        "link": str(link),
        "action": "already installed",
        "target": str(skill_source),
    }
    assert main(["skill", "status"], env=env) == 0
    assert capsys.readouterr().out == (
        f"link: {link}\nstate: installed\ntarget: {skill_source}\n"
        f"source: {skill_source}\n"
    )
    assert main(["skill", "status", "--json"], env=env) == 0
    assert json.loads(capsys.readouterr().out) == {
        "link": str(link),
        "state": "installed",
        "target": str(skill_source),
        "source": str(skill_source),
        "source_reason": None,
    }
    assert main(["skill", "uninstall", "--json"], env=env) == 0
    assert json.loads(capsys.readouterr().out) == {
        "link": str(link),
        "action": "removed",
        "target": str(skill_source),
    }
    assert main(["skill", "uninstall"], env=env) == 0
    assert capsys.readouterr().out == f"not installed: {link}\n"


def test_skill_install_refusals(
    env: dict[str, str],
    skill_source: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    link = Path(env["HOME"]) / ".claude" / "skills" / "ember"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path)
    assert main(["skill", "install"], env=env) == 75
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"emb skill: {link} links to {tmp_path.resolve()}, not to {skill_source}\n"
        "next: emb skill install --force\n"
    )
    assert main(["skill", "install", "--force"], env=env) == 0
    capsys.readouterr()
    link.unlink()
    link.mkdir()
    assert main(["skill", "uninstall"], env=env) == 75
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"owner: move or remove {link}, then emb skill install"
    )
    assert link.is_dir()


def test_skill_without_source(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "nowhere"
    monkeypatch.setattr(skill, "source_candidates", lambda: (missing,))
    assert main(["skill", "status"], env=env) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"source: null (the skill is missing from {missing})"
    )
    assert main(["skill", "install", "--json"], env=env) == 66
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "emb skill: no skill source found\n"
        f"owner: reinstall Ember; the skill is missing from {missing}\n"
    )
    assert main(["skill", "uninstall"], env=env) == 0


# emb app


def test_app_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["app", "--help"])
    assert exit_info.value.code == 0
    assert "--hidden" in capsys.readouterr().out


def test_app_usage_error_exits_64(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["app", "--nope"])
    assert exit_info.value.code == 64
    assert "unrecognized arguments: --nope" in capsys.readouterr().err


def test_app_without_a_graphical_session_exits_69(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    assert main(["app", "--hidden"], env=env) == 69
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "emb app: no graphical session (WAYLAND_DISPLAY and DISPLAY are unset)\n"
    )


def test_other_commands_do_not_import_qt(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from ember.cli import main\n"
        "try:\n"
        "    main(['deps', '--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(sorted(name for name in sys.modules if name.startswith('PySide6')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=tmp_path,
    )
    assert result.stdout.splitlines()[-1] == "[]"
