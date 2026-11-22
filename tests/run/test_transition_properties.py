"""Property tests mirroring the invariants of spec/Run.tla on the pure layer.

A random walk of transition requests, biased towards enabled ones, is checked
step by step against the spec oracle in tests/run/helpers.py: the code must
accept exactly what the spec allows and keep every invariant.
(OneActiveRunPerChange spans runs and is checked in test_store_crash.py.)
"""

import dataclasses
import re
from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from refinery.run import transitions
from refinery.run.archive import ReplayValidator
from refinery.run.model import (
    TERMINAL,
    Accepted,
    Action,
    Artifact,
    BugKind,
    Event,
    Outcome,
    Phase,
    Refused,
    Role,
    RunRecord,
    TransitionRequest,
    Verdict,
)
from tests.run.helpers import (
    EDGES,
    FROZEN_IN,
    ROLE_OF,
    at,
    passing_trace,
    run_async,
    started,
)

A = Action
P = Phase

STEP_ACTIONS = [a for a in Action if a is not A.START_RUN]
NOTES = st.sampled_from([None, "", "  ", "looks wrong"])
VALID_COMMIT = re.compile(r"[0-9a-f]{40}")
# Mostly valid reviewed commits; sometimes missing or malformed.
COMMITS = st.one_of(
    st.sampled_from(["c" * 40, "0123456789abcdef0123456789abcdef01234567"]),
    st.sampled_from([None, "", "abc123", "C" * 40, "c" * 39, "g" * 40]),
)
UNFREEZERS = {A.RESOLVE_TO_SPEC, A.RESOLVE_TO_TESTS}


@dataclass(frozen=True)
class Step:
    """One drawn step: a request, or a simulated failed trace validation."""

    request: TransitionRequest | None  # None: AbortArchive (validation failed)


def spec_allows(record: RunRecord, req: TransitionRequest) -> bool:
    """The oracle: would the spec accept ``req`` on ``record``?"""
    sources, _ = EDGES[req.name]
    if record.phase not in sources or req.actor is not ROLE_OF[req.name]:
        return False
    filled = {
        "note": bool(req.note and req.note.strip()),
        "reason": bool(req.reason and req.reason.strip()),
    }
    match req.name:
        case A.REJECT_SPEC | A.REJECT_TESTS | A.REJECT_FINAL:
            return filled["note"]
        case A.SKIP_MODEL:
            return filled["reason"]
        case A.ESCALATE:
            return req.bug is not None and filled["reason"]
        case A.APPROVE_SPEC | A.APPROVE_TESTS:
            return req.commit is not None and bool(VALID_COMMIT.fullmatch(req.commit))
        case A.RESOLVE_TO_IMPL:
            return Artifact.TESTS in record.frozen
        case A.RETRY_ARCHIVE:
            return record.trace.verdict is Verdict.FAIL
        case A.ARCHIVE:
            return record.trace.verdict is Verdict.NONE
        case _:
            return True


@st.composite
def step_for(draw: st.DrawFn, record: RunRecord) -> Step:
    """Mostly an enabled action with the right role; sometimes anything."""
    if (
        record.phase is P.ARCHIVE
        and record.trace.verdict is Verdict.NONE
        and draw(st.integers(0, 3)) == 0
    ):
        return Step(None)
    enabled = [a for a in STEP_ACTIONS if record.phase in EDGES[a][0]]
    biased = draw(st.integers(0, 4)) > 0 and bool(enabled)
    name = draw(st.sampled_from(enabled if biased else STEP_ACTIONS))
    actor = (
        ROLE_OF[name] if draw(st.integers(0, 5)) > 0 else draw(st.sampled_from(Role))
    )
    return Step(
        TransitionRequest(
            name=name,
            actor=actor,
            at=at(record.seq),
            note=draw(NOTES) if draw(st.booleans()) else "fine by me",
            reason=draw(NOTES) if draw(st.booleans()) else "because",
            bug=draw(st.sampled_from([None, BugKind.SPEC_BUG, BugKind.TEST_BUG])),
            commit=draw(COMMITS),
        )
    )


def perform(record: RunRecord, req: TransitionRequest) -> Outcome:
    """Archive needs a verdict; everything else goes through ``apply``."""
    if req.name is A.ARCHIVE:
        return transitions.archive(record, req, passing_trace(record.seq + 1))
    return transitions.apply(record, req)


def check_invariants(record: RunRecord, events: list[Event]) -> None:
    """FrozenMatchesPhase, ArchivedOnlyWithPassingTrace, ApprovalsBeforeArchive."""
    assert record.frozen in FROZEN_IN[record.phase], (record.phase, record.frozen)
    if record.phase is P.ARCHIVED:
        assert record.trace.verdict is Verdict.PASS
        seen = [e.name for e in events]
        gates = [A.APPROVE_SPEC, A.APPROVE_TESTS, A.APPROVE_FINAL]
        positions = [max(i for i, n in enumerate(seen) if n is g) for g in gates]
        assert positions == sorted(positions), seen
    if record.trace.verdict is Verdict.FAIL:
        assert record.phase in {P.ARCHIVE, P.CANCELLED}
    # Approvals record the reviewed commit: one exactly per frozen artifact.
    assert (record.spec_commit is not None) == (Artifact.SPEC in record.frozen)
    assert (record.tests_commit is not None) == (Artifact.TESTS in record.frozen)


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_random_walk_refines_the_spec(data: st.DataObject) -> None:
    first = started()
    record, events = first.record, [first.event]
    for _ in range(data.draw(st.integers(0, 60), label="length")):
        step = data.draw(step_for(record), label="step")
        before = record
        if step.request is None:
            record = transitions.abort_archive(record, at(record.seq))
            assert (record.phase, record.seq) == (before.phase, before.seq)
            check_invariants(record, events)
            continue
        req = step.request
        outcome = perform(record, req)
        allowed = spec_allows(record, req)
        # TerminalIsFinal is part of the oracle: terminal phases are no source.
        assert not (before.phase in TERMINAL and isinstance(outcome, Accepted))
        if isinstance(outcome, Refused):
            assert not allowed, f"spec allows {req.name} from {before.phase}"
            continue
        assert allowed, f"spec refuses {req.name} by {req.actor} from {before.phase}"
        record, event = outcome.record, outcome.event
        # The event describes exactly the step taken (CommittedTraceValid).
        assert (event.seq, event.name, event.actor) == (
            before.seq + 1,
            req.name,
            req.actor,
        )
        assert (event.from_phase, event.to_phase) == (before.phase, record.phase)
        assert record.phase is EDGES[req.name][1]
        assert record.seq == before.seq + 1
        # FrozenReleasedOnlyByResolve.
        if not before.frozen <= record.frozen:
            assert req.name in UNFREEZERS
        events.append(event)
        check_invariants(record, events)
    verdict = run_async(ReplayValidator().validate(record.run_id, events))
    assert verdict.passed, verdict.reason


@settings(max_examples=100, deadline=None)
@given(st.data())
def test_replay_validator_rejects_a_corrupted_log(data: st.DataObject) -> None:
    first = started()
    record, events = first.record, [first.event]
    for _ in range(data.draw(st.integers(1, 30), label="length")):
        step = data.draw(step_for(record), label="step")
        if step.request is None:
            continue
        outcome = perform(record, step.request)
        if isinstance(outcome, Accepted):
            record = outcome.record
            events.append(outcome.event)
    victim = data.draw(st.integers(0, len(events) - 1), label="victim")
    original = events[victim]
    # Corrupt either end of one event: the replay must check both.
    field = data.draw(st.sampled_from(["from_phase", "to_phase"]), label="field")
    current: Phase = getattr(original, field)
    wrong = data.draw(
        st.sampled_from([p for p in Phase if p is not current]), label="wrong"
    )
    corrupted = [
        *events[:victim],
        dataclasses.replace(original, **{field: wrong}),
        *events[victim + 1 :],
    ]
    verdict = run_async(ReplayValidator().validate(record.run_id, corrupted))
    assert not verdict.passed
