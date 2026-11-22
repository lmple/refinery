"""Scenarios of specs/agent-jobs/spec.md (runner) against the Jobs service of
a real Workflow: RunStore, TokenStore and job logs on disk."""

import asyncio
import json
from typing import cast

import pytest

from refinery.auth.tokens import Principal, Role
from refinery.factoryd.config import ServerConfig
from refinery.factoryd.workflow import Json, Workflow
from refinery.jobs.service import JobError
from refinery.run.model import Action, Phase
from tests.factoryd.conftest import Factory, start, start_with_token, to_phase
from tests.run.helpers import accepted, request

A = Action
P = Phase
X, Y = "x0000001", "x0000002"
MCP = "http://127.0.0.1:8740/mcp"


def job_log(fx: Factory, run_id: str) -> list[dict[str, object]]:
    path = fx.data / "runs" / run_id / "jobs.jsonl"
    if not path.is_file():
        return []
    return [
        cast(dict[str, object], json.loads(line))
        for line in path.read_text().splitlines()
    ]


def got(claimed: Json | None) -> Json:
    assert claimed is not None, "expected a job, got 204"
    return claimed


def agent_of(wf: Workflow, claimed: Json) -> Principal:
    agent = wf.authenticate(str(claimed["agent_token"]))
    assert agent is not None
    return agent


async def fail_attempt(wf: Workflow, runner: str = X) -> Json:
    """A job whose agent exits 1 without moving the run."""
    claimed = got(await wf.jobs.claim(runner, [], MCP))
    await wf.jobs.finish(runner, str(claimed["job"]), "exited", 1, "/logs/a")
    return claimed


# Requirement: Runs in an agent phase want an agent


def test_waiting_for_review_gets_no_job(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.SPEC_REVIEW)
        assert await wf.jobs.claim(X, [], MCP) is None

    fx.run(scenario)


def test_after_a_rejection(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.TESTS_REVIEW)
        accepted(
            await wf.store.transition(run_id, request(A.REJECT_TESTS, note="redo"))
        )
        claimed = got(await wf.jobs.claim(X, [], MCP))
        assert (claimed["run_id"], claimed["phase"]) == (run_id, "tests")

    fx.run(scenario)


# Requirement: Claiming a job


def test_claim(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        assert claimed["job"] == "demo.1.j1"
        assert (claimed["run_id"], claimed["change"], claimed["phase"]) == (
            run_id,
            "demo",
            "spec",
        )
        assert claimed["worktree"] == str(fx.root)
        assert claimed["mcp_url"] == MCP
        assert "get_task" in str(claimed["prompt"])
        assert run_id in str(claimed["prompt"])
        agent = agent_of(wf, claimed)
        assert (agent.role, agent.run_id) == (Role.AGENT, run_id)

    fx.run(scenario)


def test_two_runners_one_run(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        await start(wf, fx)
        results = await asyncio.gather(
            wf.jobs.claim(X, [], MCP), wf.jobs.claim(Y, [], MCP)
        )
        assert sorted(r is None for r in results) == [False, True]
        assert len([e for e in job_log(fx, "demo.1") if e["event"] == "Claim"]) == 1

    fx.run(scenario)


def test_the_run_waiting_longest_is_claimed_first(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        a, _ = await start(wf, fx, "a")
        b, _ = await start(wf, fx, "b")
        first = got(await wf.jobs.claim(X, [], MCP))
        second = got(await wf.jobs.claim(Y, [], MCP))
        assert (first["run_id"], second["run_id"]) == (a, b)

    fx.run(scenario)


# Requirement: A claim issues the run's only agent token


def test_claim_recorded(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        seq = wf.store.get(run_id).seq
        claimed = got(await wf.jobs.claim(X, [], MCP))
        agent = agent_of(wf, claimed)
        last = job_log(fx, run_id)[-1]
        assert last["event"] == "Claim"
        assert (last["job"], last["runner"], last["claim_seq"]) == (
            "demo.1.j1",
            X,
            seq,
        )
        assert last["token_id"] == agent.token_id

    fx.run(scenario)


def test_a_new_claim_revokes_the_old_token(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        _, stray = await start_with_token(wf, fx)
        assert wf.authenticate(stray) is not None
        claimed = got(await wf.jobs.claim(X, [], MCP))
        assert wf.authenticate(stray) is None
        assert wf.authenticate(str(claimed["agent_token"])) is not None

    fx.run(scenario)


def test_claim_cannot_be_recorded(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        (fx.data / "runs" / run_id / "jobs.jsonl").mkdir(parents=True)
        before = {t.id for t in wf.tokens.list()}
        with pytest.raises(JobError) as refused:
            await wf.jobs.claim(X, [], MCP)
        assert refused.value.status == 503
        issued = [t for t in wf.tokens.list() if t.id not in before]
        assert issued, "the claim issued no token before failing"
        assert all(t.revoked for t in issued)

    fx.run(scenario)


# Requirement: Jobs end only through their runner


def test_hello_after_a_restart(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        hello = await wf.jobs.hello(X)
        assert hello["released"] == ["demo.1.j1"]
        last = job_log(fx, run_id)[-1]
        assert (last["event"], last["job"], last["why"]) == (
            "Release",
            "demo.1.j1",
            "runner_restart",
        )
        assert wf.authenticate(str(claimed["agent_token"])) is None
        again = got(await wf.jobs.claim(Y, [], MCP))
        assert (again["run_id"], again["job"]) == (run_id, "demo.1.j2")

    fx.run(scenario)


def test_a_silent_runner_keeps_its_job(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        await start(wf, fx)
        got(await wf.jobs.claim(X, [], MCP))
        assert await wf.jobs.claim(Y, [], MCP) is None
        assert (await wf.jobs.hello(Y))["released"] == []
        assert wf.jobs.view("demo.1")["state"] == "running"

    fx.run(scenario)


def test_finish_closes_the_job_and_revokes_its_token(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        await wf.jobs.finish(X, str(claimed["job"]), "exited", 0, "/logs/j1")
        assert wf.authenticate(str(claimed["agent_token"])) is None
        last = job_log(fx, run_id)[-1]
        assert (last["event"], last["outcome"], last["exit_code"], last["log"]) == (
            "Finish",
            "exited",
            0,
            "/logs/j1",
        )
        assert last["finish_seq"] == wf.store.get(run_id).seq

    fx.run(scenario)


def test_release_closes_the_job(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        await wf.jobs.release(X, str(claimed["job"]))
        assert wf.authenticate(str(claimed["agent_token"])) is None
        last = job_log(fx, run_id)[-1]
        assert (last["event"], last["why"]) == ("Release", "runner_shutdown")
        assert wf.jobs.view(run_id)["state"] == "waiting"

    fx.run(scenario)


# Requirement: Closing a job revokes its token


def test_finish_twice(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        job = str(got(await wf.jobs.claim(X, [], MCP))["job"])
        await wf.jobs.finish(X, job, "exited", 0, "/l")
        events = job_log(fx, run_id)
        await wf.jobs.finish(X, job, "exited", 0, "/l")
        await wf.jobs.release(X, job)
        assert job_log(fx, run_id) == events

    fx.run(scenario)


@pytest.mark.parametrize("operation", ["release", "finish", "poll"])
def test_another_runners_job(fx: Factory, operation: str) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        job = str(got(await wf.jobs.claim(X, [], MCP))["job"])
        calls = {
            "release": lambda: wf.jobs.release(Y, job),
            "finish": lambda: wf.jobs.finish(Y, job, "exited", 0, "/l"),
            "poll": lambda: wf.jobs.poll(Y, job),
        }
        call = calls[operation]
        with pytest.raises(JobError) as refused:
            await call()
        assert refused.value.status == 404
        view = wf.jobs.view(run_id)
        assert (view["state"], view["runner"]) == ("running", X)

    fx.run(scenario)


def test_unknown_job(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        await start(wf, fx)
        with pytest.raises(JobError) as refused:
            await wf.jobs.poll(X, "nope.1.j1")
        assert refused.value.status == 404

    fx.run(scenario)


# Requirement: Claims reconcile the runner's jobs


def test_a_claim_response_is_lost(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        lost = got(await wf.jobs.claim(X, [], MCP))  # the runner never saw it
        again = got(await wf.jobs.claim(X, [], MCP))
        assert (again["run_id"], again["job"]) == (run_id, "demo.1.j2")
        released = [e for e in job_log(fx, run_id) if e["event"] == "Release"]
        assert [(e["job"], e["why"]) for e in released] == [("demo.1.j1", "lost")]
        assert wf.authenticate(str(lost["agent_token"])) is None
        assert wf.authenticate(str(again["agent_token"])) is not None
        assert wf.jobs.view(run_id)["attempts"] == 0

    fx.run(scenario)


def test_jobs_the_runner_lists_stay_open(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        a, _ = await start(wf, fx, "a")
        b, _ = await start(wf, fx, "b")
        first = got(await wf.jobs.claim(X, [], MCP))
        assert first["run_id"] == a
        second = got(await wf.jobs.claim(X, [str(first["job"])], MCP))
        assert second["run_id"] == b
        assert wf.jobs.view(a)["state"] == "running"
        assert wf.authenticate(str(first["agent_token"])) is not None

    fx.run(scenario)


def test_reconciling_happens_even_without_a_job_to_give(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        agent = agent_of(wf, claimed)
        await to_phase(wf, fx, run_id, agent, P.SPEC_REVIEW)
        assert await wf.jobs.claim(X, [], MCP) is None
        last = job_log(fx, run_id)[-1]
        assert (last["event"], last["job"], last["why"]) == (
            "Release",
            "demo.1.j1",
            "lost",
        )

    fx.run(scenario)


# Requirement: Failed attempts and stuck runs


def test_progress_is_not_a_failure(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        await wf.submit_for_review(agent_of(wf, claimed))  # spec -> model
        await wf.jobs.finish(X, str(claimed["job"]), "exited", 0, "/l")
        assert wf.jobs.view(run_id)["attempts"] == 0
        again = got(await wf.jobs.claim(X, [], MCP))
        assert (again["job"], again["phase"]) == ("demo.1.j2", "model")

    fx.run(scenario)


def test_two_failures_make_a_run_stuck(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        await fail_attempt(wf)
        assert wf.jobs.view(run_id)["attempts"] == 1
        assert not wf.jobs.stuck(run_id)
        await fail_attempt(wf)
        assert wf.jobs.stuck(run_id)
        view = wf.jobs.view(run_id)
        assert (view["state"], view["attempts"], view["log"]) == ("stuck", 2, "/logs/a")
        assert await wf.jobs.claim(Y, [], MCP) is None

    fx.run(scenario)


def test_a_released_job_is_not_an_attempt(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        await fail_attempt(wf)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        await wf.jobs.release(X, str(claimed["job"]))
        assert not wf.jobs.stuck(run_id)
        assert got(await wf.jobs.claim(X, [], MCP))["run_id"] == run_id

    fx.run(scenario)


def test_attempts_from_the_environment() -> None:
    assert ServerConfig.from_env({}).agent_attempts == 2
    assert ServerConfig.from_env({"REFINERY_AGENT_ATTEMPTS": "3"}).agent_attempts == 3


# Requirement: Retrying a stuck run


def test_retry(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        await fail_attempt(wf)
        await fail_attempt(wf)
        await wf.jobs.retry(run_id)
        assert not wf.jobs.stuck(run_id)
        assert job_log(fx, run_id)[-1]["event"] == "Retry"
        assert got(await wf.jobs.claim(X, [], MCP))["run_id"] == run_id

    fx.run(scenario)


def test_retry_a_run_that_is_not_stuck(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        with pytest.raises(JobError) as refused:
            await wf.jobs.retry(run_id)
        assert refused.value.status == 409
        assert job_log(fx, run_id) == []

    fx.run(scenario)


# Requirement: Polling tells the runner when to stop


def test_poll_continues_in_an_agent_phase(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        await start(wf, fx)
        job = str(got(await wf.jobs.claim(X, [], MCP))["job"])
        assert (await wf.jobs.poll(X, job))["action"] == "continue"
        assert wf.jobs.view("demo.1")["runner_last_seen"] is not None

    fx.run(scenario)


def test_poll_after_a_cancel(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        job = str(got(await wf.jobs.claim(X, [], MCP))["job"])
        accepted(await wf.store.transition(run_id, request(A.CANCEL)))
        answer = await wf.jobs.poll(X, job)
        assert answer["action"] == "stop"
        assert "cancelled" in str(answer["why"])

    fx.run(scenario)


def test_poll_after_submitting_into_review(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        claimed = got(await wf.jobs.claim(X, [], MCP))
        await to_phase(wf, fx, run_id, agent_of(wf, claimed), P.SPEC_REVIEW)
        assert (await wf.jobs.poll(X, str(claimed["job"])))["action"] == "stop"

    fx.run(scenario)


def test_poll_a_closed_job(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        await start(wf, fx)
        job = str(got(await wf.jobs.claim(X, [], MCP))["job"])
        await wf.jobs.finish(X, job, "exited", 0, "/l")
        assert (await wf.jobs.poll(X, job))["action"] == "stop"

    fx.run(scenario)


# Requirement: The job log


def test_restart_keeps_a_stuck_run_stuck(fx: Factory) -> None:
    async def make_stuck(wf: Workflow) -> None:
        await start(wf, fx)
        await fail_attempt(wf)
        await fail_attempt(wf)

    async def after_restart(wf: Workflow) -> None:
        assert wf.jobs.stuck("demo.1")
        assert await wf.jobs.claim(X, [], MCP) is None

    fx.run(make_stuck)
    fx.run(after_restart)


def test_job_view_of_a_run_in_review(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.SPEC_REVIEW)
        view = wf.jobs.view(run_id)
        assert view["state"] == "none"
        assert set(view) >= {"id", "state", "runner", "runner_last_seen", "attempts"}

    fx.run(scenario)
