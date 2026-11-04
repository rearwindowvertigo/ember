import json
import os
import stat
from pathlib import Path

import pytest

from ember import paths
from ember.engines import status

T0 = 1_800_000_000.0


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PATH": str(bin_dir),
    }


def _stub(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _bin(env: dict[str, str]) -> Path:
    return Path(env["PATH"])


CODEX_OK = """case "$1" in
--version) echo "codex-cli 1.2.3";;
login) echo "Logged in using ChatGPT";;
esac
"""

OPENCODE_OK = """case "$1" in
--version) echo "0.9.0";;
auth) echo "Credentials"; echo "OpenRouter api";;
esac
"""

CLAUDE_OK = """case "$1" in
--version) echo "2.1.0 (Claude Code)";;
auth) echo '{"loggedIn": true, "authMethod": "claude.ai"}';;
esac
"""


def _connect_all(env: dict[str, str]) -> None:
    _stub(_bin(env), "codex", CODEX_OK)
    _stub(_bin(env), "opencode", OPENCODE_OK)
    _stub(_bin(env), "claude", CLAUDE_OK)


# check


@pytest.mark.parametrize("engine", status.ENGINES)
def test_missing_cli_is_not_installed(engine: str, env: dict[str, str]) -> None:
    result = status.check(engine, env)
    assert result.state == status.NOT_INSTALLED
    assert result.installed is False
    assert result.binary is None
    assert result.version is None
    assert result.signed_in is None
    assert result.connected is False
    assert result.reason
    install, sign_in = result.requirements
    assert install.met is False
    assert install.fix == status.INSTALL[engine]
    assert sign_in.met is False
    assert sign_in.fix is None


def test_codex_connected(env: dict[str, str]) -> None:
    binary = _stub(_bin(env), "codex", CODEX_OK)
    result = status.check("codex", env)
    assert result.state == status.CONNECTED
    assert result.connected is True
    assert result.installed is True
    assert result.binary == str(binary)
    assert result.version == "codex-cli 1.2.3"
    assert result.signed_in is True
    assert result.reason is None
    assert all(item.met and item.fix is None for item in result.requirements)


def test_codex_not_logged_in(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "codex",
        'case "$1" in --version) echo "codex-cli 1";; '
        'login) echo "Not logged in"; exit 1;; esac\n',
    )
    result = status.check("codex", env)
    assert result.state == status.NOT_SIGNED_IN
    assert result.signed_in is False
    assert result.connected is False
    sign_in = result.requirements[1]
    assert sign_in.met is False
    assert sign_in.fix == "codex login"


def test_codex_api_key_sign_in_is_not_enough(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "codex",
        'case "$1" in --version) echo "codex-cli 1";; '
        'login) echo "Logged in using an API key";; esac\n',
    )
    result = status.check("codex", env)
    assert result.state == status.NOT_SIGNED_IN
    assert result.signed_in is False


def test_unexpected_sign_in_output_is_check_failed(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "codex",
        'case "$1" in --version) echo "codex-cli 1";; '
        'login) echo; echo "segfault in status"; exit 139;; esac\n',
    )
    result = status.check("codex", env)
    assert result.state == status.CHECK_FAILED
    assert result.signed_in is None
    assert result.connected is False
    assert result.reason is not None
    assert "segfault in status" in result.reason
    assert "139" in result.reason
    assert result.requirements[1].fix is None


def test_unexpected_format_with_exit_0_is_check_failed(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "codex",
        'case "$1" in --version) echo "codex-cli 1";; login) echo "hello";; esac\n',
    )
    result = status.check("codex", env)
    assert result.state == status.CHECK_FAILED
    assert result.reason is not None
    assert "hello" in result.reason


def test_failed_version_is_check_failed_but_keeps_sign_in(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "codex",
        'case "$1" in --version) echo "broken" >&2; exit 2;; '
        'login) echo "Logged in using ChatGPT";; esac\n',
    )
    result = status.check("codex", env)
    assert result.state == status.CHECK_FAILED
    assert result.version is None
    assert result.signed_in is True
    assert result.connected is False
    assert result.reason is not None
    assert "broken" in result.reason


def test_timeout_is_check_failed(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(status, "TIMEOUT", 0.5)
    _stub(_bin(env), "codex", "exec /bin/sleep 30\n")
    result = status.check("codex", env)
    assert result.state == status.CHECK_FAILED
    assert result.reason is not None
    assert "did not finish" in result.reason


def test_cli_that_cannot_run_is_check_failed(env: dict[str, str]) -> None:
    path = _bin(env) / "codex"
    path.write_text("#!/nonexistent/interpreter\n")
    path.chmod(0o755)
    result = status.check("codex", env)
    assert result.state == status.CHECK_FAILED
    assert result.reason is not None
    assert "could not run" in result.reason


def test_claude_connected_and_hidden_variables_removed(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "claude",
        'if [ -n "$ANTHROPIC_API_KEY$CLAUDECODE$CLAUDE_CODE_ENTRYPOINT" ]; then\n'
        '  echo "leaked"; exit 3\n'
        "fi\n" + CLAUDE_OK,
    )
    run_env = {
        **env,
        "ANTHROPIC_API_KEY": "x",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }
    result = status.check("claude", run_env)
    assert result.state == status.CONNECTED
    assert result.version == "2.1.0 (Claude Code)"


def test_claude_logged_out(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "claude",
        'case "$1" in --version) echo "2.1.0";; '
        "auth) echo '{\"loggedIn\": false}'; exit 1;; esac\n",
    )
    result = status.check("claude", env)
    assert result.state == status.NOT_SIGNED_IN
    assert result.requirements[1].fix == "claude auth login"


def test_claude_output_not_json_is_check_failed(env: dict[str, str]) -> None:
    _stub(
        _bin(env),
        "claude",
        'case "$1" in --version) echo "2.1.0";; auth) echo "Unknown command";; esac\n',
    )
    result = status.check("claude", env)
    assert result.state == status.CHECK_FAILED
    assert result.reason is not None
    assert "Unknown command" in result.reason


@pytest.mark.parametrize(
    ("version", "auth_args"),
    [
        ("0.9.0", "auth list"),
        ("1.19.5", "auth list"),
        ("opencode v2.2.5", "auth list --standalone"),
        ("2.2.5", "auth list --standalone"),
        ("opencode v2.invalid", "auth list"),
        ("unknown", "auth list"),
    ],
)
@pytest.mark.parametrize(
    ("output", "signed_in"),
    [
        ("Credentials\nOpenRouter api", True),
        ("Anthropic oauth", False),
        ("No authenticated integrations", False),
    ],
)
def test_opencode_sign_in_uses_version(
    env: dict[str, str],
    tmp_path: Path,
    version: str,
    auth_args: str,
    output: str,
    signed_in: bool,
) -> None:
    """Probe sign-in with the arguments selected by the single version read."""
    calls = tmp_path / "opencode-calls"
    _stub(
        _bin(env),
        "opencode",
        f'echo "$*" >> "{calls}"\n'
        f'case "$*" in\n--version) echo "{version}";;\n'
        f'{auth_args!r}) echo "{output}";;\n'
        '*) echo "unexpected arguments"; exit 9;;\nesac\n',
    )
    result = status.check("opencode", env)
    assert calls.read_text().splitlines() == ["--version", auth_args]
    assert result.version == version
    assert result.signed_in is signed_in
    assert result.connected is signed_in
    assert result.state == (status.CONNECTED if signed_in else status.NOT_SIGNED_IN)
    assert result.requirements[1].fix == (
        None if signed_in else "opencode auth login --provider openrouter"
    )


@pytest.mark.parametrize("version", ["1.19.5", "opencode v2.2.5"])
def test_opencode_failed_auth_is_check_failed(
    env: dict[str, str], version: str
) -> None:
    """Report an auth command failure for either OpenCode version."""
    _stub(
        _bin(env),
        "opencode",
        f'case "$1" in --version) echo "{version}";; '
        'auth) echo "store unavailable"; exit 3;; esac\n',
    )
    result = status.check("opencode", env)
    assert result.state == status.CHECK_FAILED
    assert result.signed_in is None
    assert result.reason == "opencode auth list exited 3: store unavailable"


def test_opencode_failed_version_uses_legacy_auth(env: dict[str, str]) -> None:
    """Use the OpenCode 1 probe when the version command fails."""
    _stub(
        _bin(env),
        "opencode",
        'case "$*" in --version) echo "broken"; exit 2;; '
        '"auth list") echo "OpenRouter api";; *) exit 9;; esac\n',
    )
    result = status.check("opencode", env)
    assert result.state == status.CHECK_FAILED
    assert result.version is None
    assert result.signed_in is True
    assert result.reason == "opencode --version exited 2: broken"


def test_opencode_found_in_its_install_folder(env: dict[str, str]) -> None:
    folder = Path(env["HOME"]) / ".opencode" / "bin"
    binary = _stub(folder, "opencode", OPENCODE_OK.replace("OpenRouter api", "none"))
    result = status.check("opencode", env)
    assert result.installed is True
    assert result.binary == str(binary)
    assert result.requirements[1].fix == f"{binary} auth login --provider openrouter"


def test_unknown_engine_raises(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="unknown engine"):
        status.check("gpt", env)


def test_check_all_checks_every_engine(env: dict[str, str]) -> None:
    _connect_all(env)
    result = status.check_all(env)
    assert list(result) == list(status.ENGINES)
    assert all(item.connected for item in result.values())


def test_json_round_trip(env: dict[str, str]) -> None:
    _stub(_bin(env), "codex", CODEX_OK)
    for item in status.check_all(env).values():
        data = json.loads(json.dumps(item.to_json()))
        assert status.from_json(data) == item


# read and the cache


def test_read_writes_private_cache(env: dict[str, str]) -> None:
    _connect_all(env)
    result = status.read(env, now=T0)
    assert all(item.checked_at == T0 for item in result.values())
    path = status.cache_path(env)
    assert path == paths.cache_dir(env) / "engines.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [entry.name for entry in path.parent.iterdir()] == ["engines.json"]
    assert status.cached(env) == result


def test_read_uses_cache_until_refresh(env: dict[str, str]) -> None:
    _connect_all(env)
    first = status.read(env, now=T0)
    (_bin(env) / "codex").unlink()
    assert status.read(env, now=T0 + 10**6) == first
    refreshed = status.read(env, refresh=True, now=T0 + 5)
    assert refreshed["codex"].state == status.NOT_INSTALLED
    assert refreshed["codex"].checked_at == T0 + 5
    assert status.cached(env) == refreshed


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{not json",
        "[]",
        '{"engines": {}}',
        '{"engines": {"codex": {"engine": "codex"}}}',
    ],
)
def test_invalid_cache_is_absent(env: dict[str, str], content: str) -> None:
    path = status.cache_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert status.cached(env) is None
    result = status.read(env, now=T0)
    assert result["codex"].state == status.NOT_INSTALLED
    assert status.cached(env) == result


def test_cache_with_engine_under_wrong_key_is_absent(env: dict[str, str]) -> None:
    status.read(env, now=T0)
    path = status.cache_path(env)
    data = json.loads(path.read_text())
    data["engines"]["codex"] = data["engines"]["claude"]
    path.write_text(json.dumps(data))
    assert status.cached(env) is None


def test_cache_that_cannot_be_written_is_ignored(env: dict[str, str]) -> None:
    cache_root = Path(env["XDG_CACHE_HOME"])
    cache_root.mkdir()
    (cache_root / "ember").write_text("a file, not a folder")
    result = status.read(env, now=T0)
    assert result["codex"].state == status.NOT_INSTALLED
    assert status.cached(env) is None


def test_save_cache_file_replaces_whole(env: dict[str, str]) -> None:
    path = paths.cache_dir(env) / "x.json"
    status.save_cache_file(path, {"a": 1}, env)
    status.save_cache_file(path, {"b": 2}, env)
    assert json.loads(path.read_text()) == {"b": 2}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["x.json"]
