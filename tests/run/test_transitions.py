"""Scenarios of specs/run-lifecycle/spec.md against the pure transition layer."""

import re

import pytest

from refinery.run import transitions
from refinery.run.model import (
    TERMINAL,
    Action,
    Artifact,
    BugKind,
    Phase,
    Refused,
    Role,
    RunRecord,
    TraceResult,
    Verdict,
)
from tests.run.helpers import (
    BOTH,
    EDGES,
    HAPPY_PATH,
    NOTHING,
    ONLY_SPEC,
    ROLE_OF,
    accepted,
    at,
    drive,
    passing_trace,
    request,
    started,
)

A = Action
P = Phase

TO_ESCALATED_FROM_IMPL = (*HAPPY_PATH[:5], A.ESCALATE)  # ... ApproveTests, Escalate
TO_ESCALATED_FROM_TESTS = (*HAPPY_PATH[:3], A.ESCALATE)  # ... ApproveSpec, Escalate


def in_phase(target: Phase) -> RunRecord:
    """A run driven along the happy path until it is in ``target``."""
    record = started().record
    path = iter(HAPPY_PATH)
    while record.phase is not target:
        record, _ = drive(record, [next(path)])
    return record


def archived() -> RunRecord:
    """A run that went through the whole happy path and was archived."""
    record = in_phase(P.ARCHIVE)
    trace = passing_trace(record.seq + 1)
    return accepted(transitions.archive(record, request(A.ARCHIVE), trace)).record


# Requirement: Run phases


def test_phases_are_exactly_the_specified_ones() -> None:
    assert {p.value for p in Phase} - {"none"} == {
        "spec", "model", "spec_review", "tests", "tests_review", "impl",
        "verify", "final_review", "archive", "escalated", "archived", "cancelled",
    }  # fmt: skip
    assert frozenset({P.ARCHIVED, P.CANCELLED}) == TERMINAL


def test_new_run_starts_in_spec_phase() -> None:
    step = started("run-lifecycle.1", "run-lifecycle")
    assert step.record.run_id == "run-lifecycle.1"
    assert step.record.change == "run-lifecycle"
    assert step.record.phase is P.SPEC
    assert step.record.frozen == NOTHING
    assert step.record.seq == 1
    assert step.record.trace == TraceResult()
    assert step.record.started_at == step.record.updated_at == at(0)
    assert step.event.seq == 1
    assert step.event.name is A.START_RUN
    assert (step.event.from_phase, step.event.to_phase) == (P.NONE, P.SPEC)
    assert step.event.actor is Role.HUMAN


# Requirement: One unfinished run per change (pure part; the store tests cover
# concurrency and restarting after cancellation)


def test_start_refused_when_change_has_unfinished_run() -> None:
    outcome = transitions.start_run("c.2", "c", request(A.START_RUN), "c.1")
    assert isinstance(outcome, Refused)
    assert "c.1" in outcome.reason


# Requirements: Work, review gate and gate result transitions


def test_model_completed_goes_to_spec_review() -> None:
    record, _ = drive(in_phase(P.MODEL), [A.COMPLETE_MODEL])
    assert record.phase is P.SPEC_REVIEW


def test_tests_rejected_goes_back_to_tests() -> None:
    record, _ = drive(in_phase(P.TESTS_REVIEW), [A.REJECT_TESTS])
    assert record.phase is P.TESTS


def test_gate_failure_goes_back_to_impl() -> None:
    record, _ = drive(in_phase(P.VERIFY), [A.FAIL_GATE])
    assert record.phase is P.IMPL


@pytest.mark.parametrize("name", [a for a in Action if a is not A.START_RUN])
def test_each_transition_reaches_its_specified_target(name: Action) -> None:
    sources, target = EDGES[name]
    record = started().record
    for source in sorted(sources):
        if source is P.NONE:
            continue
        here = _reach(record, source)
        if here is None:
            continue
        if name is A.ARCHIVE:
            outcome = transitions.archive(
                here, request(name), passing_trace(here.seq + 1)
            )
        elif name is A.RETRY_ARCHIVE:
            outcome = transitions.apply(
                transitions.abort_archive(here, at(99)), request(name)
            )
        else:
            outcome = transitions.apply(here, request(name))
        step = accepted(outcome)
        assert step.record.phase is target
        assert (step.event.from_phase, step.event.to_phase) == (source, target)


def _reach(record: RunRecord, phase: Phase) -> RunRecord | None:
    """Some way of reaching ``phase``; ResolveToImpl needs frozen tests."""
    if phase is P.ESCALATED:
        reached, _ = drive(record, TO_ESCALATED_FROM_IMPL)
        return reached
    return in_phase(phase)


# Requirement: Other transitions are refused


def test_happy_path_ends_archived() -> None:
    record = archived()
    assert record.phase is P.ARCHIVED
    assert record.seq == 10


def test_out_of_order_transition_is_refused() -> None:
    record = in_phase(P.SPEC)
    outcome = transitions.apply(record, request(A.COMPLETE_IMPL))
    assert isinstance(outcome, Refused)
    assert "spec" in outcome.reason


def test_apply_refuses_archive_without_a_verdict() -> None:
    outcome = transitions.apply(in_phase(P.ARCHIVE), request(A.ARCHIVE))
    assert isinstance(outcome, Refused)


@pytest.mark.parametrize("verdict", [Verdict.NONE, Verdict.FAIL])
def test_archive_refused_without_passing_trace(verdict: Verdict) -> None:
    record = in_phase(P.ARCHIVE)
    outcome = transitions.archive(
        record, request(A.ARCHIVE), TraceResult(verdict=verdict)
    )
    assert isinstance(outcome, Refused)


def test_archive_stores_the_verdict() -> None:
    record = archived()
    assert record.trace == passing_trace(10)


# Requirement: Transitions are restricted by role


def test_agent_cannot_approve() -> None:
    record = in_phase(P.SPEC_REVIEW)
    outcome = transitions.apply(record, request(A.APPROVE_SPEC, actor=Role.AGENT))
    assert isinstance(outcome, Refused)


def test_agent_cannot_report_its_own_gate_result() -> None:
    record = in_phase(P.VERIFY)
    outcome = transitions.apply(record, request(A.PASS_GATE, actor=Role.AGENT))
    assert isinstance(outcome, Refused)


def test_role_table_covers_every_action() -> None:
    assert set(ROLE_OF) == set(Action)


@pytest.mark.parametrize(
    ("name", "actor"),
    [(a, r) for a in Action for r in Role if r is not ROLE_OF[a] and a not in
     (A.START_RUN, A.ARCHIVE, A.RETRY_ARCHIVE, A.RESOLVE_TO_SPEC,
      A.RESOLVE_TO_TESTS, A.RESOLVE_TO_IMPL)],
)  # fmt: skip
def test_every_wrong_role_is_refused(name: Action, actor: Role) -> None:
    sources, _ = EDGES[name]
    phase = P.SPEC if P.SPEC in sources else min(sources - {P.NONE})
    outcome = transitions.apply(in_phase(phase), request(name, actor=actor))
    assert isinstance(outcome, Refused)


@pytest.mark.parametrize(
    "name", [A.RESOLVE_TO_SPEC, A.RESOLVE_TO_TESTS, A.RESOLVE_TO_IMPL]
)
@pytest.mark.parametrize("actor", [Role.AGENT, Role.FACTORYD])
def test_only_humans_resolve(name: Action, actor: Role) -> None:
    record, _ = drive(started().record, TO_ESCALATED_FROM_IMPL)
    assert isinstance(transitions.apply(record, request(name, actor=actor)), Refused)


@pytest.mark.parametrize("actor", [Role.AGENT, Role.FACTORYD])
def test_only_humans_start_runs(actor: Role) -> None:
    outcome = transitions.start_run("c.1", "c", request(A.START_RUN, actor=actor), None)
    assert isinstance(outcome, Refused)


@pytest.mark.parametrize("actor", [Role.HUMAN, Role.AGENT])
def test_only_factoryd_archives(actor: Role) -> None:
    record = in_phase(P.ARCHIVE)
    trace = passing_trace(record.seq + 1)
    outcome = transitions.archive(record, request(A.ARCHIVE, actor=actor), trace)
    assert isinstance(outcome, Refused)


# Requirement: Review decisions carry a note


@pytest.mark.parametrize("note", [None, "", "   "])
def test_rejection_without_a_note_is_refused(note: str | None) -> None:
    record = in_phase(P.TESTS_REVIEW)
    outcome = transitions.apply(record, request(A.REJECT_TESTS, note=note))
    assert isinstance(outcome, Refused)
    assert record.phase is P.TESTS_REVIEW


def test_rejection_note_is_recorded() -> None:
    record = in_phase(P.SPEC_REVIEW)
    step = accepted(
        transitions.apply(record, request(A.REJECT_SPEC, note="scenario 3 has no THEN"))
    )
    assert step.event.note == "scenario 3 has no THEN"


def test_approval_may_carry_a_note() -> None:
    record = in_phase(P.SPEC_REVIEW)
    step = accepted(transitions.apply(record, request(A.APPROVE_SPEC, note="nice")))
    assert step.event.note == "nice"


# Requirement: Skipping the model needs a reason


@pytest.mark.parametrize("reason", [None, "", "  "])
def test_skip_model_without_reason_is_refused(reason: str | None) -> None:
    record = in_phase(P.MODEL)
    outcome = transitions.apply(record, request(A.SKIP_MODEL, reason=reason))
    assert isinstance(outcome, Refused)


def test_skip_model_reason_is_recorded() -> None:
    record = in_phase(P.MODEL)
    step = accepted(transitions.apply(record, request(A.SKIP_MODEL, reason="docs")))
    assert step.record.phase is P.SPEC_REVIEW
    assert step.event.reason == "docs"


# Requirement: Approved artifacts are frozen


@pytest.mark.parametrize("phase", [P.IMPL, P.VERIFY, P.FINAL_REVIEW, P.ARCHIVE])
def test_both_frozen_during_implementation(phase: Phase) -> None:
    assert in_phase(phase).frozen == BOTH


def test_both_frozen_once_archived() -> None:
    assert archived().frozen == BOTH


def test_spec_frozen_from_spec_approval() -> None:
    assert in_phase(P.TESTS).frozen == ONLY_SPEC
    assert in_phase(P.TESTS_REVIEW).frozen == ONLY_SPEC


def test_rejection_does_not_unfreeze() -> None:
    record, _ = drive(in_phase(P.FINAL_REVIEW), [A.REJECT_FINAL])
    assert record.phase is P.IMPL
    assert record.frozen == BOTH


# Requirement: Escalation to the human


def test_test_bug_sent_back_to_tests() -> None:
    record, _ = drive(started().record, TO_ESCALATED_FROM_IMPL)
    assert record.phase is P.ESCALATED
    record, _ = drive(record, [A.RESOLVE_TO_TESTS])
    assert record.phase is P.TESTS
    assert record.frozen == ONLY_SPEC


def test_spec_bug_sent_back_to_spec_unfreezes_everything() -> None:
    record, _ = drive(started().record, TO_ESCALATED_FROM_IMPL)
    record, _ = drive(record, [A.RESOLVE_TO_SPEC])
    assert record.phase is P.SPEC
    assert record.frozen == NOTHING


def test_resolve_to_impl_keeps_both_frozen() -> None:
    record, _ = drive(started().record, TO_ESCALATED_FROM_IMPL)
    record, _ = drive(record, [A.RESOLVE_TO_IMPL])
    assert record.phase is P.IMPL
    assert record.frozen == BOTH


def test_resolve_to_impl_before_tests_were_approved_is_refused() -> None:
    record, _ = drive(started().record, TO_ESCALATED_FROM_TESTS)
    assert record.frozen == ONLY_SPEC
    outcome = transitions.apply(record, request(A.RESOLVE_TO_IMPL))
    assert isinstance(outcome, Refused)


def test_escalation_without_classification_is_refused() -> None:
    record = in_phase(P.IMPL)
    outcome = transitions.apply(record, request(A.ESCALATE, bug=None))
    assert isinstance(outcome, Refused)


@pytest.mark.parametrize("reason", [None, "", " "])
def test_escalation_without_reason_is_refused(reason: str | None) -> None:
    record = in_phase(P.IMPL)
    outcome = transitions.apply(record, request(A.ESCALATE, reason=reason))
    assert isinstance(outcome, Refused)


def test_escalation_records_classification_and_reason() -> None:
    record = in_phase(P.IMPL)
    step = accepted(
        transitions.apply(
            record, request(A.ESCALATE, bug=BugKind.SPEC_BUG, reason="spec is wrong")
        )
    )
    assert (step.event.bug, step.event.reason) == (BugKind.SPEC_BUG, "spec is wrong")


# Requirement: Cancellation


def test_cancel_during_review() -> None:
    record, _ = drive(in_phase(P.SPEC_REVIEW), [A.CANCEL])
    assert record.phase is P.CANCELLED


@pytest.mark.parametrize("phase", [P.SPEC, P.TESTS_REVIEW, P.VERIFY, P.ARCHIVE])
def test_cancel_from_any_unfinished_phase(phase: Phase) -> None:
    record, _ = drive(in_phase(phase), [A.CANCEL])
    assert record.phase is P.CANCELLED


# Requirement: Terminal phases are final


@pytest.mark.parametrize("name", [a for a in Action if a is not A.START_RUN])
def test_no_transition_after_archive(name: Action) -> None:
    record = archived()
    if name is A.ARCHIVE:
        outcome = transitions.archive(
            record, request(name), passing_trace(record.seq + 1)
        )
    else:
        outcome = transitions.apply(record, request(name))
    assert isinstance(outcome, Refused)


@pytest.mark.parametrize("name", [a for a in Action if a is not A.START_RUN])
def test_no_transition_after_cancel(name: Action) -> None:
    record, _ = drive(in_phase(P.TESTS), [A.CANCEL])
    assert isinstance(transitions.apply(record, request(name)), Refused)


# Record bookkeeping (run-record spec: seq; design D2)


def test_each_transition_increments_seq_and_stamps_time() -> None:
    record, events = drive(started().record, HAPPY_PATH[:3])
    assert [e.seq for e in events] == [2, 3, 4]
    assert record.seq == 4
    assert record.updated_at == at(3)
    assert record.started_at == at(0)


def test_retry_archive_resets_a_failed_verdict() -> None:
    failed = transitions.abort_archive(in_phase(P.ARCHIVE), at(50))
    assert failed.trace.verdict is Verdict.FAIL
    assert failed.phase is P.ARCHIVE
    step = accepted(transitions.apply(failed, request(A.RETRY_ARCHIVE)))
    assert step.record.trace == TraceResult()
    assert step.record.phase is P.ARCHIVE


def test_retry_archive_needs_a_failed_verdict() -> None:
    outcome = transitions.apply(in_phase(P.ARCHIVE), request(A.RETRY_ARCHIVE))
    assert isinstance(outcome, Refused)


def test_abort_archive_keeps_seq() -> None:
    record = in_phase(P.ARCHIVE)
    assert transitions.abort_archive(record, at(50)).seq == record.seq


# AGENTS.md: one function per transition, named after the TLA+ action


@pytest.mark.parametrize("name", list(Action))
def test_one_function_per_action(name: Action) -> None:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name.value).lower()
    assert callable(getattr(transitions, snake, None)), snake


def test_rules_match_the_spec() -> None:
    assert {a: (r.sources, r.target) for a, r in transitions.RULES.items()} == {
        a: edge for a, edge in EDGES.items() if a is not A.START_RUN
    }
    assert {a: r.role for a, r in transitions.RULES.items()} == {
        a: role for a, role in ROLE_OF.items() if a is not A.START_RUN
    }
    assert transitions.RULES[A.APPROVE_SPEC].freeze == frozenset({Artifact.SPEC})
