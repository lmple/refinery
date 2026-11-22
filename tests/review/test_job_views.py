"""The runner change's delta to review-queue ("The review queue": stuck runs)
and the job in Review.show (human-api, "Reading runs"), against Review."""

from typing import cast

from refinery.factoryd.workflow import Workflow
from refinery.review.service import Review
from tests.review.conftest import ReviewFactory, started, to_review

X = "x0000001"
MCP = "http://127.0.0.1:8740/mcp"


async def make_stuck(wf: Workflow) -> None:
    for _ in range(2):
        claimed = await wf.jobs.claim(X, [], MCP)
        assert claimed is not None
        await wf.jobs.finish(X, str(claimed["job"]), "exited", 1, "/logs/x")


def test_queue_contents_unchanged_by_jobs(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        a, agent_a = await started(wf, rf, "a")
        b, _ = await started(wf, rf, "b")
        await to_review(wf, rf, a, agent_a)
        assert [r["run_id"] for r in await review.queue()] == [a]
        assert not wf.jobs.stuck(b)

    rf.run_review(scenario)


def test_a_stuck_run_is_in_the_queue(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, _ = await started(wf, rf)
        await make_stuck(wf)
        queued = await review.queue()
        assert [r["run_id"] for r in queued] == [run_id]
        assert queued[0]["phase"] == "spec"
        assert queued[0]["stuck"] is True
        assert float(str(queued[0]["waiting_s"])) >= 0
        await review.retry(run_id)
        assert await review.queue() == []

    rf.run_review(scenario)


def test_show_includes_the_job(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, _ = await started(wf, rf)
        shown = await review.show(run_id)
        job = cast(dict[str, object], shown["job"])
        assert (job["state"], job["attempts"]) == ("waiting", 0)
        await make_stuck(wf)
        job = cast(dict[str, object], (await review.show(run_id))["job"])
        assert (job["state"], job["attempts"], job["log"]) == ("stuck", 2, "/logs/x")

    rf.run_review(scenario)
