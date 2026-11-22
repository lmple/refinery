"""Shared test helpers, including an oracle transcribed from the spec deltas.

The oracle (ROLE_OF, EDGES, FROZEN_IN) is written from
openspec/changes/run-lifecycle/specs/run-lifecycle/spec.md, independently of
refinery.run.transitions.RULES, so tests compare the code to the spec rather
than to itself.
"""

import asyncio
from collections.abc import Coroutine, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from refinery.run import transitions
from refinery.run.model import (
    Accepted,
    Action,
    Artifact,
    BugKind,
    Event,
    Outcome,
    Phase,
    Role,
    RunRecord,
    TraceResult,
    TransitionRequest,
    Verdict,
)

A = Action
P = Phase

# Requirement "Transitions are restricted by role".
ROLE_OF: dict[Action, Role] = {
    **dict.fromkeys(
        (
            A.START_RUN,
            A.APPROVE_SPEC,
            A.REJECT_SPEC,
            A.APPROVE_TESTS,
            A.REJECT_TESTS,
            A.APPROVE_FINAL,
            A.REJECT_FINAL,
            A.RESOLVE_TO_SPEC,
            A.RESOLVE_TO_TESTS,
            A.RESOLVE_TO_IMPL,
            A.RETRY_ARCHIVE,
            A.CANCEL,
        ),
        Role.HUMAN,
    ),
    **dict.fromkeys(
        (
            A.COMPLETE_SPEC,
            A.COMPLETE_MODEL,
            A.SKIP_MODEL,
            A.COMPLETE_TESTS,
            A.COMPLETE_IMPL,
            A.ESCALATE,
        ),
        Role.AGENT,
    ),
    **dict.fromkeys((A.PASS_GATE, A.FAIL_GATE, A.ARCHIVE), Role.FACTORYD),
}

NON_TERMINAL: frozenset[Phase] = frozenset(Phase) - {P.NONE, P.ARCHIVED, P.CANCELLED}

# Allowed (sources, target) per action, from the transition requirements.
EDGES: dict[Action, tuple[frozenset[Phase], Phase]] = {
    A.START_RUN: (frozenset({P.NONE}), P.SPEC),
    A.COMPLETE_SPEC: (frozenset({P.SPEC}), P.MODEL),
    A.COMPLETE_MODEL: (frozenset({P.MODEL}), P.SPEC_REVIEW),
    A.SKIP_MODEL: (frozenset({P.MODEL}), P.SPEC_REVIEW),
    A.APPROVE_SPEC: (frozenset({P.SPEC_REVIEW}), P.TESTS),
    A.REJECT_SPEC: (frozenset({P.SPEC_REVIEW}), P.SPEC),
    A.COMPLETE_TESTS: (frozenset({P.TESTS}), P.TESTS_REVIEW),
    A.APPROVE_TESTS: (frozenset({P.TESTS_REVIEW}), P.IMPL),
    A.REJECT_TESTS: (frozenset({P.TESTS_REVIEW}), P.TESTS),
    A.COMPLETE_IMPL: (frozenset({P.IMPL}), P.VERIFY),
    A.PASS_GATE: (frozenset({P.VERIFY}), P.FINAL_REVIEW),
    A.FAIL_GATE: (frozenset({P.VERIFY}), P.IMPL),
    A.ESCALATE: (frozenset({P.TESTS, P.IMPL}), P.ESCALATED),
    A.RESOLVE_TO_SPEC: (frozenset({P.ESCALATED}), P.SPEC),
    A.RESOLVE_TO_TESTS: (frozenset({P.ESCALATED}), P.TESTS),
    A.RESOLVE_TO_IMPL: (frozenset({P.ESCALATED}), P.IMPL),
    A.APPROVE_FINAL: (frozenset({P.FINAL_REVIEW}), P.ARCHIVE),
    A.REJECT_FINAL: (frozenset({P.FINAL_REVIEW}), P.IMPL),
    A.ARCHIVE: (frozenset({P.ARCHIVE}), P.ARCHIVED),
    A.RETRY_ARCHIVE: (frozenset({P.ARCHIVE}), P.ARCHIVE),
    A.CANCEL: (NON_TERMINAL, P.CANCELLED),
}

BOTH: frozenset[Artifact] = frozenset({Artifact.SPEC, Artifact.TESTS})
ONLY_SPEC: frozenset[Artifact] = frozenset({Artifact.SPEC})
NOTHING: frozenset[Artifact] = frozenset()

# Requirement "Approved artifacts are frozen" (FrozenMatchesPhase in Run.tla).
FROZEN_IN: dict[Phase, frozenset[frozenset[Artifact]]] = {
    **{p: frozenset({NOTHING}) for p in (P.SPEC, P.MODEL, P.SPEC_REVIEW)},
    **{p: frozenset({ONLY_SPEC}) for p in (P.TESTS, P.TESTS_REVIEW)},
    **{
        p: frozenset({BOTH})
        for p in (P.IMPL, P.VERIFY, P.FINAL_REVIEW, P.ARCHIVE, P.ARCHIVED)
    },
    P.ESCALATED: frozenset({ONLY_SPEC, BOTH}),
    P.CANCELLED: frozenset({NOTHING, ONLY_SPEC, BOTH}),
}

HAPPY_PATH: tuple[Action, ...] = (
    A.COMPLETE_SPEC,
    A.COMPLETE_MODEL,
    A.APPROVE_SPEC,
    A.COMPLETE_TESTS,
    A.APPROVE_TESTS,
    A.COMPLETE_IMPL,
    A.PASS_GATE,
    A.APPROVE_FINAL,
)

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
FAKE_SHA = "0" * 64


def at(minutes: int) -> datetime:
    """A fixed, timezone-aware UTC timestamp ``minutes`` after T0."""
    return T0 + timedelta(minutes=minutes)


def request(
    name: Action,
    actor: Role | None = None,
    minute: int = 0,
    **payload: Any,
) -> TransitionRequest:
    """A request for ``name``, by its own role unless ``actor`` is given.

    Without explicit payload, actions that need one get a valid one, so the
    default request is always acceptable from the right phase.
    """
    defaults: dict[str, Any] = {
        A.REJECT_SPEC: {"note": "scenario 3 has no THEN"},
        A.REJECT_TESTS: {"note": "test names do not match scenarios"},
        A.REJECT_FINAL: {"note": "diff touches an unrelated file"},
        A.SKIP_MODEL: {"reason": "docs only, no behaviour change"},
        A.ESCALATE: {
            "bug": BugKind.TEST_BUG,
            "reason": "test 2 contradicts scenario 4",
        },
    }.get(name, {})
    return TransitionRequest(
        name=name,
        actor=actor if actor is not None else ROLE_OF[name],
        at=at(minute),
        **(defaults | payload),
    )


def accepted(outcome: Outcome) -> Accepted:
    """Narrow ``outcome`` to ``Accepted``, failing the test otherwise."""
    assert isinstance(outcome, Accepted), f"expected Accepted, got {outcome!r}"
    return outcome


def started(run_id: str = "c.1", change: str = "c") -> Accepted:
    """A freshly started run, via the pure StartRun function."""
    return accepted(transitions.start_run(run_id, change, request(A.START_RUN), None))


def drive(
    record: RunRecord, actions: Iterable[Action]
) -> tuple[RunRecord, list[Event]]:
    """Apply default requests for ``actions`` in order; all must be accepted."""
    events: list[Event] = []
    for minute, name in enumerate(actions, start=1):
        step = accepted(transitions.apply(record, request(name, minute=minute)))
        record = step.record
        events.append(step.event)
    return record, events


def passing_trace(events: int) -> TraceResult:
    """A passing trace result as Archive would store it."""
    return TraceResult(verdict=Verdict.PASS, events=events, sha256=FAKE_SHA)


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


def names(events: Sequence[Event]) -> list[Action]:
    """The transition names of ``events``."""
    return [e.name for e in events]
