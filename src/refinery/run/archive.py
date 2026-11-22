"""Trace validation behind an interface (design.md, D7).

The Archive flow itself lives in ``RunStore.archive``, which needs the event
log; this module holds what it validates against.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from refinery.run.model import Action, Artifact, Event, Phase, Role
from refinery.run.transitions import RULES


@dataclass(frozen=True)
class ValidationResult:
    """Whether a run's event log is a behaviour of ``spec/Run.tla``."""

    passed: bool
    reason: str = ""


class TraceValidator(Protocol):
    """Validates a complete event log, the pending Archive event included."""

    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        """Return the verdict for ``events``."""
        ...


class ReplayValidator:
    """Replays events through the transition rules; for tests (not independent).

    It checks the code against the code's own table, so it cannot catch a
    wrong table; the TLC-backed validator (gate-runner) checks against
    ``spec/Run.tla`` itself.
    """

    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        """Check the events chain from ``none`` along allowed transitions."""
        return _replay(events)


def _replay(events: Sequence[Event]) -> ValidationResult:
    if not events:
        return ValidationResult(False, "empty log")
    phase = Phase.NONE
    frozen: frozenset[Artifact] = frozenset()
    for expected_seq, event in enumerate(events, start=1):
        problem = _check(event, expected_seq, phase, frozen)
        if problem:
            return ValidationResult(False, f"event {expected_seq}: {problem}")
        phase = event.to_phase
        if event.name is not Action.START_RUN:
            freeze = RULES[event.name].freeze
            frozen = frozen if freeze is None else freeze
    return ValidationResult(True)


def _check(
    event: Event, seq: int, phase: Phase, frozen: frozenset[Artifact]
) -> str | None:
    """Why ``event`` cannot follow a run in ``phase``, or None if it can."""
    if event.seq != seq:
        return f"seq {event.seq}, expected {seq}"
    if event.from_phase is not phase:
        return f"starts from {event.from_phase}, but the run is in {phase}"
    if event.name is Action.START_RUN:
        ok = (phase, event.to_phase, event.actor) == (
            Phase.NONE,
            Phase.SPEC,
            Role.HUMAN,
        )
        return None if ok else "StartRun must be human, none → spec, and first"
    rule = RULES[event.name]
    if phase not in rule.sources or event.to_phase is not rule.target:
        return f"{event.name} {event.from_phase} → {event.to_phase} is not allowed"
    if event.actor is not rule.role:
        return f"{event.name} by {event.actor}, expected {rule.role}"
    if event.name is Action.RESOLVE_TO_IMPL and Artifact.TESTS not in frozen:
        return "ResolveToImpl before the tests were approved"
    return None
