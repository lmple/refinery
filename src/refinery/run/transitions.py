"""Pure transition functions, one per ``spec/Run.tla`` action.

Each function takes the current record and a request and returns ``Accepted``
(the next record and its event) or ``Refused``; none of them does I/O.
"""

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from refinery.run.model import (
    TERMINAL,
    Accepted,
    Action,
    Artifact,
    Event,
    Outcome,
    Phase,
    Refused,
    Role,
    RunRecord,
    TraceResult,
    TransitionRequest,
    Verdict,
)

A = Action
P = Phase


@dataclass(frozen=True)
class TransitionRule:
    """Where a transition may start, where it ends, and who may request it."""

    name: Action
    sources: frozenset[Phase]
    target: Phase
    role: Role
    freeze: frozenset[Artifact] | None  # None keeps the current frozen set


def _rule(
    name: Action,
    sources: set[Phase] | frozenset[Phase],
    target: Phase,
    role: Role,
    freeze: set[Artifact] | None = None,
) -> TransitionRule:
    return TransitionRule(
        name,
        frozenset(sources),
        target,
        role,
        None if freeze is None else frozenset(freeze),
    )


_UNFINISHED = frozenset(Phase) - TERMINAL - {P.NONE}
_BOTH = {Artifact.SPEC, Artifact.TESTS}

# The transition table of specs/run-lifecycle/spec.md; StartRun creates a run
# rather than moving one, so it is handled by start_run.
RULES: Mapping[Action, TransitionRule] = {
    r.name: r
    for r in (
        _rule(A.COMPLETE_SPEC, {P.SPEC}, P.MODEL, Role.AGENT),
        _rule(A.COMPLETE_MODEL, {P.MODEL}, P.SPEC_REVIEW, Role.AGENT),
        _rule(A.SKIP_MODEL, {P.MODEL}, P.SPEC_REVIEW, Role.AGENT),
        _rule(A.APPROVE_SPEC, {P.SPEC_REVIEW}, P.TESTS, Role.HUMAN, {Artifact.SPEC}),
        _rule(A.REJECT_SPEC, {P.SPEC_REVIEW}, P.SPEC, Role.HUMAN),
        _rule(A.COMPLETE_TESTS, {P.TESTS}, P.TESTS_REVIEW, Role.AGENT),
        _rule(A.APPROVE_TESTS, {P.TESTS_REVIEW}, P.IMPL, Role.HUMAN, _BOTH),
        _rule(A.REJECT_TESTS, {P.TESTS_REVIEW}, P.TESTS, Role.HUMAN),
        _rule(A.COMPLETE_IMPL, {P.IMPL}, P.VERIFY, Role.AGENT),
        _rule(A.PASS_GATE, {P.VERIFY}, P.FINAL_REVIEW, Role.FACTORYD),
        _rule(A.FAIL_GATE, {P.VERIFY}, P.IMPL, Role.FACTORYD),
        _rule(A.ESCALATE, {P.TESTS, P.IMPL}, P.ESCALATED, Role.AGENT),
        _rule(A.RESOLVE_TO_SPEC, {P.ESCALATED}, P.SPEC, Role.HUMAN, set()),
        _rule(A.RESOLVE_TO_TESTS, {P.ESCALATED}, P.TESTS, Role.HUMAN, {Artifact.SPEC}),
        _rule(A.RESOLVE_TO_IMPL, {P.ESCALATED}, P.IMPL, Role.HUMAN),
        _rule(A.APPROVE_FINAL, {P.FINAL_REVIEW}, P.ARCHIVE, Role.HUMAN),
        _rule(A.REJECT_FINAL, {P.FINAL_REVIEW}, P.IMPL, Role.HUMAN),
        _rule(A.ARCHIVE, {P.ARCHIVE}, P.ARCHIVED, Role.FACTORYD),
        _rule(A.RETRY_ARCHIVE, {P.ARCHIVE}, P.ARCHIVE, Role.HUMAN),
        _rule(A.CANCEL, _UNFINISHED, P.CANCELLED, Role.HUMAN),
    )
}


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def _event(seq: int, request: TransitionRequest, src: Phase, dst: Phase) -> Event:
    return Event(
        seq=seq,
        name=request.name,
        from_phase=src,
        to_phase=dst,
        actor=request.actor,
        at=request.at,
        note=request.note,
        reason=request.reason,
        bug=request.bug,
        gate=request.gate,
    )


def _step(
    record: RunRecord, request: TransitionRequest, trace: TraceResult | None = None
) -> Outcome:
    """The checks every transition shares, then the move itself."""
    rule = RULES[request.name]
    if record.phase not in rule.sources:
        return Refused(f"{request.name} is not allowed in phase {record.phase}")
    if request.actor is not rule.role:
        return Refused(
            f"{request.name} must be requested by {rule.role}, not {request.actor}"
        )
    event = _event(record.seq + 1, request, record.phase, rule.target)
    moved = dataclasses.replace(
        record,
        phase=rule.target,
        frozen=record.frozen if rule.freeze is None else rule.freeze,
        seq=record.seq + 1,
        trace=record.trace if trace is None else trace,
        updated_at=request.at,
    )
    return Accepted(moved, event)


def _with_note(record: RunRecord, request: TransitionRequest) -> Outcome:
    if _blank(request.note):
        return Refused(f"{request.name} needs a note saying what to change")
    return _step(record, request)


def start_run(
    run_id: str, change: str, request: TransitionRequest, claimed_by: str | None
) -> Outcome:
    """StartRun on ``change``, refused if ``claimed_by`` names an unfinished run."""
    if request.name is not A.START_RUN:
        return Refused(f"expected StartRun, got {request.name}")
    if request.actor is not Role.HUMAN:
        return Refused(f"StartRun must be requested by human, not {request.actor}")
    if claimed_by is not None:
        return Refused(f"change {change} already has an unfinished run: {claimed_by}")
    record = RunRecord(
        run_id=run_id,
        change=change,
        phase=P.SPEC,
        frozen=frozenset(),
        seq=1,
        trace=TraceResult(),
        started_at=request.at,
        updated_at=request.at,
    )
    return Accepted(record, _event(1, request, P.NONE, P.SPEC))


def complete_spec(record: RunRecord, request: TransitionRequest) -> Outcome:
    """CompleteSpec: spec → model."""
    return _step(record, request)


def complete_model(record: RunRecord, request: TransitionRequest) -> Outcome:
    """CompleteModel: model → spec_review."""
    return _step(record, request)


def skip_model(record: RunRecord, request: TransitionRequest) -> Outcome:
    """SkipModel: model → spec_review, with the reason no model is needed."""
    if _blank(request.reason):
        return Refused(
            "SkipModel needs a reason why the change does not affect behaviour"
        )
    return _step(record, request)


def approve_spec(record: RunRecord, request: TransitionRequest) -> Outcome:
    """ApproveSpec: spec_review → tests; freezes the spec."""
    return _step(record, request)


def reject_spec(record: RunRecord, request: TransitionRequest) -> Outcome:
    """RejectSpec: spec_review → spec, with a note."""
    return _with_note(record, request)


def complete_tests(record: RunRecord, request: TransitionRequest) -> Outcome:
    """CompleteTests: tests → tests_review."""
    return _step(record, request)


def approve_tests(record: RunRecord, request: TransitionRequest) -> Outcome:
    """ApproveTests: tests_review → impl; freezes the tests too."""
    return _step(record, request)


def reject_tests(record: RunRecord, request: TransitionRequest) -> Outcome:
    """RejectTests: tests_review → tests, with a note."""
    return _with_note(record, request)


def complete_impl(record: RunRecord, request: TransitionRequest) -> Outcome:
    """CompleteImpl: impl → verify."""
    return _step(record, request)


def pass_gate(record: RunRecord, request: TransitionRequest) -> Outcome:
    """PassGate: verify → final_review."""
    return _step(record, request)


def fail_gate(record: RunRecord, request: TransitionRequest) -> Outcome:
    """FailGate: verify → impl."""
    return _step(record, request)


def escalate(record: RunRecord, request: TransitionRequest) -> Outcome:
    """Escalate: tests or impl → escalated, classified, with a reason."""
    if request.bug is None:
        return Refused("Escalate needs a classification: spec bug or test bug")
    if _blank(request.reason):
        return Refused("Escalate needs a reason")
    return _step(record, request)


def resolve_to_spec(record: RunRecord, request: TransitionRequest) -> Outcome:
    """ResolveToSpec: escalated → spec; unfreezes everything."""
    return _step(record, request)


def resolve_to_tests(record: RunRecord, request: TransitionRequest) -> Outcome:
    """ResolveToTests: escalated → tests; only the spec stays frozen."""
    return _step(record, request)


def resolve_to_impl(record: RunRecord, request: TransitionRequest) -> Outcome:
    """ResolveToImpl: escalated → impl, only once the tests are frozen."""
    if record.phase is P.ESCALATED and Artifact.TESTS not in record.frozen:
        return Refused("ResolveToImpl needs approved (frozen) tests")
    return _step(record, request)


def approve_final(record: RunRecord, request: TransitionRequest) -> Outcome:
    """ApproveFinal: final_review → archive."""
    return _step(record, request)


def reject_final(record: RunRecord, request: TransitionRequest) -> Outcome:
    """RejectFinal: final_review → impl, with a note."""
    return _with_note(record, request)


def archive(
    record: RunRecord, request: TransitionRequest, trace: TraceResult
) -> Outcome:
    """Archive: only with a passing trace verdict, which the record then stores."""
    if request.name is not A.ARCHIVE:
        return Refused(f"expected Archive, got {request.name}")
    if record.phase is P.ARCHIVE and record.trace.verdict is not Verdict.NONE:
        return Refused("Archive after a failed validation needs RetryArchive first")
    if trace.verdict is not Verdict.PASS or trace.sha256 is None:
        return Refused("Archive needs a passing trace validation")
    if trace.events != record.seq + 1:
        return Refused(f"trace covers {trace.events} events, expected {record.seq + 1}")
    return _step(record, request, trace)


def abort_archive(record: RunRecord, at: datetime) -> RunRecord:
    """AbortArchive (not an event): record a failed trace validation."""
    if record.phase is not P.ARCHIVE:
        raise ValueError(f"AbortArchive needs phase archive, not {record.phase}")
    return dataclasses.replace(record, trace=TraceResult(Verdict.FAIL), updated_at=at)


def retry_archive(record: RunRecord, request: TransitionRequest) -> Outcome:
    """RetryArchive: reset a failed verdict so Archive may run again."""
    if record.phase is P.ARCHIVE and record.trace.verdict is not Verdict.FAIL:
        return Refused("RetryArchive needs a failed trace validation")
    return _step(record, request, TraceResult())


def cancel(record: RunRecord, request: TransitionRequest) -> Outcome:
    """Cancel: any unfinished phase → cancelled."""
    return _step(record, request)


_DISPATCH: Mapping[Action, Callable[[RunRecord, TransitionRequest], Outcome]] = {
    A.COMPLETE_SPEC: complete_spec,
    A.COMPLETE_MODEL: complete_model,
    A.SKIP_MODEL: skip_model,
    A.APPROVE_SPEC: approve_spec,
    A.REJECT_SPEC: reject_spec,
    A.COMPLETE_TESTS: complete_tests,
    A.APPROVE_TESTS: approve_tests,
    A.REJECT_TESTS: reject_tests,
    A.COMPLETE_IMPL: complete_impl,
    A.PASS_GATE: pass_gate,
    A.FAIL_GATE: fail_gate,
    A.ESCALATE: escalate,
    A.RESOLVE_TO_SPEC: resolve_to_spec,
    A.RESOLVE_TO_TESTS: resolve_to_tests,
    A.RESOLVE_TO_IMPL: resolve_to_impl,
    A.APPROVE_FINAL: approve_final,
    A.REJECT_FINAL: reject_final,
    A.RETRY_ARCHIVE: retry_archive,
    A.CANCEL: cancel,
}


def apply(record: RunRecord, request: TransitionRequest) -> Outcome:
    """Dispatch to the function for ``request.name``; Archive is refused here."""
    if request.name is A.START_RUN:
        return Refused("StartRun creates a run; use start_run")
    if request.name is A.ARCHIVE:
        return Refused("Archive needs a trace verdict; use archive")
    return _DISPATCH[request.name](record, request)
