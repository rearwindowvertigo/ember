import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import __version__, branches, choices, deps, paths, skill
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


@pytest.mark.parametrize("claim", [None, "claim-a"])
@pytest.mark.parametrize("target", [None, "main"])
def test_cleanup_passes_claim_and_branch(
    monkeypatch: pytest.MonkeyPatch, claim: str | None, target: str | None
) -> None:
    calls: list[tuple[str | None, dict[str, Any]]] = []

    def cleanup(target: str | None, **kwargs: Any) -> int:  # noqa: ANN401
        calls.append((target, kwargs))
        return 0

    monkeypatch.setattr(branches, "cleanup", cleanup)
    argv = ["cleanup", "--branch", "ember/claude/run-000001"]
    if target is not None:
        argv.extend(["--merged-into", target])
    if claim is not None:
        argv.extend(["--claim", claim])
    if target is None and claim is None:
        with pytest.raises(SystemExit) as exit_info:
            main(argv, env={})
        assert exit_info.value.code == 64
        assert calls == []
        return
    assert main(argv, env={}) == 0
    [(passed_target, options)] = calls
    assert passed_target == target
    assert options["claim"] == claim
    assert options["branch"] == "ember/claude/run-000001"


def test_cleanup_help_names_claim(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["cleanup", "--help"])
    assert exit_info.value.code == 0
    assert "--claim ID" in capsys.readouterr().out


def test_choices_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["choices", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--expect-generation" in help_text
    assert "--skill X.Y.Z" in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["--nope"],
        ["nope"],
        [
            "merge",
            "ember/codex/test-run",
            "--into",
            "main",
            "--verdict",
            "keep",
            "--note",
            "Keep",
            "--subject",
            " ",
        ],
        [
            "merge",
            "ember/codex/test-run",
            "--into",
            "main",
            "--verdict",
            "keep",
            "--note",
            "Keep",
            "--subject",
            "feat: x\nbody",
        ],
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
        "claim": {"id": "run-000001", "status": "gone", "was": "claimed"},
        "generation": 3,
        "errors": [],
    }
    code, data, _ = _run(["--clear", "single.writer"], env, capsys)
    assert data["settings"] == {"shortlist.codex": ["gpt-5@high", "o3@low"]}
    code, data, _ = _run(["--clear-once"], env, capsys)
    assert data == {"cleared": "one-time", "generation": 4, "errors": []}


def test_empty_list_value(
    env: dict[str, str], terminal: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, data, _ = _run(["--set", "sandbox.read="], env, capsys)
    assert code == 0
    assert data["settings"] == {"sandbox.read": []}


@pytest.mark.parametrize("loaded", ["0.12.0", "0.13.0", "1.0.0"])
def test_claim_skill_version_reply_and_warning(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str], loaded: str
) -> None:
    installed = tmp_path / "installed-skill"
    installed.mkdir()
    (installed / "SKILL.md").write_text(
        "---\nname: ember\n---\n"
        "Skill version: 0.13.0 <!-- x-release-please-version -->\n"
    )
    link = skill.link_path(env)
    link.parent.mkdir(parents=True)
    link.symlink_to(installed)
    choices.set_once({"single.writer_as": "subagent"}, env=env)

    code, data, err = _run(
        ["--claim", "--id", "run-000001", "--skill", loaded], env, capsys
    )

    assert code == 0
    assert data["skill"] == ("current" if loaded == "0.13.0" else "stale")
    assert data["loaded_skill_version"] == loaded
    assert data["installed_skill_version"] == "0.13.0"
    assert data["claim"]["status"] == "claimed"
    assert err == (
        ""
        if loaded == "0.13.0"
        else (
            f"WARNING: the loaded ember skill is {loaded}; "
            "the installed skill is 0.13.0\n"
            "next: reload the ember skill before the next step\n"
        )
    )


def test_brief_claim_keeps_claim_skill_menu_and_engine_answers(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    installed = tmp_path / "installed-skill"
    installed.mkdir()
    (installed / "SKILL.md").write_text(
        "---\nname: ember\n---\n"
        "Skill version: 0.13.0 <!-- x-release-please-version -->\n"
    )
    link = skill.link_path(env)
    link.parent.mkdir(parents=True)
    link.symlink_to(installed)
    choices.set_once({"single.writer_as": "subagent"}, env=env)

    code, data, err = _run(
        ["--claim", "--id", "run-000001", "--skill", "0.12.0", "--brief"], env, capsys
    )

    assert code == 0
    assert "WARNING: the loaded ember skill is 0.12.0" in err
    full = choices.read("run-000001", env=env)
    assert data["claim"] == {"id": "run-000001", "status": "claimed"}
    assert data["skill"] == "stale"
    assert data["loaded_skill_version"] == "0.12.0"
    assert data["installed_skill_version"] == "0.13.0"
    assert data["menu"] == {key: full["menu"][key] for key in choices.BRIEF_MENU_KEYS}
    assert set(data) == {
        *full,
        "skill",
        "loaded_skill_version",
        "installed_skill_version",
    }
    assert set(data["answers"]) == {
        key for key in full["answers"] if key.startswith(("engine.", "shortlist."))
    }
    assert data["answers"]["engine.codex"] == full["answers"]["engine.codex"]


def test_brief_read_and_refusals(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, data, _ = _run(["--brief"], env, capsys)
    assert code == 0
    assert set(data["answers"]) == {
        *(f"engine.{name}" for name in choices.ENGINES),
        *(f"shortlist.{name}" for name in choices.ENGINES),
    }
    code, _, err = _run(["--finish", "run-000001", "--brief"], env, capsys)
    assert code == 64
    assert err == "emb choices: --brief cannot be used with --finish\n"


@pytest.mark.parametrize("version_line", [None, "", "Skill version: invalid\n"])
def test_claim_unknown_skill_warns_and_keeps_claim(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    version_line: str | None,
) -> None:
    if version_line is not None:
        installed = tmp_path / "installed-skill"
        installed.mkdir()
        (installed / "SKILL.md").write_text("---\nname: ember\n---\n" + version_line)
        link = skill.link_path(env)
        link.parent.mkdir(parents=True)
        link.symlink_to(installed)
    choices.set_once({"single.writer_as": "subagent"}, env=env)

    code, data, err = _run(
        ["--claim", "--id", "run-000001", "--skill", "0.12.0"], env, capsys
    )

    assert code == 0
    assert data["skill"] == "unknown"
    assert data["installed_skill_version"] is None
    assert data["claim"]["status"] == "claimed"
    assert choices.read("run-000001", env=env)["claim"]["status"] == "claimed"
    assert choices.snapshot(env=env)["once"] is None
    assert err.startswith("WARNING: the installed ember skill version is ")
    assert "owner:" in err
    assert "before starting a worker" in err


@pytest.mark.parametrize("loaded", ["", "0.12", "v0.12.0", "0.12.0\n"])
def test_claim_invalid_skill_exits_64_and_keeps_once(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], loaded: str
) -> None:
    choices.set_once({"single.writer_as": "subagent"}, env=env)
    before = choices.snapshot(env=env)
    code, data, err = _run(
        ["--claim", "--id", "run-000001", "--skill", loaded], env, capsys
    )
    assert code == 64
    assert data is None
    assert "--skill needs a version in x.y.z form" in err
    assert choices.snapshot(env=env) == before
    assert choices.read("run-000001", env=env)["claim"]["status"] == "unknown"


def test_claim_without_skill_has_no_skill_fields_or_warning(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, data, err = _run(["--claim", "--id", "run-000001"], env, capsys)
    assert code == 0
    assert err == ""
    assert "skill" not in data
    assert "loaded_skill_version" not in data
    assert "installed_skill_version" not in data


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
        ["--skill", "0.12.0"],
        ["--id", "run-000001", "--skill", "0.12.0"],
        ["--finish", "run-000001", "--skill", "0.12.0"],
        ["--once", "single.writer=ask", "--skill", "0.12.0"],
        ["--set", "app.theme=dark", "--skill", "0.12.0"],
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
    assert lines[3] == "  at least one integration connected in OpenCode (use /connect)"
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
        "format_version": models.CACHE_VERSION,
        "integrations": [{"id": "openrouter", "name": "OpenRouter", "login": "api"}],
        "models": [
            models.Model(
                "openrouter/openai/gpt-4o",
                "GPT-4o",
                ("low", "high"),
                None,
                128000,
                0.0000025,
                0.00001,
                True,
                "openrouter",
                "OpenRouter",
                "api",
            ).to_json()
        ],
    }
    status.save_cache_file(models.cache_path("opencode", env), payload, env)
    code, out, err = _engines(["--models", "opencode"], env, capsys)
    assert code == 0
    assert err == ""
    assert out.splitlines() == [
        "opencode models from cache, read 2 min ago",
        "openrouter/openai/gpt-4o  GPT-4o  efforts low, high  context 128000  "
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
        "integrations": [],
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
    assert lines[:10] == [
        "Codex: current, installed 1.0.0, newest 1.0.0",
        "OpenCode: missing",
        "  install: curl -fsSL https://opencode.ai/v2/install | bash",
        "Claude Code: missing",
        "  install: curl -fsSL https://claude.ai/install.sh | bash",
        "Claude Desktop: update available, installed 1.0, newest 1.1",
        "  update: sudo apt install --only-upgrade claude-desktop",
        "ember skill: missing",
        "  install: emb skill install",
        "tmux: update available, installed 1.0, newest 1.1",
    ]
    assert lines[-1].startswith("PySide6: ")
    assert "resolved path:" not in captured.out


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
    assert data["components"][0]["resolved_path"] == f"{env['PATH']}/codex"
    assert data["components"][0]["method"] is None
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
    for option in (
        "--engine",
        "--task-file",
        "--recover",
        "--answered",
        "--stall",
        "--trailer",
        "--dry-run",
    ):
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
        (["--close", "r", "--json"], "--json needs --status or --dry-run"),
        (["--status", "--dry-run"], "--dry-run cannot be used with --status"),
        (["--dry-run"], "a new run needs --engine, --dir, --task-file"),
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


@pytest.mark.parametrize(
    ("options", "model", "effort", "model_source", "effort_source"),
    [
        ([], "opus", "high", "single.writer", "single.writer"),
        (["--read-only"], "opus", "high", "single.reviewer", "single.reviewer"),
        (["--model", "opus"], "opus", "high", "--model", "single.writer"),
        (["--model", "sonnet"], "sonnet", "medium", "--model", "--model"),
        (["--effort", "low"], "opus", "low", "single.writer", "--effort"),
        (
            ["--model", "sonnet", "--effort", "low"],
            "sonnet",
            "low",
            "--model",
            "--effort",
        ),
    ],
)
def test_run_dry_run_text_and_json_sources(
    tmp_path: Path,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
    model: str,
    effort: str,
    model_source: str,
    effort_source: str,
) -> None:
    from ember import engines, paths

    stub = _stub_engine()
    driver = stub.StubDriver("claude", stub.script_path(run_env))
    monkeypatch.setattr(engines, "load_driver", lambda name, env=None: driver)
    repo = stub.make_repo(tmp_path, run_env)
    brief = tmp_path / "brief.md"
    brief.write_text("Task\n")
    argv = [
        "run", "--engine", "claude", "--dir", str(repo), "--task-file", str(brief),
        "--dry-run", *options,
    ]  # fmt: skip
    row = "Single tasks Reviewer" if "--read-only" in options else "Single tasks Writer"
    assert main([*argv, "--json"], env=run_env) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert json.loads(out) == {
        "screen": "Team",
        "row": row,
        "engine": "claude",
        "model": model,
        "effort": effort,
        "model_source": model_source,
        "effort_source": effort_source,
    }
    assert main(argv, env=run_env) == 0
    out, err = capsys.readouterr()
    source = (
        model_source
        if model_source == effort_source
        else f"model: {model_source}, effort: {effort_source}"
    )
    assert out == f"Team, {row}: claude {model} · {effort} ({source})\n"
    assert err == ""
    assert driver.commands == []
    assert not paths.runs_dir(run_env).exists()


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
    captured = capsys.readouterr()
    out = captured.out.splitlines()
    assert len(out) == 2
    run_id = out[0].removeprefix("run ")
    assert out[1].endswith(" · exit 0 · 1 files · turn 1")
    [next_line] = [
        line for line in captured.err.splitlines() if line.startswith("next:")
    ]
    assert "--expect-files 1, then emb merge " in next_line
    assert main(["run", "--status", run_id, "--json"], env=run_env) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["step"] == "merge"
    assert data["next"] == next_line
    assert captured.err == next_line + "\n"
    assert data["trailers"] == ["Co-Authored-By: X <x@y>"]
    assert data["session_state"] == "live"
    assert main(["run", "--status", run_id], env=run_env) == 0
    captured = capsys.readouterr()
    assert "step: merge\n" in captured.out
    assert "next:" not in captured.out
    assert captured.err == next_line + "\n"
    assert main(["run", "--status"], env=run_env) == 0
    captured = capsys.readouterr()
    assert "step:" not in captured.out and "next:" not in captured.err
    assert main(["run", "--close", run_id], env=run_env) == 0
    captured = capsys.readouterr()
    assert captured.out == f"closed {run_id}\n"
    assert captured.err == next_line + "\n"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("parent", [False, True])
def test_run_turn_commands_print_one_step(
    tmp_path: Path,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    force: bool,
    parent: bool,
) -> None:
    from ember import engines
    from ember.runs import core, record

    stub = _stub_engine()
    monkeypatch.setattr(core, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(
        engines,
        "load_driver",
        lambda name, env=None: stub.StubDriver(name, stub.script_path(run_env)),
    )
    repo = stub.make_repo(tmp_path, run_env)
    brief = tmp_path / "brief.md"
    brief.write_text("Task\n")
    assert (
        main(
            [
                "run",
                "--engine",
                "claude",
                "--dir",
                str(repo),
                "--task-file",
                str(brief),
                "--quiet",
            ],
            env=run_env,
        )
        == 0
    )
    captured = capsys.readouterr()
    run_id = captured.out.splitlines()[0].removeprefix("run ")
    line = f"next: emb run --close {run_id}"
    assert captured.err.splitlines()[-1] == line
    run = record.load(run_id, run_env)
    record.save(
        replace(
            run, claim="claim-a", parent=record.parent("gapcheck") if parent else None
        )
    )
    for argv in (
        ["--send", run_id, "--message-file", str(brief)],
        ["--recover", run_id],
    ):
        assert main(["run", *argv, "--quiet"], env=run_env) == 0
        captured = capsys.readouterr()
        assert [s for s in captured.err.splitlines() if s.startswith("next:")] == (
            [] if parent else [line]
        )
    assert (
        main(["run", "--close", run_id, *(["--force"] if force else [])], env=run_env)
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ("" if parent else "next: emb choices --finish claim-a\n")


def test_gapcheck_prints_no_run_step(
    tmp_path: Path,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ember import engines
    from ember.runs import core

    stub = _stub_engine()
    repo = stub.make_repo(tmp_path, run_env)
    plan = tmp_path / "plan.md"
    plan.write_text("## Failure model\nStop on failure.\n1. First\n2. Second\n")
    reply = tmp_path / "reply.json"
    reply.write_text('{"steps": [{"step": 1, "gaps": []}, {"step": 2, "gaps": []}]}')
    monkeypatch.setattr(core, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(
        engines,
        "load_driver",
        lambda name, env=None: stub.StubDriver(
            name, stub.script_path(run_env), reply_file=reply
        ),
    )
    assert (
        main(
            [
                "gapcheck",
                "--plan",
                str(plan),
                "--dir",
                str(repo),
                "--engine",
                "claude",
                "--model",
                "opus",
                "--json",
            ],
            env=run_env,
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["run"]["closed"] is True
    assert "next:" not in captured.err


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
    assert captured.err.splitlines()[-2] == (
        "owner: this Ember build has no driver for codex; update Ember"
    )


# emb gapcheck


def test_gapcheck_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["gapcheck", "--help"])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for option in (
        "--plan",
        "--dir",
        "--engine",
        "--model",
        "--effort",
        "--claim",
        "--read",
        "--window",
        "--stall",
        "--json",
        "--brief",
        "--reply",
        "--brief-file",
        "--previous",
        "--answer",
        "--id",
        "--text",
    ):
        assert option in output


def test_gapcheck_unknown_option_exits_64(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["gapcheck", "--plan", "plan.md", "--nope"])
    assert exit_info.value.code == 64
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments: --nope" in captured.err


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


def test_flow_help_and_order(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["flow", "--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "--lead FLOW TASK" in output and "--resume FLOW" in output
    assert "--cancel FLOW" in output and "--reason TEXT" in output
    assert "--previous FILE" in output
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert output.index("gapcheck") < output.index("flow") < output.index("ground")


def test_flow_unknown_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["flow", "--unknown"])
    assert result.value.code == 64
    assert "unrecognized arguments" in capsys.readouterr().err


def test_flow_dispatch(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["flow", "--status", "bad-id"], env=env) == 64
    assert "emb flow: invalid flow id" in capsys.readouterr().err


def test_panel_help_and_command_order(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["panel", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    for option in (
        "--question-file",
        "--dir",
        "--member",
        "--claim",
        "--read",
        "--stall",
        "--timeout",
        "--dry-run",
        "--status",
        "--recover",
        "--retry",
        "--close",
        "--json",
    ):
        assert option in help_text
    with pytest.raises(SystemExit):
        main(["--help"])
    help_text = capsys.readouterr().out
    assert (
        help_text.index("flow") < help_text.index("panel") < help_text.index("ground")
    )


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--status", "--member", "codex:x"],
        ["--recover", "x", "--dry-run"],
        ["--question-file", "x", "--dry-run", "--stall", "1"],
        ["--question-file", "x", "--status"],
    ],
)
def test_panel_usage_dispatch(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], options: list[str]
) -> None:
    assert main(["panel", *options], env=env) == 64
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.endswith("next: emb panel --help\n")


@pytest.mark.parametrize("option", ["--stall", "--timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
def test_panel_requires_positive_finite_minutes(
    capsys: pytest.CaptureFixture[str], option: str, value: str
) -> None:
    with pytest.raises(SystemExit) as result:
        main(["panel", "--recover", "x", option, value])
    assert result.value.code == 64
    assert "not a positive number" in capsys.readouterr().err


def test_panel_status_dispatch(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["panel", "--status", "--json"], env=env) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n" and not captured.err


@pytest.mark.parametrize(
    ("argv", "prog"),
    [
        (["panel", "--recover", "x", "--timeout", "0"], "emb panel"),
        (["flow", "--start", "--stall", "x"], "emb flow"),
        (["run", "--no-such-option"], "emb"),
        (["--no-such-option"], "emb"),
    ],
)
def test_parser_errors_end_with_a_next_line(
    argv: list[str], prog: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 64
    assert capsys.readouterr().err.splitlines()[-1] == f"next: {prog} --help"


@pytest.mark.parametrize("options", [["--retry", "bad", "1"], ["--close", "bad"]])
def test_panel_retry_close_dispatch(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], options: list[str]
) -> None:
    assert main(["panel", *options, "--json"], env=env) == 65
    captured = capsys.readouterr()
    assert not captured.out
    assert "invalid panel id: bad" in captured.err


@pytest.mark.parametrize(
    "options",
    [
        ["--retry", "x", "1", "--close", "x"],
        ["--retry", "x", "1", "--member", "codex:x"],
        ["--retry", "x", "1", "--dry-run"],
        ["--close", "x", "--timeout", "1"],
        ["--close", "x", "--stall", "1"],
        ["--close", "x", "--read", "."],
    ],
)
def test_panel_retry_close_usage(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], options: list[str]
) -> None:
    assert main(["panel", *options], env=env) == 64
    assert capsys.readouterr().err.endswith("next: emb panel --help\n")


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--older-than"],
        ["--older-than", "-1"],
        ["--older-than", "1.5"],
        ["--older-than", "word"],
        ["--older-than", "+1"],
    ],
)
def test_prune_invalid_age_exits_64(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["prune", *arguments])
    assert raised.value.code == 64
    assert "--older-than" in capsys.readouterr().err


def test_prune_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["prune", "--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert all(option in output for option in ("--older-than", "--dry-run", "--json"))


def test_prune_empty_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = {
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    assert main(["prune", "--older-than", "0", "--dry-run", "--json"], env=env) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "removed": [],
        "skipped": [],
        "branches": [],
        "worktrees": [],
    }
    assert captured.err == ""


def test_deps_unknown_install_text_and_json(
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    deps_fetch: _DepsFetch,
) -> None:
    _deps_stubs(env)
    _stub(env, "codex", "echo codex-cli 0.9.0\n")
    assert main(["deps"], env=env) == 0
    output = capsys.readouterr().out
    launcher = str(Path(env["PATH"]) / "codex")
    assert output.startswith("Codex: update available, installed 0.9.0, newest 1.0.0\n")
    assert f"  resolved path: {launcher}\n" in output
    assert f"  update: {launcher} update\n" in output
    assert "  Ember could not tell how the tool was installed.\n" in output
    assert main(["deps", "--json"], env=env) == 0
    item = json.loads(capsys.readouterr().out)["components"][0]
    assert item["resolved_path"] == launcher
    assert item["method"] is None
    assert item["command_kind"] == "update"
    assert item["note"] == "Ember could not tell how the tool was installed."


def test_deps_failed_check_hides_path(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], deps_fetch: _DepsFetch
) -> None:
    _deps_stubs(env)
    _stub(env, "codex", "echo unrecognised-version\n")
    assert main(["deps"], env=env) == 0
    output = capsys.readouterr().out
    assert output.startswith("Codex: check failed")
    assert "resolved path:" not in output
    assert "Ember could not tell" not in output
    assert main(["deps", "--json"], env=env) == 0
    item = json.loads(capsys.readouterr().out)["components"][0]
    assert item["resolved_path"] == f"{env['PATH']}/codex"
    assert item["method"] is None


@pytest.mark.parametrize(
    ("code", "variant"),
    [
        (69, None),
        (69, "tmux"),
        (73, "capacity"),
        (73, "outage"),
        (73, "usage_limit"),
        (73, "uncertain"),
        (74, "uncertain"),
        (74, None),
        (74, "retry"),
        (74, "complete"),
        (74, "starting"),
        (74, "send"),
        (76, None),
        (77, None),
        (77, "add"),
        (80, None),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_run_cli_reports_after_exit_line(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    variant: str | None,
    dry_run: bool,
) -> None:
    """Start failures print the report after the exit line, including dry runs."""
    from ember.runs import core, exits

    def fail(self: core.Core, options: core.StartOptions) -> int:
        """Fail the start before a run record exists."""
        raise core.RunError(
            code, "failed", variant, branch="worker", worktree="/worktree"
        )

    monkeypatch.setattr(core.Core, "start", fail)
    assert (
        main(
            [
                "run",
                "--engine",
                "claude",
                "--dir",
                ".",
                "--task-file",
                "brief",
                *(["--dry-run"] if dry_run else []),
            ],  # fmt: skip
            env=run_env,
        )
        == code
    )
    captured = capsys.readouterr()
    expected = [
        "emb run: failed",
        exits.line(code, variant, branch="worker", worktree="/worktree"),
    ]
    report = exits.report(code, variant, branch="worker", worktree="/worktree")
    if report:
        expected.append(report)
    assert captured.err.splitlines() == expected
    assert captured.out == ""


@pytest.mark.parametrize(
    "mode",
    ["attach", "screen", "close", "status", "send", "recover", "interrupt", "answered"],
)
def test_non_start_unavailable_does_not_report_no_worker(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    """A failing non-start command does not claim that no worker started."""
    from ember.runs import core, exits

    def fail(self: core.Core, *args: object, **kwargs: object) -> int:
        """Fail without a run context, as a missing dependency can."""
        raise core.RunError(exits.UNAVAILABLE, "tmux is missing", "tmux")

    monkeypatch.setattr(core.Core, mode, fail)
    args = ["run", f"--{mode}", "missing-run"]
    if mode == "send":
        args.extend(["--message-file", "message.md"])
    assert main(args, env=run_env) == exits.UNAVAILABLE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "emb run: tmux is missing",
        exits.line(exits.UNAVAILABLE, "tmux"),
    ]


@pytest.fixture
def finished_cli_run(
    tmp_path: Path,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> str:
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
    brief.write_text("Task\n")
    assert (
        main(
            [
                "run",
                "--engine",
                "claude",
                "--dir",
                str(repo),
                "--task-file",
                str(brief),
            ],
            env=run_env,
        )
        == 0
    )
    return capsys.readouterr().out.splitlines()[0].removeprefix("run ")


def test_closed_run_with_gone_claim_status_is_done(
    run_env: dict[str, str],
    finished_cli_run: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ember.runs import core, record

    run_id = finished_cli_run
    run = record.load(run_id, run_env)
    record.save(replace(run, claim="claim-a"))
    assert main(["run", "--close", run_id], env=run_env) == 0
    capsys.readouterr()
    assert main(["run", "--status", run_id, "--json"], env=run_env) == 0
    before = json.loads(capsys.readouterr().out)
    assert before["step"] == "finish"
    assert before["session_state"] == core.MISSING

    choices.finish("claim-a", env=run_env)
    assert main(["run", "--status", run_id], env=run_env) == 0
    captured = capsys.readouterr()
    assert "step: done\n" in captured.out
    assert f"session_state: {before['session_state']}\n" in captured.out
    assert "next:" not in captured.out
    assert captured.err == ""

    assert main(["run", "--status", run_id, "--json"], env=run_env) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["step"] == "done"
    assert document["next"] is None
    assert document["session_state"] == before["session_state"]
    assert captured.err == ""


@pytest.mark.parametrize("mode", ["start", "send", "recover", "recover-collected"])
@pytest.mark.parametrize("denial", ["none", "hidden", "protected"])
def test_finished_run_command_owner_wait_exit(
    tmp_path: Path,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    denial: str,
) -> None:
    from ember import engines, sandbox
    from ember.runs import core, exits, record

    stub = _stub_engine()
    monkeypatch.setattr(core, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        engines,
        "load_driver",
        lambda name, env=None: stub.StubDriver(name, stub.script_path(run_env)),
    )
    choices.set_settings({"sandbox.claude": "on"}, env=run_env)
    repo = stub.make_repo(tmp_path, run_env)
    folder = Path(run_env["HOME"]) / ".toolchain"
    folder.mkdir()
    denied_path = run_env["XDG_RUNTIME_DIR"] if denial == "protected" else str(folder)
    reply = "done" if denial == "none" else f"{denied_path}/python: Permission denied"
    brief = tmp_path / "brief.md"
    brief.write_text(f"Task\n@write result.txt done\n@reply {reply}\n")
    start = [
        "run", "--engine", "claude", "--dir", str(repo), "--task-file", str(brief),
    ]  # fmt: skip
    run_id = None
    if mode == "start":
        argv = start
    elif mode == "send":
        clean = tmp_path / "clean.md"
        clean.write_text("First turn\n")
        assert main([*start[:-1], str(clean)], env=run_env) == 0
        run_id = capsys.readouterr().out.splitlines()[0].removeprefix("run ")
        argv = ["run", "--send", run_id, "--message-file", str(brief)]
    else:
        run_core = core.Core(run_env)
        assert (
            run_core.execute(
                lambda: run_core.start(
                    core.StartOptions("claude", str(repo), str(brief))
                )
            )
            == 0
        )
        run_id = capsys.readouterr().out.splitlines()[0].removeprefix("run ")
        run = record.load(run_id, run_env)
        if mode == "recover-collected":
            outcome = record.load_outcome(run, run.turn)
            outcome["exit"] = None
            record.outcome_path(run, run.turn).write_text(json.dumps(outcome))
        argv = ["run", "--recover", run_id]
    assert main(argv, env=run_env) == (exits.OWNER_WAIT if denial == "hidden" else 0)
    captured = capsys.readouterr()
    if run_id is None:
        run_id = captured.out.splitlines()[0].removeprefix("run ")
    run = record.load(run_id, run_env)
    owners = [line for line in captured.err.splitlines() if line.startswith("owner:")]
    assert owners == list(run.owner_wait)
    assert bool(owners) == (denial == "hidden")
    assert run.last_exit == 0
    assert record.load_outcome(run, run.turn)["exit"] == 0
    if mode == "recover":
        assert "nothing to recover" in captured.err
    else:
        assert " · exit 0 · " in captured.out
    if denial == "hidden":
        assert "next:" not in captured.err
        # A no-op recovery repeats every pending line exactly once per call.
        extra = "owner: repair another pending action"
        record.update(run, {"owner_wait": (*run.owner_wait, extra), "owner_read": ()})
        assert main(["run", "--recover", run_id], env=run_env) == exits.OWNER_WAIT
        assert capsys.readouterr().err.splitlines()[1:] == [*owners, extra]


@pytest.mark.parametrize("code", [69, 71, 73, 74, 75, 76, 77, 80, 124])
@pytest.mark.parametrize("mode", ["start", "send", "recover"])
def test_run_failure_keeps_exit_with_pending_owner_wait(
    finished_cli_run: str,
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    code: int,
) -> None:
    from ember.runs import core, record

    run = record.load(finished_cli_run, run_env)
    record.update(run, {"owner_wait": ("owner: answer the denied path",)})

    def fail(self: core.Core, *args: object, **kwargs: object) -> int:
        self.last_run_id = run.id
        raise core.RunError(code, "command failed", run=run.id)

    monkeypatch.setattr(core.Core, mode, fail)
    if mode == "start":
        argv = ["--engine", "claude", "--dir", run.repo, "--task-file", "brief"]
    elif mode == "send":
        message = tmp_path / "message.md"
        message.write_text("Correction")
        argv = ["--send", run.id, "--message-file", str(message)]
    else:
        argv = ["--recover", run.id]
    assert main(["run", *argv], env=run_env) == code


def test_run_owner_status_and_answered(
    finished_cli_run: str,
    run_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ember.runs import record

    run = record.load(finished_cli_run, run_env)
    record.update(
        run,
        {
            "owner_wait": ("owner: install tmux",),
            "last_exit": 69,
            "last_variant": "tmux",
            "turn": 0,
            "status": record.FAILED,
        },
    )
    assert main(["run", "--status", run.id, "--json"], env=run_env) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["step"] == "owner" and data["next"] is None
    assert captured.err.splitlines() == [
        "owner: install tmux",
        "report: no worker started; do not retry the start or start another "
        "engine before the owner answers",
    ]
    assert main(["run", "--answered", run.id], env=run_env) == 0
    captured = capsys.readouterr()
    assert captured.err.startswith("next: emb run --engine")
    assert "owner:" not in captured.err
    answered = record.load(run.id, run_env)
    assert answered.owner_wait == () and answered.answered_at is not None
    assert main(["run", "--answered", run.id], env=run_env) == 0
    assert capsys.readouterr().err == captured.err
    assert record.load(run.id, run_env).answered_at == answered.answered_at


@pytest.mark.parametrize(
    ("code", "variant"),
    [
        (73, "capacity"),
        (73, "uncertain"),
        (74, "uncertain"),
        (74, "retry"),
        (74, "complete"),
        (77, "add"),
        (69, "fix"),
    ],
)
def test_failed_status_report_uses_saved_variant(
    finished_cli_run: str,
    run_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    code: int,
    variant: str,
) -> None:
    from ember.runs import exits, record

    run = record.load(finished_cli_run, run_env)
    record.update(
        run, {"last_exit": code, "last_variant": variant, "status": record.FAILED}
    )
    assert main(["run", "--status", run.id], env=run_env) == 0
    captured = capsys.readouterr()
    assert "step: failed" in captured.out
    report = exits.report(code, variant, **run.to_json())
    if report:
        assert captured.err.splitlines()[-1] == report
    else:
        assert "report:" not in captured.err
    assert captured.err.splitlines()[0] == exits.line(
        code, variant, run=run.id, worktree=run.worktree, engine=run.engine
    )


def test_answered_unknown_run(
    run_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--answered", "missing-run"], env=run_env) == 64
    assert (
        capsys.readouterr().err
        == "emb run: no run missing-run\nnext: emb run --status\n"
    )


@pytest.mark.parametrize("command", ["merge", "cleanup"])
@pytest.mark.parametrize("variant", [None, "conflict"])
def test_branch_cli_reports_only_conflict(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    variant: str | None,
) -> None:
    from ember.runs import exits

    def fail(*args: object, **kwargs: Any) -> int:  # noqa: ANN401
        error = branches.BranchError(
            75,
            "conflict",
            "owner: resolve the conflict on worker, or ask for a new run cut from "
            "the updated target",
            variant,
        )
        return branches._print_failure(command, error, sys.stderr, kwargs["failures"])

    monkeypatch.setattr(branches, command, fail)
    argv = (
        ["merge", "worker", "--into", "main", "--verdict", "keep", "--note", "Keep"]
        if command == "merge"
        else ["cleanup", "--merged-into", "main"]
    )
    assert main(argv, env=run_env) == 75
    captured = capsys.readouterr()
    assert "next:" not in captured.err
    if variant:
        assert captured.err.splitlines()[-1] == exits.report(75, "conflict")
    else:
        assert "report:" not in captured.err


@pytest.mark.parametrize("resolution", ["answered", "read"])
def test_successful_denial_waits_before_carry_on(
    finished_cli_run: str,
    run_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resolution: str,
) -> None:
    from ember import sandbox
    from ember.runs import exits, record

    previous = record.load(finished_cli_run, run_env)
    hidden = Path(run_env["HOME"]) / "private-toolchain"
    hidden.mkdir()
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=run_env)
    brief = tmp_path / "denied.md"
    brief.write_text(f"Task\n@reply Permission denied: '{hidden}/python'\n")
    assert (
        main(
            [
                "run",
                "--engine",
                "claude",
                "--dir",
                previous.repo,
                "--task-file",
                str(brief),
            ],
            env=run_env,
        )
        == exits.OWNER_WAIT
    )
    captured = capsys.readouterr()
    run_id = captured.out.splitlines()[0].removeprefix("run ")
    assert "next:" not in captured.err
    [owner] = [line for line in captured.err.splitlines() if line.startswith("owner:")]
    assert main(["run", "--status", run_id, "--json"], env=run_env) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["step"] == "owner"
    assert json.loads(captured.out)["next"] is None
    assert captured.err == owner + "\n"
    if resolution == "answered":
        assert main(["run", "--answered", run_id], env=run_env) == 0
    else:
        choices.set_settings({"sandbox.read": [str(hidden)]}, env=run_env)
        assert main(["run", "--status", run_id], env=run_env) == 0
    assert capsys.readouterr().err == f"next: emb run --close {run_id}\n"
    assert record.load(run_id, run_env).owner_wait == ()


def test_failed_launch_keeps_no_worker_report_in_status(
    finished_cli_run: str,
    run_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ember.runs import core, exits, record

    previous = record.load(finished_cli_run, run_env)

    def fail(
        self: core.Core, run: record.Run, *args: object, **kwargs: object
    ) -> tuple[record.Run, str | None]:
        record.update(run, {"turn": 1, "turn_state": record.SENT})
        raise core.RunError(69, "cannot launch", "runtime", run=run.id)

    monkeypatch.setattr(core.Core, "_launch", fail)
    brief = tmp_path / "launch.md"
    brief.write_text("Task\n")
    assert (
        main(
            [
                "run",
                "--engine",
                "claude",
                "--dir",
                previous.repo,
                "--task-file",
                str(brief),
            ],
            env=run_env,
        )
        == 69
    )
    captured = capsys.readouterr()
    run_id = captured.out.splitlines()[0].removeprefix("run ")
    assert captured.err.splitlines()[-1] == exits.report(69)
    assert record.load(run_id, run_env).last_start_failure
    assert main(["run", "--status", run_id, "--json"], env=run_env) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["step"] == "owner"
    assert captured.err.splitlines()[-1] == exits.report(69)
