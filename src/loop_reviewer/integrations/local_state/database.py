"""Open private SQLite state and manage schema and transaction lifetimes."""

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from loop_reviewer.integrations.local_state.inbox_schema import (
    add_inbox_scan_version,
    add_title_exclusions,
    create_inbox_tables,
    rename_inbox_projects,
)
from loop_reviewer.integrations.local_state.review_schema import (
    add_generation_metadata,
    add_reviewer_report,
    migrate_reviews,
)

_MIGRATIONS = (
    (6, migrate_reviews),
    (7, create_inbox_tables),
    (8, rename_inbox_projects),
    (9, add_generation_metadata),
    (10, add_reviewer_report),
    (11, add_title_exclusions),
    (12, add_inbox_scan_version),
)
_SCHEMA_VERSION = _MIGRATIONS[-1][0]


class UnsupportedSchemaVersion(Exception):
    pass


class LocalDatabase:
    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def read(self) -> Generator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            yield connection

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Generator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _connect(self) -> sqlite3.Connection:
        self._prepare_directory()
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        if os.name == "posix":
            os.chmod(self._path, 0o600)
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            self._initialize(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _prepare_directory(self) -> None:
        directory = self._path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(directory, 0o700)

    def _initialize(self, connection: sqlite3.Connection) -> None:
        version = _version(connection)
        if version == _SCHEMA_VERSION:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = _version(connection)
            if not 0 <= version <= _SCHEMA_VERSION:
                raise UnsupportedSchemaVersion
            for target, migrate in _MIGRATIONS:
                if version < target:
                    migrate(connection, version)
                    connection.execute(f"PRAGMA user_version = {target}")
                    version = target
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def _version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return 0 if row is None else int(row[0])
