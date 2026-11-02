import os
from pathlib import Path

import pytest

from ember import autostart


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    emb = bin_dir / "emb"
    emb.write_text("#!/bin/sh\n")
    emb.chmod(0o755)
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": str(bin_dir),
    }


def test_path_follows_xdg_config_home(env: dict[str, str]) -> None:
    assert autostart.path(env) == (
        Path(env["XDG_CONFIG_HOME"])
        / "autostart"
        / "io.github.rearwindowvertigo.Ember.desktop"
    )


def test_on_writes_the_entry(env: dict[str, str]) -> None:
    assert autostart.sync(True, env) is None
    path = autostart.path(env)
    emb = Path(env["PATH"]) / "emb"
    assert path.read_text() == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Ember\n"
        f"Exec={emb} app --hidden\n"
        "Icon=io.github.rearwindowvertigo.Ember\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_unchanged_entry_is_not_rewritten(env: dict[str, str]) -> None:
    assert autostart.sync(True, env) is None
    path = autostart.path(env)
    before = path.stat()
    os.utime(path, ns=(1, 1))
    assert autostart.sync(True, env) is None
    after = path.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == 1


def test_a_different_entry_is_replaced(env: dict[str, str]) -> None:
    path = autostart.path(env)
    path.parent.mkdir(parents=True)
    path.write_text("[Desktop Entry]\nExec=/old/emb app --hidden\n")
    assert autostart.sync(True, env) is None
    assert "Exec=/old/emb" not in path.read_text()
    assert "app --hidden" in path.read_text()


def test_off_removes_the_entry(env: dict[str, str]) -> None:
    assert autostart.sync(True, env) is None
    assert autostart.sync(False, env) is None
    assert not autostart.path(env).exists()
    assert autostart.sync(False, env) is None


def test_emb_not_found_writes_nothing(env: dict[str, str], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    reason = autostart.sync(True, {**env, "PATH": str(empty)})
    assert reason is not None
    assert "emb is not on PATH" in reason
    assert not autostart.path(env).exists()


def test_write_error_returns_a_reason(env: dict[str, str]) -> None:
    config = Path(env["XDG_CONFIG_HOME"])
    config.mkdir()
    (config / "autostart").write_text("a file where the folder should be")
    reason = autostart.sync(True, env)
    assert reason is not None
    assert reason.startswith("cannot write")


def test_exec_quotes_a_path_with_spaces() -> None:
    assert 'Exec="/opt/my apps/emb" app --hidden\n' in autostart.entry(
        "/opt/my apps/emb"
    )
    assert "Exec=/opt/100%%/emb app" in autostart.entry("/opt/100%/emb")
