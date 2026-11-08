import json
import os
import shlex
import stat
import subprocess
import tomllib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from ember import choices, paths, sandbox
from ember.runs import core, exits, record


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


def test_kept_paths_keeps_the_workers_cache(home: Path, tmp_path: Path) -> None:
    kept = sandbox.kept_paths(
        home, home / "wt", home / "wt/.git", env={"HOME": str(home)}
    )
    assert home / ".cache/ember/worker" in kept
    cache_home = tmp_path / "cache"
    kept = sandbox.kept_paths(
        home, home / "wt", home / "wt/.git", env={"XDG_CACHE_HOME": str(cache_home)}
    )
    assert cache_home / "ember/worker" in kept


def test_worker_cache_env(home: Path) -> None:
    variables = sandbox.worker_cache_env({"HOME": str(home)})
    root = home / ".cache/ember/worker"
    assert variables == {
        "XDG_CACHE_HOME": str(root),
        "UV_CACHE_DIR": str(root / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(root / "uv-python"),
        "PIP_CACHE_DIR": str(root / "pip"),
        "npm_config_cache": str(root / "npm"),
        "GOMODCACHE": str(root / "go/mod"),
    }


def test_worker_cache_env_replaces_the_owners_values(
    home: Path, tmp_path: Path
) -> None:
    env = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "UV_CACHE_DIR": str(home / ".cache/uv"),
    }
    variables = sandbox.worker_cache_env(env)
    assert variables["XDG_CACHE_HOME"] == str(tmp_path / "cache/ember/worker")
    assert variables["UV_CACHE_DIR"] == str(tmp_path / "cache/ember/worker/uv")


def test_worker_cache_env_preserves_tool_homes(home: Path) -> None:
    env = {
        "HOME": str(home),
        "CARGO_HOME": str(home / "cargo"),
        "GRADLE_USER_HOME": str(home / "gradle"),
    }
    worker = {**env, **sandbox.worker_cache_env(env)}
    assert worker["CARGO_HOME"] == env["CARGO_HOME"]
    assert worker["GRADLE_USER_HOME"] == env["GRADLE_USER_HOME"]
    assert "CARGO_HOME" not in sandbox.worker_cache_env(env)
    assert "GRADLE_USER_HOME" not in sandbox.worker_cache_env(env)


def test_check_kept_refuses_a_workers_cache_inside_embers_folders(
    env: dict[str, str], home: Path
) -> None:
    env["XDG_CACHE_HOME"] = str(home / ".config")
    with pytest.raises(sandbox.KeptPathError) as caught:
        sandbox.check_kept(home / "wt", home / "wt/.git", [], env=env)
    assert caught.value.path == home / ".config/ember/worker"
    assert caught.value.folder == home / ".config/ember"


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


def test_check_kept_accepts_paths_outside_embers_folders(
    env: dict[str, str], home: Path
) -> None:
    worktree = home / ".local/share/ember/worktrees/run-1"
    sandbox.check_kept(worktree, home / "src/proj/.git", [], env=env)
    sandbox.check_kept(worktree, home / "src/proj/.git", [home / ".config"], env=env)


@pytest.mark.parametrize(
    ("kept", "folder"),
    [
        ("worktree", ".local/state/ember"),
        ("common", ".config/ember"),
        ("read", ".config/ember"),
        ("read", ".local/state/ember"),
    ],
)
def test_check_kept_refuses_a_kept_path_inside_embers_folders(
    env: dict[str, str], home: Path, kept: str, folder: str
) -> None:
    inside = home / folder / "inside"
    worktree = inside if kept == "worktree" else home / "wt"
    common = inside if kept == "common" else home / "src/proj/.git"
    read = [inside] if kept == "read" else []
    with pytest.raises(sandbox.KeptPathError) as caught:
        sandbox.check_kept(worktree, common, read, env=env)
    assert caught.value.path == inside
    assert caught.value.folder == home / folder
    assert str(caught.value) == (
        f"the kept path {inside} is inside {home / folder}, "
        "which the sandbox hides from every worker"
    )


def test_check_kept_follows_a_symlink_into_embers_folder(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    _tree(home, ".config/ember/data/")
    link = tmp_path / "link"
    link.symlink_to(home / ".config/ember/data")
    with pytest.raises(sandbox.KeptPathError) as caught:
        sandbox.check_kept(home / "wt", home / "wt/.git", [link], env=env)
    assert caught.value.path == home / ".config/ember/data"


def test_check_kept_refuses_worktrees_in_a_shared_data_and_state_base(
    env: dict[str, str], tmp_path: Path
) -> None:
    env.update(XDG_DATA_HOME=str(tmp_path / "x"), XDG_STATE_HOME=str(tmp_path / "x"))
    worktree = paths.worktree_path("run-1", env)
    with pytest.raises(sandbox.KeptPathError) as caught:
        sandbox.check_kept(worktree, tmp_path / "repo/.git", [], env=env)
    assert caught.value.folder == tmp_path / "x/ember"


@pytest.mark.parametrize("kept", ["other", "root", "alias"])
def test_check_kept_refuses_reading_other_workers(
    env: dict[str, str], tmp_path: Path, kept: str
) -> None:
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    root = paths.ensure_worktrees_dir(env)
    other = root / "other"
    other.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(other, target_is_directory=True)
    read = {"other": other, "root": root, "alias": alias}[kept]
    with pytest.raises(sandbox.KeptPathError) as caught:
        sandbox.check_kept(root / "own", tmp_path / "repo/.git", [read], env=env)
    assert caught.value.folder == root


def test_check_kept_accepts_reads_inside_own_worktree(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = paths.worktrees_dir(env)
    sandbox.check_kept(
        root / "own", tmp_path / "repo/.git", [root / "own/data"], env=env
    )


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


@pytest.mark.parametrize("read_only", [False, True])
def test_codex_args_keep_the_workers_cache_writable(
    env: dict[str, str], home: Path, read_only: bool
) -> None:
    _tree(home, ".cache/uv/", ".cache/ember/sandbox.json")
    args = sandbox.codex_args(
        home / "wt", home / "wt/.git", read_only=read_only, env=env
    )
    config = _profile(args)
    cache = home / ".cache/ember/worker"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    filesystem = config["permissions"]["ember"]["filesystem"]
    assert filesystem[str(cache)] == "write"
    assert filesystem[str(home / ".cache/uv")] == "deny"
    assert filesystem[str(home / ".cache/ember/sandbox.json")] == "deny"
    assert config["shell_environment_policy"]["set"] == sandbox.worker_cache_env(env)


def test_codex_args_legacy_read_only_and_network(
    env: dict[str, str], home: Path
) -> None:
    config = _profile(
        sandbox.codex_args(
            home / "wt", home / "wt/.git", read_only=True, network=True, env=env
        )
    )
    profile = config["permissions"]["ember"]
    assert profile["extends"] == ":read-only"
    assert profile["network"]["enabled"] is True
    assert profile["filesystem"][str(home / "wt")] == "read"
    assert profile["filesystem"]["/tmp"] == "write"
    assert "TMPDIR" not in config["shell_environment_policy"]["set"]


@pytest.mark.parametrize("engine", ["codex", "opencode", "claude"])
@pytest.mark.parametrize("read_only", [False, True])
def test_sandbox_workers_use_default_tmp(
    env: dict[str, str], home: Path, engine: str, read_only: bool
) -> None:
    worktree = home / "wt"
    worktree.mkdir()
    owner_tmp = Path(env["TMPDIR"])
    assert owner_tmp not in sandbox.kept_paths(
        home, worktree, worktree / ".git", env=env
    )
    if engine == "codex":
        config = _profile(
            sandbox.codex_args(
                worktree, worktree / ".git", read_only=read_only, env=env
            )
        )
        assert config["permissions"]["ember"]["filesystem"]["/tmp"] == "write"
        assert "TMPDIR" not in config["shell_environment_policy"]["set"]
    else:
        wrap = sandbox.opencode_wrap if engine == "opencode" else sandbox.claude_wrap
        args = wrap(
            ["x"], worktree=worktree, common=worktree / ".git",
            read_only=read_only, env=env,
        )  # fmt: skip
        assert "TMPDIR" not in dict(_pairs(args, "--setenv"))
        tmp_mount = next(
            i
            for i, arg in enumerate(args)
            if arg == "--tmpfs" and args[i + 1] == "/tmp"
        )
        assert tmp_mount < min(
            i
            for i, arg in enumerate(args)
            if arg in ("--bind", "--ro-bind") and args[i + 1] != "/"
        )
        for flag in ("--bind", "--ro-bind"):
            assert ("/tmp", "/tmp") not in _pairs(args, flag)
            assert (str(owner_tmp), str(owner_tmp)) not in _pairs(args, flag)


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


def _worker_worktrees(
    env: dict[str, str], tmp_path: Path, location: str
) -> tuple[Path, Path]:
    if location != "home":
        env["XDG_DATA_HOME"] = str(tmp_path / "data")
    if location == "symlink":
        target = tmp_path / "private"
        target.mkdir(mode=0o700)
        data = paths.data_dir(env)
        data.mkdir(parents=True, mode=0o700)
        (data / "worktrees").symlink_to(target, target_is_directory=True)
    root = paths.ensure_worktrees_dir(env)
    own = root / "own"
    own.mkdir()
    (own / ".git").write_text(f"gitdir: {tmp_path}/repo/.git/worktrees/own\n")
    (root / "other").mkdir()
    (root / "other/secret").write_text("private worker data")
    (root / "private.txt").write_text("private worker metadata")
    return root, own


@pytest.mark.parametrize("engine", ["codex", "opencode", "claude"])
def test_sandbox_creates_worktrees_folder_before_walking_it(
    env: dict[str, str], tmp_path: Path, engine: str
) -> None:
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    root = paths.worktrees_dir(env)
    assert not root.exists()
    if engine == "codex":
        args = sandbox.codex_args(
            tmp_path / "repo", tmp_path / "repo/.git", [tmp_path], env=env
        )
        filesystem = _profile(args)["permissions"]["ember"]["filesystem"]
        assert str(root) not in filesystem
    else:
        wrap = sandbox.opencode_wrap if engine == "opencode" else sandbox.claude_wrap
        args = wrap(
            ["x"], worktree=tmp_path / "repo", common=tmp_path / "repo/.git", env=env
        )
        assert ("--tmpfs", str(root)) not in zip(args, args[1:], strict=False)
    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.parametrize("location", ["home", "external", "symlink"])
@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("owns_worktree", [False, True])
@pytest.mark.parametrize("broad_read", [False, True])
def test_codex_args_hides_worktrees_except_own(
    env: dict[str, str],
    tmp_path: Path,
    location: str,
    read_only: bool,
    owns_worktree: bool,
    broad_read: bool,
) -> None:
    root, own = _worker_worktrees(env, tmp_path, location)
    worktree = own if owns_worktree else tmp_path / "readonly-checkout"
    args = sandbox.codex_args(
        worktree,
        tmp_path / "repo/.git",
        [tmp_path] if broad_read else [],
        read_only=read_only,
        env=env,
    )
    filesystem = _profile(args)["permissions"]["ember"]["filesystem"]
    assert str(root) not in filesystem
    assert filesystem[str(worktree)] == ("read" if read_only else "write")
    assert filesystem[str(worktree / ".git")] == "read"
    assert filesystem[str(root / "other")] == "deny"
    assert filesystem[str(root / "private.txt")] == "deny"
    if not owns_worktree:
        assert filesystem[str(own)] == "deny"
    _assert_no_kept_under_denial(
        [Path(path) for path, access in filesystem.items() if access != "deny"],
        [Path(path) for path, access in filesystem.items() if access == "deny"],
    )


def test_codex_args_keeps_protected_folders_hidden_inside_own_worktree(
    env: dict[str, str], tmp_path: Path
) -> None:
    root, own = _worker_worktrees(env, tmp_path, "external")
    env["XDG_CONFIG_HOME"] = str(own / "config")
    env["XDG_STATE_HOME"] = str(own / "state")
    for folder in (paths.config_dir(env), paths.state_dir(env)):
        folder.mkdir(parents=True)
    args = sandbox.codex_args(own, tmp_path / "repo/.git", env=env)
    filesystem = _profile(args)["permissions"]["ember"]["filesystem"]
    assert str(root) not in filesystem
    assert filesystem[str(own)] == "write"
    for folder in (paths.config_dir(env), paths.state_dir(env)):
        assert filesystem[str(folder)] == "deny"


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


def _assert_no_kept_under_denial(kept: Sequence[Path], denied: Sequence[Path]) -> None:
    for path in kept:
        for hidden in denied:
            assert not path.is_relative_to(hidden), (path, hidden)
            assert not path.resolve().is_relative_to(hidden.resolve()), (path, hidden)


def _assert_no_bind_under_mask(args: Sequence[str]) -> None:
    binds = _pairs(args, "--bind") + _pairs(args, "--ro-bind")
    # /tmp is a private namespace; sibling worktree masks are denials.
    denied = [
        Path(args[i + 1])
        for i, arg in enumerate(args)
        if arg == "--tmpfs" and args[i + 1] != "/tmp"
    ]
    denied.extend(Path(target) for source, target in binds if source == "/dev/null")
    kept = [Path(target) for source, target in binds if source != "/dev/null"]
    _assert_no_kept_under_denial(kept, denied)


def test_opencode_wrap(env: dict[str, str], home: Path, tmp_path: Path) -> None:
    _tree(home, ".local/bin/", ".local/share/opencode/", ".config/opencode/", "wt/.git")
    worktree = home / "wt"
    common = tmp_path / "repo.git"
    common.mkdir()
    args = sandbox.opencode_wrap(
        ["opencode", "run"], worktree=worktree, common=common, env=env
    )
    assert args[:8] == [
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
    ]  # fmt: skip
    tmpfs = [args[i + 1] for i, arg in enumerate(args) if arg == "--tmpfs"]
    assert env["XDG_RUNTIME_DIR"] in tmpfs
    assert tmpfs[0] == "/tmp" and str(home) not in tmpfs
    _assert_no_bind_under_mask(args)
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


def test_opencode_wrap_legacy_read_only(env: dict[str, str], home: Path) -> None:
    (home / "wt").mkdir()
    args = sandbox.opencode_wrap(
        ["x"], worktree=home / "wt", common=home / "wt/.git", read_only=True, env=env
    )
    assert (str(home / "wt"),) * 2 not in _pairs(args, "--bind")
    assert (str(home / "wt"),) * 2 in _pairs(args, "--ro-bind")
    assert (str(home / ".cache/ember/worker"),) * 2 in _pairs(args, "--bind")


def test_opencode_wrap_gives_the_workers_cache(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    cache_home = tmp_path / "cache"
    env["XDG_CACHE_HOME"] = str(cache_home)
    (home / "wt").mkdir()
    args = sandbox.opencode_wrap(
        ["x"], worktree=home / "wt", common=home / "wt/.git", env=env
    )
    cache = cache_home / "ember/worker"
    assert cache.is_dir()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert (str(cache),) * 2 in _pairs(args, "--bind")
    assert dict(_pairs(args, "--setenv")) == sandbox.worker_cache_env(env)
    assert (str(cache_home / "opencode"),) * 2 not in _pairs(args, "--bind")


def test_opencode_wrap_hides_ember_folders(env: dict[str, str], home: Path) -> None:
    _tree(home, ".config/ember/settings.db", ".local/state/ember/")
    args = sandbox.opencode_wrap(
        ["x"], worktree=home / "wt", common=home / "wt/.git", read=[str(home)], env=env
    )
    tmpfs = [args[i + 1] for i, arg in enumerate(args) if arg == "--tmpfs"]
    assert str(home / ".config/ember") in tmpfs
    assert str(home / ".local/state/ember") in tmpfs
    last_bind = max(
        i
        for i, arg in enumerate(args)
        if arg in ("--bind", "--ro-bind") and args[i + 1] != "/dev/null"
    )
    assert args.index(str(home / ".config/ember")) > last_bind


@pytest.mark.parametrize("engine", ["opencode", "claude"])
@pytest.mark.parametrize("location", ["home", "external", "symlink"])
@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("owns_worktree", [False, True])
@pytest.mark.parametrize("broad_read", [False, True])
def test_wrap_hides_worktrees_except_own(
    env: dict[str, str],
    tmp_path: Path,
    engine: str,
    location: str,
    read_only: bool,
    owns_worktree: bool,
    broad_read: bool,
) -> None:
    root, own = _worker_worktrees(env, tmp_path, location)
    worktree = own if owns_worktree else tmp_path / "readonly-checkout"
    wrap = sandbox.opencode_wrap if engine == "opencode" else sandbox.claude_wrap
    args = wrap(
        ["x"],
        worktree=worktree,
        common=tmp_path / "repo/.git",
        read=[tmp_path] if broad_read else [],
        read_only=read_only,
        env=env,
    )
    masks = [i for i, arg in enumerate(args) if arg == "--tmpfs"]
    mask = next(i for i in masks if args[i + 1] == str(root / "other"))
    if broad_read:
        read_bind = next(
            i
            for i, arg in enumerate(args)
            if arg == "--ro-bind" and args[i + 1] == str(tmp_path)
        )
        assert mask > read_bind
    assert ("/dev/null", str(root / "private.txt")) in _pairs(args, "--ro-bind")
    binds = _pairs(args, "--ro-bind" if read_only else "--bind")
    assert ((str(own), str(own)) in binds) is owns_worktree
    if owns_worktree:
        assert (str(own / ".git"),) * 2 in _pairs(args, "--ro-bind")
    assert not any(
        Path(source).is_relative_to(root / "other")
        for source, _ in _pairs(args, "--bind") + _pairs(args, "--ro-bind")
    )
    assert str(root) not in [args[i + 1] for i in masks]
    _assert_no_bind_under_mask(args)


@pytest.mark.parametrize("engine", ["opencode", "claude"])
@pytest.mark.parametrize("ancestor", ["data", "ember", "parent"])
@pytest.mark.parametrize("read_only", [False, True])
def test_wrap_hides_worktrees_through_symlinked_read_ancestors(
    env: dict[str, str],
    home: Path,
    tmp_path: Path,
    engine: str,
    ancestor: str,
    read_only: bool,
) -> None:
    root, own = _worker_worktrees(env, tmp_path, "external")
    target = {"data": root.parent.parent, "ember": root.parent, "parent": tmp_path}[
        ancestor
    ]
    first = home / "data-link"
    first.symlink_to(target, target_is_directory=True)
    second = home / "another-link"
    second.symlink_to(first, target_is_directory=True)
    # A read path can contain a symlink in its parent components too.
    read = [first, second / root.parent.relative_to(target)]
    common = tmp_path / "repo/.git"
    sandbox.check_kept(own, common, read, env=env)
    wrap = sandbox.opencode_wrap if engine == "opencode" else sandbox.claude_wrap
    args = wrap(
        ["x"], worktree=own, common=common, read=read, read_only=read_only, env=env
    )
    for alias in (root, *(link / root.relative_to(target) for link in (first, second))):
        mask = next(
            i
            for i, arg in enumerate(args)
            if arg == "--tmpfs" and args[i + 1] == str(alias / "other")
        )
        assert not any(
            arg == "--tmpfs" and args[i + 1] == str(alias) for i, arg in enumerate(args)
        )
        assert mask > max(i for i, arg in enumerate(args) if arg == "--bind")
        for flag in ("--bind", "--ro-bind"):
            assert not any(
                Path(destination).is_relative_to(alias / "other")
                for source, destination in _pairs(args, flag)
                if source != "/dev/null"
            )
    _assert_no_bind_under_mask(args)


@pytest.mark.parametrize("engine", ["opencode", "claude"])
@pytest.mark.parametrize("folder", ["config", "state"])
def test_wrap_hides_protected_folders_through_symlinked_read_ancestors(
    env: dict[str, str], home: Path, tmp_path: Path, engine: str, folder: str
) -> None:
    base = tmp_path / folder
    protected = base / "ember"
    protected.mkdir(parents=True)
    env[f"XDG_{folder.upper()}_HOME"] = str(base)
    alias = home / "folder-link"
    alias.symlink_to(base, target_is_directory=True)
    own = home / "wt"
    own.mkdir()
    common = tmp_path / "repo/.git"
    sandbox.check_kept(own, common, [alias], env=env)
    wrap = sandbox.opencode_wrap if engine == "opencode" else sandbox.claude_wrap
    args = wrap(["x"], worktree=own, common=common, read=[alias], env=env)
    last_bind = max(
        i
        for i, arg in enumerate(args)
        if arg in ("--bind", "--ro-bind") and args[i + 1] != "/dev/null"
    )
    for hidden in (protected, alias / "ember"):
        mask = next(
            i
            for i, arg in enumerate(args)
            if arg == "--tmpfs" and args[i + 1] == str(hidden)
        )
        assert mask > last_bind


def test_wrap_restores_git_protection_inside_own_worktree(
    env: dict[str, str], tmp_path: Path
) -> None:
    root, own = _worker_worktrees(env, tmp_path, "external")
    common = own / "git-metadata"
    common.mkdir()
    args = sandbox.opencode_wrap(["x"], worktree=own, common=common, env=env)
    assert (str(common),) * 2 in _pairs(args, "--ro-bind")
    assert args.index(str(common)) > args.index(str(own))
    assert ("--tmpfs", str(root)) not in zip(args, args[1:], strict=False)
    _assert_no_bind_under_mask(args)


def test_wrap_hides_protected_folders_after_restoring_own_worktree(
    env: dict[str, str], tmp_path: Path
) -> None:
    _, own = _worker_worktrees(env, tmp_path, "external")
    env["XDG_CONFIG_HOME"] = str(own / "config")
    env["XDG_STATE_HOME"] = str(own / "state")
    for folder in (paths.config_dir(env), paths.state_dir(env)):
        folder.mkdir(parents=True)
    args = sandbox.opencode_wrap(
        ["x"], worktree=own, common=tmp_path / "repo/.git", env=env
    )
    last_bind = max(
        i
        for i, arg in enumerate(args)
        if arg in ("--bind", "--ro-bind") and args[i + 1] != "/dev/null"
    )
    for folder in (paths.config_dir(env), paths.state_dir(env)):
        mask = next(
            i
            for i, arg in enumerate(args)
            if arg == "--tmpfs" and args[i + 1] == str(folder)
        )
        assert mask > last_bind


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
    cache = home / ".cache/ember/worker"
    for path in (home / ".claude", home / ".claude.json", worktree, cache):
        assert (str(path),) * 2 in binds
    assert (str(home / ".cache/claude-cli-nodejs"),) * 2 not in binds
    assert ("XDG_CACHE_HOME", str(cache)) in _pairs(args, "--setenv")
    read_only = _pairs(args, "--ro-bind")
    assert (str(install),) * 2 in read_only
    assert (str(data),) * 2 in read_only
    assert not any("opencode" in pair[0] for pair in [*binds, *read_only])
    assert "--unshare-net" not in args
    assert ("--tmpfs", str(home)) not in zip(args, args[1:], strict=False)
    _assert_no_bind_under_mask(args)


def test_claude_wrap_legacy_read_only_and_config_dir(
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


# check cache


def _sample(engine: str, *, ok: bool = True, at: float = 100.0) -> sandbox.SandboxCheck:
    return sandbox.SandboxCheck(
        engine=engine,
        provider="bubblewrap",
        bwrap_path="/usr/bin/bwrap",
        bwrap_version="0.11.1",
        ok=ok,
        detail="/bin/true ran under bubblewrap" if ok else "the sandbox test failed",
        causes=() if ok else ("AppArmor blocks it",),
        checked_at=at,
    )


@pytest.fixture
def cache_env(env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    return {**env, "XDG_CACHE_HOME": str(tmp_path / "cache")}


def _cache_file(env: dict[str, str]) -> Path:
    return Path(env["XDG_CACHE_HOME"]) / "ember" / "sandbox.json"


def test_last_checks_without_cache(cache_env: dict[str, str]) -> None:
    assert sandbox.last_checks(cache_env) == {}


def test_save_check_round_trip(cache_env: dict[str, str]) -> None:
    check = _sample("opencode", ok=False)
    sandbox.save_check(check, cache_env)
    assert sandbox.last_checks(cache_env) == {"opencode": check}
    path = _cache_file(cache_env)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {"checks": {"opencode": check.to_json()}}


def test_save_check_replaces_only_its_engine(cache_env: dict[str, str]) -> None:
    sandbox.save_check(_sample("claude", at=1.0), cache_env)
    sandbox.save_check(_sample("codex", at=2.0), cache_env)
    newer = _sample("claude", ok=False, at=3.0)
    sandbox.save_check(newer, cache_env)
    checks = sandbox.last_checks(cache_env)
    assert list(checks) == ["codex", "claude"]
    assert checks["claude"] == newer
    assert checks["codex"] == _sample("codex", at=2.0)


def test_save_check_from_threads_keeps_every_engine(
    cache_env: dict[str, str],
) -> None:
    engines = ["codex", "opencode", "claude"] * 4

    def save(index: int) -> None:
        sandbox.save_check(_sample(engines[index], at=float(index)), cache_env)

    with ThreadPoolExecutor(max_workers=len(engines)) as pool:
        list(pool.map(save, range(len(engines))))
    assert set(sandbox.last_checks(cache_env)) == {"codex", "opencode", "claude"}


@pytest.mark.parametrize("text", ["not json", "[]", '{"checks": []}', ""])
def test_corrupt_cache_reads_empty_and_is_replaced(
    cache_env: dict[str, str], text: str
) -> None:
    path = _cache_file(cache_env)
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert sandbox.last_checks(cache_env) == {}
    sandbox.save_check(_sample("codex"), cache_env)
    assert sandbox.last_checks(cache_env) == {"codex": _sample("codex")}


def test_invalid_entries_are_skipped(cache_env: dict[str, str]) -> None:
    good = _sample("codex").to_json()
    path = _cache_file(cache_env)
    path.parent.mkdir(parents=True)
    entries = {
        "codex": good,
        "opencode": {**good, "engine": "opencode", "ok": "yes"},
        "claude": {**good, "engine": "codex"},
        "gemini": {**good, "engine": "gemini"},
    }
    path.write_text(json.dumps({"checks": entries}))
    assert sandbox.last_checks(cache_env) == {"codex": _sample("codex")}
    sandbox.save_check(_sample("claude"), cache_env)
    assert set(json.loads(path.read_text())["checks"]) == {"codex", "claude"}


def test_save_check_that_cannot_write_raises_nothing(
    cache_env: dict[str, str],
) -> None:
    Path(cache_env["XDG_CACHE_HOME"]).write_text("a file, not a folder")
    sandbox.save_check(_sample("codex"), cache_env)
    assert sandbox.last_checks(cache_env) == {}


def test_save_check_rejects_other_engines(cache_env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        sandbox.save_check(_sample("gemini"), cache_env)


def _denial_snapshot(
    env: dict[str, str], home: Path, read: tuple[str, ...] = ()
) -> dict[str, Any]:
    worktree = home / "project"
    worktree.mkdir(exist_ok=True)
    return sandbox.hidden_snapshot(worktree, worktree / ".git", read, env=env)


@pytest.mark.parametrize(
    "error", ["Permission denied", "Operation not permitted", "EACCES"]
)
@pytest.mark.parametrize("operand", ["file", "folder", "missing file"])
def test_denials_match_hidden_folder_and_offer_deepest_safe_folder(
    env: dict[str, str], home: Path, error: str, operand: str
) -> None:
    tool = home / ".toolchain" / "versions" / "3.14"
    tool.mkdir(parents=True)
    binary = tool / "python"
    binary.write_text("")
    denied = {"file": binary, "folder": tool, "missing file": tool / "missing"}[operand]
    snapshot = _denial_snapshot(env, home)
    expected = [
        {"path": str(home / ".toolchain"), "reason": "hidden", "allow_path": str(tool)}
    ]
    assert sandbox.denied_paths(f"{error}: '{denied}'", snapshot) == expected
    assert (
        sandbox.denied_paths(f"{denied}: {error}\n{denied}: {error}", snapshot)
        == expected
    )
    assert sandbox.denied_paths(f"{denied}: file not found", snapshot) == []
    assert sandbox.denied_paths(f"{error}: /etc/passwd", snapshot) == []
    assert sandbox.valid_denial_allow_path(home / ".toolchain", tool)


@pytest.mark.parametrize(
    ("restriction", "reason"),
    [
        ("none", "hidden"),
        ("secret", "secret location"),
        ("protected", "protected"),
        ("config", "hidden"),
        ("contains secret", "hidden"),
    ],
)
def test_denial_of_uv_symlink_offers_resolved_pipx_folder_after_safety_checks(
    env: dict[str, str], home: Path, restriction: str, reason: str
) -> None:
    tool = home / ".local/share/pipx/venvs/uv/bin"
    tool.mkdir(parents=True)
    binary = tool / "uv"
    binary.touch()
    alias = home / ".local/bin/uv"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(binary)
    if restriction == "secret":
        (home / ".ssh").symlink_to(tool, target_is_directory=True)
    elif restriction == "protected":
        env["XDG_RUNTIME_DIR"] = str(tool)
    elif restriction == "config":
        env["XDG_CONFIG_HOME"] = str(tool)
    elif restriction == "contains secret":
        secret = tool / "credentials"
        secret.mkdir()
        (home / ".ssh").symlink_to(secret, target_is_directory=True)
    snapshot = _denial_snapshot(env, home)
    for name in (alias, binary):
        result = sandbox.denied_paths(f"{name}: Permission denied", snapshot)
        assert len(result) == 1
        assert result[0]["reason"] == reason
        assert result[0]["allow_path"] == (str(tool) if restriction == "none" else None)
        if restriction == "none":
            assert result[0]["path"] == str(home / ".local/share/pipx")
            assert sandbox.valid_denial_allow_path(result[0]["path"], str(tool))


@pytest.mark.parametrize("code", [exits.OK, exits.PROVIDER_ERROR])
def test_symlink_denial_owner_line_suggests_resolved_folder(
    env: dict[str, str], home: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    tool = home / ".local/share/pipx/venvs/uv/bin"
    tool.mkdir(parents=True)
    binary = tool / "uv"
    binary.touch()
    alias = home / ".local/bin/uv"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(binary)
    denials = sandbox.denied_paths(
        f"{alias}: Permission denied", _denial_snapshot(env, home)
    )
    run = Mock(spec=record.Run, read_effective=(), read=())
    err = StringIO()
    runner = core.Core(env=env, err=err)
    monkeypatch.setattr(
        runner, "_saved_outcome", Mock(return_value={"denied_paths": denials})
    )
    wait_owner = Mock(return_value=run)
    monkeypatch.setattr(runner, "_wait_owner", wait_owner)
    runner._warn_denial_owners(run, code)
    owner = err.getvalue().strip()
    qualification = (
        "the turn finished (exit 0); read access is needed only if "
        "the worker's report says the work needed the path; "
        if code == exits.OK
        else ""
    )
    assert owner == (
        f"owner: {qualification}Settings, Sandbox read paths; emb choices --set "
        + shlex.quote("sandbox.read=" + json.dumps([str(tool)]))
    )
    wait_owner.assert_called_once_with(run, owner, str(tool))


@pytest.mark.parametrize(
    ("hidden", "offered", "valid"),
    [
        ("tools", "tools", True),
        ("tools", "tools/versions/3.14", True),
        ("tools/versions/3.14", "tools", True),
        ("tools", "tools-other", False),
        (".local/share/pipx", ".local/bin", False),
        ("tools", None, False),
    ],
)
def test_recorded_denial_offer_must_overlap_hidden_path(
    tmp_path: Path, hidden: str, offered: str | None, valid: bool
) -> None:
    allow = None if offered is None else str(tmp_path / offered)
    assert sandbox.valid_denial_allow_path(str(tmp_path / hidden), allow) is valid


@pytest.mark.parametrize("hidden_alias", [False, True])
@pytest.mark.parametrize("related", [False, True])
def test_recorded_denial_offer_resolves_symlinks(
    tmp_path: Path, hidden_alias: bool, related: bool
) -> None:
    hidden = tmp_path / "pipx"
    hidden.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(
        hidden if related else tmp_path / "unrelated", target_is_directory=True
    )
    first, second = (alias, hidden) if hidden_alias else (hidden, alias)
    assert sandbox.valid_denial_allow_path(first, second) is related


@pytest.mark.parametrize("hidden", ["relative", "/absolute"])
def test_recorded_denial_offer_rejects_relative_paths(hidden: str) -> None:
    assert not sandbox.valid_denial_allow_path(hidden, "relative")
    assert not sandbox.valid_denial_allow_path("relative", hidden)


def test_recorded_denial_offer_rejects_symlink_loops(tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(alias)
    assert not sandbox.valid_denial_allow_path(alias, tmp_path)
    assert not sandbox.valid_denial_allow_path(tmp_path, alias)


@pytest.mark.parametrize("relative", sandbox.SECRET_HOME_PATHS)
def test_every_secret_location_is_reported_without_allow(
    env: dict[str, str], home: Path, relative: str
) -> None:
    secret = home / relative
    secret.parent.mkdir(parents=True, exist_ok=True)
    if relative.endswith(("config.json", ".netrc")):
        secret.touch()
        denied = secret
    else:
        secret.mkdir(exist_ok=True)
        denied = secret / "credentials"
    result = sandbox.denied_paths(
        f"'{denied}': Permission denied", _denial_snapshot(env, home)
    )
    assert len(result) == 1
    assert result[0]["reason"] == "secret location"
    assert result[0]["allow_path"] is None


def test_denial_aliases_are_deduplicated_and_secret_target_is_checked(
    env: dict[str, str], home: Path
) -> None:
    secret = home / ".ssh"
    secret.mkdir()
    alias = home / "tools"
    alias.symlink_to(secret, target_is_directory=True)
    snapshot = _denial_snapshot(env, home)
    result = sandbox.denied_paths(
        f"{alias}/id: EACCES\n{secret}/id: Permission denied", snapshot
    )
    assert len(result) == 1
    assert result[0]["reason"] == "secret location"
    assert result[0]["allow_path"] is None


def test_snapshot_retains_hidden_names_and_resolved_targets(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "versions"
    target.mkdir()
    alias = home / "tools"
    alias.symlink_to(target, target_is_directory=True)
    snapshot = _denial_snapshot(env, home)
    # Changing current settings does not change an existing run's inventory.
    choices.set_settings({"sandbox.read": [str(alias)]}, env=env)
    assert sandbox.denied_paths(f"{target}/python: EACCES", snapshot)[0]["path"] == str(
        alias
    )
    assert (
        sandbox.denied_paths(
            f"{target}/python: EACCES", _denial_snapshot(env, home, (str(alias),))
        )
        == []
    )


def test_config_runtime_and_other_worktree_cannot_be_offered(
    env: dict[str, str], home: Path
) -> None:
    config = home / ".config" / "tool"
    config.mkdir(parents=True)
    other = paths.ensure_worktrees_dir(env) / "other"
    other.mkdir()
    snapshot = _denial_snapshot(env, home)
    for path, reason in (
        (config / "a", "hidden"),
        (Path(env["XDG_RUNTIME_DIR"]) / "a", "protected"),
        (other / "a", "protected"),
    ):
        result = sandbox.denied_paths(f"{path}: EACCES", snapshot)
        assert len(result) == 1
        assert result[0]["reason"] == reason
        assert result[0]["allow_path"] is None
    assert not sandbox.allow_reading_path(config, env)
    assert not sandbox.allow_reading_path(home, env)
    assert not sandbox.allow_reading_path(home / ".config", env)


def test_denials_handle_spaces_json_and_hidden_files(
    env: dict[str, str], home: Path
) -> None:
    folder = home / "tool with spaces"
    folder.mkdir()
    file = home / ".toolrc"
    file.touch()
    snapshot = _denial_snapshot(env, home)
    output = json.dumps(
        {"output": f"'{folder}/python': Permission denied\n'{file}': EACCES"}
    )
    result = sandbox.denied_paths(output, snapshot)
    assert result == [
        {"path": str(folder), "reason": "hidden", "allow_path": str(folder)},
        {"path": str(file), "reason": "hidden", "allow_path": None},
    ]


@pytest.mark.parametrize("engine", ["claude", "opencode"])
def test_snapshot_excludes_engine_folders_and_binary(
    env: dict[str, str], home: Path, engine: str
) -> None:
    own = home / (".claude" if engine == "claude" else ".config/opencode")
    own.mkdir(parents=True)
    binary = home / ".tool" / "version" / engine
    binary.parent.mkdir(parents=True)
    binary.touch()
    worktree = home / "project"
    worktree.mkdir()
    snapshot = sandbox.hidden_snapshot(
        worktree, worktree / ".git", engine=engine, binary=binary, env=env
    )
    assert sandbox.denied_paths(f"{own}/a: EACCES\n{binary}: EACCES", snapshot) == []


def test_protected_folder_inside_own_worktree_is_still_reported(
    env: dict[str, str], home: Path
) -> None:
    worktree = home / "project"
    worktree.mkdir()
    env["XDG_CONFIG_HOME"] = str(worktree / "config")
    private = paths.config_dir(env)
    private.mkdir(parents=True)
    snapshot = sandbox.hidden_snapshot(worktree, worktree / ".git", env=env)
    result = sandbox.denied_paths(f"{private}/settings.db: EACCES", snapshot)
    assert result == [
        {"path": str(private), "reason": "secret location", "allow_path": None}
    ]


def test_redirected_config_inventory_and_descendant_secrets_block_offer(
    env: dict[str, str], home: Path
) -> None:
    config = home / "custom-config"
    config.mkdir()
    env["XDG_CONFIG_HOME"] = str(config)
    (config / "gh").mkdir()
    (config / "tool").mkdir()
    snapshot = _denial_snapshot(env, home)
    for folder, reason in (
        (config / "gh", "secret location"),
        (config / "tool", "hidden"),
    ):
        result = sandbox.denied_paths(f"{folder}/a: EACCES", snapshot)
        assert result[0]["reason"] == reason
        assert result[0]["allow_path"] is None
    # A folder holding a credential location cannot be offered, even for a
    # different denied file directly in that folder.
    var = home / ".var" / "app"
    var.mkdir(parents=True)
    snapshot = _denial_snapshot(env, home)
    result = sandbox.denied_paths(f"'{var}/marker': Permission denied", snapshot)
    assert result[0]["allow_path"] is None


def test_runtime_protection_takes_precedence_over_secret_alias(
    env: dict[str, str], home: Path
) -> None:
    runtime = Path(env["XDG_RUNTIME_DIR"])
    secret = runtime / "credentials"
    secret.mkdir()
    (home / ".ssh").symlink_to(secret, target_is_directory=True)
    snapshot = _denial_snapshot(env, home)
    item = sandbox.denied_paths(f"{home}/.ssh/id: EACCES", snapshot)[0]
    assert item["reason"] == "protected"
    assert item["allow_path"] is None


@pytest.mark.parametrize(
    "output",
    [
        "error: failed to read the installation directory\n"
        "  Caused by: failed to read directory '{path}'\n"
        "  Caused by: Permission denied (os error 13)",
        "Permission denied:\n'{path}'",
    ],
)
def test_denial_path_can_be_in_neighboring_diagnostic_line(
    env: dict[str, str], home: Path, output: str
) -> None:
    folder = home / ".toolchain"
    folder.mkdir()
    snapshot = _denial_snapshot(env, home)
    result = sandbox.denied_paths(output.format(path=folder), snapshot)
    assert result == [
        {"path": str(folder), "reason": "hidden", "allow_path": str(folder)}
    ]
    assert sandbox.denied_paths(f"'{folder}'\n\nPermission denied", snapshot) == []


@pytest.mark.parametrize("spelling", ["absolute", "home"])
def test_fully_stated_denial_paths_are_detected(
    env: dict[str, str], home: Path, spelling: str
) -> None:
    folder = home / ".toolchain"
    folder.mkdir()
    snapshot = _denial_snapshot(env, home)
    name = str(folder / "python") if spelling == "absolute" else "~/.toolchain/python"
    assert sandbox.denied_paths(f"cat: {name}: Permission denied", snapshot) == [
        {"path": str(folder), "reason": "hidden", "allow_path": str(folder)}
    ]
    # Expanding ~/ uses the launch snapshot even if the owner's HOME changes.
    env["HOME"] = str(home / "different-home")
    assert sandbox.denied_paths(f"{name}: EACCES", snapshot)[0]["path"] == str(folder)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "cat: ../.toolchain/python: Permission denied",
        "cat: '../.toolchain/python': Permission denied",
        "EACCES: `../.toolchain/python`",
        "cat: ../other-worker/result.txt: Permission denied",
        "cat: key: Permission denied",
        "cat: 'key': Permission denied",
    ],
)
def test_relative_denials_are_not_resolved(
    env: dict[str, str], home: Path, diagnostic: str
) -> None:
    folder = home / ".toolchain"
    folder.mkdir()
    (folder / "python").touch()
    snapshot = _denial_snapshot(env, home)
    (home / "project/key").symlink_to(folder / "python")
    # Even a record with the former cwd field does not supply missing context.
    snapshot["cwd"] = str(home / "project")
    assert sandbox.denied_paths(diagnostic, snapshot) == []


def test_relative_names_with_absolute_looking_suffixes_are_ignored(
    env: dict[str, str], home: Path
) -> None:
    folder = home / ".toolchain"
    folder.mkdir()
    snapshot = _denial_snapshot(env, home)
    for name in (f"relative{folder}/python", f"../prefix {folder}/python"):
        assert sandbox.denied_paths(f"cat: '{name}': Permission denied", snapshot) == []
    assert (
        sandbox.denied_paths(
            f"cat: relative{folder}/python: Permission denied", snapshot
        )
        == []
    )


def test_absolute_denial_in_structured_message_is_detected(
    env: dict[str, str], home: Path
) -> None:
    folder = home / "toolchain with spaces"
    folder.mkdir()
    snapshot = _denial_snapshot(env, home)
    diagnostic = json.dumps({"message": f"cat: '{folder}/python': Permission denied"})
    assert sandbox.denied_paths(diagnostic, snapshot) == [
        {"path": str(folder), "reason": "hidden", "allow_path": str(folder)}
    ]


@pytest.mark.parametrize(
    "diagnostic",
    [
        "cat: ../.toolchain/python: Permission denied",
        "cat: 'key': Permission denied",
        "cat: key: Permission denied",
        "Permission denied: key",
    ],
)
def test_execution_directory_metadata_does_not_complete_a_relative_error(
    env: dict[str, str],
    home: Path,
    diagnostic: str,
) -> None:
    folder = home / ".toolchain"
    folder.mkdir()
    snapshot = _denial_snapshot(env, home)
    assert (
        sandbox.denied_paths(
            json.dumps(
                {
                    "cwd": str(folder),
                    "input": {"command": f"cd {folder} && cat ../.toolchain/python"},
                    "output": diagnostic,
                }
            ),
            snapshot,
        )
        == []
    )
    assert sandbox.denied_paths(f"cwd: {folder}\n{diagnostic}", snapshot) == []


@pytest.mark.parametrize(
    "error", ["Permission denied", "Operation not permitted", "EACCES"]
)
@pytest.mark.parametrize("spelling", ["absolute", "home"])
@pytest.mark.parametrize("container", ["output", "nested", "array", "scalar"])
def test_structured_diagnostic_starting_with_path_is_not_quoted_as_filename(
    env: dict[str, str],
    home: Path,
    error: str,
    spelling: str,
    container: str,
) -> None:
    folder = home / ".tools"
    folder.mkdir()
    snapshot = _denial_snapshot(env, home)
    name = str(folder) if spelling == "absolute" else "~/.tools"
    diagnostic = f"{name}: {error}"
    structured: object = {
        "output": {"output": diagnostic},
        "nested": {"result": {"content": [{"text": diagnostic}]}},
        "array": [{"output": diagnostic}],
        "scalar": diagnostic,
    }[container]
    expected = [{"path": str(folder), "reason": "hidden", "allow_path": str(folder)}]
    assert sandbox.denied_paths(diagnostic, snapshot) == expected
    assert sandbox.denied_paths(json.dumps(structured), snapshot) == expected


def test_standalone_path_field_can_contain_error_words_and_spaces(
    env: dict[str, str], home: Path
) -> None:
    folder = home / "tools with Permission denied in the name"
    folder.mkdir()
    snapshot = _denial_snapshot(env, home)
    assert sandbox.denied_paths(
        json.dumps({"error": "EACCES", "path": str(folder)}), snapshot
    ) == [{"path": str(folder), "reason": "hidden", "allow_path": str(folder)}]


@pytest.mark.parametrize("reverse", [False, True])
def test_hidden_folder_and_credential_denials_always_merge_as_secret(
    env: dict[str, str], home: Path, reverse: bool
) -> None:
    folder = home / ".docker"
    folder.mkdir()
    secret = folder / "config.json"
    secret.touch()
    snapshot = _denial_snapshot(env, home)
    lines = [f"{folder}: Permission denied", f"{secret}: Permission denied"]
    assert sandbox.denied_paths(lines[0], snapshot) == [
        {"path": str(folder), "reason": "hidden", "allow_path": None}
    ]
    if reverse:
        lines.reverse()
    assert sandbox.denied_paths("\n".join(lines), snapshot) == [
        {"path": str(folder), "reason": "secret location", "allow_path": None}
    ]


@pytest.mark.parametrize(
    "order",
    [
        (0, 1, 2),
        (0, 2, 1),
        (1, 0, 2),
        (1, 2, 0),
        (2, 0, 1),
        (2, 1, 0),
    ],
)
def test_denials_merge_as_protected_in_every_order(
    env: dict[str, str], home: Path, order: tuple[int, int, int]
) -> None:
    folder = home / ".docker"
    folder.mkdir()
    secret = folder / "config.json"
    secret.touch()
    runtime = folder / "runtime"
    runtime.mkdir(mode=0o700)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    snapshot = _denial_snapshot(env, home)
    names = [folder, secret, runtime / "socket"]
    text = "\n".join(f"{names[index]}: Permission denied" for index in order)
    assert sandbox.denied_paths(text, snapshot) == [
        {"path": str(folder), "reason": "protected", "allow_path": None}
    ]


@pytest.mark.parametrize("name", sandbox.STARTUP_HOME_FILES)
@pytest.mark.parametrize("link", ["none", "alias", "target"])
@pytest.mark.parametrize("engine", ["codex", "claude", "opencode"])
def test_startup_file_denials_keep_reason_without_read_offer(
    env: dict[str, str], home: Path, name: str, link: str, engine: str
) -> None:
    startup = home / name
    if link == "target":
        target = home / "dotfiles" / "shell-settings"
        target.parent.mkdir()
        target.touch()
        startup.symlink_to(target)
    else:
        startup.touch()
    alias = home / "startup-alias"
    alias.symlink_to(startup)
    worktree = home / "repo"
    worktree.mkdir()
    snapshot = sandbox.hidden_snapshot(
        worktree, worktree / ".git", engine=engine, env=env
    )
    assert sandbox.valid_snapshot(snapshot)
    denied = alias if link == "alias" else startup
    result = sandbox.denied_paths(f"bash: {denied}: Permission denied", snapshot)
    assert len(result) == 1
    assert result[0]["reason"] == "startup file"
    assert result[0]["allow_path"] is None
    # Resolved names and the original shell diagnostic identify the same denial.
    assert (
        sandbox.denied_paths(f"bash: {denied.resolve()}: Permission denied", snapshot)
        == result
    )


def test_startup_symlink_does_not_hide_other_denials_in_target_folder(
    env: dict[str, str], home: Path
) -> None:
    folder = home / "dotfiles"
    folder.mkdir()
    target = folder / "profile"
    target.touch()
    (home / ".profile").symlink_to(target)
    snapshot = _denial_snapshot(env, home)
    result = sandbox.denied_paths(
        f"bash: {home}/.profile: Permission denied\n"
        f"cat: {folder}/needed: Permission denied",
        snapshot,
    )
    assert result == [
        {"path": str(target), "reason": "startup file", "allow_path": None},
        {"path": str(folder), "reason": "hidden", "allow_path": str(folder)},
    ]


def test_startup_snapshot_keeps_launch_time_symlink_target(
    env: dict[str, str], home: Path
) -> None:
    target = home / "shell-settings"
    target.touch()
    startup = home / ".profile"
    startup.symlink_to(target)
    snapshot = _denial_snapshot(env, home)
    startup.unlink()
    startup.symlink_to(home / "new-settings")
    result = sandbox.denied_paths(f"bash: {target}: Permission denied", snapshot)
    assert result[0]["reason"] == "startup file"
    assert result[0]["allow_path"] is None


def test_legacy_snapshot_without_startup_inventory_is_valid(
    env: dict[str, str], home: Path
) -> None:
    (home / ".profile").touch()
    snapshot = _denial_snapshot(env, home)
    del snapshot["startup"]
    assert sandbox.valid_snapshot(snapshot)
    assert sandbox.denied_paths(
        f"bash: {home}/.profile: Permission denied", snapshot
    ) == [{"path": str(home / ".profile"), "reason": "hidden", "allow_path": None}]


@pytest.mark.parametrize("startup", [None, "a", ["relative"], [1]])
def test_invalid_startup_snapshot_is_rejected(
    env: dict[str, str], home: Path, startup: object
) -> None:
    snapshot = _denial_snapshot(env, home)
    snapshot["startup"] = startup
    assert not sandbox.valid_snapshot(snapshot)


@pytest.mark.parametrize("secret", [False, True])
def test_startup_symlink_into_restricted_folder_has_no_read_offer(
    env: dict[str, str], home: Path, secret: bool
) -> None:
    folder = home / ".ssh" if secret else Path(env["XDG_RUNTIME_DIR"])
    folder.mkdir(exist_ok=True)
    target = folder / "settings"
    target.touch()
    (home / ".profile").symlink_to(target)
    snapshot = _denial_snapshot(env, home)
    for name in (home / ".profile", target):
        result = sandbox.denied_paths(f"bash: {name}: Permission denied", snapshot)
        assert len(result) == 1
        assert result[0]["reason"] == "startup file"
        assert result[0]["allow_path"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_startup_and_hidden_denials_merge_without_read_offer(reverse: bool) -> None:
    hidden = {"path": "/home/owner/.profile", "reason": "hidden", "allow_path": None}
    startup = {**hidden, "reason": "startup file"}
    first, second = (startup, hidden) if reverse else (hidden, startup)
    assert sandbox.merge_denial(first, second) == startup
