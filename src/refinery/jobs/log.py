"""The job log: ``<data>/runs/<run-id>/jobs.jsonl``, append-only (runner D4).

One JSON object per line, ``{"event": <name>, ...fields}``. Event names are
``spec/Runner.tla``'s actions: Claim, Finish, Release and Retry. Every event
is fsynced before its effect is reported (agent-jobs, "The job log").
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

JOB_LOG = "jobs.jsonl"


class EventName(StrEnum):
    """Job log event names, equal to Runner.tla's action names."""

    CLAIM = "Claim"
    FINISH = "Finish"
    RELEASE = "Release"
    RETRY = "Retry"


class ReleaseReason(StrEnum):
    """Why a job was released; never an attempt."""

    RUNNER_RESTART = "runner_restart"  # hello after a restart
    RUNNER_SHUTDOWN = "runner_shutdown"  # release on SIGINT/SIGTERM
    LOST = "lost"  # a claim response the runner never got


@dataclass(frozen=True)
class Claim:
    """A runner took the run: ``claim_seq`` is the run's seq at the claim."""

    job: str
    runner: str
    token_id: str
    claim_seq: int
    at: datetime


@dataclass(frozen=True)
class Finish:
    """The agent call ended: ``finish_seq`` is the run's seq when recorded."""

    job: str
    outcome: str  # a supervisor Outcome value
    exit_code: int | None
    log: str
    finish_seq: int
    at: datetime


@dataclass(frozen=True)
class Release:
    """The job was given back without an agent result."""

    job: str
    why: ReleaseReason
    at: datetime


@dataclass(frozen=True)
class Retry:
    """The human re-queued a stuck run."""

    at: datetime


type JobEvent = Claim | Finish | Release | Retry


class JobLogError(Exception):
    """The job log could not be written or read."""


def log_path(data_dir: Path, run_id: str) -> Path:
    """Where ``run_id``'s job log lives."""
    return data_dir / "runs" / run_id / JOB_LOG


def to_json(event: JobEvent) -> str:
    """One log line (no newline)."""
    raise NotImplementedError("job log encoding (runner D4)")


def from_json(line: str) -> JobEvent:
    """The event a log line holds."""
    raise NotImplementedError("job log decoding (runner D4)")


def append(path: Path, event: JobEvent) -> None:
    """Append and fsync ``event``; JobLogError if that fails."""
    raise NotImplementedError("job log append (runner D4)")


def read(path: Path) -> list[JobEvent]:
    """Every event in the log, oldest first; [] if there is no log."""
    raise NotImplementedError("job log read (runner D4)")
