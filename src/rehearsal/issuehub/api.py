"""Local HTTP access to the bounded IssueHub issue list."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rehearsal.demo import preview_demo
from rehearsal.domain.models import as_utc
from rehearsal.domain.plans import PlanValidationError
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
from rehearsal.issuehub.review import ReviewReport, review_report


def create_app(
    database: Path,
    *,
    clock: datetime | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    allowed_board_id = Policy().allowed_board_id
    reader = IssueHubReader(database, allowed_board_id=allowed_board_id)
    app = FastAPI(title="IssueHub", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    fixed_clock = as_utc(clock) if clock is not None else None

    @app.get("/api/rehearsal/preview")
    def preview(response: Response) -> ReviewReport:
        response.headers["Cache-Control"] = "no-store"
        try:
            snapshot = reader.capture(allowed_board_id, clock=fixed_clock or datetime.now(UTC))
            return review_report(preview_demo(snapshot, full=True))
        except BoardNotFound as exc:
            raise HTTPException(404, "BOARD_NOT_FOUND") from exc
        except PlanValidationError as exc:
            raise HTTPException(422, "DEMO_PLAN_UNAVAILABLE") from exc
        except (sqlite3.Error, ValueError, OverflowError) as exc:
            raise HTTPException(503, "ISSUEHUB_UNAVAILABLE") from exc

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

    if web_dist is not None:
        index = web_dist / "index.html"
        if not index.is_file() or not (web_dist / "assets").is_dir():
            raise ValueError("review assets are missing; run npm run build in web")
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="review-assets")

        @app.get("/", include_in_schema=False)
        def review_page() -> FileResponse:
            return FileResponse(
                index,
                headers={
                    "Cache-Control": "no-cache",
                    "Content-Security-Policy": (
                        "default-src 'self'; base-uri 'none'; "
                        "frame-ancestors 'none'; form-action 'none'"
                    ),
                },
            )

    return app
