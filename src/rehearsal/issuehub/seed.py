"""Explicit fixture installation into a local IssueHub database."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from rehearsal.domain.models import BoardSnapshot
from rehearsal.issuehub.schema import migrate


class BoardAlreadySeeded(ValueError):
    """The board already exists and its current state was preserved."""


def seed_board(database: Path, snapshot: BoardSnapshot) -> None:
    """Install a new board at the revisions supplied by its trusted fixture."""
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        migrate(connection)
        existing = connection.execute(
            "SELECT 1 FROM boards WHERE id = ?", (snapshot.board_id,)
        ).fetchone()
        if existing:
            raise BoardAlreadySeeded(f"{snapshot.board_id} already exists")
        connection.execute(
            "INSERT INTO boards (id, revision) VALUES (?, ?)",
            (snapshot.board_id, snapshot.board_revision),
        )
        connection.executemany(
            "INSERT INTO labels (board_id, label) VALUES (?, ?)",
            ((snapshot.board_id, label) for label in snapshot.label_registry),
        )
        connection.executemany(
            """INSERT INTO issues (
                board_id, id, title, body, state, labels_json,
                last_activity_at, revision, resolved_by_maintainer
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    issue.board_id,
                    issue.id,
                    issue.title,
                    issue.body,
                    issue.state,
                    json.dumps(issue.labels, ensure_ascii=False),
                    issue.last_activity_at.isoformat(),
                    issue.revision,
                    issue.resolved_by_maintainer,
                )
                for issue in snapshot.issues
            ),
        )
