import os
import sqlite3
import stat
from pathlib import Path

import pytest

from ember import db, paths


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "config")}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_open_write_creates_private_folder_and_file(tmp_path: Path) -> None:
    env = _env(tmp_path)
    old_umask = os.umask(0)
    try:
        with db.open_write(env) as conn, db.transaction(conn):
            db.ensure_schema(conn)
    finally:
        os.umask(old_umask)
    path = paths.settings_db_path(env)
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    for extra in path.parent.iterdir():
        assert _mode(extra) == 0o600


def test_open_write_sets_existing_file_to_600(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = paths.settings_db_path(env)
    path.parent.mkdir(parents=True)
    path.touch()
    path.chmod(0o644)
    with db.open_write(env) as conn, db.transaction(conn):
        db.ensure_schema(conn)
    assert _mode(path) == 0o600


def test_open_write_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    with db.open_write(_env(tmp_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_open_read_of_missing_database_creates_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with db.open_read(env) as conn:
        assert conn is None
    assert not (tmp_path / "config").exists()


def test_open_read_is_read_only(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with db.open_write(env) as conn, db.transaction(conn):
        db.ensure_schema(conn)
    with db.open_read(env) as conn:
        assert conn is not None
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM meta")


def test_schema_is_created_with_generation_0(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with db.open_write(env) as conn, db.transaction(conn):
        assert db.ensure_schema(conn) == 0
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    assert rows == {"schema": "1", "generation": "0", "opencode_id_format": "1"}


def test_transaction_commits_on_success(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with db.open_write(env) as conn:
        with db.transaction(conn):
            db.ensure_schema(conn)
            assert db.bump_generation(conn) == 1
        with db.read_transaction(conn):
            assert db.read_generation(conn) == 1


def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with db.open_write(env) as conn:
        with db.transaction(conn):
            db.ensure_schema(conn)
        with pytest.raises(KeyError), db.transaction(conn):
            db.bump_generation(conn)
            raise KeyError("boom")
        assert not conn.in_transaction
        with db.read_transaction(conn):
            assert db.read_generation(conn) == 0


def test_read_generation_of_empty_database_is_none(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = paths.settings_db_path(env)
    path.parent.mkdir(parents=True)
    path.touch()
    with db.open_read(env) as conn:
        assert conn is not None
        with db.read_transaction(conn):
            assert db.read_generation(conn) is None


@pytest.mark.parametrize(
    "statements",
    [
        ["CREATE TABLE other (x)"],
        ["CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"],
        [
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO meta VALUES ('schema', '2'), ('generation', '0')",
        ],
        [
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO meta VALUES ('schema', 'one'), ('generation', '0')",
        ],
        [
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO meta VALUES ('schema', '1'), ('generation', 'x')",
        ],
    ],
)
def test_unsupported_schema_is_refused(tmp_path: Path, statements: list[str]) -> None:
    env = _env(tmp_path)
    path = paths.settings_db_path(env)
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()
    with db.open_read(env) as read_conn:
        assert read_conn is not None
        with db.read_transaction(read_conn), pytest.raises(db.UnsupportedSchemaError):
            db.read_generation(read_conn)
    with (
        db.open_write(env) as write_conn,
        pytest.raises(db.UnsupportedSchemaError),
        db.transaction(write_conn),
    ):
        db.ensure_schema(write_conn)


def test_translate_errors_maps_busy_and_other_errors() -> None:
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorname = "SQLITE_BUSY"
    with pytest.raises(db.DatabaseBusyError), db.translate_errors():
        raise busy
    with pytest.raises(db.DatabaseUnavailableError), db.translate_errors():
        raise sqlite3.DatabaseError("file is not a database")
    with pytest.raises(db.DatabaseUnavailableError), db.translate_errors():
        raise PermissionError("denied")


def test_exit_codes() -> None:
    assert db.DatabaseUnavailableError.exit_code == 74
    assert db.DatabaseBusyError.exit_code == 75
    assert db.StaleGenerationError.exit_code == 75
    assert db.UnsupportedSchemaError.exit_code == 78
