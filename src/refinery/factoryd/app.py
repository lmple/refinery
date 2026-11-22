"""The factoryd ASGI app: MCP at /mcp, Workflow opened and closed by lifespan."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Mount

from refinery.factoryd.config import ServerConfig
from refinery.factoryd.mcp import build_mcp
from refinery.factoryd.workflow import Workflow


def create_app(config: ServerConfig) -> Starlette:
    """The app; ``app.state.workflow`` is the Workflow while it runs."""
    opened: dict[str, Workflow] = {}

    def workflow() -> Workflow:
        try:
            return opened["workflow"]
        except KeyError:
            raise RuntimeError("factoryd is not started") from None

    mcp_app = build_mcp(config, workflow).streamable_http_app(
        streamable_http_path="/mcp", host=config.host
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        # Startup order (factoryd-server spec): Workflow.open reaps, recovers,
        # then schedules gates, all before the first request is served.
        opened["workflow"] = app.state.workflow = await Workflow.open(config)
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            await opened.pop("workflow").close()

    return Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)
