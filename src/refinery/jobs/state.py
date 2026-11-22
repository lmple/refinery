"""The job state: a pure fold over a run's job log (runner D4).

``spec/Runner.tla`` models it: a run has at most one open job; a finish is a
failed attempt only if nothing moved the run during the job; releases are
never attempts; Retry resets the attempts; a stuck run is handed to nobody.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from refinery.jobs.log import JobEvent
from refinery.run.model import Phase

# The phases in which it is the agent's turn (agent-jobs).
AGENT_PHASES: frozenset[Phase] = frozenset(
    {Phase.SPEC, Phase.MODEL, Phase.TESTS, Phase.IMPL}
)
DEFAULT_MAX_ATTEMPTS = 2


class JobState(StrEnum):
    """A run's job, as shown to the human."""

    NONE = "none"  # not the agent's turn
    WAITING = "waiting"  # wants an agent
    RUNNING = "running"  # an open job
    STUCK = "stuck"  # failed attempts used up


@dataclass(frozen=True)
class OpenJob:
    """The run's open job: claimed, not finished or released."""

    job: str
    runner: str
    token_id: str
    claim_seq: int
    claimed_at: datetime


@dataclass(frozen=True)
class JobsView:
    """What the job log says about a run, given the run's current seq."""

    open: OpenJob | None
    attempts: int  # failed attempts in the current turn since the last Retry
    claims: int  # how many jobs were ever claimed (the next job is .j<claims+1>)
    last_finish_at: datetime | None
    last_log: str | None


def fold(events: Sequence[JobEvent], current_seq: int) -> JobsView:
    """The view of a run whose seq is ``current_seq``, from its job log."""
    raise NotImplementedError("job state fold (runner D4)")


def is_stuck(view: JobsView, max_attempts: int) -> bool:
    """Whether the run's attempts are used up."""
    raise NotImplementedError("stuck runs (runner D4)")


def wants_agent(phase: Phase, view: JobsView, max_attempts: int) -> bool:
    """An agent phase, no open job, not stuck."""
    raise NotImplementedError("runs that want an agent (runner D4)")


def job_state(phase: Phase, view: JobsView, max_attempts: int) -> JobState:
    """The state shown by ``refine status``."""
    raise NotImplementedError("job state (runner D8)")
