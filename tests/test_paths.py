import os
import pwd
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from unittest.mock import Mock

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
        ("XDG_DATA_HOME", paths.data_dir),
        ("XDG_CACHE_HOME", paths.cache_dir),
    ],
)
def test_absolute_variable_is_used(
    variable: str, function: PathFunction, home: Path, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    env = {"HOME": str(home), variable: str(base)}
    assert function(env) == base / "ember"


@pytest.mark.parametrize("kind", ["folder", "file", "symlink", "dangling"])
@pytest.mark.parametrize("late", [False, True])
def test_record_creation_refuses_existing_destination(
    tmp_path: Path, kind: str, late: bool
) -> None:
    directory = tmp_path / "record"

    def occupy() -> None:
        if kind == "folder":
            directory.mkdir()
        elif kind == "file":
            directory.write_text("keep")
        else:
            target = tmp_path / "target"
            if kind == "symlink":
                target.mkdir()
            directory.symlink_to(target, target_is_directory=True)

    if not late:
        occupy()
    with (
        pytest.raises(FileExistsError),
        paths.create_record_dir(directory, root=tmp_path) as temporary,
    ):
        (temporary / "record.json").write_text("{}")
        occupy()
    assert os.path.lexists(directory)
    assert not list(tmp_path.glob(f"{paths.CREATION_TMP_PREFIX}*"))
    if kind == "file":
        assert directory.read_text() == "keep"
    elif kind == "folder":
        assert list(directory.iterdir()) == []
    else:
        assert directory.is_symlink()


def test_record_creation_rename_failure_removes_temporary_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "rename", Mock(side_effect=OSError("rename failed")))
    with (
        pytest.raises(OSError, match="rename failed"),
        paths.create_record_dir(tmp_path / "record", root=tmp_path) as temporary,
    ):
        (temporary / "record.json").write_text("{}")
    assert list(tmp_path.iterdir()) == []


DEFAULTS = [
    ("XDG_CONFIG_HOME", paths.config_dir, (".config",)),
    ("XDG_STATE_HOME", paths.state_dir, (".local", "state")),
    ("XDG_DATA_HOME", paths.data_dir, (".local", "share")),
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


def test_empty_home_falls_back_to_password_database() -> None:
    expected = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert paths.home_dir({"HOME": ""}) == expected


def test_absent_home_falls_back_to_password_database() -> None:
    expected = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert paths.home_dir({}) == expected


def test_named_paths(home: Path, tmp_path: Path) -> None:
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    assert paths.settings_db_path(env) == tmp_path / "config/ember/settings.db"
    assert paths.runs_dir(env) == tmp_path / "state/ember/runs"


def test_worker_cache_dir(home: Path, tmp_path: Path) -> None:
    assert paths.worker_cache_dir({"HOME": str(home)}) == home / ".cache/ember/worker"
    env = {"HOME": str(home), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    assert paths.worker_cache_dir(env) == tmp_path / "cache/ember/worker"


def test_ensure_worker_cache_dir_creates_it_at_700(home: Path) -> None:
    env = {"HOME": str(home)}
    folder = paths.ensure_worker_cache_dir(env)
    assert folder == home / ".cache/ember/worker"
    assert _mode(folder) == 0o700
    assert _mode(home / ".cache/ember") == 0o700


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


@pytest.mark.parametrize("below", [Path(".."), Path("..") / "x", Path("a/../../x")])
def test_ensure_private_dir_rejects_parent_reference(
    below: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "config"
    root = outside / "ember"
    root.mkdir(parents=True)
    outside.chmod(0o755)
    with pytest.raises(ValueError, match="not inside"):
        paths.ensure_private_dir(root / below, root=root)
    assert _mode(outside) == 0o755
    assert not (outside / "x").exists()


def test_ensure_private_dir_creates_missing_ancestors_at_700(tmp_path: Path) -> None:
    root = tmp_path / "local" / "state" / "ember"
    previous = os.umask(0)
    try:
        paths.ensure_private_dir(root, root=root)
    finally:
        os.umask(previous)
    assert _mode(tmp_path / "local") == 0o700
    assert _mode(tmp_path / "local" / "state") == 0o700
    assert _mode(root) == 0o700


def test_ensure_private_dir_keeps_existing_ancestor_modes(tmp_path: Path) -> None:
    existing = tmp_path / "local"
    existing.mkdir()
    existing.chmod(0o755)
    root = existing / "state" / "ember"
    paths.ensure_private_dir(root, root=root)
    assert _mode(existing) == 0o755
    assert _mode(existing / "state") == 0o700


def test_ensure_private_dir_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "ember"
    root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (root / "runs").symlink_to(real)
    with pytest.raises(NotADirectoryError, match="symlink"):
        paths.ensure_private_dir(root / "runs", root=root)


def test_worktree_path_is_in_the_data_folder(home: Path, tmp_path: Path) -> None:
    env = {"HOME": str(home)}
    expected = home / ".local/share/ember/worktrees/codex-20261104-000000-abcdef"
    assert paths.worktrees_dir(env) == home / ".local/share/ember/worktrees"
    assert paths.worktree_path("codex-20261104-000000-abcdef", env) == expected
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    expected = tmp_path / "data/ember/worktrees/flow-1"
    assert paths.worktree_path("flow-1", env) == expected


def test_worktree_path_resolves_symlinks(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "link")}
    assert paths.worktree_path("run-1", env) == real / "ember/worktrees/run-1"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\0b"])
def test_worktree_path_refuses_a_name_that_is_not_an_id(home: Path, name: str) -> None:
    with pytest.raises(ValueError, match="not a worktree id"):
        paths.worktree_path(name, {"HOME": str(home)})


@pytest.mark.parametrize("setting", ["XDG_DATA_HOME", "HOME"])
def test_check_worktree_path_refuses_a_path_inside_a_repository_folder(
    tmp_path: Path, setting: str
) -> None:
    checkout = tmp_path / "project"
    checkout.mkdir()
    if setting == "HOME":
        env = {"HOME": str(checkout)}
    else:
        env = {"HOME": str(tmp_path / "home"), setting: str(checkout / "data")}
    worktree = paths.worktree_path("run-1", env)
    other = tmp_path / "other"
    paths.check_worktree_path(worktree, [other], env)
    with pytest.raises(paths.WorktreeDirError) as caught:
        paths.check_worktree_path(worktree, [other, checkout], env)
    assert str(caught.value) == (
        f"the worktree folder {worktree.parent} is inside the repository folder "
        f"{checkout}; {setting} places it there"
    )


def test_check_worktree_path_accepts_no_folders(home: Path) -> None:
    env = {"HOME": str(home)}
    paths.check_worktree_path(paths.worktree_path("run-1", env), [], env)


def test_ensure_worktrees_dir_creates_it_at_700(home: Path, tmp_path: Path) -> None:
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "data")}
    folder = paths.ensure_worktrees_dir(env)
    assert folder == tmp_path / "data/ember/worktrees"
    assert _mode(folder) == 0o700
    assert _mode(folder.parent) == 0o700


@pytest.mark.parametrize("name", ["ember", "ember/worktrees"])
def test_ensure_worktrees_dir_accepts_a_private_symlink_target(
    home: Path, tmp_path: Path, name: str
) -> None:
    data = tmp_path / "data"
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    link = data / name
    link.parent.mkdir(parents=True, mode=0o700)
    link.symlink_to(target, target_is_directory=True)
    env = {"HOME": str(home), "XDG_DATA_HOME": str(data)}
    expected = target / "worktrees" if name == "ember" else target
    assert paths.ensure_worktrees_dir(env) == expected
    assert paths.worktree_path("run-1", env) == expected / "run-1"
    assert _mode(expected) == 0o700
    assert link.is_symlink()


@pytest.mark.parametrize("name", ["ember", "ember/worktrees"])
@pytest.mark.parametrize("mode", [0o755, 0o770, 0o1700])
def test_ensure_worktrees_dir_refuses_a_symlink_target_without_mode_700(
    home: Path, tmp_path: Path, name: str, mode: int
) -> None:
    data = tmp_path / "data"
    target = tmp_path / "public"
    target.mkdir()
    target.chmod(mode)
    link = data / name
    link.parent.mkdir(parents=True, mode=0o700)
    link.symlink_to(target, target_is_directory=True)
    env = {"HOME": str(home), "XDG_DATA_HOME": str(data)}
    with pytest.raises(PermissionError, match=f"{target} must have mode 700"):
        paths.ensure_worktrees_dir(env)
    assert _mode(target) == mode


@pytest.mark.parametrize("name", ["ember", "ember/worktrees"])
def test_ensure_worktrees_dir_refuses_another_owners_symlink_target(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    data = tmp_path / "data"
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    link = data / name
    link.parent.mkdir(parents=True, mode=0o700)
    link.symlink_to(target, target_is_directory=True)
    env = {"HOME": str(home), "XDG_DATA_HOME": str(data)}
    owner = os.getuid()
    monkeypatch.setattr(paths.os, "getuid", lambda: owner + 1)
    with pytest.raises(PermissionError, match="not owned by the current user"):
        paths.ensure_worktrees_dir(env)


@pytest.mark.parametrize("kind", ["file", "missing"])
def test_ensure_worktrees_dir_refuses_a_symlink_to_a_nonfolder(
    tmp_path: Path, kind: str
) -> None:
    target = tmp_path / "target"
    if kind == "file":
        target.write_text("")
    data = tmp_path / "data"
    data.mkdir()
    (data / "ember").symlink_to(target)
    with pytest.raises(OSError):
        paths.ensure_worktrees_dir({"XDG_DATA_HOME": str(data)})


@pytest.mark.parametrize("marker", ["folder", "absolute-file", "relative-file"])
@pytest.mark.parametrize("symlink", [False, True])
def test_check_worktree_path_refuses_an_unrelated_git_working_tree(
    home: Path, tmp_path: Path, marker: str, symlink: bool
) -> None:
    checkout = tmp_path / "unrelated"
    checkout.mkdir()
    dotgit = checkout / ".git"
    if marker == "folder":
        git = dotgit
        common = git
    else:
        common = tmp_path / "other.git"
        git = common / "worktrees/linked"
        pointer = (
            git if marker == "absolute-file" else Path("../other.git/worktrees/linked")
        )
        dotgit.write_text(f"gitdir: {pointer}\n")
        git.mkdir(parents=True)
        (git / "commondir").write_text("../..\n")
    (common / "objects").mkdir(parents=True)
    (common / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    data = checkout / "data"
    if symlink:
        link = tmp_path / "link"
        link.symlink_to(checkout, target_is_directory=True)
        data = link / "data"
    env = {"HOME": str(home), "XDG_DATA_HOME": str(data)}
    worktree = paths.worktree_path("run-1", env)
    with pytest.raises(paths.WorktreeDirError) as caught:
        paths.check_worktree_path(worktree, [tmp_path / "source"], env)
    assert str(caught.value) == (
        f"the worktree folder {worktree.parent} is inside the repository folder "
        f"{checkout}; XDG_DATA_HOME places it there"
    )


def test_check_worktree_path_refuses_a_worktrees_folder_that_is_a_checkout(
    tmp_path: Path,
) -> None:
    env = {"XDG_DATA_HOME": str(tmp_path)}
    root = paths.worktrees_dir(env)
    (root / ".git/objects").mkdir(parents=True)
    (root / ".git/refs").mkdir()
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(paths.WorktreeDirError, match=str(root)):
        paths.check_worktree_path(paths.worktree_path("run-1", env), [], env)


@pytest.mark.parametrize("marker", ["empty-folder", "file", "missing-gitdir"])
def test_check_worktree_path_accepts_a_folder_with_invalid_git_metadata(
    tmp_path: Path, marker: str
) -> None:
    if marker == "empty-folder":
        (tmp_path / ".git").mkdir()
    else:
        (tmp_path / ".git").write_text(
            "not a git file\n" if marker == "file" else "gitdir: missing\n"
        )
    env = {"XDG_DATA_HOME": str(tmp_path)}
    paths.check_worktree_path(paths.worktree_path("run-1", env), [], env)


def test_run_tmp_dir_uses_short_suffix(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "RUN_TMP_ROOT", Path("/tmp"))
    assert paths.run_tmp_dir(
        "codex-20261108-031019-f00121", {"HOME": str(home)}
    ) == Path("/tmp/ember-f00121")


@pytest.mark.parametrize("ident", ["", ".", "..", "a/b", "a\0b", "run", "12345g"])
def test_run_tmp_dir_refuses_invalid_ids(home: Path, ident: str) -> None:
    with pytest.raises(ValueError):
        paths.run_tmp_dir(ident, {"HOME": str(home)})


def test_ensure_run_tmp_dir_sets_private_modes(home: Path) -> None:
    env = {"HOME": str(home)}
    parent = paths.RUN_TMP_ROOT
    parent.chmod(0o777)
    before = os.umask(0o777)
    try:
        first = paths.ensure_run_tmp_dir("codex-20260101-000000-aabbcc", env)
        second = paths.ensure_run_tmp_dir("codex-20260101-000000-ddeeff", env)
    finally:
        os.umask(before)
    assert first != second
    assert all(_mode(folder) == 0o700 for folder in (first, second))
    assert _mode(parent) == 0o777


@pytest.mark.parametrize("kind", ["folder", "file", "symlink", "dangling"])
def test_ensure_run_tmp_dir_refuses_existing_entries(
    home: Path, tmp_path: Path, kind: str
) -> None:
    env = {"HOME": str(home)}
    target = tmp_path / "outside"
    target.mkdir(mode=0o755)
    folder = paths.run_tmp_dir("codex-20260101-000000-aabbcc", env)
    if kind == "folder":
        folder.mkdir(mode=0o755)
    elif kind == "file":
        folder.write_text("keep")
    else:
        folder.symlink_to(target if kind == "symlink" else target / "missing")
    with pytest.raises(FileExistsError):
        paths.ensure_run_tmp_dir("codex-20260101-000000-aabbcc", env)
    assert _mode(target) == 0o755
    if kind == "folder":
        assert _mode(folder) == 0o755
    elif kind == "file":
        assert folder.read_text() == "keep"


@pytest.mark.parametrize("link_at_root", [False, True])
def test_remove_run_tmp_dir_does_not_follow_symlinks(
    home: Path, tmp_path: Path, link_at_root: bool
) -> None:
    env = {"HOME": str(home)}
    ident = "codex-20260101-000000-aabbcc"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("keep")
    folder = paths.run_tmp_dir(ident, env)
    if link_at_root:
        folder.symlink_to(outside, target_is_directory=True)
    else:
        folder.mkdir()
        (folder / "link").symlink_to(outside, target_is_directory=True)
        (folder / "file").write_text("temporary")
    assert paths.remove_run_tmp_dir(ident, env)
    assert not os.path.lexists(folder)
    assert sentinel.read_text() == "keep"
    assert not paths.remove_run_tmp_dir(ident, env)


def test_remove_run_tmp_dir_refuses_a_symlinked_parent(
    home: Path, tmp_path: Path
) -> None:
    env = {"HOME": str(home)}
    outside = tmp_path / "outside"
    (outside / "ember-aabbcc").mkdir(parents=True)
    parent = paths.RUN_TMP_ROOT
    parent.rmdir()
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        paths.remove_run_tmp_dir("codex-20260101-000000-aabbcc", env)
    assert (outside / "ember-aabbcc").is_dir()


def test_run_temporary_helpers_accept_a_private_base(tmp_path: Path) -> None:
    ident = "codex-20260101-000000-aabbcc"
    folder = paths.ensure_run_tmp_dir(ident, base=tmp_path)
    assert folder == tmp_path / "ember-aabbcc"
    assert paths.run_tmp_dir(ident, base=tmp_path) == folder
    assert paths.remove_run_tmp_dir(ident, base=tmp_path)
    assert not folder.exists()


def test_clean_run_tmp_dir_keeps_selected_names_and_codex_entries(
    tmp_path: Path,
) -> None:
    ident = "codex-20260101-000000-aabbcc"
    folder = paths.ensure_run_tmp_dir(ident, base=tmp_path)
    keep = {"config", "data", "state"}
    for name in {*keep, "codex-session"}:
        (folder / name).mkdir()
        (folder / name / "saved").touch()
    (folder / "codex-log").touch()
    (folder / "file").touch()
    nested = folder / "pytest" / "nested"
    nested.mkdir(parents=True)
    (nested / "result").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "untouched").touch()
    (nested / "link").symlink_to(outside)
    (folder / "link").symlink_to(outside)

    for _ in range(2):
        paths.clean_run_tmp_dir(ident, keep, base=tmp_path)
        assert {entry.name for entry in folder.iterdir()} == {
            *keep,
            "codex-session",
            "codex-log",
        }
        assert (outside / "untouched").is_file()
        for name in {*keep, "codex-session"}:
            assert (folder / name / "saved").is_file()


@pytest.mark.parametrize("at_parent", [False, True])
def test_clean_run_tmp_dir_refuses_symlinked_folder(
    tmp_path: Path, at_parent: bool
) -> None:
    ident = "codex-20260101-000000-aabbcc"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    outside.mkdir()
    if at_parent:
        base.symlink_to(outside)
        target = paths.ensure_run_tmp_dir(ident, base=outside)
    else:
        base.mkdir()
        paths.run_tmp_dir(ident, base=base).symlink_to(outside)
        target = outside
    (target / "saved").touch()
    with pytest.raises(OSError):
        paths.clean_run_tmp_dir(ident, [], base=base)
    assert (target / "saved").is_file()


def test_clean_run_tmp_dir_ignores_a_missing_folder(tmp_path: Path) -> None:
    ident = "codex-20260101-000000-aabbcc"
    for base in (tmp_path, tmp_path / "missing"):
        paths.clean_run_tmp_dir(ident, [], base=base)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "invalid",
    ["flow-20260101-000000-aabbcc", "panel-20260101-000000-aabbcc", "codex-aabbcc"],
)
def test_clean_run_tmp_dir_refuses_invalid_id_with_matching_suffix(
    tmp_path: Path, invalid: str
) -> None:
    folder = paths.ensure_run_tmp_dir("codex-20260101-000000-aabbcc", base=tmp_path)
    leftover = folder / "leftover"
    leftover.touch()
    with pytest.raises(ValueError, match="run id"):
        paths.clean_run_tmp_dir(invalid, [], base=tmp_path)
    assert leftover.is_file()


def test_clean_run_tmp_dir_continues_after_removal_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = "codex-20260101-000000-aabbcc"
    folder = paths.ensure_run_tmp_dir(ident, base=tmp_path)
    for name in ("first", "second", "removable"):
        (folder / name).touch()
    unlink = os.unlink
    errors: list[str] = []

    def fail_two(name: str, *, dir_fd: int) -> None:
        if name != "removable":
            errors.append(name)
            raise PermissionError(name)
        unlink(name, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_two)
        with pytest.raises(PermissionError) as caught:
            paths.clean_run_tmp_dir(ident, [], base=tmp_path)
    assert str(caught.value) == errors[0]
    assert (
        set(errors) == {entry.name for entry in folder.iterdir()} == {"first", "second"}
    )


def test_check_tmp_dir_uses_full_id_and_private_mode(tmp_path: Path) -> None:
    flow = "flow-20261121-172129-05d358"
    prev = os.umask(0o777)
    try:
        folder = paths.ensure_check_tmp_dir(flow, base=tmp_path)
    finally:
        os.umask(prev)
    assert folder == tmp_path / f"ember-{flow}"
    assert not paths.RUN_TMP_PATTERN.fullmatch(folder.name)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert paths.remove_check_tmp_dir(flow, base=tmp_path)
    assert not paths.remove_check_tmp_dir(flow, base=tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "file", "foreign"])
def test_check_tmp_helpers_leave_unowned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    flow = "flow-20261121-172129-05d358"
    folder = paths.check_tmp_dir(flow, base=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "saved").touch()
    if kind == "symlink":
        folder.symlink_to(outside)
    elif kind == "file":
        folder.touch()
    else:
        folder.mkdir()
        foreign = os.getuid() + 1
        monkeypatch.setattr(os, "getuid", lambda: foreign)
    for operation in (paths.ensure_check_tmp_dir, paths.remove_check_tmp_dir):
        with pytest.raises(PermissionError, match=str(folder)):
            operation(flow, base=tmp_path)
        assert folder.exists() and (outside / "saved").is_file()
