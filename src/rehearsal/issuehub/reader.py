"""Read IssueHub through SQLite connections that cannot write."""

import json
import sqlite3
from base64 import b64decode, urlsafe_b64encode
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import AwareDatetime, Field, TypeAdapter

from rehearsal.domain.models import (
    MAX_SNAPSHOT_ISSUES,
    BoardSnapshot,
    FrozenModel,
    Issue,
    IssueId,
    Label,
    Revision,
    as_utc,
    canonical_labels,
)
from rehearsal.issuehub.schema import SCHEMA_VERSION, schema_version

IssueState = Literal["open", "closed"]
MAX_PAGE_SIZE = 50
MAX_CURSOR_LENGTH = 512
MAX_COMMENT_PAGE_SIZE = 20
MAX_COMMENT_BODY = 1000
MAX_BOARD_LABELS = 100
_LABELS = TypeAdapter(tuple[Label, ...])
_CursorT = TypeVar("_CursorT", bound=FrozenModel)


class BoardOutOfScope(ValueError):
    """The requested board is outside the configured read scope."""


class BoardNotFound(LookupError):
    """The configured board has not been seeded."""


class IssueNotFound(LookupError):
    """The requested issue does not exist on the board."""


class MigrationRequired(ValueError):
    """The database needs an explicit upgrade for this operation."""


class LabelLimitExceeded(ValueError):
    """The board has too many labels for a bounded response."""


class InvalidCursor(ValueError):
    """The cursor does not belong to this listing."""


class StaleCursor(ValueError):
    """The board changed after the previous page."""


class SnapshotTooLarge(ValueError):
    """The board exceeds the supported snapshot size."""


class _Cursor(FrozenModel):
    board_id: str
    revision: Revision
    state: IssueState | None
    after: IssueId


class _CommentCursor(FrozenModel):
    board_id: str
    revision: Revision
    issue_id: IssueId
    after: Annotated[int, Field(ge=1, le=2**63 - 1)]


class IssueSummary(FrozenModel):
    id: IssueId
    title: str = Field(max_length=160)
    state: IssueState
    labels: tuple[Label, ...]
    last_activity_at: AwareDatetime
    revision: Revision
    resolved_by_maintainer: bool


class IssuePage(FrozenModel):
    board_id: str
    board_revision: Revision
    issues: Annotated[tuple[IssueSummary, ...], Field(max_length=MAX_PAGE_SIZE)]
    next_cursor: str | None


class BoardRevision(FrozenModel):
    board_id: str
    board_revision: Revision
    server_clock: AwareDatetime


class IssueDetail(FrozenModel):
    board_id: str
    board_revision: Revision
    issue: IssueSummary


class BoardLabels(FrozenModel):
    board_id: str
    board_revision: Revision
    labels: Annotated[tuple[Label, ...], Field(max_length=MAX_BOARD_LABELS)]


class IssueComment(FrozenModel):
    id: Revision
    author: Annotated[str, Field(max_length=128)]
    body: Annotated[str, Field(max_length=MAX_COMMENT_BODY)]
    body_truncated: bool
    created_at: AwareDatetime


class CommentPage(FrozenModel):
    board_id: str
    board_revision: Revision
    issue_id: IssueId
    comments: Annotated[tuple[IssueComment, ...], Field(max_length=MAX_COMMENT_PAGE_SIZE)]
    next_cursor: str | None


def _issue(row: sqlite3.Row) -> Issue:
    return Issue(
        id=row["id"],
        board_id=row["board_id"],
        title=row["title"],
        body=row["body"],
        state=row["state"],
        labels=_LABELS.validate_json(row["labels_json"], strict=True),
        last_activity_at=datetime.fromisoformat(row["last_activity_at"]),
        revision=row["revision"],
        resolved_by_maintainer=bool(row["resolved_by_maintainer"]),
    )


def _summary(row: sqlite3.Row) -> IssueSummary:
    return IssueSummary(
        id=row["id"],
        title=row["title"],
        state=row["state"],
        labels=canonical_labels(_LABELS.validate_json(row["labels_json"], strict=True)),
        last_activity_at=as_utc(datetime.fromisoformat(row["last_activity_at"])),
        revision=row["revision"],
        resolved_by_maintainer=bool(row["resolved_by_maintainer"]),
    )


_SUMMARY_SQL = """SELECT id, substr(title, 1, 160) AS title,
    state, labels_json, last_activity_at, revision, resolved_by_maintainer FROM issues"""


def _decode_cursor(value: str, cursor_type: type[_CursorT]) -> _CursorT:
    if len(value) > MAX_CURSOR_LENGTH:
        raise InvalidCursor("invalid listing cursor")
    try:
        return cursor_type.model_validate_json(b64decode(value, altchars=b"-_", validate=True))
    except ValueError as exc:
        raise InvalidCursor("invalid listing cursor") from exc


def _encode_cursor(cursor: _Cursor | _CommentCursor) -> str:
    return urlsafe_b64encode(cursor.model_dump_json().encode("utf-8")).decode("ascii")


def capture_board(
    connection: sqlite3.Connection, board_id: str, revision: int, *, clock: datetime
) -> BoardSnapshot:
    captured_at = as_utc(clock)
    rows = connection.execute(
        "SELECT * FROM issues WHERE board_id = ? ORDER BY id LIMIT ?",
        (board_id, MAX_SNAPSHOT_ISSUES + 1),
    ).fetchall()
    if len(rows) > MAX_SNAPSHOT_ISSUES:
        raise SnapshotTooLarge("board exceeds the supported snapshot size")
    issues = tuple(_issue(row) for row in rows)
    if any(issue.last_activity_at > captured_at for issue in issues):
        raise ValueError("capture clock predates activity in the current board")
    labels = connection.execute(
        "SELECT label FROM labels WHERE board_id = ? ORDER BY label", (board_id,)
    )
    identity = json.dumps([board_id, revision, captured_at.isoformat()]).encode("utf-8")
    return BoardSnapshot(
        snapshot_id=sha256(identity).hexdigest(),
        board_id=board_id,
        board_revision=revision,
        captured_at=captured_at,
        issues=issues,
        label_registry=tuple(row["label"] for row in labels),
    )


class IssueHubReader:
    def __init__(self, database: Path, *, allowed_board_id: str) -> None:
        self._database = database.resolve().as_uri()
        self._allowed_board_id = allowed_board_id

    @contextmanager
    def _read(
        self, board_id: str, *, minimum_schema: int = 1
    ) -> Iterator[tuple[sqlite3.Connection, int]]:
        if board_id != self._allowed_board_id:
            raise BoardOutOfScope("board is outside the configured read scope")
        with closing(sqlite3.connect(f"{self._database}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            version = schema_version(connection)
            if not 1 <= version <= SCHEMA_VERSION:
                raise sqlite3.DatabaseError("unsupported IssueHub schema version")
            if version < minimum_schema:
                raise MigrationRequired("run the IssueHub migrate command")
            board = connection.execute(
                "SELECT revision FROM boards WHERE id = ?", (board_id,)
            ).fetchone()
            if board is None:
                raise BoardNotFound("configured board has not been seeded")
            yield connection, board["revision"]

    def capture(self, board_id: str, *, clock: datetime) -> BoardSnapshot:
        with self._read(board_id) as (connection, revision):
            return capture_board(connection, board_id, revision, clock=clock)

    def list_issues(
        self,
        board_id: str,
        *,
        state: IssueState | None = None,
        cursor: str | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> IssuePage:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        with self._read(board_id) as (connection, revision):
            after = ""
            if cursor is not None:
                position = _decode_cursor(cursor, _Cursor)
                if (position.board_id, position.state) != (board_id, state):
                    raise InvalidCursor("cursor belongs to a different listing")
                if position.revision != revision:
                    raise StaleCursor("board changed between pages")
                after = position.after

            query = _SUMMARY_SQL + " WHERE board_id = ? AND id > ?"
            parameters: tuple[str | int, ...] = (board_id, after)
            if state is not None:
                query += " AND state = ?"
                parameters += (state,)
            rows = connection.execute(
                query + " ORDER BY id LIMIT ?", (*parameters, limit + 1)
            ).fetchall()

        issues = tuple(_summary(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            next_cursor = _encode_cursor(
                _Cursor(board_id=board_id, revision=revision, state=state, after=issues[-1].id)
            )
        return IssuePage(
            board_id=board_id,
            board_revision=revision,
            issues=issues,
            next_cursor=next_cursor,
        )

    def get_board_revision(self, board_id: str, *, clock: datetime) -> BoardRevision:
        with self._read(board_id) as (_, revision):
            return BoardRevision(
                board_id=board_id, board_revision=revision, server_clock=as_utc(clock)
            )

    def list_labels(self, board_id: str) -> BoardLabels:
        with self._read(board_id) as (connection, revision):
            rows = connection.execute(
                "SELECT label FROM labels WHERE board_id = ? ORDER BY label LIMIT ?",
                (board_id, MAX_BOARD_LABELS + 1),
            ).fetchall()
        if len(rows) > MAX_BOARD_LABELS:
            raise LabelLimitExceeded("too many labels to return")
        return BoardLabels(
            board_id=board_id,
            board_revision=revision,
            labels=canonical_labels(tuple(row["label"] for row in rows)),
        )

    def get_issue(self, board_id: str, issue_id: str) -> IssueDetail:
        with self._read(board_id) as (connection, revision):
            row = connection.execute(
                _SUMMARY_SQL + " WHERE board_id = ? AND id = ?", (board_id, issue_id)
            ).fetchone()
        if row is None:
            raise IssueNotFound("issue does not exist on the board")
        return IssueDetail(board_id=board_id, board_revision=revision, issue=_summary(row))

    def get_issue_comments(
        self, board_id: str, issue_id: str, *, cursor: str | None = None
    ) -> CommentPage:
        with self._read(board_id, minimum_schema=2) as (connection, revision):
            after = 0
            if cursor is not None:
                position = _decode_cursor(cursor, _CommentCursor)
                if (position.board_id, position.issue_id) != (board_id, issue_id):
                    raise InvalidCursor("cursor belongs to a different issue")
                if position.revision != revision:
                    raise StaleCursor("board changed between pages")
                after = position.after
            issue = connection.execute(
                "SELECT 1 FROM issues WHERE board_id = ? AND id = ?", (board_id, issue_id)
            ).fetchone()
            if issue is None:
                raise IssueNotFound("issue does not exist on the board")
            rows = connection.execute(
                """SELECT id, substr(author, 1, 128) AS author, substr(body, 1, ?) AS body,
                   length(body) > ? AS truncated, created_at
                   FROM comments WHERE board_id = ? AND issue_id = ? AND id > ?
                   ORDER BY id LIMIT ?""",
                (
                    MAX_COMMENT_BODY,
                    MAX_COMMENT_BODY,
                    board_id,
                    issue_id,
                    after,
                    MAX_COMMENT_PAGE_SIZE + 1,
                ),
            ).fetchall()

        comments = tuple(
            IssueComment(
                id=row["id"],
                author=row["author"],
                body=row["body"],
                body_truncated=bool(row["truncated"]),
                created_at=as_utc(datetime.fromisoformat(row["created_at"])),
            )
            for row in rows[:MAX_COMMENT_PAGE_SIZE]
        )
        next_cursor = None
        if len(rows) > MAX_COMMENT_PAGE_SIZE:
            next_cursor = _encode_cursor(
                _CommentCursor(
                    board_id=board_id, revision=revision, issue_id=issue_id, after=comments[-1].id
                )
            )
        return CommentPage(
            board_id=board_id,
            board_revision=revision,
            issue_id=issue_id,
            comments=comments,
            next_cursor=next_cursor,
        )
