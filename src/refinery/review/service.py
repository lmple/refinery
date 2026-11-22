"""The Review service: every human operation, for the API and refine (D1)."""

from refinery.auth.tokens import Principal
from refinery.factoryd.workflow import Json, Workflow


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

    async def runs(self) -> list[Json]:
        """Every run, newest first."""
        raise NotImplementedError

    async def queue(self) -> list[Json]:
        """Unfinished runs whose next move is the human's."""
        raise NotImplementedError

    async def show(self, run_id: str) -> Json:
        """One run: phase, frozen commits, last decision, PR, publish error, gate."""
        raise NotImplementedError

    async def start(self, who: Principal, change: str, worktree: str) -> Json:
        """Start a run (start_workflow); returns the agent token once."""
        raise NotImplementedError

    async def approve(self, run_id: str, note: str | None = None) -> Json:
        """ApproveSpec / ApproveTests / ApproveFinal, from the phase."""
        raise NotImplementedError

    async def reject(self, run_id: str, note: str | None) -> Json:
        """RejectSpec / RejectTests / RejectFinal, with a note."""
        raise NotImplementedError

    async def cancel(self, run_id: str) -> Json:
        """Cancel an unfinished run."""
        raise NotImplementedError

    async def resolve(self, run_id: str, to: str) -> Json:
        """Resolve an escalation to spec, tests or impl."""
        raise NotImplementedError

    async def retry_archive(self, run_id: str) -> Json:
        """Resume a stopped archive sequence (RetryArchive after a trace failure)."""
        raise NotImplementedError
