"""Scenarios of specs/review-publishing/spec.md (review-queue): publishing at
review gates, failing open, and the branch-worktree rule."""

import json

import pytest

from refinery.factoryd.workflow import Workflow, WorkflowError
from refinery.review.publish import publish_path, read_state
from refinery.review.service import Review, ReviewError
from refinery.run.model import Phase
from tests.factoryd.conftest import HUMAN
from tests.gate.conftest import BRANCH, CHANGE, git
from tests.review.conftest import PR_URL, ReviewFactory, started, to_review

P = Phase


# Requirement: Publishing at review gates


def test_first_review_gate(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        head = rf.current_head()
        assert rf.remote_head(BRANCH) == head
        record = git(rf.root, "show", f"HEAD:.refinery/runs/{run_id}.json")
        assert json.loads(record)["phase"] == "spec_review"
        assert git(rf.root, "log", "-1", "--format=%s") == f"run: {run_id} spec_review"
        creates = [c for c in rf.log("gh") if c.startswith("pr create")]
        assert len(creates) == 1
        assert "--draft" in creates[0]
        assert f"--head {BRANCH}" in creates[0]
        assert "--base main" in creates[0]
        state = read_state(rf.data, run_id)
        assert (state.pr, state.branch, state.published) == (PR_URL, BRANCH, head)
        assert state.last_error is None
        assert publish_path(rf.data, run_id).exists()

    rf.run_review(scenario)


def test_later_gates_reuse_the_pr(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        first = rf.current_head()
        await review.approve(run_id)
        await wf.submit_for_review(agent)  # tests -> tests_review
        assert rf.remote_head(BRANCH) == rf.current_head() != first
        assert len([c for c in rf.log("gh") if c.startswith("pr create")]) == 1
        assert read_state(rf.data, run_id).pr == PR_URL

    rf.run_review(scenario)


def test_escalation_is_published(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        await review.approve(run_id)
        await wf.escalate(agent, "spec_bug", "scenario 2 contradicts scenario 3")
        assert git(rf.root, "log", "-1", "--format=%s") == f"run: {run_id} escalated"
        assert rf.remote_head(BRANCH) == rf.current_head()

    rf.run_review(scenario)


def test_get_task_and_show_report_the_pr(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        assert (await wf.get_task(agent))["pr"] is None
        await to_review(wf, rf, run_id, agent)
        assert (await wf.get_task(agent))["pr"] == PR_URL
        assert (await review.show(run_id))["pr"] == PR_URL

    rf.run_review(scenario)


# Requirement: Publishing fails open


def test_push_rejected(rf: ReviewFactory) -> None:
    rf.flag("push_rejected")

    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        assert wf.store.get(run_id).phase is P.SPEC_REVIEW
        state = read_state(rf.data, run_id)
        assert state.last_error
        assert state.published == rf.current_head()  # reviewable locally
        assert (await review.show(run_id))["publish_error"]
        assert not any(c.startswith("pr create") for c in rf.log("gh"))
        assert (await review.approve(run_id))["phase"] == "tests"

    rf.run_review(scenario)


def test_publishing_recovers_at_the_next_gate(rf: ReviewFactory) -> None:
    rf.flag("push_rejected")

    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        await review.approve(run_id)
        rf.flag("push_rejected", on=False)
        await wf.submit_for_review(agent)  # tests -> tests_review
        state = read_state(rf.data, run_id)
        assert state.last_error is None
        assert state.pr == PR_URL
        assert rf.remote_head(BRANCH) == rf.current_head()

    rf.run_review(scenario)


# Requirement: Runs need a branch worktree


def test_start_on_main(rf: ReviewFactory) -> None:
    git(rf.root, "checkout", "-q", "main")

    async def scenario(wf: Workflow, review: Review) -> None:
        with pytest.raises(WorkflowError, match="main"):
            await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        with pytest.raises(ReviewError) as refused:
            await review.start(HUMAN, CHANGE, str(rf.root))
        assert refused.value.status == 409
        assert wf.store.runs() == []

    rf.run_review(scenario)


def test_start_on_a_detached_head(rf: ReviewFactory) -> None:
    git(rf.root, "checkout", "-q", "--detach")

    async def scenario(wf: Workflow, review: Review) -> None:
        with pytest.raises(WorkflowError):
            await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        assert wf.store.runs() == []

    rf.run_review(scenario)


def test_start_through_review(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        started_run = await review.start(HUMAN, CHANGE, str(rf.root))
        assert (started_run["run_id"], started_run["phase"]) == ("demo.1", "spec")
        assert str(started_run["agent_token"]).startswith("rft_a_")

    rf.run_review(scenario)
