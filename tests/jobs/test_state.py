"""The job log and the job state fold (agent-jobs; runner D4), as unit tests
and hypothesis properties mirroring spec/Runner.tla.

The properties drive random but valid job histories, the way factoryd would
write them, alongside a reference model with Runner.tla's semantics: a run
that moves resets its failures; a finish counts as a failure only if the run
did not move during the job; releases never count; Retry resets.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from refinery.jobs import log
from refinery.jobs.log import Claim, Finish, JobEvent, ReleaseReason, Retry
from refinery.jobs.log import Release as ReleaseEvent
from refinery.jobs.state import (
    AGENT_PHASES,
    DEFAULT_MAX_ATTEMPTS,
    JobState,
    fold,
    is_stuck,
    job_state,
    wants_agent,
)
from refinery.run.model import Phase

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
RUN = "demo.1"
P = Phase


def at(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


def claim(n: int, seq: int, runner: str = "x0000001") -> Claim:
    return Claim(f"{RUN}.j{n}", runner, f"t000000{n}", seq, at(n))


def finish(n: int, seq: int, code: int = 0) -> Finish:
    return Finish(f"{RUN}.j{n}", "exited", code, f"/logs/j{n}", seq, at(n))


def release(n: int, why: ReleaseReason = ReleaseReason.RUNNER_RESTART) -> ReleaseEvent:
    return ReleaseEvent(f"{RUN}.j{n}", why, at(n))


# Requirement: The job log


@pytest.mark.parametrize(
    ("event", "name"),
    [
        (claim(1, 3), "Claim"),
        (finish(1, 3, code=1), "Finish"),
        (release(1, ReleaseReason.LOST), "Release"),
        (Retry(at(5)), "Retry"),
    ],
)
def test_event_names_match_runner_tla(event: JobEvent, name: str) -> None:
    line = log.to_json(event)
    assert "\n" not in line
    assert json.loads(line)["event"] == name
    assert log.from_json(line) == event


def test_append_then_read(tmp_path: Path) -> None:
    path = log.log_path(tmp_path, RUN)
    events: list[JobEvent] = [claim(1, 1), finish(1, 1), Retry(at(2))]
    for event in events:
        log.append(path, event)
    assert path == tmp_path / "runs" / RUN / "jobs.jsonl"
    assert log.read(path) == events
    assert len(path.read_text().splitlines()) == 3


def test_no_log_reads_empty(tmp_path: Path) -> None:
    assert log.read(log.log_path(tmp_path, RUN)) == []


def test_append_failure_is_a_job_log_error(tmp_path: Path) -> None:
    path = log.log_path(tmp_path, RUN)
    path.mkdir(parents=True)  # a directory where the log should be
    with pytest.raises(log.JobLogError):
        log.append(path, claim(1, 1))


# Requirement: Failed attempts and stuck runs (examples)


def test_no_events() -> None:
    view = fold([], current_seq=1)
    assert frozenset({P.SPEC, P.MODEL, P.TESTS, P.IMPL}) == AGENT_PHASES
    assert (view.open, view.attempts, view.claims) == (None, 0, 0)
    assert wants_agent(P.SPEC, view, DEFAULT_MAX_ATTEMPTS)
    assert job_state(P.SPEC, view, DEFAULT_MAX_ATTEMPTS) is JobState.WAITING


def test_open_job() -> None:
    view = fold([claim(1, 1)], current_seq=1)
    assert view.open is not None
    assert (view.open.job, view.open.runner, view.open.claim_seq) == (
        "demo.1.j1",
        "x0000001",
        1,
    )
    assert not wants_agent(P.SPEC, view, DEFAULT_MAX_ATTEMPTS)
    assert job_state(P.SPEC, view, DEFAULT_MAX_ATTEMPTS) is JobState.RUNNING


def test_progress_is_not_a_failure() -> None:
    # Claimed at seq 1 in spec; the agent submitted (seq 2, model); finished.
    view = fold([claim(1, 1), finish(1, 2)], current_seq=2)
    assert view.open is None
    assert view.attempts == 0
    assert wants_agent(P.MODEL, view, DEFAULT_MAX_ATTEMPTS)


def test_two_failures_make_a_run_stuck() -> None:
    events = [claim(1, 4), finish(1, 4, code=1), claim(2, 4), finish(2, 4, code=1)]
    view = fold(events, current_seq=4)
    assert view.attempts == 2
    assert is_stuck(view, DEFAULT_MAX_ATTEMPTS)
    assert not wants_agent(P.IMPL, view, DEFAULT_MAX_ATTEMPTS)
    assert job_state(P.IMPL, view, DEFAULT_MAX_ATTEMPTS) is JobState.STUCK
    assert view.last_log == "/logs/j2"
    assert view.last_finish_at == at(2)


def test_one_failure_is_not_stuck() -> None:
    view = fold([claim(1, 4), finish(1, 4, code=1)], current_seq=4)
    assert view.attempts == 1
    assert wants_agent(P.IMPL, view, DEFAULT_MAX_ATTEMPTS)


def test_a_released_job_is_not_an_attempt() -> None:
    events = [
        claim(1, 4),
        finish(1, 4, code=1),
        claim(2, 4),
        release(2, ReleaseReason.RUNNER_SHUTDOWN),
    ]
    view = fold(events, current_seq=4)
    assert view.attempts == 1
    assert view.open is None
    assert wants_agent(P.IMPL, view, DEFAULT_MAX_ATTEMPTS)


def test_retry_resets_the_attempts() -> None:
    events: list[JobEvent] = [
        claim(1, 4),
        finish(1, 4),
        claim(2, 4),
        finish(2, 4),
        Retry(at(3)),
    ]
    view = fold(events, current_seq=4)
    assert view.attempts == 0
    assert wants_agent(P.IMPL, view, DEFAULT_MAX_ATTEMPTS)


def test_a_new_turn_starts_from_zero() -> None:
    # Two failures in impl at seq 4, then the human cancelled and resolved...
    # any move of the run (seq 5) starts a new count.
    events = [claim(1, 4), finish(1, 4), claim(2, 4), finish(2, 4)]
    view = fold(events, current_seq=5)
    assert view.attempts == 0
    assert view.claims == 2


def test_attempts_limit_is_a_parameter() -> None:
    events = [claim(1, 4), finish(1, 4), claim(2, 4), finish(2, 4)]
    view = fold(events, current_seq=4)
    assert not is_stuck(view, 3)
    assert is_stuck(view, 2)


@pytest.mark.parametrize(
    "phase",
    [p for p in Phase if p not in AGENT_PHASES],
)
def test_only_agent_phases_want_an_agent(phase: Phase) -> None:
    view = fold([], current_seq=1)
    assert not wants_agent(phase, view, DEFAULT_MAX_ATTEMPTS)
    assert job_state(phase, view, DEFAULT_MAX_ATTEMPTS) is JobState.NONE


def test_open_job_after_the_run_left_the_agent_turn_is_still_running() -> None:
    # The agent submitted into review; its runner has not finished the job yet.
    view = fold([claim(1, 1)], current_seq=3)
    assert job_state(P.SPEC_REVIEW, view, DEFAULT_MAX_ATTEMPTS) is JobState.RUNNING


# Properties mirroring Runner.tla


RUNNERS = ("x0000001", "x0000002")
OPS = ("claim", "finish", "release", "retry", "move")


@dataclass
class Reference:
    """Runner.tla's view of one run: its seq, open job, failures."""

    max_attempts: int
    seq: int = 1
    open: tuple[str, str, int] | None = None  # job, runner, claim seq
    failures: int = 0
    claims: int = 0
    events: list[JobEvent] = field(default_factory=list[JobEvent])

    @property
    def stuck(self) -> bool:
        return self.failures >= self.max_attempts

    def step(self, op: str, runner: str, minute: int) -> None:
        """Apply ``op`` if factoryd could have recorded it now; else skip it."""
        now = at(minute)
        if op == "claim" and self.open is None and not self.stuck:
            self.claims += 1
            job = f"{RUN}.j{self.claims}"
            self.open = (job, runner, self.seq)
            self.events.append(Claim(job, runner, f"tok{self.claims}", self.seq, now))
        elif op == "finish" and self.open is not None:
            job, _, claim_seq = self.open
            if claim_seq == self.seq:  # Runner.tla Finish: ~moved
                self.failures += 1
            self.events.append(Finish(job, "exited", 1, "/log", self.seq, now))
            self.open = None
        elif op == "release" and self.open is not None:
            why = ReleaseReason.LOST if minute % 2 else ReleaseReason.RUNNER_RESTART
            self.events.append(ReleaseEvent(self.open[0], why, now))
            self.open = None
        elif op == "retry" and self.stuck:
            self.failures = 0
            self.events.append(Retry(now))
        elif op == "move":  # Progress, Review or End: the run's seq advances
            self.seq += 1
            self.failures = 0


histories = st.lists(
    st.tuples(st.sampled_from(OPS), st.sampled_from(RUNNERS)), max_size=40
)


def replay(ops: list[tuple[str, str]], max_attempts: int) -> list[Reference]:
    """The reference after each step."""
    ref = Reference(max_attempts)
    states: list[Reference] = []
    for minute, (op, runner) in enumerate(ops):
        ref.step(op, runner, minute)
        states.append(
            Reference(
                max_attempts,
                ref.seq,
                ref.open,
                ref.failures,
                ref.claims,
                list(ref.events),
            )
        )
    return states


@settings(max_examples=300, deadline=None)
@given(histories, st.integers(min_value=1, max_value=3))
def test_fold_matches_runner_tla(ops: list[tuple[str, str]], max_attempts: int) -> None:
    for ref in replay(ops, max_attempts):
        view = fold(ref.events, ref.seq)
        # OneOpenJob: the fold finds exactly the reference's open job.
        opened = None if view.open is None else (view.open.job, view.open.runner)
        expected = None if ref.open is None else (ref.open[0], ref.open[1])
        assert opened == expected
        # Failed attempts: only finishes of jobs during which nothing moved.
        assert view.attempts == ref.failures
        assert view.claims == ref.claims
        assert is_stuck(view, max_attempts) == ref.stuck


@settings(max_examples=300, deadline=None)
@given(histories, st.integers(min_value=1, max_value=3))
def test_stuck_runs_have_no_job_and_want_no_agent(
    ops: list[tuple[str, str]], max_attempts: int
) -> None:
    for ref in replay(ops, max_attempts):
        view = fold(ref.events, ref.seq)
        if is_stuck(view, max_attempts):
            assert view.open is None  # StuckRunsHaveNoJob
            assert not wants_agent(P.IMPL, view, max_attempts)  # no claim when stuck
            assert job_state(P.IMPL, view, max_attempts) is JobState.STUCK
        wanted = view.open is None and not is_stuck(view, max_attempts)
        assert wants_agent(P.TESTS, view, max_attempts) == wanted
        assert not wants_agent(P.TESTS_REVIEW, view, max_attempts)


@settings(max_examples=300, deadline=None)
@given(histories)
def test_releases_are_never_attempts(ops: list[tuple[str, str]]) -> None:
    for ref in replay(ops, DEFAULT_MAX_ATTEMPTS):
        if ref.open is None:
            continue
        before = fold(ref.events, ref.seq)
        for why in ReleaseReason:
            released = [*ref.events, ReleaseEvent(ref.open[0], why, at(99))]
            after = fold(released, ref.seq)
            assert after.attempts == before.attempts
            assert after.open is None


@settings(max_examples=200, deadline=None)
@given(histories)
def test_the_log_round_trips(ops: list[tuple[str, str]]) -> None:
    refs = replay(ops, DEFAULT_MAX_ATTEMPTS)
    events = refs[-1].events if refs else []
    assert [log.from_json(log.to_json(e)) for e in events] == events
