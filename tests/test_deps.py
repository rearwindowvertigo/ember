import json
import os
import re
import shlex
import stat
import time
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from ember import cli, deps, skill
from ember.engines import status

NOW = 1_800_000_000.0
HOUR = 3600.0


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


def _stub(env: dict[str, str], name: str, body: str) -> None:
    path = Path(env["PATH"]) / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class Fetcher:
    def __init__(self, replies: dict[str, str | Exception]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> str:
        assert timeout == deps.TIMEOUT
        self.calls.append(url)
        reply = self.replies[url]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _specs(text: str) -> tuple[deps.Spec, ...]:
    return deps.parse_components(text)


APT = """
[[components]]
id = "tmux"
label = "tmux"
group = "system"
installed = { kind = "apt", package = "tmux" }
newest = { kind = "apt", package = "tmux" }
update = "sudo apt install --only-upgrade tmux"
install = "sudo apt install tmux"
"""


def _apt_stubs(
    env: dict[str, str], installed: str, candidate: str, older: bool
) -> None:
    _stub(
        env,
        "dpkg-query",
        f'[ "$3" = tmux ] || exit 1\nprintf "installed\\t{installed}"\n',
    )
    _stub(
        env,
        "apt-cache",
        f'printf "tmux:\\n  Installed: {installed}\\n  Candidate: {candidate}\\n"\n',
    )
    _stub(
        env,
        "dpkg",
        f'echo "$@" >> "{env["PATH"]}/dpkg.log"\nexit {0 if older else 1}\n',
    )


def _read(
    env: dict[str, str],
    text: str,
    fetch: Fetcher | None = None,
    *,
    installed: bool = True,
    newest: str = "due",
    now: float = NOW,
    deadline: float = deps.TIMEOUT,
) -> deps.DepsReport:
    return deps.read(
        env,
        installed=installed,
        newest=newest,
        now=now,
        fetch=fetch or Fetcher({}),
        deadline=deadline,
        components=_specs(text),
    )


def _one(report: deps.DepsReport) -> deps.Component:
    (item,) = report.components
    return item


# The packaged list


def test_packaged_list_is_valid() -> None:
    specs = deps.load_components()
    assert [spec.id for spec in specs] == [
        "codex",
        "opencode",
        "claude",
        "claude-desktop",
        "ember-skill",
        "tmux",
        "bubblewrap",
        "git",
        "python3",
        "xdg-terminal-exec",
        "pyside6",
    ]
    for spec in specs:
        assert spec.group in deps.GROUPS


def test_packaged_engine_install_matches_status() -> None:
    specs = {spec.id: spec for spec in deps.load_components()}
    for engine in status.ENGINES:
        assert specs[engine].installed.engine == engine
        assert specs[engine].install == status.INSTALL[engine]


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_every_runtime_dependency_has_a_library_component() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    names = {
        _normalise(re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", item).group(0))  # type: ignore[union-attr]
        for item in project.get("dependencies", [])
    }
    libraries = {
        _normalise(spec.installed.package or "")
        for spec in deps.load_components()
        if spec.group == "libraries" and spec.installed.kind == "python"
    }
    assert names <= libraries


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('label = "tmux"', 'label = "tmux"\nextra = 1', "unknown field extra"),
        ('label = "tmux"\n', "", "missing field label"),
        (
            'newest = { kind = "apt", package = "tmux" }',
            'newest = { kind = "nope" }',
            "kind must be one of",
        ),
        ('group = "system"', 'group = "other"', "group must be one of"),
        ('id = "tmux"', 'id = "Tmux"', "must match"),
        ('package = "tmux" }\nnewest', 'package = "tmux", x = 1 }\nnewest', "unknown"),
        ('kind = "apt", package = "tmux" }\nnewest', 'kind = "npm" }\nnewest', "kind"),
        ('update = "sudo apt install --only-upgrade tmux"', "update = 3", "update"),
        ("[[components]]", "[[components]]\n[[", "not valid TOML"),
    ],
)
def test_schema_refusals(old: str, new: str, message: str) -> None:
    assert old in APT
    with pytest.raises(deps.ComponentsError, match=message):
        _specs(APT.replace(old, new, 1))


def test_schema_refuses_duplicate_id() -> None:
    with pytest.raises(deps.ComponentsError, match="lists tmux twice"):
        _specs(APT + APT)


def test_schema_refuses_empty_list_and_unknown_key() -> None:
    with pytest.raises(deps.ComponentsError, match=r"\[\[components\]\]"):
        _specs("")
    with pytest.raises(deps.ComponentsError, match="unknown key other"):
        _specs(APT + "\n[other]\n")


COMMAND = """
[[components]]
id = "codex"
label = "Codex"
group = "apps"
installed = {{ kind = "command", {how}pattern = 'codex-cli (\\S+)' }}
newest = {{ kind = "json", url = "https://example.test/codex", field = "version" }}
update = "codex update"
install = "npm install -g @openai/codex"
"""


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("", "exactly one of engine and argv"),
        ('engine = "codex", argv = ["codex"], ', "exactly one of engine and argv"),
        ('engine = "gpt", ', "unknown engine"),
        ("argv = [], ", "argv must be"),
    ],
)
def test_schema_command_source(how: str, message: str) -> None:
    with pytest.raises(deps.ComponentsError, match=message):
        _specs(COMMAND.format(how=how))


def test_schema_pattern_needs_a_group() -> None:
    text = COMMAND.format(how='engine = "codex", ').replace("(\\S+)", "\\S+")
    with pytest.raises(deps.ComponentsError, match="needs a group"):
        _specs(text)


def test_schema_apt_newest_needs_apt_installed() -> None:
    text = APT.replace(
        'installed = { kind = "apt", package = "tmux" }',
        'installed = { kind = "python", package = "tmux" }',
    )
    with pytest.raises(deps.ComponentsError, match="apt installed read"):
        _specs(text)


def test_read_refuses_unknown_newest_mode(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="newest"):
        _read(env, APT, newest="sometimes")


# apt


def test_apt_current(env: dict[str, str]) -> None:
    _apt_stubs(env, "3.6a-2", "3.6a-2", older=False)
    item = _one(_read(env, APT))
    assert item.status == deps.CURRENT
    assert (item.installed, item.newest, item.error) == ("3.6a-2", "3.6a-2", None)
    assert item.command is None and item.command_kind is None and item.note is None


def test_apt_update_available_with_epoch(env: dict[str, str]) -> None:
    _apt_stubs(env, "1:2.53.0-1ubuntu1.1", "1:2.53.0-1ubuntu1.2", older=True)
    item = _one(_read(env, APT))
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.installed == "1:2.53.0-1ubuntu1.1"
    assert item.newest == "1:2.53.0-1ubuntu1.2"
    assert item.command == "sudo apt install --only-upgrade tmux"
    assert item.command_kind == "update"
    log = (Path(env["PATH"]) / "dpkg.log").read_text()
    assert log == "--compare-versions 1:2.53.0-1ubuntu1.1 lt 1:2.53.0-1ubuntu1.2\n"


def test_apt_commands_run_with_c_locale_and_no_input(env: dict[str, str]) -> None:
    log = Path(env["PATH"]) / "env.log"
    _stub(
        env,
        "dpkg-query",
        f'echo "$LC_ALL" > "{log}"\nread line && echo input >> "{log}"\n'
        'printf "installed\\t1.0"\n',
    )
    _stub(env, "apt-cache", 'printf "  Candidate: 1.0\\n"\n')
    _stub(env, "dpkg", "exit 1\n")
    item = _one(_read(env, APT))
    assert item.status == deps.CURRENT
    assert log.read_text() == "C\n"


@pytest.mark.parametrize(
    "dpkg_query",
    ["exit 1\n", 'printf "config-files\\t3.6a-2"\n', 'printf "not-installed\\t"\n'],
)
def test_apt_missing(env: dict[str, str], dpkg_query: str) -> None:
    _stub(env, "dpkg-query", dpkg_query)
    item = _one(_read(env, APT))
    assert item.status == deps.MISSING
    assert item.installed is None and item.error is None
    assert item.command == "sudo apt install tmux"
    assert item.command_kind == "install"


def test_apt_without_candidate_fails_and_keeps_installed(env: dict[str, str]) -> None:
    _apt_stubs(env, "3.6a-2", "(none)", older=False)
    item = _one(_read(env, APT))
    assert item.status == deps.CHECK_FAILED
    assert item.installed == "3.6a-2"
    assert item.newest is None
    assert (
        item.error == "newest version: apt-cache policy tmux shows the candidate (none)"
    )
    assert item.command is None


def test_apt_unknown_to_apt_cache_fails(env: dict[str, str]) -> None:
    _apt_stubs(env, "3.6a-2", "x", older=False)
    _stub(env, "apt-cache", "exit 0\n")
    item = _one(_read(env, APT))
    assert item.error == "newest version: apt-cache policy tmux shows no candidate"


def test_apt_without_dpkg_tools_fails(env: dict[str, str]) -> None:
    item = _one(_read(env, APT))
    assert item.status == deps.CHECK_FAILED
    assert item.error == (
        "installed version: dpkg-query is not on PATH; "
        "newest version: apt-cache is not on PATH"
    )


def test_apt_comparison_failure(env: dict[str, str]) -> None:
    _apt_stubs(env, "3.6a-2", "!bad", older=False)
    _stub(env, "dpkg", "echo 'dpkg: error: bad syntax' >&2\nexit 2\n")
    item = _one(_read(env, APT))
    assert item.status == deps.CHECK_FAILED
    assert item.installed == "3.6a-2"
    assert item.newest == "!bad"
    assert item.error == (
        "comparison: dpkg --compare-versions exited 2: dpkg: error: bad syntax"
    )


def test_apt_dpkg_query_failure(env: dict[str, str]) -> None:
    _stub(env, "dpkg-query", "echo broken >&2\nexit 2\n")
    _stub(env, "apt-cache", 'printf "  Candidate: 1.0\\n"\n')
    item = _one(_read(env, APT))
    assert item.status == deps.CHECK_FAILED
    assert item.error == "installed version: dpkg-query exited 2: broken"
    assert item.newest == "1.0"


# Commands and the network


CODEX = COMMAND.format(how='engine = "codex", ')
CODEX_URL = "https://example.test/codex"


@pytest.mark.parametrize(
    ("installed", "newest", "state"),
    [
        ("0.164.0", "0.166.0", deps.UPDATE_AVAILABLE),
        ("0.164.0", "0.164.0", deps.CURRENT),
        ("0.166.0", "v0.166", deps.CURRENT),
        ("0.167.0", "0.166.0", deps.CURRENT),
    ],
)
def test_command_and_json(
    env: dict[str, str], installed: str, newest: str, state: str
) -> None:
    _stub(env, "codex", f'[ "$1" = --version ] && echo "codex-cli {installed}"\n')
    fetch = Fetcher({CODEX_URL: json.dumps({"version": newest})})
    item = _one(_read(env, CODEX, fetch))
    assert item.status == state
    assert (item.installed, item.newest) == (installed, newest)
    assert fetch.calls == [CODEX_URL]


def test_command_missing_reads_no_newest(env: dict[str, str]) -> None:
    fetch = Fetcher({})
    report = _read(env, CODEX, fetch)
    item = _one(report)
    assert item.status == deps.MISSING
    assert item.command == "npm install -g @openai/codex"
    assert fetch.calls == []
    assert report.counts() == 1
    assert report.newest_read_complete() is True
    assert report.oldest_newest_read() is None


def test_command_exit_failure(env: dict[str, str]) -> None:
    _stub(env, "codex", "echo 'no config' >&2\nexit 3\n")
    fetch = Fetcher({CODEX_URL: '{"version": "1.0"}'})
    item = _one(_read(env, CODEX, fetch))
    assert item.status == deps.CHECK_FAILED
    assert item.error == "installed version: codex --version exited 3: no config"
    assert item.newest == "1.0"


def test_command_output_without_version(env: dict[str, str]) -> None:
    _stub(env, "codex", "echo hello\n")
    fetch = Fetcher({CODEX_URL: '{"version": "1.0"}'})
    item = _one(_read(env, CODEX, fetch))
    assert item.error == (
        "installed version: codex --version printed 'hello', with no version"
    )


def test_unparsable_version_is_check_failed(env: dict[str, str]) -> None:
    _stub(env, "codex", "echo codex-cli 1.2.0-beta.1\n")
    fetch = Fetcher({CODEX_URL: '{"version": "1.3.0"}'})
    item = _one(_read(env, CODEX, fetch))
    assert item.status == deps.CHECK_FAILED
    assert item.installed == "1.2.0-beta.1"
    assert item.error == "comparison: '1.2.0-beta.1' is not a dotted version"


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (OSError("no network"), f"{CODEX_URL}: no network"),
        ("<html>", f"{CODEX_URL} did not return JSON"),
        ('{"name": "codex"}', f"{CODEX_URL} has no version"),
        ('{"version": 3}', f"{CODEX_URL}: version is not a version string"),
    ],
)
def test_json_read_failures(
    env: dict[str, str], reply: str | Exception, error: str
) -> None:
    _stub(env, "codex", "echo codex-cli 1.0.0\n")
    item = _one(_read(env, CODEX, Fetcher({CODEX_URL: reply})))
    assert item.status == deps.CHECK_FAILED
    assert item.installed == "1.0.0"
    assert item.error == f"newest version: {error}"


CLAUDE = """
[[components]]
id = "claude"
label = "Claude Code"
group = "apps"
installed = {{ kind = "command", engine = "claude", pattern = '{pattern}' }}
newest = {{ kind = "text", url = "https://example.test/claude" }}
update = "claude update"
""".format(pattern="^(\\S+) \\(Claude Code\\)$")


def test_engine_command_runs_without_session_variables(env: dict[str, str]) -> None:
    log = Path(env["PATH"]) / "claude.log"
    _stub(
        env,
        "claude",
        'printf "%s|%s|%s" "$LC_ALL" "$CLAUDE_CODE_ENTRYPOINT" '
        f'"$ANTHROPIC_API_KEY" > "{log}"\n'
        'echo "2.1.316 (Claude Code)"\n',
    )
    env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    env["ANTHROPIC_API_KEY"] = "x"
    fetch = Fetcher({"https://example.test/claude": "2.1.317\n"})
    item = _one(_read(env, CLAUDE, fetch))
    assert item.status == deps.UPDATE_AVAILABLE
    assert (item.installed, item.newest) == ("2.1.316", "2.1.317")
    assert log.read_text() == "C||"


def test_text_read_with_more_than_a_version_fails(env: dict[str, str]) -> None:
    _stub(env, "claude", 'echo "2.1.316 (Claude Code)"\n')
    fetch = Fetcher({"https://example.test/claude": "<html> page </html>"})
    item = _one(_read(env, CLAUDE, fetch))
    assert item.error == (
        "newest version: https://example.test/claude did not return a single version"
    )


ARGV = """
[[components]]
id = "tool"
label = "Tool"
group = "system"
installed = { kind = "command", argv = ["tool", "-V"], pattern = 'tool (\\S+)' }
newest = { kind = "text", url = "https://example.test/tool" }
"""


def test_argv_command(env: dict[str, str]) -> None:
    _stub(env, "tool", '[ "$1" = -V ] && echo "tool 2.0"\n')
    fetch = Fetcher({"https://example.test/tool": "2.1"})
    item = _one(_read(env, ARGV, fetch))
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.command is None and item.command_kind is None and item.note is None


def test_argv_command_missing_without_install(env: dict[str, str]) -> None:
    item = _one(_read(env, ARGV))
    assert item.status == deps.MISSING
    assert item.command is None and item.command_kind is None and item.note is None


def test_command_past_deadline_is_killed_with_its_group(
    env: dict[str, str], tmp_path: Path
) -> None:
    pid_file = tmp_path / "child.pid"
    _stub(env, "codex", f'/bin/sleep 30 &\necho $! > "{pid_file}"\nwait\n')
    fetch = Fetcher({CODEX_URL: '{"version": "1.0"}'})
    started = time.monotonic()
    item = _one(_read(env, CODEX, fetch, deadline=0.2))
    assert time.monotonic() - started < 5
    assert item.status == deps.CHECK_FAILED
    assert item.error == (
        "installed version: codex --version did not finish within 0.2 seconds"
    )
    pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except FileNotFoundError, ProcessLookupError:
            # The process ended before or while its stat file was read.
            break
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("the command's child process is still running")


PYTHON = """
[[components]]
id = "lib"
label = "Lib"
group = "libraries"
installed = {{ kind = "python", package = "{package}" }}
newest = {{ kind = "pypi", package = "{package}" }}
update_note = "Updates with the next Ember release."
install_note = "Reinstall Ember."
"""


def test_python_update_available_shows_update_note(env: dict[str, str]) -> None:
    from importlib.metadata import version

    installed = version("pytest")
    url = "https://pypi.org/pypi/pytest/json"
    fetch = Fetcher({url: json.dumps({"info": {"version": "999.0"}})})
    item = _one(_read(env, PYTHON.format(package="pytest"), fetch))
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.installed == installed
    assert item.command is None and item.command_kind is None
    assert item.note == "Updates with the next Ember release."


def test_python_current(env: dict[str, str]) -> None:
    from importlib.metadata import version

    installed = version("pytest")
    url = "https://pypi.org/pypi/pytest/json"
    fetch = Fetcher({url: json.dumps({"info": {"version": installed}})})
    item = _one(_read(env, PYTHON.format(package="pytest"), fetch))
    assert item.status == deps.CURRENT
    assert item.note is None


def test_python_missing_shows_install_note(env: dict[str, str]) -> None:
    item = _one(_read(env, PYTHON.format(package="no-such-package-for-ember")))
    assert item.status == deps.MISSING
    assert item.note == "Reinstall Ember."
    assert item.command is None


def test_python_pypi_failure(env: dict[str, str]) -> None:
    url = "https://pypi.org/pypi/pytest/json"
    fetch = Fetcher({url: TimeoutError("timed out")})
    item = _one(_read(env, PYTHON.format(package="pytest"), fetch))
    assert item.status == deps.CHECK_FAILED
    assert item.error == f"newest version: {url}: timed out"
    assert item.note is None


# Cache


def _cache(env: dict[str, str]) -> dict[str, object]:
    return json.loads(deps.cache_path(env).read_text())


def test_cache_is_private_and_holds_reads(env: dict[str, str]) -> None:
    _apt_stubs(env, "3.6a-2", "3.6a-2", older=False)
    report = _read(env, APT)
    path = deps.cache_path(env)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert _cache(env) == {
        "components": {
            "tmux": {
                "installed": {
                    "version": "3.6a-2",
                    "missing": False,
                    "error": None,
                    "read_at": NOW,
                },
                "newest": {"version": "3.6a-2", "read_at": NOW},
            }
        }
    }
    assert report.installed_read_at == {"tmux": NOW}
    assert report.newest_read_at == {"tmux": NOW}
    assert report.oldest_newest_read() == NOW
    assert report.newest_read_complete() is True


def _codex_setup(env: dict[str, str]) -> Fetcher:
    _stub(env, "codex", "echo codex-cli 1.0.0\n")
    return Fetcher({CODEX_URL: '{"version": "1.1.0"}'})


def test_newest_not_read_again_before_six_hours(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    _read(env, CODEX, fetch)
    report = _read(env, CODEX, fetch, now=NOW + 6 * HOUR - 1)
    assert fetch.calls == [CODEX_URL]
    item = _one(report)
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.newest == "1.1.0"
    assert report.newest_read_at == {"codex": NOW}
    assert report.installed_read_at == {"codex": NOW + 6 * HOUR - 1}


def test_newest_read_again_at_six_hours(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    _read(env, CODEX, fetch)
    report = _read(env, CODEX, fetch, now=NOW + 6 * HOUR)
    assert fetch.calls == [CODEX_URL, CODEX_URL]
    assert report.newest_read_at == {"codex": NOW + 6 * HOUR}


def test_newest_always_and_never(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    _read(env, CODEX, fetch)
    _read(env, CODEX, fetch, now=NOW + 1, newest="always")
    assert len(fetch.calls) == 2
    report = _read(env, CODEX, fetch, now=NOW + 7 * HOUR, newest="never")
    assert len(fetch.calls) == 2
    assert _one(report).newest == "1.1.0"
    assert report.newest_read_at == {"codex": NOW + 1}


def test_newest_never_without_cache_is_check_failed(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    report = _read(env, CODEX, fetch, newest="never")
    item = _one(report)
    assert fetch.calls == []
    assert item.status == deps.CHECK_FAILED
    assert item.installed == "1.0.0"
    assert item.error == "newest version: not read and nothing cached"
    assert report.newest_read_complete() is False


def test_installed_from_cache(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    report = _read(env, CODEX, fetch, installed=False, newest="never")
    assert _one(report).error == (
        "installed version: not read and nothing cached; "
        "newest version: not read and nothing cached"
    )
    _read(env, CODEX, fetch)
    _stub(env, "codex", "exit 1\n")
    item = _one(_read(env, CODEX, fetch, installed=False, now=NOW + 1))
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.installed == "1.0.0"
    (Path(env["PATH"]) / "codex").unlink()
    item = _one(_read(env, CODEX, fetch, now=NOW + 2))
    assert item.status == deps.MISSING


def test_failed_newest_read_is_not_cached(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    _read(env, CODEX, fetch)
    fetch.replies[CODEX_URL] = OSError("no network")
    later = NOW + 7 * HOUR
    report = _read(env, CODEX, fetch, now=later)
    item = _one(report)
    assert item.status == deps.CHECK_FAILED
    assert item.installed == "1.0.0"
    assert item.newest is None
    assert report.newest_read_at == {"codex": None}
    held = _cache(env)["components"]["codex"]  # type: ignore[index]
    assert held["newest"] == {"version": "1.1.0", "read_at": NOW}
    fetch.replies[CODEX_URL] = '{"version": "1.2.0"}'
    report = _read(env, CODEX, fetch, now=later + 1)
    assert len(fetch.calls) == 3
    assert _one(report).newest == "1.2.0"


def test_failed_first_newest_read_is_tried_again(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    fetch.replies[CODEX_URL] = OSError("no network")
    _read(env, CODEX, fetch)
    assert "newest" not in _cache(env)["components"]["codex"]  # type: ignore[index]
    fetch.replies[CODEX_URL] = '{"version": "1.0.0"}'
    item = _one(_read(env, CODEX, fetch, now=NOW + 1))
    assert len(fetch.calls) == 2
    assert item.status == deps.CURRENT


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"components": []}',
        '{"components": {"codex": {"newest": {"version": 1, "read_at": "x"}}}}',
    ],
)
def test_corrupt_cache_is_ignored(env: dict[str, str], content: str) -> None:
    fetch = _codex_setup(env)
    path = deps.cache_path(env)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(content)
    item = _one(_read(env, CODEX, fetch))
    assert fetch.calls == [CODEX_URL]
    assert item.status == deps.UPDATE_AVAILABLE
    assert _cache(env)["components"]["codex"]["newest"]["version"] == "1.1.0"  # type: ignore[index]


def test_unwritable_cache_is_ignored(env: dict[str, str], tmp_path: Path) -> None:
    fetch = _codex_setup(env)
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    env["XDG_CACHE_HOME"] = str(blocker)
    item = _one(_read(env, CODEX, fetch))
    assert item.status == deps.UPDATE_AVAILABLE


def test_cache_drops_components_no_longer_listed(env: dict[str, str]) -> None:
    fetch = _codex_setup(env)
    _read(env, CODEX, fetch)
    _apt_stubs(env, "1.0", "1.0", older=False)
    _read(env, APT, fetch)
    assert list(_cache(env)["components"]) == ["tmux"]  # type: ignore[arg-type]


# The report


def test_report_order_counts_and_json(env: dict[str, str]) -> None:
    _apt_stubs(env, "1.0", "1.1", older=True)
    text = APT + PYTHON.format(package="no-such-package-for-ember") + CODEX
    fetch = Fetcher({CODEX_URL: OSError("no network")})
    report = _read(env, text, fetch)
    assert [item.id for item in report.components] == ["tmux", "lib", "codex"]
    assert [item.status for item in report.components] == [
        deps.UPDATE_AVAILABLE,
        deps.MISSING,
        deps.MISSING,
    ]
    assert report.counts() == 3
    data = report.to_json()
    assert data["counts"] == 3
    assert data["components"][1] == {
        "id": "lib",
        "label": "Lib",
        "group": "libraries",
        "status": "missing",
        "installed": None,
        "newest": None,
        "error": None,
        "command": None,
        "command_kind": None,
        "note": "Reinstall Ember.",
        "resolved_path": None,
        "method": None,
    }
    assert data["installed_read_at"] == {"tmux": NOW, "lib": NOW, "codex": NOW}
    assert data["newest_read_at"] == {"tmux": NOW, "lib": None, "codex": None}
    assert data["newest_read_oldest"] == NOW
    assert data["newest_read_complete"] is True
    json.dumps(data)


def test_partial_newest_coverage(env: dict[str, str]) -> None:
    _apt_stubs(env, "1.0", "1.0", older=False)
    _stub(env, "codex", "echo codex-cli 1.0.0\n")
    fetch = Fetcher({CODEX_URL: OSError("no network")})
    report = _read(env, APT + CODEX, fetch)
    assert report.counts() == 0
    assert report.oldest_newest_read() == NOW
    assert report.newest_read_complete() is False


def test_dotted() -> None:
    assert deps.dotted("v1.2.0") == deps.dotted("1.2") == (1, 2)
    assert deps.dotted("0") == (0,)
    with pytest.raises(deps.ReadError):
        deps.dotted("1.2a")


# Install methods


def _engine_spec(engine: str = "codex") -> deps.Spec:
    return next(spec for spec in deps.load_components() if spec.id == engine)


def _engine_read(
    env: dict[str, str],
    spec: deps.Spec,
    *,
    installed: bool = True,
    newest: str = "due",
    version: str = "2.0.0",
    deadline: float = deps.TIMEOUT,
) -> deps.Component:
    body = version if spec.newest.kind == "text" else json.dumps({"version": version})
    return _one(
        deps.read(
            env,
            components=(spec,),
            fetch=Fetcher({spec.newest.url or "": body}),
            installed=installed,
            newest=newest,
            deadline=deadline,
        )
    )


def _launcher(env: dict[str, str], engine: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    versions = {
        "codex": "codex-cli 1.0.0",
        "opencode": "1.0.0",
        "claude": "1.0.0 (Claude Code)",
    }
    target.write_text(
        "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(versions[engine]) + "\n"
    )
    target.chmod(0o755)
    launcher = Path(env["PATH"]) / engine
    launcher.symlink_to(target)
    return launcher


@pytest.mark.parametrize("engine", ["codex", "opencode", "claude"])
@pytest.mark.parametrize("manager", ["npm", "bun", "brew"])
@pytest.mark.parametrize("writable", [True, False])
def test_install_method_layouts(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    manager: str,
    writable: bool,
) -> None:
    prefix = tmp_path / "prefix with ' spaces"
    package = {
        "codex": "@openai/codex",
        "opencode": "opencode-ai",
        "claude": "@anthropic-ai/claude-code",
    }[engine]
    layouts = {
        "npm": (
            f"lib/node_modules/{package}/bin/cli.js",
            ["npm", "install", "-g", "--prefix", str(prefix), package + "@latest"],
        ),
        "bun": (
            f"install/global/node_modules/{package}/bin/cli.js",
            ["bun", "add", "-g", package + "@latest"],
        ),
        "brew": (f"Cellar/{engine}/1.0.0/bin/{engine}", ["brew", "upgrade", engine]),
    }
    layout, expected = layouts[manager]
    target = prefix / layout
    _launcher(env, engine, target)
    checked = []
    access = os.access

    def check_access(path: str | Path, mode: int) -> bool:
        if mode == os.W_OK:
            checked.append(Path(path))
            return writable
        return access(path, mode)

    monkeypatch.setattr(deps.os, "access", check_access)
    item = _engine_read(env, _engine_spec(engine))
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.command_kind == "update"
    assert shlex.split(item.command or "") == ([] if writable else ["sudo"]) + expected
    assert checked == [
        prefix / "lib/node_modules" if manager == "npm" else target.parent
    ]
    assert item.resolved_path == str(target)
    assert item.method is not None and item.method.path is not None
    assert item.note is None
    assert item.to_json()["method"] == item.method.to_json()
    assert item.to_json()["resolved_path"] == str(target)


@pytest.mark.parametrize("engine", ["claude", "opencode"])
@pytest.mark.parametrize("writable", [True, False])
def test_vendor_launcher(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, engine: str, writable: bool
) -> None:
    home = Path(env["HOME"])
    target = home / (
        ".local/share/claude/versions/1.0.0"
        if engine == "claude"
        else ".opencode/bin/opencode"
    )
    launcher = _launcher(env, engine, target)
    if engine == "opencode":
        launcher.unlink()
        launcher = target  # The known location outside PATH.
    access = os.access
    monkeypatch.setattr(
        deps.os,
        "access",
        lambda path, mode: writable if mode == os.W_OK else access(path, mode),
    )
    item = _engine_read(env, _engine_spec(engine))
    assert shlex.split(item.command or "") == ([] if writable else ["sudo"]) + [
        str(launcher),
        "update" if engine == "claude" else "upgrade",
    ]
    assert item.resolved_path == str(target)
    assert item.method is not None
    assert item.note is None


@pytest.mark.parametrize("engine", ["codex", "opencode", "claude"])
def test_dpkg_method(env: dict[str, str], tmp_path: Path, engine: str) -> None:
    target = tmp_path / "usr/bin" / engine
    _launcher(env, engine, target)
    _stub(env, "dpkg-query", 'printf "%s\\n" "engine-package:amd64: $2"\n')
    item = _engine_read(env, _engine_spec(engine))
    assert item.command == "sudo apt install --only-upgrade engine-package:amd64"
    assert item.method is not None and item.method.kind == "dpkg"
    assert item.note is None


@pytest.mark.parametrize(
    "reply",
    [
        "exit 1\n",
        "exit 2\n",
        "echo malformed\n",
        'printf "one: %s\\ntwo: %s\\n" "$2" "$2"\n',
        'echo "one, two: $2"\n',
        'echo "one: /some/other/file"\n',
        "/bin/sleep 1\n",
    ],
)
def test_dpkg_failure_uses_launcher(
    env: dict[str, str], tmp_path: Path, reply: str
) -> None:
    target = tmp_path / "unknown/real-codex"
    launcher = _launcher(env, "codex", target)
    _stub(env, "dpkg-query", reply)
    item = _engine_read(env, _engine_spec(), deadline=0.2)
    assert item.command == f"{launcher} update"
    assert item.command_kind == "update"
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.method is None
    assert item.resolved_path == str(target)
    assert item.note == "Ember could not tell how the tool was installed."


@pytest.mark.parametrize("version", ["1.0.0", "2.0.0"])
def test_unknown_install_note_only_with_update(
    env: dict[str, str], tmp_path: Path, version: str
) -> None:
    target = tmp_path / "unknown/real-codex"
    _launcher(env, "codex", target)
    item = _engine_read(env, _engine_spec(), version=version)
    assert item.method is None
    assert item.resolved_path == str(target)
    assert (item.note is not None) == (version == "2.0.0")
    assert (item.command is not None) == (version == "2.0.0")


def test_methods_are_resolved_on_cached_reads(
    env: dict[str, str], tmp_path: Path
) -> None:
    spec = _engine_spec()
    first = tmp_path / "npm/lib/node_modules/@openai/codex/bin/codex.js"
    launcher = _launcher(env, "codex", first)
    assert _engine_read(env, spec).method == spec.methods[0]
    launcher.unlink()
    second = tmp_path / "bun/install/global/node_modules/@openai/codex/bin/codex.js"
    _launcher(env, "codex", second)
    item = _engine_read(env, spec, installed=False, newest="never")
    assert item.method == spec.methods[2]
    assert item.resolved_path == str(second)
    assert item.command == "bun add -g @openai/codex@latest"
    cached = json.dumps(_cache(env))
    assert "resolved_path" not in cached and "method" not in cached
    launcher.unlink()
    item = _engine_read(env, spec, installed=False, newest="never")
    assert item.status == deps.MISSING
    assert item.resolved_path is None and item.method is None
    assert item.command == spec.install
    assert item.command_kind == "install" and item.note is None


def test_dpkg_skipped_on_cached_read(env: dict[str, str], tmp_path: Path) -> None:
    _launcher(env, "codex", tmp_path / "usr/bin/codex")
    log = tmp_path / "dpkg-calls"
    _stub(
        env,
        "dpkg-query",
        f'echo called >> {shlex.quote(str(log))}\nprintf "%s\\n" "codex: $2"\n',
    )
    spec = _engine_spec()
    assert _engine_read(env, spec).method is not None
    item = _engine_read(env, spec, installed=False, newest="never")
    assert log.read_text() == "called\n"
    assert item.method is None and item.note is not None


def test_path_methods_precede_dpkg_and_keep_order(
    env: dict[str, str], tmp_path: Path
) -> None:
    spec = _engine_spec()
    target = tmp_path / "lib/node_modules/@openai/codex/bin/codex.js"
    _launcher(env, "codex", target)
    log = tmp_path / "dpkg-calls"
    _stub(env, "dpkg-query", f"echo called >> {shlex.quote(str(log))}\n")
    spec = replace(
        spec,
        methods=(
            spec.methods[-1],
            *spec.methods[:-1],
            deps.Method(path="{prefix}/bin/*", update="other"),
        ),
    )
    item = _engine_read(env, spec)
    assert item.method == spec.methods[1]
    assert not log.exists()


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        (
            "{prefix}/lib/node_modules/{package}",
            "/usr/lib/node_modules/@openai/codex/bin/codex.js",
            {"prefix": "/usr", "package": "@openai/codex"},
        ),
        (
            "{prefix}/lib/node_modules/{package}",
            "/usr/lib/node_modules/codex",
            {"prefix": "/usr", "package": "codex"},
        ),
        ("{prefix}/lib/node_modules/{package}", "/usr/lib/node_modules/@openai", None),
        ("/opt/bin/codex", "/opt/bin/codex-extra", None),
        ("/opt/bin/codex", "/else/opt/bin/codex", None),
        ("/opt/*/codex", "/opt/a/b/codex", None),
        (
            "{prefix}/Cellar/{formula}",
            "/opt/Cellar/codex/1/bin/codex",
            {"prefix": "/opt", "formula": "codex"},
        ),
    ],
)
def test_method_pattern_boundaries(
    pattern: str, path: str, expected: dict[str, str] | None
) -> None:
    match = deps._path_pattern(pattern).match(path)
    assert (None if match is None else match.groupdict()) == expected


@pytest.mark.parametrize(
    "method",
    [
        'methods = "bad"',
        "methods = [3]",
        'methods = [{kind = "other", update = "cmd"}]',
        'methods = [{kind = "dpkg", path = "/bin/codex", update = "cmd"}]',
        'methods = [{path = "/bin/codex"}]',
        'methods = [{path = "/bin/{unknown}", update = "cmd"}]',
        'methods = [{path = "/bin/{package}", update = "cmd {prefix}"}]',
        'methods = [{path = "/bin/{package}", update = "cmd {package!r}"}]',
    ],
)
def test_invalid_method_schema(method: str) -> None:
    text = CODEX.replace(
        'update = "codex update"', 'updater = "codex update"\n' + method
    )
    with pytest.raises(deps.ComponentsError):
        deps.parse_components(text)


def test_dpkg_does_not_duplicate_sudo(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launcher(env, "codex", tmp_path / "usr/bin/codex")
    _stub(env, "dpkg-query", 'printf "%s\\n" "codex: $2"\n')
    access = os.access
    monkeypatch.setattr(
        deps.os,
        "access",
        lambda path, mode: False if mode == os.W_OK else access(path, mode),
    )
    item = _engine_read(env, _engine_spec())
    assert item.command == "sudo apt install --only-upgrade codex"


def test_fallback_quotes_launcher_and_checks_resolved_parent(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "owner's tools"
    bin_dir.mkdir()
    env["PATH"] = str(bin_dir)
    target = tmp_path / "unknown/codex"
    launcher = _launcher(env, "codex", target)
    shim = tmp_path / "shim"
    shim.symlink_to(target)
    launcher.unlink()
    launcher.symlink_to(shim)
    checked = []
    access = os.access

    def check_access(path: str | Path, mode: int) -> bool:
        if mode == os.W_OK:
            checked.append(Path(path))
            return False
        return access(path, mode)

    monkeypatch.setattr(deps.os, "access", check_access)
    item = _engine_read(env, _engine_spec())
    assert shlex.split(item.command or "") == ["sudo", str(launcher), "update"]
    assert item.resolved_path == str(target)
    assert checked == [target.parent]


def test_missing_engine_has_no_method_or_resolved_path(env: dict[str, str]) -> None:
    item = _engine_read(env, _engine_spec())
    assert item.status == deps.MISSING
    assert item.to_json()["resolved_path"] is None
    assert item.to_json()["method"] is None
    assert item.command == "npm install -g @openai/codex"
    assert item.command_kind == "install" and item.note is None


def test_failed_version_check_has_no_install_method_note(
    env: dict[str, str], tmp_path: Path
) -> None:
    _launcher(env, "codex", tmp_path / "unknown/codex")
    item = _engine_read(env, _engine_spec(), version="not-a-version")
    assert item.status == deps.CHECK_FAILED
    assert item.note is None and item.command is None
    assert item.resolved_path is not None


@pytest.mark.parametrize("engine", ["codex", "opencode", "claude"])
@pytest.mark.parametrize("writable", [True, False])
def test_pnpm_shell_shim(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    writable: bool,
) -> None:
    pnpm_home = Path(env["HOME"]) / ".local/share/pnpm"
    pnpm_home.mkdir(parents=True)
    env["PATH"] = str(pnpm_home)
    package = {
        "codex": "@openai/codex",
        "opencode": "opencode-ai",
        "claude": "@anthropic-ai/claude-code",
    }[engine]
    relative = f"global/5/node_modules/{package}/bin/{engine}.js"
    modules = pnpm_home / "global/5/node_modules"
    stored_package = (
        modules / ".pnpm" / f"{package.replace('/', '+')}@1.0.0/node_modules" / package
    )
    target = stored_package / f"bin/{engine}.js"
    launcher = _launcher(env, engine, target)
    package_link = modules / package
    package_link.parent.mkdir(parents=True, exist_ok=True)
    package_link.symlink_to(
        os.path.relpath(stored_package, package_link.parent), target_is_directory=True
    )
    launcher.unlink()
    launcher.write_text(
        f'#!/bin/sh\nbasedir=${{0%/*}}\nexec node "$basedir/{relative}" "$@"\n'
    )
    launcher.chmod(0o755)
    _stub(env, "node", 'exec /bin/sh "$@"\n')
    checked = []
    access = os.access

    def check_access(path: str | Path, mode: int) -> bool:
        if mode == os.W_OK:
            checked.append(Path(path))
            return writable
        return access(path, mode)

    monkeypatch.setattr(deps.os, "access", check_access)
    item = _engine_read(env, _engine_spec(engine))
    assert not launcher.is_symlink()
    assert package_link.is_symlink()
    assert item.status == deps.UPDATE_AVAILABLE
    assert (
        item.command == ("" if writable else "sudo ") + f"pnpm add -g {package}@latest"
    )
    assert item.method == _engine_spec(engine).methods[1]
    assert item.resolved_path == str(target)
    assert checked == [target.parent]
    assert item.note is None


@pytest.mark.parametrize("engine", ["claude", "opencode"])
def test_vendor_pattern_resolves_home_symlink(
    env: dict[str, str], tmp_path: Path, engine: str
) -> None:
    real_home = Path(env["HOME"])
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    env["HOME"] = str(linked_home)
    target = real_home / (
        ".local/share/claude/versions/1.0.0"
        if engine == "claude"
        else ".opencode/bin/opencode"
    )
    launcher = _launcher(env, engine, target)
    if engine == "opencode":
        launcher.unlink()
        launcher = linked_home / ".opencode/bin/opencode"
    item = _engine_read(env, _engine_spec(engine))
    assert item.method == _engine_spec(engine).methods[-2]
    assert item.resolved_path == str(target)
    assert item.command == f"{launcher} {'update' if engine == 'claude' else 'upgrade'}"
    assert item.note is None


def test_pnpm_store_is_not_a_package(env: dict[str, str], tmp_path: Path) -> None:
    target = tmp_path / "global/5/node_modules/.pnpm/codex@1.0.0/bin/codex.js"
    launcher = _launcher(env, "codex", target)
    item = _engine_read(env, _engine_spec())
    assert item.method is None
    assert item.command == f"{launcher} update"
    assert item.note is not None


def test_shim_target_preserves_first_node_modules_path(tmp_path: Path) -> None:
    target = tmp_path / "real/node_modules/codex/cli.js"
    target.parent.mkdir(parents=True)
    target.touch()
    shim = tmp_path / "codex"
    shim.write_text(
        '#!/bin/sh\n"$basedir/node"\n'
        '"$basedir/./real/node_modules/codex/../codex/cli.js"\n'
        '"$basedir/other/node_modules/codex/cli.js"\n'
    )
    # The first qualifying path itself contains a symlink.
    (tmp_path / "real/node_modules/codex/cli.js").unlink()
    actual = tmp_path / "actual.js"
    actual.touch()
    target.symlink_to(actual)
    assert deps._shim_target(shim) == target
    assert target.resolve() == actual


@pytest.mark.parametrize(
    "content",
    [
        b'\x7fELF"$basedir/global/5/node_modules/codex/cli.js"',
        b'"$basedir/global/5/node_modules/codex/cli.js"',
        b'#!/bin/sh\n"$basedir/node"',
        b'#!/bin/sh\n"$basedir/global/5/node_modules/codex/cli.js"\xff',
        b'#!/bin/sh\n"$basedir/global/5/node_modules/codex/cli.js"' + b" " * 65536,
        b'#!/bin/sh\n"$basedir/missing/node_modules/codex/cli.js"',
    ],
)
def test_shim_target_falls_back(tmp_path: Path, content: bytes) -> None:
    target = tmp_path / "global/5/node_modules/codex/cli.js"
    target.parent.mkdir(parents=True)
    target.touch()
    shim = tmp_path / "codex"
    shim.write_bytes(content)
    assert deps._shim_target(shim) == shim


def test_shim_target_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "codex"
    shim.write_text('#!/bin/sh\n"$basedir/global/5/node_modules/codex/cli.js"')

    def fail_read(self: Path, mode: str) -> None:
        raise OSError("read failed")

    monkeypatch.setattr(Path, "open", fail_read)
    assert deps._shim_target(shim) == shim


def test_shim_at_size_limit(tmp_path: Path) -> None:
    target = tmp_path / "node_modules/codex/cli.js"
    target.parent.mkdir(parents=True)
    target.touch()
    shim = tmp_path / "codex"
    content = b'#!/bin/sh\n"$basedir/node_modules/codex/cli.js"\n'
    shim.write_bytes(content.ljust(64 * 1024, b" "))
    assert deps._shim_target(shim) == target
    assert deps._shim_target(tmp_path) == tmp_path


def test_shim_dpkg_lookup_uses_resolved_target(
    env: dict[str, str], tmp_path: Path
) -> None:
    target = tmp_path / "store/node_modules/codex/bin/codex.js"
    launcher = _launcher(env, "codex", target)
    modules = tmp_path / "node_modules"
    modules.mkdir()
    (modules / "codex").symlink_to(target.parent.parent, target_is_directory=True)
    launcher.unlink()
    launcher.write_text(
        "#!/bin/sh\nbasedir=${0%/*}\n"
        'exec /bin/sh "$basedir/../node_modules/codex/bin/codex.js" "$@"\n'
    )
    launcher.chmod(0o755)
    log = tmp_path / "dpkg-path"
    _stub(
        env,
        "dpkg-query",
        f'printf "%s" "$2" > {shlex.quote(str(log))}\nprintf "%s\\n" "codex: $2"\n',
    )
    item = _engine_read(env, _engine_spec())
    assert log.read_text() == str(target)
    assert item.resolved_path == str(target)
    assert item.method is not None and item.method.kind == "dpkg"
    assert item.command == "sudo apt install --only-upgrade codex"


# The skill link


@pytest.fixture
def skill_spec() -> deps.Spec:
    return next(spec for spec in deps.load_components() if spec.id == "ember-skill")


@pytest.mark.parametrize("newest", deps.NEWEST_MODES)
@pytest.mark.parametrize("has_source", [True, False])
@pytest.mark.parametrize(
    ("state", "expected_status", "command"),
    [
        ("installed", deps.CURRENT, None),
        ("missing", deps.MISSING, "emb skill install"),
        ("broken link", deps.CHECK_FAILED, "emb skill install"),
        ("other link", deps.CHECK_FAILED, "emb skill install --force"),
        ("folder", deps.CHECK_FAILED, None),
        ("file", deps.CHECK_FAILED, None),
    ],
)
def test_skill_link_component(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill_spec: deps.Spec,
    newest: str,
    has_source: bool,
    state: str,
    expected_status: str,
    command: str | None,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: ember\n---\n")
    candidates = (source,) if has_source else (tmp_path / "absent",)
    monkeypatch.setattr(skill, "source_candidates", lambda: candidates)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude-config")
    link = skill.link_path(env)
    link.parent.mkdir(parents=True)
    if state == "installed":
        link.symlink_to(source)
        if not has_source:
            state, expected_status = "other link", deps.CHECK_FAILED
    elif state == "broken link":
        link.symlink_to(tmp_path / "gone")
    elif state == "other link":
        other = tmp_path / "other"
        other.mkdir()
        link.symlink_to(other)
    elif state == "folder":
        link.mkdir()
    elif state == "file":
        link.write_text("keep this file")
    expected = skill.status(env, candidates)
    fetch = Fetcher({})
    report = deps.read(
        env, components=(skill_spec,), newest=newest, fetch=fetch, now=NOW
    )
    item = _one(report)
    assert item.group == "apps"
    assert item.status == expected_status
    assert item.link_state == expected.state == state
    assert item.installed is None and item.newest is None
    assert item.error == (state if expected_status == deps.CHECK_FAILED else None)
    assert item.command == (command if has_source else None)
    assert item.command_kind == ("install" if item.command else None)
    if not has_source:
        assert item.note == "Reinstall Ember."
    elif state in ("folder", "file"):
        assert item.note == f"Move {link}, then run emb skill install."
    else:
        assert item.note is None
    assert report.counts() == int(expected_status != deps.CURRENT)
    assert report.installed_read_at == {item.id: NOW}
    assert report.newest_read_at == {item.id: None}
    assert report.oldest_newest_read() is None
    assert report.newest_read_complete()
    assert fetch.calls == []
    assert skill.status(env, candidates) == expected

    monkeypatch.setattr(deps, "read", lambda *args, **kwargs: report)
    assert cli.main(["deps"], env=env) == 0
    output = capsys.readouterr().out
    assert state in output
    assert "version" not in output and "newest" not in output
    if item.command:
        assert f"install: {item.command}" in output
    if item.note:
        assert item.note in output
    assert cli.main(["deps", "--json"], env=env) == 0
    data = json.loads(capsys.readouterr().out)["components"][0]
    assert data["installed"] is None and data["newest"] is None
    assert data["link_state"] == state


@pytest.mark.parametrize("state", ["installed", "missing", "broken link"])
def test_skill_cached_read(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_spec: deps.Spec,
    state: str,
) -> None:
    current = skill.Status(tmp_path / "link", state, None, tmp_path / "source", None)
    monkeypatch.setattr(skill, "status", lambda *_args: current)
    first = deps.read(env, components=(skill_spec,), newest="never", now=NOW)

    def no_read(*args: object) -> skill.Status:
        pytest.fail("cached skill status must not read the link")

    monkeypatch.setattr(skill, "status", no_read)
    cached = deps.read(
        env, components=(skill_spec,), installed=False, newest="always", now=NOW + HOUR
    )
    assert cached == first


def test_none_newest_excluded_from_reads_and_summaries(
    env: dict[str, str],
) -> None:
    _apt_stubs(env, "1.0", "1.0", older=False)
    # A versioned component may also opt out of newest reads.
    no_newest = replace(_specs(APT)[0], id="no-newest", newest=deps.Source("none"))
    for mode in deps.NEWEST_MODES:
        report = deps.read(
            env,
            components=(*_specs(APT), no_newest),
            newest=mode,
            now=NOW,
            fetch=Fetcher({}),
        )
        assert report.newest_read_at == {"tmux": NOW, "no-newest": None}
        assert report.oldest_newest_read() == NOW
        assert report.newest_read_complete()
        stale = replace(report, newest_read_at={"tmux": NOW, "no-newest": NOW - HOUR})
        assert stale.oldest_newest_read() == NOW
        assert stale.newest_read_complete()


def test_skill_counts_leave_other_failed_checks_unchanged(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_spec: deps.Spec,
) -> None:
    monkeypatch.setattr(
        skill,
        "status",
        lambda *_args: skill.Status(tmp_path / "link", "file", None, None, "missing"),
    )
    report = deps.read(env, components=(skill_spec,), newest="never", fetch=Fetcher({}))
    failed_skill = _one(report)
    failed_tool = replace(failed_skill, id="tool", installed_kind="command")
    report = replace(report, components=(failed_skill, failed_tool))
    assert report.counts() == 1


@pytest.mark.parametrize(
    "commands", ['"bad"', '{ file = "rm file" }', "{ missing = 1 }"]
)
def test_skill_commands_schema(commands: str) -> None:
    with pytest.raises(deps.ComponentsError):
        _specs(
            "[[components]]" + "\n"
            'id = "skill"\nlabel = "Skill"\ngroup = "apps"\n'
            f'installed = {{ kind = "skill", commands = {commands} }}\n'
            'newest = { kind = "none" }\n'
        )


@pytest.mark.parametrize("newest", deps.NEWEST_MODES)
def test_none_newest_discards_cached_version(
    env: dict[str, str],
    newest: str,
) -> None:
    _apt_stubs(env, "1.0", "1.0", older=False)
    _read(env, APT)
    spec = replace(_specs(APT)[0], newest=deps.Source("none"))
    _stub(env, "apt-cache", 'echo unexpected > "' + env["PATH"] + '/read-newest"')
    report = deps.read(
        env, components=(spec,), newest=newest, now=NOW, fetch=Fetcher({})
    )
    assert _one(report).newest is None
    assert report.newest_read_at == {"tmux": None}
    assert report.oldest_newest_read() is None
    assert report.newest_read_complete()
    assert not (Path(env["PATH"]) / "read-newest").exists()
    cached = _cache(env)["components"]
    assert isinstance(cached, dict)
    assert "newest" not in cached["tmux"]


def test_skill_requires_no_newest_source() -> None:
    with pytest.raises(deps.ComponentsError, match="a skill needs newest kind none"):
        _specs(
            '[[components]]\nid = "skill"\nlabel = "Skill"\ngroup = "apps"\n'
            'installed = { kind = "skill" }\n'
            'newest = { kind = "pypi", package = "example" }\n'
        )


# Ember releases


@pytest.fixture
def ember_version(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    versions: list[str | None] = ["0.2.1"]

    def version(package: str) -> str:
        assert package == "ember-app"
        if versions[0] is None:
            raise deps.importlib.metadata.PackageNotFoundError(package)
        return versions[0]

    monkeypatch.setattr(deps.importlib.metadata, "version", version)
    return versions
