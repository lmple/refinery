"""The human HTTP API under /api (D7).

Human bearer tokens only: no token or an unknown one is 401, an agent token
403. Refused operations answer ``{"error": ...}`` with the ReviewError's
status (404, 409, 422); FastAPI's own body validation also answers 422.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from refinery.auth.tokens import Principal, Role
from refinery.factoryd.workflow import Json, Workflow
from refinery.review.service import Review, ReviewError


class StartBody(BaseModel):
    """POST /api/runs."""

    change: str
    worktree: str


class ApproveBody(BaseModel):
    """POST /api/runs/{id}/approve."""

    note: str | None = None


class RejectBody(BaseModel):
    """POST /api/runs/{id}/reject."""

    note: str


class ResolveBody(BaseModel):
    """POST /api/runs/{id}/resolve."""

    to: Literal["spec", "tests", "impl"]


class _AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def build_api(
    workflow: Callable[[], Workflow], review: Callable[[], Review]
) -> FastAPI:
    """A FastAPI app, mounted at /api: human tokens only, JSON errors."""

    async def human(authorization: Annotated[str | None, Header()] = None) -> Principal:
        token = (authorization or "").removeprefix("Bearer ").strip()
        principal = workflow().authenticate(token) if token else None
        if principal is None:
            raise _AuthError(401, "a valid human token is required")
        if principal.role is not Role.HUMAN:
            raise _AuthError(403, "this API is for humans; agents use the MCP tools")
        return principal

    # Every route needs a human; FastAPI caches the dependency per request, so
    # start_run asking for the principal again does not authenticate twice.
    api = FastAPI(
        title="Refinery factoryd",
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(human)],
    )

    @api.exception_handler(ReviewError)
    async def review_error(request: Request, exc: ReviewError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @api.exception_handler(_AuthError)
    async def auth_error(request: Request, exc: _AuthError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @api.get("/runs")
    async def list_runs(waiting: bool = False) -> Json:
        runs = await (review().queue() if waiting else review().runs())
        return {"runs": runs}

    @api.get("/runs/{run_id}")
    async def show_run(run_id: str) -> Json:
        return await review().show(run_id)

    @api.post("/runs")
    async def start_run(
        who: Annotated[Principal, Depends(human)], body: StartBody
    ) -> Json:
        return await review().start(who, body.change, body.worktree)

    @api.post("/runs/{run_id}/approve")
    async def approve(run_id: str, body: ApproveBody | None = None) -> Json:
        return await review().approve(run_id, body.note if body else None)

    @api.post("/runs/{run_id}/reject")
    async def reject(run_id: str, body: RejectBody) -> Json:
        return await review().reject(run_id, body.note)

    @api.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> Json:
        return await review().cancel(run_id)

    @api.post("/runs/{run_id}/resolve")
    async def resolve(run_id: str, body: ResolveBody) -> Json:
        return await review().resolve(run_id, body.to)

    @api.post("/runs/{run_id}/retry-archive")
    async def retry_archive(run_id: str) -> Json:
        return await review().retry_archive(run_id)

    return api
