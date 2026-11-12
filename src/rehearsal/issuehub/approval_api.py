"""Loopback owner authentication and exact-plan decision routes."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import Field

from rehearsal.domain.models import FrozenModel
from rehearsal.domain.plans import Sha256
from rehearsal.issuehub.approval import (
    SESSION_TTL,
    ApprovalError,
    ApprovalRecord,
    ApprovalService,
    Rejection,
    RunReview,
)
from rehearsal.issuehub.execution import ExecutionReceipt

_COOKIE = "rehearsal_owner"
_CSRF_HEADER = "x-rehearsal-csrf"
RunId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class SessionResponse(FrozenModel):
    csrf_token: str


class RunRequest(FrozenModel):
    target_id: str = Field(min_length=1, max_length=64)
    goal: Literal["Rehearse stale-issue maintenance"]


class ApproveRequest(FrozenModel):
    plan_digest: Sha256
    decision: Literal["approve"]


class RejectRequest(FrozenModel):
    plan_digest: Sha256
    decision: Literal["reject"]


_ERROR_STATUS = {
    "OWNER_AUTH_REQUIRED": 401,
    "CSRF_INVALID": 403,
    "TARGET_OUT_OF_SCOPE": 403,
    "BOARD_NOT_FOUND": 404,
    "RUN_NOT_FOUND": 404,
    "RECEIPT_NOT_FOUND": 404,
    "DEMO_PLAN_UNAVAILABLE": 422,
    "NO_APPROVABLE_ACTIONS": 422,
    "MIGRATION_REQUIRED": 503,
}


@contextmanager
def _workflow_errors() -> Iterator[None]:
    try:
        yield
    except ApprovalError as exc:
        raise HTTPException(_ERROR_STATUS.get(exc.code, 409), exc.code) from exc
    except (sqlite3.Error, ValueError, OverflowError) as exc:
        raise HTTPException(503, "ISSUEHUB_UNAVAILABLE") from exc


def _verify_origin(request: Request) -> None:
    origin = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.getlist("origin") != [origin]:
        raise HTTPException(403, "ORIGIN_INVALID")


def _credentials(request: Request) -> tuple[str, str]:
    _verify_origin(request)
    return request.cookies.get(_COOKIE, ""), request.headers.get(_CSRF_HEADER, "")


async def _require_empty_body(request: Request) -> None:
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(422, "BODY_NOT_ALLOWED")


def approval_router(service: ApprovalService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/owner/session")
    def open_session(request: Request, response: Response) -> SessionResponse:
        _verify_origin(request)
        scheme, _, secret = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secret:
            raise HTTPException(401, "OWNER_AUTH_REQUIRED")
        with _workflow_errors():
            session = service.open_session(secret)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            _COOKIE,
            session.token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/api",
            max_age=int(SESSION_TTL.total_seconds()),
        )
        return SessionResponse(csrf_token=session.csrf_token)

    @router.post("/runs", status_code=201)
    def create_run(body: RunRequest, request: Request, response: Response) -> RunReview:
        response.headers["Cache-Control"] = "no-store"
        token, csrf = _credentials(request)
        with _workflow_errors():
            return service.create_run(body.target_id, token, csrf)

    @router.get("/runs/{run_id}/preview")
    def get_review(run_id: RunId, request: Request, response: Response) -> RunReview:
        response.headers["Cache-Control"] = "no-store"
        with _workflow_errors():
            return service.get_review(run_id, request.cookies.get(_COOKIE, ""))

    @router.post("/runs/{run_id}/approve")
    def approve(
        run_id: RunId, body: ApproveRequest, request: Request, response: Response
    ) -> ApprovalRecord:
        response.headers["Cache-Control"] = "no-store"
        token, csrf = _credentials(request)
        with _workflow_errors():
            return service.approve(run_id, body.plan_digest, token, csrf)

    @router.post("/runs/{run_id}/reject")
    def reject(
        run_id: RunId, body: RejectRequest, request: Request, response: Response
    ) -> Rejection:
        response.headers["Cache-Control"] = "no-store"
        token, csrf = _credentials(request)
        with _workflow_errors():
            return service.reject(run_id, body.plan_digest, token, csrf)

    @router.post("/runs/{run_id}/execute", dependencies=[Depends(_require_empty_body)])
    def execute(run_id: RunId, request: Request, response: Response) -> ExecutionReceipt:
        response.headers["Cache-Control"] = "no-store"
        token, csrf = _credentials(request)
        with _workflow_errors():
            return service.execute(run_id, token, csrf)

    @router.get("/runs/{run_id}/receipt")
    def get_receipt(run_id: RunId, request: Request, response: Response) -> ExecutionReceipt:
        response.headers["Cache-Control"] = "no-store"
        with _workflow_errors():
            return service.get_receipt(run_id, request.cookies.get(_COOKIE, ""))

    @router.post("/runs/{run_id}/reconcile", dependencies=[Depends(_require_empty_body)])
    def reconcile(run_id: RunId, request: Request, response: Response) -> ExecutionReceipt:
        response.headers["Cache-Control"] = "no-store"
        token, csrf = _credentials(request)
        with _workflow_errors():
            return service.reconcile(run_id, token, csrf)

    return router
