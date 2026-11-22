"""The MCP adapter: six tools over the workflow service (D1).

All SDK use lives here and in app.py. Bearer authentication is the SDK's own
middleware with Refinery's TokenVerifier; each tool resolves the caller from
the verified token and delegates to ``Workflow``. A ``WorkflowError`` becomes
a tool error: the call fails and nothing changed.
"""

from collections.abc import Awaitable, Callable

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AnyHttpUrl

from refinery.auth.tokens import Principal, RefineryTokenVerifier, Role
from refinery.factoryd.config import ServerConfig
from refinery.factoryd.workflow import Json, Workflow, WorkflowError

TOOLS = (
    "start_workflow",
    "get_task",
    "submit_for_review",
    "escalate",
    "run_gate",
    "report_progress",
)

_INSTRUCTIONS = (
    "Refinery factoryd. Agents: call get_task first and follow its instructions; "
    "submit_for_review completes your current phase. Humans: start_workflow."
)


def build_mcp(config: ServerConfig, workflow: Callable[[], Workflow]) -> MCPServer:
    """An MCPServer with bearer auth whose tools call ``workflow()``."""
    base = f"http://{config.host}:{config.port}"
    server = MCPServer(
        name="refinery",
        instructions=_INSTRUCTIONS,
        token_verifier=RefineryTokenVerifier(
            lambda token: workflow().authenticate(token)
        ),
        # factoryd issues its own tokens: it is both issuer and resource server.
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(base),
            resource_server_url=AnyHttpUrl(f"{base}/mcp"),
            # Tokens carry no audience: RefineryTokenVerifier checks each one
            # against the token store (and, for agents, the run) itself.
            validate_token_resource=False,
        ),
    )

    @server.tool(description="Human: start a run on an OpenSpec change from a root.")
    async def start_workflow(change: str, root: str) -> Json:
        return await _call(lambda who: workflow().start_workflow(who, change, root))

    @server.tool(
        description="Agent: your run's phase, whose turn it is, and what to do."
    )
    async def get_task() -> Json:
        return await _call(lambda who: workflow().get_task(who))

    @server.tool(
        description=(
            "Agent: complete your current phase (spec, model, tests or impl). In the "
            "model phase, give skip_model_reason when the change needs no TLA+ model."
        )
    )
    async def submit_for_review(skip_model_reason: str | None = None) -> Json:
        return await _call(
            lambda who: workflow().submit_for_review(who, skip_model_reason)
        )

    @server.tool(
        description="Agent: report a suspected spec_bug or test_bug, with a reason."
    )
    async def escalate(bug: str, reason: str) -> Json:
        return await _call(lambda who: workflow().escalate(who, bug, reason))

    @server.tool(
        description="Agent: run a dry-run gate (tests or impl); never moves the run."
    )
    async def run_gate() -> Json:
        return await _call(lambda who: workflow().run_gate(who))

    @server.tool(description="Agent: note progress; never blocks, never moves the run.")
    async def report_progress(message: str) -> Json:
        return await _call(lambda who: workflow().report_progress(who, message))

    return server


def _principal() -> Principal:
    """The caller, from the token the SDK's middleware verified."""
    token = get_access_token()
    if token is None or not token.scopes:
        raise ToolError("not authenticated")
    run_id = (token.claims or {}).get("run_id")
    return Principal(
        token.client_id,
        Role(token.scopes[0]),
        run_id if isinstance(run_id, str) else None,
    )


async def _call(operation: Callable[[Principal], Awaitable[Json]]) -> Json:
    who = _principal()
    try:
        return await operation(who)
    except WorkflowError as exc:
        raise ToolError(str(exc)) from exc
