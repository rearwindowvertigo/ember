import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ember import __version__, choices, paths
from ember.cli import main


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
