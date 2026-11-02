import json
import re
import stat
import time
import tomllib
from pathlib import Path

import pytest

from ember import deps
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
        except FileNotFoundError:
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
    (Path(env["PATH"]) / "codex").unlink()
    item = _one(_read(env, CODEX, fetch, installed=False, now=NOW + 1))
    assert item.status == deps.UPDATE_AVAILABLE
    assert item.installed == "1.0.0"
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
