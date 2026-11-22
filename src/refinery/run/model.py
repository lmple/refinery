"""Run domain types. Names and values mirror ``spec/Run.tla``."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

SCHEMA_VERSION = 1


class Phase(StrEnum):
    """Phases of a run; values equal the phase strings in ``spec/Run.tla``."""

    NONE = "none"  # before StartRun; only ever the `from` of a StartRun event
    SPEC = "spec"
    MODEL = "model"
    SPEC_REVIEW = "spec_review"
    TESTS = "tests"
    TESTS_REVIEW = "tests_review"
    IMPL = "impl"
    VERIFY = "verify"
    FINAL_REVIEW = "final_review"
    ARCHIVE = "archive"
    ESCALATED = "escalated"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"


TERMINAL: frozenset[Phase] = frozenset({Phase.ARCHIVED, Phase.CANCELLED})


class Role(StrEnum):
    """Who may request a transition."""

    HUMAN = "human"
    AGENT = "agent"
    FACTORYD = "factoryd"


class Artifact(StrEnum):
    """What an approved review gate freezes."""

    SPEC = "spec"  # spec/, openspec/specs/ and the change's spec deltas
    TESTS = "tests"


class Verdict(StrEnum):
    """Trace-validation verdict stored in the run record."""

    NONE = "none"
    PASS = "pass"
    FAIL = "fail"


class BugKind(StrEnum):
    """Classification an agent gives when escalating."""

    SPEC_BUG = "spec_bug"
    TEST_BUG = "test_bug"


class Action(StrEnum):
    """Transitions; values are exactly the action names in ``spec/Run.tla``."""

    START_RUN = "StartRun"
    COMPLETE_SPEC = "CompleteSpec"
    COMPLETE_MODEL = "CompleteModel"
    SKIP_MODEL = "SkipModel"
    APPROVE_SPEC = "ApproveSpec"
    REJECT_SPEC = "RejectSpec"
    COMPLETE_TESTS = "CompleteTests"
    APPROVE_TESTS = "ApproveTests"
    REJECT_TESTS = "RejectTests"
    COMPLETE_IMPL = "CompleteImpl"
    PASS_GATE = "PassGate"
    FAIL_GATE = "FailGate"
    ESCALATE = "Escalate"
    RESOLVE_TO_SPEC = "ResolveToSpec"
    RESOLVE_TO_TESTS = "ResolveToTests"
    RESOLVE_TO_IMPL = "ResolveToImpl"
    APPROVE_FINAL = "ApproveFinal"
    REJECT_FINAL = "RejectFinal"
    ARCHIVE = "Archive"
    RETRY_ARCHIVE = "RetryArchive"
    CANCEL = "Cancel"


@dataclass(frozen=True)
class TraceResult:
    """Trace-validation result; ``events`` and ``sha256`` are set once archived."""

    verdict: Verdict = Verdict.NONE
    events: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class RunRecord:
    """The run record (``.refinery/runs/<run-id>.json``)."""

    run_id: str
    change: str
    phase: Phase
    frozen: frozenset[Artifact]
    seq: int
    trace: TraceResult
    started_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TransitionRequest:
    """A request to perform one transition, as some role."""

    name: Action
    actor: Role
    at: datetime
    note: str | None = None
    reason: str | None = None
    bug: BugKind | None = None
    gate: str | None = None


@dataclass(frozen=True)
class Event:
    """One line of a run's ``events.jsonl``."""

    seq: int
    name: Action
    from_phase: Phase
    to_phase: Phase
    actor: Role
    at: datetime
    note: str | None = None
    reason: str | None = None
    bug: BugKind | None = None
    gate: str | None = None


@dataclass(frozen=True)
class Accepted:
    """A transition was accepted: the next record and the event recording it."""

    record: RunRecord
    event: Event


@dataclass(frozen=True)
class Refused:
    """A transition was refused; nothing changed."""

    reason: str


type Outcome = Accepted | Refused
