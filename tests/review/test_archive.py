"""Requirement "Archive sequence" (review-publishing, review-queue): final
approval archives; failures stop in archive; every step resumes."""

import datetime as dt

from refinery.factoryd.workflow import Workflow
from refinery.review.publish import read_state
from refinery.review.service import Review
from refinery.run.model import Action, Phase, Verdict
from tests.gate.conftest import BRANCH, CHANGE, git
from tests.review.conftest import ReviewFactory, started, to_final_review
from tests.run.helpers import accepted, request

A = Action
P = Phase


def archived_dir(rf: ReviewFactory) -> list[str]:
    archive = rf.root / "openspec" / "changes" / "archive"
    return sorted(p.name for p in archive.iterdir()) if archive.exists() else []


def subjects(rf: ReviewFactory, n: int = 5) -> list[str]:
    return git(rf.root, "log", f"-{n}", "--format=%s").splitlines()


def test_final_approval_archives(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_final_review(wf, review, rf, run_id, agent)
        await review.approve(run_id)
        await wf.wait_idle()
        record = wf.store.get(run_id)
        assert record.phase is P.ARCHIVED
        assert record.trace.verdict is Verdict.PASS
        assert not (rf.root / "openspec" / "changes" / CHANGE).exists()
        assert archived_dir(rf) == [f"{dt.date.today():%Y-%m-%d}-{CHANGE}"]
        assert f"{CHANGE}: archive the OpenSpec change" in subjects(rf)
        assert subjects(rf)[0] == f"run: {run_id} archived"
        assert git(rf.root, "status", "--porcelain") == ""
        assert rf.remote_head(BRANCH) == rf.current_head()
        assert any(c.startswith("pr ready") for c in rf.log("gh"))
        assert read_state(rf.data, run_id).last_error is None
        assert rf.log("openspec") == [f"archive {CHANGE} --yes"]

    rf.run_review(scenario)


def test_openspec_archive_fails(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_final_review(wf, review, rf, run_id, agent)
        rf.flag("openspec_fails")
        await review.approve(run_id)
        await wf.wait_idle()
        assert wf.store.get(run_id).phase is P.ARCHIVE
        assert A.ARCHIVE not in [e.name for e in wf.store.events(run_id)]
        shown = await review.show(run_id)
        assert "openspec" in str(shown["publish_error"])
        assert run_id in {q["run_id"] for q in await review.queue()}
        rf.flag("openspec_fails", on=False)
        await review.retry_archive(run_id)
        await wf.wait_idle()
        assert wf.store.get(run_id).phase is P.ARCHIVED

    rf.run_review(scenario)


def test_resume_after_openspec_archive(rf: ReviewFactory) -> None:
    """Crash after step a: the change is archived and committed, no Archive yet."""

    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_final_review(wf, review, rf, run_id, agent)
        accepted(await wf.store.transition(run_id, request(A.APPROVE_FINAL)))
        archive = rf.root / "openspec" / "changes" / "archive"
        archive.mkdir(parents=True)
        (rf.root / "openspec" / "changes" / CHANGE).rename(
            archive / f"2026-01-01-{CHANGE}"
        )
        git(rf.root, "add", "-A", "openspec")
        git(rf.root, "commit", "-q", "-m", f"{CHANGE}: archive the OpenSpec change")
        await review.retry_archive(run_id)
        await wf.wait_idle()
        assert wf.store.get(run_id).phase is P.ARCHIVED
        assert rf.log("openspec") == []

    rf.run_review(scenario)


def test_resume_on_startup(rf: ReviewFactory) -> None:
    """A run left in archive (factoryd died after ApproveFinal) is finished on start."""

    async def leave_in_archive(wf: Workflow, review: Review) -> str:
        run_id, agent = await started(wf, rf)
        await to_final_review(wf, review, rf, run_id, agent)
        accepted(await wf.store.transition(run_id, request(A.APPROVE_FINAL)))
        return run_id

    run_id = rf.run_review(leave_in_archive)

    async def restart(wf: Workflow, review: Review) -> None:
        await wf.wait_idle()
        assert wf.store.get(run_id).phase is P.ARCHIVED
        assert any(c.startswith("pr ready") for c in rf.log("gh"))

    rf.run_review(restart)


def test_resume_after_archive_before_push(rf: ReviewFactory) -> None:
    """Archived, but the final push and PR ready never happened."""

    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_final_review(wf, review, rf, run_id, agent)
        rf.flag("push_rejected")
        await review.approve(run_id)
        await wf.wait_idle()
        assert wf.store.get(run_id).phase is P.ARCHIVED
        assert not any(c.startswith("pr ready") for c in rf.log("gh"))
        assert read_state(rf.data, run_id).last_error
        rf.flag("push_rejected", on=False)
        await review.retry_archive(run_id)
        await wf.wait_idle()
        assert rf.remote_head(BRANCH) == rf.current_head()
        assert any(c.startswith("pr ready") for c in rf.log("gh"))
        assert rf.log("openspec") == [f"archive {CHANGE} --yes"]

    rf.run_review(scenario)


def test_trace_validation_fails_then_retry(rf: ReviewFactory) -> None:
    async def scenario(wf: Workflow, review: Review) -> None:
        run_id, agent = await started(wf, rf)
        await to_final_review(wf, review, rf, run_id, agent)
        rf.validator.passed = False
        await review.approve(run_id)
        await wf.wait_idle()
        record = wf.store.get(run_id)
        assert (record.phase, record.trace.verdict) == (P.ARCHIVE, Verdict.FAIL)
        rf.validator.passed = True
        await review.retry_archive(run_id)
        await wf.wait_idle()
        assert wf.store.get(run_id).phase is P.ARCHIVED
        assert [e.name for e in wf.store.events(run_id)][-2:] == [
            A.RETRY_ARCHIVE,
            A.ARCHIVE,
        ]
        assert rf.log("openspec") == [f"archive {CHANGE} --yes"]  # not run again

    rf.run_review(scenario)
