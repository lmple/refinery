"""Scenarios of specs/review-queue/spec.md (review-queue) against Review."""

import pytest

from refinery.factoryd.workflow import Workflow
from refinery.review.service import Review, ReviewError
from refinery.run.model import Action, BugKind, Phase
from tests.review.conftest import (
    ReviewFactory,
    commit_file,
    started,
    to_final_review,
    to_review,
)

A = Action
P = Phase


# Requirement: Decisions at review gates


def test_approve_the_spec(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        result = await review.approve(run_id)
        assert result["phase"] == "tests"
        assert wf.store.events(run_id)[-1].name is A.APPROVE_SPEC

    rf.run_review(scenario)


def test_reject_the_tests_with_a_note(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        await review.approve(run_id)
        await wf.submit_for_review(agent)  # tests -> tests_review
        result = await review.reject(run_id, "test 4 tests nothing")
        assert result["phase"] == "tests"
        last = wf.store.events(run_id)[-1]
        assert (last.name, last.note) == (A.REJECT_TESTS, "test 4 tests nothing")

    rf.run_review(scenario)


@pytest.mark.parametrize("note", [None, "", "   "])
def test_rejection_needs_a_note(rf: ReviewFactory, note: str | None) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        with pytest.raises(ReviewError) as refused:
            await review.reject(run_id, note)
        assert refused.value.status == 422
        assert wf.store.get(run_id).phase is P.SPEC_REVIEW

    rf.run_review(scenario)


def test_nothing_to_decide(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, _ = await started(wf, rf)
        before = wf.store.events(run_id)
        with pytest.raises(ReviewError) as refused:
            await review.approve(run_id)
        assert refused.value.status == 409
        assert wf.store.events(run_id) == before

    rf.run_review(scenario)


def test_unknown_run(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        with pytest.raises(ReviewError) as refused:
            await review.approve("nope.1")
        assert refused.value.status == 404
        assert "nope.1" in refused.value.message

    rf.run_review(scenario)


# Requirement: factoryd records the reviewed commit


def test_commit_recorded_on_approval(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        head = rf.current_head()
        await review.approve(run_id)
        assert wf.store.get(run_id).spec_commit == head
        assert wf.store.events(run_id)[-1].commit == head

    rf.run_review(scenario)


def test_new_commit_after_publishing(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        published = rf.current_head()
        sneaked = commit_file(rf, "src/late.py", "LATE = 1\n", "after publishing")
        with pytest.raises(ReviewError) as refused:
            await review.approve(run_id)
        assert refused.value.status == 409
        assert published[:7] in refused.value.message
        assert sneaked[:7] in refused.value.message
        assert wf.store.get(run_id).phase is P.SPEC_REVIEW

    rf.run_review(scenario)


def test_dirty_worktree_at_approval(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        await review.approve(run_id)
        await wf.submit_for_review(agent)  # tests -> tests_review
        (rf.root / "tests" / "test_x.py").write_text("# edited after submitting\n")
        with pytest.raises(ReviewError) as refused:
            await review.approve(run_id)
        assert refused.value.status == 409
        assert "uncommitted" in refused.value.message
        assert wf.store.get(run_id).phase is P.TESTS_REVIEW

    rf.run_review(scenario)


def test_final_approval_also_needs_a_clean_worktree(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_final_review(wf, review, rf, run_id, agent)
        (rf.root / "src" / "x.py").write_text("X = 99\n")
        with pytest.raises(ReviewError) as refused:
            await review.approve(run_id)
        assert refused.value.status == 409
        assert wf.store.get(run_id).phase is P.FINAL_REVIEW

    rf.run_review(scenario)


# Requirement: Recovery decisions


async def _escalated(wf: Workflow, review: Review, rf: ReviewFactory) -> str:
    run_id, agent = await started(wf, rf)
    await to_review(wf, rf, run_id, agent)
    await review.approve(run_id)
    await wf.submit_for_review(agent)
    await review.approve(run_id)  # impl
    await wf.escalate(agent, BugKind.TEST_BUG.value, "test 2 contradicts scenario 1")
    assert wf.store.get(run_id).phase is P.ESCALATED
    return run_id


def test_resolve_to_tests(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id = await _escalated(wf, review, rf)
        result = await review.resolve(run_id, "tests")
        assert result["phase"] == "tests"
        assert wf.store.events(run_id)[-1].name is A.RESOLVE_TO_TESTS

    rf.run_review(scenario)


@pytest.mark.parametrize(
    ("to", "action"), [("spec", A.RESOLVE_TO_SPEC), ("impl", A.RESOLVE_TO_IMPL)]
)
def test_resolve_to_spec_or_impl(rf: ReviewFactory, to: str, action: Action) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id = await _escalated(wf, review, rf)
        await review.resolve(run_id, to)
        assert wf.store.events(run_id)[-1].name is action

    rf.run_review(scenario)


def test_resolve_outside_an_escalation(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, _ = await started(wf, rf)
        with pytest.raises(ReviewError) as refused:
            await review.resolve(run_id, "spec")
        assert refused.value.status == 409
        assert wf.store.get(run_id).phase is P.SPEC

    rf.run_review(scenario)


def test_resolve_to_nowhere(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id = await _escalated(wf, review, rf)
        with pytest.raises(ReviewError) as refused:
            await review.resolve(run_id, "verify")
        assert refused.value.status == 422

    rf.run_review(scenario)


def test_cancel(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, _ = await started(wf, rf)
        assert (await review.cancel(run_id))["phase"] == "cancelled"
        with pytest.raises(ReviewError) as refused:
            await review.cancel(run_id)
        assert refused.value.status == 409

    rf.run_review(scenario)


def test_retry_archive_outside_archive(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, _ = await started(wf, rf)
        with pytest.raises(ReviewError) as refused:
            await review.retry_archive(run_id)
        assert refused.value.status == 409

    rf.run_review(scenario)


# Requirement: The review queue


def test_queue_contents(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        a, agent_a = await started(wf, rf, "a")
        await to_review(wf, rf, a, agent_a)
        b, agent_b = await started(wf, rf, "b")
        await to_review(wf, rf, b, agent_b)
        await review.approve(b)
        await wf.submit_for_review(agent_b)
        await review.approve(b)  # b.1 now in impl
        c = await _escalated(wf, review, rf)
        queue = await review.queue()
        assert {q["run_id"] for q in queue} == {a, c}
        for entry in queue:
            assert {"run_id", "change", "phase", "waiting_s"} <= set(entry)
            assert isinstance(entry["waiting_s"], float | int)
            assert entry["waiting_s"] >= 0
        assert {r["run_id"] for r in await review.runs()} == {a, b, c}

    rf.run_review(scenario)


def test_show_a_run(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_review(wf, rf, run_id, agent)
        shown = await review.show(run_id)
        assert (shown["run_id"], shown["phase"]) == (run_id, "spec_review")
        assert {"frozen", "last_decision", "pr", "publish_error", "gate"} <= set(shown)

    rf.run_review(scenario)
