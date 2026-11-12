"""Local HTTP access to the bounded IssueHub issue list."""

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from rehearsal.domain.policy import Policy
from rehearsal.issuehub.reader import (
    MAX_CURSOR_LENGTH,
    MAX_PAGE_SIZE,
    BoardNotFound,
    BoardOutOfScope,
    InvalidCursor,
    IssueHubReader,
    IssuePage,
    IssueState,
    StaleCursor,
)


def create_app(database: Path) -> FastAPI:
    allowed_board_id = Policy().allowed_board_id
    reader = IssueHubReader(database, allowed_board_id=allowed_board_id)
    app = FastAPI(title="IssueHub", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/api/issuehub/issues")
    def list_issues(
        board_id: Annotated[str, Query(min_length=1, max_length=64)] = allowed_board_id,
        state: IssueState | None = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=MAX_CURSOR_LENGTH)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
    ) -> IssuePage:
        try:
            return reader.list_issues(board_id, state=state, cursor=cursor, limit=limit)
        except BoardOutOfScope as exc:
            raise HTTPException(403, "TARGET_OUT_OF_SCOPE") from exc
        except BoardNotFound as exc:
            raise HTTPException(404, "BOARD_NOT_FOUND") from exc
        except InvalidCursor as exc:
            raise HTTPException(422, "INVALID_CURSOR") from exc
        except StaleCursor as exc:
            raise HTTPException(409, "SNAPSHOT_STALE") from exc
        except (sqlite3.Error, ValueError) as exc:
            raise HTTPException(503, "ISSUEHUB_UNAVAILABLE") from exc

    return app
