"""The human HTTP API under /api (D7)."""

from collections.abc import Callable

from fastapi import FastAPI

from refinery.factoryd.workflow import Workflow
from refinery.review.service import Review


def build_api(
    workflow: Callable[[], Workflow], review: Callable[[], Review]
) -> FastAPI:
    """A FastAPI app, mounted at /api: human tokens only, JSON errors."""
    raise NotImplementedError
