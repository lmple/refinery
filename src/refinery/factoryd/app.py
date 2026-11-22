"""The factoryd ASGI app: MCP at /mcp, the human API at /api, and a lifespan that
opens and closes the Workflow.

Closing is also reachable from outside the lifespan, through ``shutdown(app)``:
uvicorn skips the lifespan shutdown on a forced exit (a second Ctrl-C), and
factoryd's own shutdown must run on every path (factoryd-server, Shutdown).
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

from starlette.applications import Starlette
from starlette.routing import Mount

from refinery.factoryd.api import build_api
from refinery.factoryd.config import ServerConfig
from refinery.factoryd.mcp import build_mcp
from refinery.factoryd.workflow import Workflow
from refinery.review.service import Review
from refinery.run.archive import TraceValidator


def create_app(
    config: ServerConfig, *, validator: TraceValidator | None = None
) -> Starlette:
    """The app; ``app.state.workflow`` is the Workflow while it runs."""
    opened: dict[str, Workflow] = {}

    async def close() -> None:
        # Idempotent: the lifespan and serve() may both call it.
        workflow = opened.pop("workflow", None)
        if workflow is not None:
            await workflow.close()

    def workflow() -> Workflow:
        try:
            return opened["workflow"]
        except KeyError:
            raise RuntimeError("factoryd is not started") from None

    def review() -> Review:
        return Review(workflow())

    api = build_api(workflow, review)
    mcp_app = build_mcp(config, workflow).streamable_http_app(
        streamable_http_path="/mcp", host=config.host
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        # Startup order (factoryd-server spec): Workflow.open reaps, recovers,
        # then schedules gates, all before the first request is served.
        opened["workflow"] = app.state.workflow = await Workflow.open(
            config, validator=validator
        )
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            await close()

    app = Starlette(
        routes=[Mount("/api", app=api), Mount("/", app=mcp_app)], lifespan=lifespan
    )
    app.state.shutdown = close
    return app


async def shutdown(app: Starlette) -> None:
    """factoryd's own shutdown (cancel gate runs, close the run index), if the
    lifespan has not already run it."""
    close = cast(Callable[[], Awaitable[None]], app.state.shutdown)
    await close()
