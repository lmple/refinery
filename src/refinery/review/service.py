"""The Review service: every human operation, for the API and refine (D1).

``spec/Review.tla`` models the decisions. An approval is of the published
commit, with a clean worktree (ApprovalsAreOfPublishedCommits); a final
approval starts the archive sequence; retry-archive resumes it.
"""

from datetime import UTC, datetime

from refinery.auth.tokens import Principal
from refinery.factoryd.workflow import GATES, Json, Workflow, WorkflowError
from refinery.gate.frozen import frozen_paths
from refinery.review.publish import read_state
from refinery.review.repo import GitError
from refinery.run.model import (
    TERMINAL,
    Action,
    Artifact,
    Phase,
    Refused,
    RunRecord,
    TransitionRequest,
    Verdict,
)
from refinery.run.model import Role as RunRole

_APPROVE = {
    Phase.SPEC_REVIEW: Action.APPROVE_SPEC,
    Phase.TESTS_REVIEW: Action.APPROVE_TESTS,
    Phase.FINAL_REVIEW: Action.APPROVE_FINAL,
}
_REJECT = {
    Phase.SPEC_REVIEW: Action.REJECT_SPEC,
    Phase.TESTS_REVIEW: Action.REJECT_TESTS,
    Phase.FINAL_REVIEW: Action.REJECT_FINAL,
}
_RESOLVE = {
    "spec": Action.RESOLVE_TO_SPEC,
    "tests": Action.RESOLVE_TO_TESTS,
    "impl": Action.RESOLVE_TO_IMPL,
}


class ReviewError(Exception):
    """A refused human operation: ``status`` is the HTTP status to answer."""

    def __init__(self, status: int, message: str) -> None:
        """404 unknown run, 409 refused / wrong phase, 422 invalid request."""
        super().__init__(message)
        self.status = status
        self.message = message


class Review:
    """Human decisions, queue and recovery over a Workflow."""

    def __init__(self, workflow: Workflow) -> None:
        """Uses the workflow's store, supervisor, config and validator."""
        self.workflow = workflow

    # Reading

    async def runs(self) -> list[Json]:
        """Every run, newest first."""
        records = sorted(
            self.workflow.store.runs(), key=lambda r: r.updated_at, reverse=True
        )
        return [self._summary(r) for r in records]

    async def queue(self) -> list[Json]:
        """Unfinished runs whose next move is the human's."""
        now = datetime.now(UTC)
        waiting = [r for r in self.workflow.store.runs() if self._waits_for_human(r)]
        waiting.sort(key=lambda r: r.updated_at)
        return [
            {**self._summary(r), "waiting_s": (now - r.updated_at).total_seconds()}
            for r in waiting
        ]

    async def show(self, run_id: str) -> Json:
        """One run: phase, frozen commits, last decision, PR, publish error, gate."""
        record = self._get(run_id)
        state = read_state(self.workflow.config.data_dir, run_id)
        paths = frozen_paths(record.change)
        commits = {
            Artifact.SPEC: record.spec_commit,
            Artifact.TESTS: record.tests_commit,
        }
        task_view = self.workflow.store.events(run_id)
        gate = next((e.gate for e in reversed(task_view) if e.gate), None)
        return {
            **self._summary(record),
            "frozen": [
                {"artifact": a.value, "commit": commits[a], "paths": list(paths[a])}
                for a in sorted(record.frozen)
            ],
            "last_decision": self.workflow.last_decision(run_id),
            "pr": state.pr,
            "publish_error": state.last_error,
            "published": state.published,
            "gate": gate,
            "trace": record.trace.verdict.value,
        }

    # Acting

    async def start(self, who: Principal, change: str, worktree: str) -> Json:
        """Start a run (start_workflow); returns the agent token once."""
        try:
            return await self.workflow.start_workflow(who, change, worktree)
        except WorkflowError as exc:
            raise ReviewError(409, str(exc)) from exc

    async def approve(self, run_id: str, note: str | None = None) -> Json:
        """ApproveSpec / ApproveTests / ApproveFinal, from the phase."""
        record = self._get(run_id)
        name = _APPROVE.get(record.phase)
        if name is None:
            raise ReviewError(409, f"{run_id} is in {record.phase}: nothing to approve")
        head = await self._reviewable_head(record)
        commit = head if name in (Action.APPROVE_SPEC, Action.APPROVE_TESTS) else None
        result = await self._transition(run_id, name, note=note, commit=commit)
        if name is Action.APPROVE_FINAL:
            self.workflow.schedule_archive(run_id)
        return result

    async def reject(self, run_id: str, note: str | None) -> Json:
        """RejectSpec / RejectTests / RejectFinal, with a note."""
        if note is None or not note.strip():
            raise ReviewError(422, "a rejection needs a note saying what to change")
        record = self._get(run_id)
        name = _REJECT.get(record.phase)
        if name is None:
            raise ReviewError(409, f"{run_id} is in {record.phase}: nothing to reject")
        return await self._transition(run_id, name, note=note)

    async def cancel(self, run_id: str) -> Json:
        """Cancel an unfinished run."""
        self._get(run_id)
        result = await self._transition(run_id, Action.CANCEL)
        self.workflow.gates.cancel(run_id)
        return result

    async def resolve(self, run_id: str, to: str) -> Json:
        """Resolve an escalation to spec, tests or impl."""
        name = _RESOLVE.get(to)
        if name is None:
            raise ReviewError(422, f"resolve to spec, tests or impl, not {to!r}")
        record = self._get(run_id)
        if record.phase is not Phase.ESCALATED:
            raise ReviewError(409, f"{run_id} is in {record.phase}, not escalated")
        return await self._transition(run_id, name)

    async def retry_archive(self, run_id: str) -> Json:
        """Resume a stopped archive sequence (RetryArchive after a trace failure)."""
        record = self._get(run_id)
        state = read_state(self.workflow.config.data_dir, run_id)
        unfinished = record.phase is Phase.ARCHIVED and not state.finished
        if record.phase is not Phase.ARCHIVE and not unfinished:
            raise ReviewError(
                409, f"{run_id} is in {record.phase}: no archive to retry"
            )
        result = self._summary(record)
        if record.phase is Phase.ARCHIVE and record.trace.verdict is Verdict.FAIL:
            result = await self._transition(run_id, Action.RETRY_ARCHIVE)
        self.workflow.schedule_archive(run_id)
        return result

    # Internals

    def _get(self, run_id: str) -> RunRecord:
        try:
            return self.workflow.store.get(run_id)
        except KeyError:
            raise ReviewError(404, f"no run {run_id}") from None

    async def _reviewable_head(self, record: RunRecord) -> str:
        """HEAD, if it is what the human reviewed: clean, and the published commit."""
        repo = self.workflow.repo(record.run_id)
        try:
            dirty = await repo.dirty_paths()
            head = await repo.head()
        except GitError as exc:
            raise ReviewError(409, f"cannot check the worktree: {exc}") from exc
        if dirty:
            raise ReviewError(
                409, f"the worktree has uncommitted changes: {', '.join(dirty)}"
            )
        published = read_state(self.workflow.config.data_dir, record.run_id).published
        if published != head:
            seen = published[:7] if published else "nothing"
            raise ReviewError(
                409,
                f"HEAD {head[:7]} is not the commit published for this gate ({seen}): "
                "there are commits you have not reviewed",
            )
        return head

    async def _transition(
        self,
        run_id: str,
        name: Action,
        *,
        note: str | None = None,
        commit: str | None = None,
    ) -> Json:
        request = TransitionRequest(
            name, RunRole.HUMAN, datetime.now(UTC), note=note, commit=commit
        )
        outcome = await self.workflow.store.transition(run_id, request)
        if isinstance(outcome, Refused):
            raise ReviewError(409, outcome.reason)
        return self._summary(outcome.record)

    def _waits_for_human(self, record: RunRecord) -> bool:
        if record.phase in GATES:
            return True
        if record.phase is not Phase.ARCHIVE:
            return False
        state = read_state(self.workflow.config.data_dir, record.run_id)
        return record.trace.verdict is Verdict.FAIL or state.last_error is not None

    @staticmethod
    def _summary(record: RunRecord) -> Json:
        return {
            "run_id": record.run_id,
            "change": record.change,
            "phase": record.phase.value,
            "finished": record.phase in TERMINAL,
            "updated_at": record.updated_at.isoformat(),
        }
