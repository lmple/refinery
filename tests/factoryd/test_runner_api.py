"""The runner API (agent-jobs; runner D5) over real HTTP: uvicorn serves
create_app on an ephemeral port inside the test's event loop. Also the
access-tokens scenario "Runner token is only for the runner API" on /api."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

import httpx2
import uvicorn

from refinery.auth.tokens import Role, TokenStore
from refinery.factoryd.app import create_app
from refinery.factoryd.workflow import Workflow
from tests.factoryd.conftest import Factory, start, start_with_token

CLAIM = "/api/runner/claim"


@asynccontextmanager
async def serving(fx: Factory) -> AsyncGenerator[tuple[str, Workflow]]:
    """factoryd on an ephemeral port; yields its base URL and its Workflow."""
    app = create_app(fx.config)
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


def run(fx: Factory, scenario: Callable[[str, Workflow], Awaitable[None]]) -> None:
    async def go() -> None:
        async with serving(fx) as (url, workflow):
            await scenario(url, workflow)

    asyncio.run(go())


def client(url: str, token: str | None) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(base_url=url, headers=headers, timeout=30)


def body(response: httpx2.Response) -> dict[str, object]:
    value = response.json()
    assert isinstance(value, dict)
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def runner_token(fx: Factory) -> str:
    return TokenStore(fx.data).create(Role.RUNNER).secret


# Requirement: The runner API needs a runner token


def test_no_token(fx: Factory) -> None:
    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, None) as http:
            response = await http.post(CLAIM, json={"holding": []})
            assert response.status_code == 401

    run(fx, scenario)


def test_unknown_token(fx: Factory) -> None:
    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, "rft_r_00000000_" + "0" * 64) as http:
            response = await http.post(CLAIM, json={"holding": []})
            assert response.status_code == 401

    run(fx, scenario)


def test_human_token_on_the_runner_api(fx: Factory) -> None:
    human = TokenStore(fx.data).create(Role.HUMAN).secret

    async def scenario(url: str, wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        async with client(url, human) as http:
            response = await http.post(CLAIM, json={"holding": []})
            assert response.status_code == 403
            assert body(response)["error"]
        assert wf.jobs.view(run_id)["state"] == "waiting"

    run(fx, scenario)


def test_agent_token_on_the_runner_api(fx: Factory) -> None:
    async def scenario(url: str, wf: Workflow) -> None:
        _, token = await start_with_token(wf, fx)
        async with client(url, token) as http:
            response = await http.post(CLAIM, json={"holding": []})
            assert response.status_code == 403

    run(fx, scenario)


def test_runner_token_on_the_human_api(fx: Factory) -> None:
    token = runner_token(fx)

    async def scenario(url: str, wf: Workflow) -> None:
        async with client(url, token) as http:
            assert (await http.get("/api/runs")).status_code == 403
            response = await http.post("/api/runs/demo.1/cancel", json={})
            assert response.status_code == 403

    run(fx, scenario)


# Requirements: Claiming a job; Polling; Jobs end only through their runner


def test_claim_poll_finish(fx: Factory) -> None:
    x, y = runner_token(fx), runner_token(fx)

    async def scenario(url: str, wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        async with client(url, x) as runner_x, client(url, y) as runner_y:
            claimed = await runner_x.post(CLAIM, json={"holding": []})
            assert claimed.status_code == 200, claimed.text
            job = body(claimed)
            assert (job["job"], job["run_id"], job["phase"]) == (
                "demo.1.j1",
                run_id,
                "spec",
            )
            assert job["mcp_url"] == f"{url}/mcp"
            assert job["worktree"] == str(fx.root)
            assert wf.authenticate(str(job["agent_token"])) is not None
            other = await runner_y.post(CLAIM, json={"holding": []})
            assert other.status_code == 204
            jobs = f"/api/runner/jobs/{job['job']}"
            polled = body((await runner_x.post(f"{jobs}/poll")).raise_for_status())
            assert polled["action"] == "continue"
            finished = await runner_x.post(
                f"{jobs}/finish",
                json={"outcome": "exited", "exit_code": 0, "log": "/l"},
            )
            assert finished.status_code == 200, finished.text
            again = await runner_x.post(
                f"{jobs}/finish",
                json={"outcome": "exited", "exit_code": 0, "log": "/l"},
            )
            assert again.status_code == 200
            polled = body((await runner_x.post(f"{jobs}/poll")).raise_for_status())
            assert polled["action"] == "stop"
            assert wf.authenticate(str(job["agent_token"])) is None

    run(fx, scenario)


def test_release_and_hello(fx: Factory) -> None:
    x = runner_token(fx)

    async def scenario(url: str, wf: Workflow) -> None:
        await start(wf, fx)
        async with client(url, x) as http:
            first = body(
                (await http.post(CLAIM, json={"holding": []})).raise_for_status()
            )
            released = await http.post(f"/api/runner/jobs/{first['job']}/release")
            assert released.status_code == 200, released.text
            second = body(
                (await http.post(CLAIM, json={"holding": []})).raise_for_status()
            )
            assert second["job"] == "demo.1.j2"
            hello = body((await http.post("/api/runner/hello")).raise_for_status())
            assert hello["released"] == ["demo.1.j2"]
            assert wf.authenticate(str(second["agent_token"])) is None

    run(fx, scenario)


def test_claim_lists_held_jobs(fx: Factory) -> None:
    x = runner_token(fx)

    async def scenario(url: str, wf: Workflow) -> None:
        await start(wf, fx, "a")
        await start(wf, fx, "b")
        async with client(url, x) as http:
            first = body(
                (await http.post(CLAIM, json={"holding": []})).raise_for_status()
            )
            second = body(
                (
                    await http.post(CLAIM, json={"holding": [first["job"]]})
                ).raise_for_status()
            )
            assert (first["run_id"], second["run_id"]) == ("a.1", "b.1")
        assert wf.jobs.view("a.1")["state"] == "running"

    run(fx, scenario)


def test_another_runners_job(fx: Factory) -> None:
    x, y = runner_token(fx), runner_token(fx)

    async def scenario(url: str, wf: Workflow) -> None:
        await start(wf, fx)
        async with client(url, x) as runner_x, client(url, y) as runner_y:
            job = body(
                (await runner_x.post(CLAIM, json={"holding": []})).raise_for_status()
            )["job"]
            for action in ("poll", "release"):
                response = await runner_y.post(f"/api/runner/jobs/{job}/{action}")
                assert response.status_code == 404
                assert body(response)["error"]
            finish = await runner_y.post(
                f"/api/runner/jobs/{job}/finish", json={"outcome": "exited"}
            )
            assert finish.status_code == 404
        assert wf.jobs.view("demo.1")["state"] == "running"

    run(fx, scenario)


def test_claim_cannot_be_recorded(fx: Factory) -> None:
    x = runner_token(fx)

    async def scenario(url: str, wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        (fx.data / "runs" / run_id / "jobs.jsonl").mkdir(parents=True)
        async with client(url, x) as http:
            response = await http.post(CLAIM, json={"holding": []})
            assert response.status_code == 503
            assert body(response)["error"]

    run(fx, scenario)
