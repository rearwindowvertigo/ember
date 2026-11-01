import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from ember import paths

PathFunction = Callable[[Mapping[str, str] | None], Path]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


@pytest.mark.parametrize(
    ("variable", "function"),
    [
        ("XDG_CONFIG_HOME", paths.config_dir),
        ("XDG_STATE_HOME", paths.state_dir),
        ("XDG_CACHE_HOME", paths.cache_dir),
    ],
)
def test_absolute_variable_is_used(
    variable: str, function: PathFunction, home: Path, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    env = {"HOME": str(home), variable: str(base)}
    assert function(env) == base / "ember"


DEFAULTS = [
    ("XDG_CONFIG_HOME", paths.config_dir, (".config",)),
    ("XDG_STATE_HOME", paths.state_dir, (".local", "state")),
    ("XDG_CACHE_HOME", paths.cache_dir, (".cache",)),
]


@pytest.mark.parametrize(("variable", "function", "default"), DEFAULTS)
def test_unset_variable_uses_default(
    variable: str, function: PathFunction, default: tuple[str, ...], home: Path
) -> None:
    assert function({"HOME": str(home)}) == home.joinpath(*default, "ember")


@pytest.mark.parametrize(("variable", "function", "default"), DEFAULTS)
@pytest.mark.parametrize("value", ["", "relative/base", "./base"])
def test_empty_or_relative_variable_is_ignored(
    variable: str,
    function: PathFunction,
    default: tuple[str, ...],
    value: str,
    home: Path,
) -> None:
    env = {"HOME": str(home), variable: value}
    assert function(env) == home.joinpath(*default, "ember")


def test_empty_home_falls_back_to_path_home() -> None:
    assert paths.home_dir({"HOME": ""}) == Path.home()
    assert paths.home_dir({}) == Path.home()


def test_named_paths(home: Path, tmp_path: Path) -> None:
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    assert paths.settings_db_path(env) == tmp_path / "config/ember/settings.db"
    assert paths.runs_dir(env) == tmp_path / "state/ember/runs"


def test_runtime_variable_is_used(tmp_path: Path) -> None:
    env = {"XDG_RUNTIME_DIR": str(tmp_path / "run")}
    missing_root = tmp_path / "no-fallback"
    assert paths.runtime_dir(env, fallback_root=missing_root) == tmp_path / "run/ember"


@pytest.mark.parametrize(
    "env", [{}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "run"}]
)
def test_runtime_falls_back_to_uid_folder(env: dict[str, str], tmp_path: Path) -> None:
    uid_folder = tmp_path / str(os.getuid())
    uid_folder.mkdir()
    assert paths.runtime_dir(env, fallback_root=tmp_path) == uid_folder / "ember"


@pytest.mark.parametrize(
    "env", [{}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "run"}]
)
def test_runtime_fallback_absent_raises(env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(paths.RuntimeDirError, match="XDG_RUNTIME_DIR"):
        paths.runtime_dir(env, fallback_root=tmp_path)


def test_ensure_private_dir_creates_folders_at_700(tmp_path: Path) -> None:
    outside = tmp_path / "config"
    root = outside / "ember"
    target = root / "runs" / "one"
    assert paths.ensure_private_dir(target, root=root) == target
    assert _mode(root) == 0o700
    assert _mode(root / "runs") == 0o700
    assert _mode(target) == 0o700
    assert outside.is_dir()


def test_ensure_private_dir_corrects_wide_ember_folder(tmp_path: Path) -> None:
    outside = tmp_path / "config"
    outside.mkdir()
    outside.chmod(0o755)
    root = outside / "ember"
    root.mkdir()
    root.chmod(0o755)
    paths.ensure_private_dir(root, root=root)
    assert _mode(root) == 0o700
    assert _mode(outside) == 0o755


def test_ensure_private_dir_rejects_path_outside_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not inside"):
        paths.ensure_private_dir(tmp_path / "other", root=tmp_path / "ember")


def test_ensure_private_dir_rejects_file_on_the_way(tmp_path: Path) -> None:
    root = tmp_path / "ember"
    root.mkdir()
    (root / "runs").write_text("")
    with pytest.raises(NotADirectoryError):
        paths.ensure_private_dir(root / "runs", root=root)


def test_open_private_file_creates_at_600(tmp_path: Path) -> None:
    target = tmp_path / "settings.db"
    fd = paths.open_private_file(target)
    try:
        os.write(fd, b"data")
    finally:
        os.close(fd)
    assert _mode(target) == 0o600
    assert target.read_bytes() == b"data"


def test_open_private_file_corrects_existing_644(tmp_path: Path) -> None:
    target = tmp_path / "settings.db"
    target.write_bytes(b"kept")
    target.chmod(0o644)
    fd = paths.open_private_file(target, os.O_RDONLY)
    try:
        assert os.read(fd, 10) == b"kept"
    finally:
        os.close(fd)
    assert _mode(target) == 0o600


def test_open_private_file_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_bytes(b"")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        paths.open_private_file(link)


def test_umask_zero_still_gives_700_and_600(tmp_path: Path) -> None:
    root = tmp_path / "ember"
    target = root / "runs"
    previous = os.umask(0)
    try:
        paths.ensure_private_dir(target, root=root)
        fd = paths.open_private_file(target / "record")
        os.close(fd)
    finally:
        os.umask(previous)
    assert _mode(root) == 0o700
    assert _mode(target) == 0o700
    assert _mode(target / "record") == 0o600
