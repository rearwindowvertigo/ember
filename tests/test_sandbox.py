import os
import stat
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ember import choices, sandbox


def _tool(folder: Path, name: str, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path = folder / name
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def env(tmp_path: Path, home: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "run").mkdir()
    (tmp_path / "tmp").mkdir()
    return {
        "HOME": str(home),
        "PATH": str(bin_dir),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "TMPDIR": str(tmp_path / "tmp"),
    }


@pytest.fixture(autouse=True)
def no_system_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "SYSTEM_CODEX_CONFIG", tmp_path / "etc-codex.toml")
    monkeypatch.setattr(sandbox, "APPARMOR_DIR", tmp_path / "apparmor.d")
    monkeypatch.setattr(sandbox, "DOCKER_SOCKETS", ())


def _bin(env: dict[str, str]) -> Path:
    return Path(env["PATH"])


# method


def test_method_default_follows_bwrap_on_path(env: dict[str, str]) -> None:
    for engine in ("codex", "opencode", "claude"):
        assert sandbox.method(engine, env) == "none"
    _tool(_bin(env), "bwrap")
    assert sandbox.method("codex", env) == "codex"
    assert sandbox.method("opencode", env) == "bubblewrap"
    assert sandbox.method("claude", env) == "bubblewrap"


def test_method_reads_setting(env: dict[str, str]) -> None:
    _tool(_bin(env), "bwrap")
    choices.set_settings({"sandbox.codex": "off", "sandbox.claude": "off"}, env=env)
    assert sandbox.method("codex", env) == "none"
    assert sandbox.method("claude", env) == "none"
    assert sandbox.method("opencode", env) == "bubblewrap"


def test_method_rejects_other_engines(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        sandbox.method("gemini", env)


def test_provider_of() -> None:
    assert sandbox.provider_of("codex", "on") == "codex"
    assert sandbox.provider_of("opencode", "on") == "bubblewrap"
    assert sandbox.provider_of("claude", "on") == "bubblewrap"
    for engine in sandbox.ENGINES:
        assert sandbox.provider_of(engine, "off") == "none"


# require


def test_require_needs_bwrap(env: dict[str, str]) -> None:
    for engine in ("opencode", "claude"):
        with pytest.raises(sandbox.SandboxError, match="bwrap is not installed"):
            sandbox.require(engine, env, run=FakeRun())
    with pytest.raises(sandbox.SandboxError, match="codex is not installed"):
        sandbox.require("codex", env, run=FakeRun())


def test_require_runs_bwrap(env: dict[str, str]) -> None:
    bwrap = _tool(_bin(env), "bwrap")
    run = FakeRun()
    assert sandbox.require("claude", env, run=run) is None
    ((argv, kwargs),) = run.calls
    assert argv[0] == str(bwrap)
    assert argv[-1] == "/bin/true"
    assert "--unshare-pid" in argv
    assert kwargs["timeout"] == sandbox.TIMEOUT
    assert sandbox.require("codex", env, run=FakeRun()) is None


def test_require_reports_a_blocked_bwrap(env: dict[str, str]) -> None:
    _tool(_bin(env), "bwrap")
    run = FakeRun(code=1, stderr="bwrap: setting up uid map: Permission denied\n")
    with pytest.raises(sandbox.SandboxError, match="uid map: Permission denied"):
        sandbox.require("opencode", env, run=run)


def test_require_uses_codex_s_bundled_bwrap(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, bundled = _npm_install(tmp_path / "local", monkeypatch)
    (_bin(env) / "codex").symlink_to(codex)
    run = FakeRun()
    folder = sandbox.require("codex", env, run=run)
    shim = Path(env["XDG_RUNTIME_DIR"]) / "ember/bin"
    assert folder == shim
    ((argv, _),) = run.calls
    assert argv[0] == str(shim / "bwrap")
    assert (shim / "bwrap").resolve() == bundled.resolve()


def test_none_warning() -> None:
    assert sandbox.none_warning("codex") == (
        "WARNING: codex runs without a sandbox; it can read every file you can read."
    )


# kept paths and deny list


def test_kept_paths(home: Path, tmp_path: Path) -> None:
    worktree = home / "src" / "wt"
    common = home / "src" / "repo" / ".git"
    kept = sandbox.kept_paths(home, worktree, common, ["/opt/data"], env={})
    for path in (
        home / ".local/bin",
        home / ".local/lib",
        home / ".gitconfig",
        home / ".config/git",
        worktree,
        common,
        Path("/opt/data"),
    ):
        assert path in kept
    assert kept == sorted(kept, key=str)


def test_kept_paths_uses_xdg_config_home(home: Path, tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    kept = sandbox.kept_paths(
        home, home / "wt", home / "wt/.git", env={"XDG_CONFIG_HOME": str(config)}
    )
    assert config / "git" in kept
    assert home / ".config/git" not in kept


def test_kept_paths_adds_symlink_targets(home: Path, tmp_path: Path) -> None:
    target = tmp_path / "real-bin"
    target.mkdir()
    (home / ".local").mkdir()
    (home / ".local/bin").symlink_to(target)
    kept = sandbox.kept_paths(home, home / "wt", home / "wt/.git", env={})
    assert home / ".local/bin" in kept
    assert target in kept


def _tree(home: Path, *entries: str) -> None:
    for entry in entries:
        path = home / entry
        if entry.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")


def test_deny_list_walks_only_ancestors(home: Path, tmp_path: Path) -> None:
    _tree(
        home,
        ".ssh/id_ed25519",
        ".local/bin/",
        ".local/lib/",
        ".local/share/",
        "src/proj/",
        "src/other/",
        ".gitconfig",
        "notes.txt",
    )
    runtime = tmp_path / "run"
    kept = sandbox.kept_paths(home, home / "src/proj", home / "src/proj/.git", env={})
    denied = sandbox.deny_list(home, kept, runtime)
    assert denied == sorted(
        [
            home / ".local/share",
            home / ".ssh",
            home / "notes.txt",
            home / "src/other",
            runtime,
        ],
        key=str,
    )


def test_deny_list_symlink_to_kept_is_not_denied(home: Path, tmp_path: Path) -> None:
    _tree(home, "src/proj/", "secret/")
    (home / "proj-link").symlink_to(home / "src/proj")
    (home / "secret-link").symlink_to(home / "secret")
    kept = sandbox.kept_paths(home, home / "src/proj", home / "src/proj/.git", env={})
    denied = sandbox.deny_list(home, kept, tmp_path / "run")
    assert home / "proj-link" not in denied
    # A symlink to a denied path is dropped in favour of the plain entry.
    assert home / "secret" in denied
    assert home / "secret-link" not in denied


def test_deny_list_walks_symlink_to_ancestor(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "data"
    (outside / "proj").mkdir(parents=True)
    (outside / "private").mkdir()
    (home / "data").symlink_to(outside)
    kept = sandbox.kept_paths(home, outside / "proj", outside / "proj/.git", env={})
    denied = sandbox.deny_list(home, kept, tmp_path / "run")
    assert home / "data" not in denied
    assert outside / "private" in denied
    assert outside / "proj" not in denied


def test_deny_list_protected_wins_over_kept(home: Path, tmp_path: Path) -> None:
    _tree(home, ".config/ember/settings.db", ".config/other/")
    kept = sandbox.kept_paths(
        home, home / "wt", home / "wt/.git", [str(home / ".config")], env={}
    )
    denied = sandbox.deny_list(
        home,
        kept,
        tmp_path / "run",
        protected=[home / ".config/ember", tmp_path / "missing"],
    )
    assert home / ".config/ember" in denied
    assert home / ".config/other" not in denied
    assert tmp_path / "missing" not in denied


def test_deny_list_drops_nested_entries(home: Path, tmp_path: Path) -> None:
    _tree(home, "secret/inner/")
    kept = sandbox.kept_paths(home, home / "wt", home / "wt/.git", env={})
    denied = sandbox.deny_list(
        home, kept, tmp_path / "run", protected=[home / "secret/inner"]
    )
    assert home / "secret" in denied
    assert home / "secret/inner" not in denied


def test_deny_list_missing_home(tmp_path: Path) -> None:
    home = tmp_path / "nohome"
    assert sandbox.deny_list(home, [], tmp_path / "run") == [tmp_path / "run"]


# codex_args


def _profile(args: Sequence[str]) -> dict[str, Any]:
    assert args[::2] == ["-c"] * (len(args) // 2)
    text = "\n".join(args[1::2])
    return tomllib.loads(text)


def test_codex_args(env: dict[str, str], home: Path, tmp_path: Path) -> None:
    _tree(home, ".ssh/key", "src/proj/", ".config/ember/settings.db")
    worktree = home / "src/proj"
    common = tmp_path / "repo.git"
    args = sandbox.codex_args(worktree, common, env=env)
    config = _profile(args)
    assert config["default_permissions"] == "ember"
    profile = config["permissions"]["ember"]
    assert profile["extends"] == ":workspace"
    assert profile["network"]["enabled"] is False
    filesystem = profile["filesystem"]
    assert filesystem[str(worktree)] == "write"
    assert filesystem[str(common)] == "read"
    assert filesystem[str(worktree / ".git")] == "read"
    assert filesystem[str(home / ".ssh")] == "deny"
    assert filesystem[str(home / ".config/ember")] == "deny"
    assert str(home / ".config") not in filesystem
    assert filesystem[env["XDG_RUNTIME_DIR"]] == "deny"


def test_codex_args_read_only_and_network(env: dict[str, str], home: Path) -> None:
    config = _profile(
        sandbox.codex_args(
            home / "wt", home / "wt/.git", read_only=True, network=True, env=env
        )
    )
    profile = config["permissions"]["ember"]
    assert profile["extends"] == ":read-only"
    assert profile["network"]["enabled"] is True
    assert profile["filesystem"][str(home / "wt")] == "read"


def test_codex_args_denies_ember_folders_outside_home(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    config_home = tmp_path / "xdg-config"
    state_home = tmp_path / "xdg-state"
    (config_home / "ember").mkdir(parents=True)
    (state_home / "ember").mkdir(parents=True)
    env = {
        **env,
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_STATE_HOME": str(state_home),
    }
    filesystem = _profile(
        sandbox.codex_args(home / "wt", home / "wt/.git", [str(tmp_path)], env=env)
    )["permissions"]["ember"]["filesystem"]
    assert filesystem[str(config_home / "ember")] == "deny"
    assert filesystem[str(state_home / "ember")] == "deny"


def test_codex_args_unicode_path(env: dict[str, str], home: Path) -> None:
    worktree = home / "wt-é"
    filesystem = _profile(sandbox.codex_args(worktree, worktree / ".git", env=env))[
        "permissions"
    ]["ember"]["filesystem"]
    assert filesystem[str(worktree)] == "write"


# codex_config_conflicts


def test_codex_config_conflicts(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "wt"
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text('sandbox_mode = "workspace-write"\n')
    (repo / ".codex").mkdir(parents=True)
    (repo / ".codex/config.toml").write_text('model = "x"\n')
    (worktree / ".codex").mkdir(parents=True)
    (worktree / ".codex/config.toml").write_text("not = [valid\n")
    sandbox.SYSTEM_CODEX_CONFIG.write_text('sandbox_mode = "read-only"\n')
    found = sandbox.codex_config_conflicts(worktree, repo / ".git", env)
    assert found == [
        sandbox.SYSTEM_CODEX_CONFIG,
        home / ".codex/config.toml",
        worktree / ".codex/config.toml",
    ]


def test_codex_config_conflicts_codex_home(env: dict[str, str], tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('sandbox_mode = "x"\n')
    found = sandbox.codex_config_conflicts(
        tmp_path / "wt", tmp_path / "wt/.git", {**env, "CODEX_HOME": str(codex_home)}
    )
    assert found == [codex_home / "config.toml"]


def test_codex_config_conflicts_none(env: dict[str, str], tmp_path: Path) -> None:
    assert (
        sandbox.codex_config_conflicts(tmp_path / "wt", tmp_path / "wt/.git", env) == []
    )


# bundled bwrap


def _npm_install(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, hoisted: bool = False
) -> tuple[Path, Path]:
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "x86_64")
    package = root / "lib/node_modules/@openai/codex"
    (package / "bin").mkdir(parents=True)
    launcher = package / "bin/codex.js"
    launcher.write_text("")
    launcher.chmod(0o755)
    scope = package.parent if hoisted else package / "node_modules/@openai"
    resources = (
        scope / "codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex-resources"
    )
    resources.mkdir(parents=True)
    bwrap = _tool(resources, "bwrap")
    (root / "bin").mkdir(parents=True, exist_ok=True)
    codex = root / "bin/codex"
    codex.symlink_to(Path("../lib/node_modules/@openai/codex/bin/codex.js"))
    return codex, bwrap


@pytest.mark.parametrize("hoisted", [False, True])
def test_codex_bundled_bwrap_npm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hoisted: bool
) -> None:
    codex, bwrap = _npm_install(tmp_path / "local", monkeypatch, hoisted=hoisted)
    assert sandbox.codex_bundled_bwrap(codex) == bwrap


def test_codex_bundled_bwrap_native(tmp_path: Path) -> None:
    triple = tmp_path / "vendor/x86_64-unknown-linux-musl"
    (triple / "codex").mkdir(parents=True)
    binary = _tool(triple / "codex", "codex")
    (triple / "codex-resources").mkdir()
    bwrap = _tool(triple / "codex-resources", "bwrap")
    assert sandbox.codex_bundled_bwrap(binary) == bwrap


def test_codex_bundled_bwrap_missing(tmp_path: Path) -> None:
    assert sandbox.codex_bundled_bwrap(_tool(tmp_path, "codex")) is None


def test_codex_bundled_path(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, bwrap = _npm_install(tmp_path / "local", monkeypatch)
    folder = sandbox.codex_bundled_path(codex, env)
    assert folder == Path(env["XDG_RUNTIME_DIR"]) / "ember/bin"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert Path(os.readlink(folder / "bwrap")) == bwrap
    assert sandbox.codex_bundled_path(codex, env) == folder


def test_codex_bundled_path_replaces_stale_link(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, bwrap = _npm_install(tmp_path / "local", monkeypatch)
    folder = Path(env["XDG_RUNTIME_DIR"]) / "ember/bin"
    folder.mkdir(parents=True)
    (folder / "bwrap").symlink_to(tmp_path / "old-bwrap")
    sandbox.codex_bundled_path(codex, env)
    assert Path(os.readlink(folder / "bwrap")) == bwrap
    assert sorted(path.name for path in folder.iterdir()) == ["bwrap"]


def test_codex_bundled_path_without_bundle_raises(
    env: dict[str, str], tmp_path: Path
) -> None:
    with pytest.raises(sandbox.SandboxError, match="no bundled bwrap"):
        sandbox.codex_bundled_path(_tool(tmp_path, "codex"), env)


# opencode_wrap


def _pairs(args: Sequence[str], flag: str) -> list[tuple[str, str]]:
    return [(args[i + 1], args[i + 2]) for i, arg in enumerate(args) if arg == flag]


def test_opencode_wrap(env: dict[str, str], home: Path, tmp_path: Path) -> None:
    _tree(home, ".local/bin/", ".local/share/opencode/", ".config/opencode/", "wt/.git")
    worktree = home / "wt"
    common = tmp_path / "repo.git"
    common.mkdir()
    args = sandbox.opencode_wrap(
        ["opencode", "run"], worktree=worktree, common=common, env=env
    )
    assert args[:9] == [
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs",
    ]  # fmt: skip
    tmpfs = [args[i + 1] for i, arg in enumerate(args) if arg == "--tmpfs"]
    assert tmpfs[:3] == ["/tmp", str(home), env["XDG_RUNTIME_DIR"]]
    ro = _pairs(args, "--ro-bind")
    rw = _pairs(args, "--bind")
    assert (str(home / ".local/bin"),) * 2 in ro
    assert (str(home / ".local/share/opencode"),) * 2 in rw
    assert (str(home / ".config/opencode"),) * 2 in ro
    assert (str(worktree),) * 2 in rw
    assert (str(common),) * 2 in ro
    assert (str(worktree / ".git"),) * 2 in ro
    assert args[-6:] == [
        "--unshare-pid",
        "--die-with-parent",
        "--chdir",
        str(worktree),
        "opencode",
        "run",
    ]


def test_opencode_wrap_read_only(env: dict[str, str], home: Path) -> None:
    (home / "wt").mkdir()
    args = sandbox.opencode_wrap(
        ["x"], worktree=home / "wt", common=home / "wt/.git", read_only=True, env=env
    )
    assert (str(home / "wt"),) * 2 not in _pairs(args, "--bind")
    assert (str(home / "wt"),) * 2 in _pairs(args, "--ro-bind")


def test_opencode_wrap_hides_ember_folders(env: dict[str, str], home: Path) -> None:
    _tree(home, ".config/ember/settings.db", ".local/state/ember/")
    args = sandbox.opencode_wrap(
        ["x"], worktree=home / "wt", common=home / "wt/.git", read=[str(home)], env=env
    )
    tmpfs = [args[i + 1] for i, arg in enumerate(args) if arg == "--tmpfs"]
    assert str(home / ".config/ember") in tmpfs
    assert str(home / ".local/state/ember") in tmpfs
    last_bind = max(i for i, arg in enumerate(args) if arg in ("--bind", "--ro-bind"))
    assert args.index(str(home / ".config/ember")) > last_bind


def test_opencode_wrap_keeps_binary_folder(env: dict[str, str], home: Path) -> None:
    (home / ".opencode/bin").mkdir(parents=True)
    binary = _tool(home / ".opencode/bin", "opencode")
    args = sandbox.opencode_wrap(
        ["x"], worktree=home / "wt", common=home / "wt/.git", binary=binary, env=env
    )
    assert (str(home / ".opencode/bin"),) * 2 in _pairs(args, "--ro-bind")


# claude_wrap


def test_claude_wrap(env: dict[str, str], home: Path, tmp_path: Path) -> None:
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text("{}")
    (home / ".cache/claude-cli-nodejs").mkdir(parents=True)
    (home / ".config/opencode").mkdir(parents=True)
    install = home / ".local/share/claude/versions"
    install.mkdir(parents=True)
    binary = install / "2.1.0"
    binary.write_text("")
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/claude").symlink_to(binary)
    worktree = home / "repo"
    (worktree / ".git").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    args = sandbox.claude_wrap(
        [str(home / ".local/bin/claude"), "-p"],
        worktree=worktree,
        common=worktree / ".git",
        read=[data],
        network=False,
        env=env,
    )
    assert args[0] == "bwrap"
    assert args[-2:] == [str(home / ".local/bin/claude"), "-p"]
    binds = _pairs(args, "--bind")
    for path in (home / ".claude", home / ".claude.json", worktree):
        assert (str(path),) * 2 in binds
    assert (str(home / ".cache/claude-cli-nodejs"),) * 2 in binds
    read_only = _pairs(args, "--ro-bind")
    assert (str(install),) * 2 in read_only
    assert (str(data),) * 2 in read_only
    assert not any("opencode" in pair[0] for pair in [*binds, *read_only])
    assert "--unshare-net" not in args
    assert ("--tmpfs", str(home)) in zip(args, args[1:], strict=False)


def test_claude_wrap_read_only_and_config_dir(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    config = tmp_path / "claude-config"
    config.mkdir()
    env = {**env, "CLAUDE_CONFIG_DIR": str(config)}
    args = sandbox.claude_wrap(
        ["claude"],
        worktree=home / "wt",
        common=home / "wt/.git",
        read_only=True,
        env=env,
    )
    assert (str(config),) * 2 in _pairs(args, "--bind")
    assert (str(home / "wt"),) * 2 in _pairs(args, "--ro-bind")
    assert (str(home / "wt"),) * 2 not in _pairs(args, "--bind")


# check


class FakeRun:
    def __init__(self, code: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.code = code
        self.stderr = stderr

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "bubblewrap 0.11.1\n", "")
        return subprocess.CompletedProcess(argv, self.code, "", self.stderr)

    def probes(self) -> list[tuple[list[str], dict[str, Any]]]:
        return [call for call in self.calls if call[0][-1] != "--version"]


def test_check_none(env: dict[str, str]) -> None:
    run = FakeRun()
    result = sandbox.check("codex", env, run=run)
    assert result.provider == "none"
    assert result.to_json()["provider"] == "none"
    assert result.ok is True
    assert result.detail == "no sandbox"
    assert result.bwrap_path is None
    assert run.calls == []
    assert result.to_json()["causes"] == []


def test_check_codex_bubblewrap(env: dict[str, str]) -> None:
    bwrap = _tool(_bin(env), "bwrap")
    codex = _tool(_bin(env), "codex")
    choices.set_settings({"worker.network": "on"}, env=env)
    run = FakeRun()
    result = sandbox.check("codex", env, run=run)
    assert result.ok is True, result.detail
    assert result.provider == "codex"
    assert result.detail == "/bin/true ran under Codex's sandbox"
    assert result.bwrap_path == str(bwrap)
    assert result.bwrap_version == "0.11.1"
    ((argv, kwargs),) = run.probes()
    assert argv[:2] == [str(codex), "sandbox"]
    assert argv[-1] == "/bin/true"
    profile = _profile(argv[2:-1])["permissions"]["ember"]
    assert profile["network"]["enabled"] is True
    assert kwargs["timeout"] == sandbox.TIMEOUT
    assert kwargs["env"]["PATH"] == env["PATH"]
    assert profile["filesystem"][str(kwargs["cwd"])] == "write"
    assert not Path(kwargs["cwd"]).exists()


def test_check_codex_bundled(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _ = _npm_install(tmp_path / "local", monkeypatch)
    (_bin(env) / "codex").symlink_to(codex)
    run = FakeRun()
    result = sandbox.check("codex", env, run=run)
    assert result.ok is True, result.detail
    assert result.provider == "codex"
    shim = Path(env["XDG_RUNTIME_DIR"]) / "ember/bin"
    assert result.bwrap_path == str(shim / "bwrap")
    ((_, kwargs),) = run.probes()
    assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(shim)


def test_check_codex_bundled_missing(env: dict[str, str]) -> None:
    _tool(_bin(env), "codex")
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    run = FakeRun()
    result = sandbox.check("codex", env, run=run)
    assert result.ok is False
    assert "no bundled bwrap" in result.detail
    assert run.probes() == []


def test_check_codex_not_installed(env: dict[str, str]) -> None:
    _tool(_bin(env), "bwrap")
    result = sandbox.check("codex", env, run=FakeRun())
    assert result.ok is False
    assert "codex is not installed" in result.detail


def test_check_failure_reports_apparmor_and_install(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    _tool(_bin(env), "bwrap")
    (home / ".npm/bin").mkdir(parents=True)
    _tool(home / ".npm/bin", "codex")
    env = {**env, "PATH": os.pathsep.join([env["PATH"], str(home / ".npm/bin")])}
    sandbox.APPARMOR_DIR.mkdir()
    (sandbox.APPARMOR_DIR / "bwrap-userns-restrict").write_text("")
    result = sandbox.check(
        "codex", env, run=FakeRun(code=1, stderr="\nbwrap: denied\n")
    )
    assert result.ok is False
    assert result.detail == "the sandbox test failed with exit code 1: bwrap: denied"
    assert any("bwrap-userns-restrict" in cause for cause in result.causes)
    assert any(str(home / ".npm/bin/codex") in cause for cause in result.causes)


def test_check_success_ignores_apparmor(env: dict[str, str]) -> None:
    _tool(_bin(env), "bwrap")
    _tool(_bin(env), "codex")
    sandbox.APPARMOR_DIR.mkdir()
    (sandbox.APPARMOR_DIR / "bwrap-userns-restrict").write_text("")
    result = sandbox.check("codex", env, run=FakeRun())
    assert result.ok is True
    assert result.causes == ()


def test_check_sandbox_mode_fails(env: dict[str, str], home: Path) -> None:
    _tool(_bin(env), "bwrap")
    _tool(_bin(env), "codex")
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text('sandbox_mode = "danger-full-access"\n')
    result = sandbox.check("codex", env, run=FakeRun())
    assert result.ok is False
    assert len(result.causes) == 1
    assert "sandbox_mode" in result.causes[0]


def test_check_timeout(env: dict[str, str]) -> None:
    _tool(_bin(env), "bwrap")
    _tool(_bin(env), "codex")

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "bubblewrap 0.11.1\n", "")
        raise subprocess.TimeoutExpired(argv, sandbox.TIMEOUT)

    result = sandbox.check("codex", env, run=run)
    assert result.ok is False
    assert "timed out" in result.detail


def test_check_opencode(env: dict[str, str]) -> None:
    bwrap = _tool(_bin(env), "bwrap")
    run = FakeRun()
    result = sandbox.check("opencode", env, run=run)
    assert result.ok is True, result.detail
    ((argv, kwargs),) = run.probes()
    assert argv[0] == str(bwrap)
    assert argv[-1] == "/bin/true"
    assert argv[-3:-1] == ["--chdir", str(kwargs["cwd"])]
    assert result.bwrap_version == "0.11.1"


def test_check_claude(env: dict[str, str], home: Path) -> None:
    bwrap = _tool(_bin(env), "bwrap")
    (home / ".claude").mkdir()
    run = FakeRun()
    result = sandbox.check("claude", env, run=run)
    assert result.ok is True, result.detail
    assert result.provider == "bubblewrap"
    assert result.detail == "/bin/true ran under bubblewrap"
    ((argv, kwargs),) = run.probes()
    assert argv[0] == str(bwrap)
    assert argv[-1] == "/bin/true"
    assert (str(home / ".claude"),) * 2 in _pairs(argv, "--bind")


def test_check_opencode_without_bwrap(env: dict[str, str]) -> None:
    choices.set_settings({"sandbox.opencode": "on"}, env=env)
    result = sandbox.check("opencode", env, run=FakeRun())
    assert result.ok is False
    assert "bwrap is not installed" in result.detail


def test_check_bad_version_output(env: dict[str, str]) -> None:
    _tool(_bin(env), "bwrap")

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "--version":
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = sandbox.check("opencode", env, run=run)
    assert result.ok is True
    assert result.bwrap_version is None


def test_check_rejects_other_engines(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        sandbox.check("gemini", env, run=FakeRun())
