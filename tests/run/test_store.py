"""Scenarios of specs/run-record/spec.md (and the store-level parts of
specs/run-lifecycle/spec.md) against RunStore and the record/log files."""

import asyncio
import dataclasses
import hashlib
import json
import os
import stat
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from refinery.run import eventlog, record
from refinery.run.archive import ReplayValidator, ValidationResult
from refinery.run.model import (
    Accepted,
    Action,
    Event,
    Phase,
    Refused,
    RunRecord,
    TraceResult,
    Verdict,
)
from refinery.run.store import RunStore
from tests.run.helpers import HAPPY_PATH, accepted, at, request, run_async

A = Action
P = Phase
CHANGE = "run-lifecycle"
RUN = "run-lifecycle.1"


class Dirs:
    """A data directory and one worktree, both under pytest's tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.worktree = tmp_path / "worktree"
        self.worktree.mkdir()

    async def store(self) -> RunStore:
        return await RunStore.open(self.data, [self.worktree])

    def record_file(self, run_id: str = RUN) -> Path:
        return self.worktree / ".refinery" / "runs" / f"{run_id}.json"

    def log_file(self, run_id: str = RUN) -> Path:
        return self.data / "runs" / run_id / "events.jsonl"


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    return Dirs(tmp_path)


async def start(store: RunStore, dirs: Dirs, change: str = CHANGE) -> RunRecord:
    return accepted(
        await store.start_run(change, dirs.worktree, request(A.START_RUN))
    ).record


async def advance(store: RunStore, run_id: str, actions: Sequence[Action]) -> RunRecord:
    rec = store.get(run_id)
    for minute, name in enumerate(actions, start=rec.seq):
        rec = accepted(
            await store.transition(run_id, request(name, minute=minute))
        ).record
    return rec


async def to_archive(store: RunStore, dirs: Dirs) -> RunRecord:
    await start(store, dirs)
    return await advance(store, RUN, HAPPY_PATH)


def log_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class FixedValidator:
    """Returns a fixed verdict and remembers what it was asked."""

    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.seen: list[Event] = []

    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        self.seen = list(events)
        return ValidationResult(self.passed, "" if self.passed else "not a behaviour")


class RaisingValidator:
    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        raise RuntimeError("TLC crashed")


class SlowValidator:
    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        await asyncio.sleep(10)
        return ValidationResult(True)


# Requirement: Run record file


def test_record_after_start(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        rec = await start(store, dirs)
        assert rec.run_id == RUN
        store.close()

    run_async(go())
    text = dirs.record_file().read_text()
    data = json.loads(text)
    assert data["schema"] == 1
    assert data["run_id"] == RUN
    assert data["change"] == CHANGE
    assert data["phase"] == "spec"
    assert data["seq"] == 1
    assert data["frozen"] == []
    assert data["trace"] == {"verdict": "none", "events": None, "sha256": None}
    assert datetime.fromisoformat(data["started_at"]) == at(0)
    # D3: stable, reviewable diffs.
    assert list(data) == sorted(data)
    assert text.endswith("\n")


def test_records_of_different_runs_do_not_collide(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        a = await start(store, dirs, "a")
        b = await start(store, dirs, "b")
        assert (a.run_id, b.run_id) == ("a.1", "b.1")
        await advance(store, "a.1", [A.COMPLETE_SPEC])
        store.close()

    run_async(go())
    assert record.read_record(dirs.record_file("a.1")).phase is P.MODEL
    assert record.read_record(dirs.record_file("b.1")).phase is P.SPEC


def test_record_path_layout(tmp_path: Path) -> None:
    assert record.record_path(tmp_path, "x.3") == tmp_path / ".refinery/runs/x.3.json"
    assert eventlog.log_path(tmp_path, "x.3") == tmp_path / "runs/x.3/events.jsonl"


# Requirement: Run record writes are atomic


def test_crash_during_record_write_leaves_previous_record(
    dirs: Dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> RunRecord:
        store = await dirs.store()
        rec = await start(store, dirs)
        store.close()
        return rec

    first = run_async(go())
    path = dirs.record_file()
    before = path.read_bytes()

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os, "replace", failing_replace)
    changed = dataclasses.replace(first, phase=P.MODEL, seq=2)
    with pytest.raises(OSError, match="simulated"):
        record.write_record(path, changed)
    assert path.read_bytes() == before
    assert record.read_record(path) == first


def test_record_round_trips(dirs: Dirs) -> None:
    async def go() -> RunRecord:
        store = await dirs.store()
        await to_archive(store, dirs)
        rec = store.get(RUN)
        store.close()
        return rec

    rec = run_async(go())
    assert record.read_record(dirs.record_file()) == rec


# Requirement: Event log


def test_event_matches_the_transition(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        await advance(store, RUN, HAPPY_PATH[:3])  # ApproveSpec is the 4th
        store.close()

    run_async(go())
    fourth = log_lines(dirs.log_file())[3]
    assert fourth["seq"] == 4
    assert fourth["name"] == "ApproveSpec"
    assert (fourth["from"], fourth["to"]) == ("spec_review", "tests")
    assert fourth["actor"] == "human"
    assert datetime.fromisoformat(str(fourth["at"])) == at(3)


def test_log_is_outside_the_worktree(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        store.close()

    run_async(go())
    assert dirs.log_file().exists()
    assert not any(p.name == "events.jsonl" for p in dirs.worktree.rglob("*"))


def test_rejection_note_is_in_the_log(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        await advance(store, RUN, [A.COMPLETE_SPEC, A.COMPLETE_MODEL])
        accepted(
            await store.transition(
                RUN, request(A.REJECT_SPEC, minute=9, note="scenario 3 has no THEN")
            )
        )
        store.close()

    run_async(go())
    assert log_lines(dirs.log_file())[-1]["note"] == "scenario 3 has no THEN"


def test_refused_transitions_are_not_logged(dirs: Dirs) -> None:
    async def go() -> Refused:
        store = await dirs.store()
        await start(store, dirs)
        outcome = await store.transition(RUN, request(A.COMPLETE_IMPL))
        store.close()
        assert isinstance(outcome, Refused)
        return outcome

    run_async(go())
    assert len(log_lines(dirs.log_file())) == 1
    assert record.read_record(dirs.record_file()).seq == 1


def test_unknown_run_is_refused(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        outcome = await store.transition("nope.1", request(A.CANCEL))
        assert isinstance(outcome, Refused)
        store.close()

    run_async(go())


@pytest.mark.parametrize("name", [A.START_RUN, A.ARCHIVE])
def test_transition_refuses_start_and_archive(dirs: Dirs, name: Action) -> None:
    async def go() -> None:
        store = await dirs.store()
        await to_archive(store, dirs)
        outcome = await store.transition(RUN, request(name))
        assert isinstance(outcome, Refused)
        store.close()

    run_async(go())


# Requirement: Event is durable before the record


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_log_write_failure_refuses_the_transition(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        log_dir = dirs.log_file().parent
        before = dirs.record_file().read_bytes()
        dirs.log_file().chmod(0o444)
        log_dir.chmod(0o555)
        try:
            outcome = await store.transition(RUN, request(A.COMPLETE_SPEC))
        finally:
            log_dir.chmod(0o755)
            dirs.log_file().chmod(0o644)
        assert isinstance(outcome, Refused)
        assert dirs.record_file().read_bytes() == before
        assert store.get(RUN).phase is P.SPEC
        store.close()

    run_async(go())


def test_appended_event_is_on_disk_before_the_record(
    dirs: Dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the record is replaced, the event must already be in the log."""

    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        real_replace = os.replace
        seen: list[int] = []

        def spying_replace(src: str | Path, dst: str | Path) -> None:
            if str(dst).endswith(f"{RUN}.json"):
                seen.append(len(dirs.log_file().read_text().splitlines()))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spying_replace)
        await advance(store, RUN, [A.COMPLETE_SPEC])
        assert seen == [2]
        store.close()

    run_async(go())


# Requirement: Crash recovery


def test_crash_between_event_and_record(dirs: Dirs) -> None:
    async def go() -> RunRecord:
        store = await dirs.store()
        await start(store, dirs)
        rec = await advance(store, RUN, HAPPY_PATH[:3])  # record seq 4
        store.close()
        return rec

    rec = run_async(go())
    stray = Event(
        seq=5,
        name=A.COMPLETE_TESTS,
        from_phase=P.TESTS,
        to_phase=P.TESTS_REVIEW,
        actor=request(A.COMPLETE_TESTS).actor,
        at=at(4),
    )
    eventlog.append_event(dirs.log_file(), stray)  # event 5 written, record not

    async def reopen() -> None:
        store = await dirs.store()
        assert [e.seq for e in store.events(RUN)] == [1, 2, 3, 4]
        assert store.get(RUN).phase is P.TESTS
        store.close()

    run_async(reopen())
    assert [e.seq for e in eventlog.read_events(dirs.log_file())] == [1, 2, 3, 4]
    assert record.read_record(dirs.record_file()) == rec


def test_truncate_events_reports_dropped(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    for seq in (1, 2, 3):
        eventlog.append_event(
            path,
            Event(seq, A.CANCEL, P.SPEC, P.CANCELLED, request(A.CANCEL).actor, at(seq)),
        )
    assert eventlog.truncate_events(path, 1) == 2
    assert [e.seq for e in eventlog.read_events(path)] == [1]


# Requirement: Trace validation at archive


def test_valid_trace_archives_the_run(dirs: Dirs) -> None:
    async def go() -> RunRecord:
        store = await dirs.store()
        await to_archive(store, dirs)
        validator = FixedValidator(passed=True)
        rec = accepted(await store.archive(RUN, validator, at(20), 5.0)).record
        # The validator saw the complete log, its own Archive event included.
        assert [e.name for e in validator.seen][-1] is A.ARCHIVE
        assert len(validator.seen) == 10
        store.close()
        return rec

    rec = run_async(go())
    data = dirs.log_file().read_bytes()
    assert rec.phase is P.ARCHIVED
    assert rec.trace == TraceResult(
        verdict=Verdict.PASS, events=10, sha256=hashlib.sha256(data).hexdigest()
    )
    assert record.read_record(dirs.record_file()) == rec


def test_replay_validator_archives_a_real_run(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await to_archive(store, dirs)
        outcome = await store.archive(RUN, ReplayValidator(), at(20), 5.0)
        assert accepted(outcome).record.phase is P.ARCHIVED
        store.close()

    run_async(go())


@pytest.mark.parametrize(
    "validator",
    [FixedValidator(passed=False), RaisingValidator(), SlowValidator()],
    ids=["invalid", "error", "timeout"],
)
def test_failed_validation_blocks_archive(
    dirs: Dirs, validator: FixedValidator | RaisingValidator | SlowValidator
) -> None:
    async def go() -> None:
        store = await dirs.store()
        before = await to_archive(store, dirs)
        outcome = await store.archive(RUN, validator, at(20), 0.2)
        assert isinstance(outcome, Refused)
        rec = store.get(RUN)
        assert rec.phase is P.ARCHIVE
        assert rec.seq == before.seq
        assert rec.trace.verdict is Verdict.FAIL
        assert A.ARCHIVE not in [e.name for e in store.events(RUN)]
        store.close()

    run_async(go())
    assert record.read_record(dirs.record_file()).trace.verdict is Verdict.FAIL
    assert "Archive" not in [line["name"] for line in log_lines(dirs.log_file())]


def test_retry_after_a_fix(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await to_archive(store, dirs)
        assert isinstance(
            await store.archive(RUN, FixedValidator(passed=False), at(20), 5.0), Refused
        )
        rec = accepted(await store.transition(RUN, request(A.RETRY_ARCHIVE, minute=21)))
        assert rec.record.trace.verdict is Verdict.NONE
        done = accepted(await store.archive(RUN, ReplayValidator(), at(22), 5.0))
        assert done.record.phase is P.ARCHIVED
        assert [e.name for e in store.events(RUN)][-2:] == [A.RETRY_ARCHIVE, A.ARCHIVE]
        store.close()

    run_async(go())


def test_archive_refused_outside_archive_phase(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        outcome = await store.archive(RUN, FixedValidator(passed=True), at(5), 5.0)
        assert isinstance(outcome, Refused)
        assert len(store.events(RUN)) == 1
        store.close()

    run_async(go())


# Requirement: Sealed event log


def test_archived_log_is_sealed_and_verifiable(dirs: Dirs) -> None:
    async def go() -> RunRecord:
        store = await dirs.store()
        await to_archive(store, dirs)
        rec = accepted(await store.archive(RUN, ReplayValidator(), at(20), 5.0)).record
        assert isinstance(await store.transition(RUN, request(A.CANCEL)), Refused)
        store.close()
        return rec

    rec = run_async(go())
    path = dirs.log_file()
    assert path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert rec.trace.events is not None
    assert rec.trace.sha256 is not None
    assert eventlog.verify_log(path, rec.trace.events, rec.trace.sha256)


def test_tampered_log_is_detected(dirs: Dirs) -> None:
    async def go() -> RunRecord:
        store = await dirs.store()
        await to_archive(store, dirs)
        rec = accepted(await store.archive(RUN, ReplayValidator(), at(20), 5.0)).record
        store.close()
        return rec

    rec = run_async(go())
    path = dirs.log_file()
    path.chmod(0o644)
    path.write_text(path.read_text().replace('"human"', '"agent"', 1))
    assert rec.trace.events is not None
    assert rec.trace.sha256 is not None
    assert not eventlog.verify_log(path, rec.trace.events, rec.trace.sha256)


def test_digest_matches_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    eventlog.append_event(
        path, Event(1, A.START_RUN, P.NONE, P.SPEC, request(A.START_RUN).actor, at(0))
    )
    digest = eventlog.digest_log(path)
    assert digest.events == 1
    assert digest.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


# Requirement: One unfinished run per change (store level)


def test_second_start_is_refused(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        await advance(store, RUN, HAPPY_PATH[:3])  # in tests
        outcome = await store.start_run(CHANGE, dirs.worktree, request(A.START_RUN))
        assert isinstance(outcome, Refused)
        assert RUN in outcome.reason
        assert not dirs.record_file("run-lifecycle.2").exists()
        store.close()

    run_async(go())


def test_start_after_cancellation_gets_a_new_run_id(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        await advance(store, RUN, [A.CANCEL])
        again = await start(store, dirs)
        assert again.run_id == "run-lifecycle.2"
        assert again.phase is P.SPEC
        assert store.get(RUN).phase is P.CANCELLED
        store.close()

    run_async(go())


def test_concurrent_starts(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        outcomes = await asyncio.gather(
            *(
                store.start_run(CHANGE, dirs.worktree, request(A.START_RUN))
                for _ in range(5)
            )
        )
        winners = [o for o in outcomes if isinstance(o, Accepted)]
        assert len(winners) == 1
        assert all(
            winners[0].record.run_id in o.reason
            for o in outcomes
            if isinstance(o, Refused)
        )
        store.close()

    run_async(go())


def test_run_ids_survive_a_restart(dirs: Dirs) -> None:
    async def go() -> None:
        store = await dirs.store()
        await start(store, dirs)
        await advance(store, RUN, [A.CANCEL])
        store.close()
        store = await dirs.store()
        assert (await start(store, dirs)).run_id == "run-lifecycle.2"
        store.close()

    run_async(go())
