"""The runner API under /api/runner (runner D5).

Runner bearer tokens only: no token or an unknown one is 401, a human or
agent token 403. Refusals answer ``{"error": ...}`` with the JobError's status.
"""

from collections.abc import Callable

from fastapi import FastAPI
from pydantic import BaseModel

from refinery.factoryd.workflow import Workflow


class ClaimBody(BaseModel):
    """POST /api/runner/claim: the jobs the runner is running."""

    holding: list[str] = []


class FinishBody(BaseModel):
    """POST /api/runner/jobs/{job}/finish."""

    outcome: str
    exit_code: int | None = None
    log: str = ""


def build_runner_api(workflow: Callable[[], Workflow]) -> FastAPI:
    """A FastAPI app, mounted at /api/runner: runner tokens only."""
    raise NotImplementedError("runner API (runner D5)")
