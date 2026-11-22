"""Scenarios of specs/human-api/spec.md (review-queue) over real HTTP: uvicorn
serves create_app on an ephemeral port inside the test's event loop."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

import httpx2
import uvicorn

from refinery.auth.tokens import Role, TokenStore
from refinery.factoryd.app import create_app
from refinery.factoryd.workflow import Workflow
from refinery.run.model import Phase
from tests.factoryd.conftest import HUMAN, rows
from tests.gate.conftest import CHANGE
from tests.review.conftest import ReviewFactory, started, to_review

P = Phase


@asynccontextmanager
async def serving(rf: ReviewFactory) -> AsyncGenerator[tuple[str, Workflow]]:
    """factoryd on an ephemeral port; yields its base URL and its Workflow."""
    app = create_app(rf.config, validator=rf.validator)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, lifespan="on", log_level="warning"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", cast(Workflow, app.state.workflow)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


def run(
    rf: ReviewFactory, scenario: Callable[[str, Workflow], Awaitable[None]]
) -> None:
    async def go() -> None:
        async with serving(rf) as (url, workflow):
            await scenario(url, workflow)

    asyncio.run(go())


def client(url: str, token: str | None) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(base_url=url, headers=headers, timeout=30)


def body(response: httpx2.Response) -> dict[str, object]:
    value = response.json()
    assert isinstance(value, dict)
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def human_token(rf: ReviewFactory) -> str:
    return TokenStore(rf.data).create(Role.HUMAN).secret


# Requirement: API authentication


def test_no_token(rf: ReviewFactory) -> None:
    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, None) as http:
            assert (await http.get("/api/runs")).status_code == 401

    run(rf, scenario)


def test_unknown_token(rf: ReviewFactory) -> None:
    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, "rft_h_00000000_" + "0" * 64) as http:
            assert (await http.get("/api/runs")).status_code == 401

    run(rf, scenario)


def test_agent_token_on_the_api(rf: ReviewFactory) -> None:
    async def scenario(url: str, wf: Workflow) -> None:
        started_run = await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        async with client(url, str(started_run["agent_token"])) as http:
            listed = await http.get("/api/runs")
            assert listed.status_code == 403
            approve = await http.post("/api/runs/demo.1/approve", json={})
            assert approve.status_code == 403
        assert wf.store.get("demo.1").phase is P.SPEC

    run(rf, scenario)


# Requirements: Reading runs; Acting on runs


def test_start_list_show_and_approve(rf: ReviewFactory) -> None:
    token = human_token(rf)

    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, token) as http:
            started_run = await http.post(
                "/api/runs", json={"change": CHANGE, "worktree": str(rf.root)}
            )
            assert started_run.status_code == 200, started_run.text
            run_id = str(body(started_run)["run_id"])
            agent = wf.authenticate(str(body(started_run)["agent_token"]))
            assert agent is not None
            await to_review(wf, rf, run_id, agent)
            listed = body((await http.get("/api/runs")).raise_for_status())
            assert [r["run_id"] for r in rows(listed["runs"])] == [run_id]
            waiting = body(
                (await http.get("/api/runs", params={"waiting": 1})).raise_for_status()
            )
            assert [r["run_id"] for r in rows(waiting["runs"])] == [run_id]
            shown = body((await http.get(f"/api/runs/{run_id}")).raise_for_status())
            assert shown["phase"] == "spec_review"
            assert shown["pr"]
            approved = await http.post(f"/api/runs/{run_id}/approve", json={})
            assert approved.status_code == 200, approved.text
            assert body(approved)["phase"] == "tests"
            waiting = body(
                (await http.get("/api/runs", params={"waiting": 1})).raise_for_status()
            )
            assert waiting["runs"] == []

    run(rf, scenario)


def test_reject_cancel_resolve_and_retry(rf: ReviewFactory) -> None:
    token = human_token(rf)

    async def scenario(url: str, wf: Workflow) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        async with client(url, token) as http:
            rejected = await http.post(
                f"/api/runs/{run_id}/reject", json={"note": "redo"}
            )
            assert (rejected.status_code, body(rejected)["phase"]) == (200, "spec")
            resolve = await http.post(
                f"/api/runs/{run_id}/resolve", json={"to": "spec"}
            )
            assert resolve.status_code == 409
            retry = await http.post(f"/api/runs/{run_id}/retry-archive", json={})
            assert retry.status_code == 409
            cancelled = await http.post(f"/api/runs/{run_id}/cancel", json={})
            assert (cancelled.status_code, body(cancelled)["phase"]) == (
                200,
                "cancelled",
            )

    run(rf, scenario)


# Requirement: API errors


def test_unknown_run(rf: ReviewFactory) -> None:
    token = human_token(rf)

    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, token) as http:
            response = await http.post("/api/runs/nope.1/approve", json={})
            assert response.status_code == 404
            assert "nope.1" in str(body(response)["error"])
            assert (await http.get("/api/runs/nope.1")).status_code == 404

    run(rf, scenario)


def test_wrong_phase(rf: ReviewFactory) -> None:
    token = human_token(rf)

    async def scenario(url: str, wf: Workflow) -> None:
        run_id, _ = await started(wf, rf)
        async with client(url, token) as http:
            response = await http.post(f"/api/runs/{run_id}/approve", json={})
            assert response.status_code == 409
            assert body(response)["error"]
        assert wf.store.get(run_id).phase is P.SPEC

    run(rf, scenario)


def test_invalid_requests(rf: ReviewFactory) -> None:
    token = human_token(rf)

    async def scenario(url: str, wf: Workflow) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        async with client(url, token) as http:
            no_note = await http.post(f"/api/runs/{run_id}/reject", json={})
            assert no_note.status_code == 422
            bad_target = await http.post(
                f"/api/runs/{run_id}/resolve", json={"to": "verify"}
            )
            assert bad_target.status_code == 422
            no_change = await http.post("/api/runs", json={"worktree": str(rf.root)})
            assert no_change.status_code == 422
        assert wf.store.get(run_id).phase is P.SPEC_REVIEW

    run(rf, scenario)


def test_mcp_still_served_next_to_the_api(rf: ReviewFactory) -> None:
    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, None) as http:
            assert (await http.post("/mcp", json={})).status_code == 401

    run(rf, scenario)
