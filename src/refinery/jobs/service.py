"""Jobs: factoryd's side of the runner protocol (runner D4, D5).

Claims are serialised by one lock. A claim first releases the caller's open
jobs it did not list (lost claim responses), then hands out the run that has
wanted an agent longest, with a fresh agent token that revokes the run's
others. Only a job's own runner closes it: finish, release, hello or an
unlisted claim. ``spec/Runner.tla`` models the protocol.
"""

from collections.abc import Sequence
from pathlib import Path

from refinery.auth.tokens import TokenStore
from refinery.jobs.state import DEFAULT_MAX_ATTEMPTS
from refinery.run.store import RunStore

type Json = dict[str, object]


class JobError(Exception):
    """A refused runner or human operation: ``status`` is the HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        """404 unknown job or run, 409 wrong state, 503 job log not writable."""
        super().__init__(message)
        self.status = status
        self.message = message


class Jobs:
    """Jobs of every run in ``store``, logged under ``data_dir``."""

    def __init__(
        self,
        store: RunStore,
        tokens: TokenStore,
        data_dir: Path,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """``max_attempts``: failed attempts that make a run stuck."""
        self.store = store
        self.tokens = tokens
        self.data_dir = data_dir
        self.max_attempts = max_attempts

    async def claim(
        self, runner: str, holding: Sequence[str], mcp_url: str
    ) -> Json | None:
        """Reconcile ``runner``'s jobs with ``holding``, then a job, or None (204).

        A job is ``{job, run_id, change, phase, worktree, agent_token, mcp_url,
        prompt}``.
        """
        raise NotImplementedError("claims (runner D5)")

    async def poll(self, runner: str, job: str) -> Json:
        """``{"action": "continue"}`` or ``{"action": "stop", "why": ...}``."""
        raise NotImplementedError("polling (runner D5)")

    async def finish(
        self, runner: str, job: str, outcome: str, exit_code: int | None, log: str
    ) -> Json:
        """Record how the agent call ended; idempotent on a closed job."""
        raise NotImplementedError("finish (runner D5)")

    async def release(self, runner: str, job: str) -> Json:
        """Release (runner_shutdown); idempotent on a closed job."""
        raise NotImplementedError("release (runner D5)")

    async def hello(self, runner: str) -> Json:
        """Release (runner_restart) every open job of ``runner``: ``{released}``."""
        raise NotImplementedError("hello (runner D5)")

    async def retry(self, run_id: str) -> Json:
        """Retry a stuck run; JobError(409) if it is not stuck."""
        raise NotImplementedError("retry (runner D8)")

    def view(self, run_id: str) -> Json:
        """``{id, state, runner, runner_last_seen, attempts, log}`` for status."""
        raise NotImplementedError("job view (runner D8)")

    def stuck(self, run_id: str) -> bool:
        """Whether ``run_id`` is stuck."""
        raise NotImplementedError("stuck runs (runner D4)")
