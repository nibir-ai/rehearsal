"""Versioned migrations for the local IssueHub database."""

import sqlite3
from contextlib import closing
from pathlib import Path

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE boards (
            id TEXT PRIMARY KEY NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1)
        )""",
        """CREATE TABLE labels (
            board_id TEXT NOT NULL REFERENCES boards(id),
            label TEXT NOT NULL,
            PRIMARY KEY (board_id, label)
        )""",
        """CREATE TABLE issues (
            board_id TEXT NOT NULL REFERENCES boards(id),
            id TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('open', 'closed')),
            labels_json TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            resolved_by_maintainer INTEGER NOT NULL CHECK (resolved_by_maintainer IN (0, 1)),
            PRIMARY KEY (board_id, id)
        )""",
        "CREATE INDEX issues_by_state ON issues (board_id, state, id)",
    ),
    (
        """CREATE TABLE comments (
            board_id TEXT NOT NULL,
            issue_id TEXT NOT NULL,
            id INTEGER NOT NULL CHECK (id >= 1),
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (board_id, issue_id, id),
            FOREIGN KEY (board_id, issue_id) REFERENCES issues(board_id, id)
        )""",
    ),
)
SCHEMA_VERSION = len(MIGRATIONS)


def schema_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def migrate(connection: sqlite3.Connection) -> None:
    """Apply migrations inside the caller's write transaction."""
    version = schema_version(connection)
    if version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError("unsupported IssueHub schema version")
    for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
        for statement in migration:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {number}")


def upgrade_database(database: Path) -> None:
    """Upgrade an existing IssueHub database without reseeding it."""
    uri = f"{database.resolve().as_uri()}?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        if schema_version(connection) == 0:
            raise sqlite3.DatabaseError("database has not been initialized by IssueHub")
        migrate(connection)
