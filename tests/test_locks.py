import fcntl
import os
from pathlib import Path

from ember import locks, paths


def lock_line(path: Path) -> str:
    info = path.stat()
    return (
        f"1: FLOCK ADVISORY WRITE 123 {os.major(info.st_dev):02x}:"
        f"{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n"
    )


def test_lock_parser_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    path.touch()
    held = locks.held_locks(
        lock_line(path) + "2: FLOCK ADVISORY WRITE 1 0a:ff:42 0 EOF\n"
        "3: POSIX ADVISORY WRITE 1 00:01:3 0 EOF\n"
        "4: -> FLOCK ADVISORY WRITE 1 00:01:4 0 EOF\n"
        "5: FLOCK bad\n6: FLOCK ADVISORY WRITE 1 invalid 0 EOF\n"
        "7: FLOCK ADVISORY WRITE 1 zz:01:2 0 EOF\n"
    )
    assert len(held) == 2
    assert (10, 255, 42) in held
    assert locks.lock_held(path, held) is True
    assert locks.lock_held(path, set()) is False
    assert locks.lock_held(tmp_path / "absent", held) is False
    assert locks.lock_held(path, None) is None


def test_read_held_without_a_table(tmp_path: Path) -> None:
    assert locks.read_held(tmp_path / "absent") is None
    table = tmp_path / "locks"
    table.write_text("")
    assert locks.read_held(table) == set()


def test_lifetime_held_reads_the_kernel_table(tmp_path: Path) -> None:
    env = {"XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    owner = "flow-20260101-120000-abcdef"
    assert locks.lifetime_held(owner, env) is False
    assert not (tmp_path / "runtime").exists()
    path = paths.runtime_dir(env) / "locks" / f"{owner}.lock"
    path.parent.mkdir(parents=True)
    with path.open("w") as handle:
        assert locks.lifetime_held(owner, env) is False
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert locks.lifetime_held(owner, env) is True
        assert locks.lifetime_held(owner, env, tmp_path / "absent") is None
