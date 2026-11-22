"""Requirement "Approvals record the reviewed commit" (run-lifecycle, from the
gate-runner change), plus how the commits are stored (design.md, D6)."""

import json
from pathlib import Path

import pytest

from refinery.run import eventlog, record, transitions
from refinery.run.model import Action, Artifact, Phase, Refused, RunRecord
from tests.run.helpers import (
    HAPPY_PATH,
    SPEC_COMMIT,
    TESTS_COMMIT,
    accepted,
    drive,
    request,
    started,
)

A = Action
P = Phase
S = "1" * 40
T = "2" * 40


def at_review(phase: Phase) -> RunRecord:
    rec = started().record
    path = iter(HAPPY_PATH)
    while rec.phase is not phase:
        rec, _ = drive(rec, [next(path)])
    return rec


@pytest.mark.parametrize(
    "commit", [None, "", "abc123", "A" * 40, "1" * 39, "1" * 41, "z" * 40]
)
@pytest.mark.parametrize(
    ("phase", "name"),
    [(P.SPEC_REVIEW, A.APPROVE_SPEC), (P.TESTS_REVIEW, A.APPROVE_TESTS)],
)
def test_approval_without_a_valid_commit_is_refused(
    phase: Phase, name: Action, commit: str | None
) -> None:
    rec = at_review(phase)
    outcome = transitions.apply(rec, request(name, commit=commit))
    assert isinstance(outcome, Refused)
    assert rec.phase is phase


def test_approved_commits_are_recorded() -> None:
    rec = at_review(P.SPEC_REVIEW)
    spec = accepted(transitions.apply(rec, request(A.APPROVE_SPEC, commit=S)))
    assert spec.event.commit == S
    assert (spec.record.spec_commit, spec.record.tests_commit) == (S, None)
    rec, _ = drive(spec.record, [A.COMPLETE_TESTS])
    tests = accepted(transitions.apply(rec, request(A.APPROVE_TESTS, commit=T)))
    assert tests.event.commit == T
    assert (tests.record.spec_commit, tests.record.tests_commit) == (S, T)


def test_default_approvals_use_the_helper_commits() -> None:
    rec = at_review(P.IMPL)
    assert (rec.spec_commit, rec.tests_commit) == (SPEC_COMMIT, TESTS_COMMIT)


def _escalated() -> RunRecord:
    rec, _ = drive(started().record, (*HAPPY_PATH[:5], A.ESCALATE))
    assert rec.phase is P.ESCALATED
    return rec


def test_resolving_to_tests_clears_the_tests_commit() -> None:
    rec, _ = drive(_escalated(), [A.RESOLVE_TO_TESTS])
    assert rec.frozen == frozenset({Artifact.SPEC})
    assert (rec.spec_commit, rec.tests_commit) == (SPEC_COMMIT, None)


def test_resolving_to_spec_clears_both_commits() -> None:
    rec, _ = drive(_escalated(), [A.RESOLVE_TO_SPEC])
    assert (rec.spec_commit, rec.tests_commit) == (None, None)


def test_resolving_to_impl_keeps_both_commits() -> None:
    rec, _ = drive(_escalated(), [A.RESOLVE_TO_IMPL])
    assert (rec.spec_commit, rec.tests_commit) == (SPEC_COMMIT, TESTS_COMMIT)


def test_reapproval_replaces_the_commit() -> None:
    rec, _ = drive(_escalated(), [A.RESOLVE_TO_TESTS, A.COMPLETE_TESTS])
    again = accepted(transitions.apply(rec, request(A.APPROVE_TESTS, commit=T)))
    assert again.record.tests_commit == T


def test_record_round_trips_approved_commits(tmp_path: Path) -> None:
    rec = at_review(P.IMPL)
    path = tmp_path / "r.json"
    record.write_record(path, rec)
    data = json.loads(path.read_text())
    assert data["approved"] == {"spec": SPEC_COMMIT, "tests": TESTS_COMMIT}
    assert record.read_record(path) == rec


def test_record_without_approved_reads_as_none(tmp_path: Path) -> None:
    rec = started().record
    path = tmp_path / "r.json"
    record.write_record(path, rec)
    data = json.loads(path.read_text())
    data.pop("approved", None)
    path.write_text(json.dumps(data))
    assert record.read_record(path) == rec


def test_event_round_trips_commit(tmp_path: Path) -> None:
    rec = at_review(P.SPEC_REVIEW)
    event = accepted(transitions.apply(rec, request(A.APPROVE_SPEC, commit=S))).event
    log = tmp_path / "events.jsonl"
    eventlog.append_event(log, event)
    assert json.loads(log.read_text())["commit"] == S
    assert eventlog.read_events(log) == [event]
