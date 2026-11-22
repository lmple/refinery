"""End to end over real HTTP (mcp-workflow): uvicorn serves create_app on an
ephemeral port inside the test's event loop; the MCP SDK client talks to it.
Covers authentication (access-tokens), the tool list, a role refusal and a
start -> get_task -> submit round trip (agent-tools)."""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

import httpx2
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from refinery.auth.tokens import Role, TokenStore
from refinery.factoryd.app import create_app
from refinery.factoryd.mcp import TOOLS
from tests.factoryd.conftest import Factory
from tests.gate.conftest import CHANGE


@asynccontextmanager
async def serving(fx: Factory) -> AsyncGenerator[str]:
    """factoryd on 127.0.0.1:<ephemeral>; yields the /mcp URL."""
    config = uvicorn.Config(
        create_app(fx.config),
        host="127.0.0.1",
        port=0,
        lifespan="on",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()  # surface a startup failure
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


@asynccontextmanager
async def session(url: str, token: str) -> AsyncGenerator[ClientSession]:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


def payload(result: object) -> dict[str, object]:
    """The JSON object a tool returned (as its text content)."""
    assert isinstance(result, types.CallToolResult), result
    assert not result.is_error, result.content
    text = result.content[0]
    assert isinstance(text, types.TextContent)
    value = json.loads(text.text)
    assert isinstance(value, dict)
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def run(fx: Factory, scenario: Callable[[str], Awaitable[None]]) -> None:
    async def go() -> None:
        async with serving(fx) as url:
            await scenario(url)

    asyncio.run(go())


def human_token(fx: Factory) -> str:
    return TokenStore(fx.data).create(Role.HUMAN).secret


async def post_initialize(url: str, headers: dict[str, str]) -> int:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    async with httpx2.AsyncClient(timeout=10) as http:
        response = await http.post(
            url,
            json=body,
            headers={"Accept": "application/json, text/event-stream", **headers},
        )
        return response.status_code


# Requirement: Every request needs a valid token


def test_no_token_is_rejected(fx: Factory) -> None:
    async def scenario(url: str) -> None:
        assert await post_initialize(url, {}) == 401

    run(fx, scenario)


def test_unknown_token_is_rejected(fx: Factory) -> None:
    async def scenario(url: str) -> None:
        bogus = {"Authorization": "Bearer rft_a_00000000_bogus"}
        assert await post_initialize(url, bogus) == 401

    run(fx, scenario)


def test_revoked_token_is_rejected(fx: Factory) -> None:
    store = TokenStore(fx.data)
    issued = store.create(Role.HUMAN)
    store.revoke(issued.token.id)

    async def scenario(url: str) -> None:
        assert (
            await post_initialize(url, {"Authorization": f"Bearer {issued.secret}"})
            == 401
        )

    run(fx, scenario)


def test_valid_token_is_accepted(fx: Factory) -> None:
    token = human_token(fx)

    async def scenario(url: str) -> None:
        assert await post_initialize(url, {"Authorization": f"Bearer {token}"}) == 200

    run(fx, scenario)


# Requirement: Tool set


def test_listing_tools(fx: Factory) -> None:
    token = human_token(fx)

    async def scenario(url: str) -> None:
        async with session(url, token) as client:
            listed = await client.list_tools()
            assert sorted(t.name for t in listed.tools) == sorted(TOOLS)
            assert len(TOOLS) == 6

    run(fx, scenario)


# Requirements: Tools are restricted by role; Starting a workflow; Submitting


def test_round_trip_and_role_refusal(fx: Factory) -> None:
    token = human_token(fx)

    async def scenario(url: str) -> None:
        async with session(url, token) as human:
            refused = await human.call_tool("get_task", {})
            assert isinstance(refused, types.CallToolResult)
            assert refused.is_error
            started = payload(
                await human.call_tool(
                    "start_workflow", {"change": CHANGE, "worktree": str(fx.root)}
                )
            )
        assert (started["run_id"], started["phase"]) == ("demo.1", "spec")
        agent_token = str(started["agent_token"])
        async with session(url, agent_token) as agent:
            denied = await agent.call_tool(
                "start_workflow", {"change": "other", "worktree": str(fx.root)}
            )
            assert isinstance(denied, types.CallToolResult)
            assert denied.is_error
            task = payload(await agent.call_tool("get_task", {}))
            assert (task["run_id"], task["phase"], task["turn"]) == (
                "demo.1",
                "spec",
                "agent",
            )
            submitted = payload(await agent.call_tool("submit_for_review", {}))
            assert submitted["phase"] == "model"
            progress = payload(
                await agent.call_tool("report_progress", {"message": "hi"})
            )
            assert progress["recorded"] is True

    run(fx, scenario)
