"""Requirement "The refine CLI" (human-api, review-queue): refine against a
factoryd served in a background thread (the CLI is synchronous)."""

import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

import pytest
import uvicorn
from typer.testing import CliRunner

from refinery.auth.tokens import Role, TokenStore
from refinery.cli.refine import app
from refinery.factoryd.app import create_app
from refinery.factoryd.workflow import Workflow
from refinery.review.service import Review
from tests.gate.conftest import git
from tests.review.conftest import ReviewFactory, started, to_review

runner = CliRunner()


@contextmanager
def serving(rf: ReviewFactory) -> Generator[str]:
    """factoryd in a thread on an ephemeral port; yields its base URL."""
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(rf.config, validator=rf.validator),
            host="127.0.0.1",
            port=0,
            lifespan="on",
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "factoryd did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def at_tests_review(rf: ReviewFactory) -> str:
    """A run waiting at the tests review, prepared before factoryd serves."""

    async def prepare(wf: Workflow, review: Review) -> str:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        await review.approve(run_id)
        await wf.submit_for_review(agent)
        return run_id

    return rf.run_review(prepare)


def record(rf: ReviewFactory, run_id: str) -> dict[str, object]:
    value: object = json.loads(
        (rf.root / ".refinery" / "runs" / f"{run_id}.json").read_text()
    )
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def env(url: str, token: str | None) -> dict[str, str]:
    return {"REFINE_URL": url, **({"REFINE_TOKEN": token} if token else {})}


def test_approve_from_the_cli(rf: ReviewFactory) -> None:
    run_id = at_tests_review(rf)
    token = TokenStore(rf.data).create(Role.HUMAN).secret
    with serving(rf) as url:
        result = runner.invoke(app, ["approve", run_id], env=env(url, token))
        head = rf.current_head()
    assert result.exit_code == 0, result.output
    assert "impl" in result.output
    saved = record(rf, run_id)
    assert saved["phase"] == "impl"
    approved = cast(dict[str, object], saved["approved"])
    assert approved["tests"] == head


def test_refused_from_the_cli(rf: ReviewFactory) -> None:
    run_id = at_tests_review(rf)
    token = TokenStore(rf.data).create(Role.HUMAN).secret
    with serving(rf) as url:
        result = runner.invoke(app, ["reject", run_id], env=env(url, token))
    assert result.exit_code != 0
    assert record(rf, run_id)["phase"] == "tests_review"


def test_no_token(rf: ReviewFactory) -> None:
    with serving(rf) as url:
        result = runner.invoke(app, ["runs"], env=env(url, None))
    assert result.exit_code != 0
    assert "REFINE_TOKEN" in result.output


def test_runs_waiting_and_status(rf: ReviewFactory) -> None:
    run_id = at_tests_review(rf)
    token = TokenStore(rf.data).create(Role.HUMAN).secret
    with serving(rf) as url:
        waiting = runner.invoke(app, ["runs", "--waiting"], env=env(url, token))
        status = runner.invoke(app, ["status", run_id], env=env(url, token))
        unknown = runner.invoke(app, ["status", "nope.1"], env=env(url, token))
    assert waiting.exit_code == 0, waiting.output
    assert run_id in waiting.output
    assert "tests_review" in waiting.output
    assert status.exit_code == 0, status.output
    assert "tests_review" in status.output
    assert unknown.exit_code != 0
    assert "nope.1" in unknown.output


def test_start_from_the_cli(rf: ReviewFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    git(rf.root, "checkout", "-q", "main")  # runs start from the main checkout
    monkeypatch.chdir(rf.root)  # --root defaults to the current directory
    token = TokenStore(rf.data).create(Role.HUMAN).secret
    with serving(rf) as url:
        result = runner.invoke(app, ["start", "demo"], env=env(url, token))
    assert result.exit_code == 0, result.output
    assert "demo.1: spec" in result.output
    assert str(rf.root.parent / "repo.worktrees" / "demo") in result.output
    assert "rft_a_" not in result.output
    assert "token" not in result.output


def stuck_run(rf: ReviewFactory) -> str:
    """A run whose two attempts failed, prepared before factoryd serves."""

    async def prepare(wf: Workflow, review: Review) -> str:
        run_id, _ = await started(wf, rf)
        for _ in range(2):
            claimed = await wf.jobs.claim("x0000001", [], "http://127.0.0.1/mcp")
            assert claimed is not None
            await wf.jobs.finish("x0000001", str(claimed["job"]), "exited", 1, "")
        assert wf.jobs.stuck(run_id)
        return run_id

    return rf.run_review(prepare)


def test_retry_from_the_cli(rf: ReviewFactory) -> None:
    run_id = stuck_run(rf)
    token = TokenStore(rf.data).create(Role.HUMAN).secret
    with serving(rf) as url:
        before = runner.invoke(app, ["status", run_id], env=env(url, token))
        waiting = runner.invoke(app, ["runs", "--waiting"], env=env(url, token))
        result = runner.invoke(app, ["retry", run_id], env=env(url, token))
        after = runner.invoke(app, ["status", run_id], env=env(url, token))
        again = runner.invoke(app, ["retry", run_id], env=env(url, token))
    assert "stuck" in before.output, before.output
    assert run_id in waiting.output
    assert result.exit_code == 0, result.output
    assert after.exit_code == 0, after.output
    assert "waiting" in after.output
    assert again.exit_code != 0
