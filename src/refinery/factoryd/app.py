"""The factoryd ASGI app: MCP at /mcp, the human API at /api, and a lifespan that
opens and closes the Workflow."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

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
            await opened.pop("workflow").close()

    return Starlette(
        routes=[Mount("/api", app=api), Mount("/", app=mcp_app)], lifespan=lifespan
    )
