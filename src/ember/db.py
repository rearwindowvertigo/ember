"""The settings database: opening it, transactions, schema and generation.

The database is the SQLite file `paths.settings_db_path()`, mode 600 in a
mode 700 folder, in WAL journal mode with a busy timeout of 5 seconds. Writes
run in `BEGIN IMMEDIATE` transactions. Reads open the file read-only and never
create it.
"""

import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from ember import paths

SCHEMA_VERSION = 1
"""The schema version this module reads and writes."""

BUSY_TIMEOUT_MS = 5000
"""How long a statement waits for a lock held by another connection."""

EX_IOERR = 74
EX_TEMPFAIL = 75
EX_CONFIG = 78

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE once (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value TEXT NOT NULL,
    set_at REAL NOT NULL
);
CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'none', 'gone')),
    at REAL NOT NULL,
    copy TEXT
);
"""

_create_lock = threading.Lock()
"""Held while this process creates the database file and closes its
descriptor, and while a reader checks for the file and opens it."""

_BUSY_RETRY_SECONDS = 0.01
"""The pause between retries of a statement that SQLite reported busy."""


class DatabaseError(Exception):
    """A settings database failure, with the exit code `emb` uses for it."""

    exit_code = EX_IOERR


class DatabaseUnavailableError(DatabaseError):
    """The database cannot be opened or read."""

    exit_code = EX_IOERR


class DatabaseBusyError(DatabaseError):
    """Another connection held the database lock past the busy timeout."""

    exit_code = EX_TEMPFAIL


class UnsupportedSchemaError(DatabaseError):
    """The database's schema row is missing, invalid or not version 1."""

    exit_code = EX_CONFIG


class StaleGenerationError(DatabaseError):
    """A write expected a generation other than the stored one."""

    exit_code = EX_TEMPFAIL


@contextmanager
def translate_errors() -> Iterator[None]:
    """Turn SQLite and file system errors into `DatabaseError` subclasses.

    Yields:
        Nothing.

    Raises:
        DatabaseBusyError: SQLite reported the database busy or locked.
        DatabaseUnavailableError: Any other SQLite or file system error.
    """
    try:
        yield
    except sqlite3.Error as error:
        if _is_busy(error):
            raise DatabaseBusyError(f"settings database is busy: {error}") from error
        raise DatabaseUnavailableError(f"settings database: {error}") from error
    except OSError as error:
        raise DatabaseUnavailableError(f"settings database: {error}") from error


def _prepare_file(path: Path) -> None:
    """Create the database file at mode 600, or set an existing one to 600.

    An existing file is never opened here, because closing a descriptor drops
    every POSIX lock this process holds on the file.
    """
    with _create_lock:
        try:
            fd = paths.open_private_file(path, os.O_RDWR | os.O_EXCL)
        except FileExistsError:
            pass
        else:
            os.close(fd)
            return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise DatabaseUnavailableError(
            f"settings database: {path} is not a regular file"
        )
    if stat.S_IMODE(info.st_mode) != paths.FILE_MODE:
        path.chmod(paths.FILE_MODE)


def _is_busy(error: sqlite3.Error) -> bool:
    name = getattr(error, "sqlite_errorname", "")
    return name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED"))


def _retry_busy(conn: sqlite3.Connection, sql: str) -> None:
    """Run `sql`, retrying while SQLite reports busy, up to the busy timeout.

    Changing the journal mode of a new or empty database file reports busy at
    once, without waiting for the busy timeout.

    Raises:
        DatabaseBusyError: The database stayed busy for the busy timeout.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as error:
            if not _is_busy(error):
                raise
            if time.monotonic() >= deadline:
                raise DatabaseBusyError(
                    f"settings database is busy: {error}"
                ) from error
            time.sleep(_BUSY_RETRY_SECONDS)
        else:
            return


@contextmanager
def open_write(env: Mapping[str, str] | None = None) -> Iterator[sqlite3.Connection]:
    """Open the settings database for writing, creating it if needed.

    The configuration folder is created at mode 700 and the file at mode 600
    before SQLite opens it.

    Args:
        env: The environment; `None` reads `os.environ`.

    Yields:
        A connection in autocommit mode, closed on exit.
    """
    path = paths.settings_db_path(env)
    paths.ensure_private_dir(path.parent, root=paths.config_dir(env))
    _prepare_file(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _retry_busy(conn, "PRAGMA journal_mode = WAL")
        yield conn
    finally:
        conn.close()


@contextmanager
def open_read(
    env: Mapping[str, str] | None = None,
) -> Iterator[sqlite3.Connection | None]:
    """Open the settings database read-only.

    Args:
        env: The environment; `None` reads `os.environ`.

    Yields:
        A read-only connection in autocommit mode, closed on exit, or `None`
        when the database file does not exist.
    """
    path = paths.settings_db_path(env)
    with _create_lock:
        if not path.exists():
            conn = None
        else:
            conn = sqlite3.connect(
                path.absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None
            )
    if conn is None:
        yield None
        return
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction with `BEGIN IMMEDIATE`.

    Args:
        conn: A connection in autocommit mode.

    Yields:
        `conn`. The transaction commits when the block ends and rolls back
        when it raises any exception.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a deferred read transaction, so every read sees one snapshot.

    Args:
        conn: A connection in autocommit mode.

    Yields:
        `conn`. The transaction ends when the block ends.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.rollback()


def _has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'")
    return row.fetchone()[0] > 0


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        raise UnsupportedSchemaError("settings database has no schema row") from error
    return None if row is None else row[0]


def _check_schema(conn: sqlite3.Connection) -> int:
    """Check the schema row and return the generation."""
    schema = _meta(conn, "schema")
    if schema != str(SCHEMA_VERSION):
        found = "missing" if schema is None else repr(schema)
        raise UnsupportedSchemaError(
            f"settings database schema is {found}; this Ember reads schema "
            f"{SCHEMA_VERSION}"
        )
    generation = _meta(conn, "generation")
    if generation is None or not generation.isdecimal():
        raise UnsupportedSchemaError("settings database generation row is invalid")
    return int(generation)


def read_generation(conn: sqlite3.Connection) -> int | None:
    """Check the schema inside a read transaction and return the generation.

    Args:
        conn: A connection inside a transaction.

    Returns:
        The generation, or `None` when the database has no tables yet.

    Raises:
        UnsupportedSchemaError: The schema row is missing, invalid or not 1.
    """
    if not _has_tables(conn):
        return None
    return _check_schema(conn)


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Create schema version 1 if the database is empty, and return the generation.

    Args:
        conn: A connection inside a write transaction.

    Returns:
        The generation.

    Raises:
        UnsupportedSchemaError: The schema row is missing, invalid or not 1.
    """
    if not _has_tables(conn):
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("schema", str(SCHEMA_VERSION)),
                ("generation", "0"),
                ("opencode_id_format", "1"),
            ],
        )
    return _check_schema(conn)


def bump_generation(conn: sqlite3.Connection) -> int:
    """Raise the generation by one.

    Args:
        conn: A connection inside a write transaction.

    Returns:
        The new generation.
    """
    generation = _check_schema(conn) + 1
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'generation'", (str(generation),)
    )
    return generation
